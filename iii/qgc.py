"""Pinned host-native QGroundControl lifecycle commands."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Any, Mapping

from .operation import OperationStore, default_state_root
from .result import CommandResult, Finding, Outcome

UNIT = "iii-qgc.service"
GC_UNITS = {
    "iii-gc.target",
    "iii-gc-proxy.service",
    "iii-gc-frontend.service",
    "iii-gc-discovery.service",
    "iii-gc-mirror.service",
    "iii-gc-clock.service",
    "iii-gc-browser.service",
}


def _environment(args: argparse.Namespace) -> Mapping[str, str]:
    return getattr(args, "_iii_environment", os.environ)


def _application_root(args: argparse.Namespace) -> Path:
    value = _environment(args).get("III_GC_APPLICATION_ROOT")
    return (
        Path(value).expanduser().resolve()
        if value
        else (Path.home() / ".local/share/iii/gc-applications").resolve()
    )


def _state_root(args: argparse.Namespace) -> Path:
    value = _environment(args).get("III_GC_APPLICATION_STATE_ROOT")
    return (
        Path(value).expanduser().resolve()
        if value
        else (Path.home() / ".local/state/iii/gc").resolve()
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _systemctl_show() -> dict[str, str]:
    completed = subprocess.run(
        [
            "systemctl",
            "--user",
            "show",
            UNIT,
            "--property=LoadState,ActiveState,SubState",
        ],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if completed.returncode:
        raise ValueError(completed.stderr.strip() or f"cannot inspect {UNIT}")
    return {
        key: value
        for line in completed.stdout.splitlines()
        if "=" in line
        for key, value in (line.split("=", 1),)
    }


def _unit_definition() -> dict[str, Any]:
    source = Path.home() / ".config/systemd/user" / UNIT
    if source.is_symlink() or not source.is_file():
        raise ValueError(f"QGroundControl user unit is not safely provisioned: {UNIT}")
    metadata = source.stat(follow_symlinks=False)
    mode = format(metadata.st_mode & 0o777, "04o")
    if metadata.st_uid != os.geteuid() or mode != "0644":
        raise ValueError(
            f"QGroundControl user unit ownership or mode is unsafe: {UNIT}"
        )
    text = source.read_text(encoding="utf-8")
    for forbidden in GC_UNITS:
        if forbidden in text:
            raise ValueError(
                f"QGroundControl unit crosses into GC lifecycle: {forbidden}"
            )
    return {"sha256": hashlib.sha256(text.encode()).hexdigest(), "mode": mode}


def _selected(args: argparse.Namespace, *, required: bool) -> dict[str, Any]:
    state_path = _state_root(args) / "application-state.json"
    if state_path.is_symlink():
        raise ValueError("QGroundControl selection state is linked")
    if not state_path.is_file():
        if required:
            raise ValueError("no III-managed QGroundControl release is selected")
        return {"selected": False, "state_id": None}
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        release_id = state["active_release_id"]
        digest = state["active_qgc_sha256"]
        state_id = state["state_id"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError(f"QGroundControl selection state is invalid: {exc}") from exc
    expected_state_id = hashlib.sha256(
        json.dumps(
            {key: value for key, value in state.items() if key != "state_id"},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    if state_id != expected_state_id:
        raise ValueError("QGroundControl selection state identity is invalid")
    if not release_id or not digest:
        if required:
            raise ValueError("no III-managed QGroundControl release is selected")
        return {"selected": False, "state_id": state_id}
    slot = _application_root(args) / "qgc/slots" / digest
    selector = _application_root(args) / "qgc/current"
    binary = slot / "QGroundControl.AppImage"
    if (
        not selector.is_symlink()
        or selector.resolve() != slot.resolve()
        or slot.is_symlink()
        or not slot.is_dir()
        or binary.is_symlink()
        or not binary.is_file()
        or not os.access(binary, os.X_OK)
        or _sha256(binary) != digest
    ):
        raise ValueError("selected QGroundControl slot or checksum is invalid")
    release = state.get("releases", {}).get(release_id, {})
    qgc = release.get("qgroundcontrol", {})
    if qgc.get("sha256") != digest:
        raise ValueError("QGroundControl state does not bind the active release")
    return {
        "selected": True,
        "state_id": state_id,
        "release_id": release_id,
        "version": qgc.get("version"),
        "sha256": digest,
        "binary": str(binary),
    }


def _plan(args: argparse.Namespace) -> dict[str, Any]:
    selected = _selected(args, required=args.qgc_action in {"start", "restart"})
    value = {
        "schema": "iii.qgc-lifecycle-plan/v1",
        "action": args.qgc_action,
        "unit": _systemctl_show(),
        "unit_definition": _unit_definition(),
        "selection": selected,
        "mutations": [f"systemctl --user {args.qgc_action} {UNIT}"],
        "gc_mutation": False,
        "aircraft_mutation": False,
    }
    value["plan_id"] = hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return value


def lifecycle_preflight(args: argparse.Namespace) -> dict[str, Any]:
    identifier = getattr(args, "_iii_operation_id", None)
    if identifier:
        retained = OperationStore(default_state_root(_environment(args))).load_plan(
            identifier
        )
        if retained and isinstance(retained.get("preflight"), Mapping):
            return dict(retained["preflight"])
    return _plan(args)


def lifecycle(args: argparse.Namespace) -> CommandResult:
    command = f"iii qgc {args.qgc_action}"
    try:
        retained = getattr(args, "_iii_retained_plan", None)
        if not isinstance(retained, Mapping) or not isinstance(
            retained.get("preflight"), Mapping
        ):
            raise ValueError(
                "an exact retained QGroundControl lifecycle plan is required"
            )
        if _plan(args) != retained["preflight"]:
            raise ValueError(
                "QGroundControl unit or selected release changed after planning"
            )
        completed = subprocess.run(
            ["systemctl", "--user", args.qgc_action, UNIT],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if completed.returncode:
            raise ValueError(
                completed.stderr.strip() or "QGroundControl systemd operation failed"
            )
        state = _systemctl_show()
    except Exception as exc:
        return CommandResult(
            command=command,
            outcome=Outcome.REJECTED,
            summary="The pinned QGroundControl lifecycle mutation was refused.",
            code="III_QGC_LIFECYCLE_REJECTED",
            findings=(Finding("III_QGC_LIFECYCLE_REJECTED", str(exc)),),
            terminal_reason="No QGroundControl, GC, or aircraft mutation was performed.",
        )
    return CommandResult(
        command=command,
        outcome=Outcome.SUCCESS,
        summary=f"The pinned host QGroundControl accepted {args.qgc_action}; GC and aircraft were untouched.",
        code="III_QGC_LIFECYCLE_ACCEPTED",
        payload_schema="iii.qgc-lifecycle-result/v1",
        payload={
            "schema": "iii.qgc-lifecycle-result/v1",
            "action": args.qgc_action,
            "unit": state,
            "selection": retained["preflight"]["selection"],
            "gc_mutation": False,
            "aircraft_mutation": False,
        },
        terminal_reason="Only the independent host-native QGroundControl unit changed state.",
    )


def status(args: argparse.Namespace) -> CommandResult:
    try:
        selection = _selected(args, required=args.require_selected)
        unit = _systemctl_show()
        definition = _unit_definition()
    except Exception as exc:
        return CommandResult(
            command="iii qgc status",
            outcome=Outcome.REJECTED,
            summary="QGroundControl selection could not be authenticated.",
            code="III_QGC_STATUS_REJECTED",
            findings=(Finding("III_QGC_STATUS_REJECTED", str(exc)),),
            terminal_reason="Authentication failed closed before any local or aircraft mutation.",
        )
    findings = (
        ()
        if selection["selected"]
        else (
            Finding(
                "III_QGC_NOT_SELECTED",
                "No pinned QGroundControl release is active",
                severity="warning",
            ),
        )
    )
    return CommandResult(
        command="iii qgc status",
        outcome=Outcome.WARNING if findings else Outcome.SUCCESS,
        summary="Authenticated the independent host QGroundControl unit and selected binary.",
        code="III_QGC_STATUS",
        findings=findings,
        payload_schema="iii.qgc-status/v1",
        payload={
            "schema": "iii.qgc-status/v1",
            "selection": selection,
            "unit": unit,
            "unit_definition": definition,
            "gc_mutation": False,
            "aircraft_mutation": False,
        },
        terminal_reason="Status was read-only and did not contact QGroundControl, GC, or the aircraft.",
    )


def initialize(parser: argparse.ArgumentParser) -> None:
    commands = parser.add_subparsers(dest="qgc_command")
    for action in ("start", "stop", "restart"):
        command = commands.add_parser(
            action, help=f"{action} pinned host QGroundControl"
        )
        command.set_defaults(
            func=lifecycle,
            qgc_action=action,
            _iii_mutating=True,
            _iii_plan_provider=lifecycle_preflight,
        )
    inspect = commands.add_parser(
        "status", help="authenticate QGroundControl selection and state"
    )
    inspect.add_argument(
        "--require-selected", action="store_true", help=argparse.SUPPRESS
    )
    inspect.set_defaults(func=status, require_selected=False, _iii_mutating=False)
    configuration = commands.add_parser(
        "config", help="manage release-owned QGroundControl settings and generated data"
    )
    from . import qgc_config

    qgc_config.initialize(configuration)
