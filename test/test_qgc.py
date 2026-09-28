import json
from pathlib import Path
import subprocess

import pytest

from iii.__main__ import main
from iii import qgc


def _install(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    root = tmp_path / "gc"
    manifest = {
        "version": qgc.PINNED_VERSION,
        "url": qgc.PINNED_URL,
        "sha256": qgc.PINNED_SHA256,
        "size": qgc.PINNED_SIZE,
        "architecture": qgc.PINNED_ARCHITECTURE,
        "source_commit": qgc.PINNED_SOURCE_COMMIT,
    }
    binary = root / "qgc" / "QGroundControl.AppImage"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"pinned qgc")
    binary.chmod(0o755)
    # The payload bytes are deliberately tiny; tests below mock the digest
    # result to represent a byte-verified pinned AppImage.
    manifest["sha256"] = qgc.PINNED_SHA256
    manifest_path = root / "qgroundcontrol.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return root, {"III_GC_INSTALL_ROOT": str(root), "HOME": str(tmp_path)}


def test_qgc_parser_help_is_host_independent(capsys):
    assert main(["qgc", "--help"]) == 0
    assert "start" in capsys.readouterr().out


def test_start_requires_plan_then_confirm_and_reports_pin(monkeypatch, tmp_path):
    root, environment = _install(tmp_path)
    calls = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        if command[2] == "start":
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(
            command,
            0,
            "LoadState=loaded\nActiveState=active\nSubState=running\n"
            f"ExecStart={{ path={root}/qgc/QGroundControl.AppImage ; argv[]="
            f"{root}/qgc/QGroundControl.AppImage ; }}\n",
            "",
        )

    monkeypatch.setattr(qgc.subprocess, "run", fake_run)
    # The production digest check remains strict; use a mock hash object to
    # represent a verified pinned AppImage without embedding the release blob.
    monkeypatch.setattr(qgc, "_binary_sha256", lambda _binary: qgc.PINNED_SHA256)
    environment["III_OPERATION_STATE_DIR"] = str(tmp_path / "operations")
    # Use a deterministic retained plan/apply pair.
    plan_output = []
    assert (
        main(
            ["qgc", "start", "--dry-run", "--operation-id", "qgc-plan-test", "--json"],
            stdout=_ListWriter(plan_output),
            environment=environment,
        )
        == 0
    )
    assert (
        main(
            [
                "qgc",
                "start",
                "--operation-id",
                "qgc-plan-test",
                "--confirm",
                "--non-interactive",
                "--json",
            ],
            stdout=_ListWriter([]),
            environment=environment,
        )
        == 0
    )
    assert [call[2] for call in calls] == ["show", "start", "show"]


class _ListWriter:
    def __init__(self, output):
        self.output = output

    def write(self, value):
        self.output.append(value)

    def flush(self):
        pass


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sha256", "0" * 64),
        ("version", "5.0.9"),
        ("architecture", "aarch64"),
        ("source_commit", "0" * 40),
        ("url", "https://example.invalid/qgc.AppImage"),
        ("size", qgc.PINNED_SIZE + 1),
    ],
)
def test_start_rejects_tampered_manifest_without_systemctl(
    monkeypatch, tmp_path, field, value
):
    root, environment = _install(tmp_path)
    environment["III_OPERATION_STATE_DIR"] = str(tmp_path / "operations")
    pin_path = root / "qgroundcontrol.json"
    pin = json.loads(pin_path.read_text(encoding="utf-8"))
    pin[field] = value
    pin_path.write_text(json.dumps(pin), encoding="utf-8")
    commands = []
    monkeypatch.setattr(
        qgc.subprocess, "run", lambda command, **_kwargs: commands.append(command)
    )
    monkeypatch.setattr(qgc, "_binary_sha256", lambda _binary: qgc.PINNED_SHA256)
    assert (
        main(
            [
                "qgc",
                "start",
                "--operation-id",
                "qgc-bad-pin",
                "--confirm",
                "--non-interactive",
                "--json",
            ],
            stdout=_ListWriter([]),
            environment=environment,
        )
        == 20
    )
    assert commands == []


def test_start_does_not_call_systemctl_when_appimage_bytes_are_tampered(
    monkeypatch, tmp_path
):
    root, environment = _install(tmp_path)
    environment["III_OPERATION_STATE_DIR"] = str(tmp_path / "operations")
    binary = root / "qgc" / "QGroundControl.AppImage"
    binary.write_bytes(b"tampered AppImage")
    commands = []
    monkeypatch.setattr(
        qgc.subprocess, "run", lambda command, **_kwargs: commands.append(command)
    )
    assert (
        main(
            [
                "qgc",
                "start",
                "--operation-id",
                "qgc-tampered-binary",
                "--confirm",
                "--non-interactive",
                "--json",
            ],
            stdout=_ListWriter([]),
            environment=environment,
        )
        == 20
    )
    assert commands == []


