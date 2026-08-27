from __future__ import annotations

from io import StringIO
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from iii.__main__ import main
from iii import deploy
from iii.operation import OperationStore, create_plan
from iii.runner import inventory_parser
from iii.__main__ import build_parser

IDENTITY = "a" * 64
CHECKPOINT = "b" * 64


def px4_activation_evidence() -> dict:
    return {
        "evidence_id": "6" * 64,
        "manifest_id": "7" * 64,
        "snapshot": {"snapshot_id": "8" * 64},
        "healthy": True,
        "writes_performed": 0,
    }


def target():
    return {
        "selector": "real",
        "endpoint": "iii.local",
        "logical_id": "drone",
        "execution_host": "aircraft",
        "runtime_profile": "real",
        "parameter_profile": "real",
        "simulator_provider": "none",
        "profile_alias": None,
        "bootable": True,
        "capabilities": ["flight"],
        "middleware_policy": "iii.middleware-interface-policy/v1",
    }


def canonical(path: Path, value: dict) -> None:
    path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def component(path: Path) -> None:
    path.mkdir(parents=True)
    release = {
        "release_id": IDENTITY,
        "release_class": "field-development",
        "source_identity": "c" * 64,
    }
    canonical(path / "release-manifest.json", release)
    canonical(path / "bundle.manifest.json", {"release_id": IDENTITY})
    (path / "bundle.tar.zst").write_bytes(b"archive")


class Transfer:
    upload_id = IDENTITY

    def as_dict(self):
        return {"transfer_id": "d" * 64, "upload_id": self.upload_id}


class Manager:
    client_id = "e" * 64

    def __init__(self, order):
        self.order = order

    def upload_bundle(self, *_args, **_kwargs):
        self.order.append("drone-transfer")
        return Transfer()

    def receiver_request(self, request):
        self.order.append(request["action"])
        if request["action"] == "status":
            return {
                "target": {"logical_id": "drone", "profile": "real"},
                "operation": {"state": "completed", "result": {}},
                "activation_safety": {
                    "profile": "real",
                    "runtime_api_available": True,
                    "runtime_identity_matches": True,
                    "runtime_fresh": True,
                    "px4_available": True,
                    "px4_fresh": True,
                    "armed": False,
                    "in_air": False,
                    "mission_fresh": True,
                    "mission_active": False,
                    "mission_control_owner": False,
                    "operation_fresh": True,
                    "custom_operation_active": False,
                    "custom_operation_control_owner": False,
                    "direct_operation_active": False,
                    "reference_owner_active": False,
                    "continuously_safe_for_s": 3.5,
                },
            }
        if request["action"].startswith("plan-"):
            return {
                "plan": {
                    "plan_id": "f" * 64,
                    "action": request["action"].removeprefix("plan-"),
                },
                "nonce": "1" * 64,
            }
        return {"detached": True, "operation": {"state": "accepted"}}


def test_legacy_destructive_sync_is_unavailable(monkeypatch, tmp_path):
    monkeypatch.setenv("CLI_CONFIGURATION", "dev")
    monkeypatch.setenv("III_OPERATION_STATE_DIR", str(tmp_path))
    output = StringIO()
    status = main(["deploy", "synchronize", "--json"], stdout=output, stderr=StringIO())
    assert status == 64
    assert json.loads(output.getvalue())["code"] == "III_USAGE_ERROR"


def test_deploy_continue_is_a_declared_mutating_leaf():
    inventory = inventory_parser(build_parser())
    assert inventory[("deploy", "continue")].mutating is True


