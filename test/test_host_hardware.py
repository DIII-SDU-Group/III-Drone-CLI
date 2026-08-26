from __future__ import annotations

import argparse
import json
from pathlib import Path

from iii import host
from iii.__main__ import build_parser
from iii.result import Outcome
from iii.runner import inventory_parser


def _report(*, accepted: bool = True) -> dict:
    return {
        "schema": "iii.hardware-inspection/v1",
        "inspection_id": "a" * 64,
        "manifest_id": "b" * 64,
        "accepted": accepted,
        "roles": {
            "cable_camera": {
                "requirement": "required",
                "state": "present" if accepted else "ambiguous",
                "stable_path_ok": accepted,
            },
            "optional_debug_adapter": {
                "requirement": "optional",
                "state": "missing",
                "stable_path_ok": False,
            },
        },
        "unmatched_device_ids": ["c" * 64],
        "automatic_learning": False,
    }


def _args(tmp_path: Path, *, capture: bool = False) -> argparse.Namespace:
    return argparse.Namespace(
        target="real",
        capture=tmp_path / "hardware.json" if capture else None,
        _iii_hardware_command="iii host inspect",
        _iii_inspection_scope="hardware",
        _iii_environment={},
    )


def test_both_host_inspection_spellings_are_declared_read_only() -> None:
    inventory = inventory_parser(build_parser())
    assert inventory[("host", "inspect")].mutating is False
    assert inventory[("host", "hardware", "inspect")].mutating is False


def test_report_validation_binds_schema_identity_manifest_and_profile(tmp_path: Path):
    from iii_deployment.contracts import ContractRegistry, content_identity
    from iii_deployment.hardware_roles import inspect_hardware, load_manifest

    root = Path(__file__).resolve().parents[3]
    registry = ContractRegistry(root / "deployment/schemas/v1")
    manifest = load_manifest(
        root / "deployment/hardware/shared-hardware-role-manifest.json", registry
    )
    report = inspect_hardware(
        manifest,
        [],
        profile="real",
        boot_id="boot-a",
        captured_monotonic_ns=1,
    )
    args = _args(tmp_path)
    args.schema_root = root / "deployment/schemas/v1"
    selected = {"runtime_profile": "real"}
    host._validate_hardware_report(args, report, selected)

    report["profile"] = "opti_track"
    report["inspection_id"] = content_identity(
        {key: value for key, value in report.items() if key != "inspection_id"}
    )
    try:
        host._validate_hardware_report(args, report, selected)
    except ValueError as exc:
        assert "profile differs" in str(exc)
    else:
        raise AssertionError("profile mismatch was accepted")


def test_host_inspect_uses_authenticated_receiver_and_saves_sanitized_capture(
    monkeypatch, tmp_path: Path
) -> None:
    calls = []

    class Manager:
        client_id = "d" * 64

        def __init__(self, *, environment):
            assert environment == {}

        def receiver_request(self, request):
            calls.append(request)
            return {"inspection": _report()}

    selected = {
        "endpoint": "iii.local",
        "execution_host": "aircraft",
        "logical_id": "drone",
        "runtime_profile": "real",
    }
    monkeypatch.setattr(host, "_hardware_target", lambda _args: selected)
    monkeypatch.setattr(host, "_validate_hardware_report", lambda *args: None)
    monkeypatch.setattr("iii.ssh_manager.SSHManager", Manager)

    args = _args(tmp_path, capture=True)
    result = host.hardware_inspect(args)

    assert result.outcome is Outcome.SUCCESS
    assert calls[0]["action"] == "hardware-inspect"
    assert calls[0]["nonce"] is None
    assert calls[0]["payload"] == {}
    assert json.loads(args.capture.read_text(encoding="utf-8")) == _report()
    assert args.capture.stat().st_mode & 0o777 == 0o600


def test_missing_required_and_optional_roles_are_not_conflated(
    monkeypatch, tmp_path: Path
) -> None:
    class Manager:
        client_id = "d" * 64

        def __init__(self, *, environment):
            pass

        def receiver_request(self, request):
            return {"inspection": _report(accepted=False)}

    monkeypatch.setattr(
        host,
        "_hardware_target",
        lambda _args: {
            "endpoint": "iii.local",
            "execution_host": "aircraft",
            "logical_id": "drone",
            "runtime_profile": "real",
        },
    )
    monkeypatch.setattr("iii.ssh_manager.SSHManager", Manager)
    monkeypatch.setattr(host, "_validate_hardware_report", lambda *args: None)
    result = host.hardware_inspect(_args(tmp_path))
    assert result.outcome is Outcome.WARNING
    assert result.code == "III_HARDWARE_NOT_READY"
    by_field = {finding.field: finding for finding in result.findings}
    assert by_field["cable_camera"].severity == "error"
    assert by_field["optional_debug_adapter"].severity == "warning"


def test_capture_never_overwrites_existing_evidence(
    monkeypatch, tmp_path: Path
) -> None:
    capture = tmp_path / "hardware.json"
    capture.write_text("preserve", encoding="utf-8")
    args = _args(tmp_path, capture=True)
    args.capture = capture
    monkeypatch.setattr(
        host,
        "_hardware_target",
        lambda _args: {
            "endpoint": "iii.local",
            "execution_host": "aircraft",
            "logical_id": "drone",
            "runtime_profile": "real",
        },
    )

    class Manager:
        client_id = "d" * 64

        def __init__(self, *, environment):
            pass

        def receiver_request(self, request):
            return {"inspection": _report()}

    monkeypatch.setattr("iii.ssh_manager.SSHManager", Manager)
    monkeypatch.setattr(host, "_validate_hardware_report", lambda *args: None)
    result = host.hardware_inspect(args)
    assert result.outcome is Outcome.REJECTED
    assert capture.read_text(encoding="utf-8") == "preserve"


def test_host_inspect_composes_hardware_and_boot_findings(monkeypatch, tmp_path: Path):
    hardware = _report(accepted=True)
    boot = {
        "schema": "iii.boot-inspection/v1",
        "inspection_id": "e" * 64,
        "profile_id": "f" * 64,
        "boot_id": "boot-a",
        "accepted": False,
        "drift": ["forbidden firmware setting force_turbo is active"],
    }
    report = {
        "schema": "iii.host-inspection/v1",
        "inspection_id": "1" * 64,
        "logical_target": "drone",
        "profile": "real",
        "boot_id": "boot-a",
        "accepted": False,
        "hardware": hardware,
        "boot": boot,
    }

    class Manager:
        client_id = "d" * 64

        def __init__(self, *, environment):
            pass

        def receiver_request(self, request):
            assert request["action"] == "host-inspect"
            return {"inspection": report}

    monkeypatch.setattr(
        host,
        "_hardware_target",
        lambda _args: {
            "endpoint": "iii.local",
            "execution_host": "aircraft",
            "logical_id": "drone",
            "runtime_profile": "real",
        },
    )
    monkeypatch.setattr(host, "_validate_host_report", lambda *args: None)
    monkeypatch.setattr("iii.ssh_manager.SSHManager", Manager)
    args = _args(tmp_path)
    args._iii_inspection_scope = "host"
    result = host.hardware_inspect(args)
    assert result.outcome is Outcome.WARNING
    assert result.code == "III_HOST_NOT_READY"
    assert any(finding.code == "III_BOOT_BASELINE_DRIFT" for finding in result.findings)
