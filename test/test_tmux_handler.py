from iii.tmux_handler import TmuxHandler


def test_tmux_handler_start_materializes_session_commands(monkeypatch):
    commands = []

    def fake_run(cmd, **kwargs):
        commands.append(cmd)

        class _Result:
            returncode = 0
            stdout = ""

        return _Result()

    handler = TmuxHandler()
    monkeypatch.setattr(handler, "_list_sessions", lambda: set())
    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setenv("CLI_CONFIGURATION", "dev")
    monkeypatch.setenv("III_SYSTEM_PROFILE", "sim")

    session_spec = {
        "session_name": "iii_sim",
        "startup_window": "system",
        "windows": [
            {
                "name": "system",
                "layout": "even-horizontal",
                "panes": [
                    {"title": "status", "command": "iii system status --watch"},
                    {"title": "shell", "command": "bash"},
                ],
            }
        ],
    }

    assert handler.start(session_spec, attach=False)
    assert commands[0][:3] == ["tmux", "new-session", "-d"]
    assert "-s" in commands[0]
    assert "CLI_CONFIGURATION=dev" in commands[0]
    assert "III_SYSTEM_PROFILE=sim" in commands[0]
    assert any("[tmux pane command exited with status %s]" in part for part in commands[0])
    assert any(command[:2] == ["tmux", "split-window"] for command in commands)


def test_tmux_handler_attach_requires_existing_session(monkeypatch):
    handler = TmuxHandler()
    monkeypatch.setattr(handler, "_list_sessions", lambda: set())

    assert not handler.attach("iii_sim")
