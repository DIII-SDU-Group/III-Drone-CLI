"""Verified aircraft log/diagnostic pull and exact receipt-backed pruning."""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
from typing import Any, Mapping

from .operation import OperationStore, content_id, default_state_root
from .registry import registry_root
from .result import CommandResult, Finding, NextAction, Outcome

CHUNK_BYTES = 512 * 1024


def _environment(args: argparse.Namespace) -> Mapping[str, str]:
    return getattr(args, "_iii_environment", os.environ)


def _registry_root(args: argparse.Namespace) -> Path:
    return registry_root(_environment(args))


def _destination(args: argparse.Namespace, domain: str) -> Path:
    selected = getattr(args, "destination", None)
    root = Path(selected).expanduser() if selected is not None else _registry_root(args)
    return root.absolute() / ("log-pulls" if domain == "logs" else "diagnostic-pulls")


def _ensure_directory(path: Path, *, mode: int) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if current.exists() or current.is_symlink():
            if current.is_symlink() or not current.is_dir():
                raise ValueError("local pull directory path is unsafe")
            continue
        current.mkdir(mode=mode)


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _safe_locator(value: Any) -> PurePosixPath:
    if not isinstance(value, str):
        raise ValueError("remote log locator is not text")
    if "\\" in value or "\x00" in value:
        raise ValueError("remote log locator contains a forbidden character")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("remote log locator is unsafe")
    return path


def _sha256(path: Path) -> tuple[str, int]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("local pulled content is missing, linked, or not regular")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    digest = hashlib.sha256()
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("local pulled content is not a regular file")
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            digest.update(block)
    finally:
        os.close(descriptor)
    return digest.hexdigest(), metadata.st_size


def _atomic_document(path: Path, value: Mapping[str, Any], *, mode: int) -> None:
    _ensure_directory(path.parent, mode=0o700)
    temporary = (
        path.parent / f".{path.name}.partial-{os.getpid()}-{os.urandom(8).hex()}"
    )
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            mode,
        )
        try:
            view = memoryview(_canonical(value) + b"\n")
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("local pull manifest write made no progress")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists() and not temporary.is_symlink():
            temporary.unlink()


def _read_document(path: Path, *, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} is missing or linked")
    raw = path.read_bytes()
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is invalid JSON") from exc
    if not isinstance(value, dict) or raw != _canonical(value) + b"\n":
        raise ValueError(f"{label} is not canonical JSON")
    return value


def _target(args: argparse.Namespace) -> dict[str, Any]:
    from .deploy import _require_remote, _target as deployment_target

    selected = deployment_target(args)
    _require_remote(selected)
    return selected


def _validate_contract(name: str, value: Mapping[str, Any]) -> None:
    from iii_deployment.contracts import ContractRegistry
    from .deploy import _workspace

    ContractRegistry(_workspace() / "deployment/schemas/v1").validate(name, value)


def _manager():
    from .deploy import _manager as deployment_manager

    return deployment_manager()


def _request(
    manager,
    *,
    action: str,
    operation_id: str,
    payload: Mapping[str, Any],
    nonce: str | None = None,
) -> dict[str, Any]:
    from .deploy import _request as deployment_request

    return deployment_request(
        manager,
        action=action,
        operation_id=operation_id,
        payload=payload,
        nonce=nonce,
    )


def _binding(selected: Mapping[str, Any]) -> dict[str, str]:
    return {
        "logical_id": str(selected["logical_id"]),
        "profile": str(selected["runtime_profile"]),
    }


def _manifest_identity(manifest: Mapping[str, Any]) -> str:
    return content_id(
        {
            key: item
            for key, item in manifest.items()
            if key not in {"manifest_id", "created_at"}
        }
    )


def _validate_source_manifest(manifest: Mapping[str, Any], domain: str) -> None:
    _validate_contract("log-export-manifest", manifest)
    if manifest.get("domain") != domain or manifest.get(
        "manifest_id"
    ) != _manifest_identity(manifest):
        raise ValueError("receiver log export manifest identity is invalid")


def pull_preflight(args: argparse.Namespace) -> Mapping[str, Any]:
    domain = str(args.log_domain)
    retained_id = getattr(args, "_iii_operation_id", None)
    if retained_id:
        existing = OperationStore(default_state_root(_environment(args))).load_plan(
            retained_id
        )
        existing_preflight = (
            existing.get("preflight") if isinstance(existing, dict) else None
        )
        if (
            isinstance(existing_preflight, dict)
            and existing_preflight.get("schema") == "iii.log-pull-preflight/v1"
            and existing_preflight.get("domain") == domain
        ):
            return existing_preflight
    selected = _target(args)
    manager = _manager()
    result = _request(
        manager,
        action="log-export",
        operation_id=f"iii-{domain}-export-plan",
        payload={"domain": domain},
    )
    manifest = result.get("manifest")
    if not isinstance(manifest, dict) or manifest.get("domain") != domain:
        raise ValueError("receiver returned an invalid log export manifest")
    _validate_source_manifest(manifest, domain)
    return {
        "schema": "iii.log-pull-preflight/v1",
        "domain": domain,
        "target": _binding(selected),
        "destination": str(_destination(args, domain)),
        "manifest": manifest,
    }


