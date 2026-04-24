import json
import socketserver
import threading
from pathlib import Path

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
        assert result["echo"] == {"command": "status"}
        assert client.ping()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1.0)
