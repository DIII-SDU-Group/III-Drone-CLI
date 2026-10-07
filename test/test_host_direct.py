from __future__ import annotations

from io import StringIO
import json
import os
from pathlib import Path
import stat
import subprocess
from types import SimpleNamespace

import pytest

from iii.__main__ import main
from iii.host import _developer_first_boot_network, _developer_first_boot_user_data
from iii import px4
from iii.result import Outcome


# Test-only Wi-Fi values; never a real network's secret.
TEST_SSID = "Example Lab 5G"
TEST_PASSPHRASE = 'not-a-real "secret" #1'


def _provision_workspace(monkeypatch, tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    (workspace / "deployment/ansible/playbooks").mkdir(parents=True)
    (workspace / "deployment/ansible/playbooks/aircraft-converge.yml").write_text(
        "---\n- hosts: all\n", encoding="utf-8"
    )
    (workspace / "deployment/ansible/ansible.cfg").write_text("[defaults]\n", encoding="utf-8")
    monkeypatch.chdir(workspace)
    return workspace


def _passphrase_file(directory: Path, text: str = TEST_PASSPHRASE + "\n", mode: int = 0o600) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "wifi.psk"
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)
    return path


def _provision(argv, monkeypatch, *, which="/usr/bin/ansible-playbook", run=None):
    monkeypatch.setattr("iii.host.shutil.which", lambda _: which)
    if run is not None:
        monkeypatch.setattr("iii.host.subprocess.run", run)
    output = StringIO()
    status = main(["host", "provision", "--host", "pi.local", *argv, "--json"], stdout=output)
    return status, json.loads(output.getvalue()), output.getvalue()


class _Completed:
    returncode = 0
    stdout = "ok"
    stderr = ""


def test_host_provision_dry_run_needs_no_local_ansible(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / "deployment/ansible/playbooks").mkdir(parents=True)
    (workspace / "deployment/ansible/playbooks/aircraft-converge.yml").write_text(
        "---\n- hosts: all\n", encoding="utf-8"
    )
    (workspace / "deployment/ansible/ansible.cfg").write_text("[defaults]\n", encoding="utf-8")
    monkeypatch.chdir(workspace)
    monkeypatch.setattr("iii.host.shutil.which", lambda _: None)
    output = StringIO()
    assert main(["host", "provision", "--host", "pi.local", "--dry-run", "--json"], stdout=output) == 0
    result = json.loads(output.getvalue())
    assert result["code"] == "III_DEVELOPER_HOST_PREVIEW"
    assert result["payload"]["command"]["command"][0] == "ansible-playbook"


def test_host_provision_selects_the_stack_ros_domain(monkeypatch, tmp_path):
    _provision_workspace(monkeypatch, tmp_path)
    status, result, _ = _provision(["--profile", "opti_track", "--dry-run"], monkeypatch, which=None)
    assert status == 0
    command = result["payload"]["command"]["command"]
    assert command[-4:] == ["-e", "iii_profile=opti_track", "-e", "iii_ros_domain_id=42"]
    status, result, _ = _provision(
        ["--profile", "hil", "--ros-domain-id", "57", "--dry-run"], monkeypatch, which=None
    )
    assert status == 0
    assert "iii_ros_domain_id=57" in result["payload"]["command"]["command"]


@pytest.mark.parametrize("domain", ["102", "-1", "forty-two"])
def test_host_provision_rejects_an_unportable_ros_domain(monkeypatch, tmp_path, domain):
    _provision_workspace(monkeypatch, tmp_path)
    status, result, _ = _provision(["--ros-domain-id", domain, "--dry-run"], monkeypatch, which=None)
    assert status == 64
    assert result["code"] == "III_USAGE_ERROR"


def test_opti_track_stack_domain_must_differ_from_the_lab_domain(monkeypatch, tmp_path):
    _provision_workspace(monkeypatch, tmp_path)
    status, result, _ = _provision(
        ["--profile", "opti_track", "--ros-domain-id", "0", "--dry-run"], monkeypatch, which=None
    )
    assert status == 64
    assert result["code"] == "III_DEVELOPER_HOST_ROS_DOMAIN_INVALID"
    # HIL and real may use domain 0.
    status, _result, _ = _provision(
        ["--profile", "real", "--ros-domain-id", "0", "--dry-run"], monkeypatch, which=None
    )
    assert status == 0


