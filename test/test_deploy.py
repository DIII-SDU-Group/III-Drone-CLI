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


def component(
    path: Path,
    *,
    source_identity: str = "c" * 64,
    included_experimental: list[str] | None = None,
) -> None:
    path.mkdir(parents=True)
    included = included_experimental or []
    release = {
        "release_id": IDENTITY,
        "release_class": "field-development",
        "source_identity": source_identity,
        "mission_catalog": {
            "entries": ["inspection-production", *included],
            "included_experimental": included,
        },
    }
    canonical(path / "release-manifest.json", release)
    canonical(
        path / "bundle.manifest.json",
        {
            "release_id": IDENTITY,
            # Current bundle manifests enumerate archived entries. Older manifests
            # used a mapping with an optional archive_sha256 field.
            "content": [],
        },
    )
    (path / "bundle.tar.zst").write_bytes(b"archive")


def test_field_bundle_release_binds_source_components_and_missions(tmp_path):
    bundle = tmp_path / "bundle"
    component(bundle / "drone", included_experimental=["field-experiment"])
    component(bundle / "gc", included_experimental=["field-experiment"])

    release = deploy._field_bundle_release(
        bundle,
        components=["drone", "gc"],
        source_identity="c" * 64,
        selected_missions=["field-experiment"],
    )

    assert release["release_id"] == IDENTITY


@pytest.mark.parametrize(
    ("source_identity", "selected_missions", "match"),
    [
        ("d" * 64, ["field-experiment"], "source identity differs"),
        ("c" * 64, [], "mission selection differs"),
    ],
)
def test_field_bundle_release_rejects_stale_source_or_mission_selection(
    tmp_path, source_identity, selected_missions, match
):
    bundle = tmp_path / "bundle"
    component(bundle / "drone", included_experimental=["field-experiment"])

    with pytest.raises(ValueError, match=match):
        deploy._field_bundle_release(
            bundle,
            components=["drone"],
            source_identity=source_identity,
            selected_missions=selected_missions,
        )


def test_field_bundle_release_rejects_legacy_manifest_without_selection(tmp_path):
    bundle = tmp_path / "bundle"
    component(bundle / "drone")
    release_path = bundle / "drone/release-manifest.json"
    release = json.loads(release_path.read_text())
    del release["mission_catalog"]["entries"]
    del release["mission_catalog"]["included_experimental"]
    canonical(release_path, release)

    with pytest.raises(ValueError, match="rebuild it with current tooling"):
        deploy._field_bundle_release(
            bundle,
            components=["drone"],
            source_identity="c" * 64,
            selected_missions=[],
        )


def test_inspect_uses_explicit_bundle_trust_store(monkeypatch, tmp_path):
    from iii_deployment import bundle as bundle_module
    from iii_deployment import signers as signers_module

    trust = tmp_path / "trusted-signers.json"
    trust.write_text("{}\n", encoding="utf-8")
    observed = {}

    monkeypatch.setattr(deploy, "_target", lambda _args: target())
    monkeypatch.setattr(
        signers_module,
        "load_trusted_signers",
        lambda path, _registry: observed.setdefault("trust_path", path) or {},
    )
    monkeypatch.setattr(
        bundle_module,
        "inspect_bundle",
        lambda component_path, trusted, **_kwargs: SimpleNamespace(
            release_manifest={"release_id": IDENTITY},
            bundle_manifest={"release_id": IDENTITY},
        ),
    )

    result = deploy.inspect(
        SimpleNamespace(
            component=tmp_path / "component",
            target="real",
            trusted_signers=trust,
            _iii_environment={},
        )
    )

    assert result.code == "III_DEPLOY_BUNDLE_INSPECTED"
    assert observed["trust_path"] == trust.resolve()


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


