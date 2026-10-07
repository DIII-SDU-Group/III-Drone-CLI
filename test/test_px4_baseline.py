"""`iii px4 param-baseline`: path selection, the diff, the gate, and verification."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from iii import px4_baseline
from iii.px4_mavlink import MavlinkError
from iii.result import Outcome
from iii.runtime_api_client import RuntimeApiError

BASELINE = "param set UXRCE_DDS_PRT 8888\nparam set UXRCE_DDS_DOM_ID 42\nparam set EKF2_EV_DELAY 30\nparam save\nreboot\n"
MATCHING = {"UXRCE_DDS_PRT": 8888, "UXRCE_DDS_DOM_ID": 42, "EKF2_EV_DELAY": 30.0}
HIL_STATE = {"UXRCE_DDS_PRT": 8889, "UXRCE_DDS_DOM_ID": 0, "EKF2_EV_DELAY": 0.0}


class _Link:
    def __init__(self, parameters, *, armed=False, landed=True, keeps=True):
        self.parameters, self.saved = dict(parameters), dict(parameters)
        self.armed, self.landed, self.keeps = armed, landed, keeps
        self.writes, self.reboots = [], 0

    def vehicle_state(self):
        return self.armed, self.landed

    def read_parameters(self, names):
        return {name: self.parameters.get(name) for name in names}

    def write_parameter(self, name, value):
        self.writes.append((name, value))
        self.parameters[name] = value
        if self.keeps:
            self.saved[name] = value

    def reboot(self):
        self.reboots += 1
        self.parameters = dict(self.saved)

    def close(self):
        pass


@pytest.fixture
def workspace(monkeypatch, tmp_path: Path):
    directory = tmp_path / "deployment/px4"
    directory.mkdir(parents=True)
    for name in ("opti-track.nsh", "real.nsh", "hil-ethernet.nsh"):
        (directory / name).write_text(BASELINE, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(px4_baseline.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(px4_baseline.os.path, "exists", lambda _path: True)
    return tmp_path


def _args(**overrides):
    values = {"profile": "opti_track", "host": "pi.local", "usb_device": None, "ros_domain_id": None}
    return SimpleNamespace(**{**values, **overrides})


def _pi(monkeypatch, state=None, error=None, apply=None):
    class Client:
        def px4_parameter_baseline(self):
            if error:
                raise RuntimeApiError(error)
            return state

        def apply_px4_parameter_baseline(self):
            return apply

    monkeypatch.setattr(px4_baseline, "_api", lambda args, timeout: Client())


def _usb(monkeypatch, link, device="/dev/serial/by-id/usb-PX4"):
    monkeypatch.setattr(px4_baseline.glob, "glob", lambda _pattern: [device] if device else [])
    if isinstance(link, Exception):
        def refuse(_device):
            raise link
        monkeypatch.setattr(px4_baseline, "_connect_usb", refuse)
    else:
        monkeypatch.setattr(px4_baseline, "_connect_usb", lambda _device: link)


def _run(args):
    args._iii_retained_plan = {"preflight": px4_baseline.plan(args)}
    return px4_baseline.apply(args)


PI_READY = {
    "profile": "opti_track", "applicable": True, "checked": True, "ros_domain_id": 42,
    "mismatches": [{"name": "UXRCE_DDS_PRT", "expected": 8888, "actual": 8889}],
}


def test_the_pi_is_used_when_it_runs_the_profile_and_has_a_px4_link(monkeypatch, workspace, capsys):
    changed = PI_READY["mismatches"]
    _pi(monkeypatch, PI_READY, apply={"accepted": True, "result": {"changed": changed}})
    _usb(monkeypatch, AssertionError("USB must not be touched"))
    plan = px4_baseline.plan(_args())
    assert plan["path"] == "pi" and plan["changes"] == changed
    # The difference is shown before anything is written.
    assert "UXRCE_DDS_PRT  8889 -> 8888" in capsys.readouterr().err
    result = _run(_args())
    assert result.outcome is Outcome.SUCCESS and result.code == "III_PX4_BASELINE_APPLIED"


def test_the_pis_gate_rejection_is_reported(monkeypatch, workspace):
    _pi(monkeypatch, PI_READY, apply={"accepted": False, "message": "vehicle is armed"})
    result = _run(_args())
    assert result.outcome is Outcome.REJECTED
    assert result.findings[0].message == "vehicle is armed"


@pytest.mark.parametrize(
    "pi",
    [
        {"error": "Runtime API unavailable"},
        {"state": {**PI_READY, "profile": "hil"}},
        {"state": {**PI_READY, "checked": False, "detail": "the PX4 MAVLink link is not usable"}},
        {"state": {**PI_READY, "applicable": False, "detail": "the PX4 is simulated"}},
    ],
)
def test_usb_is_the_fallback_when_the_pi_has_no_link_for_the_profile(monkeypatch, workspace, pi):
    _pi(monkeypatch, **pi)
    link = _Link(HIL_STATE)
    _usb(monkeypatch, link)
    result = _run(_args())
    assert result.code == "III_PX4_BASELINE_APPLIED"
    assert result.payload["path"] == "usb" and result.payload["pi_unavailable"]
    # Written with PX4's own types, rebooted once, and verified afterwards.
    assert link.writes == [("UXRCE_DDS_PRT", 8888), ("UXRCE_DDS_DOM_ID", 42), ("EKF2_EV_DELAY", 30.0)]
    assert isinstance(link.writes[2][1], float)
    assert link.reboots == 1


def test_no_pi_link_and_no_usb_fails_loudly_naming_both(monkeypatch, workspace):
    _pi(monkeypatch, error="Runtime API unavailable at http://pi.local:8765")
    _usb(monkeypatch, None, device=None)
    with pytest.raises(RuntimeError) as failure:
        px4_baseline.plan(_args())
    message = str(failure.value)
    assert "no path to the flight controller" in message
    assert "Runtime API unavailable" in message and "USB: no device" in message

    _usb(monkeypatch, MavlinkError("no PX4 heartbeat was received"))
    with pytest.raises(RuntimeError, match="no PX4 heartbeat was received"):
        px4_baseline.plan(_args())


@pytest.mark.parametrize(
    ("armed", "landed", "reason"),
    [(True, True, "vehicle is armed"), (False, False, "vehicle is not landed"),
     (None, True, "PX4 did not report its armed and landed state")],
)
def test_usb_writes_nothing_unless_px4_reports_disarmed_and_landed(
    monkeypatch, workspace, armed, landed, reason
):
    _pi(monkeypatch, error="down")
    link = _Link(HIL_STATE, armed=armed, landed=landed)
    _usb(monkeypatch, link)
    result = _run(_args())
    assert result.outcome is Outcome.REJECTED and result.findings[0].message == reason
    assert link.writes == [] and link.reboots == 0


def test_a_matching_flight_controller_is_left_alone(monkeypatch, workspace):
    _pi(monkeypatch, error="down")
    link = _Link(MATCHING)
    _usb(monkeypatch, link)
    result = _run(_args())
    assert result.code == "III_PX4_BASELINE_MATCHES"
    assert link.writes == [] and link.reboots == 0


def test_a_baseline_lost_over_the_reboot_is_a_failure(monkeypatch, workspace):
    _pi(monkeypatch, error="down")
    _usb(monkeypatch, _Link(HIL_STATE, keeps=False))
    result = _run(_args())
    assert result.outcome is Outcome.FAILED
    assert "lost the baseline over its reboot" in result.findings[0].message


def test_usb_takes_the_stack_domain_from_the_pi_or_the_option(monkeypatch, workspace):
    _pi(monkeypatch, state={**PI_READY, "checked": False, "ros_domain_id": 17})
    _usb(monkeypatch, _Link(MATCHING))
    assert px4_baseline.plan(_args())["ros_domain_id"] == 17
    assert px4_baseline.plan(_args(ros_domain_id=5))["ros_domain_id"] == 5
    _pi(monkeypatch, error="down")
    assert px4_baseline.plan(_args())["ros_domain_id"] == 42


def test_cli_profile_lists_mirror_the_contract(workspace):
    import subprocess
    import sys

    from iii import system
    from iii_drone_contracts.px4_parameters import BASELINE_FILES, CHECKED_PROFILES

    assert set(px4_baseline.PROFILES) == set(BASELINE_FILES)
    assert set(system._PX4_CHECKED_PROFILES) == set(CHECKED_PROFILES)
    # The Pi's CLI starts on a Python that cannot import the Contracts package.
    probe = (
        "import sys; sys.modules['iii_drone_contracts'] = None; "
        "import iii.px4, iii.system, iii.host, iii.deploy"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True,
        cwd=str(Path(px4_baseline.__file__).resolve().parents[1]),
    )
    assert completed.returncode == 0, completed.stderr