def _retained_manifest(args: argparse.Namespace) -> dict[str, Any]:
    retained = getattr(args, "_iii_retained_plan", None)
    preflight = retained.get("preflight") if isinstance(retained, dict) else None
    manifest = preflight.get("manifest") if isinstance(preflight, dict) else None
    if not isinstance(manifest, dict) or manifest.get("domain") != args.log_domain:
        raise ValueError("retained pull plan has no exact receiver manifest")
    _validate_source_manifest(manifest, str(args.log_domain))
    return manifest


def _file_path(root: Path, locator: str, *, create: bool = True) -> Path:
    relative = _safe_locator(locator)
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        if current.exists() or current.is_symlink():
            if current.is_symlink() or not current.is_dir():
                raise ValueError("local pull path contains an unsafe parent")
        elif create:
            current.mkdir(mode=0o700)
        else:
            raise ValueError("local pulled content parent is missing")
    path = current / relative.name
    if path.is_symlink():
        raise ValueError("local pull path is a symbolic link")
    return path


def _download_file(
    manager,
    *,
    operation_id: str,
    manifest_id: str,
    item: Mapping[str, Any],
    path: Path,
) -> None:
    expected_size = item["size"]
    if (
        not isinstance(expected_size, int)
        or isinstance(expected_size, bool)
        or expected_size < 0
    ):
        raise ValueError("remote log size is invalid")
    content_identity = item["content_id"]
    if not isinstance(content_identity, str) or len(content_identity) != 64:
        raise ValueError("remote log content identity is invalid")
    observed_size = path.stat(follow_symlinks=False).st_size if path.exists() else 0
    if observed_size > expected_size:
        path.unlink()
        observed_size = 0
    if observed_size == expected_size and path.exists():
        observed_identity, _size = _sha256(path)
        if observed_identity == content_identity:
            os.chmod(path, 0o440)
            return
        path.unlink()
        observed_size = 0
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW,
        0o600,
    )
    try:
        offset = observed_size
        while offset < expected_size:
            result = _request(
                manager,
                action="log-chunk",
                operation_id=operation_id,
                payload={
                    "manifest_id": manifest_id,
                    "content_id": content_identity,
                    "offset": offset,
                    "length": CHUNK_BYTES,
                },
            )
            chunk = result.get("chunk")
            if not isinstance(chunk, dict):
                raise ValueError("receiver returned an invalid log chunk")
            if (
                chunk.get("manifest_id") != manifest_id
                or chunk.get("content_id") != content_identity
                or chunk.get("offset") != offset
            ):
                raise ValueError("receiver log chunk binding mismatch")
            try:
                data = base64.b64decode(chunk.get("data", ""), validate=True)
            except (ValueError, TypeError) as exc:
                raise ValueError("receiver log chunk encoding is invalid") from exc
            if not data or offset + len(data) > expected_size:
                raise ValueError("receiver log chunk length is invalid")
            view = memoryview(data)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("local log write made no progress")
                view = view[written:]
            os.fsync(descriptor)
            offset += len(data)
            if bool(chunk.get("eof")) != (offset == expected_size):
                raise ValueError("receiver log chunk EOF marker is inconsistent")
    finally:
        os.close(descriptor)
    observed_identity, observed_size = _sha256(path)
    if observed_size != expected_size or observed_identity != content_identity:
        path.unlink()
        raise ValueError("downloaded log content failed local hash verification")
    os.chmod(path, 0o440)


def _validate_local_pull(
    root: Path,
    manifest: Mapping[str, Any],
    *,
    selected: Mapping[str, Any],
    domain: str,
) -> dict[str, Any]:
    source = _read_document(root / "source-manifest.json", label="source pull manifest")
    if source != manifest:
        raise ValueError("local source manifest differs from retained receiver plan")
    local = _read_document(root / "pull-manifest.json", label="local pull manifest")
    _validate_contract("local-log-pull", local)
    if local.get("local_manifest_id") != content_id(
        {
            key: item
            for key, item in local.items()
            if key not in {"local_manifest_id", "completed_at"}
        }
    ):
        raise ValueError("local pull manifest identity is invalid")
    expected_files = [
        {
            "locator": item["locator"],
            "content_id": item["content_id"],
            "size": item["size"],
        }
        for item in manifest["files"]
    ]
    if (
        local.get("source_manifest_id") != manifest["manifest_id"]
        or local.get("domain") != domain
        or local.get("target") != _binding(selected)
        or local.get("files") != expected_files
    ):
        raise ValueError("local pull manifest differs from the retained pull binding")
    for item in manifest["files"]:
        path = _file_path(root / "files", item["locator"], create=False)
        digest, size = _sha256(path)
        if digest != item["content_id"] or size != item["size"]:
            raise ValueError("existing local pull content failed verification")
    return local


