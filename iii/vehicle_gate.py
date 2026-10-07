"""Vehicle-state gate for operations that restart the Pi's runtime services.

Provisioning and deployment restart the system daemon and the Runtime API,
which stops a running aircraft system. They are therefore allowed only while
the aircraft is provably disarmed and landed, as the Runtime API requires for
its own lifecycle commands (iii_drone_runtime.api.safety):

- a Pi without a provisioned profile has no runtime to interrupt;
- the HIL profile flies the workstation's simulated PX4, so it is not gated;
- real and opti_track need the Runtime API's live vehicle state.

``--force`` overrides only a vehicle state that cannot be read (flight
controller unpowered, MAVLink link not set up, Runtime API down). A vehicle
reported armed or in flight is never overridden.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import subprocess
from typing import Any, Sequence

from .host_clock import vehicle_refusal
from .result import CommandResult, Finding, NextAction, Outcome
from .runtime_api_client import RuntimeApiClient, RuntimeApiError

GATE_REJECTED = "III_VEHICLE_GATE_REJECTED"
VIRTUAL_PROFILES = frozenset({"hil"})
GATED_PROFILES = frozenset({"real", "opti_track"})
_RUNTIME_ENV = "/etc/iii/runtime.env"
_NEVER_OVERRIDABLE = frozenset({"vehicle is armed", "vehicle is in flight"})


@dataclass(frozen=True)
class RestartGate:
    allowed: bool
    profile: str | None
    reason: str
    forced: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "provisioned_profile": self.profile,
            "reason": self.reason,
            "forced": self.forced,
        }


def provisioned_profile(
    target: str, *, ssh_options: Sequence[str] = ()
) -> tuple[str | None, str | None]:
    """The Pi's provisioned profile ('' when never provisioned) and an SSH error."""

    try:
        completed = subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=10",
                *ssh_options,
                target,
                f"sed -n 's/^III_SYSTEM_PROFILE=//p' {_RUNTIME_ENV} 2>/dev/null | head -n 1",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)
    if completed.returncode != 0:
        detail = completed.stderr.strip().splitlines()
        return None, detail[-1] if detail else f"ssh exited {completed.returncode}"
    return completed.stdout.strip(), None


def _unreadable(profile: str | None, reason: str, force: bool) -> RestartGate:
    if force:
        return RestartGate(True, profile, f"{reason}; overridden by --force", forced=True)
    return RestartGate(False, profile, reason)


def evaluate(
    host: str,
    user: str,
    *,
    force: bool = False,
    ssh_options: Sequence[str] = (),
    api_host: str | None = None,
) -> RestartGate:
    profile, ssh_error = provisioned_profile(f"{user}@{host}", ssh_options=ssh_options)
    if profile is None:
        return _unreadable(None, f"the Pi could not be queried over SSH: {ssh_error}", force)
    if not profile:
        return RestartGate(True, None, "the Pi has no provisioned runtime profile")
    if profile in VIRTUAL_PROFILES:
        return RestartGate(True, profile, f"profile {profile} flies the simulated PX4")
    if profile not in GATED_PROFILES:
        return _unreadable(profile, f"the Pi's profile {profile!r} is unknown", force)
    port = os.environ.get("III_RUNTIME_API_PORT", "8765")
    try:
        vehicle = RuntimeApiClient(
            base_url=f"http://{api_host or host}:{port}", timeout_seconds=8.0
        ).vehicle_status()
    except RuntimeApiError as exc:
        return _unreadable(
            profile, f"the Runtime API's vehicle state could not be read: {exc}", force
        )
    refusal = vehicle_refusal(vehicle)
    if refusal is None:
        return RestartGate(True, profile, "the aircraft is disarmed and landed")
    if refusal in _NEVER_OVERRIDABLE:
        return RestartGate(False, profile, refusal)
    return _unreadable(profile, refusal, force)


def rejection(command: str, gate: RestartGate, *, target: str) -> CommandResult:
    overridable = gate.reason not in _NEVER_OVERRIDABLE
    return CommandResult(
        command=command,
        outcome=Outcome.REJECTED,
        summary=(
            "The aircraft is not provably disarmed and landed; nothing was changed "
            "and no service was restarted."
        ),
        code=GATE_REJECTED,
        target=target,
        profile=gate.profile,
        findings=(
            Finding(
                GATE_REJECTED,
                gate.reason
                + (
                    ". Power the flight controller and check its MAVLink link, or pass "
                    "--force when you know the aircraft is disarmed on the ground."
                    if overridable
                    else ". --force does not override an armed or flying aircraft."
                ),
            ),
        ),
        payload_schema="iii.vehicle-gate/v1",
        payload=gate.as_dict(),
        next_actions=(
            NextAction(
                ("iii", "px4", "inspect", "--host", target.rsplit("@", 1)[-1]),
                "Inspect the Pi-side PX4 link.",
            ),
        ),
    )