def test_configuration_review_is_retained_and_continued_with_exact_decisions(
    monkeypatch, tmp_path
):
    class ReviewManager(Manager):
        def receiver_request(self, request):
            self.order.append(request["action"])
            if request["action"] == "plan-activate":
                activation = request["payload"]["activation"]
                decisions = activation.get("configuration_reconciliation_decisions", {})
                key = "tracked/default.yaml:/review_fixture/gain"
                return {
                    "plan": {
                        "plan_id": "f" * 64,
                        "action": "activate",
                        "parameters": activation,
                    },
                    "nonce": "1" * 64,
                    "preflight": {
                        "ready": decisions == {key: "use_old"},
                        "rejection_reasons": (
                            [] if decisions else ["configuration review is unresolved"]
                        ),
                        "configuration_reconciliation": {
                            "schema": "iii.receiver-configuration-reconciliation-preflight/v1",
                            "ready": bool(decisions),
                            "reconciliation_plan": {
                                "plan_id": "2" * 64,
                                "initial_state_id": "3" * 64,
                            },
                            "review_items": [
                                {
                                    "set_reference": "tracked/default.yaml",
                                    "parameter": "/review_fixture/gain",
                                    "old_canonical_value": 7.0,
                                    "new_default": 3.0,
                                    "old_value_valid": True,
                                    "old_value_selectable": True,
                                }
                            ],
                            "rejection_reasons": (
                                [] if decisions else ["review required"]
                            ),
                        },
                    },
                }
            if request["action"] == "activate":
                return {"detached": True, "operation": {"state": "accepted"}}
            return super().receiver_request(request)

    order = []
    manager = ReviewManager(order)
    environment = {"III_OPERATION_STATE_DIR": str(tmp_path / "operations")}
    monkeypatch.setattr(deploy, "_target", lambda _args: target())
    monkeypatch.setattr(deploy, "_manager", lambda: manager)
    monkeypatch.setattr(
        deploy,
        "_px4_activation_evidence",
        lambda *_args, **_kwargs: px4_activation_evidence(),
    )
    first = deploy.activate(
        SimpleNamespace(
            target="real",
            release_id=IDENTITY,
            configuration_checkpoint_id=CHECKPOINT,
            qualified=False,
            decision=[],
            _iii_operation_id="iii-review-source-0001",
            _iii_environment=environment,
        )
    )
    assert first.outcome.value == "rejected"
    assert first.code == "III_DEPLOY_CONFIGURATION_REVIEW_REQUIRED"
    review_path = (
        tmp_path
        / "operations/iii-review-source-0001/configuration-reconciliation-review.json"
    )
    review = json.loads(review_path.read_text())
    assert review["origin_command"] == "iii deploy activate"
    assert order == ["plan-activate"]

    continued = deploy.continue_configuration_review(
        SimpleNamespace(
            target="real",
            review_operation_id="iii-review-source-0001",
            decision=["tracked/default.yaml:/review_fixture/gain=use_old"],
            _iii_operation_id="iii-review-continue-0001",
            _iii_environment=environment,
        )
    )
    assert continued.outcome.value == "success", continued.findings[0].message
    assert continued.code == "III_DEPLOY_CONFIGURATION_REVIEW_CONTINUED"
    assert order == ["plan-activate", "plan-activate", "activate"]


def test_field_dry_run_retains_exact_plan_without_reading_bundle(monkeypatch, tmp_path):
    monkeypatch.setenv("CLI_CONFIGURATION", "dev")
    monkeypatch.setenv("III_OPERATION_STATE_DIR", str(tmp_path))
    output = StringIO()
    status = main(
        [
            "deploy",
            "field",
            "--bundle-set",
            str(tmp_path / "absent"),
            "--configuration-checkpoint-id",
            CHECKPOINT,
            "--component",
            "both",
            "--dry-run",
            "--json",
        ],
        stdout=output,
        stderr=StringIO(),
    )
    value = json.loads(output.getvalue())
    assert status == 0
    assert value["code"] == "III_OPERATION_PLAN_READY"
    assert value["context"]["target"] == "real"
    assert list(tmp_path.glob("*/plan.json"))


def _impact(*components):
    return {
        "schema": "iii.field-impact/v1",
        "impact_id": "2" * 64,
        "source_identity": "c" * 64,
        "components": list(components),
        "component_reasons": {name: ["source"] for name in components},
        "groups": {
            "missions": [],
            "behavior_trees": [],
            "parameters": [],
            "px4_manifest_drift": [],
        },
        "detail": {
            "detail_id": "3" * 64,
            "missions": {"entries": [], "behavior_trees": []},
            "parameters": {
                "manifest": {
                    "added": [],
                    "changed": [],
                    "removed": [],
                    "reintroduced": [],
                    "reintroduction_candidates": [],
                    "reintroduction_determination": "requires-target-legacy-shadow-review",
                    "defaults_changed": [],
                },
                "parameter_sets": [],
            },
        },
        "missions": {"inferred": [], "included": [], "excluded": [], "selected": []},
        "px4_write_planned": False,
        "ordering": [name for name in ("gc", "drone") if name in components],
        "resulting_identities": {
            "mission_catalog": "4" * 64,
            "configuration": "5" * 64,
            "component_source": "c" * 64,
        },
    }


