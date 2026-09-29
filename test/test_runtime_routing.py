import json
import os
from pathlib import Path
import shlex
import subprocess
import pytest

from iii.__main__ import main
from iii import runtime_routing


EXPECTED_HASH = "a" * 64
CHECKOUT = "/work/iii-checkout"


class _Output:
    def __init__(self):
        self.value = ""

    def write(self, value):
        self.value += value

    def flush(self):
        pass


class _TTYInput:
    def isatty(self):
        return True


def _native_install(tmp_path: Path, profile="dev", **environment):
    root = tmp_path / "gc"
    root.mkdir()
    manifest = {
        "schema": "iii.gc-install/v1",
        "profile": profile,
        "checkout": {
            "checkout": CHECKOUT,
            "revision": "b" * 40,
            "content_sha256": {runtime_routing.CLI_TREE_RELATIVE: EXPECTED_HASH},
        },
    }
    (root / "install.json").write_text(json.dumps(manifest), encoding="utf-8")
    env = {
        "III_GC_INSTALL_ROOT": str(root),
        "III_GC_INSTALL_PROFILE": profile,
        **environment,
    }
    return root, env


def _completed(command, *, code=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(command, code, stdout, stderr)


def _fake_docker_run(
    calls, *, container="dev-123", identity=EXPECTED_HASH, action=None
):
    def run(command, **kwargs):
        calls.append((command, kwargs))
        if command[:2] == ["docker", "ps"]:
            return _completed(command, stdout=f"{container}\n" if container else "")
        if "python3" in command and "-c" in command:
            return _completed(command, stdout=json.dumps({"sha256": identity}))
        if action is not None:
            return action(command, kwargs)
        return _completed(command, stdout='{"code":"III_RUNTIME_STATUS"}')

    return run


@pytest.mark.parametrize(
    "argv",
    [
        ["system", "status", "--runtime-target", "sim", "--json"],
        ["--runtime-target", "sim", "system", "status", "--json"],
    ],
)
def test_native_sim_routes_by_checkout_label_and_preserves_json_result(
    monkeypatch, tmp_path, argv
):
    _root, environment = _native_install(tmp_path, III_RUNTIME_TARGET="sim")
    calls = []
    monkeypatch.setattr(runtime_routing.subprocess, "run", _fake_docker_run(calls))
    stdout = _Output()
    assert (
        main(
            argv,
            stdout=stdout,
            environment=environment,
        )
        == 0
    )
    result = json.loads(stdout.value)
    assert result["context"]["target"] == "devcontainer:dev-123"
    assert result["context"]["profile"] == "sim"
    assert result["payload"]["runtime_result"]["code"] == "III_RUNTIME_STATUS"
    action = calls[-1][0]
    assert action[:12] == [
        "docker",
        "exec",
        "--user",
        "iii",
        "--workdir",
        "/home/iii/ws",
        "dev-123",
        "env",
        "-u",
        "III_GC_INSTALL_ROOT",
        "-u",
        "III_GC_INSTALL_PROFILE",
    ]
    assert action[12:15] == ["-u", "III_RUNTIME_TARGET", "bash"]
    assert action[15] == "-lc"
    assert "source /home/iii/ws/setup/setup_dev.bash" in action[16]
    assert "unset III_GC_INSTALL_ROOT III_GC_INSTALL_PROFILE" in action[16]
    assert action[16].endswith(
        "exec /home/iii/ws/tools/III-Drone-CLI/bin/iii system status --json"
    )


def test_missing_target_fails_closed_before_docker_lookup(monkeypatch, tmp_path):
    _root, environment = _native_install(tmp_path)
    calls = []
    monkeypatch.setattr(
        runtime_routing.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs)),
    )
    stdout = _Output()
    assert (
        main(["system", "status", "--json"], stdout=stdout, environment=environment)
        == 20
    )
    assert json.loads(stdout.value)["code"] == "III_RUNTIME_TARGET_REQUIRED"
    assert calls == []


