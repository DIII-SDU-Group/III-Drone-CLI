from __future__ import annotations

from io import StringIO
import hashlib
import json
from pathlib import Path
import subprocess

import yaml

from iii.__main__ import build_parser, main
from iii.runner import inventory_parser


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    (workspace / "deps").mkdir(parents=True)
    (workspace / "tools/III-Drone-CLI").mkdir(parents=True)
    (workspace / "deps/submodule-lock.txt").write_text("fixture\n")
    return workspace


def _environment(workspace: Path, contract: Path) -> dict[str, str]:
    return {
        "CLI_CONFIGURATION": "dev",
        "WORKSPACE_DIR": str(workspace),
        "III_CONFIGURATION_CONTRACT_ROOT": str(contract),
        "CONFIG_BASE_DIR": str(workspace / ".config"),
        "III_CONFIGURATION_CHECKPOINT_ROOT": str(
            workspace / ".iii/configuration-checkpoints"
        ),
        "III_OPERATIONS_ROOT": str(workspace / ".iii/operations"),
        "III_OPERATION_STATE_DIR": str(workspace / ".iii/operations"),
        "III_ACTIVE_RELEASE_ID": "workspace-fixture-release",
    }


def _invoke(argv: list[str], environment: dict[str, str]) -> tuple[int, dict]:
    stdout = StringIO()
    stderr = StringIO()
    status = main(
        [*argv, "--json"],
        stdout=stdout,
        stderr=stderr,
        environment=environment,
    )
    value = json.loads(stdout.getvalue())
    assert stderr.getvalue() == ""
    return status, value


def _seed(contract: Path, workspace: Path) -> Path:
    from iii_drone_configuration import reconcile_simulation_startup

    living = workspace / ".config/iii_drone"
    result = reconcile_simulation_startup(
        immutable_root=contract,
        writable_state_root=living,
        operations_root=workspace / ".iii/operations",
        runtime_profile="sim",
        target_id="sim",
        release_id="workspace-fixture-release",
    )
    assert result.status == "complete"
    return living


