"""Host-native lifecycle for the checkout-pinned QGroundControl AppImage."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import time
from typing import Any, Mapping

from .result import CommandResult, Finding, NextAction, Outcome


PINNED_VERSION = "5.0.8"
PINNED_ARCHITECTURE = "x86_64"
PINNED_SHA256 = "06969c67ef58ea063def0a8271447a1cc385438c4a7df36813315b4475146737"
PINNED_SIZE = 180816376
PINNED_URL = (
    "https://github.com/mavlink/qgroundcontrol/releases/download/v5.0.8/"
    "QGroundControl-x86_64.AppImage"
)
PINNED_SOURCE_COMMIT = "e0816c957602789200ae5ba0af45217f0f2f1db4"
UNIT_NAME = "iii-qgc.service"


def _environment(args: argparse.Namespace) -> Mapping[str, str]:
    return getattr(args, "_iii_environment", os.environ)


def _install_root(environment: Mapping[str, str]) -> Path:
    override = environment.get("III_GC_INSTALL_ROOT")
    if override:
        return Path(override).expanduser()
    data_home = environment.get("XDG_DATA_HOME")
    home = Path(environment.get("HOME", str(Path.home()))).expanduser()
    base = Path(data_home).expanduser() if data_home else home / ".local" / "share"
    return base / "iii" / "gc"


def _manifest_path(root: Path, environment: Mapping[str, str]) -> Path:
    installed = root / "qgroundcontrol.json"
    if environment.get("III_GC_INSTALL_ROOT"):
        return installed
    # Source checkouts carry the same pin under deps/. Installed packages use
    # the manifest copied into their install root by the native installer. A
    # checkout manifest takes precedence over an older per-user install pin.
    checkout = Path(__file__).resolve().parents[3] / "deps" / "qgroundcontrol.json"
    return checkout if checkout.is_file() else installed


def _binary_sha256(binary: Path) -> str:
    hasher = hashlib.sha256()
    with binary.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _selection(args: argparse.Namespace) -> tuple[dict[str, Any], list[Finding]]:
    environment = _environment(args)
    root = _install_root(environment)
    binary = root / "qgc" / "QGroundControl.AppImage"
    manifest = _manifest_path(root, environment)
    selection: dict[str, Any] = {
        "selected": False,
        "binary": str(binary),
        "manifest": str(manifest),
        "version": None,
        "url": None,
        "size": None,
        "sha256": None,
        "architecture": None,
        "source_commit": None,
    }
    findings: list[Finding] = []
    try:
        pin = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        findings.append(
            Finding(
                "III_QGC_MANIFEST_UNAVAILABLE",
                f"QGroundControl pin manifest is unavailable or invalid at {manifest}: {exc}",
            )
        )
        return selection, findings

    for key in (
        "version",
        "url",
        "size",
        "sha256",
        "architecture",
        "source_commit",
    ):
        selection[key] = pin.get(key)
    expected = {
        "version": PINNED_VERSION,
        "url": PINNED_URL,
        "size": PINNED_SIZE,
        "sha256": PINNED_SHA256,
        "architecture": PINNED_ARCHITECTURE,
        "source_commit": PINNED_SOURCE_COMMIT,
    }
    mismatches = [
        f"{key} is {pin.get(key)!r}, expected {value!r}"
        for key, value in expected.items()
        if pin.get(key) != value
    ]
    if mismatches:
        findings.append(
            Finding(
                "III_QGC_PIN_MISMATCH",
                "QGroundControl manifest does not match the checkout pin: "
                + "; ".join(mismatches),
                field="manifest",
            )
        )
    try:
        digest = _binary_sha256(binary)
    except OSError as exc:
        findings.append(
            Finding(
                "III_QGC_BINARY_UNAVAILABLE",
                f"Pinned QGroundControl AppImage is unavailable at {binary}: {exc}",
                field="binary",
            )
        )
    else:
        selection["actual_sha256"] = digest
        if digest != PINNED_SHA256:
            findings.append(
                Finding(
                    "III_QGC_BINARY_DIGEST_MISMATCH",
                    f"QGroundControl AppImage SHA256 is {digest}, expected {PINNED_SHA256}.",
                    field="binary",
                )
            )
        elif not os.access(binary, os.X_OK):
            findings.append(
                Finding(
                    "III_QGC_BINARY_NOT_EXECUTABLE",
                    f"Pinned QGroundControl AppImage is not executable: {binary}",
                    field="binary",
                )
            )
    selection["selected"] = not findings
    return selection, findings


def _systemctl(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", "--user", *arguments],
        check=False,
        capture_output=True,
        text=True,
    )


def _read_unit() -> tuple[dict[str, str], subprocess.CompletedProcess[str]]:
    completed = _systemctl(
        "show",
        UNIT_NAME,
        "--property=LoadState",
        "--property=ActiveState",
        "--property=SubState",
        "--property=ExecStart",
    )
    values: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    return values, completed


def _stop_service() -> subprocess.CompletedProcess[str]:
    """Ask QGC's whole AppImage process group to exit before systemd's timeout.

    The AppImage launcher and the Qt child are separate processes. A plain
    ``systemctl stop`` only made the launcher exit on the tested host, leaving
    Qt until systemd's stop timeout. Signalling the owned unit's whole group
    lets QGC close normally and keeps this scoped to iii-qgc.service.
    """

    unit, inspection = _read_unit()
    if inspection.returncode:
        return inspection
    if unit.get("ActiveState") == "failed":
        reset = _systemctl("reset-failed", UNIT_NAME)
        if reset.returncode:
            return reset
        unit, inspection = _read_unit()
        if inspection.returncode:
            return inspection
    if unit.get("ActiveState") == "inactive":
        return subprocess.CompletedProcess(
            ["systemctl", "--user", "stop", UNIT_NAME], 0, "", ""
        )
    signal = _systemctl("kill", "--kill-who=all", "--signal=SIGINT", UNIT_NAME)
    if signal.returncode:
        return signal
    deadline = time.monotonic() + 5.0
    while True:
        unit, inspection = _read_unit()
        if inspection.returncode:
            return inspection
        if unit.get("ActiveState") == "inactive":
            return signal
        if unit.get("ActiveState") == "failed" or time.monotonic() >= deadline:
            break
        time.sleep(0.1)

    # A few Qt workers can outlive the AppImage launcher.  Kill only the
    # selected systemd unit's remaining cgroup after the graceful interval.
    if unit.get("ActiveState") != "failed":
        forced = _systemctl("kill", "--kill-who=all", "--signal=SIGKILL", UNIT_NAME)
        if forced.returncode:
            return forced
    deadline = time.monotonic() + 3.0
    while True:
        unit, inspection = _read_unit()
        if inspection.returncode:
            return inspection
        if unit.get("ActiveState") == "failed":
            reset = _systemctl("reset-failed", UNIT_NAME)
            if reset.returncode:
                return reset
            unit, inspection = _read_unit()
            if inspection.returncode:
                return inspection
        if unit.get("ActiveState") == "inactive":
            return subprocess.CompletedProcess(
                signal.args,
                0,
                signal.stdout,
                "QGroundControl required a forced stop after the graceful SIGINT interval.",
            )
        if time.monotonic() >= deadline:
            return subprocess.CompletedProcess(
                signal.args,
                1,
                signal.stdout,
                f"QGroundControl remained {unit.get('ActiveState', 'unknown')} after the forced stop.",
            )
        time.sleep(0.1)


def _unit_selects_binary(unit: Mapping[str, str], binary: str) -> bool:
    match = re.search(r"(?:^|[;{])\s*path=([^;}]+)", unit.get("ExecStart", ""))
    return match is not None and match.group(1).strip() == binary


def _result(
    action: str,
    *,
    outcome: Outcome,
    code: str,
    summary: str,
    findings: tuple[Finding, ...] = (),
    selection: dict[str, Any] | None = None,
    unit: dict[str, str] | None = None,
) -> CommandResult:
    return CommandResult(
        command=f"iii qgc {action}",
        outcome=outcome,
        summary=summary,
        code=code,
        findings=findings,
        payload_schema="iii.qgc-status/v1",
        payload={"selection": selection or {}, "unit": unit or {}},
        next_actions=(
            NextAction(
                ("iii", "qgc", "status"),
                "Inspect pinned QGroundControl selection and service state.",
            ),
        ),
    )


def status(args: argparse.Namespace) -> CommandResult:
    selection, selection_findings = _selection(args)
    unit, completed = _read_unit()
    findings = list(selection_findings)
    if completed.returncode != 0:
        findings.append(
            Finding(
                "III_QGC_UNIT_STATUS_FAILED",
                completed.stderr.strip()
                or "systemctl --user could not read iii-qgc.service.",
                field="unit",
            )
        )
    elif unit.get("LoadState") != "loaded":
        findings.append(
            Finding(
                "III_QGC_UNIT_MISSING",
                f"User service {UNIT_NAME} is not loaded (LoadState={unit.get('LoadState', 'unknown')}).",
                field="unit",
            )
        )
    elif not _unit_selects_binary(unit, selection["binary"]):
        findings.append(
            Finding(
                "III_QGC_UNIT_BINARY_MISMATCH",
                f"{UNIT_NAME} does not select the pinned AppImage at {selection['binary']}.",
                field="unit",
            )
        )
    if findings:
        return _result(
            "status",
            outcome=Outcome.FAILED,
            code="III_QGC_STATUS_INVALID",
            summary="Pinned QGroundControl or its user service is unavailable or mismatched.",
            findings=tuple(findings),
            selection=selection,
            unit=unit,
        )
    return _result(
        "status",
        outcome=Outcome.SUCCESS,
        code="III_QGC_STATUS_INSPECTED",
        summary=f"Pinned QGroundControl is selected; user service is {unit.get('ActiveState', 'unknown')}.",
        selection=selection,
        unit=unit,
    )


def lifecycle(args: argparse.Namespace) -> CommandResult:
    action = args.qgc_action
    selection, findings = _selection(args)
    if action in {"start", "restart"}:
        if findings:
            return _result(
                action,
                outcome=Outcome.REJECTED,
                code="III_QGC_START_REJECTED",
                summary="QGroundControl was not started because its pin verification failed.",
                findings=tuple(findings),
                selection=selection,
            )
        machine = platform.machine().lower()
        if machine not in {PINNED_ARCHITECTURE, "amd64"}:
            return _result(
                action,
                outcome=Outcome.REJECTED,
                code="III_QGC_ARCHITECTURE_UNSUPPORTED",
                summary=f"Pinned QGroundControl requires a Linux x86_64 host; detected {machine or 'unknown'}.",
                findings=(
                    Finding(
                        "III_QGC_ARCHITECTURE_UNSUPPORTED",
                        f"host architecture is {machine or 'unknown'}",
                        field="architecture",
                    ),
                ),
                selection=selection,
            )
        unit_before, unit_check = _read_unit()
        if unit_check.returncode != 0 or unit_before.get("LoadState") != "loaded":
            detail = unit_check.stderr.strip() or (
                f"{UNIT_NAME} is not loaded (LoadState={unit_before.get('LoadState', 'unknown')})."
            )
            return _result(
                action,
                outcome=Outcome.REJECTED,
                code="III_QGC_UNIT_MISSING",
                summary=f"QGroundControl was not {action}ed because {UNIT_NAME} is unavailable.",
                findings=(Finding("III_QGC_UNIT_MISSING", detail, field="unit"),),
                selection=selection,
                unit=unit_before,
            )
        if not _unit_selects_binary(unit_before, selection["binary"]):
            return _result(
                action,
                outcome=Outcome.REJECTED,
                code="III_QGC_UNIT_BINARY_MISMATCH",
                summary=f"QGroundControl was not {action}ed because {UNIT_NAME} selects another binary.",
                findings=(
                    Finding(
                        "III_QGC_UNIT_BINARY_MISMATCH",
                        f"expected {selection['binary']}",
                        field="unit",
                    ),
                ),
                selection=selection,
                unit=unit_before,
            )
    forced_stop = False
    if action in {"stop", "restart"}:
        completed = _stop_service()
        forced_stop = completed.returncode == 0 and bool(completed.stderr)
        if action == "restart" and completed.returncode == 0:
            completed = _systemctl("start", UNIT_NAME)
    else:
        completed = _systemctl(action, UNIT_NAME)
    unit, inspect_result = _read_unit()
    if completed.returncode != 0:
        detail = (
            completed.stderr.strip()
            or f"systemctl --user {action} failed with status {completed.returncode}."
        )
        failure_code = (
            "III_QGC_STOP_FAILED" if action == "stop" else "III_QGC_LIFECYCLE_FAILED"
        )
        return _result(
            action,
            outcome=Outcome.FAILED,
            code=failure_code,
            summary=f"Could not {action} {UNIT_NAME}.",
            findings=(Finding(failure_code, detail, field="unit"),),
            selection=selection,
            unit=unit,
        )
    if action in {"start", "restart"} and inspect_result.returncode == 0:
        if not _unit_selects_binary(unit, selection["binary"]):
            return _result(
                action,
                outcome=Outcome.FAILED,
                code="III_QGC_UNIT_BINARY_MISMATCH",
                summary=f"{UNIT_NAME} does not select the pinned AppImage.",
                findings=(
                    Finding(
                        "III_QGC_UNIT_BINARY_MISMATCH",
                        f"expected {selection['binary']}",
                        field="unit",
                    ),
                ),
                selection=selection,
                unit=unit,
            )
    if action in {"start", "restart"}:
        if inspect_result.returncode != 0:
            detail = inspect_result.stderr.strip() or (
                "systemctl --user could not verify the service after the lifecycle command."
            )
            return _result(
                action,
                outcome=Outcome.FAILED,
                code="III_QGC_READINESS_FAILED",
                summary=f"{UNIT_NAME} did not reach a verifiable active state after {action}.",
                findings=(Finding("III_QGC_READINESS_FAILED", detail, field="unit"),),
                selection=selection,
                unit=unit,
            )
        if unit.get("ActiveState") != "active":
            active_state = unit.get("ActiveState", "unknown")
            sub_state = unit.get("SubState", "unknown")
            return _result(
                action,
                outcome=Outcome.FAILED,
                code="III_QGC_READINESS_FAILED",
                summary=f"{UNIT_NAME} did not become active after {action}.",
                findings=(
                    Finding(
                        "III_QGC_READINESS_FAILED",
                        f"post-{action} state is ActiveState={active_state}, SubState={sub_state}",
                        field="unit",
                    ),
                ),
                selection=selection,
                unit=unit,
            )
    if action == "stop":
        if inspect_result.returncode != 0 or unit.get("ActiveState") != "inactive":
            detail = inspect_result.stderr.strip() or (
                "post-stop state is "
                f"ActiveState={unit.get('ActiveState', 'unknown')}, "
                f"SubState={unit.get('SubState', 'unknown')}"
            )
            return _result(
                action,
                outcome=Outcome.FAILED,
                code="III_QGC_STOP_FAILED",
                summary=f"{UNIT_NAME} did not stop cleanly.",
                findings=(Finding("III_QGC_STOP_FAILED", detail, field="unit"),),
                selection=selection,
                unit=unit,
            )
    return _result(
        action,
        outcome=Outcome.SUCCESS,
        code="III_QGC_LIFECYCLE_COMPLETED",
        summary=(
            f"QGroundControl user service {action} completed after a forced stop."
            if forced_stop
            else f"QGroundControl user service {action} command completed."
        ),
        findings=(
            (
                Finding(
                    "III_QGC_FORCED_STOP",
                    "QGroundControl did not exit within five seconds of SIGINT; its remaining service processes were killed.",
                    severity="warning",
                    field="unit",
                ),
            )
            if forced_stop
            else ()
        ),
        selection=selection,
        unit=unit,
    )


def initialize(parser: argparse.ArgumentParser) -> None:
    subparsers = parser.add_subparsers(dest="qgc_action", required=True)
    for action, help_text in (
        ("start", "start the pinned host QGroundControl service"),
        ("status", "inspect the pin and QGroundControl service state"),
        ("stop", "stop the host QGroundControl service"),
        ("restart", "restart the pinned host QGroundControl service"),
    ):
        action_parser = subparsers.add_parser(action, help=help_text)
        action_parser.set_defaults(
            func=status if action == "status" else lifecycle,
            _iii_mutating=action in {"start", "stop", "restart"},
        )