def test_dry_run_exposes_target_profile_and_checkout_route(monkeypatch, tmp_path):
    _root, environment = _native_install(tmp_path, III_RUNTIME_TARGET="sim")
    calls = []
    monkeypatch.setattr(runtime_routing.subprocess, "run", _fake_docker_run(calls))
    stdout = _Output()
    assert (
        main(
            ["system", "start", "--dry-run", "--json"],
            stdout=stdout,
            environment=environment,
        )
        == 0
    )
    result = json.loads(stdout.value)
    assert result["context"] == {
        "target": "devcontainer:dev-123",
        "profile": "sim",
        "release_id": None,
    }
    assert result["payload"]["plan"]["preflight"]["runtime_target"] == "sim"
    assert (
        result["payload"]["plan"]["preflight"]["observed_cli_sha256"] == EXPECTED_HASH
    )
    assert calls[0][0][:2] == ["docker", "ps"]
    assert len(calls) == 2


def test_target_outside_installed_profile_fails_closed(monkeypatch, tmp_path):
    _root, environment = _native_install(tmp_path, "deploy", III_RUNTIME_TARGET="hil")
    calls = []
    monkeypatch.setattr(
        runtime_routing.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs)),
    )
    stdout = _Output()
    assert (
        main(["mission", "status", "--json"], stdout=stdout, environment=environment)
        == 20
    )
    assert json.loads(stdout.value)["code"] == "III_RUNTIME_TARGET_PROFILE_MISMATCH"
    assert calls == []


def test_missing_checkout_devcontainer_has_no_local_fallback(monkeypatch, tmp_path):
    _root, environment = _native_install(tmp_path, III_RUNTIME_TARGET="sim")
    calls = []
    monkeypatch.setattr(
        runtime_routing.subprocess,
        "run",
        _fake_docker_run(calls, container=None),
    )
    stdout = _Output()
    assert (
        main(["system", "status", "--json"], stdout=stdout, environment=environment)
        == 20
    )
    assert json.loads(stdout.value)["code"] == "III_RUNTIME_CONTAINER_UNAVAILABLE"
    assert len(calls) == 1


def test_ssh_checkout_mismatch_prevents_command_execution(monkeypatch, tmp_path):
    _root, environment = _native_install(
        tmp_path, III_RUNTIME_TARGET="hil", III_SSH_HOST="pi.example"
    )
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return _completed(command, stdout=json.dumps({"sha256": "c" * 64}))

    monkeypatch.setattr(runtime_routing.subprocess, "run", run)
    stdout = _Output()
    assert (
        main(["system", "status", "--json"], stdout=stdout, environment=environment)
        == 20
    )
    assert json.loads(stdout.value)["code"] == "III_RUNTIME_CHECKOUT_MISMATCH"
    assert len(calls) == 1 and calls[0][0][0] == "ssh"


def test_ssh_route_defaults_to_iii_local(monkeypatch, tmp_path):
    _root, environment = _native_install(tmp_path, III_RUNTIME_TARGET="hil")
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        if len(calls) == 1:
            return _completed(command, stdout=json.dumps({"sha256": EXPECTED_HASH}))
        return _completed(command, stdout='{"code":"III_RUNTIME_STATUS"}')

    monkeypatch.setattr(runtime_routing.subprocess, "run", run)
    stdout = _Output()
    assert (
        main(["system", "status", "--json"], stdout=stdout, environment=environment)
        == 0
    )
    result = json.loads(stdout.value)
    assert result["context"]["target"] == "iii@iii.local"
    assert "iii@iii.local" in calls[0][0]


def test_remote_profile_default_iii_local_selects_runtime_host():
    assert runtime_routing._command_target(
        {
            "III_RUNTIME_API_HOST": "iii.local",
            "III_RUNTIME_API_URL": "http://iii.local:8765",
        }
    ) == ("iii", "iii.local")


