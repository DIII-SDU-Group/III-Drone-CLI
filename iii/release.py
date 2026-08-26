"""Qualified release discovery, cache, handoff, and status commands.

The CLI deliberately owns only argument handling and the canonical command
result.  Release verification and materialisation remain in ``iii_deployment``
so every entry point enforces the same signed contracts.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Mapping

from .registry import registry_root
from .result import CommandResult, Finding, NextAction, Outcome


DEFAULT_REPOSITORY = "DIII-SDU-Group/III-Drone-ros2-ws"
DEFAULT_BUNDLE_TRUST = Path("/etc/iii-deployment/trusted-signers.json")
DEFAULT_STATUS_TRUST = Path("/etc/iii-deployment/release-status-trusted-signers.json")


def _environment(args: argparse.Namespace) -> Mapping[str, str]:
    return getattr(args, "_iii_environment", os.environ)


def _retain_cache_evidence(args: argparse.Namespace, cached: Any) -> tuple[Path, Path]:
    from .registry import atomic_json, registry_lock

    status = cached.status_index
    record = cached.record
    root = registry_root(_environment(args))
    with registry_lock(root):
        status_path = atomic_json(
            root, f"status-indexes/{status['index_id']}.json", status
        )
        evidence_path = atomic_json(
            root, f"release-evidence/{record['record_id']}.json", record
        )
    return status_path, evidence_path


def _first_existing(candidates: list[Path], *, label: str) -> Path:
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise ValueError(f"{label} does not exist; checked: " + ", ".join(str(path) for path in candidates))


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
    schema = Path(args.schema_root) if args.schema_root else _first_existing(
        [
            Path(env["III_DEPLOYMENT_SCHEMA_ROOT"]) if env.get("III_DEPLOYMENT_SCHEMA_ROOT") else Path("/__missing__"),
            prefix / "share/iii-deployment/schemas/v1",
            Path("/usr/local/share/iii-deployment/schemas/v1"),
            Path("/usr/share/iii-deployment/schemas/v1"),
            _workspace_candidate("deployment/schemas/v1"),
        ],
        label="deployment schema root",
    )
    policy = Path(args.policy) if args.policy else _first_existing(
        [
            Path(env["III_DEPLOYMENT_POLICY"]) if env.get("III_DEPLOYMENT_POLICY") else Path("/__missing__"),
            prefix / "share/iii-deployment/policy/operational-policy.json",
            Path("/usr/local/share/iii-deployment/policy/operational-policy.json"),
            Path("/usr/share/iii-deployment/policy/operational-policy.json"),
            _workspace_candidate("deployment/operational-policy.json"),
        ],
        label="deployment operational policy",
    )
    bundle_trust = Path(
        args.trusted_signers
        or env.get("III_RELEASE_TRUSTED_SIGNERS", str(DEFAULT_BUNDLE_TRUST))
    )
    status_trust = Path(
        args.status_trusted_signers
        or env.get("III_RELEASE_STATUS_TRUSTED_SIGNERS", str(DEFAULT_STATUS_TRUST))
    )
    cache = (
        Path(args.cache_root)
        if args.cache_root
        else Path(env["III_RELEASE_CACHE"])
        if env.get("III_RELEASE_CACHE")
        else registry_root(env) / "cache/releases"
    )
    return {
        "schema": schema,
        "policy": policy,
        "bundle_trust": bundle_trust,
        "status_trust": status_trust,
        "cache": cache,
    }


def _runtime(args: argparse.Namespace) -> dict[str, Any]:
    from iii_deployment.bundle import load_bundle_limits
    from iii_deployment.contracts import ContractRegistry
    from iii_deployment.release_registry import (
        GitHubReleaseSource,
        fetch_release,
        inspect_remote_release,
        list_remote_releases,
        load_cached_release,
        materialize_cached_release,
        refresh_cached_status,
    )

    paths = _paths(args)
    registry = ContractRegistry(paths["schema"])
    return {
        **paths,
        "registry": registry,
        "limits": load_bundle_limits(paths["policy"]),
        "source": GitHubReleaseSource(args.repository),
        "fetch_release": fetch_release,
        "inspect_remote_release": inspect_remote_release,
        "list_remote_releases": list_remote_releases,
        "load_cached_release": load_cached_release,
        "materialize_cached_release": materialize_cached_release,
        "refresh_cached_status": refresh_cached_status,
    }


def _cached_root(runtime: Mapping[str, Any], version: str) -> Path:
    parent = runtime["cache"] / version
    if parent.is_symlink() or not parent.is_dir():
        raise ValueError(f"qualified release {version} is not cached")
    candidates = sorted(path for path in parent.iterdir() if path.is_dir() and not path.is_symlink())
    if len(candidates) != 1:
        raise ValueError(f"qualified release {version} cache has {len(candidates)} identities; expected exactly one")
    return candidates[0]


def _load_cached(runtime: Mapping[str, Any], version: str):
    return runtime["load_cached_release"](
        _cached_root(runtime, version),
        bundle_trust=runtime["bundle_trust"],
        status_trust=runtime["status_trust"],
        registry=runtime["registry"],
        host_limits=runtime["limits"],
    )


def _rejected(command: str, version: str | None, exc: Exception) -> CommandResult:
    path = tuple(command.split()[2:])
    return CommandResult(
        command=command,
        outcome=Outcome.REJECTED,
        summary="The qualified release operation was refused.",
        code="III_RELEASE_CONTRACT_REJECTED",
        findings=(Finding("III_RELEASE_CONTRACT_REJECTED", str(exc)),),
        next_actions=(NextAction(("iii", "release", *path, "--help"), "Review release trust and command requirements."),),
    )


def _display_rows(rows: list[Mapping[str, Any]]) -> str:
    if not rows:
        return "No qualified releases were found."
    return "\n".join(
        f"{row['version']}  {row['status']:<9}  {row['release_id']}  {row['source_commit']}"
        for row in rows
    )


def list_releases(args: argparse.Namespace) -> CommandResult:
    try:
        runtime = _runtime(args)
        rows = runtime["list_remote_releases"](
            runtime["source"],
            bundle_trust=runtime["bundle_trust"],
            status_trust=runtime["status_trust"],
            registry=runtime["registry"],
        )
    except Exception as exc:
        return _rejected("iii release list", None, exc)
    return CommandResult(
        command="iii release list",
        outcome=Outcome.SUCCESS,
        summary=f"Verified {len(rows)} qualified release(s).",
        code="III_RELEASE_LIST_VERIFIED",
        terminal_reason="The signed remote release index was displayed without mutation.",
        payload_schema="iii.release-list/v1",
        payload={"releases": rows, "display": _display_rows(rows)},
    )


def _release_payload(cached_or_tuple: Any) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    if isinstance(cached_or_tuple, tuple):
        publication, notes, record, status, _index = cached_or_tuple
    else:
        publication, notes, record, status = (
            cached_or_tuple.publication, cached_or_tuple.notes,
            cached_or_tuple.record, cached_or_tuple.status,
        )
    return publication, notes, record, status


def show_release(args: argparse.Namespace) -> CommandResult:
    try:
        runtime = _runtime(args)
        value = _load_cached(runtime, args.version) if args.offline else runtime["inspect_remote_release"](
            runtime["source"],
            args.version,
            bundle_trust=runtime["bundle_trust"],
            status_trust=runtime["status_trust"],
            registry=runtime["registry"],
        )
        publication, notes, record, status = _release_payload(value)
    except Exception as exc:
        return _rejected("iii release show", args.version, exc)
    display = notes["markdown"].rstrip() + f"\n\nCurrent status: {status['status']} — {status['reason']}"
    return CommandResult(
        command="iii release show",
        outcome=Outcome.SUCCESS,
        summary=f"Verified qualified release {args.version} ({status['status']}).",
        code="III_RELEASE_SHOW_VERIFIED",
        release_id=publication["release_id"],
        terminal_reason=("The signed cached release was displayed in explicit offline mode." if args.offline else "The signed remote release and current status were displayed without mutation."),
        payload_schema="iii.release-detail/v1",
        payload={"publication": publication, "notes": notes, "record": record, "status": status, "offline": args.offline, "display": display},
    )


def fetch(args: argparse.Namespace) -> CommandResult:
    try:
        runtime = _runtime(args)
        cached = runtime["fetch_release"](
            runtime["source"],
            args.version,
            runtime["cache"],
            bundle_trust=runtime["bundle_trust"],
            status_trust=runtime["status_trust"],
            registry=runtime["registry"],
            host_limits=runtime["limits"],
            fetched_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        )
        status_path, evidence_path = _retain_cache_evidence(args, cached)
    except Exception as exc:
        return _rejected("iii release fetch", args.version, exc)
    return CommandResult(
        command="iii release fetch",
        outcome=Outcome.SUCCESS,
        summary=f"Fetched and verified qualified release {args.version}.",
        code="III_RELEASE_FETCHED",
        release_id=cached.publication["release_id"],
        evidence=(str(cached.root), str(status_path), str(evidence_path), cached.publication["publication_id"], cached.status["statement_id"]),
        terminal_reason="The complete signed release is atomically cached and ready for offline use.",
        payload_schema="iii.release-cache/v1",
        payload={"version": args.version, "cache": str(cached.root), "status": cached.status},
    )


def cache(args: argparse.Namespace) -> CommandResult:
    try:
        runtime = _runtime(args)
        cached = _load_cached(runtime, args.version)
    except Exception as exc:
        return _rejected("iii release cache", args.version, exc)
    return CommandResult(
        command="iii release cache",
        outcome=Outcome.SUCCESS,
        summary=f"Verified cached qualified release {args.version}.",
        code="III_RELEASE_CACHE_VERIFIED",
        release_id=cached.publication["release_id"],
        evidence=(str(cached.root), cached.publication["publication_id"], cached.status["statement_id"]),
        terminal_reason="Every cached asset and its signed status chain verified without network access.",
        payload_schema="iii.release-cache/v1",
        payload={"version": args.version, "cache": str(cached.root), "status": cached.status},
    )


def verify(args: argparse.Namespace) -> CommandResult:
    try:
        runtime = _runtime(args)
        value = _load_cached(runtime, args.version) if args.offline else runtime["inspect_remote_release"](
            runtime["source"],
            args.version,
            bundle_trust=runtime["bundle_trust"],
            status_trust=runtime["status_trust"],
            registry=runtime["registry"],
        )
        publication, _notes, record, status = _release_payload(value)
    except Exception as exc:
        return _rejected("iii release verify", args.version, exc)
    return CommandResult(
        command="iii release verify",
        outcome=Outcome.SUCCESS,
        summary=f"Verified qualified release {args.version} ({status['status']}).",
        code="III_RELEASE_VERIFIED",
        release_id=publication["release_id"],
        evidence=(publication["publication_id"], record["record_id"], status["statement_id"]),
        terminal_reason=(
            "The complete cached publication, audit record, bundles, and signed status verified offline."
            if args.offline else
            "The remote publication, audit record, and current signed status verified without mutation."
        ),
        payload_schema="iii.release-verification/v1",
        payload={
            "version": args.version,
            "release_id": publication["release_id"],
            "publication_id": publication["publication_id"],
            "record_id": record["record_id"],
            "status": status,
            "offline": args.offline,
        },
    )


def deploy(args: argparse.Namespace) -> CommandResult:
    try:
        runtime = _runtime(args)
        cached = _load_cached(runtime, args.version)
        if not args.offline:
            runtime["refresh_cached_status"](
                cached.root,
                runtime["source"].latest_status_index(),
                status_trust=runtime["status_trust"],
                registry=runtime["registry"],
            )
            cached = _load_cached(runtime, args.version)
        status_path, evidence_path = _retain_cache_evidence(args, cached)
        destination = runtime["materialize_cached_release"](
            cached,
            Path(args.destination),
            bundle_trust=runtime["bundle_trust"],
            registry=runtime["registry"],
            host_limits=runtime["limits"],
        )
    except Exception as exc:
        return _rejected("iii release deploy", args.version, exc)
    return CommandResult(
        command="iii release deploy",
        outcome=Outcome.SUCCESS,
        summary=f"Materialized verified release {args.version} for deployment handoff.",
        code="III_RELEASE_HANDOFF_READY",
        release_id=cached.publication["release_id"],
        evidence=(str(destination), str(status_path), str(evidence_path), cached.publication["publication_id"], cached.status["statement_id"]),
        terminal_reason=("The explicitly offline cached release was materialized; no remote status refresh was possible." if args.offline else "The current signed status was refreshed before atomic local materialization."),
        payload_schema="iii.release-handoff/v1",
        payload={"version": args.version, "destination": str(destination), "status": cached.status, "offline": args.offline},
    )


def set_status(args: argparse.Namespace) -> CommandResult:
    try:
        runtime = _runtime(args)
        publication, _notes, _record, current, _index = runtime["inspect_remote_release"](
            runtime["source"],
            args.version,
            bundle_trust=runtime["bundle_trust"],
            status_trust=runtime["status_trust"],
            registry=runtime["registry"],
        )
        allowed = {"qualified": {"withdrawn", "unsafe"}, "withdrawn": {"unsafe"}, "unsafe": set()}
        if args.status not in allowed[current["status"]]:
            raise ValueError(f"non-monotonic status transition {current['status']} -> {args.status}")
        command = [
            "gh", "workflow", "run", "release-status.yml", "--repo", args.repository,
            "--ref", "release", "-f", f"version={args.version}", "-f", f"status={args.status}",
            "-f", f"reason={args.reason}", "-f", f"expected_statement_id={current['statement_id']}",
            "-f", f"client_operation_id={getattr(args, '_iii_operation_id', '')}",
        ]
        if args.superseding_version:
            command.extend(["-f", f"superseding_version={args.superseding_version}"])
        process = subprocess.run(command, capture_output=True, text=True, check=False)
        if process.returncode:
            raise RuntimeError(process.stderr.strip() or "release-status workflow dispatch failed")
    except Exception as exc:
        return _rejected("iii release status set", args.version, exc)
    return CommandResult(
        command="iii release status set",
        outcome=Outcome.SUCCESS,
        summary=f"Requested signed {args.status} status for {args.version}.",
        code="III_RELEASE_STATUS_DISPATCHED",
        release_id=publication["release_id"],
        evidence=(current["statement_id"],),
        terminal_reason="The trusted release-branch workflow accepted the status request; publication is serialized server-side.",
        payload_schema="iii.release-status-dispatch/v1",
        payload={"version": args.version, "release_id": publication["release_id"], "previous_status": current["status"], "requested_status": args.status, "workflow_output": process.stdout.strip()},
    )


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repository", default=DEFAULT_REPOSITORY, help="GitHub owner/repository")
    parser.add_argument("--schema-root", type=Path, help="deployment schema directory")
    parser.add_argument("--policy", type=Path, help="operational policy JSON")
    parser.add_argument("--trusted-signers", type=Path, help="qualified bundle signer trust store")
    parser.add_argument("--status-trusted-signers", type=Path, help="release-status signer trust store")
    parser.add_argument("--cache-root", type=Path, help="qualified release cache root")


def initialize(parser: argparse.ArgumentParser) -> None:
    subparsers = parser.add_subparsers(dest="release_command")

    list_parser = subparsers.add_parser("list", help="list verified qualified releases")
    _common(list_parser)
    list_parser.set_defaults(func=list_releases, _iii_mutating=False)

    show_parser = subparsers.add_parser("show", help="show verified release notes and status")
    show_parser.add_argument("version")
    show_parser.add_argument("--offline", action="store_true", help="use only the verified local cache")
    _common(show_parser)
    show_parser.set_defaults(func=show_release, _iii_mutating=False)

    fetch_parser = subparsers.add_parser("fetch", help="atomically fetch and verify a qualified release")
    fetch_parser.add_argument("version")
    _common(fetch_parser)
    fetch_parser.set_defaults(func=fetch, _iii_mutating=True)

    cache_parser = subparsers.add_parser("cache", help="verify a cached qualified release without network access")
    cache_parser.add_argument("version")
    _common(cache_parser)
    cache_parser.set_defaults(func=cache, _iii_mutating=False)

    verify_parser = subparsers.add_parser("verify", help="verify a remote or explicitly cached qualified release")
    verify_parser.add_argument("version")
    verify_parser.add_argument("--offline", action="store_true", help="verify only the complete local cache")
    _common(verify_parser)
    verify_parser.set_defaults(func=verify, _iii_mutating=False)

    deploy_parser = subparsers.add_parser("deploy", help="materialize a cached release for deployment handoff")
    deploy_parser.add_argument("version")
    deploy_parser.add_argument("--destination", required=True, type=Path)
    deploy_parser.add_argument("--offline", action="store_true", help="explicitly accept the last verified cached status")
    _common(deploy_parser)
    deploy_parser.set_defaults(func=deploy, _iii_mutating=True)

    status_parser = subparsers.add_parser("status", help="manage signed release status")
    status_subparsers = status_parser.add_subparsers(dest="release_status_command")
    set_parser = status_subparsers.add_parser("set", help="request an append-only withdrawal or unsafe status")
    set_parser.add_argument("version")
    set_parser.add_argument("--status", choices=("withdrawn", "unsafe"), required=True)
    set_parser.add_argument("--reason", required=True)
    set_parser.add_argument("--superseding-version")
    _common(set_parser)
    set_parser.set_defaults(func=set_status, _iii_mutating=True)
