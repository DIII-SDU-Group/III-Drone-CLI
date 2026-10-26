"""Bring a profile's living parameters in line with the tracked default.

A runtime keeps its own living parameter set per profile, and a start only
adds new parameters to it: values that already exist are kept, so a tuned
aircraft is never changed by a deployment. ``iii config sync`` shows how the
living set differs from the tracked default and, with ``--mode defaults``,
writes the default's values into it.

Nothing is ever overwritten without a copy: before the living default is
rewritten, the parameter set it replaces is saved as a snapshot beside it,
where the operator interface can load it again.

The command runs on the runtime host against that host's living tree. From a
ground computer it is routed to the Pi, and the checkout's tracked default
travels with it, so an edited default needs no build to be applied.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Mapping, Sequence

import yaml

from .result import CommandResult, Finding, NextAction, Outcome

SCHEMA = "iii.config-sync/v1"
MODES = ("keep", "defaults")
PROFILES = ("sim", "hil", "real", "opti_track")
DEFAULT_REFERENCE = "tracked/default.yaml"
SNAPSHOT_PREFIX = "pre_sync_"
SOURCE_PARAMETER_SETS = "src/III-Drone-Configuration/config/parameter_sets"
SOURCE_PROFILES = "src/III-Drone-Configuration/config/configuration_contract/profiles.json"
ROS_SCOPE = "/**"
COMMAND = "iii config sync"


class SyncError(ValueError):
    """The parameters cannot be compared or synchronized as requested."""


def _environment(args: argparse.Namespace) -> Mapping[str, str]:
    return getattr(args, "_iii_environment", os.environ)


def _workspace(environment: Mapping[str, str]) -> Path | None:
    configured = environment.get("WORKSPACE_DIR")
    candidates = [Path(configured)] if configured else []
    candidates.extend((Path.cwd(), *Path.cwd().parents))
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if (resolved / SOURCE_PARAMETER_SETS).is_dir():
            return resolved
    return None


def _contract_root(environment: Mapping[str, str]) -> Path:
    configured = environment.get("III_CONFIGURATION_CONTRACT_ROOT")
    if configured:
        return Path(configured).expanduser().absolute()
    from iii_drone_configuration import resolve_installed_contract_root

    return resolve_installed_contract_root()


def _living_root(environment: Mapping[str, str]) -> Path:
    base = Path(environment.get("CONFIG_BASE_DIR") or "~/.config").expanduser()
    return Path(environment.get("III_CONFIGURATION_STATE_ROOT") or base / "iii_drone").absolute()


def _set_path(environment: Mapping[str, str], scope: str, reference: str) -> Path:
    from iii_drone_configuration import schema_utils

    normalized = schema_utils.normalize_parameter_set_reference(reference, default_subdir="tracked")
    return _living_root(environment) / "parameter_sets" / scope / normalized


def _active_reference(environment: Mapping[str, str], scope: str) -> str:
    from iii_drone_configuration import schema_utils

    selector = _living_root(environment) / "profiles" / f"{scope}.yaml"
    if selector.is_file():
        reference = (yaml.safe_load(selector.read_text(encoding="utf-8")) or {}).get(
            schema_utils.PROFILE_SELECTOR_ACTIVE_FIELD
        )
        if isinstance(reference, str) and reference:
            return schema_utils.normalize_parameter_set_reference(
                reference, default_subdir="tracked"
            )
    return DEFAULT_REFERENCE


def parameter_family(profile: str, workspace: Path) -> str:
    """The parameter family (sim or real) a runtime profile draws its default from."""

    import json

    document = json.loads((workspace / SOURCE_PROFILES).read_text(encoding="utf-8"))
    for entry in document.get("profiles", []):
        if entry.get("runtime_profile") == profile:
            return str(entry["parameter_profile"])
    raise SyncError(f"runtime profile {profile} is not declared in {SOURCE_PROFILES}")


def source_default_path(profile: str, workspace: Path) -> Path:
    family = parameter_family(profile, workspace)
    return workspace / SOURCE_PARAMETER_SETS / family / "tracked" / "default.yaml"


def _parameters(document: Any, label: str) -> dict[str, Any]:
    scope = document.get(ROS_SCOPE) if isinstance(document, dict) else None
    values = scope.get("ros__parameters") if isinstance(scope, dict) else None
    if not isinstance(values, dict):
        raise SyncError(f"{label} has no '{ROS_SCOPE}' ros__parameters mapping")
    return dict(values)


def _load(path: Path, label: str) -> dict[str, Any]:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SyncError(f"{label} {path} cannot be read: {exc}") from None
    _parameters(document, label)
    return document


def _default_document(
    args: argparse.Namespace, profile: str
) -> tuple[dict[str, Any], str]:
    """The tracked default to compare with, and where it came from."""

    cached = getattr(args, "_iii_sync_default", None)
    if cached is not None:
        return cached
    if getattr(args, "defaults_stdin", False):
        stream = getattr(args, "_iii_defaults_stream", None) or sys.stdin
        try:
            document = yaml.safe_load(stream.read())
        except yaml.YAMLError as exc:
            raise SyncError(f"the tracked default on standard input is not YAML: {exc}") from None
        _parameters(document, "the tracked default on standard input")
        result = (document, "the sender's checkout")
    else:
        workspace = _workspace(_environment(args))
        source = source_default_path(profile, workspace) if workspace is not None else None
        if source is not None and source.is_file():
            result = (_load(source, "tracked default"), str(source))
        else:
            from iii_drone_configuration import load_installed_contract

            root = _contract_root(_environment(args))
            contract = load_installed_contract(root).contract
            family = contract.profile(profile).parameter_profile
            installed = root / contract.default_set(family).path
            result = (_load(installed, "installed tracked default"), str(installed))
    setattr(args, "_iii_sync_default", result)
    return result


def _installed_schema(environment: Mapping[str, str]) -> dict[str, dict[str, Any]]:
    from iii_drone_configuration import load_installed_contract, reconciliation

    root = _contract_root(environment)
    load_installed_contract(root)
    manifest = yaml.safe_load(
        (root / "schema" / "parameter_manifest.yaml").read_text(encoding="utf-8")
    )
    return reconciliation._flatten_schema(manifest)


def _selector_scope(environment: Mapping[str, str], profile: str) -> str:
    from iii_drone_configuration import load_installed_contract

    descriptor = load_installed_contract(_contract_root(environment)).contract.profile(profile)
    if not descriptor.bootable:
        raise SyncError(f"runtime profile {profile} is reserved and has no living parameters")
    return descriptor.selector_scope


def stack_running() -> bool:
    """Whether the configuration server, and so the system, is running here.

    The server holds the living values in memory and writes them back, so the
    living set must not be rewritten underneath it.
    """

    completed = subprocess.run(
        ["pgrep", "-f", "configuration_server"], check=False, capture_output=True, text=True
    )
    own = {str(os.getpid()), str(os.getppid())}
    return any(pid.strip() and pid.strip() not in own for pid in completed.stdout.splitlines())


def _same(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return float(left) == float(right)
    return left == right


def evaluate(
    profile: str,
    default_document: Mapping[str, Any],
    *,
    mode: str,
    parameters: Sequence[str] = (),
    environment: Mapping[str, str] = os.environ,
) -> dict[str, Any]:
    """Compare the living set with the default and say what the mode would write."""

    if mode not in MODES:
        raise SyncError(f"unknown mode {mode!r}; use keep or defaults")
    scope = _selector_scope(environment, profile)
    reference = _active_reference(environment, scope)
    active = _set_path(environment, scope, reference)
    target = _set_path(environment, scope, DEFAULT_REFERENCE)
    defaults = _parameters(default_document, "the tracked default")
    schema = _installed_schema(environment)
    living_exists = active.is_file()
    living = _parameters(_load(active, "living parameter set"), "living parameter set") if living_exists else {}

    requested = list(dict.fromkeys(parameters))
    unknown_requested = sorted(name for name in requested if name not in defaults)
    if unknown_requested:
        raise SyncError(
            "not in the tracked default: " + ", ".join(unknown_requested)
        )
    # A default newer than the installed schema names parameters this runtime
    # does not know yet; they arrive with a build, not with a parameter sync.
    not_installed = sorted(name for name in defaults if name not in schema)
    blocked = sorted(name for name in requested if name in not_installed)
    if blocked:
        raise SyncError(
            "not in the runtime's installed parameter schema (deploy with --build first): "
            + ", ".join(blocked)
        )
    comparable = [name for name in sorted(defaults) if name in schema]
    drift = [
        {"name": name, "living": living.get(name), "default": defaults[name]}
        for name in comparable
        if name not in living or not _same(living[name], defaults[name])
    ]
    selected = set(requested) if requested else set(comparable)
    changes = [item for item in drift if item["name"] in selected] if mode == "defaults" else []
    return {
        "schema": SCHEMA,
        "profile": profile,
        "selector_scope": scope,
        "mode": mode,
        "parameters": requested,
        "active_set": reference,
        "active_path": str(active),
        "living_exists": living_exists,
        "target_set": DEFAULT_REFERENCE,
        "target_path": str(target),
        "drift": drift,
        "changes": changes,
        "not_in_installed_schema": not_installed,
        "selects_default_set": mode == "defaults" and bool(changes) and reference != DEFAULT_REFERENCE,
    }


def show(plan: Mapping[str, Any], source: str, *, stream=None) -> None:
    """Show the comparison before the runner asks for confirmation."""

    stream = stream or sys.stderr
    drift = list(plan["drift"])
    changes = {item["name"] for item in plan["changes"]}
    profile = plan["profile"]
    lines: list[str] = []
    if not plan["living_exists"]:
        lines.append(
            f"Profile {profile} has no living parameter set yet; the next system start "
            "seeds it from the installed tracked default."
        )
    elif not drift:
        lines.append(f"Living {profile} parameters match the tracked default ({source}).")
    else:
        width = max(len(item["name"]) for item in drift)
        lines.append(
            f"Living {profile} parameters ({plan['active_set']}) that differ from the "
            f"tracked default ({source}); living -> default:"
        )
        for item in drift:
            mark = "write" if item["name"] in changes else "keep "
            living = "absent" if item["living"] is None else item["living"]
            lines.append(f"  {mark}  {item['name']:<{width}}  {living} -> {item['default']}")
        if plan["mode"] == "keep":
            lines.append("Mode keep: nothing is written. Use --mode defaults to write them.")
        else:
            lines.append(
                f"Mode defaults: {len(changes)} of {len(drift)} will be written to "
                f"{plan['target_set']}; the set it replaces is saved as a snapshot first."
            )
    if plan["not_in_installed_schema"]:
        lines.append(
            "Not in this runtime's installed schema, left out (deploy with --build): "
            + ", ".join(plan["not_in_installed_schema"])
        )
    print("\n".join(lines), file=stream, flush=True)


def _profile(args: argparse.Namespace) -> str:
    profile = getattr(args, "profile", None) or _environment(args).get("III_SYSTEM_PROFILE")
    if profile not in PROFILES:
        raise SyncError("select the runtime profile with --profile sim|hil|real|opti_track")
    return profile


def plan(args: argparse.Namespace) -> dict[str, Any]:
    profile = _profile(args)
    document, source = _default_document(args, profile)
    result = evaluate(
        profile,
        document,
        mode=args.mode,
        parameters=getattr(args, "parameter", None) or (),
        environment=_environment(args),
    )
    result["default_source"] = source
    show(result, source)
    return result


def _snapshot(path: Path, directory: Path, *, now: datetime) -> Path:
    """Copy a parameter set into the snapshot directory without replacing anything."""

    directory.mkdir(parents=True, exist_ok=True)
    stamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    data = path.read_bytes()
    for attempt in range(100):
        suffix = "" if attempt == 0 else f"_{attempt}"
        candidate = directory / f"{SNAPSHOT_PREFIX}{stamp}{suffix}.yaml"
        try:
            descriptor = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
        except FileExistsError:
            continue
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        return candidate
    raise SyncError(f"no free snapshot name under {directory}")


def write_defaults(
    plan_: Mapping[str, Any],
    default_document: Mapping[str, Any],
    *,
    now: datetime | None = None,
    environment: Mapping[str, str] = os.environ,
) -> dict[str, Any]:
    """Snapshot what is replaced, then write the selected defaults and select the set."""

    from iii_drone_configuration import reconciliation, schema_utils

    scope = plan_["selector_scope"]
    active = Path(plan_["active_path"])
    target = Path(plan_["target_path"])
    schema = _installed_schema(environment)
    defaults = _parameters(default_document, "the tracked default")
    living_document = _load(active, "living parameter set")
    values = _parameters(living_document, "living parameter set")
    for item in plan_["changes"]:
        values[item["name"]] = defaults[item["name"]]
    valid, reasons = reconciliation._validate_candidates(values, schema)
    if not valid:
        raise SyncError("the resulting parameter set is invalid: " + "; ".join(reasons))

    # Never overwrite: the living default this writes over is copied first.
    # An active set other than the default is itself left in place.
    snapshot_directory = target.parent.parent / "snapshots"
    snapshots = (
        [str(_snapshot(target, snapshot_directory, now=now or datetime.now(timezone.utc)))]
        if target.is_file()
        else []
    )

    # A complete sync also takes the default's non-parameter sections (device
    # paths and the like); a partial one keeps the living document's.
    base = living_document if plan_["parameters"] else dict(default_document)
    document = {key: value for key, value in base.items() if key != ROS_SCOPE}
    document = {ROS_SCOPE: {"ros__parameters": dict(sorted(values.items()))}, **document}
    schema_utils.save_parameter_file(target, document)
    if plan_["active_set"] != DEFAULT_REFERENCE:
        schema_utils.save_parameter_file(
            _living_root(environment) / "profiles" / f"{scope}.yaml",
            {"version": 1, schema_utils.PROFILE_SELECTOR_ACTIVE_FIELD: DEFAULT_REFERENCE},
        )
    return {
        "written": [item["name"] for item in plan_["changes"]],
        "snapshots": snapshots,
        "set": DEFAULT_REFERENCE,
        "path": str(target),
    }


def _result(
    outcome: Outcome, code: str, summary: str, payload: Mapping[str, Any], detail: str | None = None
) -> CommandResult:
    profile = payload.get("profile")
    return CommandResult(
        command=COMMAND,
        outcome=outcome,
        summary=summary,
        code=code,
        target=profile,
        profile=profile,
        findings=(Finding(code, detail),) if detail else (),
        payload_schema=SCHEMA,
        payload=payload,
        next_actions=(
            NextAction(
                ("iii", "config", "sync", "--profile", str(profile), "--dry-run"),
                "Compare the living parameters with the tracked default.",
            ),
        ),
    )


def apply(args: argparse.Namespace) -> CommandResult:
    payload = dict(args._iii_retained_plan["preflight"])
    profile = payload["profile"]
    try:
        document, _source = _default_document(args, profile)
        current = evaluate(
            profile,
            document,
            mode=payload["mode"],
            parameters=payload["parameters"],
            environment=_environment(args),
        )
        if current["changes"] != payload["changes"]:
            raise SyncError("the living parameters changed after planning; run the command again")
        if payload["mode"] == "keep" or not payload["changes"]:
            return _result(
                Outcome.SUCCESS,
                "III_CONFIG_SYNC_UNCHANGED",
                f"{len(payload['drift'])} living {profile} parameters differ from the tracked "
                "default; nothing was written.",
                payload,
            )
        if not payload["living_exists"]:
            raise SyncError(
                f"profile {profile} has no living parameter set; start the system once to seed it"
            )
        if stack_running():
            raise SyncError(
                "the system is running; shut it down first (iii system shutdown), because the "
                "configuration server would write its own values back"
            )
        payload["result"] = write_defaults(payload, document, environment=_environment(args))
    except Exception as exc:  # noqa: BLE001 - every refusal is a typed result
        return _result(
            Outcome.REJECTED,
            "III_CONFIG_SYNC_REJECTED",
            "The parameters were not synchronized.",
            payload,
            str(exc),
        )
    written = payload["result"]["written"]
    snapshots = payload["result"]["snapshots"]
    kept = (
        f"the replaced set is saved as snapshots/{Path(snapshots[0]).name}"
        if snapshots
        else f"the previously active set {payload['active_set']} is left in place"
    )
    return _result(
        Outcome.SUCCESS,
        "III_CONFIG_SYNC_APPLIED",
        f"{len(written)} {profile} parameters were set to the tracked default; {kept}. "
        "They take effect at the next system start.",
        payload,
    )


def routed(route: Any, argv: Sequence[str], args: argparse.Namespace, *, output: str,
           stdout_stream: Any, stderr_stream: Any) -> tuple[Any, Any]:
    """Plan provider and action for a sync that a ground computer routes to the Pi.

    The checkout's tracked default is sent on the remote command's standard
    input, so the Pi compares with it and nothing but the living set changes.
    """

    def default_text() -> str:
        workspace = _workspace(_environment(args))
        if workspace is None:
            raise SyncError("run this command from the III workspace (no tracked default found)")
        path = source_default_path(_profile(args), workspace)
        _load(path, "tracked default")
        return path.read_text(encoding="utf-8")

    def provider(_args: argparse.Namespace) -> dict[str, Any]:
        result = route.execute(
            argv,
            output="json",
            mutating=False,
            extra_arguments=("--defaults-stdin", "--dry-run"),
            input_text=default_text(),
        )
        payload = getattr(result, "payload", {}) or {}
        remote = payload.get("runtime_result") or {}
        preview = ((remote.get("payload") or {}).get("plan") or {}).get("preflight")
        if not isinstance(preview, dict):
            detail = remote.get("summary") or payload.get("stderr") or "no plan was returned"
            findings = [str(item.get("detail")) for item in remote.get("findings") or []]
            raise SyncError("; ".join([str(detail).strip(), *findings]))
        show(preview, str(preview.get("default_source")), stream=stderr_stream)
        return {**route.plan, "runtime_plan": preview}

    def action(_args: argparse.Namespace) -> Any:
        return route.execute(
            argv,
            output=output,
            mutating=True,
            extra_arguments=("--defaults-stdin",),
            input_text=default_text(),
            stdout_stream=stdout_stream,
        )

    return provider, action


def initialize(parser: argparse.ArgumentParser) -> None:
    parser.description = (
        "Compare a profile's living parameters with the tracked default. --mode keep "
        "(the default) writes nothing: a start already adds new parameters and keeps "
        "tuned values. --mode defaults writes the tracked default's values into the "
        "living default set and selects it, after saving the set it replaces as a "
        "snapshot. Run from the III workspace; from a ground computer the command "
        "runs on the Pi with this checkout's tracked default. The system must be "
        "shut down to write. --dry-run only shows the difference."
    )
    parser.add_argument("--profile", choices=PROFILES, help="runtime profile (default: the host's)")
    parser.add_argument("--mode", choices=MODES, default="keep")
    parser.add_argument(
        "--parameter",
        action="append",
        default=[],
        metavar="NAME",
        help="limit --mode defaults to this parameter (repeatable; default: every parameter)",
    )
    parser.add_argument(
        "--defaults-stdin",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.set_defaults(func=apply, _iii_mutating=True, _iii_plan_provider=plan)