def test_wifi_provisioning_passes_the_secret_only_through_a_private_file(monkeypatch, tmp_path):
    _provision_workspace(monkeypatch, tmp_path)
    psk_file = _passphrase_file(tmp_path / "private")
    observed = {}

    def fake_run(command, **kwargs):
        observed["command"] = list(command)
        variables = Path(command[-1].removeprefix("@"))
        observed["path"] = variables
        observed["mode"] = stat.S_IMODE(variables.stat().st_mode)
        observed["directory_mode"] = stat.S_IMODE(variables.parent.stat().st_mode)
        observed["variables"] = json.loads(variables.read_text(encoding="utf-8"))
        return _Completed()

    status, result, raw = _provision(
        [
            "--profile",
            "opti_track",
            "--wifi-ssid",
            TEST_SSID,
            "--wifi-psk-file",
            str(psk_file),
            "--wifi-country",
            "dk",
        ],
        monkeypatch,
        run=fake_run,
    )
    assert status == 0, raw
    assert observed["command"][-2] == "-e"
    assert observed["command"][-1].startswith("@")
    assert observed["mode"] == 0o600
    assert observed["directory_mode"] & 0o077 == 0
    assert observed["variables"] == {
        "iii_wifi_ssid": TEST_SSID,
        "iii_wifi_psk": TEST_PASSPHRASE,
        "iii_wifi_regulatory_domain": "DK",
    }
    # The file is gone after the run, and no output or argv carries the secret.
    assert not observed["path"].exists()
    assert TEST_PASSPHRASE not in raw
    assert all(TEST_PASSPHRASE not in part for part in observed["command"])


def test_wifi_provisioning_preview_never_reads_or_writes_a_prompted_secret(monkeypatch, tmp_path):
    _provision_workspace(monkeypatch, tmp_path)

    def no_prompt(*_args, **_kwargs):
        raise AssertionError("a preview must not prompt for the Wi-Fi passphrase")

    monkeypatch.setattr("iii.host.getpass.getpass", no_prompt)
    status, result, _ = _provision(
        ["--profile", "opti_track", "--wifi-ssid", TEST_SSID, "--dry-run"], monkeypatch, which=None
    )
    assert status == 0
    assert result["code"] == "III_DEVELOPER_HOST_PREVIEW"
    assert result["payload"]["command"]["command"][-2:] == [
        "-e",
        "@<private 0600 Wi-Fi variables file>",
    ]


def test_wifi_provisioning_prompts_for_the_passphrase_without_a_file(monkeypatch, tmp_path):
    _provision_workspace(monkeypatch, tmp_path)
    prompts = []

    def prompt(text):
        prompts.append(text)
        return TEST_PASSPHRASE

    observed = {}

    def fake_run(command, **kwargs):
        observed["variables"] = json.loads(Path(command[-1][1:]).read_text(encoding="utf-8"))
        return _Completed()

    monkeypatch.setattr("iii.host.getpass.getpass", prompt)
    status, _result, raw = _provision(["--wifi-ssid", TEST_SSID], monkeypatch, run=fake_run)
    assert status == 0, raw
    assert prompts == [f"Wi-Fi passphrase for {TEST_SSID!r}: "]
    assert observed["variables"]["iii_wifi_psk"] == TEST_PASSPHRASE
    assert observed["variables"]["iii_wifi_regulatory_domain"] == ""


def test_non_interactive_wifi_provisioning_requires_a_passphrase_file(monkeypatch, tmp_path):
    _provision_workspace(monkeypatch, tmp_path)

    def must_not_run(*_args, **_kwargs):
        raise AssertionError("provisioning must not start without the passphrase")

    status, result, _ = _provision(
        ["--wifi-ssid", TEST_SSID, "--non-interactive"], monkeypatch, run=must_not_run
    )
    assert status == 20
    assert result["code"] == "III_REQUIRED_INPUT"


