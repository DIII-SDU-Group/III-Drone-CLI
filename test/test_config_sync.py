from __future__ import annotations

from io import StringIO
import json
from pathlib import Path
import subprocess

import pytest
import yaml

from iii import config_sync, runtime_routing
from iii.__main__ import build_parser, main
from iii.runner import inventory_parser

BATTERY = "/inspection_demo/battery_voltage_threshold_v"
RIGID_BODY = "/opti_track/pose_relay/rigid_body_id"
STICK = "/mission/manual_stick_input_threshold"
REPOSITORY = Path(__file__).resolve().parents[3]


def _contract() -> Path:
    from iii_drone_configuration import resolve_installed_contract_root

    return resolve_installed_contract_root()


def _installed_default(family: str) -> dict:
    from iii_drone_configuration import load_installed_contract

    root = _contract()
    contract = load_installed_contract(root).contract
    return yaml.safe_load((root / contract.default_set(family).path).read_text(encoding="utf-8"))


def _workspace(tmp_path: Path, family: str = "real") -> Path:
    """A workspace whose source tracked default is the installed one."""

    workspace = tmp_path / "workspace"
    default = workspace / config_sync.SOURCE_PARAMETER_SETS / family / "tracked/default.yaml"
    default.parent.mkdir(parents=True)
    default.write_text(yaml.safe_dump(_installed_default(family), sort_keys=False))
    profiles = workspace / config_sync.SOURCE_PROFILES
    profiles.parent.mkdir(parents=True)
    profiles.write_text((REPOSITORY / config_sync.SOURCE_PROFILES).read_text(encoding="utf-8"))
    return workspace


def _environment(workspace: Path) -> dict[str, str]:
    return {
        "CLI_CONFIGURATION": "dev",
        "WORKSPACE_DIR": str(workspace),
        "III_CONFIGURATION_CONTRACT_ROOT": str(_contract()),
        "CONFIG_BASE_DIR": str(workspace / ".config"),
        "III_OPERATIONS_ROOT": str(workspace / ".iii/operations"),
        "III_OPERATION_STATE_DIR": str(workspace / ".iii/operations"),
    }


def _seed(workspace: Path, profile: str = "opti_track") -> Path:
    """The living set a first start of the profile leaves behind."""

    from iii_drone_configuration import load_installed_contract, reconcile_simulation_startup

    contract = load_installed_contract(_contract()).contract
    living = workspace / ".config/iii_drone"
    result = reconcile_simulation_startup(
        immutable_root=_contract(),
        writable_state_root=living,
        operations_root=workspace / ".iii/operations",
        runtime_profile=profile,
        target_id=profile,
        release_id=contract.manifest_id,
    )
    assert result.status == "complete"
    return living / "parameter_sets" / profile / "tracked/default.yaml"


def _values(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))["/**"]["ros__parameters"]


def _set(path: Path, **values) -> None:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["/**"]["ros__parameters"].update(values)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _invoke(argv: list[str], environment: dict[str, str]) -> tuple[int, dict, str]:
    stdout, stderr = StringIO(), StringIO()
    status = main([*argv, "--json"], stdout=stdout, stderr=stderr, environment=environment)
    return status, json.loads(stdout.getvalue()), stderr.getvalue()


@pytest.fixture(autouse=True)
def _system_is_down(monkeypatch):
    monkeypatch.setattr(config_sync, "stack_running", lambda: False)


def test_sync_is_a_mutating_runtime_command():
    spec = inventory_parser(build_parser())[("config", "sync")]
    assert spec.mutating is True
    assert runtime_routing.is_runtime_path(("config", "sync"))
    assert runtime_routing.is_runtime_path(("config", "sim", "inspect"))


def test_keep_mode_reports_the_drift_and_writes_nothing(tmp_path, capsys):
    workspace = _workspace(tmp_path)
    living = _seed(workspace)
    tuned = _values(living)[STICK] + 0.05
    _set(living, **{STICK: tuned})
    before = living.read_bytes()

    status, result, _ = _invoke(
        ["config", "sync", "--profile", "opti_track", "--confirm"], _environment(workspace)
    )

    assert status == 0, result
    assert result["code"] == "III_CONFIG_SYNC_UNCHANGED"
    assert [item["name"] for item in result["payload"]["drift"]] == [STICK]
    assert result["payload"]["changes"] == []
    assert living.read_bytes() == before
    assert not (living.parent.parent / "snapshots").exists()
    assert "Mode keep: nothing is written" in capsys.readouterr().err


