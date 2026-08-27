"""Portable aircraft backup, offboard registry, restore, and removed-media salvage."""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from typing import Any, Mapping
from uuid import uuid4

from . import registry
from .operation import OperationStore, default_state_root
from .result import CommandResult, Finding, NextAction, Outcome


RECEIPT_FIELDS = frozenset(
    {
        "schema",
        "backup_id",
        "receipt_id",
        "sealed_at",
        "verified_at",
        "verified",
        "external_verified",
        "fresh",
        "target",
        "release_id",
        "state_marker",
        "target_state_hash",
        "archive_sha256",
        "archive_bytes",
        "policy_id",
        "source",
        "operation_id",
        "protected",
        "references",
    }
)


def _environment(args: argparse.Namespace) -> Mapping[str, str]:
    return getattr(args, "_iii_environment", os.environ)


def _workspace() -> Path:
    current = Path.cwd().resolve()
    for candidate in (current, *current.parents):
        if (candidate / "deployment/portable-state-policy.json").is_file():
            return candidate
    raise ValueError("the III portable-state policy is unavailable")


def _root(args: argparse.Namespace) -> Path:
    selected = getattr(args, "registry_root", None)
    return (
        Path(selected).expanduser().absolute()
        if selected is not None
        else registry.registry_root(_environment(args))
    )


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
    from iii_deployment.contracts import ContractRegistry
    from iii_deployment.runtime_target import (
        load_runtime_targets,
        resolve_runtime_target,
    )

    root = _workspace()
    selected = resolve_runtime_target(
        load_runtime_targets(
            root / "deployment/runtime-targets.json",
            ContractRegistry(root / "deployment/schemas/v1"),
        ),
        selector=args.target,
        default_selector="real",
    )
    if (
        selected["endpoint"] != "iii.local"
        or selected["execution_host"] != "aircraft"
        or selected["logical_id"] != "drone"
        or selected["runtime_profile"] not in {"real", "opti_track"}
    ):
        raise ValueError("portable host backup requires the shared aircraft target")
    return {
        "logical_id": str(selected["logical_id"]),
        "profile": str(selected["runtime_profile"]),
    }


def _reject(command: str, exc: Exception) -> CommandResult:
    code = getattr(exc, "code", "III_HOST_BACKUP_REJECTED")
    return CommandResult(
        command=command,
        outcome=Outcome.REJECTED,
        summary="The portable host-state operation was refused before unsafe mutation.",
        code=code,
        findings=(Finding(code, str(exc)),),
        next_actions=(
            NextAction(
                ("iii", "host", "backup", "verify"),
                "Inspect local backup content, receipts, and external archive evidence.",
            ),
        ),
    )


def _retained_preflight(args: argparse.Namespace) -> dict[str, Any] | None:
    identifier = getattr(args, "_iii_operation_id", None)
    if not identifier:
        return None
    plan = OperationStore(default_state_root(_environment(args))).load_plan(identifier)
    value = plan and plan.get("preflight")
    return dict(value) if isinstance(value, Mapping) else None


