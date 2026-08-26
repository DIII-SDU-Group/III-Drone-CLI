"""Field preparation and non-authorizing connected-system readiness checks."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
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
        "/etc/iii-deployment/trusted-signers.json",
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


def _load_state(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("readiness state input is missing or linked")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("readiness state input must be one object")
    return value


def _live_observations(
    args: argparse.Namespace, target: Mapping[str, Any]
) -> dict[str, Any]:
    """Collect only authenticated facts and fail every unavailable check closed."""
    from .ssh_manager import SSHManager

    identifier = operation_id()
    receiver = SSHManager().verify_logical_target(
        profile=str(target["runtime_profile"]), operation_id=identifier
    )
    live = (
        receiver.get("live_state")
        if isinstance(receiver.get("live_state"), dict)
        else {}
    )
    active = (
        live.get("active_release_id") or receiver.get("active_release_id") or "unknown"
    )
    return {
        "boot_id": receiver.get("boot_id") or "unknown",
        "drone_release_id": active,
        "gc_release_id": _environment(args).get("III_GC_RELEASE_ID", "unknown"),
        "profile": target["runtime_profile"],
        "configuration_hash": live.get("configuration_hash", "unknown"),
        "commissioning_hash": live.get("commissioning_hash", "unknown"),
        "px4_required_state_hash": receiver.get("px4_required_state_hash", "unknown"),
        "mission_id": receiver.get("selected_mission_id", "unknown"),
        "qgc_pair_id": _environment(args).get("III_QGC_PAIR_ID", "unknown"),
        "receiver_available": True,
        # Explicit authenticated booleans are accepted; absence never becomes PASS.
        **{
            name: receiver.get(name, False)
            for name in (
                "commissioning_valid",
                "release_pair_compatible",
                "clock_gate_valid",
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
        },
    }


def check(args: argparse.Namespace) -> CommandResult:
    from iii_deployment.contracts import content_identity
    from iii_deployment.field import evaluate_readiness, sign_readiness

    selected = None
    try:
        selected = _target(args)
        if selected["selector"] != "real":
            raise ValueError(
                "connected field readiness is bound to the real aircraft target"
            )
        observations = (
            _load_state(args.state)
            if args.state
            else _live_observations(args, selected)
        )
        policy_hash = content_identity(
            json.loads(
                (_workspace() / "deployment/operational-policy.json").read_text(
                    encoding="utf-8"
                )
            )
        )
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
        evidence=(str(path), record["record_id"]),
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
    except Exception as exc:
        return _reject("iii field acknowledge", exc)
    return CommandResult(
        command="iii field acknowledge",
        outcome=Outcome.SUCCESS,
        summary=f"Signed acknowledgement for {len(acknowledgement['warning_ids'])} warning(s).",
        code="III_FIELD_WARNINGS_ACKNOWLEDGED",
        evidence=(str(path), acknowledgement["acknowledgement_id"]),
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

    check_parser = subparsers.add_parser(
        "check", help="seal a read-only connected-system readiness record"
    )
    check_parser.add_argument("--target", choices=("sim", "real"), default="real")
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
