from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from iii import deploy_missions, vehicle_gate
from iii.__main__ import build_parser
from iii.runner import inventory_parser

REPOSITORY = Path(__file__).resolve().parents[3]
HOVER_TREE = "behavior_trees/opti_track_hover_tree.xml"


def _share(package: str) -> Path:
    try:
        from ament_index_python.packages import get_package_share_directory

        share = Path(get_package_share_directory(package))
    except Exception:  # noqa: BLE001
        pytest.skip(f"{package} is not installed in this environment")
    if package == "iii_drone_mission" and not (share / deploy_missions.NODE_CONTRACT).exists():
        pytest.skip("the installed Mission package predates the installed node contract")
    return share


@pytest.fixture
def pi(tmp_path):
    """A Pi workspace (merged install) holding this environment's built Mission catalog."""

    workspace = tmp_path / "pi-ws"
    share = workspace / "install/share"
    shutil.copytree(
        _share("iii_drone_mission"), share / "iii_drone_mission", ignore_dangling_symlinks=True
    )
    shutil.copytree(
        _share("iii_drone_interfaces"), share / "iii_drone_interfaces", ignore_dangling_symlinks=True
    )
    generator = workspace / "install/lib/python3.12/site-packages/iii_drone_mission/mission_catalog.py"
    generator.parent.mkdir(parents=True)
    shutil.copy(REPOSITORY / deploy_missions.MISSION_SOURCE / "iii_drone_mission/mission_catalog.py", generator)
    return workspace


@pytest.fixture
def checkout(tmp_path):
    """A workspace whose Mission sources are the ones the Pi was built from."""

    workspace = tmp_path / "checkout"
    shutil.copytree(
        REPOSITORY / deploy_missions.MISSION_SOURCE,
        workspace / deploy_missions.MISSION_SOURCE,
        ignore=shutil.ignore_patterns("__pycache__", ".git", "build", "install"),
    )
    return workspace


@pytest.fixture
def remote(monkeypatch, pi):
    """Run the Pi's ssh and rsync commands against the local Pi workspace."""

    calls = {"ssh": [], "rsync": [], "restart": 0}

    def ssh(_args, script, *, capture_bytes=False):
        calls["ssh"].append(script)
        if script == deploy_missions.RESTART:
            calls["restart"] += 1
            return subprocess.CompletedProcess(script, 0, "", "")
        return subprocess.run(["bash", "-c", script], capture_output=True, text=not capture_bytes)

    def rsync(_args, source, remote_path):
        calls["rsync"].append(remote_path)
        destination = Path(remote_path)
        destination.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, destination, dirs_exist_ok=True, symlinks=False)
        return subprocess.CompletedProcess("rsync", 0, "", "")

    monkeypatch.setattr(deploy_missions, "_ssh", ssh)
    monkeypatch.setattr(deploy_missions, "_rsync", rsync)
    monkeypatch.setattr(
        vehicle_gate,
        "evaluate",
        lambda *_args, **_kwargs: vehicle_gate.RestartGate(True, "opti_track", "the aircraft is disarmed and landed"),
    )
    return calls


