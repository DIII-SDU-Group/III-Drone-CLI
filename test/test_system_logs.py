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
    args = SimpleNamespace(select_nodes=[], include_dependencies=False, kill_session=False)

    with pytest.raises(SystemExit) as exc_info:
        system.shutdown(args)

    assert exc_info.value.code == 0
    assert "not booted" in capsys.readouterr().out


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
    assert calls == [["sudo", "-n", "systemctl", "restart", "test-daemon.service"]]


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
