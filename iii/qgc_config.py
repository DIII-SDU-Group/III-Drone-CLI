"""Managed-key QGroundControl configuration commands."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Mapping

from .result import CommandResult, Finding, Outcome

UNIT = "iii-qgc.service"


def _environment(args: argparse.Namespace) -> Mapping[str, str]:
    return getattr(args, "_iii_environment", os.environ)


def _workspace(args: argparse.Namespace) -> Path | None:
    explicit = _environment(args).get("WORKSPACE_DIR")
    candidates = [Path(explicit)] if explicit else []
    candidates.extend((Path.cwd(), *Path.cwd().parents))
    for candidate in candidates:
        root = candidate.expanduser().resolve()
        if (root / "deployment/qgc/key-policy.json").is_file():
            return root
    return None


def _resource(args: argparse.Namespace, name: str) -> Path:
    env = _environment(args)
    variable = {
        "key-policy.json": "III_QGC_KEY_POLICY",
        "managed-settings.json": "III_QGC_MANAGED_SETTINGS",
    }[name]
    candidates = []
    if env.get(variable):
        candidates.append(Path(env[variable]))
    candidates.append(Path(sys.prefix) / "share/iii-deployment/qgc" / name)
    workspace = _workspace(args)
    if workspace is not None:
        candidates.append(workspace / "deployment/qgc" / name)
    for candidate in candidates:
        path = candidate.expanduser().resolve()
        if path.is_file() and not path.is_symlink():
            return path
    raise ValueError(f"cannot locate QGroundControl resource {name}")


def _schemas(args: argparse.Namespace) -> Path:
    env = _environment(args)
    candidates = []
    if env.get("III_DEPLOYMENT_SCHEMA_ROOT"):
        candidates.append(Path(env["III_DEPLOYMENT_SCHEMA_ROOT"]))
    candidates.append(Path(sys.prefix) / "share/iii-deployment/schemas/v1")
    workspace = _workspace(args)
    if workspace is not None:
        candidates.append(workspace / "deployment/schemas/v1")
    for candidate in candidates:
        path = candidate.expanduser().resolve()
        if path.is_dir() and not path.is_symlink():
            return path
    raise ValueError("cannot locate deployment schemas")


def _settings(args: argparse.Namespace) -> Path:
    value = _environment(args).get("III_QGC_SETTINGS_PATH")
    return (
        Path(value).expanduser().resolve()
        if value
        else Path.home() / ".config/QGroundControl.org/QGroundControl.ini"
    )


def _state_root(args: argparse.Namespace) -> Path:
    env = _environment(args)
    if env.get("III_QGC_CONFIGURATION_STATE_ROOT"):
        return Path(env["III_QGC_CONFIGURATION_STATE_ROOT"]).expanduser().resolve()
    if env.get("III_REGISTRY_ROOT"):
        return (
            Path(env["III_REGISTRY_ROOT"]).expanduser().resolve() / "qgc-configuration"
        )
    return Path.home() / ".local/state/iii/qgc-configuration"


def _store(args: argparse.Namespace):
    from iii_deployment.qgc_configuration import QGCConfigurationStore

    return QGCConfigurationStore(
        settings_path=_settings(args),
        state_root=_state_root(args),
        policy_path=_resource(args, "key-policy.json"),
        baseline_path=_resource(args, "managed-settings.json"),
        schema_root=_schemas(args),
    )


def _unit_active() -> bool:
    completed = subprocess.run(
        ["systemctl", "--user", "is-active", "--quiet", UNIT],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return completed.returncode == 0


def _accepted(
    command: str, code: str, summary: str, payload: Mapping[str, Any]
) -> CommandResult:
    return CommandResult(
        command=command,
        outcome=Outcome.SUCCESS,
        summary=summary,
        code=code,
        payload_schema=str(payload.get("schema", "iii.qgc-config-result/v1")),
        payload=payload,
        terminal_reason="Only the declared QGroundControl configuration boundary changed or was inspected.",
    )


def _rejected(command: str, code: str, exc: Exception) -> CommandResult:
    return CommandResult(
        command=command,
        outcome=Outcome.REJECTED,
        summary="The QGroundControl configuration operation was refused.",
        code=code,
        findings=(Finding(code, str(exc)),),
        terminal_reason="No QGroundControl configuration or aircraft mutation was performed.",
    )


def apply_preflight(args: argparse.Namespace) -> dict[str, Any]:
    store = _store(args)
    active = _unit_active()
    if active:
        raise ValueError("QGroundControl must be stopped before configuration merge")
    settings = _settings(args)
    evidence = None
    if settings.exists() or settings.is_symlink():
        if settings.is_symlink() or not settings.is_file():
            raise ValueError("QGroundControl settings path is unsafe")
        evidence = hashlib.sha256(settings.read_bytes()).hexdigest()
    return {
        "schema": "iii.qgc-config-apply-preflight/v1",
        "release_id": args.release_id,
        "qgc_version": args.qgc_version,
        "profile": args.profile,
        "settings_sha256": evidence,
        "policy_id": store.policy["policy_id"],
        "baseline_id": store.baseline["settings_id"],
        "unit_active": False,
        "mutations": ["backup and transactionally merge declared QGC managed keys"],
    }


def apply(args: argparse.Namespace) -> CommandResult:
    command = "iii qgc config apply"
    try:
        retained = getattr(args, "_iii_retained_plan", None)
        if not isinstance(retained, Mapping) or apply_preflight(args) != retained.get(
            "preflight"
        ):
            raise ValueError("QGroundControl settings or release inputs changed")
        result = _store(args).apply(
            qgc_version=args.qgc_version,
            release_id=args.release_id,
            profile=args.profile,
            qgc_running=False,
        )
    except Exception as exc:
        return _rejected(command, "III_QGC_CONFIG_APPLY_REJECTED", exc)
    return _accepted(
        command,
        "III_QGC_CONFIG_APPLY",
        "Backed up and transactionally merged the release-managed QGroundControl keys.",
        result,
    )


def capture_preflight(args: argparse.Namespace) -> dict[str, Any]:
    store = _store(args)
    active = _unit_active()
    if args.clean_exit and active:
        raise ValueError("--clean-exit requires the QGroundControl unit to be inactive")
    settings = _settings(args)
    if settings.is_symlink() or not settings.is_file():
        raise ValueError("QGroundControl settings are missing or unsafe")
    return {
        "schema": "iii.qgc-config-capture-preflight/v1",
        "release_id": args.release_id,
        "qgc_version": args.qgc_version,
        "clean_exit": args.clean_exit,
        "unit_active": active,
        "settings_sha256": hashlib.sha256(settings.read_bytes()).hexdigest(),
        "baseline_id": store.baseline["settings_id"],
        "mutations": ["create one redacted immutable local QGC capture"],
    }


def capture(args: argparse.Namespace) -> CommandResult:
    command = "iii qgc config capture"
    try:
        retained = getattr(args, "_iii_retained_plan", None)
        if not isinstance(retained, Mapping) or capture_preflight(args) != retained.get(
            "preflight"
        ):
            raise ValueError("QGroundControl capture inputs changed")
        result = _store(args).capture(
            qgc_version=args.qgc_version,
            release_id=args.release_id,
            clean_exit=args.clean_exit,
            expected_settings_sha256=retained["preflight"]["settings_sha256"],
        )
    except Exception as exc:
        return _rejected(command, "III_QGC_CONFIG_CAPTURE_REJECTED", exc)
    return _accepted(
        command,
        "III_QGC_CONFIG_CAPTURE",
        "Created a redacted immutable QGroundControl configuration capture.",
        result,
    )


def diff(args: argparse.Namespace) -> CommandResult:
    command = "iii qgc config diff"
    try:
        result = _store(args).diff(args.capture_id)
    except Exception as exc:
        return _rejected(command, "III_QGC_CONFIG_DIFF_REJECTED", exc)
    return _accepted(
        command,
        "III_QGC_CONFIG_DIFF",
        f"Compared the capture with {len(result['changes'])} managed-key changes.",
        result,
    )


def _git(args: argparse.Namespace, *command: str) -> str:
    workspace = _workspace(args)
    if workspace is None:
        raise ValueError("QGroundControl promotion requires a workspace checkout")
    completed = subprocess.run(
        ["git", *command],
        cwd=workspace,
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if completed.returncode:
        raise ValueError(completed.stderr.strip() or "git inspection failed")
    return completed.stdout.strip()


def promote_preflight(args: argparse.Namespace) -> dict[str, Any]:
    store = _store(args)
    promoted = store.promoted_baseline(args.capture_id, args.key)
    workspace = _workspace(args)
    if workspace is None:
        raise ValueError("QGroundControl promotion requires a workspace checkout")
    source = workspace / "deployment/qgc/managed-settings.json"
    active_source = json.loads(source.read_bytes())
    if active_source.get("settings_id") != store.baseline["settings_id"]:
        raise ValueError("QGroundControl feature source differs from active baseline")
    branch = _git(args, "branch", "--show-current")
    if (
        not branch
        or branch in {"develop", "main", "release"}
        or branch.startswith("promote/")
    ):
        raise ValueError("QGroundControl promotion requires a normal feature branch")
    if _git(args, "status", "--porcelain", "--", str(source)):
        raise ValueError("QGroundControl baseline source is already modified")
    return {
        "schema": "iii.qgc-config-promote-preflight/v1",
        "branch": branch,
        "head": _git(args, "rev-parse", "HEAD"),
        "capture_id": args.capture_id,
        "accepted_keys": sorted(args.key),
        "source": str(source),
        "old_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "new_settings_id": promoted["settings_id"],
        "mutations": ["write reviewed managed QGC keys to feature source"],
    }


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    if path.is_symlink():
        raise ValueError("QGroundControl promotion source is linked")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.partial")
    try:
        with temporary.open("xb") as stream:
            os.chmod(temporary, 0o600)
            stream.write(
                json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
                + b"\n"
            )
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def promote(args: argparse.Namespace) -> CommandResult:
    command = "iii qgc config promote"
    try:
        retained = getattr(args, "_iii_retained_plan", None)
        if not isinstance(retained, Mapping) or promote_preflight(args) != retained.get(
            "preflight"
        ):
            raise ValueError("QGroundControl promotion source or Git state changed")
        promoted = _store(args).promoted_baseline(args.capture_id, args.key)
        _atomic_json(Path(retained["preflight"]["source"]), promoted)
    except Exception as exc:
        return _rejected(command, "III_QGC_CONFIG_PROMOTE_REJECTED", exc)
    return _accepted(
        command,
        "III_QGC_CONFIG_PROMOTE",
        "Wrote only reviewed managed QGroundControl keys to the feature branch.",
        {
            "schema": "iii.qgc-config-promote-result/v1",
            "capture_id": args.capture_id,
            "settings_id": promoted["settings_id"],
            "accepted_keys": sorted(args.key),
            "committed": False,
            "pushed": False,
        },
    )


def cache_preflight(args: argparse.Namespace) -> dict[str, Any]:
    source = Path(args.source).expanduser().resolve()
    if source.is_symlink() or not source.is_dir():
        raise ValueError("QGroundControl generated cache source is unsafe")
    entries = []
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise ValueError("QGroundControl generated cache contains a link")
        if path.is_file():
            entries.append(
                (
                    path.relative_to(source).as_posix(),
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                )
            )
    return {
        "schema": "iii.qgc-generated-cache-preflight/v1",
        "source": str(source),
        "entries": entries,
        "qgc_version": args.qgc_version,
        "px4_firmware": args.px4_firmware,
        "parameter_manifest_id": args.manifest_id,
        "mutations": ["cache version-bound generated QGC parameter metadata"],
    }


def cache(args: argparse.Namespace) -> CommandResult:
    command = "iii qgc config cache"
    try:
        retained = getattr(args, "_iii_retained_plan", None)
        if not isinstance(retained, Mapping) or cache_preflight(args) != retained.get(
            "preflight"
        ):
            raise ValueError("QGroundControl generated cache source changed")
        result = _store(args).cache_generated(
            Path(args.source),
            qgc_version=args.qgc_version,
            px4_firmware=args.px4_firmware,
            parameter_manifest_id=args.manifest_id,
        )
    except Exception as exc:
        return _rejected(command, "III_QGC_CONFIG_CACHE_REJECTED", exc)
    return _accepted(
        command,
        "III_QGC_CONFIG_CACHE",
        "Cached generated QGroundControl metadata under its compatibility identity.",
        result,
    )


def verify_cache(args: argparse.Namespace) -> CommandResult:
    command = "iii qgc config verify-cache"
    try:
        result = _store(args).verify_generated(
            args.cache_id,
            qgc_version=args.qgc_version,
            px4_firmware=args.px4_firmware,
            parameter_manifest_id=args.manifest_id,
        )
    except Exception as exc:
        return _rejected(command, "III_QGC_CONFIG_CACHE_VERIFY_REJECTED", exc)
    return _accepted(
        command,
        "III_QGC_CONFIG_CACHE_VERIFY",
        "Verified QGroundControl generated-cache content and compatibility.",
        result,
    )


def initialize(parser: argparse.ArgumentParser) -> None:
    commands = parser.add_subparsers(dest="qgc_config_command")
    apply_parser = commands.add_parser(
        "apply", help="merge the release managed-key baseline"
    )
    apply_parser.add_argument("--release-id", required=True)
    apply_parser.add_argument("--qgc-version", required=True)
    apply_parser.add_argument("--profile", choices=("real", "sim"), required=True)
    apply_parser.set_defaults(
        func=apply, _iii_mutating=True, _iii_plan_provider=apply_preflight
    )

    capture_parser = commands.add_parser(
        "capture", help="capture redacted QGC settings"
    )
    capture_parser.add_argument("--release-id", required=True)
    capture_parser.add_argument("--qgc-version", required=True)
    capture_parser.add_argument("--clean-exit", action="store_true")
    capture_parser.set_defaults(
        func=capture, _iii_mutating=True, _iii_plan_provider=capture_preflight
    )
    diff_parser = commands.add_parser(
        "diff", help="diff a capture against the baseline"
    )
    diff_parser.add_argument("--capture-id", required=True)
    diff_parser.set_defaults(func=diff, _iii_mutating=False)
    promote_parser = commands.add_parser(
        "promote", help="promote reviewed managed keys"
    )
    promote_parser.add_argument("--capture-id", required=True)
    promote_parser.add_argument("--key", action="append", required=True)
    promote_parser.set_defaults(
        func=promote, _iii_mutating=True, _iii_plan_provider=promote_preflight
    )
    cache_parser = commands.add_parser(
        "cache", help="cache generated ParamCache content"
    )
    cache_parser.add_argument("--source", required=True)
    cache_parser.add_argument("--qgc-version", required=True)
    cache_parser.add_argument("--px4-firmware", required=True)
    cache_parser.add_argument("--manifest-id", required=True)
    cache_parser.set_defaults(
        func=cache, _iii_mutating=True, _iii_plan_provider=cache_preflight
    )
    verify_parser = commands.add_parser(
        "verify-cache", help="verify generated ParamCache content and compatibility"
    )
    verify_parser.add_argument("--cache-id", required=True)
    verify_parser.add_argument("--qgc-version", required=True)
    verify_parser.add_argument("--px4-firmware", required=True)
    verify_parser.add_argument("--manifest-id", required=True)
    verify_parser.set_defaults(func=verify_cache, _iii_mutating=False)
