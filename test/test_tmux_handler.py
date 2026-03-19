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
    assert commands[0][:4] == ["tmux", "new-session", "-d", "-s"]
    assert any(command[:2] == ["tmux", "split-window"] for command in commands)


def test_tmux_handler_attach_requires_existing_session(monkeypatch):
    handler = TmuxHandler()
    monkeypatch.setattr(handler, "_list_sessions", lambda: set())

    assert not handler.attach("iii_sim")
