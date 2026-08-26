"""Retained CLI workflow for receiver-owned aircraft host maintenance."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import time
from typing import Any, Mapping
from uuid import uuid4

from .operation import OperationStore, default_state_root
from .result import CommandResult, Finding, NextAction, Outcome


class HostOperationFailed(ValueError):
    """The authenticated receiver reported a definitive terminal failure."""


def _environment(args: argparse.Namespace) -> Mapping[str, str]:
    return getattr(args, "_iii_environment", os.environ)


def _workspace() -> Path:
    current = Path.cwd().resolve()
    for candidate in (current, *current.parents):
        if (candidate / "deployment/host-maintenance").is_dir():
            return candidate
    raise ValueError("the III workspace host-maintenance policy is unavailable")


def _registry():
    from iii_deployment.contracts import ContractRegistry

    return ContractRegistry(_workspace() / "deployment/schemas/v1")


def _manager(args: argparse.Namespace):
    from .ssh_manager import SSHManager

    return SSHManager(environment=_environment(args))


def _request(
    manager, *, action: str, operation_id: str, payload: Mapping[str, Any], nonce=None
):
    return manager.receiver_request(
        {
            "protocol_version": "1",
            "action": action,
            "operation_id": operation_id,
            "client_id": manager.client_id,
            "payload": dict(payload),
            "nonce": nonce,
        }
    )


def _target(args: argparse.Namespace) -> dict[str, str]:
    from iii_deployment.runtime_target import (
        load_runtime_targets,
        resolve_runtime_target,
    )

    root = _workspace()
    targets = load_runtime_targets(
        root / "deployment/runtime-targets.json", _registry()
    )
    selected = resolve_runtime_target(
        targets, selector=args.target, default_selector="real"
    )
    if (
        selected["endpoint"] != "iii.local"
        or selected["execution_host"] != "aircraft"
        or selected["logical_id"] != "drone"
        or selected["runtime_profile"] not in {"real", "opti_track"}
    ):
        raise ValueError("host maintenance requires the shared aircraft target")
    return {
        "logical_id": str(selected["logical_id"]),
        "profile": str(selected["runtime_profile"]),
    }


def _maintenance_request(args: argparse.Namespace) -> dict[str, Any]:
    from iii_deployment.host_maintenance import build_request

    policy = args.policy or (
        _workspace() / "deployment/host-maintenance/host-maintenance-policy.json"
    )
    return build_request(
        kind=args.kind,
        policy_path=policy,
        registry=_registry(),
        offline=args.offline,
        backup_record=args.backup_record,
        boot_profile_path=getattr(args, "boot_profile", None),
        trust_store_path=args.trust_store,
        release_status_index_path=args.release_status_index,
        retire_signer_ids=args.retire_signer or (),
        replacement_proof_paths=args.replacement_proof or (),
    )


def _retained_preflight(args: argparse.Namespace) -> dict[str, Any] | None:
    identifier = getattr(args, "_iii_operation_id", None)
    if not identifier:
        return None
    plan = OperationStore(default_state_root(_environment(args))).load_plan(identifier)
    preflight = plan and plan.get("preflight")
    return dict(preflight) if isinstance(preflight, Mapping) else None


def _plan(args: argparse.Namespace, operation_id: str) -> dict[str, Any]:
    return _request(
        _manager(args),
        action="plan-host-maintenance",
        operation_id=operation_id,
        payload={"request": _maintenance_request(args), "target": _target(args)},
    )


def apply_preflight(args: argparse.Namespace) -> dict[str, Any]:
    retained = _retained_preflight(args)
    if retained is not None:
        return retained
    identifier = getattr(args, "_iii_operation_id", None)
    if not identifier:
        raise ValueError("host maintenance requires a retained operation ID")
    return _plan(args, identifier)


def _rejected(command: str, exc: Exception) -> CommandResult:
    code = getattr(exc, "code", "III_HOST_MAINTENANCE_REJECTED")
    return CommandResult(
        command=command,
        outcome=Outcome.REJECTED,
        summary="Host maintenance was refused before an unsafe or stale mutation.",
        code=code,
        findings=(Finding(code, str(exc)),),
        next_actions=(
            NextAction(
                ("iii", "host", "maintenance", "status"),
                "Inspect retained maintenance, reboot, validation, and recovery state.",
            ),
        ),
    )


def _apply_command(args: argparse.Namespace) -> tuple[str, ...]:
    command = [
        "iii",
        "host",
        "maintenance",
        "apply",
        "--kind",
        args.kind,
        "--target",
        args.target,
    ]
    for option, value in (
        ("--backup-record", args.backup_record),
        ("--boot-profile", getattr(args, "boot_profile", None)),
        ("--trust-store", args.trust_store),
        ("--release-status-index", args.release_status_index),
        ("--policy", args.policy),
    ):
        if value is not None:
            command.extend((option, str(value)))
    if args.offline:
        command.append("--offline")
    for signer_id in args.retire_signer or ():
        command.extend(("--retire-signer", signer_id))
    for proof in args.replacement_proof or ():
        command.extend(("--replacement-proof", str(proof)))
    command.append("--dry-run")
    return tuple(command)


def check(args: argparse.Namespace) -> CommandResult:
    try:
        planned = _plan(args, f"host-check-{uuid4().hex[:20]}")
        receiver_plan = planned["plan"]
        maintenance = receiver_plan["parameters"]
    except Exception as exc:
        return _rejected("iii host maintenance check", exc)
    return CommandResult(
        command="iii host maintenance check",
        outcome=Outcome.SUCCESS,
        summary=(
            "The governed host is already converged; no maintenance mutation is needed."
            if maintenance["no_change"]
            else f"Planned {len(maintenance['mutations'])} controlled host-maintenance mutation(s)."
        ),
        code=(
            "III_HOST_MAINTENANCE_NO_CHANGE"
            if maintenance["no_change"]
            else "III_HOST_MAINTENANCE_CHECKED"
        ),
        target=args.target,
        profile=receiver_plan["target"]["profile"],
        payload_schema="iii.host-maintenance-check/v1",
        payload={
            "schema": "iii.host-maintenance-check/v1",
            "receiver_plan": receiver_plan,
            "mutation_performed": False,
        },
        next_actions=(
            NextAction(
                _apply_command(args),
                "Retain the exact receiver plan after reviewing package/trust and backup bindings.",
                mutating=True,
                target=args.target,
                profile=receiver_plan["target"]["profile"],
                confirmation_required=True,
            ),
        ),
    )


def _await_operation(
    manager, operation_id: str, *, timeout_seconds: float = 7205.0
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            status = _request(
                manager, action="status", operation_id=operation_id, payload={}
            )
        except Exception as exc:
            last_error = exc
            time.sleep(0.25)
            continue
        operation = status.get("operation")
        if isinstance(operation, dict) and operation.get("state") == "completed":
            return operation
        if isinstance(operation, dict) and operation.get("state") in {
            "failed",
            "cancelled",
        }:
            raise HostOperationFailed(
                f"receiver host maintenance ended {operation['state']}: "
                f"{operation.get('failure') or operation.get('checkpoint')}"
            )
        time.sleep(0.25)
    raise ValueError(
        "receiver host maintenance did not reach an authenticated terminal state"
        + (f": {last_error}" if last_error is not None else "")
    )


def apply(args: argparse.Namespace) -> CommandResult:
    try:
        retained = getattr(args, "_iii_retained_plan", {}).get("preflight")
        if not isinstance(retained, Mapping):
            raise ValueError("an exact retained receiver maintenance plan is required")
        manager = _manager(args)
        receiver_plan = retained["plan"]
        _request(
            manager,
            action="host-maintenance",
            operation_id=receiver_plan["operation_id"],
            payload={"plan": receiver_plan},
            nonce=retained["nonce"],
        )
        operation = _await_operation(manager, receiver_plan["operation_id"])
        result = operation["result"]
    except Exception as exc:
        return _rejected("iii host maintenance apply", exc)
    maintenance_id = result["maintenance_id"]
    reboot_required = bool(result["reboot_required"])
    return CommandResult(
        command="iii host maintenance apply",
        outcome=Outcome.SUCCESS,
        summary=(
            "Host maintenance is retained and requires an explicit reboot."
            if reboot_required
            else "Host maintenance and protected-release validation completed."
        ),
        code=(
            "III_HOST_MAINTENANCE_REBOOT_REQUIRED"
            if reboot_required
            else "III_HOST_MAINTENANCE_COMPLETED"
        ),
        target=args.target,
        profile=receiver_plan["target"]["profile"],
        evidence=(maintenance_id, result["transaction_id"]),
        payload_schema="iii.host-maintenance-result/v1",
        payload={"schema": "iii.host-maintenance-result/v1", **result},
        next_actions=(
            (
                NextAction(
                    (
                        "iii",
                        "host",
                        "maintenance",
                        "reboot",
                        "--maintenance-id",
                        maintenance_id,
                        "--target",
                        args.target,
                        "--dry-run",
                    ),
                    "Plan the separately explicit reboot and post-boot protected-release validation.",
                    mutating=True,
                    target=args.target,
                    profile=receiver_plan["target"]["profile"],
                    confirmation_required=True,
                    prerequisites=("Review the retained before/after package report.",),
                )
                if reboot_required
                else NextAction(
                    ("iii", "host", "maintenance", "status", "--target", args.target),
                    "Inspect the retained package, trust, validation, and commissioning evidence.",
                    target=args.target,
                    profile=receiver_plan["target"]["profile"],
                )
            ),
        ),
    )


def reboot_preflight(args: argparse.Namespace) -> dict[str, Any]:
    retained = _retained_preflight(args)
    if retained is not None:
        return retained
    identifier = getattr(args, "_iii_operation_id", None)
    if not identifier:
        raise ValueError("host reboot requires a retained operation ID")
    return _request(
        _manager(args),
        action="plan-host-reboot",
        operation_id=identifier,
        payload={"maintenance_id": args.maintenance_id, "target": _target(args)},
    )


def reboot(args: argparse.Namespace) -> CommandResult:
    try:
        retained = getattr(args, "_iii_retained_plan", {}).get("preflight")
        if not isinstance(retained, Mapping):
            raise ValueError("an exact retained receiver reboot plan is required")
        manager = _manager(args)
        receiver_plan = retained["plan"]
        accepted = _request(
            manager,
            action="host-reboot",
            operation_id=receiver_plan["operation_id"],
            payload={"plan": receiver_plan},
            nonce=retained["nonce"],
        )
        try:
            operation = _await_operation(
                manager, receiver_plan["operation_id"], timeout_seconds=30.0
            )
            detached = False
        except HostOperationFailed:
            raise
        except Exception:
            operation = accepted.get("operation")
            detached = True
    except Exception as exc:
        return _rejected("iii host maintenance reboot", exc)
    return CommandResult(
        command="iii host maintenance reboot",
        outcome=Outcome.WARNING if detached else Outcome.SUCCESS,
        summary=(
            "The target accepted the explicit reboot; reconnect for post-boot validation."
            if detached
            else "The explicit reboot operation reached a receiver terminal checkpoint."
        ),
        code=(
            "III_HOST_REBOOT_RECONNECT_REQUIRED"
            if detached
            else "III_HOST_REBOOT_ACCEPTED"
        ),
        target=args.target,
        payload_schema="iii.host-reboot-result/v1",
        payload={
            "schema": "iii.host-reboot-result/v1",
            "maintenance_id": args.maintenance_id,
            "detached_for_reboot": detached,
            "operation": operation,
        },
        next_actions=(
            NextAction(
                ("iii", "host", "maintenance", "status", "--target", args.target),
                "Reconnect and authenticate post-boot protected-release validation.",
                target=args.target,
            ),
        ),
    )


def status(args: argparse.Namespace) -> CommandResult:
    try:
        result = _request(
            _manager(args),
            action="host-maintenance-status",
            operation_id="host-maintenance-status",
            payload={},
        )
        maintenance = result["maintenance"]
    except Exception as exc:
        return _rejected("iii host maintenance status", exc)
    transaction = maintenance.get("transaction")
    phase = "none" if transaction is None else transaction["phase"]
    outcome = Outcome.WARNING if phase == "failed" else Outcome.SUCCESS
    return CommandResult(
        command="iii host maintenance status",
        outcome=outcome,
        summary=f"Authenticated host-maintenance state: {phase}.",
        code=(
            "III_HOST_MAINTENANCE_RECOVERY_REQUIRED"
            if phase == "failed"
            else "III_HOST_MAINTENANCE_STATUS"
        ),
        target=args.target,
        payload_schema=maintenance["schema"],
        payload=maintenance,
        findings=(
            (
                Finding(
                    "III_HOST_MAINTENANCE_RECOVERY_REQUIRED",
                    maintenance["recovery_recommendation"],
                ),
            )
            if phase == "failed"
            else ()
        ),
        terminal_reason="Maintenance state and retained evidence were inspected without mutation.",
    )


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--kind",
        choices=(
            "packages",
            "boot-settings",
            "bundle-trust",
            "release-status-trust",
        ),
        required=True,
    )
    parser.add_argument("--target", choices=("real", "opti_track"), default="real")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--backup-record", type=Path)
    parser.add_argument(
        "--boot-profile",
        type=Path,
        help="exact content-identified Raspberry Pi boot profile",
    )
    parser.add_argument("--trust-store", type=Path)
    parser.add_argument(
        "--release-status-index",
        type=Path,
        help="replacement index signed by the newly active release-status signer",
    )
    parser.add_argument("--retire-signer", action="append")
    parser.add_argument(
        "--replacement-proof",
        action="append",
        type=Path,
        help="canonical public proof-of-possession for one new active signer",
    )
    parser.add_argument(
        "--policy", type=Path, help="explicit reviewed host-maintenance policy"
    )


def initialize(commands: Any) -> None:
    parser = commands.add_parser(
        "maintenance",
        help="plan, apply, reboot, and inspect controlled host maintenance",
    )
    leaves = parser.add_subparsers(dest="host_maintenance_command")

    check_parser = leaves.add_parser(
        "check", help="read-only package/cache/trust maintenance preflight"
    )
    _common(check_parser)
    check_parser.set_defaults(func=check, _iii_mutating=False)

    apply_parser = leaves.add_parser(
        "apply", help="apply one retained receiver/Ansible maintenance plan"
    )
    _common(apply_parser)
    apply_parser.set_defaults(
        func=apply,
        _iii_mutating=True,
        _iii_plan_provider=apply_preflight,
    )

    reboot_parser = leaves.add_parser(
        "reboot", help="explicitly reboot one reboot-required maintenance transaction"
    )
    reboot_parser.add_argument("--maintenance-id", required=True)
    reboot_parser.add_argument(
        "--target", choices=("real", "opti_track"), default="real"
    )
    reboot_parser.set_defaults(
        func=reboot,
        _iii_mutating=True,
        _iii_plan_provider=reboot_preflight,
    )

    status_parser = leaves.add_parser(
        "status", help="inspect retained maintenance and post-boot validation"
    )
    status_parser.add_argument(
        "--target", choices=("real", "opti_track"), default="real"
    )
    status_parser.set_defaults(func=status, _iii_mutating=False)


__all__ = ["initialize"]
