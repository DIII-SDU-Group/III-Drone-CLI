"""Host bootstrap, imaging, and provisioning command surface."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import sys
from typing import Any, Mapping
from uuid import uuid4

from .operation import OperationStore, default_state_root
from .result import CommandResult, Finding, NextAction, Outcome
from .runner import RequiredInput


def _environment(args: argparse.Namespace) -> Mapping[str, str]:
    return getattr(args, "_iii_environment", os.environ)


def _first_existing(candidates: list[Path], *, label: str) -> Path:
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise ValueError(
        f"{label} does not exist; checked: "
        + ", ".join(str(path) for path in candidates)
    )


def _workspace_candidate(relative: str) -> Path:
    current = Path.cwd().resolve()
    for parent in (current, *current.parents):
        candidate = parent / relative
        if candidate.exists():
            return candidate
    return current / relative


def _paths(args: argparse.Namespace) -> dict[str, Path]:
    env = _environment(args)
    prefix = Path(sys.prefix)
    schema = (
        Path(args.schema_root)
        if args.schema_root
        else _first_existing(
            [
                (
                    Path(env["III_DEPLOYMENT_SCHEMA_ROOT"])
                    if env.get("III_DEPLOYMENT_SCHEMA_ROOT")
                    else Path("/__missing__")
                ),
                prefix / "share/iii-deployment/schemas/v1",
                Path("/usr/local/share/iii-deployment/schemas/v1"),
                Path("/usr/share/iii-deployment/schemas/v1"),
                _workspace_candidate("deployment/schemas/v1"),
            ],
            label="deployment schema root",
        )
    )
    source = _first_existing(
        [
            prefix / "share/iii-deployment/provisioning/ubuntu-raspi-image.json",
            Path(
                "/usr/local/share/iii-deployment/provisioning/ubuntu-raspi-image.json"
            ),
            Path("/usr/share/iii-deployment/provisioning/ubuntu-raspi-image.json"),
            _workspace_candidate("deployment/provisioning/ubuntu-raspi-image.json"),
        ],
        label="pinned host image source",
    )
    profile = _first_existing(
        [
            prefix / "share/iii-deployment/provisioning/cloud-init-profile.json",
            Path(
                "/usr/local/share/iii-deployment/provisioning/cloud-init-profile.json"
            ),
            Path("/usr/share/iii-deployment/provisioning/cloud-init-profile.json"),
            _workspace_candidate("deployment/provisioning/cloud-init-profile.json"),
        ],
        label="cloud-init profile",
    )
    return {"schema": schema, "source": source, "profile": profile}


def _rejected(command: str, exc: Exception) -> CommandResult:
    code = getattr(exc, "code", "III_HOST_IMAGE_ERROR")
    return CommandResult(
        command=command,
        outcome=Outcome.REJECTED,
        summary="Host imaging was refused before an unauthenticated or unsafe mutation.",
        code=code,
        findings=(Finding(code, str(exc)),),
        next_actions=(
            NextAction(
                ("iii", "host", "image", "inspect", "--help"),
                "Inspect the pinned image, bootstrap inputs, and removable-media candidates.",
            ),
        ),
    )


def image_inspect(args: argparse.Namespace) -> CommandResult:
    try:
        from iii_deployment.contracts import ContractRegistry
        from iii_deployment.host_imaging import (
            inspect_devices,
            inspect_image,
            load_bootstrap_input,
            load_contract,
            render_nocloud_seed,
        )

        paths = _paths(args)
        registry = ContractRegistry(paths["schema"])
        source = load_contract(
            paths["source"],
            schema_name="host-image-source",
            registry=registry,
            label="host image source",
        )
        profile = load_contract(
            paths["profile"],
            schema_name="cloud-init-profile",
            registry=registry,
            label="cloud-init profile",
        )
        bootstrap = load_bootstrap_input(args.bootstrap_input, registry)
        image = inspect_image(args.image, source)
        seed = render_nocloud_seed(profile=profile, bootstrap=bootstrap)
        devices = inspect_devices(minimum_bytes=image["minimum_target_bytes"])
    except Exception as exc:
        return _rejected("iii host image inspect", exc)
    return CommandResult(
        command="iii host image inspect",
        outcome=Outcome.SUCCESS,
        summary=f"Verified the pinned Ubuntu {source['release']} image and enumerated {len(devices)} block device(s).",
        code="III_HOST_IMAGE_INSPECTED",
        payload_schema="iii.host-image-inspection/v1",
        payload={
            "schema": "iii.host-image-inspection/v1",
            "image": image,
            "cloud_init": {
                "profile_id": seed["profile_id"],
                "instance_id": seed["instance_id"],
                "files": seed["file_evidence"],
                "contains_network_secret": seed["contains_network_secret"],
                "secret_values_rendered": False,
            },
            "devices": devices,
            "eligible_devices": [
                row["stable_path"] for row in devices if row["eligible"]
            ],
        },
        terminal_reason="Inspection verified content and topology without writing media or retaining secret values.",
    )


def _retained_preflight(args: argparse.Namespace) -> dict[str, Any] | None:
    identifier = getattr(args, "_iii_operation_id", None)
    if not identifier:
        return None
    plan = OperationStore(default_state_root(_environment(args))).load_plan(identifier)
    value = plan and plan.get("preflight")
    return dict(value) if isinstance(value, Mapping) else None


def image_write_preflight(args: argparse.Namespace) -> dict[str, Any]:
    retained = _retained_preflight(args)
    if retained is not None:
        return retained
    from iii_deployment.host_imaging import build_image_plan

    paths = _paths(args)
    identifier = getattr(args, "_iii_operation_id", None)
    if not identifier:
        raise ValueError("host image writes require a retained operation ID")
    return build_image_plan(
        operation_id=identifier,
        image_path=args.image,
        source_path=paths["source"],
        profile_path=paths["profile"],
        bootstrap_input_path=args.bootstrap_input,
        device_path=args.device,
        schema_root=paths["schema"],
        evidence_directory=args.evidence_directory,
        backup_record=args.backup_record,
        accept_data_loss=args.accept_data_loss,
    )


def image_write(args: argparse.Namespace) -> CommandResult:
    try:
        from iii_deployment.host_imaging import apply_image_plan

        retained = getattr(args, "_iii_retained_plan", None)
        if not isinstance(retained, Mapping) or not isinstance(
            retained.get("preflight"), Mapping
        ):
            raise ValueError("an exact retained host image plan is required")
        record = apply_image_plan(
            retained["preflight"], schema_root=_paths(args)["schema"]
        )
    except RequiredInput:
        raise
    except Exception as exc:
        return _rejected("iii host image write", exc)
    return CommandResult(
        command="iii host image write",
        outcome=Outcome.SUCCESS,
        summary="The Raspberry Pi media was written, read back, seeded, flushed, and ejected.",
        code="III_HOST_IMAGE_VERIFIED",
        evidence=(
            record["record_id"],
            record["evidence_path"],
            record["image"]["raw_sha256"],
        ),
        payload_schema=record["schema"],
        payload=record,
        next_actions=(
            NextAction(
                ("iii", "host", "provision", "--help"),
                "Boot with Ethernet attached, inspect local cloud-init evidence, and resume idempotent Ansible convergence.",
                prerequisites=(
                    "Do not remove bootstrap authority until permanent access and recovery are verified.",
                ),
            ),
        ),
    )


def _provision_paths(args: argparse.Namespace) -> dict[str, Path]:
    paths = _paths(args)
    workspace = _workspace_candidate("deployment/ansible").parent.parent
    cli_root = Path(__file__).resolve().parent.parent
    executable = (
        Path(args.ansible_playbook).expanduser()
        if args.ansible_playbook
        else Path(shutil.which("ansible-playbook") or "/__missing__")
    )
    return {
        "schema": paths["schema"],
        "workspace": workspace,
        "cli": cli_root,
        "ansible": workspace / "deployment/ansible",
        "ansible_playbook": executable,
    }


def _provision_plan(args: argparse.Namespace, *, operation_id: str) -> dict[str, Any]:
    from iii_deployment.host_provision import build_plan

    paths = _provision_paths(args)
    return build_plan(
        operation_id=operation_id,
        target=args.target,
        inventory=args.inventory,
        input_path=args.inputs,
        schema_root=paths["schema"],
        ansible_root=paths["ansible"],
        workspace_root=paths["workspace"],
        cli_root=paths["cli"],
        ansible_playbook=paths["ansible_playbook"],
    )


def provision_apply_preflight(args: argparse.Namespace) -> dict[str, Any]:
    retained = _retained_preflight(args)
    if retained is not None:
        return retained
    identifier = getattr(args, "_iii_operation_id", None)
    if not identifier:
        raise ValueError("host provisioning requires a retained operation ID")
    return _provision_plan(args, operation_id=identifier)


def _provision_rejected(command: str, exc: Exception) -> CommandResult:
    code = getattr(exc, "code", "III_HOST_PROVISION_ERROR")
    return CommandResult(
        command=command,
        outcome=Outcome.REJECTED,
        summary="Host provisioning was refused before an unauthenticated, stale, or unsafe mutation.",
        code=code,
        findings=(Finding(code, str(exc)),),
        next_actions=(
            NextAction(
                ("iii", "host", "provision", "check", "--help"),
                "Validate owner-controlled inputs and preview target convergence.",
            ),
        ),
    )


def provision_check(args: argparse.Namespace) -> CommandResult:
    try:
        from iii_deployment.host_provision import check_plan

        plan = _provision_plan(args, operation_id=f"iii-check-{uuid4().hex[:20]}")
        recap = check_plan(plan, schema_root=_provision_paths(args)["schema"])
    except Exception as exc:
        return _provision_rejected("iii host provision check", exc)
    changed = int(recap["totals"]["changed"])
    return CommandResult(
        command="iii host provision check",
        outcome=Outcome.SUCCESS,
        summary=f"Authenticated the host-provisioning inputs; check mode predicts {changed} change(s).",
        code="III_HOST_PROVISION_CHECKED",
        target=args.target,
        profile=plan["profile"],
        payload_schema="iii.host-provisioning-check/v1",
        payload={
            "schema": "iii.host-provisioning-check/v1",
            "plan": plan,
            "ansible": recap,
            "mutation_performed": False,
        },
        next_actions=(
            NextAction(
                (
                    "iii",
                    "host",
                    "provision",
                    "apply",
                    "--target",
                    args.target,
                    "--inventory",
                    str(args.inventory),
                    "--inputs",
                    str(args.inputs),
                    "--dry-run",
                ),
                "Retain the exact mutating plan after reviewing check-mode drift.",
                mutating=True,
                target=args.target,
                profile=plan["profile"],
            ),
        ),
    )


def provision_apply(args: argparse.Namespace) -> CommandResult:
    try:
        from iii_deployment.host_provision import apply_plan

        retained = getattr(args, "_iii_retained_plan", None)
        if not isinstance(retained, Mapping) or not isinstance(
            retained.get("preflight"), Mapping
        ):
            raise ValueError("an exact retained host provisioning plan is required")
        report = apply_plan(
            retained["preflight"], schema_root=_provision_paths(args)["schema"]
        )
        identifier = getattr(args, "_iii_operation_id", None)
        if identifier:
            OperationStore(default_state_root(_environment(args))).write_record(
                identifier, "host-provisioning-report.json", report
            )
    except Exception as exc:
        return _provision_rejected("iii host provision apply", exc)
    return CommandResult(
        command="iii host provision apply",
        outcome=Outcome.SUCCESS,
        summary="The host converged twice, proved zero second-run drift, and revoked first-boot authority.",
        code="III_HOST_PROVISIONED",
        target=args.target,
        profile=retained["preflight"]["profile"],
        evidence=(report["report_id"],),
        payload_schema=report["schema"],
        payload=report,
        next_actions=(
            NextAction(
                ("iii", "field", "check", "--target", args.target),
                "Inspect provisioned-but-not-commissioned host readiness.",
                target=args.target,
                profile=retained["preflight"]["profile"],
            ),
        ),
    )


def _image_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--image", type=Path, required=True, help="downloaded pinned .img.xz file"
    )
    parser.add_argument(
        "--bootstrap-input",
        type=Path,
        required=True,
        help="Git-ignored owner-only per-imaging JSON input",
    )
    parser.add_argument(
        "--schema-root", type=Path, help="override deployment schema directory"
    )


def initialize(parser: argparse.ArgumentParser) -> None:
    commands = parser.add_subparsers(dest="host_command")
    image = commands.add_parser(
        "image", help="inspect or write Raspberry Pi removable media"
    )
    image_commands = image.add_subparsers(dest="host_image_command")

    inspect_parser = image_commands.add_parser(
        "inspect", help="verify profiles/image and enumerate removable media"
    )
    _image_common(inspect_parser)
    inspect_parser.set_defaults(func=image_inspect, _iii_mutating=False)

    write_parser = image_commands.add_parser(
        "write", help="perform a retained, typed-proof destructive media write"
    )
    _image_common(write_parser)
    write_parser.add_argument(
        "--device", required=True, help="exact enumerated /dev/disk/by-id path"
    )
    write_parser.add_argument(
        "--evidence-directory",
        type=Path,
        required=True,
        help="owner-controlled directory for the immutable imaging record",
    )
    authority = write_parser.add_mutually_exclusive_group(required=True)
    authority.add_argument(
        "--backup-record",
        type=Path,
        help="verified pre-reimage host backup/salvage record",
    )
    authority.add_argument(
        "--accept-data-loss",
        action="store_true",
        help="separately acknowledge unrecoverable source media in the typed proof",
    )
    write_parser.set_defaults(
        func=image_write,
        _iii_mutating=True,
        _iii_interactive=True,
        _iii_plan_provider=image_write_preflight,
    )

    provision = commands.add_parser(
        "provision", help="check or apply the idempotent aircraft host baseline"
    )
    provision_commands = provision.add_subparsers(dest="host_provision_command")

    check_parser = provision_commands.add_parser(
        "check", help="run authenticated Ansible check/diff without mutation"
    )
    _provision_common(check_parser)
    check_parser.set_defaults(func=provision_check, _iii_mutating=False)

    apply_parser = provision_commands.add_parser(
        "apply", help="apply a retained convergence and bootstrap-finalization plan"
    )
    _provision_common(apply_parser)
    apply_parser.set_defaults(
        func=provision_apply,
        _iii_mutating=True,
        _iii_plan_provider=provision_apply_preflight,
    )


def _provision_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--target", required=True, help="exact Ansible inventory host or pattern"
    )
    parser.add_argument(
        "--inventory", type=Path, required=True, help="audited Ansible inventory"
    )
    parser.add_argument(
        "--inputs",
        type=Path,
        required=True,
        help="owner-only Git-ignored host-provisioning JSON input",
    )
    parser.add_argument(
        "--schema-root", type=Path, help="override deployment schema directory"
    )
    parser.add_argument(
        "--ansible-playbook",
        type=Path,
        help="explicit audited ansible-playbook executable",
    )
