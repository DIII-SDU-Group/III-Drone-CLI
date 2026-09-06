from __future__ import annotations

from io import StringIO
from types import SimpleNamespace

from iii import px4
from iii.__main__ import build_parser, main
from iii.runner import inventory_parser


class FakeAdapter:
    def status(self):
        return {
            "connected": True,
            "armed": False,
            "system_id": 1,
            "component_id": 1,
            "firmware_version": "1.16.1",
            "firmware_commit": "deadbeef",
        }


class FakeStore:
    def __init__(self):
        self.adapter = FakeAdapter()
        self.plan_value = {
            "schema": "iii.px4-parameter-plan/v1",
            "plan_id": "a" * 64,
            "profile": "real",
            "changes": [{"name": "COM_RC_IN_MODE"}],
        }

    def pull(self, profile):
        return {
            "schema": "iii.px4-parameter-snapshot/v1",
            "snapshot_id": "b" * 64,
            "profile": profile,
        }

    def compare(self, profile, snapshot_id):
        return {
            "profile": profile,
            "snapshot_id": snapshot_id,
            "required_match": True,
        }

    def load_plan(self, plan_id):
        assert plan_id == self.plan_value["plan_id"]
        return self.plan_value

    def apply(self, plan_id, confirmed_keys):
        assert plan_id == self.plan_value["plan_id"]
        assert confirmed_keys == ["COM_RC_IN_MODE"]
        return {
            "schema": "iii.px4-parameter-apply-result/v1",
            "outcome": "applied",
        }


def test_parser_inventory_covers_every_px4_leaf_and_mutation_contract():
    inventory = inventory_parser(build_parser())
    leaves = {
        path[-1]: spec
        for path, spec in inventory.items()
        if path[:2] == ("px4", "params")
    }
    assert set(leaves) == {
        "pull",
        "plan",
        "apply",
        "verify",
        "capture",
        "list",
        "show",
        "diff",
        "export",
        "import",
        "promote",
    }
    assert leaves["apply"].mutating and leaves["apply"].plan_provider
    assert leaves["promote"].mutating and leaves["promote"].plan_provider
    assert not leaves["pull"].mutating
    assert not leaves["verify"].mutating
    release_leaves = {
        path[-1]: spec
        for path, spec in inventory.items()
        if path[:2] == ("px4", "release")
    }
    assert set(release_leaves) == {"prepare", "audit"}
    assert release_leaves["prepare"].mutating
    assert release_leaves["prepare"].plan_provider
    assert not release_leaves["audit"].mutating


def test_pull_uses_canonical_result_contract(monkeypatch):
    monkeypatch.setattr(px4, "_store", lambda _args: FakeStore())
    output = StringIO()
    code = main(
        [
            "px4",
            "params",
            "pull",
            "--profile",
            "sim",
            "--json",
        ],
        stdout=output,
        stderr=StringIO(),
    )
    assert code == 0
    assert '"code":"III_PX4_PULL"' in output.getvalue()
    assert '"writes_performed":0' in output.getvalue()


def test_real_pull_uses_receiver_owned_ethernet_snapshot(monkeypatch):
    subject = FakeStore()
    snapshot = {
        "schema": "iii.px4-parameter-snapshot/v1",
        "snapshot_id": "b" * 64,
        "profile": "real",
    }
    subject.retain_snapshot = lambda value: snapshot
    monkeypatch.setattr(px4, "_store", lambda _args: subject)

    class Manager:
        def px4_audit(self, *, release_id, operation_id):
            assert release_id == "a" * 64 and operation_id
            return {"activation_evidence": {"snapshot": snapshot}}

    monkeypatch.setattr("iii.ssh_manager.SSHManager", Manager)
    result = px4.pull(SimpleNamespace(profile="real", release_id="a" * 64))
    assert result.code == "III_PX4_PULL"


def test_hil_sim_pull_uses_receiver_owned_ethernet_snapshot(monkeypatch):
    subject = FakeStore()
    snapshot = {
        "schema": "iii.px4-parameter-snapshot/v1",
        "snapshot_id": "c" * 64,
        "profile": "sim",
    }
    subject.retain_snapshot = lambda value: snapshot
    monkeypatch.setattr(px4, "_store", lambda _args: subject)

    class Manager:
        def px4_audit(self, *, release_id, operation_id):
            assert release_id == "a" * 64 and operation_id
            return {"activation_evidence": {"snapshot": snapshot}}

    monkeypatch.setattr("iii.ssh_manager.SSHManager", Manager)
    result = px4.pull(SimpleNamespace(profile="sim", release_id="a" * 64))
    assert result.code == "III_PX4_PULL"


def test_release_audit_generates_receiver_correlation_id_without_mutation_controls(
    monkeypatch,
):
    observed = []

    class Manager:
        def px4_audit(self, *, release_id, operation_id):
            observed.append((release_id, operation_id))
            return {"audit": {"healthy": True, "findings": []}}

    monkeypatch.setattr("iii.ssh_manager.SSHManager", Manager)
    output = StringIO()
    code = main(
        ["px4", "release", "audit", "--release-id", "a" * 64, "--json"],
        stdout=output,
        stderr=StringIO(),
    )

    assert code == 0
    assert observed[0][0] == "a" * 64
    assert observed[0][1].startswith("iii-")
    assert len(observed[0][1]) == 28
    assert '"code":"III_PX4_RELEASE_MATCH"' in output.getvalue()


def test_apply_reauthenticates_exact_retained_plan_and_disarmed_target(monkeypatch):
    subject = FakeStore()
    monkeypatch.setattr(px4, "_store", lambda _args: subject)
    args = SimpleNamespace(
        plan_id="a" * 64,
        key=["COM_RC_IN_MODE"],
        _iii_retained_plan=None,
    )
    preflight = px4.apply_preflight(args)
    args._iii_retained_plan = {"preflight": preflight}
    result = px4.apply(args)
    assert result.outcome.value == "success"
    assert result.payload["outcome"] == "applied"
    subject.adapter.status = lambda: {**preflight["status"], "armed": True}
    rejected = px4.apply(args)
    assert rejected.outcome.value == "rejected"


def test_all_defaults_selects_complete_non_calibration_manifest_values():
    class Store:
        def load_capture(self, capture_id):
            assert capture_id == "c" * 64
            return {"snapshot_id": "s" * 64}

        def load_snapshot(self, snapshot_id):
            assert snapshot_id == "s" * 64
            return {
                "profile": "real",
                "parameters": [
                    {"name": "NAV_ACC_RAD"},
                    {"name": "CAL_ACC0_ID"},
                    {"name": "COM_MODE0_HASH"},
                ],
            }

        def manifest(self, profile):
            assert profile == "real"
            return {
                "parameters": [
                    {"name": "NAV_ACC_RAD", "classification": "operator-tunable"},
                    {"name": "CAL_ACC0_ID", "classification": "calibration-identity"},
                    {"name": "MISSING", "classification": "release-required"},
                ]
            }

    args = SimpleNamespace(capture_id="c" * 64, all_defaults=True, key=None)
    assert px4._promotion_keys(Store(), args) == ["COM_MODE0_HASH", "NAV_ACC_RAD"]
