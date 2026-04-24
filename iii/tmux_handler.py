"""tmux session helpers derived from the canonical system specification."""

from __future__ import annotations

import subprocess


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

    def start(self, session_spec: dict, attach: bool = False) -> bool:
        session_name = session_spec["session_name"]
        if self.session_running(session_name):
            print('System already booted. Use "iii system attach" to attach to the tmux session.')
            return False

        first_window = session_spec["windows"][0]
        first_pane = first_window["panes"][0]
        subprocess.run(
            [
                "tmux",
                "new-session",
                "-d",
                "-s",
                session_name,
                "-n",
                first_window["name"],
                "bash",
                "-lc",
                first_pane["command"],
            ],
            check=True,
        )
        self._set_pane_title(session_name, first_window["name"], 0, first_pane["title"])
        self._populate_window(session_name, first_window)

        for window in session_spec["windows"][1:]:
            subprocess.run(
                [
                    "tmux",
                    "new-window",
                    "-t",
                    session_name,
                    "-n",
                    window["name"],
                    "bash",
                    "-lc",
                    window["panes"][0]["command"],
                ],
                check=True,
            )
            self._set_pane_title(session_name, window["name"], 0, window["panes"][0]["title"])
            self._populate_window(session_name, window)

        subprocess.run(
            ["tmux", "select-window", "-t", f"{session_name}:{session_spec['startup_window']}"],
            check=True,
        )

        if attach:
            return self.attach(session_name)

        print('System booted. Use "iii system start" to start the system.')
        return True

    def _populate_window(self, session_name: str, window: dict) -> None:
        for index, pane in enumerate(window["panes"][1:], start=1):
            subprocess.run(
                [
                    "tmux",
                    "split-window",
                    "-t",
                    f"{session_name}:{window['name']}",
                    "bash",
                    "-lc",
                    pane["command"],
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