def test_receiver_update_apply_uploads_plans_accepts_and_retains_exact_actual(
    monkeypatch, tmp_path
):
    bundle = tmp_path / "receiver-update"
    bundle.mkdir()
    (bundle / "receiver-update.tar").write_bytes(b"receiver archive")
    receiver_id = "7" * 64
    generation = 2
    order = []

    class ReceiverManager(Manager):
        def upload_receiver_update(self, received, **kwargs):
            assert received == bundle
            assert kwargs == {
                "receiver_id": receiver_id,
                "profile": "real",
                "operation_id": "iii-receiver-update-0001",
            }
            order.append("receiver-transfer")
            return Transfer()

    manager = ReceiverManager(order)
    monkeypatch.setattr(deploy, "_target", lambda _args: target())
    monkeypatch.setattr(deploy, "_manager", lambda: manager)
    monkeypatch.setattr(
        "iii_deployment.receiver.update.verify_receiver_update",
        lambda *_args, **_kwargs: SimpleNamespace(
            manifest={"receiver_id": receiver_id, "generation": generation},
            signature={},
        ),
    )
    environment = {"III_OPERATION_STATE_DIR": str(tmp_path / "operations")}

    result = deploy.receiver_update_apply(
        SimpleNamespace(
            target="real",
            bundle=bundle,
            trust=tmp_path / "trust.json",
            _iii_operation_id="iii-receiver-update-0001",
            _iii_environment=environment,
        )
    )

    assert result.outcome.value == "success", result.findings
    assert order == ["receiver-transfer", "plan-receiver-update", "receiver-update"]
    actual = json.loads(
        (
            tmp_path / "operations/iii-receiver-update-0001/receiver-update-actual.json"
        ).read_text()
    )
    assert actual == result.payload
    assert actual["receiver_id"] == receiver_id
    assert actual["generation"] == generation
    assert actual["transfer"]["upload_id"] == IDENTITY
    assert len(actual["actual_id"]) == 64


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


def test_field_dry_run_rejects_missing_bundle_during_preflight(monkeypatch, tmp_path):
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
    assert status == 20
    assert value["code"] == "III_OPERATION_ERROR"
    assert "release manifest is missing" in value["findings"][0]["message"]
    assert not list(tmp_path.glob("*/plan.json"))


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


def test_source_impact_rejects_local_test_mission_include(monkeypatch, tmp_path):
    from iii_deployment import field_impact as field_impact_module
    from iii_deployment import source as source_module

    monkeypatch.setattr(
        source_module,
        "load_source_policy",
        lambda *_args: {},
    )
    monkeypatch.setattr(
        source_module,
        "capture_source_snapshot",
        lambda *_args: {
            "content_identity": "c" * 64,
            "changed_paths": [],
            "impact": {"components": ["drone"], "causes": {"drone": ["source"]}},
        },
    )
    monkeypatch.setattr(
        source_module, "validate_component_selection", lambda *_args: None
    )
    monkeypatch.setattr(
        field_impact_module,
        "detailed_field_impact",
        lambda *_args: {
            "detail_id": "3" * 64,
            "missions": {
                "entries": [],
                "behavior_trees": [],
                "catalog_identity": "4" * 64,
            },
            "parameters": {
                "manifest": {},
                "parameter_sets": [],
                "configuration_identity": "5" * 64,
            },
        },
    )
    monkeypatch.setattr(
        field_impact_module,
        "mission_registry",
        lambda *_args: {
            "opti-track-up-down-test": {"classification": "test"},
            "field-experiment": {"classification": "experimental"},
        },
    )

    with pytest.raises(ValueError, match="only registered experimental"):
        deploy._source_impact(
            Path(__file__).resolve().parents[3],
            ["opti-track-up-down-test"],
            [],
            ["drone"],
        )


