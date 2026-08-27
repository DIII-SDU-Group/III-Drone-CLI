from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from iii import qgc
from iii.__main__ import build_parser
from iii.runner import inventory_parser


def _state_id(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _host(tmp_path: Path):
    unit = tmp_path / ".config/systemd/user/iii-qgc.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("[Service]\nExecStart=/managed/QGroundControl.AppImage\n")
    unit.chmod(0o644)
    application = tmp_path / "applications"
    state_root = tmp_path / "state"
    digest = hashlib.sha256(b"pinned-qgc").hexdigest()
    slot = application / "qgc/slots" / digest
    slot.mkdir(parents=True)
    binary = slot / "QGroundControl.AppImage"
    binary.write_bytes(b"pinned-qgc")
    binary.chmod(0o555)
    (application / "qgc/current").symlink_to(Path("slots") / digest)
    state_root.mkdir()
    state = {
        "schema": "iii.gc-application-state/v1",
        "generation": 1,
        "active_release_id": "a" * 64,
        "previous_release_id": None,
        "staged_release_id": None,
        "qualified_anchor_release_id": "a" * 64,
        "active_qgc_sha256": digest,
        "previous_qgc_sha256": None,
        "releases": {
            "a" * 64: {"qgroundcontrol": {"version": "5.0.8", "sha256": digest}}
        },
    }
    state["state_id"] = _state_id(state)
    (state_root / "application-state.json").write_text(json.dumps(state))
    environment = {
        "III_GC_APPLICATION_ROOT": str(application),
        "III_GC_APPLICATION_STATE_ROOT": str(state_root),
    }
    return environment, binary, unit


def _args(environment, action="start", retained=None):
    return SimpleNamespace(
        qgc_action=action,
        require_selected=False,
        _iii_environment=environment,
        _iii_retained_plan=retained,
    )


def test_parser_inventory_declares_strict_qgc_namespace():
    inventory = inventory_parser(build_parser())
    assert inventory[("qgc", "status")].mutating is False
    for action in ("start", "stop", "restart"):
        assert inventory[("qgc", action)].mutating is True
        assert inventory[("qgc", action)].plan_provider is qgc.lifecycle_preflight
    for action in ("apply", "cache", "capture", "promote"):
        leaf = inventory[("qgc", "config", action)]
        assert leaf.mutating is True
        assert leaf.plan_provider is not None
    for action in ("diff", "verify-cache"):
        assert inventory[("qgc", "config", action)].mutating is False


def test_status_authenticates_selected_checksum_and_never_mutates_gc(
    tmp_path, monkeypatch
):
    environment, binary, _unit = _host(tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(
        qgc,
        "_systemctl_show",
        lambda: {"LoadState": "loaded", "ActiveState": "inactive", "SubState": "dead"},
    )
    result = qgc.status(_args(environment))
    assert result.outcome.value == "success"
    assert result.payload["selection"]["binary"] == str(binary)
    assert result.payload["gc_mutation"] is False
    assert result.payload["aircraft_mutation"] is False

    binary.chmod(0o755)
    binary.write_bytes(b"tampered")
    rejected = qgc.status(_args(environment))
    assert rejected.outcome.value == "rejected"
    assert "checksum" in rejected.findings[0].message


@pytest.mark.parametrize("action", ["start", "stop", "restart"])
def test_lifecycle_executes_only_the_qgc_unit(tmp_path, monkeypatch, action):
    environment, _binary, _unit = _host(tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    unit_state = {"LoadState": "loaded", "ActiveState": "inactive", "SubState": "dead"}
    monkeypatch.setattr(qgc, "_systemctl_show", lambda: dict(unit_state))
    calls = []

    def run(argv, **_kwargs):
        calls.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(qgc.subprocess, "run", run)
    args = _args(environment, action=action)
    plan = qgc._plan(args)
    args._iii_retained_plan = {"preflight": plan}
    result = qgc.lifecycle(args)
    assert result.outcome.value == "success"
    assert calls == [["systemctl", "--user", action, "iii-qgc.service"]]
    assert all(not any(unit in part for unit in qgc.GC_UNITS) for part in calls[0])


def test_qgc_unit_definition_cannot_cross_gc_lifecycle(tmp_path, monkeypatch):
    environment, _binary, unit = _host(tmp_path)
    unit.write_text("[Unit]\nWants=iii-gc.target\n")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(qgc, "_systemctl_show", lambda: {})
    with pytest.raises(ValueError, match="crosses"):
        qgc._plan(_args(environment))


def test_unselected_status_warns_but_start_fails_closed(tmp_path, monkeypatch):
    environment, _binary, _unit = _host(tmp_path)
    (
        Path(environment["III_GC_APPLICATION_STATE_ROOT"]) / "application-state.json"
    ).unlink()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(qgc, "_systemctl_show", lambda: {})
    assert qgc.status(_args(environment)).outcome.value == "warning"
    with pytest.raises(ValueError, match="no III-managed"):
        qgc._plan(_args(environment, action="start"))