def _contract_variant(
    tmp_path: Path, name: str, *, extra_default: float | None
) -> Path:
    from iii_drone_configuration import resolve_installed_contract_root

    root = tmp_path / name
    import shutil

    shutil.copytree(resolve_installed_contract_root(), root, symlinks=False)
    schema_path = root / "schema/parameter_manifest.yaml"
    schema = yaml.safe_load(schema_path.read_text())
    if extra_default is None:
        schema.pop("cli_review_fixture", None)
    else:
        schema["cli_review_fixture"] = {
            "gain": {
                "type": "float",
                "value": extra_default,
                "min": 0.0,
                "max": 10.0,
            }
        }
    schema_path.write_text(yaml.safe_dump(schema, sort_keys=False))
    for profile in ("real", "sim"):
        default_path = root / f"tracked_defaults/{profile}/default.yaml"
        document = yaml.safe_load(default_path.read_text())
        values = document["/**"]["ros__parameters"]
        if extra_default is None:
            values.pop("/cli_review_fixture/gain", None)
        else:
            values["/cli_review_fixture/gain"] = extra_default
        default_path.write_text(yaml.safe_dump(document, sort_keys=False))
    manifest_path = root / "package-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    for row in manifest["artifacts"]:
        row["sha256"] = hashlib.sha256((root / row["path"]).read_bytes()).hexdigest()
    for row in manifest["tracked_sets"]:
        row["sha256"] = hashlib.sha256((root / row["path"]).read_bytes()).hexdigest()
    unsigned = {key: item for key, item in manifest.items() if key != "manifest_id"}
    manifest["manifest_id"] = hashlib.sha256(
        json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    return root


def _value(path: Path, name: str):
    return yaml.safe_load(path.read_text())["/**"]["ros__parameters"][name]


def _set_value(path: Path, name: str, value) -> None:
    document = yaml.safe_load(path.read_text())
    document["/**"]["ros__parameters"][name] = value
    path.write_text(yaml.safe_dump(document, sort_keys=False))


def test_retired_bare_config_parent_is_not_a_leaf_and_sim_leaves_are_covered():
    inventory = inventory_parser(build_parser())
    assert ("config",) not in inventory
    assert inventory[("config", "sim", "inspect")].mutating is False
    assert inventory[("config", "sim", "checkpoint")].mutating is True
    assert inventory[("config", "sim", "review")].mutating is True
    assert inventory[("config", "sim", "reset")].mutating is True


def test_inspect_checkpoint_reset_and_restore_are_clone_local_and_recoverable(
    tmp_path: Path,
):
    from iii_drone_configuration import resolve_installed_contract_root

    workspace = _workspace(tmp_path)
    contract = resolve_installed_contract_root()
    environment = _environment(workspace, contract)
    living = _seed(contract, workspace)
    tracked = living / "parameter_sets/sim/tracked/default.yaml"
    immutable_default = contract / "tracked_defaults/sim/default.yaml"
    immutable_before = immutable_default.read_bytes()

    status, inspected = _invoke(["config", "sim", "inspect"], environment)
    assert status == 0
    assert inspected["code"] == "III_CONFIG_SIM_INSPECTED"
    assert inspected["payload"]["living_root"] == str(living)

    _set_value(tracked, "/control/dt", 0.3)
    status, checkpointed = _invoke(
        [
            "config",
            "sim",
            "checkpoint",
            "--operation-id",
            "cli-checkpoint-0001",
            "--confirm",
            "--non-interactive",
        ],
        environment,
    )
    assert status == 0
    checkpoint_id = checkpointed["payload"]["checkpoint"]["checkpoint_id"]

    status, reset = _invoke(
        [
            "config",
            "sim",
            "reset",
            "--operation-id",
            "cli-reset-default-0001",
            "--confirm",
            "--non-interactive",
        ],
        environment,
    )
    assert status == 0
    assert reset["payload"]["pre_reset_checkpoint"]["checkpoint_id"] == checkpoint_id
    assert _value(tracked, "/control/dt") == _value(immutable_default, "/control/dt")

    status, restored = _invoke(
        [
            "config",
            "sim",
            "reset",
            "--restore",
            checkpoint_id,
            "--operation-id",
            "cli-reset-restore-0001",
            "--confirm",
            "--non-interactive",
        ],
        environment,
    )
    assert status == 0
    assert restored["payload"]["restored_checkpoint_id"] == checkpoint_id
    assert _value(tracked, "/control/dt") == 0.3
    assert immutable_default.read_bytes() == immutable_before


def test_legacy_mutators_fail_with_canonical_next_action(tmp_path: Path):
    configuration = Path(__file__).resolve().parents[3] / "src/III-Drone-Configuration"
    target = tmp_path / "must-remain-empty"
    target.mkdir()
    commands = (
        [str(configuration / "scripts/install.sh"), str(target)],
        [
            str(configuration / "scripts/update_installed_parameters.py"),
            "ignored.yaml",
            str(target),
        ],
    )
    for command in commands:
        completed = subprocess.run(command, text=True, capture_output=True, check=False)
        assert completed.returncode == 64
        assert "iii config sim" in completed.stderr
    assert list(target.iterdir()) == []


def test_reset_rejects_checkpoint_locator_escape_before_mutation(tmp_path: Path):
    from iii_drone_configuration import resolve_installed_contract_root

    workspace = _workspace(tmp_path)
    contract = resolve_installed_contract_root()
    environment = _environment(workspace, contract)
    living = _seed(contract, workspace)
    before = {
        path.relative_to(living).as_posix(): path.read_bytes()
        for path in living.rglob("*")
        if path.is_file()
    }
    status, result = _invoke(
        [
            "config",
            "sim",
            "reset",
            "--restore",
            "../" + "a" * 64,
            "--operation-id",
            "cli-reset-escape-0001",
            "--confirm",
            "--non-interactive",
        ],
        environment,
    )
    assert status == 20
    assert result["code"] == "III_OPERATION_ERROR"
    assert before == {
        path.relative_to(living).as_posix(): path.read_bytes()
        for path in living.rglob("*")
        if path.is_file()
    }


def test_sim_review_cli_blocks_then_seals_complete_decisions_and_resumes(
    tmp_path: Path,
):
    from iii_drone_configuration import execute_reconciliation, plan_reconciliation

    old = _contract_variant(tmp_path, "contract-old", extra_default=2.0)
    removed = _contract_variant(tmp_path, "contract-removed", extra_default=None)
    reintroduced = _contract_variant(
        tmp_path, "contract-reintroduced", extra_default=3.0
    )
    workspace = _workspace(tmp_path)
    living = _seed(old, workspace)
    tracked = living / "parameter_sets/sim/tracked/default.yaml"
    _set_value(tracked, "/cli_review_fixture/gain", 7.0)
    retire = plan_reconciliation(
        old_immutable_root=old,
        new_immutable_root=removed,
        writable_state_root=living,
        operations_root=workspace / ".iii/operations",
        operation_id="cli-retire-review-0001",
        runtime_profile="sim",
        target_id="sim",
        old_release_id="workspace-fixture-release",
        new_release_id="workspace-removed-release",
        mode="simulation",
        purpose="startup",
    )
    assert execute_reconciliation(retire).status == "complete"
    assert (
        "/cli_review_fixture/gain"
        not in yaml.safe_load(tracked.read_text())["/**"]["ros__parameters"]
    )
    environment = _environment(workspace, reintroduced)
    environment["III_ACTIVE_RELEASE_ID"] = "workspace-reintroduced-release"

    status, inspected = _invoke(["config", "sim", "inspect"], environment)
    assert status == 0
    assert inspected["payload"]["reconciliation"]["review_required"] is True
    status, reviewed = _invoke(
        [
            "config",
            "sim",
            "review",
            "--decision",
            "tracked/default.yaml:/cli_review_fixture/gain=use_old",
            "--operation-id",
            "cli-apply-review-0001",
            "--confirm",
            "--non-interactive",
        ],
        environment,
    )
    assert status == 0
    assert reviewed["code"] == "III_CONFIG_SIM_REVIEW_APPLIED"
    assert _value(tracked, "/cli_review_fixture/gain") == 7.0
    assert (
        _value(
            reintroduced / "tracked_defaults/sim/default.yaml",
            "/cli_review_fixture/gain",
        )
        == 3.0
    )