def test_ssh_route_rejects_conflicting_runtime_host_aliases(monkeypatch, tmp_path):
    _root, environment = _native_install(
        tmp_path,
        III_RUNTIME_TARGET="hil",
        III_SSH_HOST="pi-one.example",
        III_RUNTIME_HOST="pi-two.example",
    )
    calls = []
    monkeypatch.setattr(
        runtime_routing.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs)),
    )
    stdout = _Output()
    assert (
        main(["system", "status", "--json"], stdout=stdout, environment=environment)
        == 20
    )
    result = json.loads(stdout.value)
    assert result["code"] == "III_RUNTIME_HOST_CONFLICT"
    assert calls == []


@pytest.mark.parametrize("argv", [
    ["--runtime-target", "hil", "--host", "alternate.local", "system", "status", "--json"],
    ["system", "status", "--runtime-target", "hil", "--host=alternate.local", "--json"],
])
def test_runtime_host_argument_overrides_inherited_aliases(monkeypatch, tmp_path, argv):
    _root, environment = _native_install(
        tmp_path,
        III_RUNTIME_TARGET="hil",
        III_SSH_HOST="old.local",
        III_RUNTIME_HOST="old.local",
        III_RUNTIME_API_URL="http://old.local:8765",
        III_HIL_PI_ADDRESS="10.42.0.15",
    )
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            return _completed(command, stdout=json.dumps({"sha256": EXPECTED_HASH}))
        return _completed(command, stdout='{"code":"III_RUNTIME_STATUS"}')

    monkeypatch.setattr(runtime_routing.subprocess, "run", run)
    output = _Output()
    assert main(argv, stdout=output, environment=environment) == 0
    assert json.loads(output.value)["context"]["target"] == "iii@alternate.local"
    assert "iii@alternate.local" in calls[0]
    assert "--host" not in calls[-1][-1]


def test_ssh_identity_failure_is_reported_without_remote_mutation(
    monkeypatch, tmp_path
):
    _root, environment = _native_install(
        tmp_path, profile="deploy", III_RUNTIME_TARGET="real", III_SSH_HOST="pi.example"
    )
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return _completed(command, code=255, stderr="connection refused")

    monkeypatch.setattr(runtime_routing.subprocess, "run", run)
    stdout = _Output()
    assert (
        main(["system", "start", "--json"], stdout=stdout, environment=environment)
        == 20
    )
    assert json.loads(stdout.value)["code"] == "III_RUNTIME_IDENTITY_UNVERIFIABLE"
    assert len(calls) == 1 and calls[0][0][0] == "ssh"


def _ssh_option_values(command):
    return {
        command[index + 1].split("=", 1)[0]: command[index + 1].split("=", 1)[1]
        for index, token in enumerate(command[:-1])
        if token == "-o"
    }


def test_ssh_route_is_non_interactive_and_bounded_by_default(monkeypatch, tmp_path):
    _root, environment = _native_install(
        tmp_path, III_RUNTIME_TARGET="hil", III_SSH_HOST="pi.example"
    )
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if len(calls) == 1:
            return _completed(command, stdout=json.dumps({"sha256": EXPECTED_HASH}))
        return _completed(command, stdout='{"code":"III_RUNTIME_STATUS"}')

    monkeypatch.setattr(runtime_routing.subprocess, "run", run)
    assert main(["system", "status", "--json"], stdout=_Output(), environment=environment) == 0
    assert len(calls) == 2
    for command, options in calls:
        assert command[:2] in (["ssh", "-T"],)
        assert _ssh_option_values(command) == {
            "BatchMode": "yes",
            "ConnectTimeout": "10",
            "ServerAliveInterval": "5",
            "ServerAliveCountMax": "3",
        }
        # The destination and remote command stay last.
        assert command[-2] == "iii@pi.example"
    assert calls[0][1]["timeout"] == runtime_routing.DEFAULT_IDENTITY_TIMEOUT_SEC
    assert calls[1][1]["timeout"] == runtime_routing.DEFAULT_COMMAND_TIMEOUT_SEC