def _field_args(tmp_path, bundle, *, activate=False):
    return SimpleNamespace(
        target="real",
        bundle_set=bundle,
        configuration_checkpoint_id=CHECKPOINT,
        status_index=None,
        trusted_signers=tmp_path / "trust.json",
        component=[],
        include_mission=[],
        exclude_mission=[],
        activate=activate,
        gc_override_reason=None,
        gc_override_confirmation=None,
        _iii_operation_id="iii-fake-field-operation",
        _iii_environment={"III_OPERATION_STATE_DIR": str(tmp_path / "operations")},
    )


def test_gc_only_field_flow_never_contacts_drone(monkeypatch, tmp_path):
    bundle = tmp_path / "bundle"
    component(bundle / "gc")
    order = []
    monkeypatch.setattr(deploy, "_target", lambda _args: target())
    monkeypatch.setattr(
        deploy,
        "_source_impact",
        lambda *_args: ({"content_identity": "c" * 64}, _impact("gc")),
    )
    monkeypatch.setattr(
        deploy,
        "_install_gc_application",
        lambda *_args, **_kwargs: order.append("gc")
        or {"state": "prepared", "release_id": IDENTITY},
    )
    monkeypatch.setattr(
        deploy,
        "_gc_application_store",
        lambda _args: SimpleNamespace(
            state=lambda: {"active_release_id": "9" * 64},
            release_manifest=lambda _release_id: {"release_id": IDENTITY},
        ),
    )
    monkeypatch.setattr(
        deploy,
        "_manager",
        lambda: (_ for _ in ()).throw(AssertionError("drone receiver contacted")),
    )

    result = deploy.field(_field_args(tmp_path, bundle))

    assert result.outcome.value == "success"
    assert order == ["gc"]
    assert [phase["name"] for phase in result.payload["actual"]["phases"]] == [
        "gc-stage",
        "drone-stage",
    ]
    assert result.payload["actual"]["phases"][1]["state"] == "skipped"


def test_drone_only_field_flow_never_reads_gc_bundle(monkeypatch, tmp_path):
    bundle = tmp_path / "bundle"
    component(bundle / "drone")
    order = []
    monkeypatch.setattr(deploy, "_target", lambda _args: target())
    monkeypatch.setattr(deploy, "_manager", lambda: Manager(order))
    monkeypatch.setattr(
        deploy,
        "_source_impact",
        lambda *_args: ({"content_identity": "c" * 64}, _impact("drone")),
    )
    monkeypatch.setattr(
        deploy,
        "_install_gc_application",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("GC handoff prepared")
        ),
    )

    result = deploy.field(_field_args(tmp_path, bundle))

    assert result.outcome.value == "success"
    assert order == ["drone-transfer", "plan-stage", "stage", "status"]
    assert result.payload["actual"]["phases"][0] == {
        "name": "gc-stage",
        "state": "skipped",
        "reason": "source impact does not require GC",
    }


def test_staged_gc_application_must_match_exact_release_identity(tmp_path):
    component_root = tmp_path / "gc-component"
    component(component_root)
    store = SimpleNamespace(stage=lambda _component: {"release_id": "9" * 64})

    with pytest.raises(ValueError, match="differs"):
        deploy._install_gc_application(
            component_root,
            release_id=IDENTITY,
            store=store,
        )


def _old_terminal_operation(root: Path, identifier: str, *, protected=False):
    store = OperationStore(root)
    plan = create_plan(
        identifier=identifier,
        argv=["fixture"],
        command="iii fixture",
        mutating=True,
        target=None,
        profile=None,
        release_id=None,
    )
    store.retain_plan(plan)
    state = store.load_state(identifier)
    state.update(
        state="completed",
        updated_at="2020-01-01T00:00:00Z",
        exit_code=0,
        result_code="III_FIXTURE_COMPLETE",
    )
    canonical(store.state_path(identifier), state)
    if protected:
        canonical(
            store.record_path(identifier, "evidence.json"),
            {"schema": "iii.fixture-evidence/v1", "protected": True},
        )


