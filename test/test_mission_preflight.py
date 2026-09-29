from pathlib import Path
from types import SimpleNamespace

import pytest

import iii.mission_preflight as preflight


class FakeCatalogModule:
    def __init__(self, drift):
        self.drift = list(drift)
        self.calls = 0

    def source_drift(self, **_kwargs):
        self.calls += 1
        return self.drift.pop(0) if self.drift else []

    def verify_catalog(self, _path, *, expected_scope):
        assert expected_scope == "local"
        return {"catalog_hash": "sha256:" + "a" * 64}


def _workspace(tmp_path: Path):
    source = tmp_path / "src/III-Drone-Mission/iii_drone_mission"
    source.mkdir(parents=True)
    (source / "mission_catalog.py").write_text("# test\n")
    for package in ("iii_drone_mission", "iii_drone_interfaces"):
        (tmp_path / "install" / package / "share" / package).mkdir(parents=True)
    return tmp_path


def test_matching_source_state_is_a_verified_noop(monkeypatch, tmp_path):
    workspace = _workspace(tmp_path)
    module = FakeCatalogModule([[]])
    monkeypatch.setattr(preflight, "_load_source_catalog_module", lambda _root: module)
    result = preflight.ensure_sim_mission_catalog(
        environment={"WORKSPACE_DIR": str(workspace)},
        runner=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not rebuild")),
    )
    assert result["rebuilt"] is False
    assert result["catalog_hash"].startswith("sha256:")


def test_drift_runs_targeted_rebuild_then_reverifies(monkeypatch, tmp_path):
    workspace = _workspace(tmp_path)
    module = FakeCatalogModule([["changed source file: behavior_trees/a.xml"], []])
    monkeypatch.setattr(preflight, "_load_source_catalog_module", lambda _root: module)
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    result = preflight.ensure_sim_mission_catalog(
        environment={"WORKSPACE_DIR": str(workspace)}, runner=runner
    )
    assert result["rebuilt"] is True
    assert calls[0][0] == [
        "colcon", "build", "--base-paths", "src", "--packages-select", "iii_drone_mission", "--symlink-install",
    ]
    assert calls[0][1]["cwd"] == workspace


def test_failed_or_ineffective_rebuild_refuses_boot(monkeypatch, tmp_path):
    workspace = _workspace(tmp_path)
    module = FakeCatalogModule([["stale"], ["still stale"]])
    monkeypatch.setattr(preflight, "_load_source_catalog_module", lambda _root: module)
    with pytest.raises(preflight.MissionPreflightError, match="remains stale"):
        preflight.ensure_sim_mission_catalog(
            environment={"WORKSPACE_DIR": str(workspace)},
            runner=lambda *_a, **_k: SimpleNamespace(returncode=0),
        )

    module = FakeCatalogModule([["stale"]])
    monkeypatch.setattr(preflight, "_load_source_catalog_module", lambda _root: module)
    with pytest.raises(preflight.MissionPreflightError, match="rebuild failed"):
        preflight.ensure_sim_mission_catalog(
            environment={"WORKSPACE_DIR": str(workspace)},
            runner=lambda *_a, **_k: SimpleNamespace(returncode=1),
        )