@pytest.mark.parametrize(
    "case",
    ["inside-checkout", "group-readable", "short", "missing", "orphan-file", "orphan-country", "bad-country"],
)
def test_wifi_provisioning_rejects_unsafe_or_invalid_input(monkeypatch, tmp_path, case):
    workspace = _provision_workspace(monkeypatch, tmp_path)
    arguments = ["--wifi-ssid", TEST_SSID]
    if case == "inside-checkout":
        arguments += ["--wifi-psk-file", str(_passphrase_file(workspace / "secrets"))]
    elif case == "group-readable":
        arguments += ["--wifi-psk-file", str(_passphrase_file(tmp_path / "private", mode=0o640))]
    elif case == "short":
        arguments += ["--wifi-psk-file", str(_passphrase_file(tmp_path / "private", "short\n"))]
    elif case == "missing":
        arguments += ["--wifi-psk-file", str(tmp_path / "absent.psk")]
    elif case == "orphan-file":
        arguments = ["--wifi-psk-file", str(_passphrase_file(tmp_path / "private"))]
    elif case == "orphan-country":
        arguments = ["--wifi-country", "DK"]
    else:
        arguments += ["--wifi-psk-file", str(_passphrase_file(tmp_path / "private")), "--wifi-country", "DNK"]

    def must_not_run(*_args, **_kwargs):
        raise AssertionError("invalid Wi-Fi input must stop before Ansible")

    status, result, raw = _provision(arguments, monkeypatch, run=must_not_run)
    assert status == 64
    assert result["code"] == "III_DEVELOPER_HOST_WIFI_INPUT_INVALID"
    assert TEST_PASSPHRASE not in raw


def test_wifi_provisioning_accepts_a_raw_hexadecimal_psk(monkeypatch, tmp_path):
    _provision_workspace(monkeypatch, tmp_path)
    raw_psk = "0123456789abcdef" * 4
    psk_file = _passphrase_file(tmp_path / "private", raw_psk + "\r\n")
    observed = {}

    def fake_run(command, **kwargs):
        observed["variables"] = json.loads(Path(command[-1][1:]).read_text(encoding="utf-8"))
        return _Completed()

    status, _result, raw = _provision(
        ["--wifi-ssid", TEST_SSID, "--wifi-psk-file", str(psk_file)], monkeypatch, run=fake_run
    )
    assert status == 0, raw
    assert observed["variables"]["iii_wifi_psk"] == raw_psk


def test_wifi_removal_is_explicit_and_exclusive(monkeypatch, tmp_path):
    _provision_workspace(monkeypatch, tmp_path)
    status, result, _ = _provision(
        ["--profile", "hil", "--remove-wifi", "--dry-run"], monkeypatch, which=None
    )
    assert status == 0
    assert result["payload"]["command"]["command"][-2:] == ["-e", "iii_wifi_remove=true"]
    status, result, _ = _provision(
        ["--remove-wifi", "--wifi-ssid", TEST_SSID, "--dry-run"], monkeypatch, which=None
    )
    assert status == 64
    assert result["code"] == "III_USAGE_ERROR"


@pytest.mark.parametrize("profile", ["real", "opti_track"])
def test_real_and_opti_track_cannot_drop_their_wifi_client(monkeypatch, tmp_path, profile):
    _provision_workspace(monkeypatch, tmp_path)
    status, result, _ = _provision(
        ["--profile", profile, "--remove-wifi", "--dry-run"], monkeypatch, which=None
    )
    assert status == 64
    assert result["code"] == "III_DEVELOPER_HOST_WIFI_INPUT_INVALID"


def _slot_probe_run(stdout: str, commands: list):
    class Probe:
        returncode = 0
        stderr = ""

    def fake_run(command, **_kwargs):
        commands.append(list(command))
        completed = Probe()
        completed.stdout = stdout if command[0] == "ssh" else "ok"
        return completed

    return fake_run


@pytest.mark.parametrize("profile", ["real", "opti_track"])
def test_slot_profile_without_a_stored_wifi_client_needs_an_ssid(monkeypatch, tmp_path, profile):
    _provision_workspace(monkeypatch, tmp_path)
    commands = []
    status, result, _ = _provision(
        ["--profile", profile], monkeypatch, run=_slot_probe_run("", commands)
    )
    assert status == 64
    assert result["code"] == "III_DEVELOPER_HOST_WIFI_REQUIRED"
    assert "--wifi-ssid" in result["findings"][0]["message"]
    # Only the read-only slot probe ran; Ansible did not.
    assert [command[0] for command in commands] == ["ssh"]
    assert f"/etc/iii/wifi/{profile}.yaml" in commands[0][-1]


