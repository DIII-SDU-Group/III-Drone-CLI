from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from iii import field, registry, release
from iii.__main__ import build_parser
from iii.runner import inventory_parser


RELEASE_ID = "a" * 64


def test_field_parser_declares_offline_verify_read_only() -> None:
    inventory = inventory_parser(build_parser())
    assert inventory[("field", "verify")].mutating is False


def test_connected_readiness_parser_accepts_split_hil_aircraft_target() -> None:
    arguments = build_parser().parse_args(["field", "check", "--target", "hil"])
    assert arguments.target == "hil"


def target():
    return {
        "selector": "real",
        "endpoint": "iii.local",
        "logical_id": "drone",
        "runtime_profile": "real",
    }


def cached(tmp_path: Path, *, status: str = "qualified"):
    return SimpleNamespace(
        root=tmp_path / "v1.2.3/release",
        publication={
            "release_id": RELEASE_ID,
            "components": {"drone": {}, "gc": {}},
        },
        record={"record_id": "e" * 64, "schema": "iii.release-record/v1"},
        status={
            "status": status,
            "statement_id": "b" * 64,
        },
        status_index={
            "index_id": "c" * 64,
            "generated_at": "2099-01-01T00:00:00Z",
        },
    )


def prepare_args(tmp_path: Path, *, offline=False):
    return SimpleNamespace(
        target="real",
        version=["v1.2.3"],
        offline=offline,
        _iii_operation_id="iii-field-prepare-test",
        _iii_environment={
            "III_OPERATION_STATE_DIR": str(tmp_path / "operations"),
            "III_REGISTRY_ROOT": str(tmp_path / "registry"),
        },
    )


def test_online_prepare_refreshes_monotonic_status_and_seals_completeness(
    monkeypatch, tmp_path
):
    value = cached(tmp_path)
    calls = []
    runtime = {
        "source": SimpleNamespace(
            latest_status_index=lambda: calls.append("online") or b"status"
        ),
        "refresh_cached_status": lambda *args, **kwargs: calls.append("refresh"),
        "status_trust": object(),
        "registry": object(),
    }
    monkeypatch.setattr(field, "_target", lambda _args: target())
    monkeypatch.setattr(release, "_runtime", lambda _args: runtime)
    monkeypatch.setattr(release, "_load_cached", lambda *_args: value)
    result = field.prepare(prepare_args(tmp_path))
    assert result.outcome.value == "success"
    assert calls == ["online", "refresh"]
    assert result.payload["complete"] is True
    assert Path(result.evidence[0]).is_file()
    assert Path(result.evidence[1]).is_file()


def test_offline_prepare_never_refreshes_and_withdrawal_cannot_become_deployable(
    monkeypatch, tmp_path
):
    value = cached(tmp_path, status="withdrawn")
    source = SimpleNamespace(
        latest_status_index=lambda: (_ for _ in ()).throw(
            AssertionError("offline mode must not use network")
        )
    )
    monkeypatch.setattr(field, "_target", lambda _args: target())
    monkeypatch.setattr(release, "_runtime", lambda _args: {"source": source})
    monkeypatch.setattr(release, "_load_cached", lambda *_args: value)
    result = field.prepare(prepare_args(tmp_path, offline=True))
    assert result.outcome.value == "rejected"
    assert "withdrawn" in result.findings[0].message


def test_offline_old_verified_status_warns_but_does_not_expire(monkeypatch, tmp_path):
    value = cached(tmp_path)
    value.status_index["generated_at"] = "2020-01-01T00:00:00Z"
    monkeypatch.setattr(field, "_target", lambda _args: target())
    monkeypatch.setattr(release, "_runtime", lambda _args: {})
    monkeypatch.setattr(release, "_load_cached", lambda *_args: value)
    result = field.prepare(prepare_args(tmp_path, offline=True))
    assert result.outcome.value == "warning"
    assert result.code == "III_FIELD_CACHE_PREPARED_STATUS_STALE"
    assert result.payload["complete"] is True
    assert result.findings[0].code == "FIELD.RELEASE_STATUS.STALE"


