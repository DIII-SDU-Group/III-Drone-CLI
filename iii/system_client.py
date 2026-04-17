"""Socket client for the III system-manager daemon."""

from __future__ import annotations

import json
from pathlib import Path
import socket
import subprocess
import time
import os


class DaemonClient:
    def __init__(self):
        runtime_dir = Path(os.environ.get("III_SYSTEM_RUNTIME_DIR", Path.cwd() / "runtime")).expanduser()
        runtime_dir.mkdir(parents=True, exist_ok=True)

        self.socket_path = Path(
            os.environ.get("III_SYSTEM_DAEMON_SOCKET", runtime_dir / "system_manager.sock")
        ).expanduser()
        self.daemon_log = Path(
            os.environ.get("III_SYSTEM_DAEMON_LOG", runtime_dir / "system_manager.log")
        ).expanduser()
        self.systemd_service = os.environ.get("III_SYSTEMD_DAEMON_SERVICE", "iii-system-daemon.service")

    @staticmethod
    def _systemctl_command(*args: str) -> list[str]:
        command = ["systemctl", *args]
        if os.geteuid() != 0:
            command = ["sudo", "-n", *command]
        return command

    @staticmethod
    def _assert_systemd_available() -> None:
        result = subprocess.run(
            ["systemctl", "is-system-running"],
            check=False,
            capture_output=True,
            text=True,
        )
        output = f"{result.stdout}\n{result.stderr}".lower()
        unavailable_markers = (
            "offline",
            "not been booted with systemd",
            "failed to connect to bus",
            "host is down",
        )
        if any(marker in output for marker in unavailable_markers):
            raise RuntimeError(
                "System daemon must be managed by systemd, but systemd is not available. "
                "Rebuild/restart the devcontainer with systemd enabled."
            )

    def _systemd_service_is_active(self) -> bool:
        result = subprocess.run(
            ["systemctl", "is-active", "--quiet", self.systemd_service],
            check=False,
        )
        return result.returncode == 0

    def _request(self, payload: dict) -> dict:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(self.socket_path))
            client.sendall((json.dumps(payload) + "\n").encode("utf-8"))
            data = b""
            while not data.endswith(b"\n"):
                chunk = client.recv(4096)
                if not chunk:
                    break
                data += chunk
        if not data:
            raise RuntimeError("No response from system daemon.")
        response = json.loads(data.decode("utf-8"))
        if not response.get("ok"):
            raise RuntimeError(response.get("error", "Unknown daemon error"))
        return response["result"]

    def ping(self) -> bool:
        if not self.socket_path.exists():
            return False
        try:
            self._request({"command": "ping"})
        except Exception:
            return False
        return True

    def ensure_running(self, timeout_seconds: float = 10.0) -> None:
        self._assert_systemd_available()

        if self.ping():
            if not self._systemd_service_is_active():
                raise RuntimeError(
                    f"System daemon socket is responding, but {self.systemd_service} is not active. "
                    "Stop the stray daemon process and start the systemd service."
                )
            return

        if self.socket_path.exists():
            self.socket_path.unlink()

        self.daemon_log.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            self._systemctl_command("start", self.systemd_service),
            check=True,
        )

        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            if self.ping():
                return
            time.sleep(0.1)

        raise RuntimeError("Timed out waiting for the system daemon to start.")

    def boot(self, profile: str) -> dict:
        self.ensure_running()
        return self._request({"command": "boot", "profile": profile})

    def start(self, *, activate: bool, select_nodes: list[str], include_dependencies: bool) -> dict:
        return self._request(
            {
                "command": "start",
                "activate": activate,
                "select_nodes": select_nodes,
                "include_dependencies": include_dependencies,
            }
        )

    def stop(self, *, cleanup: bool, select_nodes: list[str], include_dependencies: bool) -> dict:
        return self._request(
            {
                "command": "stop",
                "cleanup": cleanup,
                "select_nodes": select_nodes,
                "include_dependencies": include_dependencies,
            }
        )

    def restart(self, *, cold: bool, select_nodes: list[str], include_dependencies: bool) -> dict:
        return self._request(
            {
                "command": "restart",
                "cold": cold,
                "select_nodes": select_nodes,
                "include_dependencies": include_dependencies,
            }
        )

    def shutdown(self, *, select_nodes: list[str], include_dependencies: bool) -> dict:
        return self._request(
            {
                "command": "shutdown",
                "select_nodes": select_nodes,
                "include_dependencies": include_dependencies,
            }
        )

    def status(self) -> dict:
        return self._request({"command": "status"})

    def list_nodes(self) -> list[str]:
        return self._request({"command": "list_nodes"})["managed_nodes"]

    def list_services(self) -> list[str]:
        return self._request({"command": "list_services"})["services"]

    def service_start(self, service_id: str) -> dict:
        return self._request({"command": "service_start", "service_id": service_id})

    def service_stop(self, service_id: str) -> dict:
        return self._request({"command": "service_stop", "service_id": service_id})

    def service_restart(self, service_id: str) -> dict:
        return self._request({"command": "service_restart", "service_id": service_id})

    def log_dir(self, entity_id: str) -> str:
        return self._request({"command": "log_dir", "entity_id": entity_id})["log_dir"]
