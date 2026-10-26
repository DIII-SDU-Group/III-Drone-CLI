"""`iii px4 param-baseline`: bring the flight controller to a profile's PX4 baseline.

The baseline is the profile's parameter file in deployment/px4/parameters. The
command reads
the flight controller's parameters, shows what differs, and only then writes,
reboots the flight controller and verifies the result. It refuses unless PX4
reports the aircraft disarmed and landed.

It reaches the flight controller through the Pi when the Pi is provisioned
for the same profile and its Runtime API has a live MAVLink link. Otherwise it
falls back to the flight controller's USB port on this computer. Without
either it fails.
"""

from __future__ import annotations

import argparse
import glob
import os
from pathlib import Path
import sys
import time
from typing import Any

from .px4_mavlink import MavlinkError, Px4Link, open_transport
from .result import CommandResult, Finding, NextAction, Outcome
from .runtime_api_client import RuntimeApiClient, RuntimeApiError

# iii_drone_contracts.px4_parameters is imported where it is used: the Pi's
# CLI runs on the system Python, which cannot import the Contracts package,
# and this command runs on the ground computer only.
PROFILES = ("hil", "opti_track", "real")
SCHEMA = "iii.px4-parameter-baseline/v1"
COMMAND = "iii px4 param-baseline"
_USB_GLOB = "/dev/serial/by-id/*PX4*"
_AUTOSAVE_SETTLE_SECONDS = 3.0
_USB_RETURN_TIMEOUT_SECONDS = 60.0
# The Pi-side apply waits for the flight controller's reboot.
_APPLY_TIMEOUT_SECONDS = 240.0


def _baseline_directory() -> Path:
    from iii_drone_contracts.px4_parameters import BASELINE_DIRECTORY, BaselineError

    current = Path.cwd().resolve()
    for candidate in (current, *current.parents):
        if (candidate / BASELINE_DIRECTORY).is_dir():
            return candidate / BASELINE_DIRECTORY
    raise BaselineError(
        f"run this command from the III workspace ({BASELINE_DIRECTORY} not found)"
    )


def _api(args: argparse.Namespace, *, timeout: float) -> RuntimeApiClient:
    port = os.environ.get("III_RUNTIME_API_PORT", "8765")
    return RuntimeApiClient(base_url=f"http://{args.host}:{port}", timeout_seconds=timeout)


def _pi_state(args: argparse.Namespace) -> tuple[dict[str, Any] | None, str | None]:
    """The Pi's view of the baseline, or why the Pi cannot be used."""

    try:
        state = _api(args, timeout=20.0).px4_parameter_baseline()
    except RuntimeApiError as exc:
        return None, str(exc)
    if state.get("profile") != args.profile:
        return state, (
            f"the Pi is provisioned for {state.get('profile')}, not {args.profile}"
        )
    if not state.get("applicable"):
        return state, f"the Pi does not check a flight controller: {state.get('detail')}"
    if not state.get("checked"):
        return state, str(state.get("detail") or "the Pi has no MAVLink link to PX4")
    return state, None


def _usb_device(args: argparse.Namespace) -> str | None:
    if args.usb_device:
        return args.usb_device
    devices = sorted(glob.glob(_USB_GLOB))
    return devices[0] if devices else None


def _usb_baseline(args: argparse.Namespace, pi_state: dict[str, Any] | None) -> dict[str, Any]:
    from iii_drone_contracts.px4_parameters import load_baseline

    domain = args.ros_domain_id
    if domain is None and pi_state is not None and pi_state.get("profile") == args.profile:
        domain = pi_state.get("ros_domain_id")
    return load_baseline(args.profile, _baseline_directory(), ros_domain_id=domain)


def _connect_usb(device: str) -> Px4Link:
    link = Px4Link(open_transport(device))
    try:
        link.connect()
    except MavlinkError:
        link.close()
        raise
    return link


def _vehicle_refusal(armed: bool | None, landed: bool | None) -> str | None:
    if armed is None or landed is None:
        return "PX4 did not report its armed and landed state"
    if armed:
        return "vehicle is armed"
    if not landed:
        return "vehicle is not landed"
    return None