def test_ssh_route_timeouts_are_configurable(monkeypatch, tmp_path):
    _root, environment = _native_install(
        tmp_path,
        III_RUNTIME_TARGET="hil",
        III_SSH_HOST="pi.example",
        III_SSH_CONNECT_TIMEOUT_SEC="3",
        III_SSH_SERVER_ALIVE_INTERVAL_SEC="2",
        III_SSH_SERVER_ALIVE_COUNT_MAX="4",
        III_RUNTIME_ROUTE_IDENTITY_TIMEOUT_SEC="7.5",
        III_RUNTIME_ROUTE_COMMAND_TIMEOUT_SEC="0",
    )
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if len(calls) == 1:
            return _completed(command, stdout=json.dumps({"sha256": EXPECTED_HASH}))
        return _completed(command, stdout='{"code":"III_RUNTIME_STATUS"}')

    monkeypatch.setattr(runtime_routing.subprocess, "run", run)
    assert main(["system", "status", "--json"], stdout=_Output(), environment=environment) == 0
    assert _ssh_option_values(calls[0][0])["ConnectTimeout"] == "3"
    assert _ssh_option_values(calls[1][0])["ServerAliveInterval"] == "2"
    assert _ssh_option_values(calls[1][0])["ServerAliveCountMax"] == "4"
    assert calls[0][1]["timeout"] == 7.5
    # Zero explicitly disables the end-to-end command bound.
    assert calls[1][1]["timeout"] is None


@pytest.mark.parametrize("value", ["soon", "-1", "0"])
def test_invalid_ssh_timeout_configuration_is_rejected_before_ssh(
    monkeypatch, tmp_path, value
):
    _root, environment = _native_install(
        tmp_path,
        III_RUNTIME_TARGET="hil",
        III_SSH_HOST="pi.example",
        III_SSH_CONNECT_TIMEOUT_SEC=value,
    )
    calls = []
    monkeypatch.setattr(
        runtime_routing.subprocess, "run", lambda command, **kwargs: calls.append(command)
    )
    stdout = _Output()
    assert main(["system", "status", "--json"], stdout=stdout, environment=environment) == 20
    assert json.loads(stdout.value)["code"] == "III_RUNTIME_ROUTE_CONFIG_INVALID"
    assert calls == []


def test_ssh_identity_timeout_is_a_distinct_rejection(monkeypatch, tmp_path):
    _root, environment = _native_install(
        tmp_path, III_RUNTIME_TARGET="hil", III_SSH_HOST="pi.example"
    )
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(runtime_routing.subprocess, "run", run)
    stdout = _Output()
    assert main(["system", "start", "--json"], stdout=stdout, environment=environment) == 20
    result = json.loads(stdout.value)
    assert result["code"] == "III_RUNTIME_ROUTE_TIMEOUT"
    message = result["findings"][0]["message"]
    assert "ssh to iii@pi.example did not complete within 30 s" in message
    assert len(calls) == 1


def test_ssh_command_timeout_is_a_distinct_failure_with_partial_output(
    monkeypatch, tmp_path
):
    _root, environment = _native_install(
        tmp_path, III_RUNTIME_TARGET="hil", III_SSH_HOST="pi.example"
    )
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if len(calls) == 1:
            return _completed(command, stdout=json.dumps({"sha256": EXPECTED_HASH}))
        raise subprocess.TimeoutExpired(
            command, kwargs["timeout"], output=b"partial status output", stderr=None
        )

    monkeypatch.setattr(runtime_routing.subprocess, "run", run)
    stdout = _Output()
    assert main(["system", "status", "--json"], stdout=stdout, environment=environment) == 30
    result = json.loads(stdout.value)
    assert result["code"] == "III_RUNTIME_ROUTE_TIMEOUT"
    assert result["payload"]["exit_status"] is None
    assert result["payload"]["stdout"] == "partial status output"
    assert result["payload"]["timeout_seconds"] == runtime_routing.DEFAULT_COMMAND_TIMEOUT_SEC
    assert "remote command may still be running" in result["findings"][0]["message"]