def test_slot_profile_with_a_stored_wifi_client_provisions_without_wifi_options(monkeypatch, tmp_path):
    _provision_workspace(monkeypatch, tmp_path)
    commands = []
    status, result, _ = _provision(
        ["--profile", "opti_track"],
        monkeypatch,
        run=_slot_probe_run("III_WIFI_SLOT_PRESENT\n", commands),
    )
    assert status == 0, result
    assert [command[0] for command in commands] == ["ssh", "/usr/bin/ansible-playbook"]
    assert not any("wifi" in argument for argument in commands[1])


def test_provisioning_is_refused_when_the_vehicle_gate_fails(monkeypatch, tmp_path):
    from iii import vehicle_gate

    _provision_workspace(monkeypatch, tmp_path)
    commands = []
    monkeypatch.setattr(
        vehicle_gate,
        "evaluate",
        lambda host, user, *, force, **_kwargs: vehicle_gate.RestartGate(
            False, "real", "vehicle state unknown"
        ),
    )
    status, result, _ = _provision(
        ["--profile", "hil"], monkeypatch, run=_slot_probe_run("", commands)
    )
    assert status != 0
    assert result["code"] == "III_VEHICLE_GATE_REJECTED"
    assert "--force" in result["findings"][0]["message"]
    assert commands == []


def test_host_provision_invokes_normal_inventory_and_uses_local_config(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / "deployment/ansible/playbooks").mkdir(parents=True)
    (workspace / "deployment/ansible/playbooks/aircraft-converge.yml").write_text(
        "---\n- hosts: all\n", encoding="utf-8"
    )
    config = workspace / "deployment/ansible/ansible.cfg"
    config.write_text("[defaults]\n", encoding="utf-8")
    monkeypatch.chdir(workspace)
    monkeypatch.setattr("iii.host.shutil.which", lambda _: "/usr/bin/ansible-playbook")
    observed = {}

    class Completed:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed["environment"] = kwargs["env"]
        return Completed()

    monkeypatch.setattr("iii.host.subprocess.run", fake_run)
    output = StringIO()
    assert main(
        ["host", "provision", "--host", "pi.local", "--profile", "hil", "--json"], stdout=output
    ) == 0
    assert observed["command"][1:4] == ["-i", "pi.local,", "-u"]
    assert observed["environment"]["ANSIBLE_CONFIG"] == str(config)


def test_first_boot_seed_creates_the_online_developer_account(monkeypatch, tmp_path):
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    (ssh / "id_ed25519.pub").write_text("ssh-ed25519 AAAA developer\n", encoding="utf-8")
    monkeypatch.setattr("iii.host.Path.home", lambda: tmp_path)

    user_data = _developer_first_boot_user_data()

    assert "name: iii" in user_data
    assert "NOPASSWD:ALL" in user_data
    assert "ssh-ed25519 AAAA developer" in user_data
    network = _developer_first_boot_network()
    assert "driver: ax88179_178a" in network
    assert "10.42.0.15/24" in network
    assert "dhcp4: true" in network


def test_hil_px4_inspection_requires_hil_ports_and_reports_them(monkeypatch):
    class Completed:
        returncode = 0
        stderr = ""
        stdout = """eth0 UP 10.41.10.1/24
10.41.10.2 dev eth0 src 10.41.10.1
1 packets transmitted, 1 received, 0% packet loss
UNCONN 0 0 0.0.0.0:8889 0.0.0.0:*
UNCONN 0 0 0.0.0.0:14542 0.0.0.0:*
IP 10.41.10.2.40123 > 10.41.10.1.8889: UDP, length 64
III_HIL_DDS_SETPOINT_OBSERVED
"""

    observed = {}

    def fake_run(command, **kwargs):
        observed["command"] = command
        return Completed()

    monkeypatch.setattr(px4.subprocess, "run", fake_run)

    result = px4.inspect(SimpleNamespace(host="10.42.0.15", user="iii", profile="hil"))

    assert result.outcome is Outcome.SUCCESS
    assert result.code == "III_PX4_LINK_INSPECTED"
    assert result.payload["expected_ports"] == {"dds": 8889, "mavlink": 14542}
    assert "ros2 topic echo --once" in observed["command"][2]
    assert "grep" not in observed["command"][2]


