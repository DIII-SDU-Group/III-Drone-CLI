from __future__ import annotations

from io import StringIO
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from iii.__main__ import main
from iii import deploy
from iii.operation import OperationStore, create_plan


IDENTITY = "a" * 64
CHECKPOINT = "b" * 64


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
        "_install_gc_handoff",
        lambda *_args, **_kwargs: order.append("gc")
        or {"state": "prepared", "release_id": IDENTITY},
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
        "gc",
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
        "_install_gc_handoff",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("GC handoff prepared")
        ),
    )

    result = deploy.field(_field_args(tmp_path, bundle))

    assert result.outcome.value == "success"
    assert order == ["drone-transfer", "plan-stage", "stage"]
    assert result.payload["actual"]["phases"][0] == {
        "name": "gc",
        "state": "skipped",
        "reason": "source impact does not require GC",
    }


def test_existing_gc_handoff_must_match_exact_release_manifest(tmp_path):
    component_root = tmp_path / "gc-component"
    component(component_root)
    destination = tmp_path / "gc-cache"
    installed = destination / IDENTITY / "META"
    installed.mkdir(parents=True)
    canonical(
        installed / "release-manifest.json",
        {
            "release_id": "9" * 64,
            "release_class": "field-development",
            "source_identity": "c" * 64,
        },
    )

    with pytest.raises(ValueError, match="different identity"):
        deploy._install_gc_handoff(
            component_root,
            release_id=IDENTITY,
            destination=destination,
            trusted_signers=tmp_path / "unused.json",
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

    def gc_first(*_args, **_kwargs):
        order.append("gc")
        return {"state": "prepared", "release_id": IDENTITY}

    monkeypatch.setattr(deploy, "_install_gc_handoff", gc_first)
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
        _iii_operation_id="iii-fake-field-operation",
        _iii_environment={"III_OPERATION_STATE_DIR": str(tmp_path / "operations")},
    )
    result = deploy.field(args)
    assert result.outcome.value == "success"
    assert order == [
        "gc",
        "drone-transfer",
        "plan-stage",
        "stage",
        "plan-activate",
        "activate",
    ]
    assert result.payload["actual"]["px4_write_performed"] is False
    assert result.payload["impact"]["groups"]["px4_manifest_drift"]
    assert "Components: drone, gc" in result.payload["display"]
    assert "Actual phases: gc=packaged" in result.payload["display"]
    record = tmp_path / "operations/iii-fake-field-operation/deployment-actual.json"
    assert json.loads(record.read_text())["phases"][0]["name"] == "gc"