def test_dry_run_of_defaults_lists_the_changes_without_writing(tmp_path):
    workspace = _workspace(tmp_path)
    living = _seed(workspace)
    _set(living, **{STICK: _values(living)[STICK] + 0.05})
    before = living.read_bytes()

    status, result, _ = _invoke(
        ["config", "sync", "--profile", "opti_track", "--mode", "defaults", "--dry-run"],
        _environment(workspace),
    )

    assert status == 0, result
    assert result["code"] == "III_OPERATION_PLAN_READY"
    changes = result["payload"]["plan"]["preflight"]["changes"]
    assert [item["name"] for item in changes] == [STICK]
    assert living.read_bytes() == before


def test_defaults_mode_snapshots_the_replaced_set_then_writes_the_default(tmp_path):
    workspace = _workspace(tmp_path)
    living = _seed(workspace)
    default = _values(living)[STICK]
    _set(living, **{STICK: default + 0.05, BATTERY: 21.0})
    tuned = living.read_bytes()

    status, result, _ = _invoke(
        ["config", "sync", "--profile", "opti_track", "--mode", "defaults", "--confirm"],
        _environment(workspace),
    )

    assert status == 0, result
    assert result["code"] == "III_CONFIG_SYNC_APPLIED"
    assert sorted(result["payload"]["result"]["written"]) == sorted([STICK, BATTERY])
    values = _values(living)
    assert values[STICK] == pytest.approx(default)
    assert values == _installed_default("real")["/**"]["ros__parameters"]
    # Never overwrite: the tuned set is kept, byte for byte, as a snapshot.
    snapshots = list((living.parent.parent / "snapshots").glob("pre_sync_*.yaml"))
    assert [str(path) for path in snapshots] == result["payload"]["result"]["snapshots"]
    assert snapshots[0].read_bytes() == tuned

    # A second run has nothing to write and adds no snapshot.
    status, result, _ = _invoke(
        ["config", "sync", "--profile", "opti_track", "--mode", "defaults", "--confirm"],
        _environment(workspace),
    )
    assert result["code"] == "III_CONFIG_SYNC_UNCHANGED"
    assert len(list((living.parent.parent / "snapshots").glob("*.yaml"))) == 1


def test_a_parameter_list_limits_what_is_written(tmp_path):
    workspace = _workspace(tmp_path)
    living = _seed(workspace)
    tuned_stick = _values(living)[STICK] + 0.05
    _set(living, **{STICK: tuned_stick, BATTERY: 21.0})

    status, result, _ = _invoke(
        ["config", "sync", "--profile", "opti_track", "--mode", "defaults",
         "--parameter", BATTERY, "--confirm"],
        _environment(workspace),
    )

    assert status == 0, result
    assert result["payload"]["result"]["written"] == [BATTERY]
    values = _values(living)
    assert values[BATTERY] == _installed_default("real")["/**"]["ros__parameters"][BATTERY]
    assert values[STICK] == pytest.approx(tuned_stick)  # the field tune stays


def test_an_edited_checkout_default_is_applied_without_a_build(tmp_path):
    workspace = _workspace(tmp_path)
    living = _seed(workspace)
    source = workspace / config_sync.SOURCE_PARAMETER_SETS / "real/tracked/default.yaml"
    _set(source, **{RIGID_BODY: 10})

    status, result, _ = _invoke(
        ["config", "sync", "--profile", "opti_track", "--mode", "defaults", "--confirm"],
        _environment(workspace),
    )

    assert status == 0, result
    assert result["payload"]["default_source"] == str(source)
    assert _values(living)[RIGID_BODY] == 10


def test_an_active_snapshot_set_is_left_in_place_and_the_default_is_selected(tmp_path):
    workspace = _workspace(tmp_path)
    living = _seed(workspace)
    field = living.parent.parent / "snapshots/field.yaml"
    field.parent.mkdir()
    field.write_bytes(living.read_bytes())
    _set(field, **{BATTERY: 21.0})
    selector = workspace / ".config/iii_drone/profiles/opti_track.yaml"
    selector.write_text("version: 1\nactive_parameter_set: snapshots/field.yaml\n")
    field_bytes = field.read_bytes()

    status, result, _ = _invoke(
        ["config", "sync", "--profile", "opti_track", "--mode", "defaults", "--confirm"],
        _environment(workspace),
    )

    assert status == 0, result
    assert field.read_bytes() == field_bytes
    assert yaml.safe_load(selector.read_text())["active_parameter_set"] == "tracked/default.yaml"
    assert _values(living)[BATTERY] == _installed_default("real")["/**"]["ros__parameters"][BATTERY]
    assert len(list(field.parent.glob("pre_sync_*.yaml"))) == 1


def test_writing_is_refused_while_the_system_runs(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)
    living = _seed(workspace)
    _set(living, **{BATTERY: 21.0})
    before = living.read_bytes()
    monkeypatch.setattr(config_sync, "stack_running", lambda: True)

    status, result, _ = _invoke(
        ["config", "sync", "--profile", "opti_track", "--mode", "defaults", "--confirm"],
        _environment(workspace),
    )

    assert status != 0
    assert result["code"] == "III_CONFIG_SYNC_REJECTED"
    assert "system is running" in result["findings"][0]["message"]
    assert living.read_bytes() == before
    assert not (living.parent.parent / "snapshots").exists()