def _field_args(tmp_path, bundle, *, activate=False, checkpoint=CHECKPOINT):
    return SimpleNamespace(
        target="real",
        bundle_set=bundle,
        configuration_checkpoint_id=checkpoint,
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


def test_field_command_preflights_selection_before_confirmation():
    inventory = inventory_parser(build_parser())
    spec = inventory[("deploy", "field")]

    assert spec.mutating is True
    assert spec.plan_provider is deploy._field_preflight


def test_field_preflight_rejects_invalid_component_selection(monkeypatch, tmp_path):
    bundle = tmp_path / "bundle"
    args = _field_args(tmp_path, bundle)
    args.component = ["drone"]
    monkeypatch.setattr(deploy, "_target", lambda _args: target())
    monkeypatch.setattr(
        deploy, "_prevalidate_explicit_field_components", lambda _args: bundle
    )
    monkeypatch.setattr(
        deploy,
        "_source_impact",
        lambda *_args: (_ for _ in ()).throw(
            ValueError("unsafe manual component omission: gc")
        ),
    )

    with pytest.raises(ValueError, match="unsafe manual component omission: gc"):
        deploy._field_preflight(args)


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


def test_field_staging_does_not_require_an_activation_checkpoint(monkeypatch, tmp_path):
    bundle = tmp_path / "bundle"
    component(bundle / "gc")
    monkeypatch.setattr(deploy, "_target", lambda _args: target())
    monkeypatch.setattr(
        deploy,
        "_source_impact",
        lambda *_args: ({"content_identity": "c" * 64}, _impact("gc")),
    )
    monkeypatch.setattr(
        deploy,
        "_install_gc_application",
        lambda *_args, **_kwargs: {"state": "prepared", "release_id": IDENTITY},
    )
    monkeypatch.setattr(
        deploy,
        "_gc_application_store",
        lambda _args: SimpleNamespace(
            state=lambda: {"active_release_id": "9" * 64},
            release_manifest=lambda _release_id: {"release_id": IDENTITY},
        ),
    )

    result = deploy.field(_field_args(tmp_path, bundle, checkpoint=None))

    assert result.outcome.value == "success"
    plan = json.loads(
        (
            tmp_path / "operations/iii-fake-field-operation/deployment-impact.json"
        ).read_text()
    )
    assert plan["configuration_checkpoint_id"] is None


def test_field_activation_requires_a_configuration_checkpoint(monkeypatch, tmp_path):
    monkeypatch.setattr(deploy, "_target", lambda _args: target())

    result = deploy.field(
        _field_args(tmp_path, tmp_path / "absent", activate=True, checkpoint=None)
    )

    assert result.outcome.value == "rejected"
    assert result.code == "III_DEPLOY_CONTRACT_REJECTED"
    assert "configuration-checkpoint-id" in result.findings[0].message


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
    assert order == ["status", "drone-transfer", "plan-stage", "stage", "status"]
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


def test_standalone_stage_next_action_preserves_hil_target(monkeypatch, tmp_path):
    component_root = tmp_path / "drone"
    component(component_root)
    selected = {
        **target(),
        "selector": "hil",
        "runtime_profile": "hil",
        "parameter_profile": "sim",
    }
    monkeypatch.setattr(deploy, "_target", lambda _args: selected)
    monkeypatch.setattr(deploy, "_manager", lambda: Manager([]))

    result = deploy.stage(
        SimpleNamespace(
            target="hil",
            component=component_root,
            status_index=None,
            _iii_operation_id="iii-hil-stage",
            _iii_environment={"III_OPERATION_STATE_DIR": str(tmp_path / "operations")},
        )
    )

    assert result.outcome.value == "success"
    assert result.next_actions[0].command[-2:] == ("--target", "hil")


def test_px4_release_required_rejection_is_renderable():
    result = deploy._reject(
        "iii deploy field",
        deploy.PX4ReleaseRequiredError("firmware mismatch"),
        target=target(),
        release_id=IDENTITY,
    )

    rendered = json.loads(result.render_json())
    assert rendered["code"] == "III_PX4_RELEASE_REQUIRED"
    assert rendered["next_actions"][0]["command"][:4] == [
        "iii",
        "px4",
        "release",
        "prepare",
    ]


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


def test_field_redeploy_audits_px4_but_skips_activation_when_release_is_active(
    monkeypatch, tmp_path
):
    bundle = tmp_path / "bundle"
    component(bundle / "drone")
    impact = _impact("drone")
    impact["component_reasons"] = {"drone": ["shared"]}
    monkeypatch.setattr(deploy, "_target", lambda _args: target())
    monkeypatch.setattr(deploy, "_manager", lambda: object())
    monkeypatch.setattr(
        deploy,
        "_source_impact",
        lambda *_args: ({"content_identity": "c" * 64}, impact),
    )
    monkeypatch.setattr(
        deploy,
        "_stage_component",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("already-active release must not be staged again")
        ),
    )
    monkeypatch.setattr(
        deploy,
        "_remote_status",
        lambda *_args, **_kwargs: {
            "live_state": {"active_release_id": IDENTITY}
        },
    )
    audits = []
    monkeypatch.setattr(
        deploy,
        "_px4_activation_evidence",
        lambda *_args, **_kwargs: audits.append("px4")
        or px4_activation_evidence(),
    )
    monkeypatch.setattr(
        deploy,
        "_activation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("already-active release must not be activated again")
        ),
    )

    result = deploy.field(_field_args(tmp_path, bundle, activate=True))

    assert result.outcome.value == "success", result.findings
    assert audits == ["px4"]
    phases = result.payload["actual"]["phases"]
    assert next(item for item in phases if item["name"] == "px4-validate")[
        "state"
    ] == "completed"
    stage = next(item for item in phases if item["name"] == "drone-stage")
    assert stage == {
        "name": "drone-stage",
        "state": "skipped",
        "reason": "exact release is already active",
    }
    activate = next(item for item in phases if item["name"] == "drone-activate")
    assert activate == {
        "name": "drone-activate",
        "state": "skipped",
        "reason": "exact release is already active",
    }