def test_prune_dry_run_binds_exact_candidates_and_preserves_protected_records(
    monkeypatch, tmp_path
):
    operations = tmp_path / "operations"
    _old_terminal_operation(operations, "iii-prune-candidate")
    _old_terminal_operation(operations, "iii-prune-protected", protected=True)
    cache = tmp_path / "cache/bundle.tar.zst"
    cache.parent.mkdir()
    cache.write_bytes(b"cache")
    monkeypatch.setenv("CLI_CONFIGURATION", "dev")
    monkeypatch.setenv("III_OPERATION_STATE_DIR", str(operations))
    output = StringIO()
    argv = [
        "deploy",
        "operations",
        "prune",
        "--days",
        "1",
        "--status",
        "completed",
    ]
    assert main([*argv, "--dry-run", "--json"], stdout=output, stderr=StringIO()) == 0
    planned = json.loads(output.getvalue())
    preflight = planned["payload"]["plan"]["preflight"]
    assert [item["operation_id"] for item in preflight["candidates"]] == [
        "iii-prune-candidate"
    ]
    assert preflight["protected"][0]["operation_id"] == "iii-prune-protected"
    operation_id = planned["operation"]["id"]

    output = StringIO()
    assert (
        main(
            [
                *argv,
                "--operation-id",
                operation_id,
                "--confirm",
                "--non-interactive",
                "--json",
            ],
            stdout=output,
            stderr=StringIO(),
        )
        == 0
    )
    assert not (operations / "iii-prune-candidate").exists()
    assert (operations / "iii-prune-protected").is_dir()
    assert cache.read_bytes() == b"cache"


def test_prune_apply_refuses_candidate_changed_after_dry_run(monkeypatch, tmp_path):
    operations = tmp_path / "operations"
    _old_terminal_operation(operations, "iii-prune-stale")
    monkeypatch.setenv("CLI_CONFIGURATION", "dev")
    monkeypatch.setenv("III_OPERATION_STATE_DIR", str(operations))
    argv = [
        "deploy",
        "operations",
        "prune",
        "--days",
        "1",
        "--status",
        "completed",
    ]
    output = StringIO()
    main([*argv, "--dry-run", "--json"], stdout=output, stderr=StringIO())
    operation_id = json.loads(output.getvalue())["operation"]["id"]
    canonical(
        operations / "iii-prune-stale/late.json",
        {"schema": "iii.fixture/v1", "value": "changed"},
    )

    output = StringIO()
    status = main(
        [
            *argv,
            "--operation-id",
            operation_id,
            "--confirm",
            "--non-interactive",
            "--json",
        ],
        stdout=output,
        stderr=StringIO(),
    )

    assert status == 20
    assert json.loads(output.getvalue())["code"] == "III_OPERATION_CONFLICT"
    assert (operations / "iii-prune-stale").is_dir()


def test_standalone_stage_and_activate_persist_self_identifying_actuals(
    monkeypatch, tmp_path
):
    component_root = tmp_path / "drone"
    component(component_root)
    manager = Manager([])
    monkeypatch.setattr(deploy, "_target", lambda _args: target())
    monkeypatch.setattr(deploy, "_manager", lambda: manager)
    monkeypatch.setattr(
        deploy,
        "_px4_activation_evidence",
        lambda *_args, **_kwargs: px4_activation_evidence(),
    )
    environment = {"III_OPERATION_STATE_DIR": str(tmp_path / "operations")}
    staged = deploy.stage(
        SimpleNamespace(
            target="real",
            component=component_root,
            status_index=None,
            _iii_operation_id="iii-standalone-stage",
            _iii_environment=environment,
        )
    )
    activated = deploy.activate(
        SimpleNamespace(
            target="real",
            release_id=IDENTITY,
            configuration_checkpoint_id=CHECKPOINT,
            qualified=False,
            _iii_operation_id="iii-standalone-activate",
            _iii_environment=environment,
        )
    )

    assert staged.outcome.value == activated.outcome.value == "success"
    for identifier in ("iii-standalone-stage", "iii-standalone-activate"):
        actual = json.loads(
            (tmp_path / f"operations/{identifier}/deployment-actual.json").read_text()
        )
        assert actual["operation_id"] == identifier
        assert actual["release_id"] == IDENTITY
        assert actual["target"]["endpoint"] == "iii.local"
        assert len(actual["actual_id"]) == 64