def test_explicit_opti_track_target_overrides_sourced_field_real_default(
    monkeypatch, tmp_path
):
    _root, environment = _native_install(
        tmp_path,
        profile="deploy",
        III_RUNTIME_TARGET="real",
        III_SYSTEM_PROFILE="real",
        III_DEFAULT_TARGET="real",
        III_SSH_HOST="pi.example",
    )
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            return _completed(command, stdout=json.dumps({"sha256": EXPECTED_HASH}))
        return _completed(command, stdout='{"code":"III_SYSTEM_STATUS_COMPLETED"}')

    monkeypatch.setattr(runtime_routing.subprocess, "run", run)
    stdout = _Output()
    assert (
        main(
            ["--runtime-target", "opti_track", "system", "status", "--json"],
            stdout=stdout,
            environment=environment,
        )
        == 0
    )
    result = json.loads(stdout.value)
    assert result["context"]["profile"] == "opti_track"
    assert "III_SYSTEM_PROFILE=opti_track" in calls[-1][-1]


def test_mutating_ssh_forwards_quoted_arguments_and_nested_result(
    monkeypatch, tmp_path
):
    _root, environment = _native_install(
        tmp_path, III_RUNTIME_TARGET="hil", III_SSH_HOST="pi.example"
    )
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if len(calls) == 1:
            return _completed(command, stdout=json.dumps({"sha256": EXPECTED_HASH}))
        return _completed(
            command,
            stdout=json.dumps(
                {"schema": "iii.command-result/v1", "code": "III_MISSION_CATALOG_SHOW"}
            ),
        )

    monkeypatch.setattr(runtime_routing.subprocess, "run", run)
    stdout = _Output()
    assert (
        main(
            [
                "mission",
                "show",
                "catalog with spaces",
                "--runtime-target=hil",
                "--json",
            ],
            stdout=stdout,
            environment=environment,
        )
        == 0
    )
    remote_command = calls[1][0][-1]
    assert "'catalog with spaces'" in remote_command
    assert "CLI_CONFIGURATION=dev" in remote_command
    assert "III_SYSTEM_PROFILE=hil" in remote_command
    assert "--runtime-target" not in remote_command
    result = json.loads(stdout.value)
    assert result["payload"]["runtime_result"]["code"] == "III_MISSION_CATALOG_SHOW"


def test_routed_human_stdout_and_stderr_reach_caller_streams(monkeypatch, tmp_path):
    _root, environment = _native_install(
        tmp_path, III_RUNTIME_TARGET="hil", III_SSH_HOST="pi.example"
    )
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if len(calls) == 1:
            return _completed(command, stdout=json.dumps({"sha256": EXPECTED_HASH}))
        return _completed(
            command, stdout="runtime status line\n", stderr="runtime diagnostic\n"
        )

    monkeypatch.setattr(runtime_routing.subprocess, "run", run)
    stdout, stderr = _Output(), _Output()
    assert (
        main(
            ["system", "status"],
            stdout=stdout,
            stderr=stderr,
            environment=environment,
        )
        == 0
    )
    assert "runtime status line" in stdout.value
    assert "Runtime command completed" in stdout.value
    assert stderr.value == "runtime diagnostic\n"


def test_child_exit_status_is_retained_under_stable_universal_failure_code(
    monkeypatch, tmp_path
):
    _root, environment = _native_install(
        tmp_path, III_RUNTIME_TARGET="hil", III_SSH_HOST="pi.example"
    )
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if len(calls) == 1:
            return _completed(command, stdout=json.dumps({"sha256": EXPECTED_HASH}))
        return _completed(
            command, code=17, stdout='{"detail":"failed"}', stderr="failed\n"
        )

    monkeypatch.setattr(runtime_routing.subprocess, "run", run)
    stdout, stderr = _Output(), _Output()
    assert (
        main(
            ["system", "status", "--json"],
            stdout=stdout,
            stderr=stderr,
            environment=environment,
        )
        == 30
    )
    result = json.loads(stdout.value)
    assert result["exit_code"] == 30
    assert result["payload"]["exit_status"] == 17
    assert result["payload"]["stderr"] == "failed\n"
    assert stderr.value == "failed\n"


