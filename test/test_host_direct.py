from __future__ import annotations

from io import StringIO
import json
from types import SimpleNamespace

from iii.__main__ import main
from iii.host import _developer_first_boot_network, _developer_first_boot_user_data
from iii import px4
from iii.result import Outcome


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
    assert main(["host", "provision", "--host", "pi.local", "--json"], stdout=output) == 0
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