def test_paired_redeploy_skips_exact_current_gc_and_drone_work(
    monkeypatch, tmp_path
):
    bundle = tmp_path / "bundle"
    component(bundle / "drone")
    component(bundle / "gc")

    class GCStore:
        def state(self):
            return {"active_release_id": IDENTITY}

        def release_manifest(self, _release_id):
            raise AssertionError("current GC release must not be reopened")

        def activate(self, *_args, **_kwargs):
            raise AssertionError("current GC release must not be reactivated")

    monkeypatch.setattr(deploy, "_target", lambda _args: target())
    monkeypatch.setattr(deploy, "_manager", lambda: object())
    monkeypatch.setattr(
        deploy,
        "_source_impact",
        lambda *_args: ({"content_identity": "c" * 64}, _impact("drone", "gc")),
    )
    monkeypatch.setattr(deploy, "_gc_application_store", lambda _args: GCStore())
    monkeypatch.setattr(
        deploy,
        "_install_gc_application",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("current GC release must not be restaged")
        ),
    )
    monkeypatch.setattr(
        deploy,
        "_stage_component",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("current drone release must not be restaged")
        ),
    )
    monkeypatch.setattr(
        deploy,
        "_remote_status",
        lambda *_args, **_kwargs: {
            "live_state": {"active_release_id": IDENTITY}
        },
    )
    monkeypatch.setattr(
        deploy,
        "_px4_activation_evidence",
        lambda *_args, **_kwargs: px4_activation_evidence(),
    )

    result = deploy.field(_field_args(tmp_path, bundle, activate=True))

    assert result.outcome.value == "success", result.findings
    phases = result.payload["actual"]["phases"]
    assert [(phase["name"], phase["state"]) for phase in phases] == [
        ("gc-stage", "skipped"),
        ("gc-activate", "skipped"),
        ("drone-stage", "skipped"),
        ("px4-validate", "completed"),
        ("drone-activate", "skipped"),
    ]


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


