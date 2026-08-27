"""Ground-control host provisioning and login-session lifecycle commands."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any, Mapping, Sequence

from .operation import OperationStore, default_state_root
from .result import CommandResult, Finding, NextAction, Outcome

TARGET_UNIT = "iii-gc.target"
BROWSER_UNIT = "iii-gc-browser.service"
MANAGED_UNITS = (
    TARGET_UNIT,
    "iii-gc-proxy.service",
    "iii-gc-frontend.service",
    "iii-gc-discovery.service",
    "iii-gc-mirror.service",
    "iii-gc-clock.service",
    BROWSER_UNIT,
)


def _environment(args: argparse.Namespace) -> Mapping[str, str]:
    return getattr(args, "_iii_environment", os.environ)


def _workspace() -> Path:
    for candidate in (Path.cwd().resolve(), *Path.cwd().resolve().parents):
        if (candidate / "deployment/gc-host-policy.json").is_file() and (
            candidate / "tools/III-Drone-CLI"
        ).is_dir():
            return candidate
    source = Path(__file__).resolve()
    for candidate in (source, *source.parents):
        if (candidate / "deployment/gc-host-policy.json").is_file():
            return candidate
    raise ValueError("cannot locate the III workspace deployment policy")


def _schema_root(args: argparse.Namespace) -> Path:
    if getattr(args, "schema_root", None):
        return Path(args.schema_root).expanduser().resolve()
    env = _environment(args)
    candidates = [
        (
            Path(env["III_DEPLOYMENT_SCHEMA_ROOT"])
            if env.get("III_DEPLOYMENT_SCHEMA_ROOT")
            else Path("/__missing__")
        ),
        Path(sys.prefix) / "share/iii-deployment/schemas/v1",
        _workspace() / "deployment/schemas/v1",
    ]
    for path in candidates:
        if path.is_dir():
            return path.resolve()
    raise ValueError("cannot locate deployment schemas")


def _ansible_playbook(args: argparse.Namespace) -> Path:
    if getattr(args, "ansible_playbook", None):
        return Path(args.ansible_playbook).expanduser().resolve()
    executable = shutil.which("ansible-playbook")
    if executable:
        return Path(executable).resolve()
    candidate = Path.home() / ".local/share/iii/controller/venv/bin/ansible-playbook"
    if candidate.is_file():
        return candidate.resolve()
    raise ValueError(
        "ansible-playbook is unavailable; run deployment/scripts/bootstrap_gc_controller.py"
    )


def _retained_preflight(args: argparse.Namespace) -> dict[str, Any] | None:
    identifier = getattr(args, "_iii_operation_id", None)
    if not identifier:
        return None
    plan = OperationStore(default_state_root(_environment(args))).load_plan(identifier)
    value = plan and plan.get("preflight")
    return dict(value) if isinstance(value, Mapping) else None


def provision_preflight(args: argparse.Namespace) -> dict[str, Any]:
    retained = _retained_preflight(args)
    if retained is not None:
        return retained
    identifier = getattr(args, "_iii_operation_id", None)
    if not identifier:
        raise ValueError("GC provisioning requires a retained operation ID")
    from iii_deployment.gc_host import build_plan

    root = _workspace()
    return build_plan(
        operation_id=identifier,
        workspace=root,
        policy_path=root / "deployment/gc-host-policy.json",
        schema_root=_schema_root(args),
        ansible_root=root / "deployment/ansible",
        ansible_playbook=_ansible_playbook(args),
        offline=args.offline,
        offline_cache=args.offline_cache,
        replacement_archive=args.replacement_archive,
    )


def _rejected(
    command: str, exc: Exception, *, next_command: Sequence[str]
) -> CommandResult:
    code = getattr(exc, "code", "III_GC_ERROR")
    return CommandResult(
        command=command,
        outcome=Outcome.REJECTED,
        summary=f"{command} was refused before an unauthenticated or stale mutation.",
        code=code,
        findings=(Finding(code, str(exc)),),
        next_actions=(
            NextAction(
                tuple(next_command), "Inspect the exact GC host state and requirements."
            ),
        ),
    )


def provision(args: argparse.Namespace) -> CommandResult:
    try:
        from iii_deployment.gc_host import apply_plan

        retained = getattr(args, "_iii_retained_plan", None)
        if not isinstance(retained, Mapping) or not isinstance(
            retained.get("preflight"), Mapping
        ):
            raise ValueError("an exact retained GC provisioning plan is required")
        report = apply_plan(retained["preflight"], schema_root=_schema_root(args))
        identifier = getattr(args, "_iii_operation_id", None)
        if identifier:
            OperationStore(default_state_root(_environment(args))).write_record(
                identifier, "gc-provisioning-report.json", report
            )
    except Exception as exc:
        return _rejected("iii gc provision", exc, next_command=("iii", "gc", "status"))
    return CommandResult(
        command="iii gc provision",
        outcome=Outcome.SUCCESS,
        summary=(
            "The local GC host converged and its second run proved zero managed drift; "
            "unmanaged user state was preserved."
        ),
        code="III_GC_PROVISIONED",
        evidence=(report["report_id"],),
        payload_schema=report["schema"],
        payload=report,
        next_actions=(
            NextAction(
                ("iii", "access", "enroll", "prepare", "--help"),
                "Enroll fresh per-computer runtime/signing authority without importing private material.",
            ),
            NextAction(
                ("iii", "gc", "status"),
                "Inspect login services and persistent boundaries.",
            ),
        ),
    )


def _systemctl_show(unit: str) -> dict[str, str]:
    completed = subprocess.run(
        [
            "systemctl",
            "--user",
            "show",
            unit,
            "--property=LoadState,ActiveState,SubState",
        ],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if completed.returncode != 0:
        raise ValueError(completed.stderr.strip() or f"cannot inspect {unit}")
    values = {}
    for line in completed.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


def _lifecycle_plan(action: str) -> dict[str, Any]:
    states = {unit: _systemctl_show(unit) for unit in MANAGED_UNITS}
    definitions = {}
    for unit in MANAGED_UNITS:
        source = Path.home() / ".config/systemd/user" / unit
        if source.is_symlink() or not source.is_file():
            raise ValueError(f"GC user unit is not safely provisioned: {unit}")
        metadata = source.stat(follow_symlinks=False)
        mode = format(metadata.st_mode & 0o777, "04o")
        if metadata.st_uid != os.geteuid() or mode != "0644":
            raise ValueError(f"GC user unit ownership or mode is unsafe: {unit}")
        definitions[unit] = {
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "mode": mode,
        }
    value = {
        "schema": "iii.gc-lifecycle-plan/v1",
        "action": action,
        "units": states,
        "unit_definitions": definitions,
        "mutations": [f"systemctl --user {action} local GC services"],
        "aircraft_mutation": False,
        "browser_automatic": False,
    }
    value["plan_id"] = hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return value


def lifecycle_preflight(args: argparse.Namespace) -> dict[str, Any]:
    retained = _retained_preflight(args)
    if retained is not None:
        return retained
    return _lifecycle_plan(args.gc_action)


def lifecycle(args: argparse.Namespace) -> CommandResult:
    command = f"iii gc {args.gc_action}"
    try:
        retained = getattr(args, "_iii_retained_plan", None)
        if not isinstance(retained, Mapping) or not isinstance(
            retained.get("preflight"), Mapping
        ):
            raise ValueError("an exact retained GC lifecycle plan is required")
        plan = retained["preflight"]
        if _lifecycle_plan(args.gc_action) != plan:
            raise ValueError("GC unit state changed after planning")
        action = args.gc_action
        if action == "open":
            argv = ["systemctl", "--user", "start", TARGET_UNIT, BROWSER_UNIT]
        else:
            verb = {"start": "start", "stop": "stop", "restart": "restart"}[action]
            argv = ["systemctl", "--user", verb, TARGET_UNIT]
        completed = subprocess.run(
            argv,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if completed.returncode != 0:
            raise ValueError(
                completed.stderr.strip() or "systemd user operation failed"
            )
        state = _systemctl_show(TARGET_UNIT)
    except Exception as exc:
        return _rejected(command, exc, next_command=("iii", "gc", "status"))
    return CommandResult(
        command=command,
        outcome=Outcome.SUCCESS,
        summary=f"The local ground-control session accepted {args.gc_action}; the aircraft was untouched.",
        code="III_GC_LIFECYCLE_ACCEPTED",
        payload_schema="iii.gc-lifecycle-result/v1",
        payload={
            "schema": "iii.gc-lifecycle-result/v1",
            "action": args.gc_action,
            "target_state": state,
            "aircraft_mutation": False,
        },
        terminal_reason="Only local graphical/user services changed state; no drone command was sent.",
    )


def status(args: argparse.Namespace) -> CommandResult:
    try:
        from iii_deployment.gc_host import inspect_status

        root = _workspace()
        report = inspect_status(
            policy_path=root / "deployment/gc-host-policy.json",
            schema_root=_schema_root(args),
        )
    except Exception as exc:
        return _rejected(
            "iii gc status", exc, next_command=("iii", "gc", "provision", "--help")
        )
    unavailable = [
        unit["unit"] for unit in report["units"] if unit["load_state"] != "loaded"
    ]
    missing = [item["path"] for item in report["paths"] if not item["exists"]]
    findings = tuple(
        [
            Finding("III_GC_UNIT_UNAVAILABLE", unit, severity="warning")
            for unit in unavailable
        ]
        + [Finding("III_GC_PATH_MISSING", path, severity="warning") for path in missing]
    )
    outcome = Outcome.WARNING if findings else Outcome.SUCCESS
    return CommandResult(
        command="iii gc status",
        outcome=outcome,
        summary="Inspected the local GC host services, identity, and persistent path boundaries.",
        code="III_GC_STATUS",
        findings=findings,
        payload_schema=report["schema"],
        payload=report,
        terminal_reason="Status inspection was read-only and did not contact or mutate the aircraft.",
    )


def _provision_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--offline",
        action="store_true",
        help="use only a verified prepared-offline cache",
    )
    parser.add_argument(
        "--offline-cache", type=Path, help="prepared-offline cache root"
    )
    parser.add_argument(
        "--replacement-archive",
        type=Path,
        help="verified P2.T8 portable archive for a fresh replacement GC",
    )
    parser.add_argument(
        "--schema-root", type=Path, help="override deployment schema root"
    )
    parser.add_argument(
        "--ansible-playbook",
        type=Path,
        help="explicit repository-managed ansible-playbook executable",
    )


def initialize(parser: argparse.ArgumentParser) -> None:
    commands = parser.add_subparsers(dest="gc_command")
    provision_parser = commands.add_parser(
        "provision", help="converge this supported Ubuntu graphical host"
    )
    _provision_arguments(provision_parser)
    provision_parser.set_defaults(
        func=provision,
        _iii_mutating=True,
        _iii_plan_provider=provision_preflight,
    )
    for action in ("start", "stop", "restart", "open"):
        action_parser = commands.add_parser(
            action, help=f"{action} the local GC user session"
        )
        action_parser.set_defaults(
            func=lifecycle,
            gc_action=action,
            _iii_mutating=True,
            _iii_plan_provider=lifecycle_preflight,
        )
    status_parser = commands.add_parser(
        "status", help="inspect the local GC host and user services"
    )
    status_parser.add_argument("--schema-root", type=Path)
    status_parser.set_defaults(func=status, _iii_mutating=False)