def _materialize_pull(
    args: argparse.Namespace,
    manager,
    selected: Mapping[str, Any],
    manifest: Mapping[str, Any],
    operation_id: str,
) -> tuple[Path, dict[str, Any]]:
    destination = _destination(args, str(args.log_domain))
    _ensure_directory(destination, mode=0o700)
    manifest_id = manifest.get("manifest_id")
    if not isinstance(manifest_id, str) or len(manifest_id) != 64:
        raise ValueError("receiver log manifest identity is invalid")
    final = destination / manifest_id
    if final.exists() or final.is_symlink():
        if final.is_symlink() or not final.is_dir():
            raise ValueError("local pull identity is occupied by an unsafe entry")
        return final, _validate_local_pull(
            final,
            manifest,
            selected=selected,
            domain=str(args.log_domain),
        )
    partial = destination / f".{manifest_id}.partial"
    if partial.exists() or partial.is_symlink():
        if partial.is_symlink() or not partial.is_dir():
            raise ValueError("local pull partial is unsafe")
    else:
        partial.mkdir(mode=0o700)
    files_root = partial / "files"
    files_root.mkdir(exist_ok=True, mode=0o700)
    for item in manifest.get("files", []):
        if not isinstance(item, dict) or set(item) != {
            "locator",
            "content_id",
            "size",
            "protected",
        }:
            raise ValueError("receiver log manifest file inventory is invalid")
        _download_file(
            manager,
            operation_id=operation_id,
            manifest_id=manifest_id,
            item=item,
            path=_file_path(files_root, item["locator"]),
        )
    local = {
        "schema": "iii.local-log-pull/v1",
        "local_manifest_id": "0" * 64,
        "source_manifest_id": manifest_id,
        "domain": args.log_domain,
        "target": _binding(selected),
        "completed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "files": [
            {
                "locator": item["locator"],
                "content_id": item["content_id"],
                "size": item["size"],
            }
            for item in manifest["files"]
        ],
    }
    local["local_manifest_id"] = content_id(
        {
            key: item
            for key, item in local.items()
            if key not in {"local_manifest_id", "completed_at"}
        }
    )
    _validate_contract("local-log-pull", local)
    _atomic_document(partial / "source-manifest.json", manifest, mode=0o440)
    _atomic_document(partial / "pull-manifest.json", local, mode=0o440)
    os.replace(partial, final)
    directory = os.open(destination, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    for path in sorted(final.rglob("*"), reverse=True):
        if path.is_dir():
            os.chmod(path, 0o550)
    os.chmod(final, 0o550)
    return final, local


def _rejection(command: str, exc: Exception, selected=None) -> CommandResult:
    code = getattr(exc, "code", "III_LOG_TRANSFER_REJECTED")
    return CommandResult(
        command=command,
        outcome=Outcome.REJECTED,
        summary="The log lifecycle operation was refused without unsafe deletion.",
        code=code,
        target=selected.get("endpoint") if selected else None,
        profile=selected.get("runtime_profile") if selected else None,
        findings=(Finding(code, str(exc)),),
        next_actions=(
            NextAction(
                ("iii", "deploy", "status", "--target", "real"),
                "Inspect authenticated receiver state.",
            ),
        ),
    )


def pull(args: argparse.Namespace) -> CommandResult:
    command = (
        "iii logs pull" if args.log_domain == "logs" else "iii deploy diagnostics pull"
    )
    selected = None
    try:
        selected = _target(args)
        preflight = args._iii_retained_plan["preflight"]
        if preflight.get("target") != _binding(selected) or preflight.get(
            "destination"
        ) != str(_destination(args, str(args.log_domain))):
            raise ValueError("retained pull target or destination is stale")
        operation_id = args._iii_operation_id
        manager = _manager()
        manifest = _retained_manifest(args)
        root, local = _materialize_pull(args, manager, selected, manifest, operation_id)
        verified = [
            {
                "locator": item["locator"],
                "content_id": item["content_id"],
                "size": item["size"],
            }
            for item in manifest["files"]
        ]
        planned = _request(
            manager,
            action="plan-log-receipt",
            operation_id=operation_id,
            payload={
                "manifest_id": manifest["manifest_id"],
                "verified_files": verified,
                "target": _binding(selected),
            },
        )
        accepted = _request(
            manager,
            action="log-receipt",
            operation_id=operation_id,
            payload={"plan": planned["plan"]},
            nonce=planned["nonce"],
        )
        receipt_id = planned["plan"]["parameters"]["receipt_id"]
        record = {
            "schema": "iii.log-pull-actual/v1",
            "operation_id": operation_id,
            "domain": args.log_domain,
            "target": _binding(selected),
            "source_manifest_id": manifest["manifest_id"],
            "local_manifest_id": local["local_manifest_id"],
            "receipt_id": receipt_id,
            "receiver_acceptance": accepted,
        }
        record["actual_id"] = content_id(record)
        OperationStore(default_state_root(_environment(args))).write_record(
            operation_id, "log-pull-actual.json", record
        )
    except Exception as exc:
        return _rejection(command, exc, selected)
    return CommandResult(
        command=command,
        outcome=Outcome.SUCCESS,
        summary=(
            f"Verified {len(manifest['files'])} {args.log_domain} files locally and "
            "durably submitted the matching onboard receipt."
        ),
        code=(
            "III_LOGS_PULLED"
            if args.log_domain == "logs"
            else "III_DEPLOY_DIAGNOSTICS_PULLED"
        ),
        target=selected["endpoint"],
        profile=selected["runtime_profile"],
        evidence=(
            str(root / "pull-manifest.json"),
            manifest["manifest_id"],
            receipt_id,
        ),
        payload_schema="iii.log-pull-result/v1",
        payload={
            "local_root": str(root),
            "local_manifest": local,
            "receipt_id": receipt_id,
            "receiver_acceptance": accepted,
        },
        next_actions=(
            NextAction(
                ("iii", "logs", "prune", "--pulled", receipt_id, "--target", "real"),
                "Plan exact deletion of only this verified pull when onboard space is needed.",
                mutating=True,
                confirmation_required=True,
            ),
        ),
    )


def prune_preflight(args: argparse.Namespace) -> Mapping[str, Any]:
    selected = _target(args)
    planned = _request(
        _manager(),
        action="plan-log-prune",
        operation_id="iii-logs-prune-plan",
        payload={"receipt_id": args.pulled, "target": _binding(selected)},
    )
    prune_plan = planned.get("plan", {}).get("parameters", {}).get("prune_plan")
    if not isinstance(prune_plan, dict):
        raise ValueError("receiver returned no exact receipt-backed prune plan")
    return {
        "schema": "iii.log-prune-preflight/v1",
        "target": _binding(selected),
        "receipt_id": args.pulled,
        "prune_plan": prune_plan,
    }


def prune(args: argparse.Namespace) -> CommandResult:
    selected = None
    try:
        selected = _target(args)
        retained = args._iii_retained_plan["preflight"]["prune_plan"]
        manager = _manager()
        planned = _request(
            manager,
            action="plan-log-prune",
            operation_id=args._iii_operation_id,
            payload={"receipt_id": args.pulled, "target": _binding(selected)},
        )
        current = planned["plan"]["parameters"]["prune_plan"]
        if current != retained:
            raise ValueError("receiver prune targets changed after retained planning")
        accepted = _request(
            manager,
            action="log-prune",
            operation_id=args._iii_operation_id,
            payload={"plan": planned["plan"]},
            nonce=planned["nonce"],
        )
    except Exception as exc:
        return _rejection("iii logs prune", exc, selected)
    return CommandResult(
        command="iii logs prune",
        outcome=Outcome.SUCCESS,
        summary=f"The receiver durably accepted exact pruning for receipt {args.pulled}.",
        code="III_LOGS_PRUNE_ACCEPTED",
        target=selected["endpoint"],
        profile=selected["runtime_profile"],
        evidence=(retained["plan_id"], args.pulled),
        payload_schema="iii.log-prune-result/v1",
        payload={"prune_plan": retained, "receiver_acceptance": accepted},
        next_actions=(
            NextAction(
                ("iii", "deploy", "status", "--target", "real"),
                "Verify terminal receiver operation state.",
            ),
        ),
    )


def initialize(parser: argparse.ArgumentParser) -> None:
    commands = parser.add_subparsers(dest="logs_command")
    pull_parser = commands.add_parser(
        "pull", help="verify and retain aircraft logs locally"
    )
    pull_parser.add_argument("--destination", type=Path)
    pull_parser.add_argument("--target", choices=("sim", "real"), default="real")
    pull_parser.set_defaults(
        func=pull,
        log_domain="logs",
        _iii_mutating=True,
        _iii_plan_provider=pull_preflight,
    )
    prune_parser = commands.add_parser(
        "prune", help="delete only exact content covered by a verified pull receipt"
    )
    prune_parser.add_argument("--pulled", required=True, metavar="RECEIPT_ID")
    prune_parser.add_argument("--target", choices=("sim", "real"), default="real")
    prune_parser.set_defaults(
        func=prune,
        _iii_mutating=True,
        _iii_plan_provider=prune_preflight,
    )
