"""Workspace deployment verification through the canonical III result surface."""

from __future__ import annotations

import argparse
from pathlib import Path


def _root(explicit: Path | None) -> Path:
    if explicit is not None:
        root = explicit.expanduser().resolve()
        if not (root / "deployment/verification/matrix.json").is_file():
            raise ValueError(
                "--root is not an III workspace with a verification matrix"
            )
        return root
    current = Path.cwd().resolve()
    for candidate in (current, *current.parents):
        if (candidate / "deployment/verification/matrix.json").is_file():
            return candidate
    raise ValueError(
        "deployment verification requires an III workspace checkout; pass --root"
    )


def deployment(args: argparse.Namespace):
    from iii_deployment.verification.cli import audit

    try:
        root = _root(args.root)
    except ValueError as exc:
        from .result import CommandResult, Finding, NextAction, Outcome

        return CommandResult(
            command="iii verify deployment",
            outcome=Outcome.REJECTED,
            summary="Deployment verification could not locate its governed source inputs.",
            code="III_VERIFY_ROOT_REQUIRED",
            findings=(Finding("III_VERIFY_ROOT_REQUIRED", str(exc)),),
            next_actions=(
                NextAction(
                    ("iii", "verify", "deployment", "--root", "<workspace>"),
                    "Select the exact workspace candidate whose matrix must be audited.",
                ),
            ),
        )
    return audit(
        backlog_path=root / "codex-backlogs/deployment-infrastructure-redesign.md",
        baseline_path=root / "deployment/verification/clause-baseline.json",
        migrations_path=root / "deployment/verification/clause-migrations.json",
        policy_path=root / "deployment/verification/policy.json",
        matrix_path=root / "deployment/verification/matrix.json",
        schema_root=root / "deployment/schemas/v1",
        evidence_paths=args.evidence,
        trusted_signers_path=args.trusted_signers,
        junit_path=args.junit,
        report_path=args.report,
        require_levels=args.require_level,
        require_complete=args.require_complete,
        audit_only=getattr(args, "audit_only", False),
    )


def initialize(parser: argparse.ArgumentParser) -> None:
    commands = parser.add_subparsers(dest="verify_command")
    deployment_parser = commands.add_parser(
        "deployment",
        help="audit the versioned deployment verification matrix and authenticated evidence",
    )
    deployment_parser.add_argument("--root", type=Path)
    deployment_parser.add_argument("--evidence", action="append", default=[], type=Path)
    deployment_parser.add_argument("--trusted-signers", type=Path)
    deployment_parser.add_argument("--junit", type=Path)
    deployment_parser.add_argument("--report", type=Path)
    deployment_parser.add_argument(
        "--require-level",
        action="append",
        default=[],
        choices=("host-independent", "target-equivalent", "physical"),
    )
    deployment_parser.add_argument("--require-complete", action="store_true")
    deployment_parser.add_argument(
        "--audit-only",
        action="store_true",
        help="validate reviewed definitions without claiming execution evidence",
    )
    deployment_parser.set_defaults(func=deployment, _iii_mutating=False)
