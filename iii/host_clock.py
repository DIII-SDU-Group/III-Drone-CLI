"""Settle an aircraft's onboard clock from the ground computer.

``iii host clock sync --profile real|opti_track --host <pi>`` runs on the ground
computer. The aircraft runtime API refuses arming and mission activation until
the Pi's chrony is synchronized with a residual offset of at most 0.1 s (it
reports this on ``GET /clock/status``); ground control runs this command when
an aircraft appears with an unsettled clock and stops it after 45 s.

The command refuses unless the runtime API reports the aircraft disarmed and
landed. A settled clock is left alone. A Pi that follows a time source but is
too far off is stepped (``chronyc makestep``). A Pi without a time source gets
this ground computer as a runtime-only chrony source first; that needs chrony
on the ground computer serving the Pi (an ``allow`` directive). The command
never changes the ground computer's configuration and adds nothing persistent
to the Pi: ``chronyc add`` lasts until chrony restarts.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import ipaddress
import os
import subprocess
import time
from typing import Any, Mapping

from .result import CommandResult, Finding, NextAction, Outcome
from .runtime_api_client import RuntimeApiClient, RuntimeApiError, _cli_token_from_env
from .runtime_routing import RouteTimeouts


AIRCRAFT_PROFILES = ("real", "opti_track")
# The runtime's arming and mission gate (iii_drone_runtime.api.clock_sync).
MAX_OFFSET_SECONDS = 0.1
# Ground control stops the command after 45 s; finish, settled or not, before.
DEADLINE_SECONDS = 40.0
POLL_SECONDS = 2.0
STEP_INTERVAL_SECONDS = 4.0
SSH_CONNECT_TIMEOUT_SECONDS = 5
REMOTE_TIMEOUT_SECONDS = 8.0
LOCAL_TIMEOUT_SECONDS = 3.0
API_TIMEOUT_SECONDS = 5.0
SCHEMA = "iii.host-clock-sync/v1"
SOURCES_MARKER = "III_CHRONY_SOURCES"
CONNECTION_MARKER = "III_SSH_CONNECTION="

# Indirections so tests can drive time.
_monotonic = time.monotonic
_sleep = time.sleep


@dataclass(frozen=True)
class Tracking:
    """One ``chronyc -c tracking`` sample, judged like the runtime judges it."""

    settled: bool
    detail: str
    leap_status: str | None = None
    offset_seconds: float | None = None
    reference: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_tracking(output: str) -> Tracking:
    """Judge ``chronyc -c tracking`` CSV the way the runtime clock gate does."""

    fields = next(
        (line.split(",") for line in output.splitlines() if line.count(",") >= 13),
        None,
    )
    if fields is None:
        return Tracking(settled=False, detail="chronyc tracking output is unavailable")
    reference = fields[1] or fields[0]
    leap_status = fields[13].strip()
    try:
        offset = float(fields[4])
    except ValueError:
        return Tracking(
            settled=False,
            detail="unrecognized chronyc system time offset",
            leap_status=leap_status,
            reference=reference,
        )
    if leap_status != "Normal":
        detail = f"chrony is not synchronized ({leap_status})"
        settled = False
    elif abs(offset) > MAX_OFFSET_SECONDS:
        detail = f"clock is {offset:+.3f} s from {reference}"
        settled = False
    else:
        detail = f"synchronized to {reference}, offset {offset:+.6f} s"
        settled = True
    return Tracking(
        settled=settled,
        detail=detail,
        leap_status=leap_status,
        offset_seconds=offset,
        reference=reference,
    )


def source_reach(output: str, address: str) -> int | None:
    """Reach register of ``address`` in ``chronyc -c sources`` output."""

    _before, marker, sources = output.partition(SOURCES_MARKER)
    for line in (sources if marker else output).splitlines():
        fields = line.split(",")
        if len(fields) >= 6 and fields[2] == address:
            try:
                return int(fields[5], 8)
            except ValueError:
                return None
    return None


def vehicle_refusal(vehicle: Any) -> str | None:
    """Why the aircraft is not provably disarmed and landed, or None.

    Mirrors the runtime's lifecycle gate (iii_drone_runtime.api.safety): the
    armed and in-air evidence must be known, fresh, and agreed by its sources.
    """

    if not isinstance(vehicle, Mapping):
        return "vehicle state unknown"
    fields = vehicle.get("telemetry_fields")
    fields = fields if isinstance(fields, Mapping) else {}
    armed_evidence = fields.get("armed")
    in_air_evidence = fields.get("in_air")
    detail = vehicle.get("degraded_reason") or vehicle.get("error_reason")
    if isinstance(armed_evidence, Mapping) and isinstance(in_air_evidence, Mapping):
        evidence = (armed_evidence, in_air_evidence)
        known = all(
            item.get("value") is not None
            and item.get("source_availability") != "unavailable"
            for item in evidence
        )
        fresh = all(item.get("freshness") == "fresh" for item in evidence)
        degraded = any(item.get("disagreement") is True for item in evidence)
        armed = armed_evidence.get("value")
        in_air = in_air_evidence.get("value")
        detail = "; ".join(
            str(item["detail"]) for item in evidence if item.get("detail")
        ) or detail
    else:
        availability = vehicle.get("source_availability", "unknown")
        armed = vehicle.get("armed")
        in_air = vehicle.get("in_air")
        known = availability != "unavailable" and armed is not None and in_air is not None
        fresh = vehicle.get("freshness") == "fresh"
        degraded = availability == "degraded"
    suffix = f": {detail}" if detail else ""
    if not known:
        return f"vehicle state unknown{suffix}"
    if not fresh:
        return f"vehicle state stale{suffix}"
    if degraded:
        return f"vehicle armed/in-air sources disagree{suffix}"
    if armed is not False:
        return "vehicle is armed"
    if in_air is not False:
        return "vehicle is in flight"
    return None


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _ssh(target: str, script: str, *, timeout: float) -> subprocess.CompletedProcess[str]:
    command = [
        "ssh",
        "-T",
        *RouteTimeouts(ssh_connect_timeout=SSH_CONNECT_TIMEOUT_SECONDS).ssh_options(),
        target,
        script,
    ]
    try:
        return subprocess.run(
            command, capture_output=True, text=True, check=False, timeout=max(timeout, 0.1)
        )
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            command, 124, _text(exc.stdout), _text(exc.stderr) + "\nssh timed out"
        )


def _local(command: list[str]) -> subprocess.CompletedProcess[str] | None:
    """Run a read-only local command; None if it is not installed."""

    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=LOCAL_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(command, 124, _text(exc.stdout), "timed out")


def _ground_ntp_problem(aircraft_address: str | None) -> str | None:
    """Why this ground computer cannot serve the Pi NTP, or None if it can.

    Read-only: it never reconfigures the ground computer.
    """

    remedy = (
        "install chrony on this ground computer, add `allow "
        f"{aircraft_address or '<pi address>'}` (and `local stratum 10` for use "
        "without internet) to /etc/chrony/chrony.conf, and restart chrony"
    )
    tracking = _local(["chronyc", "-c", "tracking"])
    if tracking is None:
        return f"chrony is not installed on this ground computer; {remedy}"
    if tracking.returncode != 0:
        return f"chrony is not running on this ground computer; {remedy}"
    state = parse_tracking(tracking.stdout)
    if state.leap_status != "Normal":
        return (
            "this ground computer's own clock is not synchronized "
            f"({state.detail}); give it internet NTP or {remedy}"
        )
    listeners = _local(["ss", "-H", "-l", "-u", "-n", "sport", "=", ":123"])
    if listeners is not None and listeners.returncode == 0 and not listeners.stdout.strip():
        return f"chrony on this ground computer does not serve NTP; {remedy}"
    return None


def _valid_host(host: str) -> bool:
    return bool(host) and host[0].isalnum() and all(
        character.isascii() and (character.isalnum() or character in ".-")
        for character in host
    )


def plan(args: argparse.Namespace) -> dict[str, Any]:
    """The retained operation plan; it reads nothing from the network."""

    return {
        "schema": SCHEMA,
        "host": args.host,
        "user": args.user,
        "profile": args.profile,
        "max_offset_seconds": MAX_OFFSET_SECONDS,
        "deadline_seconds": DEADLINE_SECONDS,
        "gate": "runtime API /cli/vehicle/status must show the aircraft disarmed and landed",
        "steps": [
            "read the Pi's chrony tracking over SSH; a settled clock is left alone",
            "step a Pi that follows a time source (chronyc makestep)",
            "otherwise add this ground computer as a runtime-only chrony source, then step",
            "wait until Leap status is Normal and the offset is within the limit",
        ],
    }


class _Sync:
    def __init__(self, args: argparse.Namespace):
        self.host: str = args.host
        self.user: str = args.user
        self.profile: str = args.profile
        self.target = f"{self.user}@{self.host}"
        self.started = _monotonic()
        self.deadline = self.started + DEADLINE_SECONDS
        self.payload: dict[str, Any] = {
            "schema": SCHEMA,
            "host": self.host,
            "profile": self.profile,
            "runtime_profile": None,
            "vehicle_refusal": None,
            "ground_computer_address": None,
            "aircraft_address": None,
            "initial": None,
            "final": None,
            "actions": [],
            "source_reach": None,
            "samples": 0,
            "elapsed_seconds": 0.0,
        }

    def remaining(self) -> float:
        return self.deadline - _monotonic()

    def ssh(self, script: str) -> subprocess.CompletedProcess[str]:
        return _ssh(self.target, script, timeout=min(REMOTE_TIMEOUT_SECONDS, self.remaining()))

    def result(
        self,
        outcome: Outcome,
        code: str,
        summary: str,
        message: str | None = None,
    ) -> CommandResult:
        self.payload["elapsed_seconds"] = round(_monotonic() - self.started, 3)
        retry = NextAction(
            (
                "iii", "host", "clock", "sync",
                "--profile", self.profile,
                "--host", self.host,
                "--confirm",
            ),
            "Retry once the stated condition holds.",
            mutating=True,
            confirmation_required=True,
            target=self.target,
            profile=self.profile,
        )
        inspect = NextAction(
            ("iii", "host", "inspect", "--host", self.host),
            "Inspect the Pi before flight preparation continues.",
        )
        return CommandResult(
            command="iii host clock sync",
            outcome=outcome,
            summary=summary,
            code=code,
            target=self.target,
            profile=self.profile,
            findings=(
                ()
                if message is None
                else (
                    Finding(
                        code,
                        message,
                        severity="error" if outcome is not Outcome.SUCCESS else "info",
                    ),
                )
            ),
            payload_schema=SCHEMA,
            payload=dict(self.payload),
            next_actions=(inspect,) if outcome is Outcome.SUCCESS else (retry, inspect),
        )

    def run(self) -> CommandResult:
        if not _valid_host(self.host):
            return self.result(
                Outcome.USAGE_ERROR,
                "III_CLOCK_SYNC_HOST_INVALID",
                "The aircraft host is invalid; nothing was changed.",
                "--host must be a hostname or IPv4 address",
            )
        refusal = self.vehicle_gate()
        if refusal is not None:
            return refusal

        probe = self.ssh(
            f'printf "{CONNECTION_MARKER}%s\\n" "$SSH_CONNECTION"; chronyc -c tracking'
        )
        if probe.returncode != 0 and CONNECTION_MARKER not in probe.stdout:
            return self.result(
                Outcome.FAILED,
                "III_CLOCK_SYNC_SSH_FAILED",
                "The aircraft could not be reached over SSH; its clock is unchanged.",
                (probe.stderr or "ssh failed").strip(),
            )
        initial = parse_tracking(probe.stdout)
        self.payload["initial"] = self.payload["final"] = initial.as_dict()
        if initial.settled:
            return self.result(
                Outcome.SUCCESS,
                "III_CLOCK_SYNC_ALREADY_SETTLED",
                f"The aircraft clock is already settled: {initial.detail}.",
            )

        if initial.leap_status != "Normal":
            failure = self.add_ground_source(probe.stdout)
            if failure is not None:
                return failure
        return self.settle(initial)

    def vehicle_gate(self) -> CommandResult | None:
        try:
            client = RuntimeApiClient(
                base_url=f"http://{self.host}:{os.environ.get('III_RUNTIME_API_PORT', '8765')}",
                cli_token=_cli_token_from_env(),
                timeout_seconds=API_TIMEOUT_SECONDS,
            )
            identity = client.identity()
            vehicle = client.vehicle_status()
        except RuntimeApiError as exc:
            self.payload["vehicle_refusal"] = "vehicle state unknown"
            return self.result(
                Outcome.REJECTED,
                "III_CLOCK_SYNC_VEHICLE_STATE_UNKNOWN",
                "The aircraft's disarmed and landed state could not be read; its clock is unchanged.",
                f"the runtime API did not report the vehicle state: {exc}",
            )
        if isinstance(identity, Mapping) and isinstance(identity.get("identity"), Mapping):
            identity = identity["identity"]
        runtime_profile = identity.get("profile") if isinstance(identity, Mapping) else None
        self.payload["runtime_profile"] = runtime_profile
        if runtime_profile != self.profile:
            return self.result(
                Outcome.REJECTED,
                "III_CLOCK_SYNC_PROFILE_MISMATCH",
                "The aircraft runs a different runtime profile; its clock is unchanged.",
                f"the runtime API at {self.host} reports profile {runtime_profile!r}, not {self.profile!r}",
            )
        refusal = vehicle_refusal(vehicle)
        self.payload["vehicle_refusal"] = refusal
        if refusal is not None:
            return self.result(
                Outcome.REJECTED,
                "III_CLOCK_SYNC_VEHICLE_NOT_SAFE",
                "The aircraft is not provably disarmed and landed; its clock is unchanged.",
                refusal,
            )
        return None

    def add_ground_source(self, probe_output: str) -> CommandResult | None:
        connection = next(
            (
                line[len(CONNECTION_MARKER):].split()
                for line in probe_output.splitlines()
                if line.startswith(CONNECTION_MARKER)
            ),
            [],
        )
        try:
            ground = str(ipaddress.IPv4Address(connection[0]))
            aircraft = str(ipaddress.IPv4Address(connection[2]))
        except (IndexError, ValueError):
            return self.result(
                Outcome.FAILED,
                "III_CLOCK_SYNC_NTP_FALLBACK_UNAVAILABLE",
                "The aircraft has no time source and the ground computer's address is unknown.",
                "SSH did not reach the Pi over IPv4; pass the Pi's IPv4 address with --host",
            )
        self.payload["ground_computer_address"] = ground
        self.payload["aircraft_address"] = aircraft
        problem = _ground_ntp_problem(aircraft)
        if problem is not None:
            return self.result(
                Outcome.FAILED,
                "III_CLOCK_SYNC_NTP_FALLBACK_UNAVAILABLE",
                "The aircraft has no time source and this ground computer cannot serve one.",
                problem,
            )
        added = self.ssh(f"sudo -n chronyc add server {ground} iburst")
        output = f"{added.stdout}\n{added.stderr}"
        if added.returncode != 0 and "already" not in output.lower():
            return self.result(
                Outcome.FAILED,
                "III_CLOCK_SYNC_SOURCE_REJECTED",
                "The Pi did not accept the ground computer as a chrony source.",
                output.strip() or "chronyc add server failed",
            )
        self.payload["actions"].append(f"add-server {ground}")
        return None

    def settle(self, initial: Tracking) -> CommandResult:
        tracking = initial
        last_step: float | None = None
        while self.remaining() > 0:
            sample = self.ssh(f"chronyc -c tracking; echo {SOURCES_MARKER}; chronyc -c sources")
            self.payload["samples"] += 1
            if sample.returncode == 0 or sample.stdout:
                tracking = parse_tracking(sample.stdout)
                self.payload["final"] = tracking.as_dict()
                ground = self.payload["ground_computer_address"]
                if ground is not None:
                    self.payload["source_reach"] = source_reach(sample.stdout, ground)
            if tracking.settled:
                return self.result(
                    Outcome.SUCCESS,
                    "III_CLOCK_SYNC_SETTLED",
                    f"The aircraft clock is settled: {tracking.detail}.",
                )
            now = _monotonic()
            if (
                tracking.leap_status == "Normal"
                and (last_step is None or now - last_step >= STEP_INTERVAL_SECONDS)
            ):
                stepped = self.ssh("sudo -n chronyc makestep")
                if stepped.returncode != 0:
                    return self.result(
                        Outcome.FAILED,
                        "III_CLOCK_SYNC_STEP_REJECTED",
                        "The Pi did not step its clock.",
                        f"{stepped.stdout}\n{stepped.stderr}".strip()
                        or "chronyc makestep failed",
                    )
                self.payload["actions"].append("makestep")
                last_step = now
                _sleep(min(0.5, max(self.remaining(), 0.0)))
                continue
            _sleep(min(POLL_SECONDS, max(self.remaining(), 0.0)))
        message = f"after {DEADLINE_SECONDS:g} s: {tracking.detail}"
        if self.payload["ground_computer_address"] and not self.payload["source_reach"]:
            message += (
                f"; the ground computer {self.payload['ground_computer_address']} never "
                "answered NTP: chrony on it needs `allow "
                f"{self.payload['aircraft_address']}` (and no firewall on UDP 123)"
            )
        return self.result(
            Outcome.FAILED,
            "III_CLOCK_SYNC_TIMEOUT",
            "The aircraft clock did not settle in time.",
            message,
        )


def sync(args: argparse.Namespace) -> CommandResult:
    return _Sync(args).run()


def initialize(parser: argparse.ArgumentParser) -> None:
    subparsers = parser.add_subparsers(dest="host_clock_command")
    sync_parser = subparsers.add_parser(
        "sync",
        help="settle an aircraft's chrony clock from this ground computer",
        description=(
            "Refuses unless the runtime API shows the aircraft disarmed and landed. "
            "Steps a Pi that follows a time source; otherwise adds this ground "
            "computer (chrony with an `allow` for the Pi) as a runtime-only source. "
            f"Succeeds when chrony reports Leap status Normal and |offset| <= "
            f"{MAX_OFFSET_SECONDS:g} s, within {DEADLINE_SECONDS:g} s."
        ),
    )
    sync_parser.add_argument("--profile", choices=AIRCRAFT_PROFILES, required=True)
    sync_parser.add_argument("--host", default="iii.local")
    sync_parser.add_argument("--user", default="iii")
    sync_parser.set_defaults(func=sync, _iii_mutating=True, _iii_plan_provider=plan)