def _show(differences: list[dict[str, Any]], path: str) -> None:
    """Show the diff before the runner asks for confirmation."""

    if not differences:
        print(f"The flight controller already matches the baseline (read through {path}).",
              file=sys.stderr, flush=True)
        return
    width = max(len(str(item["name"])) for item in differences)
    lines = [f"PX4 parameters to change (through {path}):"]
    lines += [
        f"  {str(item['name']):<{width}}  "
        f"{'unreadable' if item['actual'] is None else item['actual']} -> {item['expected']}"
        for item in differences
    ]
    print("\n".join(lines), file=sys.stderr, flush=True)


def plan(args: argparse.Namespace) -> dict[str, Any]:
    """Choose the path to the flight controller and read what would change."""

    from iii_drone_contracts.px4_parameters import mismatches

    pi_state, pi_problem = _pi_state(args)
    if pi_problem is None and pi_state is not None:
        differences = list(pi_state.get("mismatches") or [])
        _show(differences, f"the Pi at {args.host}")
        return {
            "schema": SCHEMA,
            "profile": args.profile,
            "path": "pi",
            "host": args.host,
            "changes": differences,
            "gate": "the Runtime API must show the aircraft disarmed and landed",
        }
    device = _usb_device(args)
    if device is None:
        raise RuntimeError(
            f"no path to the flight controller. Pi: {pi_problem}. "
            f"USB: no device matches {_USB_GLOB}; connect the flight controller's "
            "USB port to this computer or pass --usb-device"
        )
    try:
        link = _connect_usb(device)
    except MavlinkError as exc:
        raise RuntimeError(
            f"no path to the flight controller. Pi: {pi_problem}. USB {device}: {exc}"
        ) from None
    try:
        baseline = _usb_baseline(args, pi_state)
        armed, landed = link.vehicle_state()
        differences = mismatches(baseline, link.read_parameters(list(baseline)))
    finally:
        link.close()
    _show(differences, f"USB {device}")
    return {
        "schema": SCHEMA,
        "profile": args.profile,
        "path": "usb",
        "device": device,
        "pi_unavailable": pi_problem,
        "ros_domain_id": baseline.get("UXRCE_DDS_DOM_ID"),
        "vehicle": {"armed": armed, "landed": landed},
        "changes": differences,
        "gate": "PX4 must report disarmed and landed over USB",
    }


def _result(
    args: argparse.Namespace,
    outcome: Outcome,
    code: str,
    summary: str,
    payload: dict[str, Any],
    message: str | None = None,
) -> CommandResult:
    return CommandResult(
        command=COMMAND,
        outcome=outcome,
        summary=summary,
        code=code,
        target=args.host if payload.get("path") == "pi" else payload.get("device"),
        profile=args.profile,
        findings=(
            ()
            if message is None
            else (Finding(code, message, severity="error" if outcome is not Outcome.SUCCESS else "info"),)
        ),
        payload_schema=SCHEMA,
        payload=payload,
        next_actions=(
            NextAction(
                ("iii", "px4", "inspect", "--host", args.host, "--profile", args.profile),
                "Inspect the Pi-side PX4 link.",
            ),
        ),
    )


def _apply_through_pi(args: argparse.Namespace, payload: dict[str, Any]) -> CommandResult:
    try:
        response = _api(args, timeout=_APPLY_TIMEOUT_SECONDS).apply_px4_parameter_baseline()
    except RuntimeApiError as exc:
        return _result(
            args, Outcome.FAILED, "III_PX4_BASELINE_FAILED",
            "The Pi could not apply the PX4 baseline; verify the parameters before use.",
            payload, str(exc),
        )
    payload["result"] = response.get("result") or {}
    if not response.get("accepted"):
        return _result(
            args, Outcome.REJECTED, "III_PX4_BASELINE_REJECTED",
            "The PX4 baseline was not applied.", payload, str(response.get("message")),
        )
    return _applied(args, payload, payload["result"].get("changed") or [])


def _applied(
    args: argparse.Namespace, payload: dict[str, Any], changed: list[dict[str, Any]]
) -> CommandResult:
    payload["changed"] = changed
    if not changed:
        return _result(
            args, Outcome.SUCCESS, "III_PX4_BASELINE_MATCHES",
            f"The flight controller already matches the {args.profile} baseline; "
            "nothing was written.",
            payload,
        )
    return _result(
        args, Outcome.SUCCESS, "III_PX4_BASELINE_APPLIED",
        f"{len(changed)} PX4 parameters were set to the {args.profile} baseline; the "
        "flight controller rebooted and the baseline was verified.",
        payload,
    )


