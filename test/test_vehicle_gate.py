"""The gate that protects provisioning and deployment restarts."""

from __future__ import annotations

import pytest

from iii import vehicle_gate
from iii.runtime_api_client import RuntimeApiError
from iii.vehicle_gate import evaluate as real_evaluate

SAFE = {"armed": False, "in_air": False, "source_availability": "available", "freshness": "fresh"}


def _gate(monkeypatch, *, profile, vehicle=None, api_error=None, ssh_error=None, force=False):
    monkeypatch.setattr(
        vehicle_gate, "provisioned_profile", lambda target, **_kwargs: (profile, ssh_error)
    )

    class Client:
        def __init__(self, **_kwargs):
            pass

        def vehicle_status(self):
            if api_error is not None:
                raise RuntimeApiError(api_error)
            return vehicle

    monkeypatch.setattr(vehicle_gate, "RuntimeApiClient", Client)
    return real_evaluate("pi.local", "iii", force=force)


def test_an_unprovisioned_pi_and_the_virtual_hil_profile_pass(monkeypatch):
    assert _gate(monkeypatch, profile="").allowed
    assert _gate(monkeypatch, profile="hil").allowed


@pytest.mark.parametrize("profile", ["real", "opti_track"])
def test_aircraft_profiles_need_a_disarmed_landed_vehicle(monkeypatch, profile):
    assert _gate(monkeypatch, profile=profile, vehicle=SAFE).allowed
    armed = _gate(monkeypatch, profile=profile, vehicle={**SAFE, "armed": True})
    assert not armed.allowed and armed.reason == "vehicle is armed"
    flying = _gate(monkeypatch, profile=profile, vehicle={**SAFE, "in_air": True})
    assert not flying.allowed and flying.reason == "vehicle is in flight"


def test_force_overrides_only_an_unreadable_vehicle_state(monkeypatch):
    unreadable = {"api_error": "connection refused"}
    blocked = _gate(monkeypatch, profile="real", **unreadable)
    assert not blocked.allowed and "could not be read" in blocked.reason
    forced = _gate(monkeypatch, profile="real", force=True, **unreadable)
    assert forced.allowed and forced.forced

    stale = {**SAFE, "freshness": "stale"}
    assert not _gate(monkeypatch, profile="opti_track", vehicle=stale).allowed
    assert _gate(monkeypatch, profile="opti_track", vehicle=stale, force=True).forced

    for unsafe in ({**SAFE, "armed": True}, {**SAFE, "in_air": True}):
        assert not _gate(monkeypatch, profile="real", vehicle=unsafe, force=True).allowed


def test_an_unreachable_pi_blocks_unless_forced(monkeypatch):
    assert not _gate(monkeypatch, profile=None, ssh_error="timed out").allowed
    assert _gate(monkeypatch, profile=None, ssh_error="timed out", force=True).forced


def test_the_rejection_says_whether_force_applies():
    armed = vehicle_gate.rejection(
        "iii deploy dev",
        vehicle_gate.RestartGate(False, "real", "vehicle is armed"),
        target="iii@pi.local",
    )
    assert "--force does not override" in armed.findings[0].message
    unknown = vehicle_gate.rejection(
        "iii host provision",
        vehicle_gate.RestartGate(False, "real", "vehicle state unknown"),
        target="iii@pi.local",
    )
    assert "pass --force" in unknown.findings[0].message