def test_hil_px4_inspection_reports_missing_hil_listener(monkeypatch):
    class Completed:
        returncode = 0
        stderr = ""
        stdout = "eth0 UP 10.41.10.1/24\\n10.41.10.2 dev eth0 src 10.41.10.1\\n"

    monkeypatch.setattr(px4.subprocess, "run", lambda *_args, **_kwargs: Completed())

    result = px4.inspect(SimpleNamespace(host="10.42.0.15", user="iii", profile="hil"))

    assert result.outcome is Outcome.WARNING
    assert result.code == "III_PX4_LINK_INCOMPLETE"
    assert {finding.code for finding in result.findings} == {
        "III_PX4_PEER_UNREACHABLE",
        "III_PX4_DDS_LISTENER_MISSING",
        "III_PX4_MAVLINK_LISTENER_MISSING",
        "III_PX4_TRAFFIC_MISSING",
        "III_HIL_DDS_SETPOINT_MISSING",
    }


def test_hil_px4_inspection_requires_the_dds_setpoint_message(monkeypatch):
    class Completed:
        returncode = 0
        stderr = ""
        stdout = """eth0 UP 10.41.10.1/24
10.41.10.2 dev eth0 src 10.41.10.1
1 packets transmitted, 1 received, 0% packet loss
UNCONN 0 0 0.0.0.0:8889 0.0.0.0:*
UNCONN 0 0 0.0.0.0:14542 0.0.0.0:*
IP 10.41.10.2.40123 > 10.41.10.1.8889: UDP, length 64
III_HIL_DDS_SETPOINT_MISSING
"""

    monkeypatch.setattr(px4.subprocess, "run", lambda *_args, **_kwargs: Completed())

    result = px4.inspect(SimpleNamespace(host="10.42.0.15", user="iii", profile="hil"))

    assert result.outcome is Outcome.WARNING
    assert {finding.code for finding in result.findings} == {
        "III_HIL_DDS_SETPOINT_MISSING"
    }


OPTI_TRACK_LINK = """III_PX4_PROVISIONED_PROFILE=opti_track
III_PX4_INSPECTED_PROFILE=opti_track
eth0 UP 10.41.10.1/24
10.41.10.2 dev eth0 src 10.41.10.1
1 packets transmitted, 1 received, 0% packet loss
UNCONN 0 0 0.0.0.0:8888 0.0.0.0:*
UNCONN 0 0 0.0.0.0:14540 0.0.0.0:*
IP 10.41.10.2.40123 > 10.41.10.1.8888: UDP, length 64
III_PX4_DDS_DOMAIN=42
"""


def _inspect(monkeypatch, stdout, *, profile=None):
    observed = {}

    class Completed:
        returncode = 0
        stderr = ""

    Completed.stdout = stdout

    def fake_run(command, **kwargs):
        observed["command"] = command
        return Completed()

    monkeypatch.setattr(px4.subprocess, "run", fake_run)
    result = px4.inspect(SimpleNamespace(host="iii.local", user="iii", profile=profile))
    return result, observed["command"]


def test_px4_inspection_defaults_to_the_provisioned_opti_track_transport(monkeypatch):
    result, command = _inspect(monkeypatch, OPTI_TRACK_LINK + "III_PX4_DDS_STATUS_OBSERVED\n")
    assert result.outcome is Outcome.SUCCESS
    assert result.code == "III_PX4_LINK_INSPECTED"
    assert result.profile == "opti_track"
    assert result.payload["expected_ports"] == {"dds": 8888, "mavlink": 14540}
    assert result.payload["provisioned_profile"] == "opti_track"
    assert result.payload["dds_domain"] == "42"
    script = command[2]
    # The aircraft DDS proof runs in the provisioned stack domain.
    assert "/etc/iii/runtime.env" in script
    assert 'setup/setup_${profile}.bash' in script
    assert "ros2 topic echo --once /fmu/out/vehicle_status_v1 px4_msgs/msg/VehicleStatus" in script
    assert "grep" not in script


def test_opti_track_px4_inspection_requires_a_dds_status_sample(monkeypatch):
    result, _command = _inspect(
        monkeypatch, OPTI_TRACK_LINK + "III_PX4_DDS_STATUS_MISSING\n", profile="opti_track"
    )
    assert result.outcome is Outcome.WARNING
    assert result.code == "III_PX4_LINK_INCOMPLETE"
    assert {finding.code for finding in result.findings} == {"III_PX4_DDS_STATUS_MISSING"}
    message = result.findings[0].message
    assert "ROS domain 42" in message and "UXRCE_DDS_DOM_ID" in message
    assert "HIL" not in result.summary
    assert "opti_track" in result.summary


