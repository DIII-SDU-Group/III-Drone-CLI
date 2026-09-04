"""Release-owned PX4 parameter inventory and explicit transaction commands."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Mapping, Sequence

from .result import CommandResult, Finding, NextAction, Outcome


def _environment(args: argparse.Namespace) -> Mapping[str, str]:
    return getattr(args, "_iii_environment", os.environ)


def _workspace(args: argparse.Namespace) -> Path | None:
    explicit = _environment(args).get("WORKSPACE_DIR")
    candidates = [Path(explicit)] if explicit else []
    candidates.extend((Path.cwd(), *Path.cwd().parents))
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if (resolved / "deployment/px4/real.json").is_file():
            return resolved
    return None


def _resource_root(args: argparse.Namespace, kind: str) -> Path:
    env = _environment(args)
    variable = {
        "px4": "III_PX4_MANIFEST_ROOT",
        "schemas": "III_DEPLOYMENT_SCHEMA_ROOT",
    }[kind]
    candidates = []
    if env.get(variable):
        candidates.append(Path(env[variable]))
    candidates.append(
        Path(sys.prefix)
        / "share/iii-deployment"
        / ("px4" if kind == "px4" else "schemas/v1")
    )
    workspace = _workspace(args)
    if workspace is not None:
        candidates.append(
            workspace / "deployment" / ("px4" if kind == "px4" else "schemas/v1")
        )
    for candidate in candidates:
        path = candidate.expanduser().resolve()
        if path.is_dir() and not path.is_symlink():
            return path
    raise ValueError(f"cannot locate deployment {kind} resources")


def _state_root(args: argparse.Namespace) -> Path:
    env = _environment(args)
    if env.get("III_PX4_PARAMETER_STATE_ROOT"):
        return Path(env["III_PX4_PARAMETER_STATE_ROOT"]).expanduser().resolve()
    if env.get("III_REGISTRY_ROOT"):
        return Path(env["III_REGISTRY_ROOT"]).expanduser().resolve() / "px4"
    workspace = _workspace(args)
    if workspace is not None:
        return workspace / ".iii/px4"
    return Path.home() / ".local/state/iii/px4"


def _store(args: argparse.Namespace):
    from iii_deployment.px4_parameters import (
        MavlinkParameterAdapter,
        PX4ParameterStore,
    )

    root = _resource_root(args, "px4")
    endpoint = _environment(args).get(
        "III_PX4_MAVLINK_ENDPOINT", "udpin:127.0.0.1:14551"
    )
    return PX4ParameterStore(
        manifest_paths={"real": root / "real.json", "sim": root / "sim.json"},
        state_root=_state_root(args),
        schema_root=_resource_root(args, "schemas"),
        adapter=MavlinkParameterAdapter(endpoint),
    )


def _accepted(
    command: str,
    code: str,
    summary: str,
    payload: Mapping[str, Any],
    *,
    profile: str | None = None,
    outcome: Outcome = Outcome.SUCCESS,
    findings: Sequence[Finding] = (),
) -> CommandResult:
    return CommandResult(
        command=command,
        outcome=outcome,
        summary=summary,
        code=code,
        profile=profile,
        findings=tuple(findings),
        payload_schema=str(payload.get("schema", "iii.px4-result/v1")),
        payload=payload,
        terminal_reason="The PX4 operation completed within its declared read/write boundary.",
    )


def _rejected(command: str, code: str, exc: Exception) -> CommandResult:
    result = getattr(exc, "result", None)
    return CommandResult(
        command=command,
        outcome=Outcome.FAILED if result else Outcome.REJECTED,
        summary="The PX4 parameter operation was refused or failed closed.",
        code=code,
        findings=(Finding(code, str(exc)),),
        payload_schema=result.get("schema") if isinstance(result, Mapping) else None,
        payload=result if isinstance(result, Mapping) else {},
        terminal_reason="No unconfirmed PX4 write was retained; recovery truth is included when a write was attempted.",
    )


def pull(args: argparse.Namespace) -> CommandResult:
    command = "iii px4 params pull"
    try:
        store = _store(args)
        if args.profile == "real":
            if not getattr(args, "release_id", None):
                raise ValueError(
                    "real PX4 capture requires the exact staged --release-id; "
                    "the ground-control host must use receiver-owned Ethernet"
                )
            from .operation import operation_id as new_operation_id
            from .ssh_manager import SSHManager

            result = SSHManager().px4_audit(
                release_id=args.release_id,
                operation_id=getattr(args, "_iii_operation_id", None)
                or new_operation_id(),
            )
            evidence = result.get("activation_evidence")
            if not isinstance(evidence, Mapping) or not isinstance(
                evidence.get("snapshot"), Mapping
            ):
                raise ValueError("receiver did not return a complete PX4 inventory")
            snapshot = store.retain_snapshot(evidence["snapshot"])
        else:
            snapshot = store.pull(args.profile)
        comparison = store.compare(args.profile, snapshot["snapshot_id"])
    except Exception as exc:
        return _rejected(command, "III_PX4_PULL_REJECTED", exc)
    findings = []
    if not comparison["required_match"]:
        findings.append(
            Finding(
                "III_PX4_REQUIRED_DRIFT",
                "Required PX4 parameters differ from the release manifest",
                severity="warning",
            )
        )
    return _accepted(
        command,
        "III_PX4_PULL",
        "Captured and authenticated a complete disarmed PX4 inventory.",
        {
            "schema": "iii.px4-pull-result/v1",
            "snapshot": snapshot,
            "comparison": comparison,
            "writes_performed": 0,
        },
        profile=args.profile,
        outcome=Outcome.WARNING if findings else Outcome.SUCCESS,
        findings=findings,
    )


def plan(args: argparse.Namespace) -> CommandResult:
    command = "iii px4 params plan"
    try:
        value = _store(args).plan(
            args.profile, args.snapshot, selected_keys=args.key or None
        )
    except Exception as exc:
        return _rejected(command, "III_PX4_PLAN_REJECTED", exc)
    return _accepted(
        command,
        "III_PX4_PLAN",
        "Retained an exact per-key PX4 plan without writing the FMU.",
        value,
        profile=args.profile,
    )


def apply_preflight(args: argparse.Namespace) -> dict[str, Any]:
    store = _store(args)
    plan_value = store.load_plan(args.plan_id)
    status = dict(store.adapter.status())
    if status.get("armed"):
        raise ValueError("PX4 apply is forbidden while armed")
    return {
        "schema": "iii.px4-apply-preflight/v1",
        "plan": plan_value,
        "status": status,
        "confirmed_keys": sorted(args.key),
        "mutations": [f"PX4 PARAM_SET {name}" for name in sorted(args.key)],
    }


def apply(args: argparse.Namespace) -> CommandResult:
    command = "iii px4 params apply"
    try:
        retained = getattr(args, "_iii_retained_plan", None)
        if not isinstance(retained, Mapping) or not isinstance(
            retained.get("preflight"), Mapping
        ):
            raise ValueError("an exact retained PX4 apply plan is required")
        if apply_preflight(args) != retained["preflight"]:
            raise ValueError("PX4 target or retained parameter plan changed")
        result = _store(args).apply(args.plan_id, confirmed_keys=args.key)
    except Exception as exc:
        return _rejected(command, "III_PX4_APPLY_REJECTED", exc)
    return _accepted(
        command,
        "III_PX4_APPLY",
        "Applied the confirmed PX4 keys and verified the complete readback.",
        result,
        profile=retained["preflight"]["plan"]["profile"],
    )


def verify(args: argparse.Namespace) -> CommandResult:
    command = "iii px4 params verify"
    try:
        result = _store(args).verify(args.plan_id)
    except Exception as exc:
        return _rejected(command, "III_PX4_VERIFY_REJECTED", exc)
    findings = (
        ()
        if result["verified"]
        else (
            Finding(
                "III_PX4_READBACK_MISMATCH",
                "PX4 values differ from the retained plan",
            ),
        )
    )
    return _accepted(
        command,
        "III_PX4_VERIFY" if not findings else "III_PX4_VERIFY_MISMATCH",
        "Verified the retained PX4 plan against a fresh complete inventory.",
        result,
        outcome=Outcome.SUCCESS if not findings else Outcome.REJECTED,
        findings=findings,
    )


def capture_preflight(args: argparse.Namespace) -> dict[str, Any]:
    snapshot = _store(args).load_snapshot(args.snapshot)
    return {
        "schema": "iii.px4-capture-preflight/v1",
        "snapshot_id": snapshot["snapshot_id"],
        "short_name": args.name,
        "description_sha256": hashlib.sha256(args.description.encode()).hexdigest(),
        "mutations": ["create immutable local PX4 capture metadata"],
    }


def capture(args: argparse.Namespace) -> CommandResult:
    command = "iii px4 params capture"
    try:
        retained = getattr(args, "_iii_retained_plan", None)
        if not isinstance(retained, Mapping) or capture_preflight(args) != retained.get(
            "preflight"
        ):
            raise ValueError("PX4 capture inputs changed after planning")
        result = _store(args).capture(
            args.snapshot, short_name=args.name, description=args.description
        )
    except Exception as exc:
        return _rejected(command, "III_PX4_CAPTURE_REJECTED", exc)
    return _accepted(
        command,
        "III_PX4_CAPTURE",
        "Created immutable local metadata for the complete PX4 snapshot.",
        result,
    )


def list_captures(args: argparse.Namespace) -> CommandResult:
    command = "iii px4 params list"
    try:
        captures = _store(args).list_captures()
    except Exception as exc:
        return _rejected(command, "III_PX4_LIST_REJECTED", exc)
    return _accepted(
        command,
        "III_PX4_LIST",
        f"Found {len(captures)} verified PX4 captures.",
        {"schema": "iii.px4-capture-list/v1", "captures": captures},
    )


def show(args: argparse.Namespace) -> CommandResult:
    command = "iii px4 params show"
    try:
        capture_value = _store(args).load_capture(args.capture_id)
    except Exception as exc:
        return _rejected(command, "III_PX4_SHOW_REJECTED", exc)
    return _accepted(
        command,
        "III_PX4_SHOW",
        "Authenticated the immutable PX4 capture.",
        capture_value,
    )


def diff(args: argparse.Namespace) -> CommandResult:
    command = "iii px4 params diff"
    try:
        result = _store(args).diff_snapshots(args.left, args.right)
    except Exception as exc:
        return _rejected(command, "III_PX4_DIFF_REJECTED", exc)
    return _accepted(
        command,
        "III_PX4_DIFF",
        f"Compared PX4 snapshots with {len(result['changes'])} changed keys.",
        result,
    )


def export_preflight(args: argparse.Namespace) -> dict[str, Any]:
    capture_value = _store(args).load_capture(args.capture_id)
    destination = Path(args.destination).expanduser().resolve()
    if destination.exists() or destination.is_symlink():
        raise ValueError("PX4 export destination already exists")
    return {
        "schema": "iii.px4-export-preflight/v1",
        "capture_id": capture_value["capture_id"],
        "destination": str(destination),
        "mutations": ["create one portable PX4 capture export"],
    }


def export(args: argparse.Namespace) -> CommandResult:
    command = "iii px4 params export"
    try:
        retained = getattr(args, "_iii_retained_plan", None)
        if not isinstance(retained, Mapping) or export_preflight(args) != retained.get(
            "preflight"
        ):
            raise ValueError("PX4 export inputs changed after planning")
        result = _store(args).export_capture(
            args.capture_id, Path(args.destination).expanduser().resolve()
        )
    except Exception as exc:
        return _rejected(command, "III_PX4_EXPORT_REJECTED", exc)
    return _accepted(
        command,
        "III_PX4_EXPORT",
        "Created a portable checksummed PX4 capture export.",
        result,
    )


def import_preflight(args: argparse.Namespace) -> dict[str, Any]:
    source = Path(args.source).expanduser().resolve()
    if source.is_symlink() or not source.is_file():
        raise ValueError("PX4 capture import source is missing or linked")
    return {
        "schema": "iii.px4-import-preflight/v1",
        "source": str(source),
        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "mutations": ["deduplicate verified PX4 snapshot and capture metadata"],
    }


def import_capture(args: argparse.Namespace) -> CommandResult:
    command = "iii px4 params import"
    try:
        retained = getattr(args, "_iii_retained_plan", None)
        if not isinstance(retained, Mapping) or import_preflight(args) != retained.get(
            "preflight"
        ):
            raise ValueError("PX4 import source changed after planning")
        result = _store(args).import_capture(Path(args.source).expanduser().resolve())
    except Exception as exc:
        return _rejected(command, "III_PX4_IMPORT_REJECTED", exc)
    return _accepted(
        command,
        "III_PX4_IMPORT",
        "Verified and deduplicated the portable PX4 capture.",
        result,
    )


def _git(args: argparse.Namespace, *command: str) -> str:
    workspace = _workspace(args)
    if workspace is None:
        raise ValueError("PX4 promotion requires a workspace checkout")
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
    from iii_deployment.contracts import ContractRegistry, content_identity
    from iii_deployment.px4_release import (
        load_dds_contract,
        validate_release_inputs,
    )
    from iii_deployment.px4_network import load_network_baseline

    store = _store(args)
    accepted_keys = _promotion_keys(store, args)
    promoted = store.promoted_manifest(args.capture_id, accepted_keys=accepted_keys)
    profile = promoted["profile"]
    workspace = _workspace(args)
    if workspace is None:
        raise ValueError("PX4 promotion requires a workspace checkout")
    source = workspace / f"deployment/px4/{profile}.json"
    firmware_source = workspace / "deployment/px4/firmware.json"
    try:
        source_manifest = json.loads(source.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read PX4 feature source: {exc}") from exc
    if source_manifest.get("manifest_id") != store.manifest(profile)["manifest_id"]:
        raise ValueError("PX4 feature source differs from the active release baseline")
    branch = _git(args, "branch", "--show-current")
    if (
        not branch
        or branch in {"develop", "main", "release"}
        or branch.startswith("promote/")
    ):
        raise ValueError("PX4 promotion requires a normal feature branch")
    if _git(args, "status", "--porcelain", "--", str(source), str(firmware_source)):
        raise ValueError("PX4 manifest or firmware contract source is already modified")
    registry = ContractRegistry(_resource_root(args, "schemas"))
    firmware = json.loads(firmware_source.read_text(encoding="utf-8"))
    firmware["parameter_manifest_id"] = promoted["manifest_id"]
    firmware["spec_id"] = content_identity(
        {key: value for key, value in firmware.items() if key != "spec_id"}
    )
    dds = load_dds_contract(workspace / "deployment/px4/dds-topics.json", registry)
    network = load_network_baseline(
        workspace / "deployment/px4/network-baseline.json",
        schema_root=_resource_root(args, "schemas"),
    )
    registry.validate("px4-firmware-spec", firmware)
    validate_release_inputs(
        spec=firmware,
        dds=dds,
        network=network,
        parameters=promoted,
        registry=registry,
    )
    return {
        "schema": "iii.px4-promote-preflight/v1",
        "branch": branch,
        "head": _git(args, "rev-parse", "HEAD"),
        "capture_id": args.capture_id,
        "accepted_keys": accepted_keys,
        "all_defaults": bool(args.all_defaults),
        "source": str(source),
        "firmware_source": str(firmware_source),
        "old_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "old_firmware_sha256": hashlib.sha256(firmware_source.read_bytes()).hexdigest(),
        "new_manifest_id": promoted["manifest_id"],
        "new_spec_id": firmware["spec_id"],
        "mutations": [
            f"write reviewed keys to deployment/px4/{profile}.json",
            "rebind deployment/px4/firmware.json to the promoted manifest",
        ],
    }


def _atomic_document(path: Path, value: Mapping[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.partial")
    if temporary.exists() or temporary.is_symlink() or path.is_symlink():
        raise ValueError("PX4 promotion path is unsafe")
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


def _promotion_keys(store: Any, args: argparse.Namespace) -> list[str]:
    if not args.all_defaults:
        return sorted(args.key or [])
    capture = store.load_capture(args.capture_id)
    snapshot = store.load_snapshot(capture["snapshot_id"])
    present = {item["name"] for item in snapshot["parameters"]}
    return sorted(
        item["name"]
        for item in store.manifest(snapshot["profile"])["parameters"]
        if item["classification"] != "calibration-identity"
        and item["name"] in present
    )


def promote(args: argparse.Namespace) -> CommandResult:
    command = "iii px4 params promote"
    try:
        retained = getattr(args, "_iii_retained_plan", None)
        if not isinstance(retained, Mapping) or promote_preflight(args) != retained.get(
            "preflight"
        ):
            raise ValueError("PX4 promotion source or Git state changed")
        store = _store(args)
        accepted_keys = _promotion_keys(store, args)
        promoted = store.promoted_manifest(args.capture_id, accepted_keys=accepted_keys)
        _atomic_document(Path(retained["preflight"]["source"]), promoted)
        firmware_path = Path(retained["preflight"]["firmware_source"])
        firmware = json.loads(firmware_path.read_text(encoding="utf-8"))
        firmware["parameter_manifest_id"] = promoted["manifest_id"]
        from iii_deployment.contracts import content_identity

        firmware["spec_id"] = content_identity(
            {key: value for key, value in firmware.items() if key != "spec_id"}
        )
        if firmware["spec_id"] != retained["preflight"]["new_spec_id"]:
            raise ValueError("PX4 firmware binding changed after planning")
        _atomic_document(firmware_path, firmware)
    except Exception as exc:
        return _rejected(command, "III_PX4_PROMOTE_REJECTED", exc)
    return _accepted(
        command,
        "III_PX4_PROMOTE",
        "Wrote only the reviewed PX4 keys to the feature-branch manifest.",
        {
            "schema": "iii.px4-promote-result/v1",
            "capture_id": args.capture_id,
            "profile": promoted["profile"],
            "manifest_id": promoted["manifest_id"],
            "spec_id": firmware["spec_id"],
            "accepted_keys": accepted_keys,
            "all_defaults": bool(args.all_defaults),
            "committed": False,
            "pushed": False,
        },
        profile=promoted["profile"],
    )


def release_prepare_preflight(args: argparse.Namespace) -> dict[str, Any]:
    from iii_deployment.contracts import ContractRegistry
    from iii_deployment.px4_release import load_firmware_spec

    release_directory = Path(args.release_directory).expanduser().resolve()
    destination = Path(args.destination).expanduser().resolve()
    build_record = release_directory / "px4-firmware-build.json"
    spec = load_firmware_spec(
        release_directory / "firmware.json",
        ContractRegistry(_resource_root(args, "schemas")),
    )
    firmware = release_directory / spec["build"]["artifact"]
    if not build_record.is_file() or build_record.is_symlink():
        raise ValueError("release directory lacks the PX4 firmware build record")
    if not firmware.is_file() or firmware.is_symlink():
        raise ValueError("release directory lacks the paired PX4 firmware")
    if destination.exists() or destination.is_symlink():
        raise ValueError("PX4 preparation destination must not already exist")
    return {
        "schema": "iii.px4-release-prepare-plan/v1",
        "release_directory": str(release_directory),
        "destination": str(destination),
        "spec_id": spec["spec_id"],
        "build_record_sha256": hashlib.sha256(build_record.read_bytes()).hexdigest(),
        "firmware_sha256": hashlib.sha256(firmware.read_bytes()).hexdigest(),
        "mutations": [f"create self-verifying PX4 release media at {destination}"],
    }


def release_prepare(args: argparse.Namespace) -> CommandResult:
    command = "iii px4 release prepare"
    try:
        retained = getattr(args, "_iii_retained_plan", None)
        current = release_prepare_preflight(args)
        if not isinstance(retained, Mapping) or retained.get("preflight") != current:
            raise ValueError("PX4 release preparation inputs changed after planning")
        from iii_deployment.px4_release import prepare_release_media

        release_directory = Path(current["release_directory"])
        root = release_directory
        spec = json.loads((root / "firmware.json").read_text(encoding="utf-8"))
        package = prepare_release_media(
            destination=Path(current["destination"]),
            firmware_path=release_directory / spec["build"]["artifact"],
            build_record_path=release_directory / "px4-firmware-build.json",
            resource_root=root,
            schema_root=_resource_root(args, "schemas"),
        )
    except Exception as exc:
        return _rejected(command, "III_PX4_RELEASE_PREPARE_REJECTED", exc)
    return _accepted(
        command,
        "III_PX4_RELEASE_PREPARED",
        "Prepared the paired PX4 USB-update payload and microSD recovery files without touching the flight controller.",
        package,
    )


def release_audit(args: argparse.Namespace) -> CommandResult:
    command = "iii px4 release audit"
    try:
        from .operation import operation_id as new_operation_id
        from .ssh_manager import SSHManager

        # The receiver protocol correlates every request with an operation ID,
        # including read-only requests. The universal runner intentionally does
        # not retain plans for read-only commands, so supply a correlation-only
        # identifier here rather than asking operators for mutation controls.
        operation_id = getattr(args, "_iii_operation_id", None) or new_operation_id()
        result = SSHManager().px4_audit(
            release_id=args.release_id, operation_id=operation_id
        )
        audit = result.get("audit")
        if not isinstance(audit, Mapping):
            raise ValueError("receiver returned malformed PX4 audit evidence")
    except Exception as exc:
        return _rejected(command, "III_PX4_RELEASE_AUDIT_REJECTED", exc)
    return _accepted(
        command,
        "III_PX4_RELEASE_MATCH" if audit.get("healthy") else "III_PX4_RELEASE_REQUIRED",
        "PX4 matches the paired release." if audit.get("healthy") else "PX4 must be updated before III activation.",
        result,
        outcome=Outcome.SUCCESS if audit.get("healthy") else Outcome.REJECTED,
        findings=tuple(
            Finding(str(item.get("code", "PX4_MISMATCH")), str(item.get("detail", "PX4 release mismatch")))
            for item in audit.get("findings", [])
            if isinstance(item, Mapping)
        ),
    )


def initialize(parser: argparse.ArgumentParser) -> None:
    commands = parser.add_subparsers(dest="px4_command")
    release = commands.add_parser("release", help="prepare and audit the paired PX4 release")
    release_leaves = release.add_subparsers(dest="px4_release_command")
    release_prepare_parser = release_leaves.add_parser(
        "prepare", help="create exact USB-update payload with microSD recovery files"
    )
    release_prepare_parser.add_argument("--release-directory", required=True)
    release_prepare_parser.add_argument("--destination", required=True)
    release_prepare_parser.set_defaults(
        func=release_prepare,
        _iii_mutating=True,
        _iii_plan_provider=release_prepare_preflight,
    )
    release_audit_parser = release_leaves.add_parser(
        "audit", help="run the receiver-owned zero-write Ethernet audit"
    )
    release_audit_parser.add_argument("--release-id", required=True)
    release_audit_parser.set_defaults(func=release_audit, _iii_mutating=False)

    params = commands.add_parser("params", help="manage complete PX4 parameter sets")
    leaves = params.add_subparsers(dest="px4_params_command")

    pull_parser = leaves.add_parser(
        "pull", help="capture a complete disarmed inventory"
    )
    pull_parser.add_argument("--profile", choices=("real", "sim"), required=True)
    pull_parser.add_argument(
        "--release-id",
        help="exact staged release used for receiver-owned real PX4 Ethernet capture",
    )
    pull_parser.set_defaults(func=pull, _iii_mutating=False)

    plan_parser = leaves.add_parser("plan", help="retain an exact per-key write plan")
    plan_parser.add_argument("--profile", choices=("real", "sim"), required=True)
    plan_parser.add_argument("--snapshot", required=True)
    plan_parser.add_argument("--key", action="append", default=[])
    plan_parser.set_defaults(func=plan, _iii_mutating=False)

    apply_parser = leaves.add_parser("apply", help="apply an exact retained write plan")
    apply_parser.add_argument("--plan-id", required=True)
    apply_parser.add_argument("--key", action="append", required=True)
    apply_parser.set_defaults(
        func=apply, _iii_mutating=True, _iii_plan_provider=apply_preflight
    )

    verify_parser = leaves.add_parser(
        "verify", help="verify a plan by complete readback"
    )
    verify_parser.add_argument("--plan-id", required=True)
    verify_parser.set_defaults(func=verify, _iii_mutating=False)

    capture_parser = leaves.add_parser("capture", help="name an immutable PX4 snapshot")
    capture_parser.add_argument("--snapshot", required=True)
    capture_parser.add_argument("--name", required=True)
    capture_parser.add_argument("--description", required=True)
    capture_parser.set_defaults(
        func=capture, _iii_mutating=True, _iii_plan_provider=capture_preflight
    )

    list_parser = leaves.add_parser("list", help="list verified local PX4 captures")
    list_parser.set_defaults(func=list_captures, _iii_mutating=False)
    show_parser = leaves.add_parser("show", help="show a verified PX4 capture")
    show_parser.add_argument("--capture-id", required=True)
    show_parser.set_defaults(func=show, _iii_mutating=False)
    diff_parser = leaves.add_parser("diff", help="compare complete PX4 snapshots")
    diff_parser.add_argument("--left", required=True)
    diff_parser.add_argument("--right", required=True)
    diff_parser.set_defaults(func=diff, _iii_mutating=False)

    export_parser = leaves.add_parser("export", help="export a portable PX4 capture")
    export_parser.add_argument("--capture-id", required=True)
    export_parser.add_argument("--destination", required=True)
    export_parser.set_defaults(
        func=export, _iii_mutating=True, _iii_plan_provider=export_preflight
    )
    import_parser = leaves.add_parser("import", help="import a portable PX4 capture")
    import_parser.add_argument("--source", required=True)
    import_parser.set_defaults(
        func=import_capture,
        _iii_mutating=True,
        _iii_plan_provider=import_preflight,
    )
    promote_parser = leaves.add_parser(
        "promote", help="write reviewed capture keys to a feature-branch manifest"
    )
    promote_parser.add_argument("--capture-id", required=True)
    promote_selection = promote_parser.add_mutually_exclusive_group(required=True)
    promote_selection.add_argument("--key", action="append")
    promote_selection.add_argument(
        "--all-defaults",
        action="store_true",
        help="promote every non-calibration manifest value from the complete capture",
    )
    promote_parser.set_defaults(
        func=promote, _iii_mutating=True, _iii_plan_provider=promote_preflight
    )
