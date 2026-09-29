import importlib
import os
import pytest
import time
from types import SimpleNamespace


def test_tail_latest_log_selects_newest_file_by_mtime(monkeypatch, tmp_path):
    monkeypatch.setenv("CLI_CONFIGURATION", "dev")
    system = importlib.import_module("iii.system")

    stale_by_name = tmp_path / "zzz_old.log"
    current = tmp_path / "aaa_current.log"
    stale_by_name.write_text("old\n", encoding="utf-8")
    current.write_text("current\n", encoding="utf-8")

    old_time = time.time() - 100
    new_time = time.time()
    os.utime(stale_by_name, (old_time, old_time))
    os.utime(current, (new_time, new_time))

    calls = []

    def fake_run(cmd, **kwargs):
        del kwargs
        calls.append(cmd)

        class _Result:
            returncode = 0

        return _Result()

    monkeypatch.setattr(system.subprocess, "run", fake_run)

    assert system._tail_latest_log(str(tmp_path), follow=False) == 0
    assert calls == [["tail", "-n", "200", str(current)]]


def test_tail_latest_log_prefers_current_run_log(monkeypatch, tmp_path):
    monkeypatch.setenv("CLI_CONFIGURATION", "dev")
    system = importlib.import_module("iii.system")

    current = tmp_path / "current.log"
    newer = tmp_path / "newer_ros.log"
    current.write_text("current\n", encoding="utf-8")
    newer.write_text("newer\n", encoding="utf-8")

    old_time = time.time() - 100
    new_time = time.time()
    os.utime(current, (old_time, old_time))
    os.utime(newer, (new_time, new_time))

    calls = []

    def fake_run(cmd, **kwargs):
        del kwargs
        calls.append(cmd)

        class _Result:
            returncode = 0

        return _Result()

    monkeypatch.setattr(system.subprocess, "run", fake_run)

    assert system._tail_latest_log(str(tmp_path), follow=False) == 0
    assert calls == [["tail", "-n", "200", str(current)]]


def test_tail_latest_log_history_prefers_process_log(monkeypatch, tmp_path):
    monkeypatch.setenv("CLI_CONFIGURATION", "dev")
    system = importlib.import_module("iii.system")

    history = tmp_path / "process.log"
    current = tmp_path / "current.log"
    history.write_text("history\n", encoding="utf-8")
    current.write_text("current\n", encoding="utf-8")

    calls = []

    def fake_run(cmd, **kwargs):
        del kwargs
        calls.append(cmd)

        class _Result:
            returncode = 0

        return _Result()

    monkeypatch.setattr(system.subprocess, "run", fake_run)

    assert system._tail_latest_log(str(tmp_path), follow=False, history=True) == 0
    assert calls == [["tail", "-n", "200", str(history)]]


def test_tail_file_follows_direct_log_path(monkeypatch, tmp_path):
    monkeypatch.setenv("CLI_CONFIGURATION", "dev")
    system = importlib.import_module("iii.system")
    log_file = tmp_path / "system_manager.log"
    log_file.write_text("daemon\n", encoding="utf-8")

    calls = []

    def fake_run(cmd, **kwargs):
        del kwargs
        calls.append(cmd)

        class _Result:
            returncode = 0

        return _Result()

    monkeypatch.setattr(system.subprocess, "run", fake_run)

    assert system._tail_file(log_file, follow=True) == 0
    assert calls == [["tail", "-f", str(log_file)]]


def test_shutdown_treats_not_booted_daemon_as_success(monkeypatch, capsys):
    monkeypatch.setenv("CLI_CONFIGURATION", "dev")
    system = importlib.import_module("iii.system")

    class _Client:
        def ping(self):
            return True

        def shutdown(self, **kwargs):
            del kwargs
            raise RuntimeError("System is not booted.")

    monkeypatch.setattr(system, "_local_client", lambda: _Client())
    args = SimpleNamespace(select_nodes=[], include_dependencies=False, keep_session=True)

    with pytest.raises(SystemExit) as exc_info:
        system.shutdown(args)

    assert exc_info.value.code == 0
    assert "not booted" in capsys.readouterr().out


def test_start_reports_not_booted_without_traceback(monkeypatch, capsys):
    monkeypatch.setenv("CLI_CONFIGURATION", "dev")
    system = importlib.import_module("iii.system")

    class _Client:
        def ping(self):
            return True

        def start(self, **kwargs):
            del kwargs
            raise RuntimeError("System is not booted.")

    monkeypatch.setattr(system, "_local_client", lambda: _Client())
    args = SimpleNamespace(select_nodes=[], include_dependencies=False, skip_activate=False)

    with pytest.raises(SystemExit) as exc_info:
        system.start(args)

    assert exc_info.value.code == 1
    assert 'Use "iii system boot" first' in capsys.readouterr().out