def test_receiver_px4_mismatch_uses_stable_release_required_error(monkeypatch):
    class Manager:
        def px4_audit(self, *, release_id, operation_id):
            assert release_id == IDENTITY
            assert operation_id == "px4-audit-operation"
            return {
                "audit": {
                    "healthy": False,
                    "findings": [
                        {"code": "PX4_COMMIT_MISMATCH", "detail": "wrong commit"}
                    ],
                    "writes_performed": 0,
                },
                "activation_evidence": None,
            }

    monkeypatch.setattr(deploy, "_manager", lambda: Manager())
    args = SimpleNamespace(_iii_operation_id="px4-audit-operation")
    with pytest.raises(deploy.PX4ReleaseRequiredError) as observed:
        deploy._px4_activation_evidence(
            args,
            selected={"parameter_profile": "real"},
            release_id=IDENTITY,
        )
    assert observed.value.code == "III_PX4_RELEASE_REQUIRED"
    assert "PX4_COMMIT_MISMATCH" in str(observed.value)


def test_receiver_px4_audit_accepts_remote_hil_target(monkeypatch):
    class Manager:
        def px4_audit(self, *, release_id, operation_id):
            assert release_id == IDENTITY
            assert operation_id == "hil-px4-audit-operation"
            return {
                "audit": {"healthy": True, "findings": [], "writes_performed": 0},
                "activation_evidence": {"healthy": True, "evidence_id": "e" * 64},
            }

    monkeypatch.setattr(deploy, "_manager", lambda: Manager())
    args = SimpleNamespace(_iii_operation_id="hil-px4-audit-operation")
    evidence = deploy._px4_activation_evidence(
        args,
        selected={"parameter_profile": "sim", "runtime_profile": "hil"},
        release_id=IDENTITY,
    )
    assert evidence["evidence_id"] == "e" * 64


def test_receiver_px4_audit_retries_transient_unreachable_result(monkeypatch):
    operation_ids = []

    class Manager:
        def px4_audit(self, *, release_id, operation_id):
            assert release_id == IDENTITY
            operation_ids.append(operation_id)
            if len(operation_ids) == 1:
                return {
                    "audit": {
                        "healthy": False,
                        "findings": [
                            {"code": "PX4_UNREACHABLE", "detail": "missed heartbeat"},
                            {
                                "code": "PX4_DDS_TOPIC_CONTRACT_UNPROVEN",
                                "detail": "identity was not observed",
                            },
                        ],
                    },
                    "activation_evidence": None,
                }
            return {
                "audit": {"healthy": True, "findings": [], "writes_performed": 0},
                "activation_evidence": {"healthy": True, "evidence_id": "e" * 64},
            }

    monkeypatch.setattr(deploy, "_manager", lambda: Manager())
    monkeypatch.setattr(deploy.time, "sleep", lambda _seconds: None)
    args = SimpleNamespace(_iii_operation_id="field-activation")
    evidence = deploy._px4_activation_evidence(
        args,
        selected={"parameter_profile": "sim", "runtime_profile": "hil"},
        release_id=IDENTITY,
    )
    assert evidence["evidence_id"] == "e" * 64
    assert operation_ids == ["field-activation", "field-activation-px4-audit-2"]


def test_receiver_px4_audit_does_not_retry_deterministic_drift(monkeypatch):
    calls = 0

    class Manager:
        def px4_audit(self, *, release_id, operation_id):
            nonlocal calls
            calls += 1
            return {
                "audit": {
                    "healthy": False,
                    "findings": [
                        {"code": "PX4_COMMIT_MISMATCH", "detail": "wrong commit"}
                    ],
                },
                "activation_evidence": None,
            }

    monkeypatch.setattr(deploy, "_manager", lambda: Manager())
    args = SimpleNamespace(_iii_operation_id="field-activation")
    with pytest.raises(deploy.PX4ReleaseRequiredError):
        deploy._px4_activation_evidence(
            args,
            selected={"parameter_profile": "sim", "runtime_profile": "hil"},
            release_id=IDENTITY,
        )
    assert calls == 1