def test_px4_inspection_reports_a_profile_mismatch(monkeypatch):
    stdout = OPTI_TRACK_LINK.replace(":8888 ", ":8889 ").replace(":14540 ", ":14542 ")
    stdout = stdout.replace("10.41.10.1.8888:", "10.41.10.1.8889:")
    result, _command = _inspect(
        monkeypatch, stdout + "III_HIL_DDS_SETPOINT_OBSERVED\n", profile="hil"
    )
    assert result.outcome is Outcome.WARNING
    assert {finding.code for finding in result.findings} == {"III_PX4_PROFILE_MISMATCH"}
    assert result.payload["expected_ports"] == {"dds": 8889, "mavlink": 14542}


def test_px4_inspection_without_a_provisioned_profile_asks_for_one(monkeypatch):
    result, _command = _inspect(
        monkeypatch,
        "III_PX4_PROVISIONED_PROFILE=\nIII_PX4_INSPECTED_PROFILE=\nIII_PX4_PROFILE_UNKNOWN\n",
    )
    assert result.outcome is Outcome.WARNING
    assert {finding.code for finding in result.findings} == {"III_PX4_PROFILE_UNKNOWN"}
    assert result.payload["expected_ports"] is None


def test_px4_remote_script_runs_the_aircraft_proof_against_a_fake_pi(monkeypatch, tmp_path):
    """Execute the generated SSH script locally against stubbed Pi tools."""

    runtime_env = tmp_path / "runtime.env"
    runtime_env.write_text("III_SYSTEM_PROFILE=opti_track\nROS_DOMAIN_ID=57\n", encoding="utf-8")
    workspace = tmp_path / "ws"
    (workspace / "setup").mkdir(parents=True)
    (workspace / "setup/setup_opti_track.bash").write_text(
        "export ROS_DOMAIN_ID=57\n", encoding="utf-8"
    )
    tools = tmp_path / "bin"
    tools.mkdir()
    stubs = {
        "ip": 'case "$*" in *address*) echo "eth0 UP 10.41.10.1/24";; *) echo "10.41.10.2 dev eth0 src 10.41.10.1";; esac',
        "ping": 'echo "1 packets transmitted, 1 received, 0% packet loss"',
        "ss": 'printf "UNCONN 0 0 0.0.0.0:8888 0.0.0.0:*\\nUNCONN 0 0 0.0.0.0:14540 0.0.0.0:*\\n"',
        "sudo": 'echo "$*" > "$(dirname "$0")/tcpdump.args"; echo "IP 10.41.10.2.40123 > 10.41.10.1.8888: UDP, length 64"',
        "timeout": 'shift; exec "$@"',
        "ros2": 'echo "$ROS_DOMAIN_ID $*" > "$(dirname "$0")/ros2.args"; [ "$ROS_DOMAIN_ID" = 57 ]',
    }
    for name, body in stubs.items():
        path = tools / name
        path.write_text("#!/bin/bash\n" + body + "\n", encoding="utf-8")
        path.chmod(0o755)
    monkeypatch.setattr(px4, "_RUNTIME_ENV", str(runtime_env))
    monkeypatch.setattr(px4, "_WORKSPACE", str(workspace))
    environment = {"PATH": f"{tools}:{os.environ.get('PATH', '/usr/bin:/bin')}"}
    real_run = subprocess.run

    def run_locally(command, **kwargs):
        return real_run(
            ["bash", "-c", command[2]], env=environment, capture_output=True, text=True, check=False
        )

    monkeypatch.setattr(px4.subprocess, "run", run_locally)
    result = px4.inspect(SimpleNamespace(host="iii.local", user="iii", profile=None))
    assert result.outcome is Outcome.SUCCESS, result.payload["stdout"] + result.payload["stderr"]
    assert result.profile == "opti_track"
    assert result.payload["dds_domain"] == "57"
    assert "udp and (port 8888 or port 14540)" in (tools / "tcpdump.args").read_text(encoding="utf-8")
    assert (tools / "ros2.args").read_text(encoding="utf-8").split() == [
        "57", "topic", "echo", "--once", "/fmu/out/vehicle_status_v1", "px4_msgs/msg/VehicleStatus",
    ]
