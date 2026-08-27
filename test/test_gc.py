from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from iii import gc
from iii.__main__ import build_parser, main
from iii.runner import inventory_parser


def _state(load="loaded", active="inactive", sub="dead"):
    return {"LoadState": load, "ActiveState": active, "SubState": sub}


def _units(root: Path) -> None:
    unit_root = root / ".config/systemd/user"
    unit_root.mkdir(parents=True)
    for unit in gc.MANAGED_UNITS:
        path = unit_root / unit
        path.write_text(f"[Unit]\nDescription={unit}\n")
        path.chmod(0o644)


def test_parser_inventory_declares_every_gc_leaf_and_mutation(monkeypatch):
    monkeypatch.setattr(gc, "_systemctl_show", lambda _unit: _state())
    parser = build_parser()

    inventory = inventory_parser(parser)

    assert inventory[("gc", "status")].mutating is False
    for leaf in ("provision", "start", "stop", "restart", "open"):
        assert inventory[("gc", leaf)].mutating is True
    assert inventory[("gc", "provision")].plan_provider is gc.provision_preflight


def test_lifecycle_plan_is_local_only_and_detects_stale_unit_state(
    tmp_path, monkeypatch
):
    _units(tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    states = {unit: _state() for unit in gc.MANAGED_UNITS}
    monkeypatch.setattr(gc, "_systemctl_show", lambda unit: dict(states[unit]))
    plan = gc._lifecycle_plan("start")

    assert plan["aircraft_mutation"] is False
    assert plan["browser_automatic"] is False
    assert plan["units"][gc.TARGET_UNIT]["ActiveState"] == "inactive"
    assert set(plan["unit_definitions"]) == set(gc.MANAGED_UNITS)

    states[gc.TARGET_UNIT]["ActiveState"] = "active"
    args = SimpleNamespace(
        gc_action="start",
        _iii_retained_plan={"preflight": plan},
        _iii_environment={},
    )
    result = gc.lifecycle(args)
    assert result.outcome.value == "rejected"
    assert "changed after planning" in result.findings[0].message


def test_start_and_open_touch_only_local_user_units(tmp_path, monkeypatch):
    _units(tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(gc, "_systemctl_show", lambda _unit: _state())
    commands = []

    def run(argv, **_kwargs):
        commands.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(gc.subprocess, "run", run)
    for action in ("start", "open"):
        plan = gc._lifecycle_plan(action)
        args = SimpleNamespace(
            gc_action=action,
            _iii_retained_plan={"preflight": plan},
            _iii_environment={},
        )
        result = gc.lifecycle(args)
        assert result.outcome.value == "success"
        assert result.payload["aircraft_mutation"] is False

    assert commands[0] == ["systemctl", "--user", "start", gc.TARGET_UNIT]
    assert commands[1] == [
        "systemctl",
        "--user",
        "start",
        gc.TARGET_UNIT,
        gc.BROWSER_UNIT,
    ]
    assert all("iii.local" not in part for command in commands for part in command)


def test_lifecycle_plan_rejects_any_changed_managed_unit(tmp_path, monkeypatch):
    _units(tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(gc, "_systemctl_show", lambda _unit: _state())
    plan = gc._lifecycle_plan("start")

    (tmp_path / ".config/systemd/user/iii-gc-proxy.service").write_text(
        "[Service]\nExecStart=/bin/false\n"
    )
    args = SimpleNamespace(
        gc_action="start",
        _iii_retained_plan={"preflight": plan},
        _iii_environment={},
    )

    result = gc.lifecycle(args)

    assert result.outcome.value == "rejected"
    assert "changed after planning" in result.findings[0].message


def test_lifecycle_plan_rejects_unsafe_unit_permissions(tmp_path, monkeypatch):
    _units(tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(gc, "_systemctl_show", lambda _unit: _state())
    (tmp_path / ".config/systemd/user/iii-gc-proxy.service").chmod(0o666)

    with pytest.raises(ValueError, match="ownership or mode"):
        gc._lifecycle_plan("start")


def test_status_uses_canonical_result_and_warns_on_missing_baseline(
    monkeypatch, tmp_path
):
    import sys
    import types

    report = {
        "schema": "iii.gc-host-status/v1",
        "platform": {"platform_id": "ubuntu-24.04-x86_64"},
        "paths": [{"path": ".config/iii", "exists": False}],
        "units": [
            {
                "unit": "iii-gc-proxy.service",
                "load_state": "unavailable",
                "active_state": "unavailable",
                "unit_file_state": "unavailable",
            }
        ],
        "machine_identity": False,
        "ssh_key": False,
        "status_id": "a" * 64,
    }
    package = types.ModuleType("iii_deployment")
    host = types.ModuleType("iii_deployment.gc_host")
    host.inspect_status = lambda **_kwargs: report
    package.gc_host = host
    monkeypatch.setitem(sys.modules, "iii_deployment", package)
    monkeypatch.setitem(sys.modules, "iii_deployment.gc_host", host)
    monkeypatch.setattr(gc, "_workspace", lambda: tmp_path)
    monkeypatch.setattr(gc, "_schema_root", lambda _args: tmp_path)

    result = gc.status(SimpleNamespace())

    assert result.outcome.value == "warning"
    assert result.payload == report
    assert {finding.code for finding in result.findings} == {
        "III_GC_PATH_MISSING",
        "III_GC_UNIT_UNAVAILABLE",
    }


def test_gc_help_and_status_are_covered_by_universal_json_contract(tmp_path):
    stdout = __import__("io").StringIO()
    stderr = __import__("io").StringIO()

    code = main(["gc", "--help", "--json"], stdout=stdout, stderr=stderr)
    payload = json.loads(stdout.getvalue())

    assert code == 0
    assert payload["schema"] == "iii.command-result/v1"
    assert payload["command"] == "iii gc --help"
    assert "provision" in payload["payload"]["help"]
    assert stderr.getvalue() == ""


def test_gc_help_parser_is_ros_free_in_an_isolated_python_process():
    cli_root = Path(__file__).parents[1]
    source = (
        "import json,sys;"
        f"sys.path.insert(0,{str(cli_root)!r});"
        "from iii.__main__ import main;"
        "raise SystemExit(main(['gc','provision','--help','--json']))"
    )

    completed = subprocess.run(
        [sys.executable, "-I", "-c", source],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["command"] == "iii gc provision --help"
    assert payload["outcome"] == "success"
