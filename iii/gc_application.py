"""Transactional GC/QGroundControl application slot commands."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping

from .operation import OperationStore, default_state_root
from .result import CommandResult, Finding, NextAction, Outcome


def _environment(args: argparse.Namespace) -> Mapping[str, str]:
    return getattr(args, "_iii_environment", os.environ)


def _workspace() -> Path | None:
    for candidate in (Path.cwd().resolve(), *Path.cwd().resolve().parents):
        if (candidate / "deployment/gc-application-policy.json").is_file():
            return candidate
    return None


def _resource(args: argparse.Namespace, name: str) -> Path:
    env = _environment(args)
    variable = {
        "gc-application-policy.json": "III_GC_APPLICATION_POLICY",
        "operational-policy.json": "III_DEPLOYMENT_OPERATIONAL_POLICY",
    }[name]
    candidates = []
    if env.get(variable):
        candidates.append(Path(env[variable]))
    candidates.append(Path(sys.prefix) / "share/iii-deployment/policy" / name)
    root = _workspace()
    if root is not None:
        candidates.append(root / "deployment" / name)
    for candidate in candidates:
        if candidate.is_file() and not candidate.is_symlink():
            return candidate.expanduser().resolve()
    raise ValueError(f"cannot locate deployment resource {name}")


def _schemas(args: argparse.Namespace) -> Path:
    env = _environment(args)
    candidates = []
    if env.get("III_DEPLOYMENT_SCHEMA_ROOT"):
        candidates.append(Path(env["III_DEPLOYMENT_SCHEMA_ROOT"]))
    candidates.append(Path(sys.prefix) / "share/iii-deployment/schemas/v1")
    root = _workspace()
    if root is not None:
        candidates.append(root / "deployment/schemas/v1")
    for candidate in candidates:
        if candidate.is_dir() and not candidate.is_symlink():
            return candidate.expanduser().resolve()
    raise ValueError("cannot locate deployment schemas")


def _roots(args: argparse.Namespace) -> tuple[Path, Path, Path]:
    env = _environment(args)
    application = (
        Path(
            env.get(
                "III_GC_APPLICATION_ROOT",
                str(Path.home() / ".local/share/iii/gc-applications"),
            )
        )
        .expanduser()
        .resolve()
    )
    state = (
        Path(
            env.get(
                "III_GC_APPLICATION_STATE_ROOT",
                str(Path.home() / ".local/state/iii/gc"),
            )
        )
        .expanduser()
        .resolve()
    )
    cache = (
        Path(env.get("III_GC_APPLICATION_CACHE_ROOT", str(Path.home() / ".cache/iii")))
        .expanduser()
        .resolve()
    )
    return application, state, cache


def _trusted_signers(args: argparse.Namespace) -> Path:
    env = _environment(args)
    return (
        Path(
            env.get(
                "III_GC_TRUSTED_SIGNERS",
                str(Path.home() / ".config/iii/keys/signing/trusted-signers.json"),
            )
        )
        .expanduser()
        .resolve()
    )


def _store(args: argparse.Namespace, *, create_roots: bool = True):
    from iii_deployment.gc_application import GCApplicationStore

    application, state, cache = _roots(args)
    return GCApplicationStore(
        application_root=application,
        state_root=state,
        cache_root=cache,
        policy_path=_resource(args, "gc-application-policy.json"),
        schema_root=_schemas(args),
        trusted_signers=_trusted_signers(args),
        operational_policy_path=_resource(args, "operational-policy.json"),
        qgc_settings_path=Path(
            _environment(args).get(
                "III_QGC_SETTINGS_PATH",
                str(Path.home() / ".config/QGroundControl.org/QGroundControl.ini"),
            )
        ).expanduser().resolve(),
        qgc_configuration_state_root=Path(
            _environment(args).get(
                "III_QGC_CONFIGURATION_STATE_ROOT",
                str(Path.home() / ".local/state/iii/qgc-configuration"),
            )
        ).expanduser().resolve(),
        create_roots=create_roots,
    )


def _file_evidence(path: Path, *, required: bool = True) -> dict[str, Any] | None:
    if not path.exists() and not path.is_symlink():
        if required:
            raise ValueError(f"required file is missing: {path}")
        return None
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"required file is unsafe: {path}")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"path": str(path.resolve()), "sha256": digest, "bytes": path.stat().st_size}


def _state_evidence(args: argparse.Namespace) -> dict[str, Any] | None:
    return _file_evidence(_roots(args)[1] / "application-state.json", required=False)


def _journal_evidence(args: argparse.Namespace) -> dict[str, Any] | None:
    return _file_evidence(_roots(args)[1] / "application-journal.json", required=False)


def _bundle_evidence(path: Path) -> dict[str, Any]:
    from iii_deployment.bundle import COMPONENT_FILES

    source = path.expanduser().resolve()
    if path.is_symlink() or not source.is_dir():
        raise ValueError("GC bundle must be a real component directory")
    files = {name: _file_evidence(source / name) for name in COMPONENT_FILES}
    return {
        "path": str(source),
        "files": files,
        "content_id": hashlib.sha256(
            json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }


def _safety(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any]]:
    if getattr(args, "disconnected", False):
        value = {"connected": False, "source": "operator-explicit-disconnected"}
        return value, {
            "kind": "explicit-disconnected",
            "sha256": hashlib.sha256(
                json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        }
    if getattr(args, "sim", False):
        value = {"connected": True, "profile": "sim", "source": "operator-explicit-sim"}
        return value, {
            "kind": "explicit-sim",
            "sha256": hashlib.sha256(
                json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        }
    path = getattr(args, "safety_file", None)
    if path is None:
        raise ValueError(
            "activation requires exactly one of --safety-file, --disconnected, or --sim"
        )
    evidence = _file_evidence(Path(path).expanduser().resolve())
    try:
        value = json.loads(Path(evidence["path"]).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read activation safety evidence: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("activation safety evidence must be a JSON object")
    return value, {"kind": "authenticated-file", **evidence}


def _retained(args: argparse.Namespace) -> dict[str, Any] | None:
    identifier = getattr(args, "_iii_operation_id", None)
    if not identifier:
        return None
    plan = OperationStore(default_state_root(_environment(args))).load_plan(identifier)
    value = plan and plan.get("preflight")
    return dict(value) if isinstance(value, Mapping) else None


def preflight(args: argparse.Namespace) -> dict[str, Any]:
    retained = _retained(args)
    if retained is not None:
        return retained
    action = args.application_action
    application, state, cache = _roots(args)
    value: dict[str, Any] = {
        "schema": "iii.gc-application-plan/v1",
        "action": action,
        "roots": {
            "application": str(application),
            "state": str(state),
            "cache": str(cache),
        },
        "policy": _file_evidence(_resource(args, "gc-application-policy.json")),
        "operational_policy": _file_evidence(
            _resource(args, "operational-policy.json")
        ),
        "state": _state_evidence(args),
        "journal": _journal_evidence(args),
        "mutations": [],
    }
    if action == "stage":
        value["bundle"] = _bundle_evidence(args.bundle)
        value["trusted_signers"] = _file_evidence(_trusted_signers(args))
        value["protect_offline"] = args.protect_offline
        value["mutations"] = [
            "verify/cache/extract GC bundle",
            "import exact OCI digests",
            "stage QGC slot",
        ]
    elif action in {"activate", "rollback"}:
        _safety_value, safety_evidence = _safety(args)
        value["safety"] = safety_evidence
        value["release_id"] = getattr(args, "release_id", None)
        value["override"] = {
            "reason_sha256": (
                hashlib.sha256(args.override_reason.encode()).hexdigest()
                if args.override_reason
                else None
            ),
            "warning": args.override_confirmation,
        }
        value["mutations"] = [
            "drain browser mutations",
            "atomically select GC/QGC slots",
            "restart local application units",
        ]
    elif action == "reconcile":
        value["mutations"] = ["restore exact journaled GC/QGC pair if interrupted"]
    elif action == "prune":
        value["mutations"] = ["remove only unprotected application/cache slots"]
    value["plan_id"] = hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return value


def _result(action: str, payload: Mapping[str, Any]) -> CommandResult:
    return CommandResult(
        command=f"iii gc application {action}",
        outcome=Outcome.SUCCESS,
        summary=f"The transactional GC application {action} operation completed.",
        code=f"III_GC_APPLICATION_{action.upper().replace('-', '_')}",
        payload_schema="iii.gc-application-result/v1",
        payload={
            "schema": "iii.gc-application-result/v1",
            "action": action,
            **dict(payload),
        },
        terminal_reason="Only release-owned GC/QGroundControl application slots and local services were eligible to change.",
    )


def mutate(args: argparse.Namespace) -> CommandResult:
    action = args.application_action
    command = f"iii gc application {action}"
    try:
        retained = getattr(args, "_iii_retained_plan", None)
        if not isinstance(retained, Mapping) or not isinstance(
            retained.get("preflight"), Mapping
        ):
            raise ValueError("an exact retained GC application plan is required")
        expected = dict(retained["preflight"])
        # Recompute without accepting the retained preflight shortcut.
        identifier = getattr(args, "_iii_operation_id", None)
        if identifier:
            delattr(args, "_iii_operation_id")
        try:
            current = preflight(args)
        finally:
            if identifier:
                setattr(args, "_iii_operation_id", identifier)
        if current != expected:
            raise ValueError("GC application inputs or state changed after planning")
        store = _store(args)
        operation_id = identifier or "iii-gc-application"
        if action == "stage":
            payload = store.stage(
                Path(args.bundle).expanduser().resolve(),
                protect_offline=args.protect_offline,
            )
        elif action == "activate":
            safety, _evidence = _safety(args)
            payload = store.activate(
                args.release_id,
                operation_id=operation_id,
                safety=safety,
                override_reason=args.override_reason,
                override_confirmation=args.override_confirmation,
            )
        elif action == "rollback":
            safety, _evidence = _safety(args)
            payload = store.rollback(
                operation_id=operation_id,
                safety=safety,
                override_reason=args.override_reason,
                override_confirmation=args.override_confirmation,
            )
        elif action == "reconcile":
            payload = store.reconcile()
        elif action == "prune":
            payload = store.garbage_collect()
        else:
            raise ValueError(f"unsupported GC application action: {action}")
    except Exception as exc:
        return CommandResult(
            command=command,
            outcome=Outcome.REJECTED,
            summary="The GC application transaction was refused or failed closed.",
            code=getattr(exc, "code", "III_GC_APPLICATION_REJECTED"),
            findings=(
                Finding(getattr(exc, "code", "III_GC_APPLICATION_REJECTED"), str(exc)),
            ),
            next_actions=(
                NextAction(
                    ("iii", "gc", "application", "status"),
                    "Inspect the exact slot and recovery state.",
                ),
            ),
        )
    return _result(action, payload)


def status(args: argparse.Namespace) -> CommandResult:
    try:
        store = _store(args, create_roots=False)
        state = store.state()
        journal = _journal_evidence(args)
        payload = {
            "schema": "iii.gc-application-status/v1",
            "state": state,
            "recovery_required": journal is not None,
            "journal": journal,
            "external_state_preserved": [
                str(Path.home() / ".config/QGroundControl.org"),
                str(Path.home() / ".local/share/QGroundControl"),
                str(Path.home() / "Documents/QGroundControl"),
                str(Path.home() / ".iii"),
            ],
        }
    except Exception as exc:
        return CommandResult(
            command="iii gc application status",
            outcome=Outcome.REJECTED,
            summary="GC application state could not be authenticated.",
            code=getattr(exc, "code", "III_GC_APPLICATION_STATUS_REJECTED"),
            findings=(
                Finding(
                    getattr(exc, "code", "III_GC_APPLICATION_STATUS_REJECTED"), str(exc)
                ),
            ),
            terminal_reason="Status failed closed without mutating application or user state.",
        )
    findings = (
        ()
        if journal is None
        else (
            Finding(
                "III_GC_APPLICATION_RECOVERY_REQUIRED",
                "An interrupted application journal requires reconciliation",
                severity="warning",
            ),
        )
    )
    return CommandResult(
        command="iii gc application status",
        outcome=Outcome.WARNING if findings else Outcome.SUCCESS,
        summary="Authenticated GC/QGroundControl slots, selectors, and recovery state.",
        code="III_GC_APPLICATION_STATUS",
        findings=findings,
        payload_schema=payload["schema"],
        payload=payload,
        terminal_reason="Status was read-only and preserved all application and user state.",
    )


def _safety_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--safety-file", type=Path, help="fresh authenticated maintenance-safety JSON"
    )
    group.add_argument(
        "--disconnected",
        action="store_true",
        help="explicitly assert that no aircraft target is connected",
    )
    group.add_argument(
        "--sim",
        action="store_true",
        help="explicitly select the simulation safety profile",
    )
    parser.add_argument("--override-reason", help="separately audited recovery reason")
    parser.add_argument(
        "--override-confirmation", help="exact recovery warning confirmation"
    )


def initialize(commands: Any) -> None:
    root = commands.add_parser(
        "application", help="manage signed GC/QGroundControl release slots"
    )
    actions = root.add_subparsers(dest="application_action")
    stage = actions.add_parser(
        "stage", help="verify and stage a signed GC component bundle"
    )
    stage.add_argument("--bundle", type=Path, required=True)
    stage.add_argument(
        "--protect-offline",
        action="store_true",
        help="retain this cached bundle as an operator-designated offline set",
    )
    stage.set_defaults(
        func=mutate,
        application_action="stage",
        _iii_mutating=True,
        _iii_plan_provider=preflight,
    )
    activate = actions.add_parser(
        "activate", help="activate an exact staged GC/QGC pair"
    )
    activate.add_argument("--release-id", required=True)
    _safety_arguments(activate)
    activate.set_defaults(
        func=mutate,
        application_action="activate",
        _iii_mutating=True,
        _iii_plan_provider=preflight,
    )
    rollback = actions.add_parser(
        "rollback", help="return to the retained previous field pair"
    )
    _safety_arguments(rollback)
    rollback.set_defaults(
        func=mutate,
        application_action="rollback",
        _iii_mutating=True,
        _iii_plan_provider=preflight,
    )
    reconcile = actions.add_parser(
        "reconcile", help="recover an interrupted selector transaction"
    )
    reconcile.set_defaults(
        func=mutate,
        application_action="reconcile",
        _iii_mutating=True,
        _iii_plan_provider=preflight,
    )
    prune = actions.add_parser(
        "prune", help="remove only unprotected application/cache slots"
    )
    prune.set_defaults(
        func=mutate,
        application_action="prune",
        _iii_mutating=True,
        _iii_plan_provider=preflight,
    )
    inspect = actions.add_parser(
        "status", help="inspect slots, selectors, and recovery state"
    )
    inspect.set_defaults(func=status, application_action="status", _iii_mutating=False)
