"""Field preparation and non-authorizing connected-system readiness checks."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any, Mapping

from .operation import OperationStore, default_state_root, operation_id
from .result import CommandResult, Finding, NextAction, Outcome


def _environment(args: argparse.Namespace) -> Mapping[str, str]:
    return getattr(args, "_iii_environment", os.environ)


def _workspace() -> Path:
    current = Path.cwd().resolve()
    for candidate in (current, *current.parents):
        if (candidate / "deployment/runtime-targets.json").is_file():
            return candidate
    raise ValueError("the III workspace root could not be located")


def _retain_readiness(
    args: argparse.Namespace, identity: str, value: Mapping[str, Any]
) -> Path:
    from .registry import atomic_json, registry_lock, registry_root

    root = registry_root(_environment(args))
    with registry_lock(root):
        return atomic_json(root, f"readiness/{identity}.json", value)


def _target(args: argparse.Namespace) -> dict[str, Any]:
    from iii_deployment.contracts import ContractRegistry
    from iii_deployment.runtime_target import (
        load_runtime_targets,
        resolve_runtime_target,
    )

    root = _workspace()
    registry = ContractRegistry(root / "deployment/schemas/v1")
    targets = load_runtime_targets(root / "deployment/runtime-targets.json", registry)
    environment = _environment(args)
    default = environment.get("III_DEFAULT_TARGET") or (
        "real" if environment.get("III_ENVIRONMENT_PROFILE") == "field" else "sim"
    )
    return resolve_runtime_target(
        targets, selector=args.target, default_selector=default
    )


def _reject(
    command: str, exc: Exception, *, target: Mapping[str, Any] | None = None
) -> CommandResult:
    return CommandResult(
        command=command,
        outcome=Outcome.REJECTED,
        summary="The field operation was refused.",
        code="III_FIELD_CONTRACT_REJECTED",
        target=(str(target["endpoint"]) if target else None),
        profile=(str(target["runtime_profile"]) if target else None),
        findings=(Finding("III_FIELD_CONTRACT_REJECTED", str(exc)),),
        next_actions=(
            NextAction(
                ("iii", "field", "--help"),
                "Review field preparation and readiness requirements.",
            ),
        ),
    )


def _trusted_field_signers(args: argparse.Namespace) -> dict[str, Any]:
    from iii_deployment.contracts import ContractRegistry
    from iii_deployment.signers import load_trusted_signers

    path = getattr(args, "trusted_signers", None) or _environment(args).get(
        "III_RELEASE_TRUSTED_SIGNERS",
        str(Path.home() / ".config/iii/keys/signing/trusted-signers.json"),
    )
    return load_trusted_signers(
        Path(path),
        ContractRegistry(_workspace() / "deployment/schemas/v1"),
    )


def prepare(args: argparse.Namespace) -> CommandResult:
    from . import release as release_cli
    from iii_deployment.field import field_cache_report

    selected = None
    try:
        selected = _target(args)
        if selected["selector"] != "real":
            raise ValueError("field preparation is bound to the real aircraft target")
        runtime = release_cli._runtime(args)
        rows = []
        for version in sorted(set(args.version)):
            if args.offline:
                cached = release_cli._load_cached(runtime, version)
            else:
                try:
                    cached = release_cli._load_cached(runtime, version)
                except Exception:
                    cached = runtime["fetch_release"](
                        runtime["source"],
                        version,
                        runtime["cache"],
                        bundle_trust=runtime["bundle_trust"],
                        status_trust=runtime["status_trust"],
                        registry=runtime["registry"],
                        host_limits=runtime["limits"],
                        fetched_at=datetime.now(timezone.utc)
                        .isoformat()
                        .replace("+00:00", "Z"),
                    )
                else:
                    runtime["refresh_cached_status"](
                        cached.root,
                        runtime["source"].latest_status_index(),
                        status_trust=runtime["status_trust"],
                        registry=runtime["registry"],
                    )
                    cached = release_cli._load_cached(runtime, version)
            generated = cached.status_index["generated_at"]
            generated_at = datetime.fromisoformat(generated.replace("Z", "+00:00"))
            age_days = max(
                0.0,
                (datetime.now(timezone.utc) - generated_at).total_seconds() / 86400,
            )
            rows.append(
                {
                    "version": version,
                    "release_id": cached.publication["release_id"],
                    "verified": True,
                    "status": cached.status["status"],
                    "status_statement_id": cached.status["statement_id"],
                    "status_index_id": cached.status_index["index_id"],
                    "status_index_generated_at": generated,
                    "status_age_days": age_days,
                    "components": sorted(cached.publication["components"]),
                    "cache_root": str(cached.root),
                }
            )
            release_cli._retain_cache_evidence(args, cached)
        report = field_cache_report(rows)
        if not report["complete"]:
            raise ValueError(
                "the verified cache contains a withdrawn, unsafe, incomplete, or unpaired release"
            )
        from iii_deployment.contracts import ContractRegistry

        ContractRegistry(_workspace() / "deployment/schemas/v1").validate(
            "field-cache-completeness", report
        )
        identifier = getattr(args, "_iii_operation_id")
        path = OperationStore(default_state_root(_environment(args))).write_record(
            identifier, "field-prepare.json", report
        )
        readiness_path = _retain_readiness(args, report["cache_id"], report)
    except Exception as exc:
        return _reject("iii field prepare", exc, target=selected)
    outcome = Outcome.WARNING if report["warnings"] else Outcome.SUCCESS
    return CommandResult(
        command="iii field prepare",
        outcome=outcome,
        summary=f"Prepared {len(rows)} verified paired release(s) for offline field use.",
        code=(
            "III_FIELD_CACHE_PREPARED_STATUS_STALE"
            if report["warnings"]
            else "III_FIELD_CACHE_PREPARED"
        ),
        findings=tuple(
            Finding(item["id"], item["message"], severity="warning")
            for item in report["warnings"]
        ),
        target=selected["endpoint"],
        profile=selected["runtime_profile"],
        release_id=rows[-1]["release_id"],
        evidence=(
            str(path),
            str(readiness_path),
            report["cache_id"],
            *(row["status_statement_id"] for row in rows),
        ),
        payload_schema=report["schema"],
        payload={**report, "offline": args.offline},
        terminal_reason=(
            "The existing signed cache was verified without network access; status age remains visible."
            if args.offline
            else "The signed release-status chain was refreshed monotonically and the complete paired cache was verified."
        ),
    )


def verify_offline(args: argparse.Namespace) -> CommandResult:
    """Verify representative component packaging using only authenticated caches."""

    from . import release as release_cli
    from iii_deployment.contracts import ContractRegistry, content_identity
    from iii_deployment.field import field_cache_report

    selected = None
    try:
        selected = _target(args)
        if selected["selector"] != "real":
            raise ValueError(
                "offline field verification is bound to the real target contract"
            )
        if not args.offline:
            raise ValueError("field verification requires explicit --offline")
        runtime = release_cli._runtime(args)
        releases = []
        scenarios = []
        for version in sorted(set(args.version)):
            cached = release_cli._load_cached(runtime, version)
            generated = cached.status_index["generated_at"]
            generated_at = datetime.fromisoformat(generated.replace("Z", "+00:00"))
            age_days = max(
                0.0,
                (datetime.now(timezone.utc) - generated_at).total_seconds() / 86400,
            )
            components = sorted(cached.publication["components"])
            releases.append(
                {
                    "version": version,
                    "release_id": cached.publication["release_id"],
                    "verified": True,
                    "status": cached.status["status"],
                    "status_statement_id": cached.status["statement_id"],
                    "status_index_id": cached.status_index["index_id"],
                    "status_index_generated_at": generated,
                    "status_age_days": age_days,
                    "components": components,
                    "cache_root": str(cached.root),
                }
            )
            for scenario, required in (
                ("gc-only", ("gc",)),
                ("drone-only", ("drone",)),
                ("paired", ("drone", "gc")),
            ):
                missing = sorted(set(required) - set(components))
                if missing:
                    raise ValueError(
                        f"{version} cannot verify {scenario}; missing {', '.join(missing)}"
                    )
                scenarios.append(
                    {
                        "version": version,
                        "release_id": cached.publication["release_id"],
                        "scenario": scenario,
                        "components": list(required),
                        "component_contracts": {
                            name: cached.publication["components"][name]
                            for name in required
                        },
                        "verified": True,
                        "network_access": False,
                        "target_mutation": False,
                    }
                )
            release_cli._retain_cache_evidence(args, cached)
        completeness = field_cache_report(releases)
        if not completeness["complete"]:
            raise ValueError(
                "the offline cache is not a complete qualified paired release set"
            )
        body = {
            "schema": "iii.field-offline-verification/v1",
            "cache_id": completeness["cache_id"],
            "releases": releases,
            "scenarios": scenarios,
            "offline": True,
            "network_access": False,
            "target_mutation": False,
        }
        report = {**body, "verification_id": content_identity(body)}
        ContractRegistry(_workspace() / "deployment/schemas/v1").validate(
            "field-offline-verification", report
        )
        identifier = operation_id()
        path = OperationStore(default_state_root(_environment(args))).write_record(
            identifier, "field-offline-verification.json", report
        )
        readiness_path = _retain_readiness(args, report["verification_id"], report)
    except Exception as exc:
        return _reject("iii field verify", exc, target=selected)
    return CommandResult(
        command="iii field verify",
        outcome=Outcome.SUCCESS,
        summary=f"Verified {len(scenarios)} representative offline component scenario(s).",
        code="III_FIELD_OFFLINE_VERIFIED",
        target=selected["endpoint"],
        profile=selected["runtime_profile"],
        release_id=releases[-1]["release_id"],
        evidence=(str(path), str(readiness_path), report["verification_id"]),
        payload_schema=report["schema"],
        payload=report,
        terminal_reason="All inputs came from authenticated local caches; no network or target mutation occurred.",
    )


def _load_state(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("readiness state input is missing or linked")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("readiness state input must be one object")
    return value


def _configuration_valid(status: Mapping[str, Any]) -> bool:
    return bool(
        status.get("configuration_server_available") is True
        and status.get("pending_edits") is False
        and status.get("configuration_divergent") is False
        and status.get("mirror_state") == "current"
    )


def _cold_restart_clear(status: Mapping[str, Any]) -> bool:
    """Report restart state independently from offboard mirror durability."""

    return bool(
        status.get("configuration_server_available") is True
        and status.get("pending_restart") is False
    )


def _live_observations(
    args: argparse.Namespace, target: Mapping[str, Any]
) -> dict[str, Any]:
    """Collect only authenticated facts and fail every unavailable check closed."""
    from . import gc_application
    from .runtime_api_client import RuntimeApiClient
    from .ssh_manager import SSHManager

    identifier = operation_id()
    manager = SSHManager()
    receiver = manager.verify_logical_target(
        profile=str(target["runtime_profile"]), operation_id=identifier
    )
    runtime_client = RuntimeApiClient.from_env(endpoint=str(target["endpoint"]))
    live = (
        receiver.get("live_state")
        if isinstance(receiver.get("live_state"), dict)
        else {}
    )
    active = (
        live.get("active_release_id") or receiver.get("active_release_id") or "unknown"
    )
    runtime_response = runtime_client.command("runtime.status", {})
    runtime = (
        runtime_response.get("result", {}).get("daemon", {})
        if runtime_response.get("accepted") is True
        and isinstance(runtime_response.get("result"), dict)
        else {}
    )
    nodes = (
        runtime.get("managed_nodes")
        if isinstance(runtime.get("managed_nodes"), dict)
        else {}
    )
    processes = (
        runtime.get("processes") if isinstance(runtime.get("processes"), dict) else {}
    )
    services = (
        runtime.get("services") if isinstance(runtime.get("services"), dict) else {}
    )

    mission_response = runtime_client.command("mission.catalog.status", {})
    mission_result = (
        mission_response.get("result", {})
        if mission_response.get("accepted") is True
        and isinstance(mission_response.get("result"), dict)
        else {}
    )
    mission = (
        mission_result.get("status")
        if isinstance(mission_result.get("status"), dict)
        else {}
    )
    specification = (
        mission.get("specification")
        if isinstance(mission.get("specification"), dict)
        else {}
    )
    preflight = (
        mission.get("preflight") if isinstance(mission.get("preflight"), dict) else {}
    )
    preflight_items = {
        item.get("key"): item.get("passed") is True
        for item in preflight.get("items", [])
        if isinstance(item, dict) and isinstance(item.get("key"), str)
    }

    configuration = runtime_client.configuration_state()
    config_manifest = (
        configuration.get("manifest")
        if isinstance(configuration.get("manifest"), dict)
        else {}
    )
    config_status = (
        config_manifest.get("status")
        if isinstance(config_manifest.get("status"), dict)
        else {}
    )

    gc_environment = dict(_environment(args))
    trusted_signers = (
        getattr(args, "trusted_signers", None)
        or gc_environment.get("III_RELEASE_TRUSTED_SIGNERS")
        or Path.home() / ".config/iii/keys/signing/trusted-signers.json"
    )
    gc_environment["III_GC_TRUSTED_SIGNERS"] = str(
        Path(trusted_signers).expanduser().resolve()
    )
    gc_store = gc_application._store(
        argparse.Namespace(_iii_environment=gc_environment), create_roots=False
    )
    gc_state = gc_store.state()
    gc_release = gc_state.get("active_release_id") or "unknown"
    qgc_settings_match = False
    if isinstance(gc_release, str) and len(gc_release) == 64:
        slot = gc_store._verified_release_slot(gc_release)
        qgc_settings_match = gc_store._qgc_configuration_store(
            slot
        ).managed_settings_match()

    active_manifest = receiver.get("active_release_manifest")
    active_manifest = active_manifest if isinstance(active_manifest, dict) else {}
    selected_profile = next(
        (
            profile
            for profile in active_manifest.get("profiles", [])
            if isinstance(profile, dict)
            and profile.get("id") == target["runtime_profile"]
        ),
        {},
    )
    health = (
        selected_profile.get("health")
        if isinstance(selected_profile.get("health"), dict)
        else {}
    )
    required_nodes = (
        health.get("required_managed_nodes")
        if isinstance(health.get("required_managed_nodes"), dict)
        else None
    )
    required_services = (
        health.get("required_services")
        if isinstance(health.get("required_services"), list)
        else None
    )
    required_hardware = (
        health.get("required_hardware_roles")
        if isinstance(health.get("required_hardware_roles"), list)
        else None
    )
    runtime_healthy = bool(
        runtime.get("booted") is True
        and runtime.get("profile") == target["runtime_profile"]
        and nodes
        and all(state == "active" for state in nodes.values())
        and all(
            isinstance(process, dict)
            and process.get("alive") is True
            and process.get("recovery_in_progress") is False
            for process in processes.values()
        )
        and all(
            isinstance(service, dict)
            and service.get("alive") is True
            and service.get("ready") is True
            for service in services.values()
        )
    )
    profile_health_ready = bool(
        required_nodes is not None
        and required_services is not None
        and required_hardware is not None
        and all(
            nodes.get(name) == expected for name, expected in required_nodes.items()
        )
        and all(
            isinstance(services.get(name), dict)
            and services[name].get("alive") is True
            and services[name].get("ready") is True
            for name in required_services
        )
    )

    px4 = manager.px4_audit(release_id=active, operation_id=operation_id())
    audit = px4.get("audit") if isinstance(px4.get("audit"), dict) else {}
    evidence = (
        px4.get("activation_evidence")
        if isinstance(px4.get("activation_evidence"), dict)
        else {}
    )
    comparison = (
        evidence.get("comparison")
        if isinstance(evidence.get("comparison"), dict)
        else {}
    )
    px4_findings = (
        audit.get("findings") if isinstance(audit.get("findings"), list) else []
    )
    px4_codes = {
        item.get("code")
        for item in px4_findings
        if isinstance(item, dict) and isinstance(item.get("code"), str)
    }

    clock = receiver.get("clock") if isinstance(receiver.get("clock"), dict) else {}
    recovery = (
        receiver.get("recovery") if isinstance(receiver.get("recovery"), dict) else {}
    )
    portable_backup = (
        receiver.get("portable_backup")
        if isinstance(receiver.get("portable_backup"), dict)
        else {}
    )
    configuration_valid = _configuration_valid(config_status)
    storage_ready = preflight_items.get("storage") is True
    return {
        "boot_id": receiver.get("boot_id") or "unknown",
        "drone_release_id": active,
        "gc_release_id": gc_release,
        "profile": target["runtime_profile"],
        "configuration_hash": live.get("configuration_hash", "unknown"),
        "commissioning_hash": live.get("commissioning_hash", "unknown"),
        "px4_required_state_hash": audit.get("parameter_manifest_id", "unknown"),
        "mission_id": specification.get("catalog_id", "unknown"),
        "qgc_pair_id": _environment(args).get("III_QGC_PAIR_ID", "unknown"),
        "receiver_available": True,
        "commissioning_valid": bool(
            recovery.get("flight_capable") is True
            and recovery.get("recovery_only") is False
            and selected_profile.get("status") == "commissioned"
        ),
        "release_pair_compatible": active == gc_release,
        "clock_gate_valid": clock.get("gate") == "OPERATIONAL"
        and clock.get("fault") is None,
        "control_plane_available": runtime_response.get("accepted") is True,
        "required_hardware_ready": profile_health_ready and not required_hardware,
        "px4_firmware_matches": bool(
            isinstance(audit.get("status"), dict)
            and audit["status"].get("connected") is True
            and not any("FIRMWARE" in code for code in px4_codes)
        ),
        "px4_required_parameters_match": bool(
            comparison.get("required_match") is True
            and comparison.get("inventory_complete") is True
            and evidence.get("profile") == selected_profile.get("parameter_profile")
        ),
        "parameter_reconciliation_complete": configuration_valid,
        "selected_mission_valid": bool(
            specification.get("catalog_ready") is True
            and specification.get("active_profile") == target["runtime_profile"]
            and mission.get("required_modes_registered") is True
        ),
        "storage_reserve_valid": storage_ready,
        "credentials_valid": True,
        "runtime_healthy": runtime_healthy and profile_health_ready,
        "cold_restart_clear": _cold_restart_clear(config_status),
        "qgc_managed_settings_match": qgc_settings_match,
        "optional_hardware_ready": bool(
            isinstance(health.get("optional_hardware_roles"), list)
            and not health.get("optional_hardware_roles")
        ),
        "backup_fresh": portable_backup.get("backup_fresh") is True,
        "external_archive_recent": False,
        "offline_cache_fresh": False,
        "logging_capacity_ready": storage_ready,
    }


def check(args: argparse.Namespace) -> CommandResult:
    from iii_deployment.contracts import content_identity
    from iii_deployment.field import evaluate_readiness, sign_readiness
    from .registry import archive_coverage, registry_root

    selected = None
    try:
        selected = _target(args)
        if selected["selector"] not in {"real", "hil"}:
            raise ValueError("connected field readiness is bound to an aircraft target")
        policy = json.loads(
            (_workspace() / "deployment/operational-policy.json").read_text(
                encoding="utf-8"
            )
        )
        observations = (
            _load_state(args.state)
            if args.state
            else _live_observations(args, selected)
        )
        coverage = archive_coverage(
            registry_root(_environment(args)),
            warning_days=policy["backup"]["external_archive_warning_days"],
        )
        observations = {
            **observations,
            "external_archive_recent": (
                observations.get("external_archive_recent", coverage["recent"])
                if args.state
                else coverage["recent"]
            ),
            "record_archive_coverage": coverage,
        }
        policy_hash = content_identity(policy)
        record = evaluate_readiness(
            observations,
            target={
                "endpoint": selected["endpoint"],
                "logical_id": selected["logical_id"],
                "profile": selected["runtime_profile"],
            },
            policy_hash=policy_hash,
        )
        if args.signing_key:
            record = sign_readiness(
                record, args.signing_key, _trusted_field_signers(args)
            )
        from iii_deployment.contracts import ContractRegistry

        ContractRegistry(_workspace() / "deployment/schemas/v1").validate(
            "field-readiness", record
        )
        identifier = operation_id()
        path = OperationStore(default_state_root(_environment(args))).write_record(
            identifier, "field-readiness.json", record
        )
        readiness_path = _retain_readiness(args, record["record_id"], record)
    except Exception as exc:
        return _reject("iii field check", exc, target=selected)
    severity = record["overall"]
    outcome = {
        "PASS": Outcome.SUCCESS,
        "WARN": Outcome.WARNING,
        "FAIL": Outcome.FAILED,
    }[severity]
    findings = tuple(
        Finding(
            item["id"],
            item["message"],
            severity=("error" if item["severity"] == "FAIL" else "warning"),
        )
        for item in record["findings"]
    )
    return CommandResult(
        command="iii field check",
        outcome=outcome,
        summary=f"Connected-system field readiness: {severity} ({len(findings)} finding(s)).",
        code=f"III_FIELD_READINESS_{severity}",
        findings=findings,
        operation_id=identifier,
        state="sealed",
        target=selected["endpoint"],
        profile=selected["runtime_profile"],
        release_id=record["identity"]["drone_release_id"],
        evidence=(str(path), str(readiness_path), record["record_id"]),
        payload_schema=record["schema"],
        payload={**record, "record_path": str(path)},
        terminal_reason="The sealed record is evidence only and never authorizes or arms a later operation.",
    )


def acknowledge(args: argparse.Namespace) -> CommandResult:
    from iii_deployment.field import acknowledge_warnings

    try:
        record = _load_state(args.record)
        acknowledgement = acknowledge_warnings(
            record,
            args.finding,
            args.rationale,
            args.signing_key,
            _trusted_field_signers(args),
        )
        from iii_deployment.contracts import ContractRegistry

        ContractRegistry(_workspace() / "deployment/schemas/v1").validate(
            "field-warning-acknowledgement", acknowledgement
        )
        identifier = getattr(args, "_iii_operation_id")
        path = OperationStore(default_state_root(_environment(args))).write_record(
            identifier, "field-acknowledgement.json", acknowledgement
        )
        readiness_path = _retain_readiness(
            args, acknowledgement["acknowledgement_id"], acknowledgement
        )
    except Exception as exc:
        return _reject("iii field acknowledge", exc)
    return CommandResult(
        command="iii field acknowledge",
        outcome=Outcome.SUCCESS,
        summary=f"Signed acknowledgement for {len(acknowledgement['warning_ids'])} warning(s).",
        code="III_FIELD_WARNINGS_ACKNOWLEDGED",
        evidence=(
            str(path),
            str(readiness_path),
            acknowledgement["acknowledgement_id"],
        ),
        payload_schema=acknowledgement["schema"],
        payload=acknowledgement,
        terminal_reason="Warning severities are unchanged and the acknowledgement grants no authorization.",
    )


def _release_options(parser: argparse.ArgumentParser) -> None:
    from .release import _common

    _common(parser)


def initialize(parser: argparse.ArgumentParser) -> None:
    subparsers = parser.add_subparsers(dest="field_command")
    prepare_parser = subparsers.add_parser(
        "prepare", help="refresh and verify the signed offline field cache"
    )
    prepare_parser.add_argument(
        "version", nargs="+", help="qualified SemVer tag(s) to prepare"
    )
    prepare_parser.add_argument(
        "--offline",
        action="store_true",
        help="verify only existing cached status and artifacts",
    )
    prepare_parser.add_argument("--target", choices=("sim", "real"), default="real")
    _release_options(prepare_parser)
    prepare_parser.set_defaults(func=prepare, _iii_mutating=True)

    verify_parser = subparsers.add_parser(
        "verify",
        help="prove representative offline component packaging from prepared caches",
    )
    verify_parser.add_argument(
        "version", nargs="+", help="qualified SemVer tag(s) already prepared locally"
    )
    verify_parser.add_argument(
        "--offline",
        action="store_true",
        help="require network-free, target-free cache verification",
    )
    verify_parser.add_argument("--target", choices=("sim", "real"), default="real")
    _release_options(verify_parser)
    verify_parser.set_defaults(func=verify_offline, _iii_mutating=False)

    check_parser = subparsers.add_parser(
        "check", help="seal a read-only connected-system readiness record"
    )
    check_parser.add_argument(
        "--target", choices=("sim", "real", "hil"), default="real"
    )
    check_parser.add_argument(
        "--state",
        type=Path,
        help="explicit observation fixture (testing/offline diagnostics)",
    )
    check_parser.add_argument(
        "--signing-key", type=Path, help="authorized field Ed25519 private key"
    )
    check_parser.add_argument(
        "--trusted-signers",
        type=Path,
        help="trusted signer store containing the active workstation-field key",
    )
    check_parser.set_defaults(func=check, _iii_mutating=False)

    acknowledge_parser = subparsers.add_parser(
        "acknowledge", help="sign rationale for present warning findings"
    )
    acknowledge_parser.add_argument("record", type=Path)
    acknowledge_parser.add_argument("--finding", action="append", required=True)
    acknowledge_parser.add_argument("--rationale", required=True)
    acknowledge_parser.add_argument("--signing-key", required=True, type=Path)
    acknowledge_parser.add_argument(
        "--trusted-signers",
        type=Path,
        help="trusted signer store containing the active workstation-field key",
    )
    acknowledge_parser.set_defaults(func=acknowledge, _iii_mutating=True)
