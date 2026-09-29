import json
import subprocess

from iii.__main__ import main
from iii import api


class _Writer:
    def __init__(self):
        self.value = ""

    def write(self, value):
        self.value += value

    def flush(self):
        pass


def _completed(command, code=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(command, code, stdout, stderr)


def test_api_status_checks_expected_service_and_preserves_result(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return _completed(command, stdout="active\n")

    monkeypatch.setattr(api.subprocess, "run", run)
    stdout = _Writer()
    assert main(["api", "status", "--json"], stdout=stdout) == 0
    result = json.loads(stdout.value)
    assert result["code"] == "III_API_STATUS_COMPLETED"
    assert result["payload"]["service"] == "iii-runtime-api.service"
    assert result["payload"]["stdout"] == "active\n"
    assert calls[0][0] == ["systemctl", "is-active", "iii-runtime-api.service"]
    assert calls[0][1]["capture_output"] is True


def test_api_mutation_requires_universal_confirmation_and_calls_systemctl(
    monkeypatch, tmp_path
):
    calls = []
    monkeypatch.setattr(
        api.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs))
        or _completed(command),
    )
    environment = {"III_OPERATION_STATE_DIR": str(tmp_path / "operations")}
    planned = _Writer()
    assert (
        main(
            ["api", "restart", "--dry-run", "--json"],
            stdout=planned,
            environment=environment,
        )
        == 0
    )
    assert calls == []
    result = json.loads(planned.value)
    operation_id = result["operation"]["id"]
    assert result["payload"]["plan"]["argv"] == ["api", "restart"]
    assert (
        main(
            [
                "api",
                "restart",
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
        "sudo",
        "-n",
        "systemctl",
        "restart",
        "iii-runtime-api.service",
    ]


def test_api_failure_retains_script_output_and_universal_failure(monkeypatch):
    monkeypatch.setattr(
        api.subprocess,
        "run",
        lambda command, **kwargs: _completed(command, 4, "", "unit unavailable\n"),
    )
    stdout = _Writer()
    assert main(["api", "status", "--json"], stdout=stdout) == 30
    result = json.loads(stdout.value)
    assert result["payload"]["exit_status"] == 4
    assert result["payload"]["stderr"] == "unit unavailable\n"


def test_api_logs_follow_inherits_terminal_streams(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return _completed(command, 0)

    monkeypatch.setattr(api.subprocess, "run", run)
    assert main(["api", "logs", "--follow"]) == 0
    assert calls[0][0] == [
        "sudo",
        "-n",
        "journalctl",
        "-u",
        "iii-runtime-api.service",
        "--follow",
    ]
    assert "capture_output" not in calls[0][1]


def test_api_logs_follow_rejects_json_before_streaming(monkeypatch):
    calls = []
    monkeypatch.setattr(
        api.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs)),
    )
    stdout = _Writer()
    assert main(["api", "logs", "--follow", "--json"], stdout=stdout) == 64
    assert json.loads(stdout.value)["outcome"] == "usage_error"
    assert calls == []