def test_status_checks_selected_binary_and_loaded_user_unit(monkeypatch, tmp_path):
    root, environment = _install(tmp_path)
    environment["III_OPERATION_STATE_DIR"] = str(tmp_path / "operations")
    commands = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            "LoadState=loaded\nActiveState=inactive\nSubState=dead\n"
            f"ExecStart={{ path={root}/qgc/QGroundControl.AppImage ; }}\n",
            "",
        )

    monkeypatch.setattr(qgc.subprocess, "run", fake_run)
    monkeypatch.setattr(qgc, "_binary_sha256", lambda _binary: qgc.PINNED_SHA256)
    output = _ListWriter([])
    assert (
        main(["qgc", "status", "--json"], stdout=output, environment=environment) == 0
    )
    result = json.loads("".join(output.output))
    assert result["payload"]["selection"]["selected"] is True
    assert result["payload"]["selection"]["version"] == "5.0.8"
    assert (
        result["payload"]["selection"]["source_commit"]
        == "e0816c957602789200ae5ba0af45217f0f2f1db4"
    )
    assert result["payload"]["unit"]["ActiveState"] == "inactive"
    assert commands == [
        [
            "systemctl",
            "--user",
            "show",
            "iii-qgc.service",
            "--property=LoadState",
            "--property=ActiveState",
            "--property=SubState",
            "--property=ExecStart",
        ]
    ]


def test_status_rejects_unrelated_exec_binary_with_pinned_path_as_argument(
    monkeypatch, tmp_path
):
    root, environment = _install(tmp_path)
    monkeypatch.setattr(qgc, "_binary_sha256", lambda _binary: qgc.PINNED_SHA256)
    command = ["systemctl", "--user", "show", qgc.UNIT_NAME]
    monkeypatch.setattr(
        qgc,
        "_systemctl",
        lambda *_arguments: subprocess.CompletedProcess(
            command,
            0,
            "LoadState=loaded\nActiveState=inactive\n"
            f"ExecStart={{ path=/usr/bin/env ; argv[]=/usr/bin/env {root}/qgc/QGroundControl.AppImage ; }}\n",
            "",
        ),
    )
    output = _ListWriter([])
    assert (
        main(["qgc", "status", "--json"], stdout=output, environment=environment) == 30
    )
    result = json.loads("".join(output.output))
    assert result["code"] == "III_QGC_STATUS_INVALID"
    assert any(
        finding["code"] == "III_QGC_UNIT_BINARY_MISMATCH"
        for finding in result["findings"]
    )


def test_start_does_not_restart_unit_with_unrelated_exec_binary(monkeypatch, tmp_path):
    root, environment = _install(tmp_path)
    environment["III_OPERATION_STATE_DIR"] = str(tmp_path / "operations")
    monkeypatch.setattr(qgc, "_binary_sha256", lambda _binary: qgc.PINNED_SHA256)
    calls = []
    command = ["systemctl", "--user", "show", qgc.UNIT_NAME]

    def fake_systemctl(*arguments):
        calls.append(arguments)
        return subprocess.CompletedProcess(
            command,
            0,
            "LoadState=loaded\nExecStart={ path=/usr/bin/env ; argv[]=/usr/bin/env "
            f"{root}/qgc/QGroundControl.AppImage ; }}\n",
            "",
        )

    monkeypatch.setattr(qgc, "_systemctl", fake_systemctl)
    assert (
        main(
            [
                "qgc",
                "start",
                "--operation-id",
                "qgc-unit-mismatch",
                "--confirm",
                "--non-interactive",
                "--json",
            ],
            stdout=_ListWriter([]),
            environment=environment,
        )
        == 20
    )
    assert calls == [
        (
            "show",
            qgc.UNIT_NAME,
            "--property=LoadState",
            "--property=ActiveState",
            "--property=SubState",
            "--property=ExecStart",
        )
    ]


def test_start_reports_failure_when_systemd_returns_success_but_unit_is_inactive(
    monkeypatch, tmp_path
):
    root, environment = _install(tmp_path)
    environment["III_OPERATION_STATE_DIR"] = str(tmp_path / "operations")
    monkeypatch.setattr(qgc, "_binary_sha256", lambda _binary: qgc.PINNED_SHA256)
    calls = []

    def fake_systemctl(*arguments):
        calls.append(arguments)
        if arguments[0] == "start":
            return subprocess.CompletedProcess(
                ["systemctl", "--user", *arguments], 0, "", ""
            )
        state = (
            "inactive"
            if len([call for call in calls if call[0] == "show"]) == 1
            else "failed"
        )
        sub_state = "dead" if state == "inactive" else "failed"
        return subprocess.CompletedProcess(
            ["systemctl", "--user", *arguments],
            0,
            f"LoadState=loaded\nActiveState={state}\nSubState={sub_state}\n"
            f"ExecStart={{ path={root}/qgc/QGroundControl.AppImage ; }}\n",
            "",
        )

    monkeypatch.setattr(qgc, "_systemctl", fake_systemctl)
    output = _ListWriter([])
    assert (
        main(
            [
                "qgc",
                "start",
                "--operation-id",
                "qgc-start-inactive",
                "--confirm",
                "--non-interactive",
                "--json",
            ],
            stdout=output,
            environment=environment,
        )
        == 30
    )
    result = json.loads("".join(output.output))
    assert result["code"] == "III_QGC_READINESS_FAILED"
    assert result["payload"]["unit"]["ActiveState"] == "failed"
    assert [call[0] for call in calls] == ["show", "start", "show"]


