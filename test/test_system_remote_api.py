import importlib
from types import SimpleNamespace

import pytest


def _load_remote_system(monkeypatch):
    monkeypatch.setenv("CLI_CONFIGURATION", "remote")
    module = importlib.import_module("iii.system")
    return importlib.reload(module)


class _FakeRuntimeClient:
    def __init__(self, command_response=None, log_response=None, raises=None):
        self.command_response = command_response or {
            "accepted": True,
            "result": {"daemon": {"success": True, "managed_nodes": []}},
        }
        self.log_response = log_response or {"lines": []}
        self.raises = raises
        self.commands = []
        self.log_tails = []

    def command(self, command_id, parameters=None):
        self.commands.append((command_id, parameters or {}))
        if self.raises is not None:
            raise self.raises
        return self.command_response

    def log_tail(self, source_id, *, lines=200):
        self.log_tails.append((source_id, lines))
        if self.raises is not None:
            raise self.raises
        return self.log_response


def test_remote_start_uses_runtime_api_not_host_forward(monkeypatch, capsys):
    system = _load_remote_system(monkeypatch)
    fake = _FakeRuntimeClient()
    monkeypatch.setattr(system, "_remote_runtime_client", lambda: fake)
    monkeypatch.setattr(system, "_host_forward", lambda *args: (_ for _ in ()).throw(AssertionError(args)))

    args = SimpleNamespace(skip_activate=True, select_nodes=["camera"], include_dependencies=True)
    with pytest.raises(SystemExit) as exc_info:
        system.start(args)

    assert exc_info.value.code == 0
    assert fake.commands == [
        (
            "runtime.start",
            {"activate": False, "select_nodes": ["camera"], "include_dependencies": True},
        )
    ]
    assert "System start complete." in capsys.readouterr().out


def test_remote_status_prints_runtime_api_daemon_status(monkeypatch, capsys):
    system = _load_remote_system(monkeypatch)
    fake = _FakeRuntimeClient(
        command_response={
            "accepted": True,
            "result": {
                "daemon": {
                    "booted": True,
                    "profile": "sim",
                    "managed_nodes": {"node-a": "active"},
                    "services": {
                        "svc": {
                            "alive": True,
                            "ready": True,
                            "starts": 1,
                            "exits": 0,
                            "reason": "",
                        }
                    },
                    "processes": {"node-a": {"alive": True, "start_count": 1, "exit_count": 0}},
                }
            },
        }
    )
    monkeypatch.setattr(system, "_remote_runtime_client", lambda: fake)

    with pytest.raises(SystemExit) as exc_info:
        system.status(SimpleNamespace(watch=False))

    assert exc_info.value.code == 0
    assert fake.commands == [("runtime.status", {})]
    output = capsys.readouterr().out
    assert "Booted: True" in output
    assert "node-a: active" in output


def test_remote_mutating_conflict_is_reported_without_shell_forward(monkeypatch, capsys):
    system = _load_remote_system(monkeypatch)
    fake = _FakeRuntimeClient(
        command_response={
            "accepted": False,
            "rejection": {
                "message": "mutating remote CLI command blocked while browser GUI session is active",
            },
        }
    )
    monkeypatch.setattr(system, "_remote_runtime_client", lambda: fake)
    monkeypatch.setattr(system, "_host_forward", lambda *args: (_ for _ in ()).throw(AssertionError(args)))

    with pytest.raises(SystemExit) as exc_info:
        system.stop(SimpleNamespace(skip_cleanup=False, select_nodes=[], include_dependencies=False))

    assert exc_info.value.code == 1
    assert fake.commands[0][0] == "runtime.stop"
    assert "browser GUI session" in capsys.readouterr().out


def test_remote_runtime_api_unavailable_has_clear_error(monkeypatch, capsys):
    system = _load_remote_system(monkeypatch)
    fake = _FakeRuntimeClient(raises=system.RuntimeApiError("Runtime API unavailable at http://iii.local:8765"))
    monkeypatch.setattr(system, "_remote_runtime_client", lambda: fake)

    with pytest.raises(SystemExit) as exc_info:
        system.list_nodes(SimpleNamespace())

    assert exc_info.value.code == 1
    assert "Runtime API error: Runtime API unavailable" in capsys.readouterr().out


def test_remote_logs_use_runtime_api_tail(monkeypatch, capsys):
    system = _load_remote_system(monkeypatch)
    fake = _FakeRuntimeClient(
        log_response={
            "lines": [
                {"source_id": "daemon", "line": "one"},
                {"source_id": "daemon", "line": "two"},
            ]
        }
    )
    monkeypatch.setattr(system, "_remote_runtime_client", lambda: fake)
    monkeypatch.setattr(system, "_host_forward", lambda *args: (_ for _ in ()).throw(AssertionError(args)))

    with pytest.raises(SystemExit) as exc_info:
        system.logs(SimpleNamespace(entity_id="daemon", follow=False, history=False))

    assert exc_info.value.code == 0
    assert fake.log_tails == [("daemon", 200)]
    assert "[daemon] one" in capsys.readouterr().out
