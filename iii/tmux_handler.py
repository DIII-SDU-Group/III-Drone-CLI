"""tmux session helpers derived from the canonical system specification."""

from __future__ import annotations

import os
import subprocess


TMUX_ENVIRONMENT_NAMES = {
    "BEHAVIOR_TREES_DIR",
    "CLI_CONFIGURATION",
    "CONFIG_BASE_DIR",
    "CYCLONEDDS_URI",
    "DEBUGGABLE_NODES",
    "LD_LIBRARY_PATH",
    "MISSION_SPECIFICATION_DIR",
    "NODE_MANAGEMENT_CONFIG_DIR",
    "PATH",
    "PKG_CONFIG_PATH",
    "PYTHONPATH",
    "RMW_IMPLEMENTATION",
    "SIMULATION",
    "SIMULATION_CONFIG_DIR",
    "WORKSPACE_DIR",
}

TMUX_ENVIRONMENT_PREFIXES = (
    "AMENT_",
    "CMAKE_",
    "COLCON_",
    "GZ_",
    "III_",
    "IGN_",
    "ROS_",
)


class TmuxHandler:
    def _list_sessions(self) -> set[str]:
        process = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if process.returncode != 0:
            return set()
        return {line.strip() for line in process.stdout.splitlines() if line.strip()}

    def session_running(self, session_name: str) -> bool:
        return session_name in self._list_sessions()

    def _environment_flags(self) -> list[str]:
        flags: list[str] = []
        for key, value in sorted(os.environ.items()):
            if key in TMUX_ENVIRONMENT_NAMES or key.startswith(TMUX_ENVIRONMENT_PREFIXES):
                flags.extend(["-e", f"{key}={value}"])
        return flags

    def _pane_command(self, command: str) -> str:
        if command.strip() == "bash":
            return command
        return "\n".join(
            [
                command,
                "status=$?",
                "printf '\\n[tmux pane command exited with status %s]\\n' \"$status\"",
                "exec bash",
            ]
        )

    def start(self, session_spec: dict, attach: bool = False) -> bool:
        session_name = session_spec["session_name"]
        if self.session_running(session_name):
            print('System already booted. Use "iii system attach" to attach to the tmux session.')
            return False

        environment_flags = self._environment_flags()
        first_window = session_spec["windows"][0]
        first_pane = first_window["panes"][0]
        subprocess.run(
            [
                "tmux",
                "new-session",
                "-d",
                *environment_flags,
                "-s",
                session_name,
                "-n",
                first_window["name"],
                "bash",
                "-lc",
                self._pane_command(first_pane["command"]),
            ],
            check=True,
        )
        self._set_pane_title(session_name, first_window["name"], 0, first_pane["title"])
        self._populate_window(session_name, first_window, environment_flags)

        for window in session_spec["windows"][1:]:
            subprocess.run(
                [
                    "tmux",
                    "new-window",
                    *environment_flags,
                    "-t",
                    session_name,
                    "-n",
                    window["name"],
                    "bash",
                    "-lc",
                    self._pane_command(window["panes"][0]["command"]),
                ],
                check=True,
            )
            self._set_pane_title(session_name, window["name"], 0, window["panes"][0]["title"])
            self._populate_window(session_name, window, environment_flags)

        subprocess.run(
            ["tmux", "select-window", "-t", f"{session_name}:{session_spec['startup_window']}"],
            check=True,
        )

        if attach:
            return self.attach(session_name)

        print('System booted. Use "iii system start" to start the system.')
        return True

    def _populate_window(self, session_name: str, window: dict, environment_flags: list[str]) -> None:
        for index, pane in enumerate(window["panes"][1:], start=1):
            subprocess.run(
                [
                    "tmux",
                    "split-window",
                    *environment_flags,
                    "-t",
                    f"{session_name}:{window['name']}",
                    "bash",
                    "-lc",
                    self._pane_command(pane["command"]),
                ],
                check=True,
            )
            self._set_pane_title(session_name, window["name"], index, pane["title"])
        subprocess.run(
            ["tmux", "select-layout", "-t", f"{session_name}:{window['name']}", window["layout"]],
            check=True,
        )

    def _set_pane_title(self, session_name: str, window_name: str, pane_index: int, title: str) -> None:
        subprocess.run(
            [
                "tmux",
                "select-pane",
                "-t",
                f"{session_name}:{window_name}.{pane_index}",
                "-T",
                title,
            ],
            check=True,
        )

    def attach(self, session_name: str) -> bool:
        if not self.session_running(session_name):
            print('Session not running. Use "iii system boot --attach" to start the system and attach to the tmux session.')
            return False
        subprocess.run(["tmux", "attach", "-t", session_name], check=False)
        return True

    def kill_session(self, session_name: str) -> bool:
        if not self.session_running(session_name):
            print("Tmux session not running.")
            return False
        subprocess.run(["tmux", "kill-session", "-t", session_name], check=True)
        print("Tmux session killed.")
        return True
