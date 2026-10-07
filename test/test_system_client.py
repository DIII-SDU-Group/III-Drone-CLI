import json
import socketserver
import threading
from types import SimpleNamespace

from iii.system_client import DaemonClient


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        request = json.loads(self.rfile.readline().decode("utf-8"))
        response = {"ok": True, "result": {"echo": request}}
        self.wfile.write((json.dumps(response) + "\n").encode("utf-8"))


def test_daemon_client_request_round_trip(tmp_path, monkeypatch):
    socket_path = tmp_path / "daemon.sock"
    monkeypatch.setenv("III_SYSTEM_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("III_SYSTEM_DAEMON_SOCKET", str(socket_path))

    server = socketserver.ThreadingUnixStreamServer(str(socket_path), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        client = DaemonClient()
        result = client._request({"command": "status"})
        assert result["echo"] == {"command": "status", "daemon_timeout_sec": client.request_timeout_sec}
        assert client.ping()
        assert client.service_start("micro_ros_agent")["echo"] == {
            "command": "service_start",
            "service_id": "micro_ros_agent",
            "daemon_timeout_sec": client.request_timeout_sec,
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1.0)


def test_ensure_running_starts_systemd_daemon_service(tmp_path, monkeypatch):
    socket_path = tmp_path / "daemon.sock"
    log_path = tmp_path / "daemon.log"
    monkeypatch.setenv("III_SYSTEM_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("III_SYSTEM_DAEMON_SOCKET", str(socket_path))
    monkeypatch.setenv("III_SYSTEM_DAEMON_LOG", str(log_path))
    monkeypatch.setenv("III_SYSTEMD_DAEMON_SERVICE", "test-iii-daemon.service")

    client = DaemonClient()
    calls = []
    ping_results = iter([False, True])

    monkeypatch.setattr(client, "ping", lambda: next(ping_results))
    monkeypatch.setattr(client, "_assert_systemd_available", lambda: None)

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        socket_path.touch()

        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("iii.system_client.subprocess.run", fake_run)

    client.ensure_running(timeout_seconds=0.2)

    assert calls
    assert calls[0][0][-3:] == ["systemctl", "start", "test-iii-daemon.service"]


def test_ensure_running_requires_systemd(tmp_path, monkeypatch):
    socket_path = tmp_path / "daemon.sock"
    monkeypatch.setenv("III_SYSTEM_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("III_SYSTEM_DAEMON_SOCKET", str(socket_path))

    client = DaemonClient()
    monkeypatch.setattr(client, "ping", lambda: False)

    def fake_run(cmd, **kwargs):
        del cmd, kwargs
        return SimpleNamespace(returncode=1, stdout="offline\n", stderr="")

    monkeypatch.setattr("iii.system_client.subprocess.run", fake_run)

    try:
        client.ensure_running(timeout_seconds=0.2)
    except RuntimeError as exc:
        assert "systemd is not available" in str(exc)
    else:
        raise AssertionError("Expected ensure_running to fail when systemd is unavailable.")


def test_ensure_running_rejects_stray_non_systemd_daemon(tmp_path, monkeypatch):
    socket_path = tmp_path / "daemon.sock"
    monkeypatch.setenv("III_SYSTEM_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("III_SYSTEM_DAEMON_SOCKET", str(socket_path))

    client = DaemonClient()
    monkeypatch.setattr(client, "_assert_systemd_available", lambda: None)
    monkeypatch.setattr(client, "ping", lambda: True)
    monkeypatch.setattr(client, "_systemd_service_is_active", lambda: False)

    try:
        client.ensure_running(timeout_seconds=0.2)
    except RuntimeError as exc:
        assert "not active" in str(exc)
    else:
        raise AssertionError("Expected ensure_running to reject a non-systemd daemon.")


def test_local_boot_and_start_hold_the_flight_controller_to_the_px4_baseline(monkeypatch, capsys):
    import pytest

    from iii import system
    from iii.runtime_api_client import RuntimeApiError

    def gate(profile, state=None, error=None):
        class Client:
            def __init__(self, **_kwargs):
                pass

            def px4_parameter_baseline(self):
                if error:
                    raise RuntimeApiError(error)
                return state

        monkeypatch.setattr(system, "RuntimeApiClient", Client)
        monkeypatch.setattr(system, "_PX4_BASELINE_SETTLE_SECONDS", 0.0)
        system._px4_baseline_gate(profile)

    # Simulated-PX4 profiles never ask; a matching flight controller passes.
    gate("sim", error="must not be called")
    gate("hil", error="must not be called")
    gate("opti_track", {"profile": "opti_track", "rejection": None})

    for state, error, expected in (
        ({"profile": "real", "rejection": "PX4 parameters differ. Run `iii px4 param-baseline --profile real`."},
         None, "iii px4 param-baseline --profile real"),
        (None, "Runtime API unavailable", "iii px4 param-baseline --profile real"),
        ({"profile": "hil", "rejection": None}, None, "runs profile hil"),
    ):
        with pytest.raises(SystemExit) as stopped:
            gate("real", state, error)
        assert stopped.value.code == 1
        assert expected in capsys.readouterr().out
