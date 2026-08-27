"""Retained CLI workflow for receiver-owned transactional networking."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from .operation import OperationStore, default_state_root
from .result import CommandResult, Finding, NextAction, Outcome


def _environment(args: argparse.Namespace) -> Mapping[str, str]:
    return getattr(args, "_iii_environment", os.environ)


def _workspace() -> Path:
    current = Path.cwd().resolve()
    for candidate in (current, *current.parents):
        if (candidate / "deployment/runtime-targets.json").is_file():
            return candidate
    raise ValueError("the III workspace deployment policy is unavailable")


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
    selected = resolve_runtime_target(
        load_runtime_targets(root / "deployment/runtime-targets.json", _registry()),
        selector=args.target,
        default_selector="real",
    )
    if (
        selected["endpoint"] != "iii.local"
        or selected["execution_host"] != "aircraft"
        or selected["logical_id"] != "drone"
        or selected["runtime_profile"] not in {"real", "opti_track"}
    ):
        raise ValueError("transactional networking requires the shared aircraft target")
    return {
        "logical_id": str(selected["logical_id"]),
        "profile": str(selected["runtime_profile"]),
    }


def _input(args: argparse.Namespace) -> dict[str, Any]:
    from iii_deployment.networking import load_network_input

    return load_network_input(args.input.expanduser().absolute())


def _retained_preflight(args: argparse.Namespace) -> dict[str, Any] | None:
    identifier = getattr(args, "_iii_operation_id", None)
    if not identifier:
        return None
    plan = OperationStore(default_state_root(_environment(args))).load_plan(identifier)
    preflight = plan and plan.get("preflight")
    return dict(preflight) if isinstance(preflight, Mapping) else None


def apply_preflight(args: argparse.Namespace) -> dict[str, Any]:
    retained = _retained_preflight(args)
    if retained is not None:
        return retained
    identifier = getattr(args, "_iii_operation_id", None)
    if not identifier:
        raise ValueError("network apply requires a retained operation ID")
    return _request(
        _manager(args),
        action="network-plan",
        operation_id=identifier,
        payload={"profile": _input(args), "target": _target(args)},
    )


def confirm_preflight(args: argparse.Namespace) -> dict[str, Any]:
    retained = _retained_preflight(args)
    if retained is not None:
        return retained
    identifier = getattr(args, "_iii_operation_id", None)
    if not identifier:
        raise ValueError("network confirmation requires a retained operation ID")
    return _request(
        _manager(args),
        action="network-confirm-plan",
        operation_id=identifier,
        payload={
            "target_operation_id": args.network_operation_id,
            "target": _target(args),
        },
    )


def _rejected(command: str, exc: Exception) -> CommandResult:
    code = getattr(exc, "code", "III_NETWORK_REJECTED")
    return CommandResult(
        command=command,
        outcome=Outcome.REJECTED,
        summary="Transactional networking was refused before an unsafe or stale mutation.",
        code=code,
        findings=(Finding(code, str(exc)),),
        next_actions=(
            NextAction(
                ("iii", "host", "network", "status", "--help"),
                "Inspect the retained onboard network transaction.",
            ),
        ),
    )


def apply(args: argparse.Namespace) -> CommandResult:
    try:
        retained = getattr(args, "_iii_retained_plan", {}).get("preflight")
        if not isinstance(retained, Mapping):
            raise ValueError("an exact retained network plan is required")
        plan = retained["plan"]
        response = _request(
            _manager(args),
            action="network-apply",
            operation_id=plan["operation_id"],
            payload={"plan": plan, "profile": _input(args)},
            nonce=retained["nonce"],
        )
    except Exception as exc:
        return _rejected("iii host network apply", exc)
    parameters = plan["parameters"]
    return CommandResult(
        command="iii host network apply",
        outcome=Outcome.SUCCESS,
        summary=(
            "The retained profile is already active; no connectivity mutation was needed."
            if parameters["no_change"]
            else "The receiver accepted the network profile and will restore the prior profile unless confirmed onboard within 90 seconds."
        ),
        code=(
            "III_NETWORK_NO_CHANGE"
            if parameters["no_change"]
            else "III_NETWORK_CONFIRMATION_REQUIRED"
        ),
        target=args.target,
        profile=plan["target"]["profile"],
        evidence=(parameters["network_id"], plan["plan_id"]),
        payload_schema="iii.network-apply-result/v1",
        payload={
            "schema": "iii.network-apply-result/v1",
            "network_id": parameters["network_id"],
            "network_operation_id": plan["operation_id"],
            "connectivity_impacting": parameters["connectivity_impacting"],
            "confirmation_deadline_s": parameters["confirmation_deadline_s"],
            "profile": parameters["profile"],
            "receiver_operation": response["operation"],
        },
        next_actions=(
            (
                NextAction(
                    (
                        "iii",
                        "host",
                        "network",
                        "confirm",
                        "--network-operation-id",
                        plan["operation_id"],
                        "--target",
                        args.target,
                        "--dry-run",
                    ),
                    "After reconnecting through the candidate profile, retain the bound onboard confirmation.",
                    mutating=True,
                    target=args.target,
                    profile=plan["target"]["profile"],
                    confirmation_required=True,
                ),
            )
            if not parameters["no_change"]
            else ()
        ),
        terminal_reason=(
            "The requested network profile already matches the installed profile."
            if parameters["no_change"]
            else None
        ),
    )


def confirm(args: argparse.Namespace) -> CommandResult:
    try:
        retained = getattr(args, "_iii_retained_plan", {}).get("preflight")
        if not isinstance(retained, Mapping):
            raise ValueError("an exact retained network confirmation is required")
        confirmation = retained["confirmation"]
        response = _request(
            _manager(args),
            action="network-confirm",
            operation_id=confirmation["operation_id"],
            payload={"confirmation": confirmation},
            nonce=retained["nonce"],
        )
    except Exception as exc:
        return _rejected("iii host network confirm", exc)
    return CommandResult(
        command="iii host network confirm",
        outcome=Outcome.SUCCESS,
        summary="The candidate network profile is durably confirmed and its rollback timer is stopped.",
        code="III_NETWORK_CONFIRMED",
        target=args.target,
        evidence=(confirmation["network_id"], confirmation["confirmation_id"]),
        payload_schema="iii.network-confirm-result/v1",
        payload={"schema": "iii.network-confirm-result/v1", **response["network"]},
        next_actions=(
            NextAction(
                (
                    "iii",
                    "host",
                    "network",
                    "status",
                    "--network-operation-id",
                    confirmation["target_operation_id"],
                    "--target",
                    args.target,
                ),
                "Inspect the durable confirmed network transaction.",
                target=args.target,
            ),
        ),
    )


def status(args: argparse.Namespace) -> CommandResult:
    try:
        response = _request(
            _manager(args),
            action="network-status",
            operation_id=f"network-status-{uuid4().hex[:20]}",
            payload={"target_operation_id": args.network_operation_id},
        )
    except Exception as exc:
        return _rejected("iii host network status", exc)
    network = response["network"]
    return CommandResult(
        command="iii host network status",
        outcome=Outcome.SUCCESS,
        summary=f"Authenticated network transaction state: {network['state']}.",
        code="III_NETWORK_STATUS",
        target=args.target,
        evidence=(network["network_id"],),
        payload_schema="iii.network-status/v1",
        payload=network,
        terminal_reason="This read-only inspection made no host change.",
    )


def initialize(commands: Any) -> None:
    parser = commands.add_parser(
        "network", help="plan, apply, confirm, and inspect operator networking"
    )
    leaves = parser.add_subparsers(dest="host_network_command")

    apply_parser = leaves.add_parser(
        "apply", help="apply a retained owner-only network input transaction"
    )
    apply_parser.add_argument(
        "--input", type=Path, required=True, help="Git-ignored owner-only network JSON"
    )
    apply_parser.add_argument(
        "--target", choices=("real", "opti_track"), default="real"
    )
    apply_parser.set_defaults(
        func=apply, _iii_mutating=True, _iii_plan_provider=apply_preflight
    )

    confirm_parser = leaves.add_parser(
        "confirm", help="durably confirm a pending network transaction"
    )
    confirm_parser.add_argument("--network-operation-id", required=True)
    confirm_parser.add_argument(
        "--target", choices=("real", "opti_track"), default="real"
    )
    confirm_parser.set_defaults(
        func=confirm, _iii_mutating=True, _iii_plan_provider=confirm_preflight
    )

    status_parser = leaves.add_parser(
        "status", help="inspect an onboard network transaction"
    )
    status_parser.add_argument("--network-operation-id", required=True)
    status_parser.add_argument(
        "--target", choices=("real", "opti_track"), default="real"
    )
    status_parser.set_defaults(func=status, _iii_mutating=False)


__all__ = ["initialize"]
