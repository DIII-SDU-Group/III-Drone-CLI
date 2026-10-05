"""Read-only PX4 link inspection for the editable developer host."""

from __future__ import annotations

import argparse
import re
import shlex
import subprocess
from typing import Any

from .result import CommandResult, Finding, NextAction, Outcome


# Pi-side (uXRCE-DDS agent, MAVLink) UDP ports per provisioned profile; they
# match deployment/ansible/vars and the PX4 NSH baselines in deployment/px4.
_PROFILE_PORTS = {
    "real": (8888, 14540),
    "opti_track": (8888, 14540),
    "hil": (8889, 14542),
}

_RUNTIME_ENV = "/etc/iii/runtime.env"
_WORKSPACE = "/home/iii/ws"

# DDS evidence per profile. HIL keeps its physical-transport setpoint proof.
# Aircraft profiles source their onboard setup profile, which adopts the
# provisioned stack domain from /etc/iii/runtime.env, so a status sample proves
# that PX4's UXRCE_DDS_DOM_ID matches the stack's ROS_DOMAIN_ID.
_AIRCRAFT_DDS_TOPIC = "/fmu/out/vehicle_status_v1"
_AIRCRAFT_DDS_TYPE = "px4_msgs/msg/VehicleStatus"


def _listener_present(output: str, port: int) -> bool:
    return bool(re.search(rf":{port}\b", output))


def _peer_reachable(output: str) -> bool:
    return bool(
        re.search(r"\b1 (?:packets? )?received\b", output)
        and "0% packet loss" in output
    )


def _px4_traffic_present(output: str, ports: tuple[int, int]) -> bool:
    port_expression = "|".join(str(port) for port in ports)
    return bool(
        re.search(
            rf"\b10\.41\.10\.2\.\d+ > 10\.41\.10\.1\.(?:{port_expression}):",
            output,
        )
    )


def _hil_setpoint_present(output: str) -> bool:
    return "III_HIL_DDS_SETPOINT_OBSERVED" in output


def _aircraft_status_present(output: str) -> bool:
    return "III_PX4_DDS_STATUS_OBSERVED" in output


def _marker(output: str, name: str) -> str | None:
    match = re.search(rf"^{name}=(.*)$", output, re.MULTILINE)
    return match.group(1).strip() if match else None


def _remote_script(requested: str | None) -> str:
    """One read-only SSH script; the profile defaults to the Pi's provisioned one."""

    port_cases = " ".join(
        f"{profile}) dds={dds}; mavlink={mavlink} ;;"
        for profile, (dds, mavlink) in _PROFILE_PORTS.items()
    )
    return (
        f"provisioned=$(sed -n 's/^III_SYSTEM_PROFILE=//p' {_RUNTIME_ENV} 2>/dev/null | head -n 1); "
        'echo "III_PX4_PROVISIONED_PROFILE=${provisioned}"; '
        f"profile={shlex.quote(requested or '')}; "
        'profile="${profile:-$provisioned}"; '
        'echo "III_PX4_INSPECTED_PROFILE=${profile}"; '
        f"case \"$profile\" in {port_cases} "
        "*) echo III_PX4_PROFILE_UNKNOWN; exit 0 ;; esac; "
        "ip -brief address show eth0; "
        "ip route get 10.41.10.2; "
        "ping -c 1 -W 2 10.41.10.2; "
        "ss -Hlun; "
        "sudo -n timeout 4 tcpdump -ni eth0 -c 1 "
        '"udp and (port $dds or port $mavlink)" 2>&1 || true; '
        'if [ "$profile" = hil ]; then '
        "source /opt/ros/jazzy/setup.bash; "
        f"source {_WORKSPACE}/install/setup.bash; "
        "if timeout 5 ros2 topic echo --once "
        "/fmu/out/vehicle_local_position_setpoint >/dev/null 2>&1; then "
        "echo III_HIL_DDS_SETPOINT_OBSERVED; "
        "else echo III_HIL_DDS_SETPOINT_MISSING; fi; "
        "else "
        f'source "{_WORKSPACE}/setup/setup_${{profile}}.bash" >/dev/null 2>&1; '
        'echo "III_PX4_DDS_DOMAIN=${ROS_DOMAIN_ID:-}"; '
        f"if timeout 8 ros2 topic echo --once {_AIRCRAFT_DDS_TOPIC} "
        f"{_AIRCRAFT_DDS_TYPE} >/dev/null 2>&1; then "
        "echo III_PX4_DDS_STATUS_OBSERVED; "
        "else echo III_PX4_DDS_STATUS_MISSING; fi; "
        "fi"
    )


