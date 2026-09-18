from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from iii import developer_deploy
from iii.__main__ import build_parser, main
from iii.runner import inventory_parser


def _workspace(tmp_path: Path) -> Path:
    for relative in ("deployment", "src/pkg", "setup", "tools"):
        (tmp_path / relative).mkdir(parents=True, exist_ok=True)
    (tmp_path / "src/pkg/node.py").write_text("print('hello')\n", encoding="utf-8")
    return tmp_path


def _args(root: Path, receipts: Path, **overrides):
    values = {
        "host": "10.42.0.14",
        "user": "iii",
        "remote_workspace": "/home/iii/ws",
        "path": ["src"],
        "mirror": False,
        "build": False,
        "restart": False,
        "_iii_dry_run": False,
        "_iii_environment": {"III_DEVELOPER_DEPLOY_RECEIPTS": str(receipts)},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_parser_declares_direct_developer_deployment_without_operation_gate():
    inventory = inventory_parser(build_parser())
    spec = inventory[("deploy", "dev")]
    assert spec.direct_mutation is True
    assert spec.mutating is False


def test_default_deploy_skips_uncommitted_source_components(monkeypatch, tmp_path):
    workspace = _workspace(tmp_path / "workspace")
    (workspace / "src/clean_component").mkdir()
    (workspace / "src/experimental_component").mkdir()
    monkeypatch.setattr(
        developer_deploy,
        "_dirty_source_components",
        lambda _: frozenset({"experimental_component"}),
    )

    selected = developer_deploy._source_paths(workspace, [])

    assert selected[:3] == (
        workspace / "setup",
        workspace / "tools",
        workspace / "deployment",
    )
    assert workspace / "src/pkg" in selected
    assert workspace / "src/clean_component" in selected
    assert workspace / "src/experimental_component" not in selected


def test_explicit_path_can_deploy_an_intentional_work_in_progress(tmp_path):
    workspace = _workspace(tmp_path / "workspace")

    assert developer_deploy._source_paths(workspace, ["src"]) == (workspace / "src",)


def test_developer_deploy_dry_run_has_no_remote_side_effect(monkeypatch, tmp_path):
    workspace = _workspace(tmp_path / "workspace")
    receipts = tmp_path / "receipts"
    monkeypatch.setattr(developer_deploy, "_workspace", lambda: workspace)
    calls = []

    def fake_run(command, *, dry_run):
        calls.append((list(command), dry_run))
        return {"command": list(command), "returncode": None, "stdout": "", "stderr": ""}

    monkeypatch.setattr(developer_deploy, "_run", fake_run)
    result = developer_deploy.deploy(_args(workspace, receipts, _iii_dry_run=True))

    assert result.code == "III_DEVELOPER_DEPLOY_PREVIEW"
    assert calls[0][0] == ["ssh", "iii@10.42.0.14", "mkdir -p -- /home/iii/ws"]
    assert calls[1][0][:3] == ["rsync", "-az", "--itemize-changes"]
    receipt = Path(result.payload["receipt"])
    assert json.loads(receipt.read_text(encoding="utf-8"))["dry_run"] is True


def test_developer_deploy_runs_plain_ssh_rsync_build_and_restart(monkeypatch, tmp_path):
    workspace = _workspace(tmp_path / "workspace")
    receipts = tmp_path / "receipts"
    monkeypatch.setattr(developer_deploy, "_workspace", lambda: workspace)
    calls = []

    def fake_run(command, *, dry_run):
        calls.append(list(command))
        return {"command": list(command), "returncode": 0, "stdout": "ok", "stderr": ""}

    monkeypatch.setattr(developer_deploy, "_run", fake_run)
    result = developer_deploy.deploy(
        _args(workspace, receipts, build=True, restart=True, mirror=True)
    )

    assert result.code == "III_DEVELOPER_DEPLOY_COMPLETED"
    assert calls[1][0:4] == ["rsync", "-az", "--itemize-changes", "--delete"]
    assert "--exclude=.git" in calls[1]
    assert "--exclude=.pytest_cache" in calls[1]
    assert calls[2][0:2] == ["ssh", "iii@10.42.0.14"]
    assert "rosdep install --from-paths src --ignore-src --rosdistro jazzy -r -y" in calls[2][2]
    assert "python3 -m venv --system-site-packages .venv" in calls[2][2]
    assert ".venv/bin/python -m pip install --upgrade -e src/III-Drone-Runtime" in calls[2][2]
    assert "colcon build" in calls[2][2]
    assert "--packages-skip iii_drone_simulation" in calls[2][2]
    assert "-DBUILD_TESTING=OFF" in calls[2][2]
    assert calls[3][0:2] == ["ssh", "iii@10.42.0.14"]
    assert "sudo systemctl daemon-reload" in calls[3][2]
    assert "sudo systemctl restart iii-system-daemon.service iii-runtime-api.service" in calls[3][2]


def test_universal_dry_run_reaches_direct_developer_command(monkeypatch, tmp_path):
    workspace = _workspace(tmp_path / "workspace")
    receipts = tmp_path / "receipts"
    monkeypatch.setattr(developer_deploy, "_workspace", lambda: workspace)
    output = []

    def fake_run(command, *, dry_run):
        output.append(list(command))
        return {"command": list(command), "returncode": None, "stdout": "", "stderr": ""}

    monkeypatch.setattr(developer_deploy, "_run", fake_run)
    from io import StringIO

    rendered = StringIO()
    status = main(
        ["deploy", "dev", "--host", "10.42.0.14", "--dry-run", "--json"],
        stdout=rendered,
        stderr=StringIO(),
        environment={"III_DEVELOPER_DEPLOY_RECEIPTS": str(receipts)},
    )
    assert status == 0
    assert json.loads(rendered.getvalue())["code"] == "III_DEVELOPER_DEPLOY_PREVIEW"
    assert output