def test_native_api_and_rosbag_commands_route_to_selected_runtime(
    monkeypatch, tmp_path
):
    _root, environment = _native_install(
        tmp_path, III_RUNTIME_TARGET="hil", III_SSH_HOST="pi.example"
    )
    for command_args, expected_fragment in (
        (["api", "status", "--json"], "api status --json"),
        (["rosbag", "list", "--json"], "rosbag list --json"),
    ):
        calls = []

        def run(command, **kwargs):
            calls.append((command, kwargs))
            if len(calls) == 1:
                return _completed(command, stdout=json.dumps({"sha256": EXPECTED_HASH}))
            return _completed(command, stdout='{"code":"III_LOCAL_RESULT"}')

        monkeypatch.setattr(runtime_routing.subprocess, "run", run)
        stdout = _Output()
        assert main(command_args, stdout=stdout, environment=environment) == 0
        result = json.loads(stdout.value)
        assert result["payload"]["runtime_result"]["code"] == "III_LOCAL_RESULT"
        remote = calls[1][0][-1]
        assert "source /home/iii/ws/setup/setup_hil.bash" in remote
        assert f"/home/iii/ws/tools/III-Drone-CLI/bin/iii {expected_fragment}" in remote


def test_api_logs_follow_streams_over_ssh_with_tty_and_raw_status(
    monkeypatch, tmp_path
):
    _root, environment = _native_install(
        tmp_path, III_RUNTIME_TARGET="hil", III_SSH_HOST="pi.example"
    )
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if len(calls) == 1:
            return _completed(command, stdout=json.dumps({"sha256": EXPECTED_HASH}))
        return _completed(command, code=7)

    monkeypatch.setattr(runtime_routing.subprocess, "run", run)
    assert (
        main(
            ["api", "logs", "--follow"],
            stdin=_TTYInput(),
            environment=environment,
        )
        == 7
    )
    command, options = calls[1]
    assert command[1] == "-tt"
    assert "capture_output" not in options
    assert "api logs --follow" in command[-1]


def test_api_logs_follow_streams_over_container_without_output_capture(
    monkeypatch, tmp_path
):
    _root, environment = _native_install(tmp_path, III_RUNTIME_TARGET="sim")
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if command[:2] == ["docker", "ps"]:
            return _completed(command, stdout="dev-123\n")
        if "python3" in command and "-c" in command:
            return _completed(command, stdout=json.dumps({"sha256": EXPECTED_HASH}))
        return _completed(command, code=9)

    monkeypatch.setattr(runtime_routing.subprocess, "run", run)
    assert (
        main(
            ["api", "logs", "--follow"],
            stdin=_TTYInput(),
            environment=environment,
        )
        == 9
    )
    command, options = calls[-1]
    assert command[:4] == ["docker", "exec", "-i", "-t"]
    assert "capture_output" not in options
    assert "source /home/iii/ws/setup/setup_dev.bash" in command[-1]
    assert "api logs --follow" in command[-1]