def _await(manager, operation_id: str, *, timeout: float = 7205.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = _request(
            manager, action="status", operation_id=operation_id, payload={}
        )
        operation = status.get("operation")
        if isinstance(operation, dict) and operation.get("state") == "completed":
            return operation
        if isinstance(operation, dict) and operation.get("state") in {
            "failed",
            "cancelled",
        }:
            raise ValueError(
                f"receiver portable-state operation ended {operation['state']}: "
                f"{operation.get('failure') or operation.get('checkpoint')}"
            )
        time.sleep(0.25)
    raise ValueError("receiver portable-state operation did not complete")


def _local_detail(root: Path, backup_id: str) -> dict[str, Any]:
    from iii_deployment.portable_state import inspect_archive, validate_external_receipt

    if not registry.HASH.fullmatch(backup_id):
        raise ValueError("portable backup identity is invalid")
    unit = root / "backups" / backup_id
    receipt_path = unit / "receipt.json"
    archive_path = unit / "portable-state.tar"
    if unit.is_symlink() or receipt_path.is_symlink() or archive_path.is_symlink():
        raise ValueError("portable backup registry entry is linked")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if not isinstance(receipt, dict):
        raise ValueError("portable backup receipt is invalid")
    validate_external_receipt(receipt)
    verification = inspect_archive(archive_path)
    if (
        receipt["backup_id"] != verification["backup_id"]
        or receipt["archive_sha256"] != verification["archive_sha256"]
        or receipt["target_state_hash"] != verification["target_state_hash"]
    ):
        raise ValueError("portable backup receipt/archive binding mismatch")
    return {
        "receipt": receipt,
        "verification": verification,
        "archive_path": archive_path,
        "receipt_path": receipt_path,
    }


def _store_external(
    root: Path,
    source_archive: Path,
    source_receipt: Mapping[str, Any],
    *,
    operation_id: str,
) -> dict[str, Any]:
    from iii_deployment.contracts import canonical_json, content_identity
    from iii_deployment.portable_state import inspect_archive

    verification = inspect_archive(source_archive)
    backup_id = verification["backup_id"]
    if source_receipt.get("backup_id") != backup_id:
        raise ValueError("receiver receipt differs from downloaded portable archive")
    with registry.registry_lock(root):
        unit = root / "backups" / backup_id
        if unit.is_dir() and not unit.is_symlink():
            existing = _local_detail(root, backup_id)
            if (
                existing["verification"]["archive_sha256"]
                != verification["archive_sha256"]
            ):
                raise ValueError("local content-addressed backup identity conflicts")
            return {**existing, "duplicate_content": True}
        unit.mkdir(parents=True, mode=0o700)
        destination = unit / "portable-state.tar"
        temporary = unit / ".portable-state.tar.partial"
        try:
            source_descriptor = os.open(source_archive, os.O_RDONLY | os.O_NOFOLLOW)
            target_descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
            with os.fdopen(source_descriptor, "rb") as source, os.fdopen(
                target_descriptor, "wb"
            ) as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, destination)
            receipt = {
                key: source_receipt[key]
                for key in RECEIPT_FIELDS
                if key in source_receipt
            }
            receipt.update(
                {
                    "schema": "iii.host-backup-receipt/v1",
                    "backup_id": backup_id,
                    "receipt_id": "0" * 64,
                    "verified_at": datetime.now(timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z"),
                    "verified": True,
                    "external_verified": True,
                    "fresh": True,
                    "archive_sha256": verification["archive_sha256"],
                    "archive_bytes": verification["archive_bytes"],
                    "target_state_hash": verification["target_state_hash"],
                    "operation_id": operation_id,
                    "protected": True,
                    "references": [backup_id],
                }
            )
            receipt["receipt_id"] = content_identity(
                {key: value for key, value in receipt.items() if key != "receipt_id"}
            )
            registry.atomic_json(root, f"backups/{backup_id}/receipt.json", receipt)
            registry.write_index(root, registry.build_inventory(root))
        except Exception:
            temporary.unlink(missing_ok=True)
            shutil.rmtree(unit, ignore_errors=True)
            raise
    return _local_detail(root, backup_id)


def create_preflight(args: argparse.Namespace) -> dict[str, Any]:
    retained = _retained_preflight(args)
    if retained is not None:
        return retained
    operation_id = getattr(args, "_iii_operation_id", None)
    if not operation_id:
        raise ValueError("portable backup creation requires a retained operation ID")
    return _request(
        _manager(args),
        action="plan-backup-seal",
        operation_id=operation_id,
        payload={"target": _target(args)},
    )


def _download(manager, result: Mapping[str, Any], destination: Path) -> None:
    backup_id = str(result["backup_id"])
    expected_size = int(result["archive_bytes"])
    expected_hash = str(result["archive_sha256"])
    temporary = destination.parent / f".{destination.name}.partial-{os.getpid()}"
    digest = hashlib.sha256()
    offset = 0
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as output:
            while offset < expected_size:
                response = _request(
                    manager,
                    action="backup-chunk",
                    operation_id=f"backup-pull-{uuid4().hex[:20]}",
                    payload={
                        "backup_id": backup_id,
                        "offset": offset,
                        "length": min(4 * 1024 * 1024, expected_size - offset),
                    },
                )["chunk"]
                data = base64.b64decode(response["data_base64"], validate=True)
                if (
                    response["offset"] != offset
                    or response["bytes"] != len(data)
                    or hashlib.sha256(data).hexdigest() != response["sha256"]
                    or response["archive_sha256"] != expected_hash
                    or not data
                ):
                    raise ValueError("portable backup transfer chunk identity mismatch")
                output.write(data)
                digest.update(data)
                offset += len(data)
            output.flush()
            os.fsync(output.fileno())
        if offset != expected_size or digest.hexdigest() != expected_hash:
            raise ValueError("downloaded portable backup hash/size mismatch")
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def create(args: argparse.Namespace) -> CommandResult:
    work = Path(tempfile.mkdtemp(prefix="iii-portable-backup-"))
    try:
        retained = getattr(args, "_iii_retained_plan", {}).get("preflight")
        if not isinstance(retained, Mapping):
            raise ValueError("an exact retained receiver backup plan is required")
        receiver_plan = retained["plan"]
        manager = _manager(args)
        _request(
            manager,
            action="backup-seal",
            operation_id=receiver_plan["operation_id"],
            payload={"plan": receiver_plan},
            nonce=retained["nonce"],
        )
        operation = _await(manager, receiver_plan["operation_id"])
        result = operation["result"]
        archive = work / "portable-state.tar"
        _download(manager, result, archive)
        detail = _store_external(
            _root(args), archive, result, operation_id=receiver_plan["operation_id"]
        )
    except Exception as exc:
        return _reject("iii host backup create", exc)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    receipt = detail["receipt"]
    return CommandResult(
        command="iii host backup create",
        outcome=Outcome.SUCCESS,
        summary=f"Sealed, resumed standby, downloaded, and externally verified backup {receipt['backup_id']}.",
        code="III_HOST_BACKUP_EXTERNAL_VERIFIED",
        target=args.target,
        profile=receipt["target"]["profile"],
        evidence=(
            str(detail["archive_path"]),
            str(detail["receipt_path"]),
            receipt["archive_sha256"],
        ),
        payload_schema=receipt["schema"],
        payload=receipt,
        terminal_reason="The receiver resumed standby before the content-addressed long transfer and the external copy was independently verified.",
    )


def list_backups(args: argparse.Namespace) -> CommandResult:
    try:
        root = _root(args)
        inventory = registry.build_inventory(root, domains=["backups"])
        rows = []
        for record in inventory["records"]:
            backup_id = record["locator"].split("/", 1)[1]
            if registry.HASH.fullmatch(backup_id):
                rows.append(_local_detail(root, backup_id)["receipt"])
    except Exception as exc:
        return _reject("iii host backup list", exc)
    return CommandResult(
        command="iii host backup list",
        outcome=Outcome.SUCCESS,
        summary=f"Inventoried {len(rows)} verified external portable backup(s).",
        code="III_HOST_BACKUPS_LISTED",
        payload_schema="iii.host-backup-list/v1",
        payload={"schema": "iii.host-backup-list/v1", "backups": rows},
        terminal_reason="Only local content-addressed backup records were read.",
    )


def show(args: argparse.Namespace) -> CommandResult:
    try:
        detail = _local_detail(_root(args), args.backup_id)
    except Exception as exc:
        return _reject("iii host backup show", exc)
    return CommandResult(
        command="iii host backup show",
        outcome=Outcome.SUCCESS,
        summary=f"Verified portable backup {args.backup_id}.",
        code="III_HOST_BACKUP_SHOWN",
        payload_schema="iii.host-backup-detail/v1",
        payload={
            "schema": "iii.host-backup-detail/v1",
            "receipt": detail["receipt"],
            "verification": detail["verification"],
        },
        terminal_reason="The receipt, manifest, domain inventory, and archive hashes were verified without mutation.",
    )


def verify(args: argparse.Namespace) -> CommandResult:
    try:
        root = _root(args)
        inventory = registry.build_inventory(root, domains=["backups"])
        identities = sorted(
            {
                record["locator"].split("/", 1)[1]
                for record in inventory["records"]
                if registry.HASH.fullmatch(record["locator"].split("/", 1)[1])
            }
        )
        if args.backup_id:
            identities = [args.backup_id]
        rows = [
            {
                "backup_id": identity,
                "archive_sha256": _local_detail(root, identity)["receipt"][
                    "archive_sha256"
                ],
                "verified": True,
            }
            for identity in identities
        ]
    except Exception as exc:
        return _reject("iii host backup verify", exc)
    return CommandResult(
        command="iii host backup verify",
        outcome=Outcome.SUCCESS,
        summary=f"Verified {len(rows)} portable backup(s) byte-for-byte.",
        code="III_HOST_BACKUPS_VERIFIED",
        payload_schema="iii.host-backup-verification-list/v1",
        payload={"schema": "iii.host-backup-verification-list/v1", "backups": rows},
        terminal_reason="Every selected receipt, manifest, member hash, and structural secret boundary passed.",
    )


def export_preflight(args: argparse.Namespace) -> dict[str, Any]:
    retained = _retained_preflight(args)
    if retained is not None:
        return retained
    detail = _local_detail(_root(args), args.backup_id)
    destination = args.destination.expanduser().absolute()
    return {
        "schema": "iii.host-backup-export-plan/v1",
        "backup_id": args.backup_id,
        "archive_sha256": detail["receipt"]["archive_sha256"],
        "source": str(detail["archive_path"]),
        "destination": str(destination),
        "destination_exists": destination.exists() or destination.is_symlink(),
    }


def export(args: argparse.Namespace) -> CommandResult:
    try:
        plan = getattr(args, "_iii_retained_plan", {}).get("preflight")
        if not isinstance(plan, Mapping):
            raise ValueError("an exact retained backup export plan is required")
        source = Path(plan["source"])
        destination = Path(plan["destination"])
        if hashlib.sha256(source.read_bytes()).hexdigest() != plan["archive_sha256"]:
            raise ValueError("portable backup changed after export planning")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() or destination.is_symlink():
            if (
                destination.is_symlink()
                or hashlib.sha256(destination.read_bytes()).hexdigest()
                != plan["archive_sha256"]
            ):
                raise ValueError("backup export destination conflicts")
        else:
            shutil.copyfile(source, destination)
            with destination.open("rb") as stream:
                os.fsync(stream.fileno())
            directory = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except Exception as exc:
        return _reject("iii host backup export", exc)
    return CommandResult(
        command="iii host backup export",
        outcome=Outcome.SUCCESS,
        summary=f"Exported verified portable backup {args.backup_id}.",
        code="III_HOST_BACKUP_EXPORTED",
        evidence=(str(destination), plan["archive_sha256"]),
        payload_schema=plan["schema"],
        payload=dict(plan),
        terminal_reason="The explicit destination contains the exact verified content-addressed archive.",
    )


def import_preflight(args: argparse.Namespace) -> dict[str, Any]:
    retained = _retained_preflight(args)
    if retained is not None:
        return retained
    from iii_deployment.portable_state import inspect_archive

    verification = inspect_archive(args.archive.expanduser().absolute())
    return {
        "schema": "iii.host-backup-import-plan/v1",
        "backup_id": verification["backup_id"],
        "archive_path": str(args.archive.expanduser().absolute()),
        "archive_sha256": verification["archive_sha256"],
        "target_state_hash": verification["target_state_hash"],
    }


def import_backup(args: argparse.Namespace) -> CommandResult:
    try:
        plan = getattr(args, "_iii_retained_plan", {}).get("preflight")
        if not isinstance(plan, Mapping):
            raise ValueError("an exact retained backup import plan is required")
        from iii_deployment.portable_state import inspect_archive

        archive = Path(plan["archive_path"])
        verification = inspect_archive(archive)
        if verification["archive_sha256"] != plan["archive_sha256"]:
            raise ValueError("portable backup changed after import planning")
        manifest = verification["manifest"]
        source_receipt = {
            "backup_id": manifest["backup_id"],
            "sealed_at": manifest["sealed_at"],
            "target": manifest["target"],
            "release_id": manifest["release_id"],
            "state_marker": manifest["state_marker"],
            "target_state_hash": manifest["target_state_hash"],
            "policy_id": manifest["policy_id"],
            "source": manifest["source"],
        }
        identifier = getattr(
            args, "_iii_operation_id", f"backup-import-{uuid4().hex[:16]}"
        )
        detail = _store_external(
            _root(args), archive, source_receipt, operation_id=identifier
        )
    except Exception as exc:
        return _reject("iii host backup import", exc)
    return CommandResult(
        command="iii host backup import",
        outcome=Outcome.SUCCESS,
        summary=f"Imported and externally verified portable backup {plan['backup_id']}.",
        code="III_HOST_BACKUP_IMPORTED",
        evidence=(str(detail["archive_path"]), str(detail["receipt_path"])),
        payload_schema=detail["receipt"]["schema"],
        payload=detail["receipt"],
        terminal_reason="Only independently verified, structurally non-secret portable content was imported.",
    )


def restore_preflight(args: argparse.Namespace) -> dict[str, Any]:
    retained = _retained_preflight(args)
    if retained is not None:
        return retained
    detail = _local_detail(_root(args), args.backup_id)
    return {
        "schema": "iii.host-backup-restore-handoff-plan/v1",
        "backup_id": args.backup_id,
        "archive_path": str(detail["archive_path"]),
        "archive_sha256": detail["receipt"]["archive_sha256"],
        "target": _target(args),
        "mutations": [
            "upload-verified-archive",
            "receiver-staged-reconciliation",
            "atomic-persistent-root-activation",
            "post-restore-health-validation",
        ],
    }


def restore(args: argparse.Namespace) -> CommandResult:
    try:
        retained = getattr(args, "_iii_retained_plan", {}).get("preflight")
        if not isinstance(retained, Mapping):
            raise ValueError("an exact retained restore handoff plan is required")
        archive = Path(retained["archive_path"])
        if (
            hashlib.sha256(archive.read_bytes()).hexdigest()
            != retained["archive_sha256"]
        ):
            raise ValueError("portable backup changed after restore planning")
        identifier = getattr(args, "_iii_operation_id", None)
        if not identifier:
            raise ValueError("portable restore requires a retained operation ID")
        manager = _manager(args)
        transfer = manager.upload_backup(
            archive,
            backup_id=args.backup_id,
            profile=retained["target"]["profile"],
            operation_id=f"backup-upload-{uuid4().hex[:16]}",
        )
        planned = _request(
            manager,
            action="plan-backup-restore",
            operation_id=identifier,
            payload={"backup_id": args.backup_id, "target": retained["target"]},
        )
        OperationStore(default_state_root(_environment(args))).write_record(
            identifier, "receiver-restore-plan.json", planned
        )
        _request(
            manager,
            action="backup-restore",
            operation_id=identifier,
            payload={"plan": planned["plan"]},
            nonce=planned["nonce"],
        )
        operation = _await(manager, identifier)
        result = operation["result"]
    except Exception as exc:
        return _reject("iii host backup restore", exc)
    return CommandResult(
        command="iii host backup restore",
        outcome=Outcome.SUCCESS,
        summary=f"Restored portable backup {args.backup_id} through staged reconciliation and health validation.",
        code="III_HOST_BACKUP_RESTORED",
        target=args.target,
        profile=retained["target"]["profile"],
        evidence=(args.backup_id, transfer["archive_sha256"], result["result_id"]),
        payload_schema=result["schema"],
        payload={**result, "transfer": transfer},
        terminal_reason="The clean host retained a compatible release, atomically selected reconciled portable state, and passed health validation without restoring host identity.",
    )


def prune_preflight(args: argparse.Namespace) -> dict[str, Any]:
    retained = _retained_preflight(args)
    if retained is not None:
        return retained
    return _prune_state(_root(args), args.backup_id)


def _prune_state(root: Path, backup_id: str) -> dict[str, Any]:
    _local_detail(root, backup_id)
    references = []
    target = (root / "backups" / backup_id).resolve()
    if root.exists():
        for path in sorted(root.rglob("*.json")):
            relative = path.relative_to(root)
            if (
                path.is_symlink()
                or path.resolve().is_relative_to(target)
                or relative.parts[0]
                in {"indexes", "archive-receipts", "import-receipts"}
            ):
                continue
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if backup_id in json.dumps(value, sort_keys=True):
                references.append(relative.as_posix())
    return {
        "schema": "iii.host-backup-prune-plan/v1",
        "backup_id": backup_id,
        "references": references,
        "removable": not references,
    }


def prune(args: argparse.Namespace) -> CommandResult:
    try:
        plan = getattr(args, "_iii_retained_plan", {}).get("preflight")
        if not isinstance(plan, Mapping) or plan.get("removable") is not True:
            raise ValueError(
                "referenced restore/audit backup evidence cannot be pruned: "
                + ", ".join((plan or {}).get("references", []))
            )
        current = _prune_state(_root(args), args.backup_id)
        if current != plan:
            raise ValueError(
                "portable backup references changed after pruning was planned"
            )
        root = _root(args)
        with registry.registry_lock(root):
            unit = root / "backups" / args.backup_id
            shutil.rmtree(unit)
            registry.write_index(root, registry.build_inventory(root))
    except Exception as exc:
        return _reject("iii host backup prune", exc)
    return CommandResult(
        command="iii host backup prune",
        outcome=Outcome.SUCCESS,
        summary=f"Pruned unreferenced portable backup {args.backup_id}.",
        code="III_HOST_BACKUP_PRUNED",
        payload_schema=plan["schema"],
        payload=dict(plan),
        terminal_reason="The explicit content identity had no retained restore or audit references.",
    )


def status(args: argparse.Namespace) -> CommandResult:
    try:
        manager = _manager(args)
        result = _request(
            manager,
            action="backup-status",
            operation_id=f"backup-status-{uuid4().hex[:20]}",
            payload={},
        )["backup"]
        coverage = registry.archive_coverage(_root(args), warning_days=30)
    except Exception as exc:
        return _reject("iii host backup status", exc)
    outcome = (
        Outcome.SUCCESS
        if result["backup_fresh"] and coverage["recent"]
        else Outcome.WARNING
    )
    return CommandResult(
        command="iii host backup status",
        outcome=outcome,
        summary="Portable backup state and 30-day external archive coverage were inspected.",
        code=(
            "III_HOST_BACKUP_READY"
            if outcome == Outcome.SUCCESS
            else "III_HOST_BACKUP_STALE_OR_EXTERNAL_ARCHIVE_OVERDUE"
        ),
        payload_schema="iii.host-backup-readiness/v1",
        payload={
            "schema": "iii.host-backup-readiness/v1",
            "aircraft": result,
            "external_archive_coverage": coverage,
        },
        terminal_reason="Freshness is state-marker based; external archive age is independently derived from verified local receipts.",
    )


def salvage_preflight(args: argparse.Namespace) -> dict[str, Any]:
    retained = _retained_preflight(args)
    if retained is not None:
        return retained
    from iii_deployment.portable_state import inspect_salvage_device

    evidence = inspect_salvage_device(args.device)
    return {
        "schema": "iii.host-salvage-plan/v1",
        "device": evidence,
        "source_mutation": False,
        "mount_namespace": "private",
        "mount_options": ["ro", "noload", "nodev", "nosuid", "noexec"],
        "credentials_requested": False,
    }


def salvage(args: argparse.Namespace) -> CommandResult:
    work = Path(tempfile.mkdtemp(prefix="iii-host-salvage-"))
    try:
        retained = getattr(args, "_iii_retained_plan", {}).get("preflight")
        if not isinstance(retained, Mapping):
            raise ValueError("an exact retained salvage plan is required")
        identifier = getattr(args, "_iii_operation_id", None)
        command = [
            "unshare",
            "--mount",
            "--propagation",
            "private",
            "--",
            "iii-host-salvage-worker",
            "--device",
            args.device,
            "--output-root",
            str(work),
            "--policy",
            str(_workspace() / "deployment/portable-state-policy.json"),
            "--operation-id",
            identifier,
        ]
        result = subprocess.run(command, capture_output=True, check=False, text=True)
        if result.returncode != 0:
            raise ValueError(
                "private read-only salvage worker failed: "
                + (result.stderr or result.stdout).strip()[-1000:]
            )
        worker = json.loads(result.stdout)
        record = json.loads(Path(worker["record_path"]).read_text(encoding="utf-8"))
        detail = _store_external(
            _root(args),
            Path(worker["archive_path"]),
            {
                **record,
                "sealed_at": record["recorded_at"],
                "target": {"logical_id": "drone", "profile": "real"},
                "release_id": None,
                "state_marker": record["target_state_hash"],
                "policy_id": load_policy_id(),
                "source": "salvage",
            },
            operation_id=identifier,
        )
        destination_record = detail["archive_path"].parent / "salvage-record.json"
        shutil.copyfile(Path(worker["record_path"]), destination_record)
    except Exception as exc:
        return _reject("iii host salvage", exc)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return CommandResult(
        command="iii host salvage",
        outcome=Outcome.WARNING,
        summary=f"Salvaged verified portable state {record['backup_id']} from powered-off removed media.",
        code="III_HOST_SALVAGE_VERIFIED_RECOMMISSION_REQUIRED",
        evidence=(
            str(detail["archive_path"]),
            str(destination_record),
            record["salvage_id"],
        ),
        payload_schema=record["schema"],
        payload=record,
        terminal_reason=record["operator_notice"],
    )


def load_policy_id() -> str:
    from iii_deployment.portable_state import load_policy, policy_id

    return policy_id(
        load_policy(_workspace() / "deployment/portable-state-policy.json")
    )


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--registry-root", type=Path)


def initialize(commands: argparse._SubParsersAction) -> None:
    backup = commands.add_parser(
        "backup", help="seal, verify, transfer, restore, and prune portable host state"
    )
    actions = backup.add_subparsers(dest="host_backup_command")

    create_parser = actions.add_parser("create", help="seal and pull a new backup")
    _common(create_parser)
    create_parser.add_argument("--target", default="real")
    create_parser.set_defaults(
        func=create, _iii_mutating=True, _iii_plan_provider=create_preflight
    )

    list_parser = actions.add_parser("list", help="list verified local backups")
    _common(list_parser)
    list_parser.set_defaults(func=list_backups, _iii_mutating=False)

    show_parser = actions.add_parser("show", help="show one verified local backup")
    _common(show_parser)
    show_parser.add_argument("backup_id")
    show_parser.set_defaults(func=show, _iii_mutating=False)

    verify_parser = actions.add_parser("verify", help="verify local backup archives")
    _common(verify_parser)
    verify_parser.add_argument("backup_id", nargs="?")
    verify_parser.set_defaults(func=verify, _iii_mutating=False)

    export_parser = actions.add_parser("export", help="export one exact archive")
    _common(export_parser)
    export_parser.add_argument("backup_id")
    export_parser.add_argument("--destination", type=Path, required=True)
    export_parser.set_defaults(
        func=export, _iii_mutating=True, _iii_plan_provider=export_preflight
    )

    import_parser = actions.add_parser("import", help="verify and import an archive")
    _common(import_parser)
    import_parser.add_argument("--archive", type=Path, required=True)
    import_parser.set_defaults(
        func=import_backup, _iii_mutating=True, _iii_plan_provider=import_preflight
    )

    restore_parser = actions.add_parser(
        "restore", help="restore through receiver reconciliation"
    )
    _common(restore_parser)
    restore_parser.add_argument("backup_id")
    restore_parser.add_argument("--target", default="real")
    restore_parser.set_defaults(
        func=restore, _iii_mutating=True, _iii_plan_provider=restore_preflight
    )

    prune_parser = actions.add_parser("prune", help="prune one unreferenced backup")
    _common(prune_parser)
    prune_parser.add_argument("backup_id")
    prune_parser.set_defaults(
        func=prune, _iii_mutating=True, _iii_plan_provider=prune_preflight
    )

    status_parser = actions.add_parser("status", help="inspect backup freshness")
    _common(status_parser)
    status_parser.add_argument("--target", default="real")
    status_parser.set_defaults(func=status, _iii_mutating=False)

    salvage_parser = commands.add_parser(
        "salvage", help="read-only salvage from an explicit removed III disk"
    )
    _common(salvage_parser)
    salvage_parser.add_argument("--device", required=True)
    salvage_parser.set_defaults(
        func=salvage,
        _iii_mutating=True,
        _iii_plan_provider=salvage_preflight,
        _iii_interactive=True,
    )