def test_configuration_capture_binds_active_release_identity(monkeypatch):
    class StatusManager:
        def verify_logical_target(self, **_kwargs):
            return {
                "live_state": {
                    "configuration_hash": CHECKPOINT,
                    "active_release_id": IDENTITY,
                }
            }

    monkeypatch.setattr(deploy, "_target", lambda _args: target())
    monkeypatch.setattr(deploy, "_manager", StatusManager)

    result = deploy.configuration_capture(SimpleNamespace(target="real"))

    assert result.outcome.value == "success"
    assert result.release_id == IDENTITY
    assert result.payload["active_release_id"] == IDENTITY


def test_fake_target_field_flow_is_gc_before_drone_and_never_writes_px4(
    monkeypatch, tmp_path
):
    bundle = tmp_path / "bundle"
    component(bundle / "drone")
    component(bundle / "gc")
    order = []
    manager = Manager(order)
    impact = _impact("drone", "gc")
    impact["component_reasons"] = {"drone": ["shared"], "gc": ["shared"]}
    impact["groups"] = {
        "missions": ["mission.yaml"],
        "behavior_trees": ["tree.xml"],
        "parameters": ["default.yaml"],
        "px4_manifest_drift": ["px4-manifest.yaml"],
    }
    monkeypatch.setattr(deploy, "_target", lambda _args: target())
    monkeypatch.setattr(deploy, "_manager", lambda: manager)
    monkeypatch.setattr(
        deploy,
        "_source_impact",
        lambda *_args: ({"content_identity": "c" * 64}, impact),
    )

    class GCStore:
        def state(self):
            return {"active_release_id": "9" * 64}

        def release_manifest(self, _release_id):
            return {"release_id": IDENTITY}

        def activate(self, *_args, **_kwargs):
            order.append("gc-activate")
            return {"state": "active", "release_id": IDENTITY}

    def gc_first(*_args, **_kwargs):
        order.append("gc-stage")
        return {"state": "staged", "release_id": IDENTITY}

    monkeypatch.setattr(deploy, "_gc_application_store", lambda _args: GCStore())
    monkeypatch.setattr(deploy, "_install_gc_application", gc_first)
    monkeypatch.setattr(
        deploy,
        "_px4_activation_evidence",
        lambda *_args, **_kwargs: px4_activation_evidence(),
    )
    args = SimpleNamespace(
        target="real",
        bundle_set=bundle,
        configuration_checkpoint_id=CHECKPOINT,
        status_index=None,
        trusted_signers=tmp_path / "trust.json",
        component=[],
        include_mission=[],
        exclude_mission=[],
        activate=True,
        gc_override_reason=None,
        gc_override_confirmation=None,
        _iii_operation_id="iii-fake-field-operation",
        _iii_environment={"III_OPERATION_STATE_DIR": str(tmp_path / "operations")},
    )
    result = deploy.field(args)
    assert result.outcome.value == "success"
    assert order == [
        "gc-stage",
        "status",
        "gc-activate",
        "drone-transfer",
        "plan-stage",
        "stage",
        "status",
        "plan-activate",
        "activate",
        "status",
    ]
    assert result.payload["actual"]["px4_write_performed"] is False
    assert result.payload["actual"]["phases"][3]["name"] == "px4-validate"
    assert result.payload["actual"]["phases"][3]["result"]["writes_performed"] == 0
    assert result.payload["impact"]["groups"]["px4_manifest_drift"]
    assert "Components: drone, gc" in result.payload["display"]
    assert "Actual phases: gc-stage=staged" in result.payload["display"]
    record = tmp_path / "operations/iii-fake-field-operation/deployment-actual.json"
    assert json.loads(record.read_text())["phases"][0]["name"] == "gc-stage"


def _compatibility_manifest(runtime_range: str):
    return {
        "compatibility": {
            "api_ranges": {"runtime_api": runtime_range},
            "schema_ranges": {"configuration": ">=1.0.0,<2.0.0"},
        },
        "qgc": {
            "selected_version": "5.0.8",
            "compatible_versions": ["5.0.8"],
        },
    }


