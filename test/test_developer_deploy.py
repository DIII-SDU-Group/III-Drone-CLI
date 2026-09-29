from __future__ import annotations

import json
from io import StringIO
import os
from pathlib import Path
import shlex
import signal
import sys
from types import SimpleNamespace
from time import sleep as real_sleep

import pytest

from iii import developer_deploy
from iii.__main__ import build_parser, main
from iii.runner import inventory_parser


def _workspace(tmp_path: Path) -> Path:
    for relative in ("deployment", "scripts", "src/pkg", "setup", "tools"):
        (tmp_path / relative).mkdir(parents=True, exist_ok=True)
    (tmp_path / "src/pkg/node.py").write_text("print('hello')\n", encoding="utf-8")
    cli = tmp_path / "tools/III-Drone-CLI/bin/iii"
    cli.parent.mkdir(parents=True)
    cli.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    cli.chmod(0o755)
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

    assert selected[:4] == (
        workspace / "setup",
        workspace / "scripts",
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

    def fake_run(command, *, dry_run, **_kwargs):
        calls.append((list(command), dry_run))
        return {"command": list(command), "returncode": None, "stdout": "", "stderr": ""}

    monkeypatch.setattr(developer_deploy, "_run", fake_run)
    result = developer_deploy.deploy(_args(workspace, receipts, _iii_dry_run=True))

    assert result.code == "III_DEVELOPER_DEPLOY_PREVIEW"
    assert calls[0][0] == [
        "ssh", "-o", "ConnectTimeout=8", "-o", "ConnectionAttempts=1",
        "iii@10.42.0.14", "mkdir -p -- /home/iii/ws",
    ]
    assert calls[1][0][:3] == ["rsync", "-az", "--itemize-changes"]
    cli_install = [command for command, _ in calls if "sudo ln -sfn" in " ".join(command)]
    assert cli_install
    assert "iii --help >/dev/null" in cli_install[0][-1]
    assert all(dry_run for _, dry_run in calls)
    receipt = Path(result.payload["receipt"])
    assert json.loads(receipt.read_text(encoding="utf-8"))["dry_run"] is True


def test_developer_deploy_cross_builds_locally_then_syncs_install_and_restarts(monkeypatch, tmp_path):
    workspace = _workspace(tmp_path / "workspace")
    receipts = tmp_path / "receipts"
    cross_output = tmp_path / "cross-output"
    (cross_output / "install").mkdir(parents=True)
    (cross_output / "install" / "setup.bash").write_text("# test\n", encoding="utf-8")
    monkeypatch.setattr(developer_deploy, "_workspace", lambda: workspace)
    calls = []

    def fake_run(command, *, dry_run, **_kwargs):
        calls.append(list(command))
        return {"command": list(command), "returncode": 0, "stdout": "ok", "stderr": ""}

    monkeypatch.setattr(developer_deploy, "_run", fake_run)
    result = developer_deploy.deploy(
        _args(
            workspace,
            receipts,
            build=True,
            restart=True,
            mirror=True,
            _iii_environment={
                "III_DEVELOPER_DEPLOY_RECEIPTS": str(receipts),
                "III_CROSS_OUTPUT_DIR": str(cross_output),
            },
        )
    )

    assert result.code == "III_DEVELOPER_DEPLOY_COMPLETED"
    assert calls[0][0].endswith("scripts/build/cross_compile_arm64.sh")
    source_rsync = [call for call in calls if call[0:2] == ["rsync", "-az"] and "/install/" not in " ".join(call)]
    assert source_rsync
    assert source_rsync[0][0:4] == ["rsync", "-az", "--itemize-changes", "--delete"]
    install_rsync = [call for call in calls if "/install/" in " ".join(call)]
    assert install_rsync
    assert "--delete" in install_rsync[0]
    restart = [call for call in calls if call[0] == "ssh" and "systemctl restart" in call[-1]]
    assert restart
    assert "sudo systemctl daemon-reload" in restart[0][-1]
    cli_install = next(call for call in calls if "sudo ln -sfn" in " ".join(call))
    assert calls.index(cli_install) < calls.index(restart[0])
    assert "iii --help >/dev/null" in cli_install[-1]


def _canonical_lookup_result(command, kwargs):
    log_path = Path(kwargs["log_path"])
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text('["10.42.0.15", "192.168.1.251"]\n', encoding="utf-8")
    return {
        "command": list(command), "returncode": 0, "stdout": "", "stderr": "",
        "log": str(log_path), "interrupted": False,
    }


def test_canonical_deploy_pins_one_reachable_peer_across_all_transports(monkeypatch, tmp_path):
    workspace = _workspace(tmp_path / "workspace")
    receipts = tmp_path / "receipts"
    cross_output = tmp_path / "cross-output"
    (cross_output / "install").mkdir(parents=True)
    (cross_output / "install/setup.bash").write_text("# test\n", encoding="utf-8")
    monkeypatch.setattr(developer_deploy, "_workspace", lambda: workspace)
    attempts = []

    class Probe:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def connect(address, timeout):
        attempts.append((address, timeout))
        if address[0] == "10.42.0.15":
            raise OSError("stale peer")
        return Probe()

    monkeypatch.setattr(developer_deploy, "socket", SimpleNamespace(create_connection=connect))
    calls = []

    def fake_run(command, *, dry_run, **kwargs):
        calls.append(list(command))
        if command[0] == sys.executable:
            return _canonical_lookup_result(command, kwargs)
        return {"command": list(command), "returncode": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(developer_deploy, "_run", fake_run)
    progress = StringIO()
    result = developer_deploy.deploy(
        _args(
            workspace,
            receipts,
            host="iii.local",
            build=True,
            restart=True,
            _iii_progress_stream=progress,
            _iii_environment={
                "III_DEVELOPER_DEPLOY_RECEIPTS": str(receipts),
                "III_CROSS_OUTPUT_DIR": str(cross_output),
            },
        )
    )

    assert result.code == "III_DEVELOPER_DEPLOY_COMPLETED"
    assert attempts == [
        (("10.42.0.15", 22), 0.75),
        (("192.168.1.251", 22), 0.75),
    ]
    transports = [call for call in calls if call[0] in {"ssh", "rsync"}]
    assert transports
    for command in transports:
        rendered = " ".join(command)
        assert "iii@iii.local" in rendered
        assert "HostName=192.168.1.251" in rendered
        assert "HostKeyAlias=iii.local" in rendered
        assert "ConnectTimeout=8" in rendered
        assert "ConnectionAttempts=1" in rendered
    rsyncs = [call for call in calls if call[0] == "rsync"]
    assert len(rsyncs) == 2
    assert all(
        "--rsh=ssh -o ConnectTimeout=8 -o ConnectionAttempts=1 "
        "-o HostName=192.168.1.251 -o HostKeyAlias=iii.local" in call
        for call in rsyncs
    )
    assert "[done] Pi peer selection peer=192.168.1.251" in progress.getvalue()


def test_canonical_dns_timeout_is_receipted_before_mutation(monkeypatch, tmp_path):
    workspace = _workspace(tmp_path / "workspace")
    receipts = tmp_path / "receipts"
    monkeypatch.setattr(developer_deploy, "_workspace", lambda: workspace)
    calls = []

    def timeout_lookup(command, *, timeout_seconds=None, **kwargs):
        calls.append(list(command))
        assert timeout_seconds == 8.0
        return {
            "command": list(command), "returncode": 124, "stdout": "",
            "stderr": "", "log": "lookup.log", "interrupted": False,
            "timed_out": True, "stage": "Pi peer DNS lookup",
        }

    monkeypatch.setattr(developer_deploy, "_run", timeout_lookup)
    result = developer_deploy.deploy(_args(workspace, receipts, host="iii.local"))

    assert result.code == "III_DEVELOPER_DEPLOY_FAILED"
    assert len(calls) == 1
    receipt = json.loads(Path(result.payload["receipt"]).read_text(encoding="utf-8"))
    assert receipt["commands"][0]["stage"] == "Pi peer DNS lookup"
    assert receipt["commands"][0]["timed_out"] is True
    assert not any(item["command"][0] in {"ssh", "rsync"} for item in receipt["commands"])


def test_canonical_dns_cancellation_is_receipted_before_mutation(monkeypatch, tmp_path):
    workspace = _workspace(tmp_path / "workspace")
    receipts = tmp_path / "receipts"
    pid_file = tmp_path / "lookup.pid"
    monkeypatch.setattr(developer_deploy, "_workspace", lambda: workspace)
    calls = []
    interrupt_once = True
    real_run = developer_deploy._run

    def interrupt_sleep(_seconds):
        nonlocal interrupt_once
        if interrupt_once:
            interrupt_once = False
            deadline = developer_deploy.monotonic() + 5
            while (
                (not pid_file.exists() or not pid_file.read_text(encoding="utf-8"))
                and developer_deploy.monotonic() < deadline
            ):
                real_sleep(0.01)
            assert pid_file.exists(), "lookup child did not start"
            raise KeyboardInterrupt()
        real_sleep(_seconds)

    def cancelled_lookup(command, **kwargs):
        calls.append(list(command))
        if command[0] == sys.executable:
            return real_run(
                [
                    sys.executable,
                    "-c",
                    "import os,sys,time; open(sys.argv[1],'w').write(str(os.getpid())); time.sleep(30)",
                    str(pid_file),
                ],
                **kwargs,
            )
        return real_run(command, **kwargs)

    monkeypatch.setattr(developer_deploy, "_sleep", interrupt_sleep)
    monkeypatch.setattr(developer_deploy, "_run", cancelled_lookup)
    result = developer_deploy.deploy(_args(workspace, receipts, host="iii.local"))

    assert result.code == "III_DEVELOPER_DEPLOY_INTERRUPTED"
    child_pid = int(pid_file.read_text(encoding="utf-8"))
    receipt = json.loads(Path(result.payload["receipt"]).read_text(encoding="utf-8"))
    assert receipt["interrupted_stage"] == "Pi peer DNS lookup"
    assert receipt["commands"][-1]["interrupted"] is True
    assert not any(entry["command"][0] in {"ssh", "rsync"} for entry in receipt["commands"])
    stat = Path(f"/proc/{child_pid}/stat")
    assert not stat.exists() or stat.read_text(encoding="utf-8").split()[2] == "Z"


def test_canonical_probe_cancellation_receipts_peer_selection_stage(monkeypatch, tmp_path):
    workspace = _workspace(tmp_path / "workspace")
    receipts = tmp_path / "receipts"
    monkeypatch.setattr(developer_deploy, "_workspace", lambda: workspace)
    calls = []

    def fake_run(command, *, dry_run, **kwargs):
        calls.append(list(command))
        return _canonical_lookup_result(command, kwargs)

    def interrupt_probe(*_args, **_kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(developer_deploy, "_run", fake_run)
    monkeypatch.setattr(
        developer_deploy, "socket", SimpleNamespace(create_connection=interrupt_probe)
    )
    result = developer_deploy.deploy(
        _args(workspace, receipts, host="iii.local", build=True)
    )

    assert result.code == "III_DEVELOPER_DEPLOY_INTERRUPTED"
    receipt = json.loads(Path(result.payload["receipt"]).read_text(encoding="utf-8"))
    assert receipt["interrupted_stage"] == "Pi peer selection"
    assert receipt["commands"][-1]["stage"] == "Pi peer selection"
    assert receipt["commands"][-1]["interrupted"] is True
    assert len(calls) == 1 and calls[0][0] == sys.executable
    assert not any(
        entry["command"]
        and (
            entry["command"][0] in {"ssh", "rsync"}
            or str(entry["command"][0]).endswith("cross_compile_arm64.sh")
        )
        for entry in receipt["commands"]
    )


def test_canonical_unreachable_candidates_fail_before_build_or_transport(monkeypatch, tmp_path):
    workspace = _workspace(tmp_path / "workspace")
    receipts = tmp_path / "receipts"
    monkeypatch.setattr(developer_deploy, "_workspace", lambda: workspace)
    calls = []

    def fake_run(command, *, dry_run, **kwargs):
        calls.append(list(command))
        return _canonical_lookup_result(command, kwargs)

    def reject_probe(*_args, **_kwargs):
        raise OSError("peer unavailable")

    monkeypatch.setattr(developer_deploy, "_run", fake_run)
    monkeypatch.setattr(
        developer_deploy, "socket", SimpleNamespace(create_connection=reject_probe)
    )
    result = developer_deploy.deploy(
        _args(workspace, receipts, host="iii.local", build=True)
    )

    assert result.code == "III_DEVELOPER_DEPLOY_FAILED"
    receipt = json.loads(Path(result.payload["receipt"]).read_text(encoding="utf-8"))
    assert [entry["stage"] for entry in receipt["commands"]] == [
        "Pi peer DNS lookup", "Pi peer selection"
    ]
    assert "No reachable IPv4 address" in receipt["commands"][-1]["stderr"]
    assert len(calls) == 1 and calls[0][0] == sys.executable
    assert not any(
        entry["command"][0] in {"ssh", "rsync"}
        or str(entry["command"][0]).endswith("cross_compile_arm64.sh")
        for entry in receipt["commands"]
    )


def test_command_timeout_terminates_owned_child_group(monkeypatch, tmp_path):
    pid_file = tmp_path / "timeout.pid"
    result = developer_deploy._run(
        [
            sys.executable,
            "-c",
            "import os,sys,time; open(sys.argv[1],'w').write(str(os.getpid())); time.sleep(30)",
            str(pid_file),
        ],
        dry_run=False,
        stage="bounded lookup",
        log_root=tmp_path / "logs",
        timeout_seconds=0.1,
    )

    assert result["returncode"] == 124
    assert result["timed_out"] is True
    assert result["interrupted"] is False
    child_pid = int(pid_file.read_text(encoding="utf-8"))
    stat = Path(f"/proc/{child_pid}/stat")
    assert not stat.exists() or stat.read_text(encoding="utf-8").split()[2] == "Z"


@pytest.mark.parametrize(
    ("host", "dry_run"),
    [("iii.local", True), ("10.42.0.14", False), ("field-pi", False)],
)
def test_peer_selection_preserves_preview_explicit_ip_and_ssh_alias(
    monkeypatch, tmp_path, host, dry_run
):
    workspace = _workspace(tmp_path / "workspace")
    receipts = tmp_path / "receipts"
    monkeypatch.setattr(developer_deploy, "_workspace", lambda: workspace)
    calls = []

    def fake_run(command, *, dry_run, **kwargs):
        calls.append(list(command))
        return {"command": list(command), "returncode": None if dry_run else 0,
                "stdout": "", "stderr": ""}

    monkeypatch.setattr(developer_deploy, "_run", fake_run)
    result = developer_deploy.deploy(
        _args(workspace, receipts, host=host, _iii_dry_run=dry_run)
    )

    assert result.code == (
        "III_DEVELOPER_DEPLOY_PREVIEW" if dry_run else "III_DEVELOPER_DEPLOY_COMPLETED"
    )
    assert not any(command[0] == sys.executable for command in calls)
    ssh = next(command for command in calls if command[0] == "ssh")
    assert "HostName=" not in " ".join(ssh)
    assert "HostKeyAlias=" not in " ".join(ssh)
    assert "ConnectTimeout=8" in ssh


def test_pi_cli_install_quotes_custom_workspace_and_installs_both_entry_points():
    remote_workspace = "/home/iii/ws with space/it's-a-workspace"

    command = developer_deploy._pi_cli_install_command(remote_workspace)

    source = f"{remote_workspace}/tools/III-Drone-CLI/bin/iii"
    assert f"{shlex.quote(source)}" in command
    assert f"sudo ln -sfnT -- {shlex.quote(source)} /usr/local/bin/iii" in command
    assert f'ln -sfnT -- {shlex.quote(source)} "$HOME/.local/bin/iii"' in command
    assert 'mkdir -p -- "$HOME/.local/bin"' in command
    assert 'test "$(command -v iii)" = /usr/local/bin/iii' in command
    assert "iii --help >/dev/null" in command
    assert "source /" not in command


def test_cli_symlink_invocation_resolves_workspace_for_pythonpath(tmp_path):
    workspace = tmp_path / "workspace with spaces"
    cli_bin = workspace / "tools/III-Drone-CLI/bin/iii"
    cli_bin.parent.mkdir(parents=True)
    cli_bin.write_text(
        (Path(__file__).parents[1] / "bin/iii").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    cli_bin.chmod(0o755)
    (workspace / "deployment/src").mkdir(parents=True)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    python = fake_bin / "python3"
    python.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$PYTHONPATH\"\nprintf '%s\\n' \"$*\"\n",
        encoding="utf-8",
    )
    python.chmod(0o755)
    link_dir = tmp_path / "usr local bin"
    link_dir.mkdir()
    link = link_dir / "iii"
    link.symlink_to(cli_bin)

    completed = developer_deploy.subprocess.run(
        [str(link), "--help"],
        check=True,
        text=True,
        capture_output=True,
        env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"},
    )

    output = completed.stdout.splitlines()
    assert output[0].split(":")[0] == str(workspace / "tools/III-Drone-CLI")
    assert output[1] == "-m iii --help"


def test_cli_install_failure_is_receipted_and_stops_before_restart(monkeypatch, tmp_path):
    workspace = _workspace(tmp_path / "workspace")
    receipts = tmp_path / "receipts"
    monkeypatch.setattr(developer_deploy, "_workspace", lambda: workspace)
    calls = []

    def fail_cli_install(command, *, dry_run, **_kwargs):
        calls.append(list(command))
        failed = "sudo ln -sfn" in " ".join(command)
        return {
            "command": list(command),
            "returncode": 31 if failed else 0,
            "stdout": "",
            "stderr": "III CLI source is missing or not executable" if failed else "",
            "log": "fake.log",
            "interrupted": False,
        }

    monkeypatch.setattr(developer_deploy, "_run", fail_cli_install)
    result = developer_deploy.deploy(_args(workspace, receipts, restart=True))

    assert result.code == "III_DEVELOPER_DEPLOY_FAILED"
    assert not any("systemctl restart" in " ".join(command) for command in calls)
    receipt = json.loads(Path(result.payload["receipt"]).read_text(encoding="utf-8"))
    assert receipt["commands"][-1]["stage"] == "Pi CLI install"
    assert receipt["commands"][-1]["returncode"] == 31


def test_universal_dry_run_reaches_direct_developer_command(monkeypatch, tmp_path):
    workspace = _workspace(tmp_path / "workspace")
    receipts = tmp_path / "receipts"
    monkeypatch.setattr(developer_deploy, "_workspace", lambda: workspace)
    output = []

    def fake_run(command, *, dry_run, **_kwargs):
        output.append(list(command))
        return {"command": list(command), "returncode": None, "stdout": "", "stderr": ""}

    monkeypatch.setattr(developer_deploy, "_run", fake_run)
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


def test_subprocess_heartbeat_and_log_tail_are_bounded(tmp_path):
    progress = StringIO()
    result = developer_deploy._run(
        [sys.executable, "-c", "import time; print('started'); time.sleep(.14); print('finished')"],
        dry_run=False,
        stage="test stage",
        log_root=tmp_path / "logs",
        progress=progress,
        heartbeat_seconds=0.03,
    )

    assert result["returncode"] == 0
    assert "still running" in progress.getvalue()
    assert "finished" in result["stderr"]
    assert Path(result["log"]).is_file()


def test_failed_subprocess_preserves_exit_status_and_diagnostic_tail(tmp_path):
    result = developer_deploy._run(
        [sys.executable, "-c", "print('failure detail'); raise SystemExit(23)"],
        dry_run=False,
        stage="test failure",
        log_root=tmp_path / "logs",
    )

    assert result["returncode"] == 23
    assert "failure detail" in result["stderr"]
    assert Path(result["log"]).is_file()


def test_shared_source_log_does_not_assign_prior_output_to_silent_failure(tmp_path):
    log_root = tmp_path / "logs"
    log_path = log_root / "source-sync.log"
    first = developer_deploy._run(
        [sys.executable, "-c", "print('previous source succeeded')"],
        dry_run=False,
        stage="source sync",
        log_root=log_root,
        log_path=log_path,
        append_log=True,
    )
    failed = developer_deploy._run(
        [sys.executable, "-c", "raise SystemExit(17)"],
        dry_run=False,
        stage="source sync",
        log_root=log_root,
        log_path=log_path,
        append_log=True,
    )

    assert first["returncode"] == 0
    assert failed["returncode"] == 17
    assert failed["stderr"] == ""
    assert "previous source succeeded" in log_path.read_text(encoding="utf-8")


@pytest.mark.parametrize("interrupt_at", ["[done] Pi workspace setup", "[progress] source sync (5/6 paths)"])
def test_interrupt_after_completed_command_does_not_duplicate_it(monkeypatch, tmp_path, interrupt_at):
    workspace = _workspace(tmp_path / "workspace")
    receipts = tmp_path / "receipts"
    paths = []
    for index in range(6):
        relative = f"src/component-{index}"
        (workspace / relative).mkdir(parents=True)
        paths.append(relative)
    monkeypatch.setattr(developer_deploy, "_workspace", lambda: workspace)
    monkeypatch.setattr(
        developer_deploy,
        "_run",
        lambda command, **_kwargs: {
            "command": list(command), "returncode": 0, "stdout": "",
            "stderr": "", "log": "fake.log", "interrupted": False,
        },
    )

    class InterruptingStream:
        def write(self, value):
            if value.startswith(interrupt_at):
                raise KeyboardInterrupt()

        def flush(self):
            pass

    result = developer_deploy.deploy(
        _args(workspace, receipts, path=paths, _iii_progress_stream=InterruptingStream())
    )
    receipt = json.loads(Path(result.payload["receipt"]).read_text(encoding="utf-8"))
    commands = receipt["commands"]
    completed = [entry for entry in commands if entry["returncode"] == 0]

    assert result.code == "III_DEVELOPER_DEPLOY_INTERRUPTED"
    assert len(completed) == (1 if interrupt_at.startswith("[done]") else 6)
    assert commands[-1]["returncode"] == 130
    assert commands[-1]["command"] == []
    assert len([entry for entry in commands if entry["command"] == completed[-1]["command"]]) == 1


def test_deploy_streams_early_stage_and_records_failure(monkeypatch, tmp_path):
    workspace = _workspace(tmp_path / "workspace")
    receipts = tmp_path / "receipts"
    monkeypatch.setattr(developer_deploy, "_workspace", lambda: workspace)
    progress = StringIO()

    def failing_run(command, *, dry_run, **_kwargs):
        return {
            "command": list(command),
            "returncode": 17,
            "stdout": "",
            "stderr": "network unreachable",
            "log": str(tmp_path / "failure.log"),
            "interrupted": False,
        }

    monkeypatch.setattr(developer_deploy, "_run", failing_run)
    args = _args(workspace, receipts, _iii_progress_stream=progress)
    result = developer_deploy.deploy(args)

    rendered = progress.getvalue()
    assert rendered.startswith(f"deploy dev: target=iii@10.42.0.14 workspace={workspace}")
    assert "plan=1 source paths + Pi CLI installation" in rendered
    assert "[start] Pi workspace setup" in rendered
    assert "log=" in rendered.split("[start] Pi workspace setup", 1)[1].splitlines()[0]
    assert "[fail] Pi workspace setup" in rendered
    assert "network unreachable" in rendered
    assert result.outcome.value == "failed"
    receipt = json.loads(Path(result.payload["receipt"]).read_text(encoding="utf-8"))
    assert receipt["commands"][0]["stderr"] == "network unreachable"


def test_ctrl_c_between_stages_writes_interrupted_receipt(monkeypatch, tmp_path):
    workspace = _workspace(tmp_path / "workspace")
    receipts = tmp_path / "receipts"
    monkeypatch.setattr(developer_deploy, "_workspace", lambda: workspace)

    def successful_setup_then_interrupt(command, **kwargs):
        return {
            "command": list(command),
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "log": str(kwargs["log_path"]),
            "interrupted": False,
        }

    monkeypatch.setattr(developer_deploy, "_run", successful_setup_then_interrupt)

    class InterruptOnSourceStage:
        def write(self, value):
            if value.startswith("[start] source sync"):
                raise KeyboardInterrupt()

        def flush(self):
            return None

    result = developer_deploy.deploy(
        _args(
            workspace,
            receipts,
            _iii_progress_stream=InterruptOnSourceStage(),
        )
    )

    assert result.code == "III_DEVELOPER_DEPLOY_INTERRUPTED"
    receipt = json.loads(Path(result.payload["receipt"]).read_text(encoding="utf-8"))
    assert receipt["interrupted_stage"] == "source sync"
    assert [entry["returncode"] for entry in receipt["commands"]] == [0, 130]
    assert receipt["commands"][-1]["stage"] == "source sync"


def test_ctrl_c_before_receipt_write_retries_atomic_interrupted_receipt(monkeypatch, tmp_path):
    workspace = _workspace(tmp_path / "workspace")
    receipts = tmp_path / "receipts"
    monkeypatch.setattr(developer_deploy, "_workspace", lambda: workspace)
    monkeypatch.setattr(
        developer_deploy,
        "_run",
        lambda command, **_kwargs: {
            "command": list(command),
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "log": "fake.log",
            "interrupted": False,
        },
    )
    real_write = developer_deploy._write_receipt
    interrupt_once = True

    def interrupt_receipt_once(root, receipt):
        nonlocal interrupt_once
        if interrupt_once:
            interrupt_once = False
            raise KeyboardInterrupt()
        return real_write(root, receipt)

    monkeypatch.setattr(developer_deploy, "_write_receipt", interrupt_receipt_once)
    result = developer_deploy.deploy(_args(workspace, receipts))

    assert result.code == "III_DEVELOPER_DEPLOY_INTERRUPTED"
    receipt = json.loads(Path(result.payload["receipt"]).read_text(encoding="utf-8"))
    assert receipt["interrupted_stage"] == "final receipt"
    assert [entry["returncode"] for entry in receipt["commands"][:-1]] == [0, 0, 0]
    assert receipt["commands"][-1]["stage"] == "final receipt"


def test_ctrl_c_after_receipt_commit_replaces_success_with_single_interrupted_receipt(monkeypatch, tmp_path):
    workspace = _workspace(tmp_path / "workspace")
    receipts = tmp_path / "receipts"
    monkeypatch.setattr(developer_deploy, "_workspace", lambda: workspace)
    monkeypatch.setattr(
        developer_deploy,
        "_run",
        lambda command, **_kwargs: {
            "command": list(command), "returncode": 0, "stdout": "",
            "stderr": "", "log": "fake.log", "interrupted": False,
        },
    )
    real_write = developer_deploy._write_receipt
    interrupt_once = True

    def interrupt_after_commit(root, receipt):
        nonlocal interrupt_once
        path = real_write(root, receipt)
        if interrupt_once:
            interrupt_once = False
            raise KeyboardInterrupt()
        return path

    monkeypatch.setattr(developer_deploy, "_write_receipt", interrupt_after_commit)
    result = developer_deploy.deploy(_args(workspace, receipts))

    assert result.code == "III_DEVELOPER_DEPLOY_INTERRUPTED"
    assert len(list(receipts.glob("developer-deploy-*.json"))) == 1
    receipt = json.loads(Path(result.payload["receipt"]).read_text(encoding="utf-8"))
    assert receipt["interrupted_stage"] == "final receipt"
    assert [entry["returncode"] for entry in receipt["commands"]] == [0, 0, 0, 130]


@pytest.mark.parametrize("interrupt_on_append", [1, 2])
def test_ctrl_c_after_command_recording_does_not_repeat_command(monkeypatch, tmp_path, interrupt_on_append):
    workspace = _workspace(tmp_path / "workspace")
    receipts = tmp_path / "receipts"
    monkeypatch.setattr(developer_deploy, "_workspace", lambda: workspace)
    monkeypatch.setattr(
        developer_deploy,
        "_run",
        lambda command, **_kwargs: {
            "command": list(command), "returncode": 0, "stdout": "",
            "stderr": "", "log": "fake.log", "interrupted": False,
        },
    )

    class InterruptAfterAppend(list):
        def append(self, entry):
            super().append(entry)
            if len(self) == interrupt_on_append:
                raise KeyboardInterrupt()

    real_deploy = developer_deploy._deploy

    def deploy_with_interrupting_results(args, state):
        state["results"] = InterruptAfterAppend()
        return real_deploy(args, state)

    monkeypatch.setattr(developer_deploy, "_deploy", deploy_with_interrupting_results)
    result = developer_deploy.deploy(_args(workspace, receipts))

    assert result.code == "III_DEVELOPER_DEPLOY_INTERRUPTED"
    receipt = json.loads(Path(result.payload["receipt"]).read_text(encoding="utf-8"))
    assert [entry["returncode"] for entry in receipt["commands"]] == (
        [0, 130] if interrupt_on_append == 1 else [0, 0, 130]
    )
    assert receipt["commands"][-1]["command"] == []


def test_keyboard_interrupt_terminates_child_and_returns_interrupted_result(monkeypatch, tmp_path):
    workspace = _workspace(tmp_path / "workspace")
    receipts = tmp_path / "receipts"
    monkeypatch.setattr(developer_deploy, "_workspace", lambda: workspace)
    interrupt_once = True

    def interrupt_sleep(seconds):
        nonlocal interrupt_once
        if interrupt_once:
            interrupt_once = False
            raise KeyboardInterrupt()
        real_sleep(seconds)

    monkeypatch.setattr(developer_deploy, "_sleep", interrupt_sleep)
    real_run = developer_deploy._run

    def interrupting_run(command, **kwargs):
        return real_run(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            **kwargs,
        )

    monkeypatch.setattr(developer_deploy, "_run", interrupting_run)
    progress = StringIO()
    result = developer_deploy.deploy(
        _args(workspace, receipts, _iii_progress_stream=progress)
    )

    assert result.outcome.value == "interrupted"
    assert result.code == "III_DEVELOPER_DEPLOY_INTERRUPTED"
    assert "[interrupted] Pi workspace setup" in progress.getvalue()
    receipt = json.loads(Path(result.payload["receipt"]).read_text(encoding="utf-8"))
    assert receipt["commands"][0]["interrupted"] is True


def test_sigint_during_popen_registration_is_deferred_and_receipted(monkeypatch, tmp_path):
    workspace = _workspace(tmp_path / "workspace")
    receipts = tmp_path / "receipts"
    pid_file = tmp_path / "launch-grandchild.pid"
    mask_file = tmp_path / "launch-child-sigint-mask.txt"
    monkeypatch.setattr(developer_deploy, "_workspace", lambda: workspace)
    real_popen = developer_deploy.subprocess.Popen
    children = []
    script = (
        "import signal,subprocess,sys,time; "
        "blocked=signal.SIGINT in signal.pthread_sigmask(signal.SIG_BLOCK,set()); "
        "open(sys.argv[2],'w').write('blocked' if blocked else 'unblocked'); "
        "child=subprocess.Popen([sys.executable,'-c',"
        "\"import signal,sys,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        "open(sys.argv[1],'w').write(str(__import__('os').getpid())); time.sleep(30)\","
        "sys.argv[1]]); time.sleep(30)"
    )

    def launch_signal_before_return(_command, **kwargs):
        child = real_popen(
            [sys.executable, "-c", script, str(pid_file), str(mask_file)],
            **kwargs,
        )
        children.append(child)
        deadline = developer_deploy.monotonic() + 5
        while not pid_file.exists() and developer_deploy.monotonic() < deadline:
            real_sleep(0.01)
        assert pid_file.exists(), "fake grandchild did not start"
        os.kill(os.getpid(), signal.SIGINT)
        return child

    monkeypatch.setattr(
        developer_deploy.subprocess, "Popen", launch_signal_before_return
    )
    result = developer_deploy.deploy(
        _args(workspace, receipts, path=["setup"])
    )

    grandchild_pid = int(pid_file.read_text(encoding="utf-8"))
    assert result.code == "III_DEVELOPER_DEPLOY_INTERRUPTED"
    receipt = json.loads(Path(result.payload["receipt"]).read_text(encoding="utf-8"))
    assert receipt["interrupted_stage"] == "Pi workspace setup"
    assert receipt["commands"][0]["interrupted"] is True
    assert children[0].poll() is not None
    assert mask_file.read_text(encoding="utf-8") == "unblocked"
    stat = Path(f"/proc/{grandchild_pid}/stat")
    assert not stat.exists() or stat.read_text(encoding="utf-8").split()[2] == "Z"


def test_deferred_sigint_restores_handler_when_popen_fails(monkeypatch, tmp_path):
    previous_handler = signal.getsignal(signal.SIGINT)

    def fail_after_deferred_sigint(_command, **_kwargs):
        os.kill(os.getpid(), signal.SIGINT)
        raise FileNotFoundError("fake missing executable")

    monkeypatch.setattr(
        developer_deploy.subprocess, "Popen", fail_after_deferred_sigint
    )
    result = developer_deploy._run(
        ["missing-command"],
        dry_run=False,
        stage="launch failure",
        log_root=tmp_path / "logs",
    )

    assert result["interrupted"] is True
    assert result["returncode"] == 130
    assert signal.getsignal(signal.SIGINT) == previous_handler


def test_keyboard_interrupt_kills_owned_process_group_descendants(monkeypatch, tmp_path):
    pid_file = tmp_path / "grandchild.pid"
    workspace = _workspace(tmp_path / "workspace")
    receipts = tmp_path / "receipts"
    monkeypatch.setattr(developer_deploy, "_workspace", lambda: workspace)
    parent = (
        "import subprocess,sys,time; "
        "child=subprocess.Popen([sys.executable,'-c',"
        "\"import signal,sys,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        "open(sys.argv[1],'w').write(str(__import__('os').getpid())); time.sleep(30)\","
        "sys.argv[1]]); "
        "time.sleep(30)"
    )
    first_wait = True

    def interrupt_after_grandchild_starts(seconds):
        nonlocal first_wait
        if first_wait:
            deadline = developer_deploy.monotonic() + 5
            while not pid_file.exists() and developer_deploy.monotonic() < deadline:
                real_sleep(0.01)
            assert pid_file.exists(), "fake grandchild did not start"
            first_wait = False
            raise KeyboardInterrupt()
        real_sleep(seconds)

    monkeypatch.setattr(developer_deploy, "_sleep", interrupt_after_grandchild_starts)
    result = developer_deploy._run(
        [sys.executable, "-c", parent, str(pid_file)],
        dry_run=False,
        stage="process group test",
        log_root=tmp_path / "logs",
    )

    grandchild_pid = int(pid_file.read_text(encoding="utf-8"))
    deadline = developer_deploy.monotonic() + 3
    while developer_deploy.monotonic() < deadline:
        stat = Path(f"/proc/{grandchild_pid}/stat")
        if not stat.exists() or stat.read_text(encoding="utf-8").split()[2] == "Z":
            break
        real_sleep(0.02)

    assert result["interrupted"] is True
    assert result["returncode"] == 130
    stat = Path(f"/proc/{grandchild_pid}/stat")
    assert not stat.exists() or stat.read_text(encoding="utf-8").split()[2] == "Z"


@pytest.mark.parametrize("stream_error", [BrokenPipeError, ValueError])
def test_broken_progress_stream_stops_owned_process_group_and_records_failure(monkeypatch, tmp_path, stream_error):
    pid_file = tmp_path / "broken-stream-grandchild.pid"
    workspace = _workspace(tmp_path / "workspace")
    receipts = tmp_path / "receipts"
    monkeypatch.setattr(developer_deploy, "_workspace", lambda: workspace)
    real_run = developer_deploy._run
    parent = (
        "import subprocess,sys,time; "
        "child=subprocess.Popen([sys.executable,'-c',"
        "\"import signal,sys,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        "open(sys.argv[1],'w').write(str(__import__('os').getpid())); time.sleep(30)\","
        "sys.argv[1]]); time.sleep(30)"
    )

    class BrokenStream:
        def __init__(self):
            self.values = []

        def write(self, value):
            self.values.append(value)
            if not value.startswith("  …"):
                return
            deadline = developer_deploy.monotonic() + 5
            while not pid_file.exists() and developer_deploy.monotonic() < deadline:
                real_sleep(0.01)
            assert pid_file.exists(), "fake grandchild did not start"
            raise stream_error("closed progress stream")

        def flush(self):
            return None

    def run_fake_process_group(command, **kwargs):
        return real_run(
            [sys.executable, "-c", parent, str(pid_file)],
            heartbeat_seconds=0.02,
            **kwargs,
        )

    monkeypatch.setattr(developer_deploy, "_run", run_fake_process_group)
    progress = BrokenStream()
    result = developer_deploy.deploy(
        _args(workspace, receipts, _iii_progress_stream=progress)
    )

    grandchild_pid = int(pid_file.read_text(encoding="utf-8"))
    stat = Path(f"/proc/{grandchild_pid}/stat")
    assert result.code == "III_DEVELOPER_DEPLOY_FAILED"
    receipt = json.loads(Path(result.payload["receipt"]).read_text(encoding="utf-8"))
    assert receipt["commands"][0]["returncode"] == 125
    assert receipt["commands"][0]["progress_error"].startswith("progress stream failed:")
    deadline = developer_deploy.monotonic() + 3
    while stat.exists() and stat.read_text(encoding="utf-8").split()[2] != "Z" and developer_deploy.monotonic() < deadline:
        real_sleep(0.02)
    assert not stat.exists() or stat.read_text(encoding="utf-8").split()[2] == "Z"


def test_source_sync_interruption_reports_stage_and_exact_log(monkeypatch, tmp_path):
    workspace = _workspace(tmp_path / "workspace")
    receipts = tmp_path / "receipts"
    monkeypatch.setattr(developer_deploy, "_workspace", lambda: workspace)
    progress = StringIO()

    def setup_then_interrupt_source(command, **kwargs):
        if command[0] == "rsync":
            return {
                "command": list(command),
                "returncode": 130,
                "stdout": "",
                "stderr": "command interrupted",
                "log": str(kwargs["log_path"]),
                "interrupted": True,
            }
        return {
            "command": list(command),
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "log": str(kwargs["log_path"]),
            "interrupted": False,
        }

    monkeypatch.setattr(developer_deploy, "_run", setup_then_interrupt_source)
    result = developer_deploy.deploy(
        _args(workspace, receipts, _iii_progress_stream=progress)
    )

    rendered = progress.getvalue()
    start = next(line for line in rendered.splitlines() if line.startswith("[start] source sync"))
    assert "log=" in start
    assert "[interrupted] source sync (0/" in rendered
    assert "[fail] source sync" not in rendered
    assert result.code == "III_DEVELOPER_DEPLOY_INTERRUPTED"
    receipt = json.loads(Path(result.payload["receipt"]).read_text(encoding="utf-8"))
    assert receipt["commands"][-1]["log"] == start.split("log=", 1)[1]


def test_source_sync_reports_progress_every_five_paths(monkeypatch, tmp_path):
    workspace = _workspace(tmp_path / "workspace")
    receipts = tmp_path / "receipts"
    paths = []
    for index in range(12):
        relative = f"src/component-{index}"
        (workspace / relative).mkdir(parents=True)
        paths.append(relative)
    monkeypatch.setattr(developer_deploy, "_workspace", lambda: workspace)
    monkeypatch.setattr(
        developer_deploy,
        "_run",
        lambda command, **_kwargs: {
            "command": list(command),
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "log": "fake.log",
            "interrupted": False,
        },
    )
    progress = StringIO()

    result = developer_deploy.deploy(
        _args(workspace, receipts, path=paths, _iii_progress_stream=progress)
    )

    assert result.code == "III_DEVELOPER_DEPLOY_COMPLETED"
    lines = progress.getvalue().splitlines()
    assert "[progress] source sync (5/12 paths)" in lines
    assert "[progress] source sync (10/12 paths)" in lines
    assert "[done] source sync (12/12 paths)" in lines
    assert not any("component-" in line for line in lines)


def test_deploy_status_preserves_captured_stdout_and_stderr(monkeypatch):
    monkeypatch.setattr(
        developer_deploy.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1,
            stdout="inactive\n",
            stderr="systemd warning\n",
        ),
    )

    result = developer_deploy.status(SimpleNamespace(host="pi.local", user="iii"))

    assert result.payload["command"]["stdout"] == "inactive\n"
    assert result.payload["command"]["stderr"] == "systemd warning\n"


@pytest.mark.parametrize("failure_stage", ["Pi workspace setup", "source sync"])
def test_build_failure_receipt_retains_cross_install(monkeypatch, tmp_path, failure_stage):
    workspace = _workspace(tmp_path / "workspace")
    receipts = tmp_path / "receipts"
    cross_output = tmp_path / "cross-output"
    install = cross_output / "install"
    monkeypatch.setattr(developer_deploy, "_workspace", lambda: workspace)

    def fail_at_selected_stage(command, **_kwargs):
        if command[0].endswith("cross_compile_arm64.sh"):
            install.mkdir(parents=True, exist_ok=True)
            (install / "setup.bash").write_text("# test\n", encoding="utf-8")
            return {"command": list(command), "returncode": 0, "stdout": "", "stderr": ""}
        stage = "source sync" if command[0] == "rsync" else "Pi workspace setup"
        code = 17 if stage == failure_stage else 0
        return {
            "command": list(command),
            "returncode": code,
            "stdout": "",
            "stderr": "selected stage failed" if code else "",
            "log": "fake.log",
            "interrupted": False,
        }

    monkeypatch.setattr(developer_deploy, "_run", fail_at_selected_stage)
    result = developer_deploy.deploy(
        _args(
            workspace,
            receipts,
            path=["src"],
            build=True,
            _iii_environment={
                "III_DEVELOPER_DEPLOY_RECEIPTS": str(receipts),
                "III_CROSS_OUTPUT_DIR": str(cross_output),
            },
        )
    )

    assert result.code == "III_DEVELOPER_DEPLOY_FAILED"
    receipt = json.loads(Path(result.payload["receipt"]).read_text(encoding="utf-8"))
    assert receipt["cross_install"] == str(install)


def test_json_deploy_keeps_progress_off_stdout(monkeypatch, tmp_path):
    workspace = _workspace(tmp_path / "workspace")
    receipts = tmp_path / "receipts"
    monkeypatch.setattr(developer_deploy, "_workspace", lambda: workspace)

    def failing_run(command, *, dry_run, **_kwargs):
        return {
            "command": list(command),
            "returncode": 17,
            "stdout": "",
            "stderr": "network unreachable",
            "log": str(tmp_path / "failure.log"),
            "interrupted": False,
        }

    monkeypatch.setattr(developer_deploy, "_run", failing_run)
    stdout, stderr = StringIO(), StringIO()
    status = main(
        ["deploy", "dev", "--host", "10.42.0.14", "--json"],
        stdout=stdout,
        stderr=stderr,
        environment={"III_DEVELOPER_DEPLOY_RECEIPTS": str(receipts)},
    )

    assert status != 0
    assert json.loads(stdout.getvalue())["code"] == "III_DEVELOPER_DEPLOY_FAILED"
    assert stderr.getvalue() == ""


def test_broken_outside_child_progress_disables_updates_and_keeps_receipt(monkeypatch, tmp_path):
    workspace = _workspace(tmp_path / "workspace")
    receipts = tmp_path / "receipts"
    monkeypatch.setattr(developer_deploy, "_workspace", lambda: workspace)
    monkeypatch.setattr(
        developer_deploy,
        "_run",
        lambda command, **_kwargs: {
            "command": list(command),
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "log": "fake.log",
            "interrupted": False,
        },
    )

    class BrokenStream:
        def write(self, _value):
            raise OSError("closed progress destination")

        def flush(self):
            return None

    result = developer_deploy.deploy(
        _args(workspace, receipts, _iii_progress_stream=BrokenStream())
    )

    assert result.code == "III_DEVELOPER_DEPLOY_COMPLETED"
    receipt = json.loads(Path(result.payload["receipt"]).read_text(encoding="utf-8"))
    assert receipt["commands"][0]["returncode"] == 0