def _args(pi: Path, checkout: Path, **overrides):
    values = {
        "host": "iii.local",
        "user": "iii",
        "remote_workspace": str(pi),
        "force": False,
        "_iii_dry_run": False,
        "_iii_environment": {"WORKSPACE_DIR": str(checkout)},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _installed_catalog(pi: Path) -> dict:
    return json.loads((pi / "install/share/iii_drone_mission/mission_catalog/catalog.json").read_text())


def _edit_hover_tree(checkout: Path) -> Path:
    tree = checkout / deploy_missions.MISSION_SOURCE / HOVER_TREE
    tree.write_text(tree.read_text().replace('value="0,0,1.5"', 'value="0,0,1.6"'))
    return tree


def test_missions_is_a_direct_deploy_command():
    spec = inventory_parser(build_parser())[("deploy", "missions")]
    assert spec.direct_mutation is True and spec.mutating is False


def test_an_unchanged_checkout_installs_nothing(pi, checkout, remote):
    before = _installed_catalog(pi)

    result = deploy_missions.deploy(_args(pi, checkout))

    assert result.code == "III_DEPLOY_MISSIONS_UNCHANGED", result.findings
    assert _installed_catalog(pi) == before
    assert remote["restart"] == 0 and remote["rsync"] == []


def test_dry_run_lists_a_changed_tree_without_touching_the_pi(pi, checkout, remote):
    _edit_hover_tree(checkout)
    before = _installed_catalog(pi)

    result = deploy_missions.deploy(_args(pi, checkout, _iii_dry_run=True))

    assert result.code == "III_DEPLOY_MISSIONS_PREVIEW", result.findings
    assert result.payload["changes"] == [
        {"id": "opti-track-hover", "change": "changed", "files": HOVER_TREE}
    ]
    assert _installed_catalog(pi) == before
    assert remote["restart"] == 0 and remote["rsync"] == []


def test_a_changed_tree_is_installed_with_a_backup_and_a_restart(pi, checkout, remote):
    tree = _edit_hover_tree(checkout)
    share = pi / "install/share/iii_drone_mission"
    before = (share / "mission_catalog/catalog.json").read_bytes()

    result = deploy_missions.deploy(_args(pi, checkout))

    assert result.code == "III_DEPLOY_MISSIONS_INSTALLED", result.findings
    catalog = _installed_catalog(pi)
    hover = next(entry for entry in catalog["entries"] if entry["id"] == "opti-track-hover")
    asset = next(asset for asset in hover["assets"] if asset["logical_name"] == HOVER_TREE)
    stored = share / "mission_catalog/assets/sha256" / asset["asset_id"].removeprefix("sha256:")
    assert stored.read_bytes() == tree.read_bytes()
    # The previous catalog is kept, and no staging directory is left behind.
    backup = Path(result.payload["backup"])
    assert (backup / "mission_catalog/catalog.json").read_bytes() == before
    assert (backup / "mission_catalog_variants/qualified/catalog.json").is_file()
    assert not list(share.glob(".catalog-staging-*"))
    assert remote["restart"] == 1
    # The Pi's source copy follows its install.
    synced = pi / deploy_missions.MISSION_SOURCE / HOVER_TREE
    assert synced.read_bytes() == tree.read_bytes()
    # The installed catalog verifies with the generator, every scope.
    generator = deploy_missions._generator(checkout / deploy_missions.MISSION_SOURCE)
    for scope, relative in deploy_missions.CATALOG_DIRECTORIES.items():
        generator.verify_catalog(share / relative, expected_scope=scope)


def test_a_new_mission_from_the_registrations_file_is_installed(pi, checkout, remote):
    source = checkout / deploy_missions.MISSION_SOURCE
    specification = source / "mission_specification/mission_specification_opti_track_hover_copy.yaml"
    shutil.copy(source / "mission_specification/mission_specification_opti_track_hover.yaml", specification)
    text = specification.read_text()
    specification.write_text(text.replace('"ot_hover"', '"ot_hover_copy"').replace('"OT Hover"', '"OT Hover Copy"'))
    registrations = source / deploy_missions.REGISTRATIONS
    document = json.loads(registrations.read_text())
    document["missions"].append(
        {"id": "opti-track-hover-copy", "specification": specification.relative_to(source).as_posix(),
         "classification": "experimental", "status": "active", "profiles": ["opti_track"], "default_for": []}
    )
    registrations.write_text(json.dumps(document, indent=2))

    result = deploy_missions.deploy(_args(pi, checkout))

    assert result.code == "III_DEPLOY_MISSIONS_INSTALLED", result.findings
    assert {"id": "opti-track-hover-copy", "change": "added"} in result.payload["changes"]
    assert "opti-track-hover-copy" in {entry["id"] for entry in _installed_catalog(pi)["entries"]}


def test_a_cpp_change_needs_a_build_and_leaves_the_pi_unchanged(pi, checkout, remote):
    _edit_hover_tree(checkout)
    # A behaviour-node source: it shapes the node contract the trees are checked against.
    source = checkout / deploy_missions.MISSION_SOURCE / "src/tools/behavior_node_contract_exporter.cpp"
    source.write_text(source.read_text() + "\n// changed\n")
    before = _installed_catalog(pi)

    result = deploy_missions.deploy(_args(pi, checkout))

    assert result.code == deploy_missions.BUILD_NEEDED
    assert "iii deploy dev --build" in result.findings[0].message
    assert source.relative_to(checkout / deploy_missions.MISSION_SOURCE).as_posix() in result.findings[0].message
    assert _installed_catalog(pi) == before
    assert remote["restart"] == 0


def test_a_pi_without_the_node_contract_needs_one_build_deploy(pi, checkout, remote):
    (pi / "install/share/iii_drone_mission" / deploy_missions.NODE_CONTRACT).unlink()

    result = deploy_missions.deploy(_args(pi, checkout, _iii_dry_run=True))

    assert result.code == deploy_missions.BUILD_NEEDED
    assert "iii deploy dev --build once" in result.findings[0].message


def test_a_tree_with_an_unknown_node_is_rejected(pi, checkout, remote):
    tree = checkout / deploy_missions.MISSION_SOURCE / HOVER_TREE
    tree.write_text(tree.read_text().replace('ID="Hover"', 'ID="NoSuchNode"', 1))
    before = _installed_catalog(pi)

    result = deploy_missions.deploy(_args(pi, checkout))

    assert result.code == "III_DEPLOY_MISSIONS_REJECTED"
    assert "mission catalog is invalid" in result.findings[0].message
    assert _installed_catalog(pi) == before
    assert remote["restart"] == 0


def test_the_vehicle_gate_stops_the_install(pi, checkout, remote, monkeypatch):
    _edit_hover_tree(checkout)
    monkeypatch.setattr(
        vehicle_gate, "evaluate",
        lambda *_args, **_kwargs: vehicle_gate.RestartGate(False, "opti_track", "vehicle is armed"),
    )
    before = _installed_catalog(pi)

    result = deploy_missions.deploy(_args(pi, checkout))

    assert result.code == vehicle_gate.GATE_REJECTED
    assert _installed_catalog(pi) == before
    assert remote["restart"] == 0 and remote["rsync"] == []