def _reconnect_usb(device: str) -> Px4Link:
    """The USB device disappears while the flight controller reboots."""

    deadline = time.monotonic() + _USB_RETURN_TIMEOUT_SECONDS
    time.sleep(3.0)
    last = "the USB device did not return"
    while time.monotonic() < deadline:
        if os.path.exists(device):
            try:
                return _connect_usb(device)
            except MavlinkError as exc:
                last = str(exc)
        time.sleep(1.0)
    raise MavlinkError(f"the flight controller did not come back after its reboot: {last}")


def _apply_through_usb(args: argparse.Namespace, payload: dict[str, Any]) -> CommandResult:
    from iii_drone_contracts.px4_parameters import (
        BaselineError,
        describe,
        load_baseline,
        mismatches,
    )

    device = str(payload["device"])
    try:
        baseline = load_baseline(
            args.profile, _baseline_directory(), ros_domain_id=payload.get("ros_domain_id")
        )
        link = _connect_usb(device)
        try:
            refusal = _vehicle_refusal(*link.vehicle_state())
            if refusal is not None:
                return _result(
                    args, Outcome.REJECTED, "III_PX4_BASELINE_REJECTED",
                    "The aircraft is not provably disarmed and landed; nothing was written.",
                    payload, refusal,
                )
            before = link.read_parameters(list(baseline))
            differences = mismatches(baseline, before)
            if not differences:
                return _applied(args, payload, [])
            for item in differences:
                name = str(item["name"])
                # Write with the type PX4 reported for the parameter.
                value = float(baseline[name]) if isinstance(before.get(name), float) else baseline[name]
                link.write_parameter(name, value)
            written = mismatches(baseline, link.read_parameters(list(baseline)))
            if written:
                raise MavlinkError(f"PX4 did not accept the baseline: {describe(written)}")
            # PX4 saves changed parameters on its own shortly after a write.
            time.sleep(_AUTOSAVE_SETTLE_SECONDS)
            link.reboot()
        finally:
            link.close()
        link = _reconnect_usb(device)
        try:
            after = mismatches(baseline, link.read_parameters(list(baseline)))
        finally:
            link.close()
        if after:
            raise MavlinkError(f"PX4 lost the baseline over its reboot: {describe(after)}")
    except (MavlinkError, BaselineError) as exc:
        return _result(
            args, Outcome.FAILED, "III_PX4_BASELINE_FAILED",
            "The PX4 baseline could not be applied and verified; verify the "
            "parameters before use.",
            payload, str(exc),
        )
    return _applied(args, payload, differences)


def apply(args: argparse.Namespace) -> CommandResult:
    payload = dict(args._iii_retained_plan["preflight"])
    if payload["path"] == "pi":
        return _apply_through_pi(args, payload)
    return _apply_through_usb(args, payload)


def initialize(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "param-baseline",
        help="set the flight controller's parameters to a profile's PX4 baseline",
        description=(
            "Compare the flight controller with the profile's baseline in "
            "deployment/px4/parameters, show the difference, then write it, reboot the flight "
            "controller and verify. Refuses unless PX4 reports the aircraft disarmed "
            "and landed. Uses the Pi's MAVLink link when the Pi runs the same "
            "profile, otherwise the flight controller's USB port on this computer. "
            "--dry-run only shows the difference."
        ),
    )
    parser.add_argument("--profile", choices=PROFILES, required=True)
    parser.add_argument("--host", default="iii.local", help="Pi hostname or IP")
    parser.add_argument(
        "--usb-device",
        metavar="PATH",
        help=f"flight controller serial device for the USB path (default: {_USB_GLOB})",
    )
    parser.add_argument(
        "--ros-domain-id",
        type=int,
        metavar="N",
        help=(
            "stack ROS domain written to UXRCE_DDS_DOM_ID on the USB path (default: "
            "the Pi's when it is reachable, otherwise the baseline's)"
        ),
    )
    parser.set_defaults(func=apply, _iii_mutating=True, _iii_plan_provider=plan)
