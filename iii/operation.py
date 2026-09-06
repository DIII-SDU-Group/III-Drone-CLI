"""Atomic operation planning and durable state for the universal CLI runner."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from typing import Any, Mapping, Sequence
from uuid import uuid4


OPERATION_SCHEMA = "iii.cli-operation-state/v1"
PLAN_SCHEMA = "iii.cli-operation-plan/v1"
OPERATION_ID = re.compile(r"^[a-z0-9][a-z0-9-]{7,63}$")


class OperationError(RuntimeError):
    code = "III_OPERATION_ERROR"


class OperationConflict(OperationError):
    code = "III_OPERATION_CONFLICT"


class OperationNotFound(OperationError):
    code = "III_OPERATION_NOT_FOUND"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def operation_id() -> str:
    return f"iii-{uuid4().hex[:24]}"


def content_id(value: Mapping[str, Any]) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def validate_plan(value: Mapping[str, Any]) -> None:
    if value.get("schema") != PLAN_SCHEMA:
        raise OperationError("unsupported retained operation plan")
    identifier = value.get("operation_id")
    if not isinstance(identifier, str) or not OPERATION_ID.fullmatch(identifier):
        raise OperationError("retained operation plan has an invalid operation ID")
    plan_identity = value.get("plan_id")
    unsigned = {key: item for key, item in value.items() if key != "plan_id"}
    if not isinstance(plan_identity, str) or content_id(unsigned) != plan_identity:
        raise OperationConflict("retained operation plan content identity mismatch")


def validate_state(value: Mapping[str, Any]) -> None:
    if value.get("schema") != OPERATION_SCHEMA:
        raise OperationError("unsupported retained operation state")
    identifier = value.get("operation_id")
    if not isinstance(identifier, str) or not OPERATION_ID.fullmatch(identifier):
        raise OperationError("retained operation state has an invalid operation ID")


def default_state_root(environment: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environment is None else environment
    explicit = env.get("III_OPERATION_STATE_DIR")
    if explicit:
        return Path(explicit).expanduser()
    registry = env.get("III_REGISTRY_ROOT")
    if registry:
        return Path(registry).expanduser() / "operations"
    workspace = env.get("WORKSPACE_DIR")
    if workspace:
        return Path(workspace).expanduser() / ".iii" / "operations"
    xdg_state = env.get("XDG_STATE_HOME")
    base = (
        Path(xdg_state).expanduser() if xdg_state else Path.home() / ".local" / "state"
    )
    return base / "iii" / "operations"


def create_plan(
    *,
    identifier: str,
    argv: Sequence[str],
    command: str,
    mutating: bool,
    target: str | None,
    profile: str | None,
    release_id: str | None,
    preflight: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if not OPERATION_ID.fullmatch(identifier):
        raise OperationError("invalid operation ID")
    unsigned: dict[str, Any] = {
        "schema": PLAN_SCHEMA,
        "operation_id": identifier,
        "created_at": utc_now(),
        "command": command,
        "argv": list(argv),
        "mutating": mutating,
        "context": {
            "target": target,
            "profile": profile,
            "release_id": release_id,
        },
    }
    if preflight is not None:
        unsigned["preflight"] = dict(preflight)
    unsigned["plan_id"] = content_id(unsigned)
    return unsigned


def initial_state(plan: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": OPERATION_SCHEMA,
        "operation_id": plan["operation_id"],
        "plan_id": plan["plan_id"],
        "state": "planned",
        "attempt": 0,
        "updated_at": utc_now(),
        "exit_code": None,
        "result_code": None,
        "evidence": [],
    }


@dataclass
class OperationStore:
    root: Path

    def _validate_id(self, identifier: str) -> None:
        if not OPERATION_ID.fullmatch(identifier):
            raise OperationError("invalid operation ID")

    def operation_path(self, identifier: str) -> Path:
        self._validate_id(identifier)
        return self.root / identifier

    def plan_path(self, identifier: str) -> Path:
        return self.operation_path(identifier) / "plan.json"

    def state_path(self, identifier: str) -> Path:
        return self.operation_path(identifier) / "state.json"

    def record_path(self, identifier: str, name: str) -> Path:
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}\.json", name):
            raise OperationError("invalid operation record name")
        return self.operation_path(identifier) / name

    def _read(self, path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        if path.is_symlink():
            raise OperationError(f"refusing symbolic-link operation data: {path.name}")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise OperationError(f"cannot read retained operation data: {exc}") from exc
        if not isinstance(value, dict):
            raise OperationError("retained operation data must be an object")
        return value

    def write_record(
        self, identifier: str, name: str, value: Mapping[str, Any]
    ) -> Path:
        path = self.record_path(identifier, name)
        self._atomic_write(path, value)
        return path

    def load_record(self, identifier: str, name: str) -> dict[str, Any] | None:
        return self._read(self.record_path(identifier, name))

    def list_operations(self) -> list[str]:
        if not self.root.exists():
            return []
        if self.root.is_symlink() or not self.root.is_dir():
            raise OperationError("operation-state root must be a real directory")
        values = []
        for path in sorted(self.root.iterdir(), key=lambda item: item.name):
            if path.name.startswith("."):
                continue
            if path.is_symlink() or not path.is_dir():
                raise OperationError("operation registry contains an unsafe entry")
            self._validate_id(path.name)
            values.append(path.name)
        return values

    def load_plan(self, identifier: str) -> dict[str, Any] | None:
        value = self._read(self.plan_path(identifier))
        if value is not None:
            validate_plan(value)
            if value["operation_id"] != identifier:
                raise OperationConflict("retained plan path and operation ID disagree")
        return value

    def load_state(self, identifier: str) -> dict[str, Any] | None:
        value = self._read(self.state_path(identifier))
        if value is not None:
            validate_state(value)
            if value["operation_id"] != identifier:
                raise OperationConflict("retained state path and operation ID disagree")
        return value

    def retain_plan(self, plan: Mapping[str, Any]) -> dict[str, Any]:
        validate_plan(plan)
        identifier = str(plan["operation_id"])
        existing = self.load_plan(identifier)
        if existing is not None and existing.get("plan_id") != plan.get("plan_id"):
            raise OperationConflict(
                "operation ID is bound to a different exact command plan"
            )
        if existing is None:
            self._atomic_write(self.plan_path(identifier), plan)
        state = self.load_state(identifier)
        if state is None:
            state = initial_state(plan)
            self.save_state(state)
        elif state.get("plan_id") != plan.get("plan_id"):
            raise OperationConflict(
                "retained operation state is bound to a different plan"
            )
        return state

    def save_state(self, state: Mapping[str, Any]) -> None:
        value = dict(state)
        value["updated_at"] = utc_now()
        validate_state(value)
        self._atomic_write(self.state_path(str(value["operation_id"])), value)

    def transition(
        self,
        identifier: str,
        state_name: str,
        *,
        exit_code: int | None = None,
        result_code: str | None = None,
        evidence: Sequence[str] = (),
        increment_attempt: bool = False,
    ) -> dict[str, Any]:
        state = self.load_state(identifier)
        if state is None:
            raise OperationNotFound(f"operation {identifier} is not retained")
        state["state"] = state_name
        state["exit_code"] = exit_code
        state["result_code"] = result_code
        if evidence:
            state["evidence"] = list(
                dict.fromkeys([*state.get("evidence", []), *evidence])
            )
        if increment_attempt:
            state["attempt"] = int(state.get("attempt", 0)) + 1
        self.save_state(state)
        return state

    def remove_operation(
        self, identifier: str, *, expected_records: Mapping[str, str]
    ) -> None:
        """Remove one exact operation snapshot while holding the registry lock."""

        root = self.operation_path(identifier)
        with self._locked():
            if root.parent != self.root or root.is_symlink() or not root.is_dir():
                raise OperationConflict("prune target escaped the operation registry")
            observed: dict[str, str] = {}
            for path in sorted(root.glob("*.json"), key=lambda item: item.name):
                value = self._read(path)
                if value is None:
                    raise OperationConflict("prune candidate record disappeared")
                observed[path.name] = content_id(value)
            if observed != dict(expected_records):
                raise OperationConflict(
                    "prune candidate changed after the retained preflight"
                )
            shutil.rmtree(root)
            descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def _atomic_write(self, path: Path, value: Mapping[str, Any]) -> None:
        if self.root.is_symlink():
            raise OperationError("refusing symbolic-link operation-state directory")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.is_symlink() or not self.root.is_dir():
            raise OperationError("operation-state root must be a real directory")
        if hasattr(os, "geteuid") and self.root.stat().st_uid != os.geteuid():
            raise OperationError(
                "operation-state root is not owned by the current user"
            )
        os.chmod(self.root, 0o700)
        with self._locked():
            self._atomic_write_locked(path, value)

    @contextmanager
    def _locked(self):
        lock = self.root / ".registry.lock"
        if lock.is_symlink():
            raise OperationError("refusing symbolic-link operation registry lock")
        descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            observed = os.fstat(descriptor)
            if hasattr(os, "geteuid") and observed.st_uid != os.geteuid():
                raise OperationError("operation registry lock is not user-owned")
            if observed.st_mode & 0o077:
                os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _atomic_write_locked(self, path: Path, value: Mapping[str, Any]) -> None:
        operation_root = path.parent
        if operation_root.parent != self.root:
            raise OperationError("operation record path escapes its fixed directory")
        if operation_root.is_symlink():
            raise OperationError("refusing symbolic-link operation directory")
        operation_root.mkdir(mode=0o700, exist_ok=True)
        if operation_root.is_symlink() or not operation_root.is_dir():
            raise OperationError("operation directory must be a real directory")
        if hasattr(os, "geteuid") and operation_root.stat().st_uid != os.geteuid():
            raise OperationError("operation directory is not owned by the current user")
        os.chmod(operation_root, 0o700)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
        data = json.dumps(dict(value), sort_keys=True, separators=(",", ":")) + "\n"
        try:
            with temporary.open("x", encoding="utf-8") as handle:
                os.chmod(temporary, 0o600)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            directory = os.open(operation_root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            root_descriptor = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(root_descriptor)
            finally:
                os.close(root_descriptor)
        finally:
            temporary.unlink(missing_ok=True)
