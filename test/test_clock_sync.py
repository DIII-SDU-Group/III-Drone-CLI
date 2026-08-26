from __future__ import annotations

from types import SimpleNamespace

from iii import ssh_manager, system
from iii.__main__ import build_parser


class FakeManager:
    client_id = "a" * 64

    def __init__(self):
        self.requests = []
        self.samples = 0

    def verify_logical_target(self, *, profile, operation_id):
        assert profile == "real"
        assert operation_id.endswith(str(self.samples))
        self.samples += 1
        return {
            "target": {"logical_id": "drone", "profile": "real"},
            "clock": {
                "boot_id": "boot-a",
                "gate": "DEGRADED_CLOCK",
                "target_monotonic_ns": 10_000_000_000 + self.samples,
                "target_wall_ns": 2_000_000_000 + self.samples,
            },
        }

    def receiver_request(self, request):
        self.requests.append(request)
        if request["action"] == "plan-clock-sync":
            return {
                "plan": {"plan_id": "b" * 64},
                "nonce": "c" * 64,
                "preflight": {"ready": True},
            }
        return {"detached": True, "operation": {"state": "accepted"}}


def test_clock_sync_collects_five_samples_and_uses_plan_apply(monkeypatch):
    manager = FakeManager()
    monkeypatch.setattr(ssh_manager, "SSHManager", lambda: manager)
    result = system.clock_sync(
        SimpleNamespace(
            target="real",
            profile="real",
            _iii_operation_id="iii-clock-operation",
        )
    )
    assert result.outcome.value == "success"
    assert manager.samples == 5
    assert [item["action"] for item in manager.requests] == [
        "plan-clock-sync",
        "clock-sync",
    ]
    assert len(manager.requests[0]["payload"]["samples"]) == 5
    assert result.payload["expected_target"] == result.payload["advertised_target"]


def test_clock_sync_refuses_sim_without_transport(monkeypatch):
    monkeypatch.setattr(
        ssh_manager,
        "SSHManager",
        lambda: (_ for _ in ()).throw(AssertionError("must not construct transport")),
    )
    result = system.clock_sync(
        SimpleNamespace(
            target="sim",
            profile="real",
            _iii_operation_id="iii-clock-operation",
        )
    )
    assert result.outcome.value == "rejected"
    assert result.code == "III_CLOCK_SYNC_REJECTED"


def test_boot_profile_is_explicit_per_command_and_does_not_mutate_environment(
    monkeypatch,
):
    monkeypatch.setenv("III_SYSTEM_PROFILE", "real")
    args = build_parser().parse_args(["system", "boot", "--profile", "opti_track"])
    assert system._requested_profile(args) == "opti_track"
    assert system._profile_name() == "real"
