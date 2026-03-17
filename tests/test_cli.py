import argparse
import importlib

import pytest


def test_container_manager_execute_cli_builds_expected_command(monkeypatch):
    from iii.container_manager import ContainerManager

    monkeypatch.setenv("DOCKER_COMPOSE_FILE", "/tmp/docker-compose.yml")

    launched = {}

    class FakeProcess:
        returncode = 0

        def wait(self):
            return 0

    def fake_popen(command, **kwargs):
        launched["command"] = command
        launched["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr("subprocess.Popen", fake_popen)

    manager = ContainerManager()
    assert manager.execute_cli("iii system status", ["--server-timeout-seconds", "3"]) is True
    assert "docker compose -f /tmp/docker-compose.yml --profile cli run --rm cli iii system status --server-timeout-seconds 3" == launched["command"]


def test_tmux_handler_reports_missing_project(monkeypatch, capsys):
    monkeypatch.delenv("TMUXINATOR_PROJECT", raising=False)

    from iii.tmux_handler import TmuxHandler

    handler = TmuxHandler()
    assert handler.start() is False
    assert "TMUXINATOR_PROJECT environment variable not set" in capsys.readouterr().out


def test_main_dispatches_to_selected_subcommand(monkeypatch):
    monkeypatch.setenv("CLI_CONFIGURATION", "host")

    build_module = importlib.import_module("iii.build")
    config_module = importlib.import_module("iii.config")
    deploy_module = importlib.import_module("iii.deploy")
    main_module = importlib.import_module("iii.__main__")
    system_module = importlib.import_module("iii.system")

    called = {}

    monkeypatch.setattr(system_module, "initialize", lambda parser: parser.set_defaults(func=lambda args: called.setdefault("system", args.subcommand), action="status"))
    monkeypatch.setattr(build_module, "initialize", lambda parser: parser.set_defaults(func=lambda args: called.setdefault("build", args.subcommand), action="system"))
    monkeypatch.setattr(deploy_module, "initialize", lambda parser: parser.set_defaults(func=lambda args: called.setdefault("deploy", args.subcommand), action="install"))
    monkeypatch.setattr(config_module, "run", lambda: called.setdefault("config", "config"))
    monkeypatch.setattr("sys.argv", ["iii", "config"])

    main_module.main()

    assert called["config"] == "config"


def test_select_nodes_completer_reads_supervisor_config(tmp_path, monkeypatch):
    config_file = tmp_path / "supervision.yaml"
    config_file.write_text(
        "managed_nodes:\n"
        "  mission: {}\n"
        "  perception: {}\n"
    )
    monkeypatch.setenv("SUPERVISOR_CONFIG_FILE", str(config_file))

    from iii.system import SelectNodesCompleter

    assert SelectNodesCompleter() == ["mission", "perception"]


def test_system_start_forwards_host_flags_and_selected_nodes(monkeypatch):
    system_module = importlib.import_module("iii.system")
    captured = {}

    class FakeContainerManager:
        def execute_cli(self, command, args):
            captured["command"] = command
            captured["args"] = args
            return True

    monkeypatch.setattr(system_module, "ContainerManager", FakeContainerManager)

    args = argparse.Namespace(
        server_timeout_seconds=7,
        skip_activate=True,
        include_dependencies=True,
        select_nodes=["mission", "perception"],
    )

    with pytest.raises(SystemExit) as exc:
        system_module.start(args)

    assert exc.value.code == 0
    assert captured["command"] == "/home/iii/.local/bin/iii system start"
    assert captured["args"] == [
        "--server-timeout-seconds",
        "7",
        "--skip-activate",
        "--include-dependencies",
        "--select-nodes",
        "mission",
        "perception",
    ]


def test_tmux_handler_attach_uses_running_session(monkeypatch):
    launched = {}

    class FakeProcess:
        def communicate(self):
            return (b"iii-dev: 1 windows\n", b"")

    monkeypatch.setenv("TMUXINATOR_PROJECT", "iii-dev")
    monkeypatch.setattr("subprocess.Popen", lambda *args, **kwargs: FakeProcess())
    monkeypatch.setattr("subprocess.run", lambda cmd: launched.setdefault("cmd", cmd))

    from iii.tmux_handler import TmuxHandler

    handler = TmuxHandler()

    assert handler.attach() is True
    assert launched["cmd"] == ["tmux", "attach", "-t", "iii-dev"]


def test_build_system_passes_colcon_args_to_container_manager(monkeypatch):
    monkeypatch.setenv("CLI_CONFIGURATION", "host")
    build_module = importlib.import_module("iii.build")
    captured = {}

    class FakeContainerManager:
        def colcon_build(self, colcon_build_args=None):
            captured["args"] = colcon_build_args

    monkeypatch.setattr(build_module, "ContainerManager", FakeContainerManager)

    build_module.build_system(argparse.Namespace(colcon_args=["--packages-select", "iii_drone_core"]))

    assert captured["args"] == ["--packages-select", "iii_drone_core"]


def test_build_container_routes_remote_all_images_to_remote_builder(monkeypatch):
    monkeypatch.setenv("CLI_CONFIGURATION", "remote")
    build_module = importlib.reload(importlib.import_module("iii.build"))
    captured = {}

    monkeypatch.setattr(
        build_module,
        "_build_container_remote",
        lambda **kwargs: captured.setdefault("kwargs", kwargs),
    )

    build_module.build_container(
        argparse.Namespace(push=True, base=False, cross_compilation=False, all=False)
    )

    assert captured["kwargs"] == {
        "push": True,
        "cross_compilation": True,
        "base": True,
    }
