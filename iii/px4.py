"""Read-only PX4 link inspection for the editable developer host."""

from __future__ import annotations

import argparse
import re
import subprocess
from typing import Any

from .result import CommandResult, Finding, NextAction, Outcome


_PROFILE_PORTS = {
    "real": (8888, 14540),
    "opti_track": (8888, 14540),
    "hil": (8889, 14542),
}


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


def inspect(args: argparse.Namespace) -> CommandResult:
    target = f"{args.user}@{args.host}"
    profile = args.profile
    dds_port, mavlink_port = _PROFILE_PORTS[profile]
    command = [
        "ssh",
        target,
        (
            "ip -brief address show eth0; "
            "ip route get 10.41.10.2; "
            "ping -c 1 -W 2 10.41.10.2; "
            "ss -Hlun; "
            "sudo -n timeout 4 tcpdump -ni eth0 -c 1 "
            f"'udp and (port {dds_port} or port {mavlink_port})' 2>&1 || true"
        ),
    ]
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    failed = completed.returncode != 0
    checks = (
        ("III_PX4_ETHERNET_ADDRESS_MISSING", "10.41.10.1/" in completed.stdout,
         "eth0 does not have the expected Pi-side PX4 address 10.41.10.1/24."),
        ("III_PX4_ROUTE_MISSING", "dev eth0" in completed.stdout,
         "the route to the PX4 peer 10.41.10.2 is not through eth0."),
        ("III_PX4_PEER_UNREACHABLE", _peer_reachable(completed.stdout),
         "the PX4 peer 10.41.10.2 did not answer an ICMP probe through eth0."),
        ("III_PX4_DDS_LISTENER_MISSING", _listener_present(completed.stdout, dds_port),
         f"no UDP listener is present on the {profile} DDS port {dds_port}."),
        ("III_PX4_MAVLINK_LISTENER_MISSING", _listener_present(completed.stdout, mavlink_port),
         f"no UDP listener is present on the {profile} MAVLink port {mavlink_port}."),
        ("III_PX4_TRAFFIC_MISSING", _px4_traffic_present(completed.stdout, (dds_port, mavlink_port)),
         "no PX4-originated DDS or MAVLink UDP packet was captured on eth0."),
    )
    missing = tuple(
        Finding(code, message, severity="warning")
        for code, present, message in checks
        if not present
    )
    incomplete = not failed and bool(missing)
    return CommandResult(
        command="iii px4 inspect",
        outcome=Outcome.FAILED if failed else Outcome.WARNING if incomplete else Outcome.SUCCESS,
        summary=(
            f"PX4 {profile} Ethernet and UDP listener inspection completed."
            if not failed and not incomplete
            else f"PX4 {profile} link is reachable but not ready for HIL traffic."
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
        findings=(Finding("III_PX4_LINK_INSPECTION_FAILED", completed.stderr),)
        if failed
        else missing,
        payload_schema="iii.px4-link-inspection/v1",
        payload={
            "command": command,
            "profile": profile,
            "expected_ports": {"dds": dds_port, "mavlink": mavlink_port},
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
        "inspect", help="read the Pi-side PX4 Ethernet and UDP-link state"
    )
    inspect_parser.add_argument("--host", default="iii.local")
    inspect_parser.add_argument("--user", default="iii")
    inspect_parser.add_argument(
        "--profile", choices=tuple(_PROFILE_PORTS), default="hil", help="PX4 transport profile"
    )
    inspect_parser.set_defaults(func=inspect, _iii_mutating=False)
