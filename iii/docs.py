"""Offline documentation validation through the canonical result contract."""

from __future__ import annotations

import argparse
from pathlib import Path

from .result import CommandResult, Finding, NextAction, Outcome


def _root(explicit: Path | None) -> Path:
    if explicit is not None:
        candidate = explicit.expanduser().resolve()
        if not (candidate / "deployment/documentation-policy.json").is_file():
            raise ValueError("--root is not an III workspace with documentation policy")
        return candidate
    current = Path.cwd().resolve()
    for candidate in (current, *current.parents):
        if (candidate / "deployment/documentation-policy.json").is_file():
            return candidate
    raise ValueError(
        "documentation checks require an III workspace checkout; pass --root"
    )


def check(args: argparse.Namespace) -> CommandResult:
    from iii_deployment.contracts import content_identity
    from iii_deployment.verification.documentation import (
        audit_manifest,
        load_policy,
        read_manifest,
    )

    try:
        root = _root(args.root)
        policy = load_policy(root / "deployment/documentation-policy.json")
        manifest = read_manifest(root / "deployment/documentation-manifest.json")
        errors = audit_manifest(root, policy, manifest)
        body = {
            "schema": "iii.documentation-check/v1",
            "manifest_id": manifest["manifest_id"],
            "policy_id": content_identity(policy),
            "documents": len(manifest["documents"]),
            "maintained": sum(
                row["lifecycle"] == "maintained" for row in manifest["documents"]
            ),
            "generated": sum(row["generated"] for row in manifest["documents"]),
            "errors": errors,
        }
        report = {**body, "check_id": content_identity(body)}
    except Exception as exc:
        return CommandResult(
            command="iii docs check",
            outcome=Outcome.FAILED,
            summary="Documentation validation could not load its governed inputs.",
            code="III_DOCS_INVALID",
            findings=(Finding("III_DOCS_INVALID", str(exc)),),
            next_actions=(
                NextAction(
                    ("iii", "docs", "check", "--root", "<workspace>", "--json"),
                    "Inspect the exact workspace documentation validation failure.",
                ),
            ),
        )
    if errors:
        return CommandResult(
            command="iii docs check",
            outcome=Outcome.REJECTED,
            summary=f"Documentation validation rejected {len(errors)} drift error(s).",
            code="III_DOCS_DRIFT",
            findings=tuple(Finding("III_DOCS_DRIFT", error) for error in errors),
            evidence=(manifest["manifest_id"], report["check_id"]),
            payload_schema=report["schema"],
            payload=report,
            next_actions=(
                NextAction(
                    (
                        "python3",
                        "deployment/scripts/update_documentation_references.py",
                    ),
                    "Regenerate references only when source command/schema changes are intended.",
                ),
                NextAction(
                    (
                        "python3",
                        "deployment/scripts/update_documentation_manifest.py",
                        "--root",
                        ".",
                        "--output",
                        "deployment/documentation-manifest.json",
                    ),
                    "Regenerate the inventory only after reviewing ownership and lifecycle changes.",
                ),
            ),
        )
    return CommandResult(
        command="iii docs check",
        outcome=Outcome.SUCCESS,
        summary=(
            f"Documentation is current: {report['maintained']} maintained document(s), "
            f"{report['generated']} generated reference(s)."
        ),
        code="III_DOCS_OK",
        evidence=(manifest["manifest_id"], report["check_id"]),
        payload_schema=report["schema"],
        payload=report,
        terminal_reason="Inventory, ownership, links, anchors, commands, schemas, routers, exclusions, and generated references are current.",
    )


def initialize(parser: argparse.ArgumentParser) -> None:
    commands = parser.add_subparsers(dest="docs_command")
    check_parser = commands.add_parser(
        "check",
        help="validate maintained documentation and generated references offline",
    )
    check_parser.add_argument("--root", type=Path)
    check_parser.set_defaults(func=check, _iii_mutating=False)
