"""`iii host clock sync`: settle an aircraft clock from the ground computer.

SSH, the local chrony/ss probes, the runtime API, and time are faked; nothing
here reaches a Pi or changes a clock.
"""

from __future__ import annotations

from io import StringIO
import json
import subprocess

import pytest

from iii import host_clock
from iii.__main__ import main
from iii.runtime_api_client import RuntimeApiError


GC_ADDRESS = "192.168.10.23"
PI_ADDRESS = "192.168.10.57"
# The exact invocation of src/III-Drone-GC/iii_drone_gc/clock_sync.py.
GC_ARGV = [
    "host", "clock", "sync",
    "--profile", "opti_track",
    "--host", "iii.local",
    "--confirm", "--non-interactive", "--json",
]


def tracking(leap: str = "Normal", offset: float = 0.0004, name: str = "185.125.190.56") -> str:
    """`chronyc -c tracking` CSV: offset is field 4, leap status field 13."""
    return f"B97DBE38,{name},2,1791196000.123,{offset},0.0001,0.0002,-12.3,0.001,0.02,0.012,0.001,64.4,{leap}\n"


def sources(reach: str = "377") -> str:
    return f"^,?,{GC_ADDRESS},3,1,{reach},2,0.000123,0.000123,0.001\n"


def vehicle(*, armed=False, in_air=False, freshness="fresh", disagreement=False, evidence=True):
    state = {
        "freshness": "fresh",
        "source_availability": "available",
        "armed": armed,
        "in_air": in_air,
    }
    if evidence:
        state["telemetry_fields"] = {
            name: {
                "value": value,
                "source": "PX4 MAVSDK",
                "freshness": freshness,
                "source_availability": "available",
                "disagreement": disagreement and name == "armed",
                "detail": "MAVSDK and ROS/uXRCE disagree" if disagreement and name == "armed" else None,
            }
            for name, value in (("armed", armed), ("in_air", in_air))
        }
    return state


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(seconds, 0.0)


class FakeAircraft:
    """The Pi over SSH plus this ground computer's chrony and ss."""

    def __init__(self, clock, *, samples, probe=None, ground_tracking=None, listening=True, add_rc=0, step_rc=0, ssh_rc=0):
        self.clock = clock
        self.samples = list(samples)
        self.probe = probe if probe is not None else self.samples[0]
        self.ground_tracking = ground_tracking if ground_tracking is not None else tracking()
        self.listening = listening
        self.add_rc = add_rc
        self.step_rc = step_rc
        self.ssh_rc = ssh_rc
        self.remote = []
        self.local = []

    def run(self, command, **kwargs):
        self.clock.now += 0.3
        if command[0] == "ssh":
            assert command[1] == "-T" and "BatchMode=yes" in command
            assert command[-2] == "iii@iii.local"
            script = command[-1]
            self.remote.append(script)
            if self.ssh_rc:
                return subprocess.CompletedProcess(command, self.ssh_rc, "", "ssh: connect to host iii.local port 22: No route to host")
            if "SSH_CONNECTION" in script:
                stdout = f"III_SSH_CONNECTION={GC_ADDRESS} 51234 {PI_ADDRESS} 22\n{self.probe}"
                return subprocess.CompletedProcess(command, 0, stdout, "")
            if "chronyc add server" in script:
                return subprocess.CompletedProcess(command, self.add_rc, "", "" if self.add_rc == 0 else "sudo: a password is required")
            if "makestep" in script:
                if self.step_rc:
                    return subprocess.CompletedProcess(command, self.step_rc, "", "sudo: a password is required")
                return subprocess.CompletedProcess(command, 0, "200 OK\n", "")
            if host_clock.SOURCES_MARKER in script:
                sample = self.samples.pop(0) if len(self.samples) > 1 else self.samples[0]
                return subprocess.CompletedProcess(command, 0, sample, "")
            raise AssertionError(f"unexpected remote script: {script}")
        self.local.append(command)
        if command[0] == "chronyc":
            if self.ground_tracking == "missing":
                raise FileNotFoundError("chronyc")
            return subprocess.CompletedProcess(command, 0, self.ground_tracking, "")
        if command[0] == "ss":
            listener = "UNCONN 0 0 0.0.0.0:123 0.0.0.0:*\n" if self.listening else ""
            return subprocess.CompletedProcess(command, 0, listener, "")
        raise AssertionError(f"unexpected local command: {command}")


