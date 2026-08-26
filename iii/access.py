"""Per-computer enrollment, authorization, verification, and revocation."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import time
from typing import Any, Mapping

from .result import CommandResult, Finding, NextAction, Outcome


def _environment(args: argparse.Namespace) -> Mapping[str, str]:
    return getattr(args, "_iii_environment", os.environ)


def _workspace() -> Path:
    current = Path.cwd().resolve()
    for candidate in (current, *current.parents):
        if (candidate / "deployment/schemas/v1").is_dir():
            return candidate
    raise ValueError("the III workspace/deployment schema root could not be located")


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


def _load_enrollment(path: Path) -> dict[str, Any]:
    from iii_deployment.identity import load_machine_enrollment

    return load_machine_enrollment(path, _registry())


def _rejected(command: str, exc: Exception) -> CommandResult:
    code = getattr(exc, "code", "III_ACCESS_REJECTED")
    return CommandResult(
        command=command,
        outcome=Outcome.REJECTED,
        summary="Machine access management was refused before unsafe mutation.",
        code=code,
        findings=(Finding(code, str(exc)),),
        next_actions=(
            NextAction(
                ("iii", "access", "list"),
                "Inspect independently authenticated SSH, Runtime API, and signing authority.",
            ),
        ),
    )


def _secure_passphrase(path: Path) -> bytes:
    resolved = path.expanduser().absolute()
    if resolved.is_symlink() or not resolved.is_file():
        raise ValueError("field signer passphrase file is missing or linked")
    metadata = resolved.stat(follow_symlinks=False)
    if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
        raise ValueError("field signer passphrase file must be owner-only")
    value = resolved.read_bytes().rstrip(b"\r\n")
    if len(value) < 12:
        raise ValueError("field signer passphrase must contain at least 12 bytes")
    return value


def _passphrase(args: argparse.Namespace) -> bytes:
    if args.signer_passphrase_file:
        return _secure_passphrase(args.signer_passphrase_file)
    from iii_deployment.field_signing_agent import passphrase_from_keyring

    return passphrase_from_keyring(args.keyring_account)


def prepare_preflight(args: argparse.Namespace) -> dict[str, Any]:
    root = args.directory.expanduser().resolve(strict=False)
    if root.exists() or root.is_symlink():
        raise ValueError("machine credential directory must be new")
    if root.is_relative_to(_workspace()):
        raise ValueError("machine credentials must be generated outside the repository")
    _passphrase(args)
    return {
        "schema": "iii.access-enrollment-prepare-plan/v1",
        "directory": str(root),
        "label": args.label,
        "passphrase_provider": (
            "owner-only-file" if args.signer_passphrase_file else "os-keyring"
        ),
    }


def prepare(args: argparse.Namespace) -> CommandResult:
    staging: Path | None = None
    try:
        retained = getattr(args, "_iii_retained_plan", {}).get("preflight", {})
        if retained != prepare_preflight(args):
            raise ValueError(
                "machine credential inputs changed after retained planning"
            )
        root = Path(retained["directory"])
        root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        parent = root.parent.lstat()
        if (
            root.parent.is_symlink()
            or not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != os.getuid()
            or stat.S_IMODE(parent.st_mode) & 0o077
        ):
            raise ValueError("machine credential parent directory must be owner-only")
        staging = Path(
            tempfile.mkdtemp(prefix=f".{root.name}.partial-", dir=root.parent)
        )
        staging.chmod(0o700)
        if stat.S_IMODE(staging.stat().st_mode) & 0o077:
            raise ValueError("machine credential staging directory is not owner-only")
        ssh_key = staging / "ssh_ed25519"
        result = subprocess.run(
            [
                "ssh-keygen",
                "-q",
                "-t",
                "ed25519",
                "-N",
                "",
                "-C",
                f"iii:{args.label}",
                "-f",
                str(ssh_key),
            ],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=30,
        )
        if result.returncode != 0:
            raise ValueError("ssh-keygen failed to create the per-machine identity")
        from iii_deployment.field_signing_agent import generate_field_signer
        from iii_deployment.identity import prepare_machine_enrollment

        descriptor_path = staging / "field-signing-public.json"
        generate_field_signer(
            private_key_path=staging / "field-signing-key.pem",
            public_descriptor_path=descriptor_path,
            passphrase=_passphrase(args),
            registry=_registry(),
            forbidden_roots=(_workspace(),),
        )
        enrollment, token_path = prepare_machine_enrollment(
            directory=staging,
            label=args.label,
            ssh_public_key_path=ssh_key.with_suffix(".pub"),
            field_signer_descriptor_path=descriptor_path,
            registry=_registry(),
            forbidden_roots=(_workspace(),),
        )
        os.replace(staging, root)
        staging = None
        token_path = root / token_path.name
    except Exception as exc:
        if staging is not None and staging.exists() and not staging.is_symlink():
            shutil.rmtree(staging)
        return _rejected("iii access enroll prepare", exc)
    enrollment_path = root / "enrollment.json"
    return CommandResult(
        command="iii access enroll prepare",
        outcome=Outcome.SUCCESS,
        summary=f"Prepared fresh independent credentials for {args.label}.",
        code="III_ACCESS_ENROLLMENT_PREPARED",
        evidence=(str(enrollment_path), str(token_path)),
        payload_schema="iii.access-enrollment-prepared/v1",
        payload={
            "schema": "iii.access-enrollment-prepared/v1",
            "machine_id": enrollment["machine_id"],
            "label": enrollment["label"],
            "enrollment_path": str(enrollment_path),
            "private_material_exported": False,
            "signer_ttl_default_hours": 8,
            "signer_ttl_maximum_hours": 24,
        },
        next_actions=(
            NextAction(
                (
                    "iii",
                    "access",
                    "enroll",
                    "add",
                    "--enrollment",
                    str(enrollment_path),
                ),
                "Authorize these public verifiers from an already enrolled computer.",
                mutating=True,
                confirmation_required=True,
            ),
        ),
    )


def _target(args: argparse.Namespace) -> dict[str, str]:
    from iii_deployment.runtime_target import (
        load_runtime_targets,
        resolve_runtime_target,
    )

    root = _workspace()
    registry = _registry()
    targets = load_runtime_targets(root / "deployment/runtime-targets.json", registry)
    selected = resolve_runtime_target(
        targets,
        selector=args.target,
        default_selector="real",
    )
    if (
        selected["endpoint"] != "iii.local"
        or selected["execution_host"] != "aircraft"
        or selected["logical_id"] != "drone"
        or selected["runtime_profile"] not in {"real", "opti_track"}
    ):
        raise ValueError(
            "access management requires the governed shared aircraft target"
        )
    return {
        "logical_id": str(selected["logical_id"]),
        "profile": str(selected["runtime_profile"]),
    }


def _await_operation(
    manager, operation_id: str, *, timeout_seconds: float = 10.0
) -> dict:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            status = _request(
                manager,
                action="status",
                operation_id=operation_id,
                payload={},
            )
        except Exception as exc:
            last_error = exc
            time.sleep(0.05)
            continue
        operation = status.get("operation")
        if isinstance(operation, dict) and operation.get("state") == "completed":
            return operation
        if isinstance(operation, dict) and operation.get("state") in {
            "failed",
            "cancelled",
        }:
            raise ValueError(
                f"receiver access operation ended {operation['state']}: "
                f"{operation.get('error') or operation.get('checkpoint')}"
            )
        time.sleep(0.05)
    raise ValueError(
        "receiver access operation did not reach an authenticated terminal state"
        + (f": {last_error}" if last_error is not None else "")
    )


def enroll_preflight(args: argparse.Namespace) -> dict[str, Any]:
    manager = _manager(args)
    identifier = getattr(args, "_iii_operation_id", None)
    if not identifier:
        raise ValueError("access enrollment requires a retained operation ID")
    enrollment = _load_enrollment(args.enrollment)
    return _request(
        manager,
        action="plan-access",
        operation_id=identifier,
        payload={
            "action": "access-add",
            "parameters": {"phase": args.phase, "enrollment": enrollment},
            "target": _target(args),
        },
    )


def enroll(args: argparse.Namespace) -> CommandResult:
    command = f"iii access enroll {args.phase}"
    try:
        retained = getattr(args, "_iii_retained_plan", {}).get("preflight")
        if not isinstance(retained, Mapping):
            raise ValueError("an exact receiver access plan is required")
        manager = _manager(args)
        _request(
            manager,
            action="access-add",
            operation_id=retained["plan"]["operation_id"],
            payload={"plan": retained["plan"]},
            nonce=retained["nonce"],
        )
        result = _await_operation(manager, retained["plan"]["operation_id"])
    except Exception as exc:
        return _rejected(command, exc)
    access = result.get("result", result)
    machine_id = retained["plan"]["parameters"]["enrollment"]["machine_id"]
    return CommandResult(
        command=command,
        outcome=Outcome.SUCCESS,
        summary=(
            "Machine public verifiers are pending independent proof."
            if args.phase == "add"
            else "Machine credentials were independently proved and activated."
        ),
        code=("III_ACCESS_PENDING" if args.phase == "add" else "III_ACCESS_ACTIVE"),
        payload_schema="iii.access-operation-result/v1",
        payload={
            "schema": "iii.access-operation-result/v1",
            "machine_id": machine_id,
            "receiver": access,
        },
        next_actions=(
            NextAction(
                ("iii", "access", "list"),
                "Verify SSH, Runtime API, and signing authorities independently.",
            ),
        ),
    )


def list_access(args: argparse.Namespace) -> CommandResult:
    try:
        manager = _manager(args)
        expected_target = _target(args)
        result = _request(
            manager,
            action="access-list",
            operation_id="access-list-0001",
            payload={},
        )
        if result.get("target") != expected_target:
            raise ValueError("receiver access inventory belongs to another target")
    except Exception as exc:
        return _rejected("iii access list", exc)
    return CommandResult(
        command="iii access list",
        outcome=Outcome.SUCCESS,
        summary=f"Authenticated {len(result['clients'])} machine access record(s).",
        code="III_ACCESS_LISTED",
        payload_schema="iii.access-list/v1",
        payload={"schema": "iii.access-list/v1", **result},
        terminal_reason="The receiver state was inspected without mutation.",
    )


def revoke_preflight(args: argparse.Namespace) -> dict[str, Any]:
    manager = _manager(args)
    identifier = getattr(args, "_iii_operation_id", None)
    if not identifier:
        raise ValueError("access revocation requires a retained operation ID")
    parameters = (
        {"authority": "machine", "machine_id": args.machine_id}
        if args.authority == "machine"
        else {
            "authority": "field-signing",
            "field_signer_id": args.field_signer_id,
        }
    )
    return _request(
        manager,
        action="plan-access",
        operation_id=identifier,
        payload={
            "action": "access-revoke",
            "parameters": parameters,
            "target": _target(args),
        },
    )


def revoke(args: argparse.Namespace) -> CommandResult:
    signing_only = args.authority == "field-signing"
    command = "iii access signer revoke" if signing_only else "iii access revoke"
    try:
        retained = getattr(args, "_iii_retained_plan", {}).get("preflight")
        if not isinstance(retained, Mapping):
            raise ValueError("an exact receiver access plan is required")
        manager = _manager(args)
        _request(
            manager,
            action="access-revoke",
            operation_id=retained["plan"]["operation_id"],
            payload={"plan": retained["plan"]},
            nonce=retained["nonce"],
        )
        result = _await_operation(manager, retained["plan"]["operation_id"])
    except Exception as exc:
        return _rejected(command, exc)
    return CommandResult(
        command=command,
        outcome=Outcome.SUCCESS,
        summary=(
            "Revoked the selected field signer while preserving SSH and Runtime access."
            if signing_only
            else "Revoked the selected machine across SSH, Runtime API, and field signing."
        ),
        code=("III_FIELD_SIGNER_REVOKED" if signing_only else "III_ACCESS_REVOKED"),
        payload_schema="iii.access-operation-result/v1",
        payload={
            "schema": "iii.access-operation-result/v1",
            ("field_signer_id" if signing_only else "machine_id"): (
                args.field_signer_id if signing_only else args.machine_id
            ),
            "receiver": result,
        },
        next_actions=(
            NextAction(
                ("iii", "access", "list"),
                "Verify the remaining independent authorities.",
            ),
        ),
    )


def initialize(parser: argparse.ArgumentParser) -> None:
    commands = parser.add_subparsers(dest="access_command")
    enroll_parser = commands.add_parser(
        "enroll", help="prepare, add, or prove a machine"
    )
    enroll_commands = enroll_parser.add_subparsers(dest="access_enroll_command")

    prepare_parser = enroll_commands.add_parser(
        "prepare", help="generate fresh local machine credentials"
    )
    prepare_parser.add_argument("--directory", type=Path, required=True)
    prepare_parser.add_argument("--label", required=True)
    passphrase = prepare_parser.add_mutually_exclusive_group(required=True)
    passphrase.add_argument("--signer-passphrase-file", type=Path)
    passphrase.add_argument("--keyring-account")
    prepare_parser.set_defaults(
        func=prepare, _iii_mutating=True, _iii_plan_provider=prepare_preflight
    )

    for phase in ("add", "prove"):
        phase_parser = enroll_commands.add_parser(
            phase, help=f"{phase} public machine enrollment"
        )
        phase_parser.add_argument("--enrollment", type=Path, required=True)
        phase_parser.add_argument(
            "--target", choices=("real", "opti_track"), default="real"
        )
        phase_parser.set_defaults(
            func=enroll,
            phase=phase,
            _iii_mutating=True,
            _iii_plan_provider=enroll_preflight,
        )

    list_parser = commands.add_parser(
        "list", help="list independent machine authorities"
    )
    list_parser.add_argument("--target", choices=("real", "opti_track"), default="real")
    list_parser.set_defaults(func=list_access, _iii_mutating=False)

    revoke_parser = commands.add_parser(
        "revoke", help="revoke one proved machine identity"
    )
    revoke_parser.add_argument("--machine-id", required=True)
    revoke_parser.add_argument(
        "--target", choices=("real", "opti_track"), default="real"
    )
    revoke_parser.set_defaults(
        func=revoke,
        authority="machine",
        field_signer_id=None,
        _iii_mutating=True,
        _iii_plan_provider=revoke_preflight,
    )

    signer_parser = commands.add_parser(
        "signer", help="manage field signing independently from runtime access"
    )
    signer_commands = signer_parser.add_subparsers(dest="access_signer_command")
    signer_revoke = signer_commands.add_parser(
        "revoke", help="revoke a signer without revoking SSH or Runtime access"
    )
    signer_revoke.add_argument("--signer-id", dest="field_signer_id", required=True)
    signer_revoke.add_argument(
        "--target", choices=("real", "opti_track"), default="real"
    )
    signer_revoke.set_defaults(
        func=revoke,
        authority="field-signing",
        machine_id=None,
        _iii_mutating=True,
        _iii_plan_provider=revoke_preflight,
    )


__all__ = ["initialize"]
