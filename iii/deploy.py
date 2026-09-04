"""Typed release deployment; legacy shell and synchronization are unavailable."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any, Mapping, Sequence

from .operation import OperationStore, content_id, default_state_root
from .registry import registry_root
from .result import CommandResult, Finding, NextAction, Outcome


def _environment(args: argparse.Namespace) -> Mapping[str, str]:
    return getattr(args, "_iii_environment", os.environ)


def _workspace() -> Path:
    current = Path.cwd().resolve()
    for candidate in (current, *current.parents):
        if (candidate / "deployment/runtime-targets.json").is_file():
            return candidate
    raise ValueError("the III workspace root could not be located")


def _registry_root(args: argparse.Namespace) -> Path:
    return registry_root(_environment(args))


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
        targets, selector=getattr(args, "target", None), default_selector=default
    )


def _operation(args: argparse.Namespace) -> tuple[str, OperationStore]:
    identifier = getattr(args, "_iii_operation_id", None)
    if not identifier:
        raise ValueError("a retained operation ID is required")
    return identifier, OperationStore(default_state_root(_environment(args)))


def _reject(
    command: str,
    exc: Exception,
    *,
    target: Mapping[str, Any] | None = None,
    release_id: str | None = None,
) -> CommandResult:
    code = getattr(exc, "code", "III_DEPLOY_CONTRACT_REJECTED")
    px4_required = code == "III_PX4_RELEASE_REQUIRED"
    return CommandResult(
        command=command,
        outcome=Outcome.REJECTED,
        summary=(
            "The Pi release is staged, but PX4 must be brought to the paired release before activation."
            if px4_required
            else "The deployment operation was refused before unsafe mutation."
        ),
        code=code,
        target=str(target["endpoint"]) if target else None,
        profile=str(target["runtime_profile"]) if target else None,
        release_id=release_id,
        findings=(Finding(code, str(exc)),),
        next_actions=((
            NextAction(
                (
                    "iii", "px4", "release", "prepare",
                    "--release-directory", "<qualified-px4-artifact>",
                    "--destination", "<new-px4-media-directory>",
                ),
                "Prepare the exact PX4 firmware and microSD files, then follow the generated flashing instructions.",
                mutating=True,
                confirmation_required=True,
            )
        ) if px4_required else (
            NextAction(
                ("iii", "deploy", "status", "--target", "real"),
                "Inspect authenticated target and deployment state.",
            ),
        )),
    )


def _manager():
    from .ssh_manager import SSHManager

    return SSHManager()


class PX4ReleaseRequiredError(ValueError):
    code = "III_PX4_RELEASE_REQUIRED"


def _request(
    manager,
    *,
    action: str,
    operation_id: str,
    payload: Mapping[str, Any],
    nonce: str | None = None,
) -> dict[str, Any]:
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


def _binding(selected: Mapping[str, Any]) -> dict[str, str]:
    return {
        "logical_id": str(selected["logical_id"]),
        "profile": str(selected["runtime_profile"]),
    }


def _require_remote(selected: Mapping[str, Any]) -> None:
    if (
        selected["endpoint"] != "iii.local"
        or selected["execution_host"] != "aircraft"
        or selected["runtime_profile"] not in {"real", "opti_track"}
    ):
        raise ValueError(
            "deployment receiver operations require an explicit onboard aircraft target"
        )


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    from iii_deployment.contracts import canonical_json

    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} is missing or linked")
    raw = path.read_bytes()
    value = json.loads(raw)
    if not isinstance(value, dict) or raw != canonical_json(value) + b"\n":
        raise ValueError(f"{label} must be canonical JSON")
    return value


def _component(component: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    release = _read_json(component / "release-manifest.json", label="release manifest")
    bundle = _read_json(component / "bundle.manifest.json", label="bundle manifest")
    if release.get("release_id") != bundle.get("release_id"):
        raise ValueError("bundle and release identities disagree")
    return release, bundle


def _field_bundle_release(
    bundle_root: Path,
    *,
    components: Sequence[str],
    source_identity: str,
    selected_missions: Sequence[str],
) -> dict[str, Any]:
    """Bind a field bundle set to the exact retained source and mission choice."""

    manifests = {
        name: _component(bundle_root / name)[0] for name in sorted(set(components))
    }
    if not manifests:
        raise ValueError("field bundle validation requires at least one component")
    selected_release = manifests["drone" if "drone" in manifests else "gc"]
    if selected_release.get("release_class") != "field-development":
        raise ValueError("iii deploy field accepts only a field-development release")
    if any(value != selected_release for value in manifests.values()):
        raise ValueError("selected field component release manifests disagree")
    bundle_source_identity = selected_release.get("source_identity")
    if bundle_source_identity is None and isinstance(selected_release.get("source"), dict):
        bundle_source_identity = selected_release["source"].get("content_identity")
    if bundle_source_identity != source_identity:
        raise ValueError(
            "field bundle source identity differs from the retained source snapshot"
        )
    catalog = selected_release.get("mission_catalog")
    if not isinstance(catalog, Mapping):
        raise ValueError("field bundle lacks release-bound mission catalog metadata")
    included = catalog.get("included_experimental")
    entries = catalog.get("entries")
    if not isinstance(included, list) or not isinstance(entries, list):
        raise ValueError(
            "field bundle lacks exact mission selection metadata; rebuild it with current tooling"
        )
    if any(not isinstance(item, str) for item in included + entries):
        raise ValueError("field bundle mission selection metadata is malformed")
    requested = sorted(set(selected_missions))
    declared = sorted(set(included))
    if requested != declared:
        raise ValueError(
            "field bundle experimental mission selection differs from the deployment plan: "
            f"bundle={declared}, requested={requested}"
        )
    missing = sorted(set(requested) - set(entries))
    if missing:
        raise ValueError(
            "field bundle mission catalog omits selected mission entries: "
            + ", ".join(missing)
        )
    return selected_release


def _impact_display(
    impact: Mapping[str, Any], actual: Mapping[str, Any] | None = None
) -> str:
    detail = impact["detail"]
    mission_entries = detail["missions"]["entries"]
    mission_counts = {
        state: sum(item["state"] == state for item in mission_entries)
        for state in ("added", "changed", "removed")
    }
    parameter = detail["parameters"]["manifest"]
    lines = [
        "Components: " + ", ".join(impact["components"]),
        "Component reasons:",
    ]
    for component in impact["components"]:
        lines.append(
            f"  {component}: "
            + "; ".join(impact["component_reasons"].get(component, []))
        )
    lines.extend(
        [
            (
                "Missions: "
                + ", ".join(f"{name}={count}" for name, count in mission_counts.items())
            ),
            (
                f"Behavior trees/models: {len(impact['groups']['behavior_trees'])} "
                f"path(s), {sum(len(item['impacted_mission_ids']) for item in detail['missions']['behavior_trees'])} mission reference(s)"
            ),
            (
                "Parameters: "
                f"added={len(parameter['added'])}, changed={len(parameter['changed'])}, "
                f"removed={len(parameter['removed'])}, "
                f"reintroduction-review={len(parameter['reintroduction_candidates'])}, "
                f"sets={len(detail['parameters']['parameter_sets'])}"
            ),
            (
                "Resulting identities: "
                + ", ".join(
                    f"{name}={identity}"
                    for name, identity in impact["resulting_identities"].items()
                )
            ),
            (
                f"PX4 manifest drift: {len(impact['groups']['px4_manifest_drift'])} path(s); implicit FMU write=false"
            ),
        ]
    )
    if actual is not None:
        lines.append(
            "Actual phases: "
            + ", ".join(
                f"{phase['name']}={phase['state']}" for phase in actual["phases"]
            )
        )
    return "\n".join(lines)


def status(args: argparse.Namespace) -> CommandResult:
    selected = None
    try:
        selected = _target(args)
        _require_remote(selected)
        remote = _manager().verify_logical_target(
            profile=selected["runtime_profile"],
            operation_id="deploy-status-inspection",
        )
        local_records = None
        if args.operation:
            store = OperationStore(default_state_root(_environment(args)))
            operation_root = store.operation_path(args.operation)
            if operation_root.is_symlink() or not operation_root.is_dir():
                raise ValueError("requested local operation record is unavailable")
            local_records = {
                path.name: _read_json(path, label=path.name)
                for path in sorted(operation_root.glob("*.json"))
            }
    except Exception as exc:
        return _reject("iii deploy status", exc, target=selected)
    live = remote.get("live_state", {})
    active = live.get("active_release_id") if isinstance(live, dict) else None
    return CommandResult(
        command="iii deploy status",
        outcome=Outcome.SUCCESS,
        summary=(
            f"Authenticated receiver at {selected['endpoint']} advertises "
            f"{selected['runtime_profile']}."
        ),
        code="III_DEPLOY_STATUS_VERIFIED",
        target=selected["endpoint"],
        profile=selected["runtime_profile"],
        release_id=active,
        payload_schema="iii.deploy-status/v1",
        payload={
            "target_descriptor": selected,
            "receiver": remote,
            "local_operation": local_records,
        },
        terminal_reason=(
            "Receiver identity, profile, release, recovery, and operation state "
            "were read without mutation."
        ),
    )


def plan(args: argparse.Namespace) -> CommandResult:
    from iii_deployment.contracts import ContractRegistry

    selected = None
    release_id = None
    try:
        selected = _target(args)
        _require_remote(selected)
        snapshot, impact = _source_impact(
            _workspace(), args.include_mission, args.exclude_mission, args.component
        )
        if args.bundle_set:
            release = _field_bundle_release(
                args.bundle_set.resolve(),
                components=impact["components"],
                source_identity=snapshot["content_identity"],
                selected_missions=impact["missions"]["selected"],
            )
            release_id = release["release_id"]
    except Exception as exc:
        return _reject("iii deploy plan", exc, target=selected, release_id=release_id)
    report = {"display": _impact_display(impact), "impact": impact, "actual": None}
    ContractRegistry(_workspace() / "deployment/schemas/v1").validate(
        "field-deployment-report", report
    )
    return CommandResult(
        command="iii deploy plan",
        outcome=Outcome.SUCCESS,
        summary=(
            f"Planned {len(impact['components'])} component(s) for "
            f"{selected['endpoint']} ({selected['runtime_profile']}); no mutation occurred."
        ),
        code="III_DEPLOY_IMPACT_PLANNED",
        target=selected["endpoint"],
        profile=selected["runtime_profile"],
        release_id=release_id,
        payload_schema="iii.field-deployment-report/v1",
        payload=report,
        terminal_reason=(
            "Source, mission, behavior-tree, parameter, ordering, and PX4 no-write "
            "impact were computed without build, transfer, or target mutation."
        ),
    )


def inspect(args: argparse.Namespace) -> CommandResult:
    from iii_deployment.bundle import inspect_bundle, load_bundle_limits
    from iii_deployment.contracts import ContractRegistry
    from iii_deployment.signers import load_trusted_signers

    selected = None
    try:
        selected = _target(args)
        root = _workspace()
        trust_path = getattr(args, "trusted_signers", None) or _environment(args).get(
            "III_RELEASE_TRUSTED_SIGNERS",
            "/etc/iii-deployment/trusted-signers.json",
        )
        trusted_signers = load_trusted_signers(
            Path(trust_path).expanduser().resolve(),
            ContractRegistry(root / "deployment/schemas/v1"),
        )
        verified = inspect_bundle(
            args.component,
            trusted_signers,
            registry=ContractRegistry(root / "deployment/schemas/v1"),
            host_limits=load_bundle_limits(root / "deployment/operational-policy.json"),
        )
        release_id = verified.release_manifest["release_id"]
    except Exception as exc:
        return _reject("iii deploy inspect", exc, target=selected)
    return CommandResult(
        command="iii deploy inspect",
        outcome=Outcome.SUCCESS,
        summary=(
            f"Inspected release {release_id} for {selected['endpoint']} "
            f"({selected['runtime_profile']})."
        ),
        code="III_DEPLOY_BUNDLE_INSPECTED",
        target=selected["endpoint"],
        profile=selected["runtime_profile"],
        release_id=release_id,
        payload_schema="iii.deploy-inspection/v1",
        payload={
            "target_descriptor": selected,
            "bundle_manifest": verified.bundle_manifest,
            "release_manifest": verified.release_manifest,
        },
        terminal_reason="Local bundle contracts were inspected without transfer.",
    )


def receiver_update_inspect(args: argparse.Namespace) -> CommandResult:
    from iii_deployment.contracts import ContractRegistry
    from iii_deployment.receiver.update import verify_receiver_update

    try:
        verified = verify_receiver_update(
            args.bundle,
            trust=args.trust,
            registry=ContractRegistry(_workspace() / "deployment/schemas/v1"),
        )
    except Exception as exc:
        return _reject("iii deploy receiver-update inspect", exc)
    return CommandResult(
        command="iii deploy receiver-update inspect",
        outcome=Outcome.SUCCESS,
        summary=(
            f"Verified signed receiver generation {verified.manifest['generation']} "
            f"({verified.manifest['receiver_id']})."
        ),
        code="III_RECEIVER_UPDATE_INSPECTED",
        payload_schema="iii.receiver-update-inspection/v1",
        payload={
            "receiver_manifest": verified.manifest,
            "receiver_signature": verified.signature,
        },
        terminal_reason=(
            "The signed receiver payload and compatibility declaration were verified "
            "locally without target mutation."
        ),
    )


def receiver_update_apply(args: argparse.Namespace) -> CommandResult:
    from iii_deployment.contracts import ContractRegistry
    from iii_deployment.receiver.update import verify_receiver_update

    selected = None
    receiver_id = None
    try:
        selected = _target(args)
        _require_remote(selected)
        identifier, store = _operation(args)
        verified = verify_receiver_update(
            args.bundle,
            trust=args.trust,
            registry=ContractRegistry(_workspace() / "deployment/schemas/v1"),
        )
        receiver_id = verified.manifest["receiver_id"]
        manager = _manager()
        transfer = manager.upload_receiver_update(
            args.bundle,
            receiver_id=receiver_id,
            profile=selected["runtime_profile"],
            operation_id=identifier,
        )
        archive_sha256 = hashlib.sha256(
            (args.bundle / "receiver-update.tar").read_bytes()
        ).hexdigest()
        planned = _request(
            manager,
            action="plan-receiver-update",
            operation_id=identifier,
            payload={
                "artifact": {
                    "receiver_id": receiver_id,
                    "generation": verified.manifest["generation"],
                    "archive_sha256": archive_sha256,
                    "upload_id": transfer.upload_id,
                },
                "target": _binding(selected),
            },
        )
        accepted = _request(
            manager,
            action="receiver-update",
            operation_id=identifier,
            payload={"plan": planned["plan"]},
            nonce=planned["nonce"],
        )
        actual = {
            "schema": "iii.receiver-update-actual/v1",
            "actual_id": "0" * 64,
            "operation_id": identifier,
            "receiver_id": receiver_id,
            "generation": verified.manifest["generation"],
            "target": selected,
            "transfer": transfer.as_dict(),
            "receiver_plan": planned["plan"],
            "receiver_acceptance": accepted,
        }
        actual["actual_id"] = content_id(
            {key: value for key, value in actual.items() if key != "actual_id"}
        )
        ContractRegistry(_workspace() / "deployment/schemas/v1").validate(
            "receiver-update-actual", actual
        )
        path = store.write_record(
            identifier, "receiver-update-actual.json", actual
        )
    except Exception as exc:
        return _reject(
            "iii deploy receiver-update apply", exc, target=selected
        )
    return CommandResult(
        command="iii deploy receiver-update apply",
        outcome=Outcome.SUCCESS,
        summary=(
            f"Receiver generation {actual['generation']} was transferred and "
            "durably accepted for A/B handoff."
        ),
        code="III_RECEIVER_UPDATE_ACCEPTED",
        target=selected["endpoint"],
        profile=selected["runtime_profile"],
        evidence=(
            str(path),
            actual["transfer"]["transfer_id"],
            actual["receiver_plan"]["plan_id"],
        ),
        payload_schema="iii.receiver-update-actual/v1",
        payload=actual,
        next_actions=(
            NextAction(
                (
                    "iii",
                    "deploy",
                    "status",
                    "--target",
                    str(selected["runtime_profile"]),
                    "--operation",
                    identifier,
                ),
                "Reattach after the receiver A/B handoff and verify its terminal state.",
            ),
        ),
        terminal_reason=(
            "The stable onboard bootstrap owns the selector switch, readiness "
            "deadline, and automatic fallback independently of this CLI process."
        ),
    )


def _stage_component(
    manager,
    component: Path,
    *,
    selected: Mapping[str, Any],
    operation_id: str,
    status_index: Path | None,
) -> dict[str, Any]:
    release, bundle = _component(component)
    release_id = release["release_id"]
    transfer = manager.upload_bundle(
        component,
        release_id=release_id,
        profile=selected["runtime_profile"],
        status_index=status_index,
        operation_id=operation_id,
    )
    status_id = (
        None
        if status_index is None
        else _read_json(status_index, label="release-status index")["index_id"]
    )
    content = bundle.get("content")
    archive_sha = (
        content.get("archive_sha256") if isinstance(content, Mapping) else None
    )
    if not isinstance(archive_sha, str):
        archive_sha = hashlib.sha256(
            (component / "bundle.tar.zst").read_bytes()
        ).hexdigest()
    planned = _request(
        manager,
        action="plan-stage",
        operation_id=operation_id,
        payload={
            "artifact": {
                "release_id": release_id,
                "archive_sha256": archive_sha,
                "upload_id": transfer.upload_id,
                "status_index_id": status_id,
            },
            "target": _binding(selected),
        },
    )
    accepted = _request(
        manager,
        action="stage",
        operation_id=operation_id,
        payload={"plan": planned["plan"]},
        nonce=planned["nonce"],
    )
    return {
        "release_id": release_id,
        "transfer": transfer.as_dict(),
        "receiver_plan": planned["plan"],
        "receiver_acceptance": accepted,
    }


def stage(args: argparse.Namespace) -> CommandResult:
    from iii_deployment.contracts import ContractRegistry

    selected = None
    release_id = None
    try:
        selected = _target(args)
        _require_remote(selected)
        identifier, store = _operation(args)
        release_id = _component(args.component)[0]["release_id"]
        actual = _stage_component(
            _manager(),
            args.component,
            selected=selected,
            operation_id=identifier,
            status_index=args.status_index,
        )
        record = {
            "schema": "iii.deployment-actual/v1",
            "operation_id": identifier,
            "release_id": release_id,
            "target": selected,
            "phases": [{"name": "drone-stage", "state": "accepted", **actual}],
            "px4_write_performed": False,
        }
        record["actual_id"] = content_id(record)
        ContractRegistry(_workspace() / "deployment/schemas/v1").validate(
            "deployment-actual", record
        )
        path = store.write_record(identifier, "deployment-actual.json", record)
    except Exception as exc:
        return _reject("iii deploy stage", exc, target=selected, release_id=release_id)
    return CommandResult(
        command="iii deploy stage",
        outcome=Outcome.SUCCESS,
        summary=(
            f"Release {release_id} was transferred and durably accepted for staging "
            f"by {selected['endpoint']}."
        ),
        code="III_DEPLOY_STAGE_ACCEPTED",
        target=selected["endpoint"],
        profile=selected["runtime_profile"],
        release_id=release_id,
        evidence=(
            str(path),
            actual["transfer"]["transfer_id"],
            actual["receiver_plan"]["plan_id"],
        ),
        payload_schema="iii.deploy-stage/v1",
        payload=actual,
        next_actions=(
            NextAction(
                ("iii", "deploy", "status", "--target", "real"),
                "Verify terminal staging state after detached execution.",
            ),
        ),
    )


def _activation(
    manager,
    *,
    action: str,
    operation_id: str,
    selected: Mapping[str, Any],
    release_id: str,
    checkpoint: str,
    px4_activation_evidence: Mapping[str, Any],
    qualified: bool = False,
    decisions: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    planning_action = "plan-activate" if action == "activate" else "plan-rollback"
    key = "activation" if action == "activate" else "rollback"
    parameters: dict[str, Any] = {
        "release_id": release_id,
        "configuration_checkpoint_id": checkpoint,
        "px4_activation_evidence": dict(px4_activation_evidence),
    }
    if action == "activate":
        parameters["explicit_qualified_action"] = qualified
        if decisions:
            parameters["configuration_reconciliation_decisions"] = dict(decisions)
    planned = _request(
        manager,
        action=planning_action,
        operation_id=operation_id,
        payload={key: parameters, "target": _binding(selected)},
    )
    preflight = planned.get("preflight")
    accepted = None
    if not isinstance(preflight, Mapping) or preflight.get("ready") is not False:
        accepted = _request(
            manager,
            action=action,
            operation_id=operation_id,
            payload={"plan": planned["plan"]},
            nonce=planned["nonce"],
        )
    return {
        "receiver_plan": planned["plan"],
        "preflight": preflight,
        "receiver_acceptance": accepted,
    }


def _reconciliation_decisions(values: Sequence[str]) -> dict[str, str]:
    decisions: dict[str, str] = {}
    for value in values:
        key, separator, decision = value.rpartition("=")
        if (
            not separator
            or not key
            or decision
            not in {
                "use_old",
                "use_new_default",
            }
        ):
            raise ValueError(
                "--decision requires SET_REFERENCE:PARAMETER=use_old|use_new_default"
            )
        if key in decisions:
            raise ValueError(f"configuration decision was repeated: {key}")
        decisions[key] = decision
    return dict(sorted(decisions.items()))


def _retain_configuration_review(
    *,
    store: OperationStore,
    identifier: str,
    selected: Mapping[str, Any],
    release_id: str,
    checkpoint: str,
    qualified: bool,
    actual: Mapping[str, Any],
    origin_command: str = "iii deploy activate",
) -> tuple[Path, dict[str, Any]]:
    preflight = actual.get("preflight")
    reconciliation = (
        preflight.get("configuration_reconciliation")
        if isinstance(preflight, Mapping)
        else None
    )
    if not isinstance(reconciliation, Mapping) or not reconciliation.get(
        "review_items"
    ):
        reasons = (
            preflight.get("rejection_reasons", [])
            if isinstance(preflight, Mapping)
            else []
        )
        raise ValueError(
            "receiver activation preflight rejected: " + "; ".join(map(str, reasons))
        )
    review = {
        "schema": "iii.configuration-reconciliation-review-request/v1",
        "review_id": "0" * 64,
        "operation_id": identifier,
        "origin_command": origin_command,
        "target": {
            "endpoint": selected["endpoint"],
            "logical_id": selected["logical_id"],
            "runtime_profile": selected["runtime_profile"],
        },
        "release_id": release_id,
        "source_configuration_checkpoint_id": checkpoint,
        "explicit_qualified_action": qualified,
        "receiver_plan_id": actual["receiver_plan"]["plan_id"],
        "reconciliation": dict(reconciliation),
    }
    review["review_id"] = content_id(
        {key: value for key, value in review.items() if key != "review_id"}
    )
    from iii_deployment.contracts import ContractRegistry

    ContractRegistry(_workspace() / "deployment/schemas/v1").validate(
        "configuration-reconciliation-review-request", review
    )
    path = store.write_record(
        identifier, "configuration-reconciliation-review.json", review
    )
    return path, review


def _configuration_review_result(
    *,
    command: str,
    selected: Mapping[str, Any],
    release_id: str,
    identifier: str,
    path: Path,
    review: Mapping[str, Any],
) -> CommandResult:
    return CommandResult(
        command=command,
        outcome=Outcome.REJECTED,
        summary="Activation is paused for explicit configuration reintroduction review.",
        code="III_DEPLOY_CONFIGURATION_REVIEW_REQUIRED",
        operation_id=identifier,
        state="review-required",
        target=selected["endpoint"],
        profile=selected["runtime_profile"],
        release_id=release_id,
        findings=(
            Finding(
                "III_DEPLOY_CONFIGURATION_REVIEW_REQUIRED",
                "Every reintroduced key requires use_old or use_new_default before receiver mutation.",
            ),
        ),
        evidence=(str(path), review["review_id"]),
        payload_schema=review["schema"],
        payload=dict(review),
        next_actions=(
            NextAction(
                ("iii", "deploy", "continue", identifier),
                "Continue from this immutable review with one --decision per review item.",
                mutating=True,
                prerequisites=(
                    "Review old value validity, new default, and provenance for every item.",
                ),
                confirmation_required=True,
                operation_id=identifier,
            ),
        ),
    )


def _px4_activation_evidence(
    args: argparse.Namespace,
    *,
    selected: Mapping[str, Any],
    release_id: str,
) -> dict[str, Any]:
    """Request the receiver-owned, read-only Ethernet FMU release audit."""

    parameter_profile = str(selected.get("parameter_profile", ""))
    if parameter_profile != "real":
        raise ValueError("deployment PX4 release audit requires the real target profile")
    operation_id = getattr(args, "_iii_operation_id", None)
    if not isinstance(operation_id, str):
        raise ValueError("PX4 release audit requires a retained operation ID")
    result = _manager().px4_audit(
        release_id=release_id, operation_id=operation_id
    )
    audit = result.get("audit")
    evidence = result.get("activation_evidence")
    if not isinstance(audit, dict) or audit.get("healthy") is not True:
        findings = audit.get("findings", []) if isinstance(audit, dict) else []
        codes = ", ".join(
            str(item.get("code")) for item in findings if isinstance(item, dict)
        )
        raise PX4ReleaseRequiredError(
            "PX4 does not match the staged release"
            + (f" ({codes})" if codes else "")
        )
    if not isinstance(evidence, dict) or evidence.get("healthy") is not True:
        raise PX4ReleaseRequiredError("PX4 activation evidence is incomplete")
    return evidence


def activate(args: argparse.Namespace) -> CommandResult:
    return _activate_or_rollback(args, "activate")


def rollback(args: argparse.Namespace) -> CommandResult:
    return _activate_or_rollback(args, "rollback")


def continue_configuration_review(args: argparse.Namespace) -> CommandResult:
    from iii_deployment.contracts import ContractRegistry

    selected = None
    release_id = None
    try:
        selected = _target(args)
        _require_remote(selected)
        identifier, store = _operation(args)
        review = store.load_record(
            args.review_operation_id, "configuration-reconciliation-review.json"
        )
        if review is None:
            raise ValueError("the referenced configuration review is unavailable")
        expected_fields = {
            "schema",
            "review_id",
            "operation_id",
            "origin_command",
            "target",
            "release_id",
            "source_configuration_checkpoint_id",
            "explicit_qualified_action",
            "receiver_plan_id",
            "reconciliation",
        }
        if (
            set(review) != expected_fields
            or review["schema"] != "iii.configuration-reconciliation-review-request/v1"
            or review["operation_id"] != args.review_operation_id
            or review["origin_command"] != "iii deploy activate"
            or review["review_id"]
            != content_id(
                {key: value for key, value in review.items() if key != "review_id"}
            )
        ):
            raise ValueError("the retained configuration review is invalid or edited")
        ContractRegistry(_workspace() / "deployment/schemas/v1").validate(
            "configuration-reconciliation-review-request", review
        )
        if review["target"] != {
            "endpoint": selected["endpoint"],
            "logical_id": selected["logical_id"],
            "runtime_profile": selected["runtime_profile"],
        }:
            raise ValueError("the retained review targets another endpoint or profile")
        release_id = review["release_id"]
        decisions = _reconciliation_decisions(args.decision)
        items = review["reconciliation"].get("review_items")
        if not isinstance(items, list):
            raise ValueError("the retained review item inventory is malformed")
        expected_decisions = {
            f"{item['set_reference']}:{item['parameter']}"
            for item in items
            if isinstance(item, Mapping)
            and isinstance(item.get("set_reference"), str)
            and isinstance(item.get("parameter"), str)
        }
        if set(decisions) != expected_decisions or len(expected_decisions) != len(
            items
        ):
            raise ValueError(
                "decisions must cover every and only retained configuration review item"
            )
        px4_evidence = _px4_activation_evidence(
            args, selected=selected, release_id=release_id
        )
        actual = _activation(
            _manager(),
            action="activate",
            operation_id=identifier,
            selected=selected,
            release_id=release_id,
            checkpoint=review["source_configuration_checkpoint_id"],
            px4_activation_evidence=px4_evidence,
            qualified=review["explicit_qualified_action"],
            decisions=decisions,
        )
        if actual["receiver_acceptance"] is None:
            path, refreshed = _retain_configuration_review(
                store=store,
                identifier=identifier,
                selected=selected,
                release_id=release_id,
                checkpoint=review["source_configuration_checkpoint_id"],
                qualified=review["explicit_qualified_action"],
                actual=actual,
            )
            return _configuration_review_result(
                command="iii deploy continue",
                selected=selected,
                release_id=release_id,
                identifier=identifier,
                path=path,
                review=refreshed,
            )
        record = {
            "schema": "iii.deployment-actual/v1",
            "operation_id": identifier,
            "release_id": release_id,
            "target": selected,
            "phases": [
                {
                    "name": "activate",
                    "state": "accepted",
                    "source_review_id": review["review_id"],
                    "decisions": decisions,
                    **actual,
                }
            ],
            "px4_activation_evidence_id": px4_evidence["evidence_id"],
            "px4_write_performed": False,
        }
        record["actual_id"] = content_id(record)
        ContractRegistry(_workspace() / "deployment/schemas/v1").validate(
            "deployment-actual", record
        )
        path = store.write_record(identifier, "deployment-actual.json", record)
    except Exception as exc:
        return _reject(
            "iii deploy continue", exc, target=selected, release_id=release_id
        )
    return CommandResult(
        command="iii deploy continue",
        outcome=Outcome.SUCCESS,
        summary=(
            f"Configuration decisions were revalidated and release {release_id} "
            "was accepted for detached activation."
        ),
        code="III_DEPLOY_CONFIGURATION_REVIEW_CONTINUED",
        target=selected["endpoint"],
        profile=selected["runtime_profile"],
        release_id=release_id,
        evidence=(str(path), review["review_id"], actual["receiver_plan"]["plan_id"]),
        payload_schema="iii.deploy-configuration-review-continuation/v1",
        payload={"review": review, "decisions": decisions, "activation": actual},
        next_actions=(
            NextAction(
                ("iii", "deploy", "status", "--target", "real"),
                "Verify the detached operation and paired configuration checkpoint.",
            ),
        ),
    )


def _activate_or_rollback(args: argparse.Namespace, action: str) -> CommandResult:
    from iii_deployment.contracts import ContractRegistry

    selected = None
    try:
        selected = _target(args)
        _require_remote(selected)
        identifier, store = _operation(args)
        px4_evidence = _px4_activation_evidence(
            args,
            selected=selected,
            release_id=args.release_id,
        )
        actual = _activation(
            _manager(),
            action=action,
            operation_id=identifier,
            selected=selected,
            release_id=args.release_id,
            checkpoint=args.configuration_checkpoint_id,
            px4_activation_evidence=px4_evidence,
            qualified=getattr(args, "qualified", False),
            decisions=_reconciliation_decisions(getattr(args, "decision", [])),
        )
        if actual["receiver_acceptance"] is None:
            path, review = _retain_configuration_review(
                store=store,
                identifier=identifier,
                selected=selected,
                release_id=args.release_id,
                checkpoint=args.configuration_checkpoint_id,
                qualified=getattr(args, "qualified", False),
                actual=actual,
            )
            return _configuration_review_result(
                command=f"iii deploy {action}",
                selected=selected,
                release_id=args.release_id,
                identifier=identifier,
                path=path,
                review=review,
            )
        record = {
            "schema": "iii.deployment-actual/v1",
            "operation_id": identifier,
            "release_id": args.release_id,
            "target": selected,
            "phases": [{"name": action, "state": "accepted", **actual}],
            "px4_activation_evidence_id": px4_evidence["evidence_id"],
            "px4_write_performed": False,
        }
        record["actual_id"] = content_id(record)
        ContractRegistry(_workspace() / "deployment/schemas/v1").validate(
            "deployment-actual", record
        )
        path = store.write_record(identifier, "deployment-actual.json", record)
    except Exception as exc:
        return _reject(
            f"iii deploy {action}", exc, target=selected, release_id=args.release_id
        )
    return CommandResult(
        command=f"iii deploy {action}",
        outcome=Outcome.SUCCESS,
        summary=(
            f"Release {args.release_id} was durably accepted for {action} on "
            f"{selected['endpoint']}."
        ),
        code=f"III_DEPLOY_{action.upper()}_ACCEPTED",
        target=selected["endpoint"],
        profile=selected["runtime_profile"],
        release_id=args.release_id,
        evidence=(str(path), actual["receiver_plan"]["plan_id"]),
        payload_schema=f"iii.deploy-{action}/v1",
        payload=actual,
        next_actions=(
            NextAction(
                ("iii", "deploy", "status", "--target", "real"),
                "Verify the detached operation and final selector state.",
            ),
        ),
    )


def _source_impact(
    root: Path,
    include: Sequence[str],
    exclude: Sequence[str],
    components: Sequence[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    from iii_deployment.contracts import ContractRegistry, content_identity
    from iii_deployment.source import (
        capture_source_snapshot,
        load_source_policy,
        validate_component_selection,
    )
    from iii_deployment.field_impact import detailed_field_impact, mission_registry

    registry = ContractRegistry(root / "deployment/schemas/v1")
    policy = load_source_policy(root / "deployment/source-policy.json", registry)
    snapshot = capture_source_snapshot(root, policy, registry)
    normalized_components = set(components)
    if "both" in normalized_components:
        normalized_components.remove("both")
        normalized_components.update({"drone", "gc"})
    requested = sorted(normalized_components or snapshot["impact"]["components"])
    if include and "drone" not in requested:
        requested.append("drone")
        requested.sort()
    if not requested:
        raise ValueError(
            "field deployment has no impacted component; select an explicit component"
        )
    validate_component_selection(snapshot["impact"], requested)
    changed = snapshot["changed_paths"]
    groups = {
        "missions": sorted(
            path
            for path in changed
            if "III-Drone-Mission/mission_specification/" in path
            or path.endswith("III-Drone-Mission/CMakeLists.txt")
        ),
        "behavior_trees": sorted(
            path for path in changed if "III-Drone-Mission/behavior_trees/" in path
        ),
        "parameters": sorted(
            path for path in changed if "III-Drone-Configuration/config/" in path
        ),
        "px4_manifest_drift": sorted(
            path
            for path in changed
            if path.startswith("PX4-Autopilot/") or "px4" in path.lower()
        ),
    }
    detail = detailed_field_impact(root, changed)
    registry_entries = mission_registry(root)
    invalid_includes = sorted(
        mission_id
        for mission_id in set(include)
        if registry_entries.get(mission_id, {}).get("classification") != "experimental"
    )
    if invalid_includes:
        raise ValueError(
            "only registered experimental mission entries may be explicitly included: "
            + ", ".join(invalid_includes)
        )
    inferred = sorted(
        item["id"]
        for item in detail["missions"]["entries"]
        if item["classification"] == "experimental"
    )
    if set(exclude) - set(inferred):
        raise ValueError("only inferred experimental mission entries may be excluded")
    selected = sorted((set(inferred) | set(include)) - set(exclude))
    impact = {
        "schema": "iii.field-impact/v1",
        "source_identity": snapshot["content_identity"],
        "components": requested,
        "component_reasons": snapshot["impact"]["causes"],
        "groups": groups,
        "detail": detail,
        "missions": {
            "inferred": inferred,
            "included": sorted(set(include)),
            "excluded": sorted(set(exclude)),
            "selected": selected,
        },
        "px4_write_planned": False,
        "ordering": [name for name in ("gc", "drone") if name in requested],
        "resulting_identities": {
            "mission_catalog": detail["missions"]["catalog_identity"],
            "configuration": detail["parameters"]["configuration_identity"],
            "component_source": snapshot["content_identity"],
        },
    }
    impact["impact_id"] = content_identity(impact)
    registry.validate("field-impact", impact)
    return snapshot, impact


def _gc_application_store(args: argparse.Namespace):
    from .gc_application import _store

    environment = dict(_environment(args))
    if getattr(args, "trusted_signers", None):
        environment["III_GC_TRUSTED_SIGNERS"] = str(
            Path(args.trusted_signers).expanduser().resolve()
        )
    return _store(argparse.Namespace(_iii_environment=environment))


def _install_gc_application(
    component: Path,
    *,
    release_id: str,
    store,
) -> dict[str, Any]:
    """Verify, import, and stage a release-bound local GC/QGC application."""
    result = store.stage(component)
    if result.get("release_id") != release_id:
        raise ValueError("staged GC application release identity differs")
    return result


def _remote_status(
    manager, selected: Mapping[str, Any], operation_id: str
) -> dict[str, Any]:
    value = _request(
        manager,
        action="status",
        operation_id=operation_id,
        payload={},
    )
    expected = {
        "logical_id": str(selected["logical_id"]),
        "profile": str(selected["runtime_profile"]),
    }
    if value.get("target") != expected:
        raise ValueError("receiver status advertises an unexpected logical target")
    return value


def _gc_safety(
    remote: Mapping[str, Any], selected: Mapping[str, Any]
) -> dict[str, Any]:
    observation = remote.get("activation_safety")
    if not isinstance(observation, Mapping):
        return {
            "connected": True,
            "profile": selected["runtime_profile"],
            "source": "receiver-safety-unavailable",
        }
    return {"connected": True, **dict(observation), "source": "authenticated-receiver"}


def _await_receiver_operation(
    manager,
    selected: Mapping[str, Any],
    operation_id: str,
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            status = _remote_status(manager, selected, operation_id)
        except Exception as exc:
            last_error = exc
            time.sleep(0.25)
            continue
        operation = status.get("operation")
        if isinstance(operation, Mapping) and operation.get("state") == "completed":
            return {"operation": dict(operation), "status": status}
        if isinstance(operation, Mapping) and operation.get("state") in {
            "failed",
            "cancelled",
        }:
            raise ValueError(
                f"receiver operation {operation_id} ended {operation['state']}: "
                f"{operation.get('failure') or operation.get('checkpoint')}"
            )
        time.sleep(0.25)
    raise ValueError(
        f"receiver operation {operation_id} did not reach an authenticated terminal state"
        + (f": {last_error}" if last_error is not None else "")
    )


def _child_operation_id(parent: str, suffix: str) -> str:
    candidate = f"{parent}-{suffix}"
    if len(candidate) <= 64:
        return candidate
    identity = hashlib.sha256(candidate.encode()).hexdigest()[:32]
    return f"{parent[:31].rstrip('-')}-{identity}"


def field(args: argparse.Namespace) -> CommandResult:
    from iii_deployment.contracts import ContractRegistry

    selected = None
    release_id = None
    identifier = getattr(args, "_iii_operation_id", None)
    phases: list[dict[str, Any]] = []
    try:
        selected = _target(args)
        _require_remote(selected)
        if args.activate and not args.configuration_checkpoint_id:
            raise ValueError(
                "--configuration-checkpoint-id is required when --activate is requested"
            )
        identifier, store = _operation(args)
        root = _workspace()
        snapshot, impact = _source_impact(
            root, args.include_mission, args.exclude_mission, args.component
        )
        bundle_root = args.bundle_set.resolve()
        selected_components = set(impact["components"])
        selected_release = _field_bundle_release(
            bundle_root,
            components=impact["components"],
            source_identity=snapshot["content_identity"],
            selected_missions=impact["missions"]["selected"],
        )
        release_id = selected_release["release_id"]
        plan = {
            "schema": "iii.field-deployment-plan/v1",
            "operation_id": identifier,
            "release_id": release_id,
            "target": selected,
            "impact": impact,
            "bundle_set": str(bundle_root),
            "configuration_checkpoint_id": args.configuration_checkpoint_id,
            "mutations": [
                item
                for item in (
                    "stage-gc-application" if "gc" in selected_components else None,
                    (
                        "activate-gc-application"
                        if args.activate and "gc" in selected_components
                        else None
                    ),
                    "transfer-drone" if "drone" in selected_components else None,
                    "stage-drone" if "drone" in selected_components else None,
                    (
                        "activate-drone"
                        if args.activate and "drone" in selected_components
                        else None
                    ),
                )
                if item
            ],
            "px4_write": False,
            "px4_validation": (
                "complete-disarmed-inventory-before-activation"
                if args.activate and "drone" in selected_components
                else "not-requested"
            ),
        }
        registry = ContractRegistry(root / "deployment/schemas/v1")
        registry.validate("field-deployment-plan", plan)
        plan_path = store.write_record(identifier, "deployment-impact.json", plan)
        manager = None
        gc_store = None
        gc_previous_release = None
        gc_candidate_manifest = None
        gc_activated = False
        timeout_seconds = float(
            _environment(args).get("III_DEPLOY_AWAIT_TIMEOUT_SEC", "1205")
        )
        if timeout_seconds <= 0:
            raise ValueError("III_DEPLOY_AWAIT_TIMEOUT_SEC must be greater than zero")
        if "gc" in selected_components:
            gc_store = _gc_application_store(args)
            gc_previous_release = gc_store.state()["active_release_id"]
            if (
                args.activate
                and "drone" in selected_components
                and gc_previous_release is None
            ):
                raise ValueError(
                    "paired field update requires a previously active GC release for deterministic rollback"
                )
            result = _install_gc_application(
                bundle_root / "gc",
                release_id=release_id,
                store=gc_store,
            )
            phases.append({"name": "gc-stage", "state": "staged", "result": result})
            gc_candidate_manifest = gc_store.release_manifest(release_id)
            if args.activate:
                manager = _manager()
                safety_status = _remote_status(
                    manager,
                    selected,
                    _child_operation_id(identifier, "gc-safety"),
                )
                activated_gc = gc_store.activate(
                    release_id,
                    operation_id=_child_operation_id(identifier, "gc-activate"),
                    safety=_gc_safety(safety_status, selected),
                    override_reason=getattr(args, "gc_override_reason", None),
                    override_confirmation=getattr(
                        args, "gc_override_confirmation", None
                    ),
                )
                gc_activated = True
                phases.append(
                    {
                        "name": "gc-activate",
                        "state": "activated",
                        "result": activated_gc,
                    }
                )
        else:
            phases.append(
                {
                    "name": "gc-stage",
                    "state": "skipped",
                    "reason": "source impact does not require GC",
                }
            )
        if "drone" in selected_components:
            manager = manager or _manager()
            try:
                stage_operation = _child_operation_id(identifier, "stage")
                staged = _stage_component(
                    manager,
                    bundle_root / "drone",
                    selected=selected,
                    operation_id=stage_operation,
                    status_index=args.status_index,
                )
                staged_terminal = _await_receiver_operation(
                    manager,
                    selected,
                    stage_operation,
                    timeout_seconds=timeout_seconds,
                )
                phases.append(
                    {
                        "name": "drone-stage",
                        "state": "completed",
                        "result": {**staged, "terminal": staged_terminal},
                    }
                )
                if args.activate:
                    px4_evidence = _px4_activation_evidence(
                        args,
                        selected=selected,
                        release_id=release_id,
                    )
                    phases.append(
                        {
                            "name": "px4-validate",
                            "state": "completed",
                            "result": {
                                "evidence_id": px4_evidence["evidence_id"],
                                "snapshot_id": px4_evidence["snapshot"]["snapshot_id"],
                                "manifest_id": px4_evidence["manifest_id"],
                                "writes_performed": 0,
                            },
                        }
                    )
                    activation_operation = _child_operation_id(identifier, "activate")
                    activated = _activation(
                        manager,
                        action="activate",
                        operation_id=activation_operation,
                        selected=selected,
                        release_id=release_id,
                        checkpoint=args.configuration_checkpoint_id,
                        px4_activation_evidence=px4_evidence,
                        decisions=_reconciliation_decisions(
                            getattr(args, "decision", [])
                        ),
                    )
                    if activated["receiver_acceptance"] is None:
                        review_path, review = _retain_configuration_review(
                            store=store,
                            identifier=identifier,
                            selected=selected,
                            release_id=release_id,
                            checkpoint=args.configuration_checkpoint_id,
                            qualified=False,
                            actual=activated,
                            origin_command="iii deploy field",
                        )
                        phases.append(
                            {
                                "name": "configuration-review",
                                "state": "review-required",
                                "review_id": review["review_id"],
                                "path": str(review_path),
                            }
                        )
                        raise ValueError(
                            "configuration reintroduction review is required; "
                            "rerun the exact field deployment with one --decision per retained item"
                        )
                    activated_terminal = _await_receiver_operation(
                        manager,
                        selected,
                        activation_operation,
                        timeout_seconds=timeout_seconds,
                    )
                    phases.append(
                        {
                            "name": "drone-activate",
                            "state": "completed",
                            "result": {**activated, "terminal": activated_terminal},
                        }
                    )
                else:
                    phases.append(
                        {
                            "name": "drone-activate",
                            "state": "skipped",
                            "reason": "activation was not requested",
                        }
                    )
            except Exception:
                if (
                    gc_activated
                    and gc_store is not None
                    and gc_candidate_manifest is not None
                ):
                    recovery_status = _remote_status(
                        manager,
                        selected,
                        _child_operation_id(identifier, "pair-recovery"),
                    )
                    restored_drone = recovery_status.get("active_release_manifest")
                    from iii_deployment.gc_application import (
                        application_pair_compatible,
                    )

                    if isinstance(
                        restored_drone, Mapping
                    ) and application_pair_compatible(
                        gc_candidate_manifest, restored_drone
                    ):
                        phases.append(
                            {
                                "name": "gc-reconcile",
                                "state": "retained-compatible",
                                "reason": "new GC remains compatible with the authenticated restored drone release",
                            }
                        )
                    else:
                        if gc_previous_release is None:
                            raise ValueError(
                                "drone failed and no prior GC release exists for paired rollback"
                            )
                        restored_gc = gc_store.restore_release(
                            gc_previous_release,
                            operation_id=_child_operation_id(identifier, "gc-rollback"),
                            safety=_gc_safety(recovery_status, selected),
                            override_reason=getattr(args, "gc_override_reason", None),
                            override_confirmation=getattr(
                                args, "gc_override_confirmation", None
                            ),
                        )
                        phases.append(
                            {
                                "name": "gc-reconcile",
                                "state": "rolled-back",
                                "result": restored_gc,
                            }
                        )
                raise
        else:
            phases.append(
                {
                    "name": "drone-stage",
                    "state": "skipped",
                    "reason": "source impact does not require drone deployment",
                }
            )
        actual = {
            "schema": "iii.deployment-actual/v1",
            "operation_id": identifier,
            "release_id": release_id,
            "target": selected,
            "impact_id": impact["impact_id"],
            "phases": phases,
            "px4_write_performed": False,
        }
        actual["actual_id"] = hashlib.sha256(
            json.dumps(actual, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        registry.validate("deployment-actual", actual)
        actual_path = store.write_record(identifier, "deployment-actual.json", actual)
    except Exception as exc:
        if identifier:
            try:
                rejected = {
                    "schema": "iii.deployment-actual/v1",
                    "operation_id": identifier,
                    "release_id": release_id,
                    "target": selected,
                    "phases": phases,
                    "terminal_state": "rejected",
                    "error": {
                        "code": getattr(exc, "code", "III_DEPLOY_CONTRACT_REJECTED"),
                        "message": str(exc),
                    },
                    "px4_write_performed": False,
                }
                from iii_deployment.contracts import ContractRegistry

                ContractRegistry(_workspace() / "deployment/schemas/v1").validate(
                    "deployment-actual", rejected
                )
                OperationStore(default_state_root(_environment(args))).write_record(
                    identifier, "deployment-actual.json", rejected
                )
            except Exception:
                pass
        return _reject("iii deploy field", exc, target=selected, release_id=release_id)
    report = {
        "display": _impact_display(impact, actual),
        "impact": impact,
        "actual": actual,
    }
    ContractRegistry(_workspace() / "deployment/schemas/v1").validate(
        "field-deployment-report", report
    )
    return CommandResult(
        command="iii deploy field",
        outcome=Outcome.SUCCESS,
        summary=(
            f"Field deployment {release_id} completed requested durable acceptance "
            "phases in GC-before-drone order."
        ),
        code="III_FIELD_DEPLOYMENT_ACCEPTED",
        target=selected["endpoint"],
        profile=selected["runtime_profile"],
        release_id=release_id,
        evidence=(
            str(plan_path),
            str(actual_path),
            impact["impact_id"],
            actual["actual_id"],
        ),
        payload_schema="iii.field-deployment-report/v1",
        payload=report,
        next_actions=(
            NextAction(
                ("iii", "deploy", "status", "--target", "real"),
                "Verify detached receiver completion before runtime use.",
            ),
        ),
    )


def operations_list(args: argparse.Namespace) -> CommandResult:
    try:
        store = OperationStore(default_state_root(_environment(args)))
        rows = []
        for identifier in store.list_operations():
            state = store.load_state(identifier)
            rows.append(
                {
                    "operation_id": identifier,
                    "state": state and state["state"],
                    "updated_at": state and state["updated_at"],
                    "records": sorted(
                        path.name
                        for path in store.operation_path(identifier).glob("*.json")
                    ),
                }
            )
    except Exception as exc:
        return _reject("iii deploy operations list", exc)
    return CommandResult(
        command="iii deploy operations list",
        outcome=Outcome.SUCCESS,
        summary=f"Inventoried {len(rows)} retained deployment operation(s).",
        code="III_DEPLOY_OPERATIONS_LISTED",
        payload_schema="iii.deploy-operation-list/v1",
        payload={"operations": rows},
        terminal_reason=(
            "Compact records were inspected without artifact-cache traversal or mutation."
        ),
    )


def operations_show(args: argparse.Namespace) -> CommandResult:
    try:
        store = OperationStore(default_state_root(_environment(args)))
        root = store.operation_path(args.operation)
        if not root.is_dir() or root.is_symlink():
            raise ValueError("operation record does not exist or is unsafe")
        records = {
            path.name: _read_json(path, label=path.name)
            for path in sorted(root.glob("*.json"))
        }
    except Exception as exc:
        return _reject("iii deploy operations show", exc)
    return CommandResult(
        command="iii deploy operations show",
        outcome=Outcome.SUCCESS,
        summary=f"Loaded retained operation {args.operation}.",
        code="III_DEPLOY_OPERATION_SHOWN",
        operation_id=args.operation,
        state=records.get("state.json", {}).get("state", "recorded"),
        payload_schema="iii.deploy-operation-detail/v1",
        payload={"records": records},
        terminal_reason=(
            "The exact plan, impact, actual result, and diagnostics were displayed."
        ),
    )


def _record_protection(record: Mapping[str, Any]) -> list[str]:
    reasons = []
    if record.get("protected") is True:
        reasons.append("declared-protected")
    if record.get("references"):
        reasons.append("cross-domain-reference")
    if record.get("review_status") not in {None, "resolved", "acknowledged"}:
        reasons.append("unresolved-review")
    if record.get("acknowledged") is False:
        reasons.append("unacknowledged-failure")
    schema = str(record.get("schema", ""))
    if "qualified" in schema and ("evidence" in schema or "release" in schema):
        reasons.append("qualified-release-evidence")
    return sorted(set(reasons))


def _prune_preflight(args: argparse.Namespace) -> dict[str, Any]:
    if args.days < 1:
        raise ValueError("prune age must be at least one day")
    store = OperationStore(default_state_root(_environment(args)))
    threshold = datetime.now(timezone.utc).timestamp() - args.days * 86400
    candidates = []
    protected = []
    for identifier in store.list_operations():
        state = store.load_state(identifier)
        if not state or state["state"] not in set(args.status):
            continue
        updated = datetime.fromisoformat(
            state["updated_at"].replace("Z", "+00:00")
        ).timestamp()
        if updated >= threshold:
            continue
        records = {
            path.name: _read_json(path, label=path.name)
            for path in sorted(store.operation_path(identifier).glob("*.json"))
        }
        reasons = sorted(
            {
                reason
                for record in records.values()
                for reason in _record_protection(record)
            }
        )
        row = {
            "operation_id": identifier,
            "state": state["state"],
            "updated_at": state["updated_at"],
            "records": {
                name: content_id(value) for name, value in sorted(records.items())
            },
        }
        if reasons:
            protected.append({**row, "reasons": reasons})
        else:
            candidates.append(row)
    return {
        "schema": "iii.deploy-operation-prune-preflight/v1",
        "age_days": args.days,
        "statuses": sorted(set(args.status)),
        "candidates": candidates,
        "protected": protected,
        "cache_mutation": False,
    }


def operations_prune(args: argparse.Namespace) -> CommandResult:
    try:
        retained = getattr(args, "_iii_retained_plan", None)
        if not isinstance(retained, dict) or not isinstance(
            retained.get("preflight"), dict
        ):
            raise ValueError("an exact retained prune preflight is required")
        preflight = retained["preflight"]
        store = OperationStore(default_state_root(_environment(args)))
        removed = []
        for candidate in preflight["candidates"]:
            identifier = candidate["operation_id"]
            store.remove_operation(identifier, expected_records=candidate["records"])
            removed.append(identifier)
    except Exception as exc:
        return _reject("iii deploy operations prune", exc)
    return CommandResult(
        command="iii deploy operations prune",
        outcome=Outcome.SUCCESS,
        summary=(
            f"Pruned {len(removed)} explicitly selected unprotected operation record(s)."
        ),
        code="III_DEPLOY_OPERATIONS_PRUNED",
        payload_schema="iii.deploy-operation-prune/v1",
        payload={
            "age_days": args.days,
            "statuses": sorted(set(args.status)),
            "removed": removed,
            "protected": preflight["protected"],
            "cache_mutation": False,
        },
        terminal_reason=(
            "Only exact old terminal records without declared protection or references "
            "were removed; artifact caches were untouched."
        ),
    )


def configuration_capture(args: argparse.Namespace) -> CommandResult:
    selected = None
    try:
        selected = _target(args)
        _require_remote(selected)
        remote = _manager().verify_logical_target(
            profile=selected["runtime_profile"],
            operation_id="configuration-capture",
        )
        live = remote.get("live_state", {})
        identity = live.get("configuration_hash") if isinstance(live, dict) else None
        release_id = live.get("active_release_id") if isinstance(live, dict) else None
        if not identity:
            raise ValueError(
                "receiver status did not advertise a configuration identity"
            )
        if (
            not isinstance(release_id, str)
            or len(release_id) != 64
            or any(character not in "0123456789abcdef" for character in release_id)
        ):
            raise ValueError(
                "receiver status did not advertise an active release identity"
            )
    except Exception as exc:
        return _reject("iii deploy configuration-capture", exc, target=selected)
    return CommandResult(
        command="iii deploy configuration-capture",
        outcome=Outcome.SUCCESS,
        summary=(
            f"Captured configuration identity {identity} from {selected['endpoint']}."
        ),
        code="III_DEPLOY_CONFIGURATION_CAPTURED",
        target=selected["endpoint"],
        profile=selected["runtime_profile"],
        release_id=release_id,
        evidence=(identity,),
        payload_schema="iii.configuration-capture/v1",
        payload={
            "configuration_hash": identity,
            "active_release_id": release_id,
            "receiver": remote,
        },
        terminal_reason=(
            "This read-only capture retained no secret values and performed no mutation."
        ),
    )


def _target_option(
    parser: argparse.ArgumentParser, *, default: str | None = None
) -> None:
    parser.add_argument(
        "--target",
        choices=("sim", "real"),
        default=default,
        help="explicit per-command runtime target",
    )


def initialize(parser: argparse.ArgumentParser) -> None:
    from . import logs

    subparsers = parser.add_subparsers(dest="deploy_command")

    status_parser = subparsers.add_parser(
        "status", help="inspect authenticated receiver and deployment state"
    )
    _target_option(status_parser, default="real")
    status_parser.add_argument(
        "--operation", help="include the exact retained local operation records"
    )
    status_parser.set_defaults(func=status, _iii_mutating=False)

    plan_parser = subparsers.add_parser(
        "plan", help="compute detailed field-development impact without mutation"
    )
    plan_parser.add_argument("--bundle-set", type=Path)
    plan_parser.add_argument(
        "--component", choices=("gc", "drone", "both"), action="append", default=[]
    )
    plan_parser.add_argument("--include-mission", action="append", default=[])
    plan_parser.add_argument("--exclude-mission", action="append", default=[])
    _target_option(plan_parser, default="real")
    plan_parser.set_defaults(func=plan, _iii_mutating=False)

    inspect_parser = subparsers.add_parser(
        "inspect", help="inspect a local component bundle"
    )
    inspect_parser.add_argument("component", type=Path)
    inspect_parser.add_argument(
        "--trusted-signers",
        type=Path,
        help="bundle signer trust store (defaults to III_RELEASE_TRUSTED_SIGNERS)",
    )
    _target_option(inspect_parser)
    inspect_parser.set_defaults(func=inspect, _iii_mutating=False)

    stage_parser = subparsers.add_parser(
        "stage", help="transfer and durably stage a drone bundle"
    )
    stage_parser.add_argument("component", type=Path)
    stage_parser.add_argument("--status-index", type=Path)
    _target_option(stage_parser, default="real")
    stage_parser.set_defaults(func=stage, _iii_mutating=True)

    receiver_update = subparsers.add_parser(
        "receiver-update", help="inspect or apply a signed receiver A/B update"
    )
    receiver_update_commands = receiver_update.add_subparsers(
        dest="receiver_update_command"
    )
    receiver_inspect = receiver_update_commands.add_parser(
        "inspect", help="verify a signed receiver update without mutation"
    )
    receiver_inspect.add_argument("bundle", type=Path)
    receiver_inspect.add_argument("--trust", type=Path, required=True)
    receiver_inspect.set_defaults(func=receiver_update_inspect, _iii_mutating=False)
    receiver_apply = receiver_update_commands.add_parser(
        "apply", help="transfer, plan, and accept a signed receiver A/B update"
    )
    receiver_apply.add_argument("bundle", type=Path)
    receiver_apply.add_argument("--trust", type=Path, required=True)
    _target_option(receiver_apply, default="real")
    receiver_apply.set_defaults(func=receiver_update_apply, _iii_mutating=True)

    for name, handler in (("activate", activate), ("rollback", rollback)):
        action_parser = subparsers.add_parser(
            name, help=f"plan and accept receiver {name}"
        )
        action_parser.add_argument("release_id")
        action_parser.add_argument(
            "--configuration-checkpoint-id",
            required=True,
            help=(
                "current/source checkpoint for activation; paired rollback checkpoint "
                "for explicit rollback"
            ),
        )
        if name == "activate":
            action_parser.add_argument(
                "--qualified",
                action="store_true",
                help="declare explicit qualified activation authority",
            )
            action_parser.add_argument(
                "--decision",
                action="append",
                default=[],
                metavar="SET:PARAMETER=CHOICE",
                help="resolve one reviewed reintroduction with use_old or use_new_default",
            )
        _target_option(action_parser, default="real")
        action_parser.set_defaults(func=handler, _iii_mutating=True)

    continue_parser = subparsers.add_parser(
        "continue", help="continue a retained configuration reintroduction review"
    )
    continue_parser.add_argument("review_operation_id")
    continue_parser.add_argument(
        "--decision",
        action="append",
        default=[],
        required=True,
        metavar="SET:PARAMETER=CHOICE",
        help="resolve one exact retained review item with use_old or use_new_default",
    )
    _target_option(continue_parser, default="real")
    continue_parser.set_defaults(func=continue_configuration_review, _iii_mutating=True)

    field_parser = subparsers.add_parser(
        "field", help="deploy one field-development bundle set"
    )
    field_parser.add_argument("--bundle-set", required=True, type=Path)
    field_parser.add_argument(
        "--configuration-checkpoint-id",
        help=(
            "currently selected source checkpoint reconciled by receiver activation "
            "(required with --activate; staging does not require one)"
        ),
    )
    field_parser.add_argument("--status-index", type=Path)
    field_parser.add_argument(
        "--trusted-signers", type=Path, help="field bundle signer trust store"
    )
    field_parser.add_argument(
        "--component", choices=("gc", "drone", "both"), action="append", default=[]
    )
    field_parser.add_argument("--include-mission", action="append", default=[])
    field_parser.add_argument("--exclude-mission", action="append", default=[])
    field_parser.add_argument("--activate", action="store_true")
    field_parser.add_argument(
        "--decision",
        action="append",
        default=[],
        metavar="SET:PARAMETER=CHOICE",
        help="resolve reviewed reintroduction values during drone activation",
    )
    field_parser.add_argument(
        "--gc-override-reason",
        help="separately audited GC recovery reason when receiver safety is unavailable",
    )
    field_parser.add_argument(
        "--gc-override-confirmation",
        help="exact GC recovery warning confirmation",
    )
    _target_option(field_parser, default="real")
    field_parser.set_defaults(func=field, _iii_mutating=True)

    capture_parser = subparsers.add_parser(
        "configuration-capture",
        help="capture authenticated target configuration identity",
    )
    _target_option(capture_parser, default="real")
    capture_parser.set_defaults(func=configuration_capture, _iii_mutating=False)

    diagnostics = subparsers.add_parser(
        "diagnostics", help="pull immutable deployment activation diagnostics"
    )
    diagnostic_commands = diagnostics.add_subparsers(dest="diagnostics_command")
    diagnostics_pull = diagnostic_commands.add_parser(
        "pull", help="verify and retain deployment diagnostics locally"
    )
    diagnostics_pull.add_argument("--destination", type=Path)
    _target_option(diagnostics_pull, default="real")
    diagnostics_pull.set_defaults(
        func=logs.pull,
        log_domain="diagnostics",
        _iii_mutating=True,
        _iii_plan_provider=logs.pull_preflight,
    )

    operations = subparsers.add_parser(
        "operations", help="inspect or explicitly prune compact deployment records"
    )
    commands = operations.add_subparsers(dest="operation_command")
    list_parser = commands.add_parser("list")
    list_parser.set_defaults(func=operations_list, _iii_mutating=False)
    show_parser = commands.add_parser("show")
    show_parser.add_argument("operation")
    show_parser.set_defaults(func=operations_show, _iii_mutating=False)
    prune_parser = commands.add_parser("prune")
    prune_parser.add_argument("--days", type=int, required=True)
    prune_parser.add_argument(
        "--status",
        choices=("completed", "cancelled"),
        action="append",
        required=True,
    )
    prune_parser.set_defaults(
        func=operations_prune,
        _iii_mutating=True,
        _iii_plan_provider=_prune_preflight,
    )