class FakeRuntime:
    def __init__(self, *, profile="opti_track", state=None, error=None):
        self.profile = profile
        self.state = vehicle() if state is None else state
        self.error = error
        self.base_urls = []

    def client(self, *, base_url, timeout_seconds):
        self.base_urls.append(base_url)
        runtime = self

        class Client:
            def identity(self):
                if runtime.error:
                    raise RuntimeApiError(runtime.error)
                return {"runtime_id": "iii-runtime", "runtime_name": "III Runtime", "profile": runtime.profile}

            def vehicle_status(self):
                return runtime.state

        return Client()


@pytest.fixture
def clock(monkeypatch, tmp_path):
    fake = FakeClock()
    monkeypatch.setattr(host_clock, "_monotonic", fake.monotonic)
    monkeypatch.setattr(host_clock, "_sleep", fake.sleep)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("III_RUNTIME_API_PORT", raising=False)
    return fake


def _sync(monkeypatch, tmp_path, aircraft, runtime, argv=GC_ARGV):
    monkeypatch.setattr(host_clock.subprocess, "run", aircraft.run)
    monkeypatch.setattr(host_clock, "RuntimeApiClient", runtime.client)
    output = StringIO()
    status = main(
        list(argv),
        stdout=output,
        environment={"III_OPERATION_STATE_DIR": str(tmp_path / "operations"), "HOME": str(tmp_path)},
    )
    # Exactly one JSON result object, as ground control expects.
    return status, json.loads(output.getvalue())


def test_a_settled_aircraft_clock_is_left_alone(monkeypatch, tmp_path, clock):
    aircraft = FakeAircraft(clock, samples=[tracking()])
    runtime = FakeRuntime()
    status, result = _sync(monkeypatch, tmp_path, aircraft, runtime)
    assert status == 0
    assert result["code"] == "III_CLOCK_SYNC_ALREADY_SETTLED"
    assert result["payload"]["actions"] == []
    assert len(aircraft.remote) == 1
    assert aircraft.local == []
    assert runtime.base_urls == ["http://iii.local:8765"]


def test_an_aircraft_following_a_source_is_stepped(monkeypatch, tmp_path, clock):
    aircraft = FakeAircraft(
        clock,
        probe=tracking(offset=3.2),
        samples=[tracking(offset=3.1), tracking(offset=0.0002)],
    )
    status, result = _sync(monkeypatch, tmp_path, aircraft, FakeRuntime())
    assert status == 0, result
    assert result["code"] == "III_CLOCK_SYNC_SETTLED"
    assert result["payload"]["actions"] == ["makestep"]
    assert result["payload"]["initial"]["offset_seconds"] == 3.2
    assert result["payload"]["final"]["settled"] is True
    assert not any("add server" in script for script in aircraft.remote)
    # The ground computer is not needed as a time source.
    assert aircraft.local == []


def test_an_aircraft_without_a_time_source_uses_the_ground_computer(monkeypatch, tmp_path, clock):
    aircraft = FakeAircraft(
        clock,
        probe=tracking(leap="Not synchronised", offset=0.0, name=""),
        samples=[
            tracking(leap="Not synchronised", offset=0.0, name="") + "III_CHRONY_SOURCES\n" + sources("1"),
            tracking(offset=7.5, name=GC_ADDRESS) + "III_CHRONY_SOURCES\n" + sources("17"),
            tracking(offset=0.0003, name=GC_ADDRESS) + "III_CHRONY_SOURCES\n" + sources("37"),
        ],
    )
    status, result = _sync(monkeypatch, tmp_path, aircraft, FakeRuntime())
    assert status == 0, result
    assert result["code"] == "III_CLOCK_SYNC_SETTLED"
    assert f"sudo -n chronyc add server {GC_ADDRESS} iburst" in aircraft.remote
    assert "sudo -n chronyc makestep" in aircraft.remote
    payload = result["payload"]
    assert payload["actions"] == [f"add-server {GC_ADDRESS}", "makestep"]
    assert payload["ground_computer_address"] == GC_ADDRESS
    assert payload["aircraft_address"] == PI_ADDRESS
    assert payload["source_reach"] == 0o37
    # The ground computer was only inspected, never reconfigured.
    assert [command[0] for command in aircraft.local] == ["chronyc", "ss"]
    assert aircraft.local[0] == ["chronyc", "-c", "tracking"]