def test_invalid_cached_status_signature_is_visible_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(field, "_target", lambda _args: target())
    monkeypatch.setattr(release, "_runtime", lambda _args: {})
    monkeypatch.setattr(
        release,
        "_load_cached",
        lambda *_args: (_ for _ in ()).throw(
            ValueError("release-status index signature is invalid")
        ),
    )
    result = field.prepare(prepare_args(tmp_path, offline=True))
    assert result.outcome.value == "rejected"
    assert "signature is invalid" in result.findings[0].message


def verify_args(tmp_path: Path, *, offline: bool = True):
    return SimpleNamespace(
        target="real",
        version=["v1.2.3"],
        offline=offline,
        _iii_environment={
            "III_OPERATION_STATE_DIR": str(tmp_path / "operations"),
            "III_REGISTRY_ROOT": str(tmp_path / "registry"),
        },
    )


def test_offline_verify_proves_gc_drone_and_paired_without_network_or_target(
    monkeypatch, tmp_path
):
    value = cached(tmp_path)
    source = SimpleNamespace(
        latest_status_index=lambda: (_ for _ in ()).throw(
            AssertionError("offline verification must not use network")
        )
    )
    monkeypatch.setattr(field, "_target", lambda _args: target())
    monkeypatch.setattr(release, "_runtime", lambda _args: {"source": source})
    monkeypatch.setattr(release, "_load_cached", lambda *_args: value)
    result = field.verify_offline(verify_args(tmp_path))
    assert result.outcome.value == "success"
    assert result.code == "III_FIELD_OFFLINE_VERIFIED"
    assert [row["scenario"] for row in result.payload["scenarios"]] == [
        "gc-only",
        "drone-only",
        "paired",
    ]
    assert all(row["network_access"] is False for row in result.payload["scenarios"])
    assert all(row["target_mutation"] is False for row in result.payload["scenarios"])
    assert Path(result.evidence[0]).is_file()


def test_field_verify_requires_explicit_offline_and_complete_pair(
    monkeypatch, tmp_path
):
    value = cached(tmp_path)
    monkeypatch.setattr(field, "_target", lambda _args: target())
    monkeypatch.setattr(release, "_runtime", lambda _args: {})
    monkeypatch.setattr(release, "_load_cached", lambda *_args: value)
    assert (
        field.verify_offline(verify_args(tmp_path, offline=False)).outcome.value
        == "rejected"
    )
    value.publication["components"] = {"gc": {}}
    result = field.verify_offline(verify_args(tmp_path / "missing"))
    assert result.outcome.value == "rejected"
    assert "missing drone" in result.findings[0].message


def observations(**changes):
    value = {
        "boot_id": "boot-a",
        "drone_release_id": RELEASE_ID,
        "gc_release_id": "d" * 64,
        "profile": "real",
        "configuration_hash": "e" * 64,
        "commissioning_hash": "f" * 64,
        "px4_required_state_hash": "1" * 64,
        "mission_id": "inspection",
        "qgc_pair_id": "qgc-1",
    }
    checks = (
        "commissioning_valid",
        "release_pair_compatible",
        "clock_gate_valid",
        "receiver_available",
        "control_plane_available",
        "required_hardware_ready",
        "px4_firmware_matches",
        "px4_required_parameters_match",
        "parameter_reconciliation_complete",
        "selected_mission_valid",
        "storage_reserve_valid",
        "credentials_valid",
        "runtime_healthy",
        "cold_restart_clear",
        "qgc_managed_settings_match",
        "optional_hardware_ready",
        "backup_fresh",
        "external_archive_recent",
        "offline_cache_fresh",
        "logging_capacity_ready",
    )
    value.update({name: True for name in checks})
    value.update(changes)
    return value