def inspect(args: argparse.Namespace) -> CommandResult:
    target = f"{args.user}@{args.host}"
    requested = getattr(args, "profile", None)
    command = ["ssh", target, _remote_script(requested)]
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    failed = completed.returncode != 0
    output = completed.stdout
    provisioned = _marker(output, "III_PX4_PROVISIONED_PROFILE") or None
    profile = requested or provisioned
    ports = _PROFILE_PORTS.get(profile) if profile else None
    dds_domain = _marker(output, "III_PX4_DDS_DOMAIN") or None
    findings: tuple[Finding, ...] = ()
    if failed:
        findings = (Finding("III_PX4_LINK_INSPECTION_FAILED", completed.stderr or "ssh failed"),)
    elif ports is None:
        findings = (
            Finding(
                "III_PX4_PROFILE_UNKNOWN",
                f"{_RUNTIME_ENV} on the Pi names no PX4 transport profile "
                f"(found {provisioned!r}); provision the Pi or pass "
                "--profile real|opti_track|hil.",
                severity="warning",
                field="profile",
            ),
        )
    else:
        dds_port, mavlink_port = ports
        checks = [
            ("III_PX4_ETHERNET_ADDRESS_MISSING", "10.41.10.1/" in output,
             "eth0 does not have the expected Pi-side PX4 address 10.41.10.1/24."),
            ("III_PX4_ROUTE_MISSING", "dev eth0" in output,
             "the route to the PX4 peer 10.41.10.2 is not through eth0."),
            ("III_PX4_PEER_UNREACHABLE", _peer_reachable(output),
             "the PX4 peer 10.41.10.2 did not answer an ICMP probe through eth0."),
            ("III_PX4_DDS_LISTENER_MISSING", _listener_present(output, dds_port),
             f"no UDP listener is present on the {profile} DDS port {dds_port}."),
            ("III_PX4_MAVLINK_LISTENER_MISSING", _listener_present(output, mavlink_port),
             f"no UDP listener is present on the {profile} MAVLink port {mavlink_port}."),
            ("III_PX4_TRAFFIC_MISSING", _px4_traffic_present(output, ports),
             "no PX4-originated DDS or MAVLink UDP packet was captured on eth0."),
        ]
        if profile == "hil":
            checks.append((
                "III_HIL_DDS_SETPOINT_MISSING",
                _hil_setpoint_present(output),
                "no /fmu/out/vehicle_local_position_setpoint message arrived through DDS.",
            ))
        else:
            checks.append((
                "III_PX4_DDS_STATUS_MISSING",
                _aircraft_status_present(output),
                f"no {_AIRCRAFT_DDS_TOPIC} message arrived through DDS in ROS domain "
                f"{dds_domain or 'unknown'}; PX4's UXRCE_DDS_DOM_ID must equal the "
                "provisioned ROS_DOMAIN_ID and the micro_ros_agent must be running.",
            ))
        findings = tuple(
            Finding(code, message, severity="warning")
            for code, present, message in checks
            if not present
        )
        if provisioned and requested and provisioned != requested:
            findings += (
                Finding(
                    "III_PX4_PROFILE_MISMATCH",
                    f"the Pi is provisioned for {provisioned}, but {requested} was inspected.",
                    severity="warning",
                    field="profile",
                ),
            )
    incomplete = not failed and bool(findings)
    label = profile or "unknown"
    return CommandResult(
        command="iii px4 inspect",
        outcome=Outcome.FAILED if failed else Outcome.WARNING if incomplete else Outcome.SUCCESS,
        summary=(
            f"PX4 {label} Ethernet, UDP listener, and DDS inspection completed."
            if not failed and not incomplete
            else f"PX4 {label} link is reachable but not ready for {label} PX4 traffic."
            if incomplete
            else "PX4 inspection could not query the Pi."
        ),
        code=(
            "III_PX4_LINK_INSPECTION_FAILED"
            if failed
            else "III_PX4_LINK_INCOMPLETE"
            if incomplete
            else "III_PX4_LINK_INSPECTED"
        ),
        target=target,
        profile=profile,
        findings=findings,
        payload_schema="iii.px4-link-inspection/v1",
        payload={
            "command": command,
            "profile": profile,
            "provisioned_profile": provisioned,
            "expected_ports": (
                {"dds": ports[0], "mavlink": ports[1]} if ports is not None else None
            ),
            "dds_domain": dds_domain,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        },
        next_actions=(
            NextAction(("iii", "system", "status"), "Inspect the supervised runtime before any flight test."),
        ),
    )


def initialize(parser: argparse.ArgumentParser) -> None:
    subparsers = parser.add_subparsers(dest="px4_command")
    inspect_parser = subparsers.add_parser(
        "inspect", help="read the Pi-side PX4 Ethernet, UDP, and DDS link state"
    )
    inspect_parser.add_argument("--host", default="iii.local")
    inspect_parser.add_argument("--user", default="iii")
    inspect_parser.add_argument(
        "--profile",
        choices=tuple(_PROFILE_PORTS),
        default=None,
        help=(
            "PX4 transport profile: real/opti_track use DDS UDP 8888 and MAVLink "
            "14540, hil uses 8889 and 14542 (default: the profile provisioned "
            "in /etc/iii/runtime.env on the Pi)"
        ),
    )
    inspect_parser.set_defaults(func=inspect, _iii_mutating=False)
