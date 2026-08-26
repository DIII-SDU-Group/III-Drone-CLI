"""Canonical CLI provider for the user-owned local record registry."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any, Mapping

from . import registry
from .operation import OperationStore, default_state_root
from .result import CommandResult, Finding, NextAction, Outcome


def _environment(args: argparse.Namespace) -> Mapping[str, str]:
    return getattr(args, "_iii_environment", os.environ)


def _root(args: argparse.Namespace) -> Path:
    selected = getattr(args, "registry_root", None)
    return (
        Path(selected).expanduser().absolute()
        if selected is not None
        else registry.registry_root(_environment(args))
    )


def _controlling_record(args: argparse.Namespace) -> list[str]:
    identifier = getattr(args, "_iii_operation_id", None)
    return [f"operations/{identifier}"] if identifier else []


def _retained_preflight(args: argparse.Namespace) -> dict[str, Any] | None:
    identifier = getattr(args, "_iii_operation_id", None)
    if not identifier:
        return None
    plan = OperationStore(default_state_root(_environment(args))).load_plan(identifier)
    value = plan and plan.get("preflight")
    return dict(value) if isinstance(value, Mapping) else None


def _reject(command: str, exc: Exception) -> CommandResult:
    code = getattr(exc, "code", "III_RECORD_REGISTRY_ERROR")
    return CommandResult(
        command=command,
        outcome=Outcome.REJECTED,
        summary="The local record operation was refused before unsafe mutation.",
        code=code,
        findings=(Finding(code, str(exc)),),
        next_actions=(
            NextAction(
                ("iii", "records", "verify"),
                "Inspect the registry integrity and archive coverage.",
            ),
        ),
    )


def inventory(args: argparse.Namespace) -> CommandResult:
    try:
        root = _root(args)
        value = registry.build_inventory(root, domains=args.domain)
        coverage = registry.archive_coverage(root, warning_days=args.warning_days)
    except Exception as exc:
        return _reject("iii records inventory", exc)
    return CommandResult(
        command="iii records inventory",
        outcome=Outcome.SUCCESS,
        summary=(
            f"Inventoried {len(value['records'])} local record(s) and "
            f"{len(value['blobs'])} shared blob(s)."
        ),
        code="III_RECORDS_INVENTORIED",
        payload_schema=value["schema"],
        payload={**value, "registry_root": str(root), "archive_coverage": coverage},
        terminal_reason="The derived inventory was read without mutation or pruning.",
    )


def verify(args: argparse.Namespace) -> CommandResult:
    try:
        root = _root(args)
        derived = registry.build_inventory(root, domains=args.domain)
        stored = registry.read_index(root) if root.exists() else None
        coverage = registry.archive_coverage(root, warning_days=args.warning_days)
        integrity_findings = [
            {
                "record_id": record["record_id"],
                "locator": record["locator"],
                "issues": record["integrity"]["issues"],
            }
            for record in derived["records"]
            if record["integrity"]["state"] != "verified"
        ]
        corrupt_blobs = [
            item for item in derived["blobs"] if item["integrity"] != "verified"
        ]
        index_state = (
            "not-compared-for-filtered-inventory"
            if args.domain
            else (
                "missing"
                if stored is None
                else "current" if stored["index_id"] == derived["index_id"] else "stale"
            )
        )
        content_verified = (
            not integrity_findings and not corrupt_blobs and not derived["omitted"]
        )
        index_current = index_state in {
            "current",
            "not-compared-for-filtered-inventory",
        }
        verified = content_verified and index_current
    except Exception as exc:
        return _reject("iii records verify", exc)
    finding_rows = (
        [
            Finding(
                "III_RECORD_INDEX_NOT_CURRENT",
                f"The derived record index is {index_state}.",
            )
        ]
        if index_state not in {"current", "not-compared-for-filtered-inventory"}
        else []
    )
    if integrity_findings:
        finding_rows.append(
            Finding(
                "III_RECORD_CONTENT_CORRUPT",
                f"{len(integrity_findings)} record(s) contain invalid metadata.",
            )
        )
    if corrupt_blobs:
        finding_rows.append(
            Finding(
                "III_RECORD_BLOB_CORRUPT",
                f"{len(corrupt_blobs)} shared blob(s) failed content verification.",
            )
        )
    if derived["omitted"]:
        finding_rows.append(
            Finding(
                "III_RECORD_STAGING_INCOMPLETE",
                f"{len(derived['omitted'])} incomplete staging path(s) remain.",
            )
        )
    findings = tuple(finding_rows)
    outcome = (
        Outcome.FAILED
        if not content_verified
        else Outcome.SUCCESS if index_current else Outcome.WARNING
    )
    code = (
        "III_RECORDS_INTEGRITY_FAILED"
        if not content_verified
        else (
            "III_RECORDS_VERIFIED"
            if index_current
            else "III_RECORD_INDEX_REFRESH_REQUIRED"
        )
    )
    return CommandResult(
        command="iii records verify",
        outcome=outcome,
        summary=(
            "The local record registry is internally verified."
            if verified
            else (
                "Record content is verified; the derived index needs refresh."
                if content_verified
                else "The local record registry has unresolved integrity findings."
            )
        ),
        code=code,
        findings=findings,
        payload_schema="iii.record-verification/v1",
        payload={
            "schema": "iii.record-verification/v1",
            "registry_root": str(root),
            "registry_index_id": derived["index_id"],
            "stored_index_state": index_state,
            "record_integrity_findings": integrity_findings,
            "corrupt_blobs": corrupt_blobs,
            "omitted": derived["omitted"],
            "archive_coverage": coverage,
            "content_verified": content_verified,
            "verified": verified,
        },
        next_actions=(
            NextAction(
                ("iii", "records", "archive", "--help"),
                "Create or refresh an explicit verified external archive.",
            ),
        ),
    )


def archive_preflight(args: argparse.Namespace) -> dict[str, Any]:
    retained = _retained_preflight(args)
    if retained is not None:
        return retained
    return registry.build_archive_plan(
        _root(args),
        destination=args.destination,
        domains=args.domain,
        base_archive=args.base,
        exclude_record_locators=_controlling_record(args),
    )


def archive(args: argparse.Namespace) -> CommandResult:
    try:
        retained = getattr(args, "_iii_retained_plan", None)
        if not isinstance(retained, dict) or not isinstance(
            retained.get("preflight"), dict
        ):
            raise registry.RegistryConflict(
                "an exact retained archive plan is required"
            )
        receipt = registry.apply_archive_plan(_root(args), retained["preflight"])
    except Exception as exc:
        return _reject("iii records archive", exc)
    return CommandResult(
        command="iii records archive",
        outcome=Outcome.SUCCESS,
        summary=f"Created and verified record archive {receipt['archive_id']}.",
        code="III_RECORD_ARCHIVE_VERIFIED",
        evidence=(
            receipt["archive_path"],
            receipt["archive_sha256"],
            receipt["receipt_id"],
        ),
        payload_schema=receipt["schema"],
        payload=receipt,
        terminal_reason="The explicit archive path and every included blob were verified after durable write.",
    )


def import_preflight(args: argparse.Namespace) -> dict[str, Any]:
    retained = _retained_preflight(args)
    return retained or registry.build_import_plan(
        _root(args), archive_path=args.archive
    )


def import_archive(args: argparse.Namespace) -> CommandResult:
    try:
        retained = getattr(args, "_iii_retained_plan", None)
        if not isinstance(retained, dict) or not isinstance(
            retained.get("preflight"), dict
        ):
            raise registry.RegistryConflict("an exact retained import plan is required")
        receipt = registry.apply_import_plan(_root(args), retained["preflight"])
    except Exception as exc:
        return _reject("iii records import", exc)
    return CommandResult(
        command="iii records import",
        outcome=Outcome.SUCCESS,
        summary=f"Verified and imported record archive {receipt['archive_id']}.",
        code="III_RECORD_ARCHIVE_IMPORTED",
        evidence=(
            receipt["source_path"],
            receipt["archive_sha256"],
            receipt["receipt_id"],
        ),
        payload_schema=receipt["schema"],
        payload=receipt,
        terminal_reason="Only verified non-secret content was materialized; conflicting local content was not overwritten.",
    )


def prune_preflight(args: argparse.Namespace) -> dict[str, Any]:
    retained = _retained_preflight(args)
    return retained or registry.build_prune_plan(
        _root(args),
        record_ids=args.record,
        exclude_record_locators=_controlling_record(args),
    )


def prune(args: argparse.Namespace) -> CommandResult:
    try:
        retained = getattr(args, "_iii_retained_plan", None)
        if not isinstance(retained, dict) or not isinstance(
            retained.get("preflight"), dict
        ):
            raise registry.RegistryConflict("an exact retained prune plan is required")
        result = registry.apply_prune_plan(_root(args), retained["preflight"])
    except Exception as exc:
        return _reject("iii records prune", exc)
    return CommandResult(
        command="iii records prune",
        outcome=Outcome.SUCCESS,
        summary=f"Pruned {len(result['removed_record_ids'])} explicit unprotected record(s).",
        code="III_RECORDS_PRUNED",
        payload_schema=result["schema"],
        payload=result,
        terminal_reason="No automatic pruning occurred; protected records and shared blobs remain intact.",
    )


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--registry-root",
        type=Path,
        help="override the user-owned registry root (Git worktrees require .iii/)",
    )


def initialize(parser: argparse.ArgumentParser) -> None:
    commands = parser.add_subparsers(dest="records_command")

    inventory_parser = commands.add_parser(
        "inventory", help="derive the local record inventory"
    )
    _common(inventory_parser)
    inventory_parser.add_argument(
        "--domain", action="append", choices=sorted(dict(registry.DOMAIN_ROOTS))
    )
    inventory_parser.add_argument("--warning-days", type=int, default=30)
    inventory_parser.set_defaults(func=inventory, _iii_mutating=False)

    verify_parser = commands.add_parser(
        "verify", help="verify records, blobs, index, and archive coverage"
    )
    _common(verify_parser)
    verify_parser.add_argument(
        "--domain", action="append", choices=sorted(dict(registry.DOMAIN_ROOTS))
    )
    verify_parser.add_argument("--warning-days", type=int, default=30)
    verify_parser.set_defaults(func=verify, _iii_mutating=False)

    archive_parser = commands.add_parser(
        "archive", help="plan or write a deterministic portable archive"
    )
    _common(archive_parser)
    archive_parser.add_argument("destination", type=Path)
    archive_parser.add_argument(
        "--domain", action="append", choices=sorted(dict(registry.DOMAIN_ROOTS))
    )
    archive_parser.add_argument(
        "--base", type=Path, help="verified base archive for incremental output"
    )
    archive_parser.set_defaults(
        func=archive, _iii_mutating=True, _iii_plan_provider=archive_preflight
    )

    import_parser = commands.add_parser(
        "import", help="plan or import a verified portable archive"
    )
    _common(import_parser)
    import_parser.add_argument("archive", type=Path)
    import_parser.set_defaults(
        func=import_archive, _iii_mutating=True, _iii_plan_provider=import_preflight
    )

    prune_parser = commands.add_parser(
        "prune", help="delete explicit unprotected record identities"
    )
    _common(prune_parser)
    prune_parser.add_argument(
        "--record", action="append", required=True, metavar="SHA256"
    )
    prune_parser.set_defaults(
        func=prune, _iii_mutating=True, _iii_plan_provider=prune_preflight
    )
