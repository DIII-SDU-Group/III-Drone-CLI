import json
from pathlib import Path
import subprocess

from iii.__main__ import main
from iii import rosbag


class _Writer:
    def __init__(self):
        self.value = ""

    def write(self, value):
        self.value += value

    def flush(self):
        pass


def _completed(command, code=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(command, code, stdout, stderr)


def test_rosbag_start_preserves_existing_topic_options_and_plan_gate(
    monkeypatch, tmp_path
):
    script = tmp_path / "iii_rosbag.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    script.chmod(0o755)
    calls = []
    monkeypatch.setattr(
        rosbag.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs))
        or _completed(command, stdout="recording started\n"),
    )
    environment = {
        "III_ROSBAG_SCRIPT": str(script),
        "III_OPERATION_STATE_DIR": str(tmp_path / "operations"),
    }
    args = [
        "rosbag",
        "start",
        "--id",
        "field run",
        "--topic",
        "/camera/image",
        "--topic",
        "/vehicle/state",
        "--include-hidden",
    ]
    planned = _Writer()
    assert (
        main([*args, "--dry-run", "--json"], stdout=planned, environment=environment)
        == 0
    )
    assert calls == []
    result = json.loads(planned.value)
    operation_id = result["operation"]["id"]
    assert result["payload"]["plan"]["argv"] == args
    assert (
        main(
            [
                *args,
                "--operation-id",
                operation_id,
                "--confirm",
                "--non-interactive",
                "--json",
            ],
            stdout=_Writer(),
            environment=environment,
        )
        == 0
    )
    assert calls[0][0] == [
        str(script),
        "start",
        "--id",
        "field run",
        "--topic",
        "/camera/image",
        "--topic",
        "/vehicle/state",
        "--include-hidden",
    ]
    assert calls[0][1]["capture_output"] is True


def test_rosbag_clear_requires_explicit_force_before_script_call(monkeypatch):
    calls = []
    monkeypatch.setattr(
        rosbag.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs)),
    )
    stdout = _Writer()
    assert main(["rosbag", "clear", "--json"], stdout=stdout) == 64
    assert calls == []


def test_rosbag_clear_force_still_requires_confirmed_operation_plan(
    monkeypatch, tmp_path
):
    script = tmp_path / "iii_rosbag.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    script.chmod(0o755)
    calls = []
    monkeypatch.setattr(
        rosbag.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs)),
    )
    output = _Writer()
    environment = {
        "III_ROSBAG_SCRIPT": str(script),
        "III_OPERATION_STATE_DIR": str(tmp_path / "operations"),
    }
    assert (
        main(
            ["rosbag", "clear", "--force", "--dry-run", "--json"],
            stdout=output,
            environment=environment,
        )
        == 0
    )
    result = json.loads(output.value)
    assert result["payload"]["plan"]["argv"] == ["rosbag", "clear", "--force"]
    assert calls == []


def test_rosbag_delete_refusal_and_script_exit_are_retained(monkeypatch, tmp_path):
    script = tmp_path / "iii_rosbag.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    script.chmod(0o755)
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return _completed(command, 1, "", "a recording is active; stop it first\n")

    monkeypatch.setattr(rosbag.subprocess, "run", run)
    stdout = _Writer()
    environment = {
        "III_ROSBAG_SCRIPT": str(script),
        "III_OPERATION_STATE_DIR": str(tmp_path / "operations"),
    }
    assert (
        main(
            [
                "rosbag",
                "delete",
                "capture-001",
                "--confirm",
                "--non-interactive",
                "--json",
            ],
            stdout=stdout,
            environment=environment,
        )
        == 30
    )
    result = json.loads(stdout.value)
    assert result["code"] == "III_ROSBAG_DELETE_FAILED"
    assert result["payload"]["exit_status"] == 1
    assert "a recording is active" in result["payload"]["stderr"]
    assert calls[0][0] == [str(script), "delete", "capture-001"]


def test_rosbag_human_output_is_rendered_and_supports_list(monkeypatch, tmp_path):
    script = Path(rosbag.SCRIPT_DEFAULT)
    calls = []
    monkeypatch.setattr(
        rosbag.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs))
        or _completed(command, stdout="No recordings.\n"),
    )
    stdout = _Writer()
    assert main(["rosbag", "list"], stdout=stdout) == 0
    assert "No recordings." in stdout.value
    assert calls[0][0] == [str(script), "list"]