@pytest.mark.parametrize(
    ("state", "reason"),
    [
        (vehicle(armed=True, in_air=True), "vehicle is armed"),
        (vehicle(in_air=True), "vehicle is in flight"),
        (vehicle(freshness="stale"), "vehicle state stale"),
        (vehicle(disagreement=True), "vehicle armed/in-air sources disagree"),
        ({"freshness": "unknown", "source_availability": "unavailable"}, "vehicle state unknown"),
        (vehicle(evidence=False) | {"freshness": "stale"}, "vehicle state stale"),
    ],
)
def test_the_clock_is_untouched_unless_the_aircraft_is_disarmed_and_landed(
    monkeypatch, tmp_path, clock, state, reason
):
    aircraft = FakeAircraft(clock, samples=[tracking(offset=5.0)])
    status, result = _sync(monkeypatch, tmp_path, aircraft, FakeRuntime(state=state))
    assert status == 20
    assert result["code"] == "III_CLOCK_SYNC_VEHICLE_NOT_SAFE"
    assert result["findings"][0]["message"].startswith(reason)
    assert aircraft.remote == []


def test_an_unreadable_runtime_fails_closed(monkeypatch, tmp_path, clock):
    aircraft = FakeAircraft(clock, samples=[tracking(offset=5.0)])
    runtime = FakeRuntime(error="Runtime API unavailable at http://iii.local:8765: [Errno 111] Connection refused")
    status, result = _sync(monkeypatch, tmp_path, aircraft, runtime)
    assert status == 20
    assert result["code"] == "III_CLOCK_SYNC_VEHICLE_STATE_UNKNOWN"
    assert aircraft.remote == []


def test_a_runtime_with_another_profile_is_refused(monkeypatch, tmp_path, clock):
    aircraft = FakeAircraft(clock, samples=[tracking(offset=5.0)])
    status, result = _sync(monkeypatch, tmp_path, aircraft, FakeRuntime(profile="real"))
    assert status == 20
    assert result["code"] == "III_CLOCK_SYNC_PROFILE_MISMATCH"
    assert aircraft.remote == []


@pytest.mark.parametrize(
    ("ground_tracking", "listening", "expected"),
    [
        ("missing", True, "chrony is not installed on this ground computer"),
        (tracking(leap="Not synchronised"), True, "own clock is not synchronized"),
        (tracking(), False, "does not serve NTP"),
    ],
)
def test_a_ground_computer_that_cannot_serve_ntp_is_reported(
    monkeypatch, tmp_path, clock, ground_tracking, listening, expected
):
    aircraft = FakeAircraft(
        clock,
        samples=[tracking(leap="Not synchronised", offset=0.0, name="")],
        ground_tracking=ground_tracking,
        listening=listening,
    )
    status, result = _sync(monkeypatch, tmp_path, aircraft, FakeRuntime())
    assert status == 30
    assert result["code"] == "III_CLOCK_SYNC_NTP_FALLBACK_UNAVAILABLE"
    message = result["findings"][0]["message"]
    assert expected in message
    assert f"allow {PI_ADDRESS}" in message
    assert not any("add server" in script for script in aircraft.remote)