@pytest.mark.parametrize("compatible", [True, False])
def test_drone_failure_reconciles_new_gc_against_authenticated_restored_drone(
    monkeypatch, tmp_path, compatible
):
    bundle = tmp_path / "bundle"
    component(bundle / "drone")
    component(bundle / "gc")
    order = []
    candidate = _compatibility_manifest(">=2.0.0,<3.0.0")
    restored = _compatibility_manifest(
        ">=2.2.0,<3.0.0" if compatible else ">=3.0.0,<4.0.0"
    )

    class FailureManager(Manager):
        def __init__(self, actions):
            super().__init__(actions)
            self.status_calls = 0

        def receiver_request(self, request):
            if request["action"] != "status":
                return super().receiver_request(request)
            self.order.append("status")
            self.status_calls += 1
            state = "failed" if self.status_calls == 2 else "completed"
            return {
                "target": {"logical_id": "drone", "profile": "real"},
                "operation": {"state": state, "failure": "simulated stage failure"},
                "activation_safety": Manager([]).receiver_request({"action": "status"})[
                    "activation_safety"
                ],
                "active_release_manifest": restored,
            }

    class GCStore:
        def state(self):
            return {"active_release_id": "9" * 64}

        def release_manifest(self, _release_id):
            return candidate

        def activate(self, *_args, **_kwargs):
            order.append("gc-activate")
            return {"state": "active"}

        def restore_release(self, release_id, **_kwargs):
            order.append(f"gc-restore:{release_id}")
            return {"state": "active", "release_id": release_id}

    manager = FailureManager(order)
    monkeypatch.setattr(deploy, "_target", lambda _args: target())
    monkeypatch.setattr(deploy, "_manager", lambda: manager)
    monkeypatch.setattr(
        deploy,
        "_source_impact",
        lambda *_args: ({"content_identity": "c" * 64}, _impact("drone", "gc")),
    )
    monkeypatch.setattr(deploy, "_gc_application_store", lambda _args: GCStore())
    monkeypatch.setattr(
        deploy,
        "_install_gc_application",
        lambda *_args, **_kwargs: {"state": "staged", "release_id": IDENTITY},
    )

    result = deploy.field(_field_args(tmp_path, bundle, activate=True))

    assert result.outcome.value == "rejected"
    if compatible:
        assert all(not item.startswith("gc-restore:") for item in order)
    else:
        assert f"gc-restore:{'9' * 64}" in order
    actual = json.loads(
        (
            tmp_path / "operations/iii-fake-field-operation/deployment-actual.json"
        ).read_text()
    )
    reconciliation = next(
        phase for phase in actual["phases"] if phase["name"] == "gc-reconcile"
    )
    assert reconciliation["state"] == (
        "retained-compatible" if compatible else "rolled-back"
    )


def test_gc_failure_leaves_drone_entirely_untouched(monkeypatch, tmp_path):
    bundle = tmp_path / "bundle"
    component(bundle / "drone")
    component(bundle / "gc")
    order = []

    class GCStore:
        def state(self):
            return {"active_release_id": "9" * 64}

        def release_manifest(self, _release_id):
            return _compatibility_manifest(">=2.0.0,<3.0.0")

        def activate(self, *_args, **_kwargs):
            raise ValueError("simulated GC health failure")

    manager = Manager(order)
    monkeypatch.setattr(deploy, "_target", lambda _args: target())
    monkeypatch.setattr(deploy, "_manager", lambda: manager)
    monkeypatch.setattr(
        deploy,
        "_source_impact",
        lambda *_args: ({"content_identity": "c" * 64}, _impact("drone", "gc")),
    )
    monkeypatch.setattr(deploy, "_gc_application_store", lambda _args: GCStore())
    monkeypatch.setattr(
        deploy,
        "_install_gc_application",
        lambda *_args, **_kwargs: {"state": "staged", "release_id": IDENTITY},
    )

    result = deploy.field(_field_args(tmp_path, bundle, activate=True))

    assert result.outcome.value == "rejected"
    assert "drone-transfer" not in order
    assert "plan-stage" not in order
    assert "stage" not in order