def check_args(tmp_path: Path, state: dict, *, registry_root: Path | None = None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "state.json"
    path.write_text(json.dumps(state), encoding="utf-8")
    environment = {
        "III_OPERATION_STATE_DIR": str(tmp_path / "operations"),
        "III_REGISTRY_ROOT": str(registry_root or tmp_path / "registry"),
    }
    return SimpleNamespace(
        target="real",
        state=path,
        signing_key=None,
        _iii_environment=environment,
    )


def test_check_exit_families_and_sealed_records(monkeypatch, tmp_path):
    monkeypatch.setattr(field, "_target", lambda _args: target())
    passed = field.check(check_args(tmp_path / "pass", observations()))
    warned = field.check(
        check_args(tmp_path / "warn", observations(backup_fresh=False))
    )
    failed = field.check(
        check_args(tmp_path / "fail", observations(clock_gate_valid=False))
    )
    assert (passed.exit_code, warned.exit_code, failed.exit_code) == (0, 10, 30)
    assert passed.state == warned.state == failed.state == "sealed"
    assert Path(failed.evidence[0]).is_file()
    assert Path(failed.evidence[1]).is_file()
    assert failed.payload["authorization"] is False


def test_check_seals_split_hil_aircraft_readiness(monkeypatch, tmp_path):
    selected = {
        "selector": "hil",
        "endpoint": "iii.local",
        "logical_id": "drone",
        "runtime_profile": "hil",
    }
    monkeypatch.setattr(field, "_target", lambda _args: selected)
    state = observations(profile="hil")
    arguments = check_args(tmp_path / "hil", state)
    arguments.target = "hil"

    result = field.check(arguments)

    assert result.exit_code == 0
    assert result.profile == "hil"
    assert result.payload["target"]["profile"] == "hil"


def _live_receiver(release_id: str = RELEASE_ID):
    return {
        "boot_id": "boot-hil",
        "live_state": {
            "active_release_id": release_id,
            "configuration_hash": "c" * 64,
            "commissioning_hash": "d" * 64,
        },
        "active_release_manifest": {
            "release_id": release_id,
            "profiles": [
                {
                    "id": "hil",
                    "status": "commissioned",
                    "parameter_profile": "sim",
                    "health": {
                        "required_hardware_roles": [],
                        "optional_hardware_roles": [],
                        "required_managed_nodes": {"mission_executor": "active"},
                        "required_services": ["micro_ros_agent"],
                    },
                }
            ],
        },
        "clock": {"gate": "OPERATIONAL", "fault": None},
        "recovery": {"flight_capable": True, "recovery_only": False},
        "portable_backup": {"backup_fresh": True},
    }


def _runtime_response(command: str):
    if command == "runtime.status":
        return {
            "accepted": True,
            "result": {
                "daemon": {
                    "booted": True,
                    "profile": "hil",
                    "managed_nodes": {"mission_executor": "active"},
                    "processes": {
                        "mission_executor": {
                            "alive": True,
                            "recovery_in_progress": False,
                        }
                    },
                    "services": {
                        "micro_ros_agent": {
                            "alive": True,
                            "ready": True,
                        }
                    },
                }
            },
        }
    return {
        "accepted": True,
        "result": {
            "status": {
                "specification": {
                    "catalog_id": "inspection-production",
                    "catalog_ready": True,
                    "active_profile": "hil",
                },
                "required_modes_registered": True,
                "preflight": {"items": [{"key": "storage", "passed": True}]},
            }
        },
    }


def test_live_hil_observations_use_authenticated_subsystem_facts(monkeypatch):
    import iii.gc_application as gc_module
    import iii.runtime_api_client as runtime_module
    import iii.ssh_manager as ssh_module

    class Manager:
        def verify_logical_target(self, **_kwargs):
            return _live_receiver()

        def px4_audit(self, **_kwargs):
            return {
                "audit": {
                    "status": {"connected": True},
                    "findings": [],
                    "parameter_manifest_id": "e" * 64,
                },
                "activation_evidence": {
                    "profile": "sim",
                    "comparison": {
                        "required_match": True,
                        "inventory_complete": True,
                    },
                },
            }

    class Runtime:
        endpoints = []

        @classmethod
        def from_env(cls, *, endpoint=None):
            cls.endpoints.append(endpoint)
            return cls()

        def command(self, command, _parameters):
            return _runtime_response(command)

        def configuration_state(self):
            return {
                "manifest": {
                    "status": {
                        "configuration_server_available": True,
                        "pending_edits": False,
                        "configuration_divergent": False,
                        "mirror_state": "current",
                        "pending_restart": False,
                    }
                }
            }

    class QGCConfig:
        def managed_settings_match(self):
            return True

    class Store:
        def state(self):
            return {"active_release_id": RELEASE_ID}

        def _verified_release_slot(self, _release_id):
            return {}

        def _qgc_configuration_store(self, _slot):
            return QGCConfig()

    monkeypatch.setattr(ssh_module, "SSHManager", Manager)
    monkeypatch.setattr(runtime_module, "RuntimeApiClient", Runtime)
    gc_environments = []

    def gc_store(args, **_kwargs):
        gc_environments.append(args._iii_environment)
        return Store()

    monkeypatch.setattr(gc_module, "_store", gc_store)

    result = field._live_observations(
        SimpleNamespace(
            _iii_environment={
                "III_RELEASE_TRUSTED_SIGNERS": "/trusted/field-signers.json"
            },
            trusted_signers=None,
        ),
        {
            "runtime_profile": "hil",
            "selector": "hil",
            "endpoint": "iii.local",
            "logical_id": "drone",
        },
    )

    assert result["release_pair_compatible"] is True
    assert result["clock_gate_valid"] is True
    assert result["runtime_healthy"] is True
    assert result["required_hardware_ready"] is True
    assert result["px4_firmware_matches"] is True
    assert result["px4_required_parameters_match"] is True
    assert result["parameter_reconciliation_complete"] is True
    assert result["selected_mission_valid"] is True
    assert result["qgc_managed_settings_match"] is True
    assert Runtime.endpoints == ["iii.local"]
    assert gc_environments == [
        {
            "III_RELEASE_TRUSTED_SIGNERS": "/trusted/field-signers.json",
            "III_GC_TRUSTED_SIGNERS": "/trusted/field-signers.json",
        }
    ]


def test_live_observations_fail_closed_on_degraded_configuration():
    status = {
        "configuration_server_available": True,
        "pending_edits": False,
        "configuration_divergent": False,
        "mirror_state": "degraded",
    }
    assert field._configuration_valid(status) is False
    status["mirror_state"] = "current"
    assert field._configuration_valid(status) is True


def test_cold_restart_state_is_independent_of_mirror_durability():
    status = {
        "configuration_server_available": True,
        "pending_restart": False,
        "mirror_state": "degraded",
    }
    assert field._cold_restart_clear(status) is True

    status["pending_restart"] = True
    assert field._cold_restart_clear(status) is False


def test_check_reports_verified_external_archive_coverage(monkeypatch, tmp_path):
    monkeypatch.setattr(field, "_target", lambda _args: target())
    local = tmp_path / "registry"
    record = local / "captures/flight-1.json"
    record.parent.mkdir(parents=True)
    record.write_bytes(
        registry.canonical_json(
            {"schema": "iii.capture/v1", "creation_source": "iii capture"}
        )
        + b"\n"
    )
    archive = tmp_path / "external/records.tar"
    registry.apply_archive_plan(
        local, registry.build_archive_plan(local, destination=archive)
    )
    state = observations()
    state.pop("external_archive_recent")
    result = field.check(check_args(tmp_path / "check", state, registry_root=local))
    coverage = result.payload["observations"]["record_archive_coverage"]
    assert result.exit_code == 0
    assert result.payload["observations"]["external_archive_recent"] is True
    assert coverage["complete"] is True
    assert coverage["age_days"] >= 0
    assert coverage["archive_id"]
    assert coverage["archive_available"] is True
