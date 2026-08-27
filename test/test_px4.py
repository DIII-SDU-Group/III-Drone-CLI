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


def test_pull_uses_canonical_result_contract(monkeypatch):
    monkeypatch.setattr(px4, "_store", lambda _args: FakeStore())
    output = StringIO()
    code = main(
        ["px4", "params", "pull", "--profile", "real", "--json"],
        stdout=output,
        stderr=StringIO(),
    )
    assert code == 0
    assert '"code":"III_PX4_PULL"' in output.getvalue()
    assert '"writes_performed":0' in output.getvalue()


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