@pytest.mark.parametrize(
    ("parameter", "expected"),
    [("/no/such_parameter", "not in the tracked default"),],
)
def test_an_unknown_parameter_is_refused_before_anything_is_planned(tmp_path, parameter, expected):
    workspace = _workspace(tmp_path)
    _seed(workspace)

    status, result, _ = _invoke(
        ["config", "sync", "--profile", "opti_track", "--mode", "defaults",
         "--parameter", parameter, "--dry-run"],
        _environment(workspace),
    )

    assert status != 0
    assert expected in json.dumps(result)


def test_a_default_newer_than_the_installed_schema_is_reported_and_left_out(tmp_path):
    workspace = _workspace(tmp_path)
    living = _seed(workspace)
    source = workspace / config_sync.SOURCE_PARAMETER_SETS / "real/tracked/default.yaml"
    _set(source, **{"/mission/not_built_yet": 1.0, BATTERY: 21.5})

    status, result, _ = _invoke(
        ["config", "sync", "--profile", "opti_track", "--mode", "defaults", "--confirm"],
        _environment(workspace),
    )

    assert status == 0, result
    assert result["payload"]["not_in_installed_schema"] == ["/mission/not_built_yet"]
    assert result["payload"]["result"]["written"] == [BATTERY]
    assert "/mission/not_built_yet" not in _values(living)


def test_ground_computer_sends_its_tracked_default_to_the_pi(monkeypatch, tmp_path):
    workspace = _workspace(tmp_path)
    root = tmp_path / "gc"
    root.mkdir()
    expected_hash = "a" * 64
    (root / "install.json").write_text(
        json.dumps(
            {
                "schema": "iii.gc-install/v1",
                "profile": "deploy",
                "checkout": {
                    "checkout": str(workspace),
                    "revision": "b" * 40,
                    "content_sha256": {runtime_routing.CLI_TREE_RELATIVE: expected_hash},
                },
            }
        ),
        encoding="utf-8",
    )
    environment = {
        "III_GC_INSTALL_ROOT": str(root),
        "III_GC_INSTALL_PROFILE": "deploy",
        "WORKSPACE_DIR": str(workspace),
        "III_OPERATION_STATE_DIR": str(tmp_path / "operations"),
    }
    preview = {
        "schema": config_sync.SCHEMA, "profile": "opti_track", "mode": "defaults",
        "parameters": [], "active_set": "tracked/default.yaml", "living_exists": True,
        "target_set": "tracked/default.yaml", "default_source": "the sender's checkout",
        "drift": [{"name": BATTERY, "living": 14.0, "default": 22.6}],
        "changes": [{"name": BATTERY, "living": 14.0, "default": 22.6}],
        "not_in_installed_schema": [],
    }
    calls = []

    def run(command, **kwargs):
        calls.append((list(command), kwargs))
        if len(calls) == 1:
            return subprocess.CompletedProcess(command, 0, json.dumps({"sha256": expected_hash}), "")
        if "--dry-run" in command[-1]:
            body = {"code": "III_OPERATION_PLAN_READY", "payload": {"plan": {"preflight": preview}}}
        else:
            body = {"code": "III_CONFIG_SYNC_APPLIED"}
        return subprocess.CompletedProcess(command, 0, json.dumps(body), "")

    monkeypatch.setattr(runtime_routing.subprocess, "run", run)
    stdout, stderr = StringIO(), StringIO()

    status = main(
        ["config", "sync", "--host", "pi.example", "--profile", "opti_track",
         "--mode", "defaults", "--confirm", "--json"],
        stdout=stdout, stderr=stderr, environment=environment,
    )

    assert status == 0, stdout.getvalue()
    sent = (workspace / config_sync.SOURCE_PARAMETER_SETS / "real/tracked/default.yaml").read_text()
    (_identity, _), (preview_command, preview_kwargs), (apply_command, apply_kwargs) = calls
    assert "iii@pi.example" in preview_command
    # The Pi compares with this checkout's default, sent on standard input.
    assert preview_kwargs["input"] == sent and apply_kwargs["input"] == sent
    assert "--defaults-stdin" in preview_command[-1] and "--dry-run" in preview_command[-1]
    assert "--defaults-stdin" in apply_command[-1] and "--confirm" in apply_command[-1]
    assert "--dry-run" not in apply_command[-1]
    assert "--host" not in apply_command[-1]
    assert BATTERY in stderr.getvalue()
    result = json.loads(stdout.getvalue())
    assert result["payload"]["runtime_result"]["code"] == "III_CONFIG_SYNC_APPLIED"