def test_stop_reports_failure_when_systemd_times_out_into_failed_state(
    monkeypatch, tmp_path
):
    _root, environment = _install(tmp_path)
    environment["III_OPERATION_STATE_DIR"] = str(tmp_path / "operations")

    def fake_systemctl(*arguments):
        if arguments[0] == "stop":
            return subprocess.CompletedProcess(
                ["systemctl", "--user", *arguments], 0, "", ""
            )
        return subprocess.CompletedProcess(
            ["systemctl", "--user", *arguments],
            0,
            "LoadState=loaded\nActiveState=failed\nSubState=failed\n",
            "",
        )

    monkeypatch.setattr(qgc, "_systemctl", fake_systemctl)
    output = _ListWriter([])
    assert (
        main(
            ["qgc", "stop", "--confirm", "--non-interactive", "--json"],
            stdout=output,
            environment=environment,
        )
        == 30
    )
    result = json.loads("".join(output.output))
    assert result["code"] == "III_QGC_STOP_FAILED"
    assert result["payload"]["unit"]["ActiveState"] == "failed"


def test_stop_signals_owned_appimage_group_and_waits_for_inactive(
    monkeypatch, tmp_path
):
    _root, environment = _install(tmp_path)
    environment["III_OPERATION_STATE_DIR"] = str(tmp_path / "operations")
    calls = []

    def fake_systemctl(*arguments):
        calls.append(arguments)
        if arguments[0] == "kill":
            return subprocess.CompletedProcess(
                ["systemctl", "--user", *arguments], 0, "", ""
            )
        state = (
            "active"
            if len([call for call in calls if call[0] == "show"]) == 1
            else "inactive"
        )
        return subprocess.CompletedProcess(
            ["systemctl", "--user", *arguments],
            0,
            f"LoadState=loaded\nActiveState={state}\nSubState={'running' if state == 'active' else 'dead'}\n",
            "",
        )

    monkeypatch.setattr(qgc, "_systemctl", fake_systemctl)
    output = _ListWriter([])
    assert (
        main(
            ["qgc", "stop", "--confirm", "--non-interactive", "--json"],
            stdout=output,
            environment=environment,
        )
        == 0
    )
    result = json.loads("".join(output.output))
    assert result["code"] == "III_QGC_LIFECYCLE_COMPLETED"
    assert result["payload"]["unit"]["ActiveState"] == "inactive"
    assert calls[1] == ("kill", "--kill-who=all", "--signal=SIGINT", qgc.UNIT_NAME)


def test_stop_forces_only_remaining_owned_service_processes(monkeypatch, tmp_path):
    _root, environment = _install(tmp_path)
    environment["III_OPERATION_STATE_DIR"] = str(tmp_path / "operations")
    calls = []
    state = {"value": "active"}
    ticks = iter(range(100))
    monkeypatch.setattr(qgc.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(qgc.time, "sleep", lambda _seconds: None)

    def fake_systemctl(*arguments):
        calls.append(arguments)
        if arguments[0] == "kill":
            state["value"] = (
                "deactivating" if "--signal=SIGINT" in arguments else "failed"
            )
        elif arguments[0] == "reset-failed":
            state["value"] = "inactive"
        return subprocess.CompletedProcess(
            ["systemctl", "--user", *arguments],
            0,
            (
                f"LoadState=loaded\nActiveState={state['value']}\nSubState=dead\n"
                if arguments[0] == "show"
                else ""
            ),
            "",
        )

    monkeypatch.setattr(qgc, "_systemctl", fake_systemctl)
    output = _ListWriter([])
    assert (
        main(
            ["qgc", "stop", "--confirm", "--non-interactive", "--json"],
            stdout=output,
            environment=environment,
        )
        == 0
    )
    result = json.loads("".join(output.output))
    assert result["payload"]["unit"]["ActiveState"] == "inactive"
    assert any(
        finding["code"] == "III_QGC_FORCED_STOP" for finding in result["findings"]
    )
    assert ("kill", "--kill-who=all", "--signal=SIGKILL", qgc.UNIT_NAME) in calls
    assert ("reset-failed", qgc.UNIT_NAME) in calls