def test_shutdown_kills_tmux_session_by_default(monkeypatch):
    monkeypatch.setenv("CLI_CONFIGURATION", "dev")
    system = importlib.import_module("iii.system")

    class _Client:
        def ping(self):
            return True

        def shutdown(self, **kwargs):
            del kwargs
            return {"success": True}

    killed_sessions = []

    class _Tmux:
        def kill_session(self, session_name):
            killed_sessions.append(session_name)
            return True

    monkeypatch.setattr(system, "_local_client", lambda: _Client())
    monkeypatch.setattr(system, "TmuxHandler", _Tmux)
    args = SimpleNamespace(select_nodes=[], include_dependencies=False, keep_session=False)

    with pytest.raises(SystemExit) as exc_info:
        system.shutdown(args)

    assert exc_info.value.code == 0
    assert killed_sessions == ["iii_sim"]


def test_shutdown_keeps_tmux_session_when_requested(monkeypatch):
    monkeypatch.setenv("CLI_CONFIGURATION", "dev")
    system = importlib.import_module("iii.system")

    class _Client:
        def ping(self):
            return True

        def shutdown(self, **kwargs):
            del kwargs
            return {"success": True}

    class _Tmux:
        def kill_session(self, session_name):
            raise AssertionError(f"unexpected kill: {session_name}")

    monkeypatch.setattr(system, "_local_client", lambda: _Client())
    monkeypatch.setattr(system, "TmuxHandler", _Tmux)
    args = SimpleNamespace(select_nodes=[], include_dependencies=False, keep_session=True)

    with pytest.raises(SystemExit) as exc_info:
        system.shutdown(args)

    assert exc_info.value.code == 0


def test_boot_replaces_stale_tmux_session_when_daemon_was_rebooted(monkeypatch):
    monkeypatch.setenv("CLI_CONFIGURATION", "dev")
    system = importlib.import_module("iii.system")

    class _Client:
        def boot(self, profile):
            assert profile == "sim"
            return {
                "booted": False,
                "tmux": {
                    "session_name": "iii_sim",
                    "startup_window": "system",
                    "windows": [],
                },
            }

    events = []

    class _Tmux:
        def __init__(self):
            self._running = True

        def session_running(self, session_name):
            assert session_name == "iii_sim"
            return self._running

        def kill_session(self, session_name):
            events.append(("kill", session_name))
            self._running = False
            return True

        def start(self, session_spec, attach=False):
            events.append(("start", session_spec["session_name"], attach))
            return True

    monkeypatch.setattr(system, "_ensure_local_daemon", lambda: _Client())
    monkeypatch.setattr(system, "TmuxHandler", _Tmux)
    monkeypatch.setattr(
        system,
        "_sim_mission_catalog_preflight",
        lambda: {"rebuilt": False, "catalog_hash": "sha256:" + "a" * 64},
    )

    with pytest.raises(SystemExit) as exc_info:
        system.boot(SimpleNamespace(attach=False))

    assert exc_info.value.code == 0
    assert events == [("kill", "iii_sim"), ("start", "iii_sim", False)]


def test_boot_reports_daemon_policy_rejection_as_operator_failure(monkeypatch, capsys):
    monkeypatch.setenv("CLI_CONFIGURATION", "dev")
    system = importlib.import_module("iii.system")

    class _Client:
        def boot(self, profile):
            assert profile == "opti_track"
            raise RuntimeError(
                "aircraft configuration is not receiver-reconciled; "
                "runtime mutation is forbidden"
            )

    monkeypatch.setattr(system, "_ensure_local_daemon", lambda: _Client())

    with pytest.raises(SystemExit) as exc_info:
        system.boot(SimpleNamespace(attach=False, profile="opti_track"))

    assert exc_info.value.code == 1
    assert "not receiver-reconciled" in capsys.readouterr().out


def test_daemon_restart_wraps_systemctl(monkeypatch):
    monkeypatch.setenv("CLI_CONFIGURATION", "dev")
    monkeypatch.setenv("III_SYSTEMD_DAEMON_SERVICE", "test-daemon.service")
    system = importlib.import_module("iii.system")

    calls = []

    def fake_run(cmd, **kwargs):
        del kwargs
        calls.append(cmd)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(system.subprocess, "run", fake_run)
    args = SimpleNamespace(daemon_action="restart")

    with pytest.raises(SystemExit) as exc_info:
        system.daemon(args)

    assert exc_info.value.code == 0
    expected_command = ["systemctl", "restart", "test-daemon.service"]
    if os.geteuid() != 0:
        expected_command = ["sudo", "-n", *expected_command]
    assert calls == [expected_command]


def test_daemon_logs_wraps_journalctl(monkeypatch):
    monkeypatch.setenv("CLI_CONFIGURATION", "dev")
    monkeypatch.setenv("III_SYSTEMD_DAEMON_SERVICE", "test-daemon.service")
    system = importlib.import_module("iii.system")

    calls = []

    def fake_run(cmd, **kwargs):
        del kwargs
        calls.append(cmd)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(system.subprocess, "run", fake_run)
    args = SimpleNamespace(daemon_action="logs", follow=True)

    with pytest.raises(SystemExit) as exc_info:
        system.daemon(args)

    assert exc_info.value.code == 0
    assert calls == [["journalctl", "-u", "test-daemon.service", "-f"]]