def test_cli_path_keeps_gc_wrapper_on_host_and_checkout_on_pi(tmp_path):
    workspace = Path(__file__).resolve().parents[3]
    local_bin = tmp_path / ".local" / "bin"
    local_bin.mkdir(parents=True)
    cli = local_bin / "iii"
    cli.write_text("#!/bin/sh\n# managed by III GC installer\n", encoding="utf-8")
    cli.chmod(0o755)
    profile = workspace / "setup/setup_field.bash"
    command = f"source {shlex.quote(str(profile))}; command -v iii"
    environment = {
        **os.environ,
        "HOME": str(tmp_path),
        "PATH": f"{local_bin}:{os.environ['PATH']}",
    }
    native = subprocess.run(
        ["bash", "-c", command],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert native.stdout.strip() == str(cli)

    cli.write_text(
        '#!/bin/sh\nexec /workspace/tools/III-Drone-CLI/bin/iii "$@"\n',
        encoding="utf-8",
    )
    pi = subprocess.run(
        ["bash", "-c", command],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert pi.stdout.strip() == str(workspace / "tools/III-Drone-CLI/bin/iii")


def test_attach_uses_tty_route_and_propagates_exit_status(monkeypatch, tmp_path):
    _root, environment = _native_install(
        tmp_path, III_RUNTIME_TARGET="hil", III_SSH_HOST="pi.example"
    )
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if len(calls) == 1:
            return _completed(command, stdout=json.dumps({"sha256": EXPECTED_HASH}))
        return _completed(command, code=7)

    monkeypatch.setattr(runtime_routing.subprocess, "run", run)
    assert (
        main(
            ["system", "attach", "--runtime-target", "hil"],
            stdin=_TTYInput(),
            environment=environment,
        )
        == 7
    )
    command, options = calls[1]
    assert command[1] == "-tt"
    assert "capture_output" not in options
    assert "timeout" not in options
    assert _ssh_option_values(command)["ConnectTimeout"] == "10"
    assert _ssh_option_values(command)["ServerAliveInterval"] == "5"


def test_onboard_profiles_select_supported_local_cli_mode(tmp_path):
    workspace = Path(__file__).resolve().parents[3]
    fake_ros = tmp_path / "ros"
    fake_ros.mkdir()
    (fake_ros / "setup.bash").write_text(":\n", encoding="utf-8")
    fake_hil_runtime_env = tmp_path / "hil-runtime.env"
    fake_hil_runtime_env.write_text("III_SYSTEM_PROFILE=hil\n", encoding="utf-8")

    cases = (
        (
            "setup_hil.bash",
            {
                "III_HIL_ONBOARD_RUNTIME_ENV": str(fake_hil_runtime_env),
                "III_ROS_PREFIX": str(fake_ros),
                "III_RELEASE_ROOT": str(tmp_path / "missing-release"),
            },
            "dev|hil|hil|iii.local",
        ),
        (
            "setup_real.bash",
            {
                "III_ROS_PREFIX": str(fake_ros),
                "III_RELEASE_ROOT": str(tmp_path / "missing-release"),
            },
            "dev|real|real|",
        ),
        (
            "setup_opti_track.bash",
            {
                "III_ROS_PREFIX": str(fake_ros),
                "III_RELEASE_ROOT": str(tmp_path / "missing-release"),
            },
            "dev|opti_track|opti_track|",
        ),
    )
    for filename, extra_env, expected in cases:
        profile = workspace / "setup" / filename
        script = (
            f"source {shlex.quote(str(profile))}; "
            "printf '%s|%s|%s|%s' \"$CLI_CONFIGURATION\" "
            '"$III_SYSTEM_PROFILE" "$III_RUNTIME_TARGET" "${III_SSH_HOST:-}"'
        )
        shell_env = dict(os.environ)
        for key in (
            "III_SSH_HOST",
            "III_RUNTIME_HOST",
            "III_RUNTIME_API_HOST",
            "III_HIL_PI_ADDRESS",
            "III_RUNTIME_API_URL",
            "III_RUNTIME_TARGET",
            "III_SYSTEM_PROFILE",
            "III_ENVIRONMENT_PROFILE",
            "CLI_CONFIGURATION",
        ):
            shell_env.pop(key, None)
        completed = subprocess.run(
            ["bash", "-c", script],
            check=False,
            capture_output=True,
            text=True,
            env={**shell_env, **extra_env},
        )
        assert completed.returncode == 0, completed.stderr
        assert completed.stdout == expected


def test_local_cli_without_native_install_keeps_legacy_dispatch():
    assert (
        runtime_routing.prepare_route(
            ("system", "status"),
            argparse_namespace(runtime_target="hil"),
            {"CLI_CONFIGURATION": "remote"},
        )
        is None
    )


def argparse_namespace(**values):
    import argparse

    return argparse.Namespace(**values)