def test_an_unanswered_ground_source_times_out_before_ground_control_stops_it(
    monkeypatch, tmp_path, clock
):
    unsynchronised = tracking(leap="Not synchronised", offset=0.0, name="")
    aircraft = FakeAircraft(
        clock, samples=[unsynchronised + "III_CHRONY_SOURCES\n" + sources("0")]
    )
    started = clock.now
    status, result = _sync(monkeypatch, tmp_path, aircraft, FakeRuntime())
    assert status == 30
    assert result["code"] == "III_CLOCK_SYNC_TIMEOUT"
    assert "never answered NTP" in result["findings"][0]["message"]
    assert f"allow {PI_ADDRESS}" in result["findings"][0]["message"]
    # Ground control kills the command at 45 s.
    assert clock.now - started < 45
    assert result["payload"]["elapsed_seconds"] <= host_clock.DEADLINE_SECONDS + 1


def test_a_pi_without_passwordless_sudo_is_reported(monkeypatch, tmp_path, clock):
    aircraft = FakeAircraft(
        clock, samples=[tracking(leap="Not synchronised", offset=0.0, name="")], add_rc=1
    )
    status, result = _sync(monkeypatch, tmp_path, aircraft, FakeRuntime())
    assert status == 30
    assert result["code"] == "III_CLOCK_SYNC_SOURCE_REJECTED"
    assert "password is required" in result["findings"][0]["message"]


def test_a_refused_step_is_reported(monkeypatch, tmp_path, clock):
    aircraft = FakeAircraft(clock, samples=[tracking(offset=2.0)], step_rc=1)
    status, result = _sync(monkeypatch, tmp_path, aircraft, FakeRuntime())
    assert status == 30
    assert result["code"] == "III_CLOCK_SYNC_STEP_REJECTED"
    assert "password is required" in result["findings"][0]["message"]


def test_an_unreachable_pi_fails_without_changes(monkeypatch, tmp_path, clock):
    aircraft = FakeAircraft(clock, samples=[tracking()], ssh_rc=255)
    status, result = _sync(monkeypatch, tmp_path, aircraft, FakeRuntime())
    assert status == 30
    assert result["code"] == "III_CLOCK_SYNC_SSH_FAILED"
    assert len(aircraft.remote) == 1


def test_confirmation_is_required_and_a_preview_contacts_nothing(monkeypatch, tmp_path, clock):
    aircraft = FakeAircraft(clock, samples=[tracking(offset=5.0)])
    runtime = FakeRuntime()
    argv = ["host", "clock", "sync", "--profile", "real", "--non-interactive", "--json"]
    status, result = _sync(monkeypatch, tmp_path, aircraft, runtime, argv=argv)
    assert status == 20
    assert result["code"] == "III_REQUIRED_INPUT"
    status, result = _sync(
        monkeypatch, tmp_path, aircraft, runtime,
        argv=["host", "clock", "sync", "--profile", "real", "--dry-run", "--json"],
    )
    assert status == 0
    assert result["code"] == "III_OPERATION_PLAN_READY"
    assert result["payload"]["plan"]["preflight"]["profile"] == "real"
    assert aircraft.remote == [] and aircraft.local == [] and runtime.base_urls == []


@pytest.mark.parametrize(
    "argv",
    [
        ["host", "clock", "sync", "--host", "iii.local", "--confirm", "--json"],
        ["host", "clock", "sync", "--profile", "hil", "--confirm", "--json"],
        ["host", "clock", "sync", "--profile", "real", "--host", "bad host", "--confirm", "--json"],
    ],
)
def test_usage_errors_exit_64(monkeypatch, tmp_path, clock, argv):
    aircraft = FakeAircraft(clock, samples=[tracking()])
    status, result = _sync(monkeypatch, tmp_path, aircraft, FakeRuntime(), argv=argv)
    assert status == 64
    assert result["outcome"] == "usage_error"
    assert aircraft.remote == []


def test_tracking_is_judged_like_the_runtime_clock_gate():
    assert host_clock.parse_tracking(tracking(offset=0.1)).settled is True
    assert host_clock.parse_tracking(tracking(offset=-0.1001)).settled is False
    assert host_clock.parse_tracking(tracking(leap="Not synchronised")).settled is False
    assert host_clock.parse_tracking("506 Cannot talk to daemon\n").settled is False
