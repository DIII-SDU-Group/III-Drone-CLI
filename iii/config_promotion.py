"""Compare and explicitly promote sealed tuning captures into tracked defaults."""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Mapping, Sequence

import yaml

from .config_capture import _capture_root, _verified, canonical_json, content_identity
from .result import CommandResult, Finding, NextAction, Outcome


SHA256 = re.compile(r"^[a-f0-9]{64}$")
GIT_OBJECT_ID = re.compile(r"^(?:[a-f0-9]{40}|[a-f0-9]{64})$")
PROTECTED_BRANCHES = {"develop", "main", "release"}
PROMOTION_SCHEMA = "iii.configuration-promotion-plan/v1"


def _environment(args: argparse.Namespace) -> Mapping[str, str]:
    return getattr(args, "_iii_environment", os.environ)


def _workspace(args: argparse.Namespace) -> Path:
    configured = _environment(args).get("WORKSPACE_DIR")
    candidates = [Path(configured)] if configured else []
    candidates.extend((Path.cwd(), *Path.cwd().parents))
    for candidate in candidates:
        resolved = candidate.expanduser().absolute()
        if (resolved / "deps/submodule-lock.txt").is_file() and (
            resolved / "src/III-Drone-Configuration/.git"
        ).exists():
            return resolved
    raise ValueError("configuration promotion requires an III workspace checkout")


def _configuration_root(args: argparse.Namespace) -> Path:
    configured = _environment(args).get("III_CONFIGURATION_SOURCE_ROOT")
    path = (
        (
            Path(configured)
            if configured
            else _workspace(args) / "src/III-Drone-Configuration"
        )
        .expanduser()
        .absolute()
    )
    if not (path / "config/configuration_contract/package-manifest.json").is_file():
        raise ValueError("configuration source contract is unavailable")
    return path


def _run(
    cwd: Path, argv: Sequence[str], *, check: bool = True
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        list(argv),
        cwd=cwd,
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if check and completed.returncode:
        raise ValueError(
            completed.stderr.strip()
            or completed.stdout.strip()
            or f"command failed: {' '.join(argv)}"
        )
    return completed


def _git(cwd: Path, *argv: str) -> str:
    return _run(cwd, ("git", *argv)).stdout.strip()


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _read_regular(path: Path, *, label: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} is missing or linked")
    return path.read_bytes()


def _manifest_identity(value: Mapping[str, Any]) -> str:
    return content_identity(
        {key: item for key, item in value.items() if key != "manifest_id"}
    )


def _parameter_values(raw: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = yaml.safe_load(raw) or {}
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ValueError(f"{label} is invalid YAML: {exc}") from exc
    if not isinstance(value, dict) or "/**" not in value:
        raise ValueError(f"{label} does not have the fixed ROS parameter shape")

    def validate_parameter_tree(document: Mapping[str, Any]) -> None:
        if "ros__parameters" in document:
            if set(document) != {"ros__parameters"} or not isinstance(
                document["ros__parameters"], dict
            ):
                raise ValueError(f"{label} does not have the fixed ROS parameter shape")
            return
        if not document:
            raise ValueError(f"{label} does not have the fixed ROS parameter shape")
        for node_name, nested in document.items():
            if (
                not isinstance(node_name, str)
                or not node_name
                or not isinstance(nested, dict)
            ):
                raise ValueError(f"{label} does not have the fixed ROS parameter shape")
            validate_parameter_tree(nested)

    validate_parameter_tree(value)
    values = value["/**"]["ros__parameters"]
    canonical_json(value)
    if any(not isinstance(name, str) or not name.startswith("/") for name in values):
        raise ValueError(f"{label} contains a malformed parameter name")
    return dict(values)


def _yaml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return json.dumps(value, allow_nan=False)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _replace_yaml_values(
    raw: bytes, *, old_values: Mapping[str, Any], replacements: Mapping[str, Any]
) -> bytes:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("tracked default is not UTF-8") from exc
    for name in sorted(replacements):
        if name not in old_values:
            raise ValueError(
                f"selected parameter is absent from the tracked default: {name}"
            )
        pattern = re.compile(rf"^(\s+){re.escape(name)}:\s.*$", re.MULTILINE)
        matches = list(pattern.finditer(text))
        if len(matches) != 1:
            raise ValueError(
                f"tracked default must contain exactly one scalar line for {name}"
            )
        indentation = matches[0].group(1)
        text = pattern.sub(
            f"{indentation}{name}: {_yaml_scalar(replacements[name])}", text, count=1
        )
    candidate = text.encode("utf-8")
    expected = {**old_values, **replacements}
    if _parameter_values(candidate, label="proposed tracked default") != expected:
        raise ValueError("minimal tracked-default rewrite changed unselected values")
    return candidate


def _rewrite_manifest(
    raw: bytes, *, profile: str, old_default_sha: str, new_default_sha: str
) -> tuple[bytes, str, str]:
    try:
        manifest = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"configuration package manifest is invalid: {exc}") from exc
    if not isinstance(manifest, dict) or set(manifest) != {
        "schema",
        "manifest_id",
        "package_version",
        "configuration_schema_version",
        "compatibility",
        "artifacts",
        "tracked_sets",
    }:
        raise ValueError("configuration package manifest fields are invalid")
    old_manifest_id = manifest.get("manifest_id")
    if (
        manifest.get("schema") != "iii.configuration-package/v1"
        or not isinstance(old_manifest_id, str)
        or old_manifest_id != _manifest_identity(manifest)
    ):
        raise ValueError("configuration package manifest identity is invalid")
    relative = f"tracked_defaults/{profile}/default.yaml"
    artifact_rows = [
        item
        for item in manifest.get("artifacts", [])
        if isinstance(item, dict) and item.get("path") == relative
    ]
    tracked_rows = [
        item
        for item in manifest.get("tracked_sets", [])
        if isinstance(item, dict)
        and item.get("profile") == profile
        and item.get("set_id") == "default"
        and item.get("default") is True
        and item.get("path") == relative
    ]
    if (
        len(artifact_rows) != 1
        or len(tracked_rows) != 1
        or artifact_rows[0].get("sha256") != old_default_sha
        or tracked_rows[0].get("sha256") != old_default_sha
    ):
        raise ValueError("tracked default and package manifest hashes differ")
    artifact_rows[0]["sha256"] = new_default_sha
    tracked_rows[0]["sha256"] = new_default_sha
    manifest["manifest_id"] = _manifest_identity(manifest)
    new_manifest_id = manifest["manifest_id"]
    text = raw.decode("utf-8")
    if text.count(old_default_sha) != 2 or text.count(old_manifest_id) != 1:
        raise ValueError("package manifest source cannot be minimally rewritten")
    candidate = text.replace(old_default_sha, new_default_sha).replace(
        old_manifest_id, new_manifest_id
    )
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ValueError("rewritten package manifest is invalid") from exc
    if parsed != manifest:
        raise ValueError("package manifest minimal rewrite changed unrelated content")
    return candidate.encode("utf-8"), old_manifest_id, new_manifest_id


def _branch(cwd: Path) -> str:
    value = _git(cwd, "branch", "--show-current")
    if (
        not value
        or value in PROTECTED_BRANCHES
        or value.startswith("codex/")
        or value.startswith("promote/")
    ):
        raise ValueError("configuration promotion requires a normal feature branch")
    return value


def _gitlink(workspace: Path) -> str:
    value = _git(workspace, "ls-files", "--stage", "--", "src/III-Drone-Configuration")
    fields = value.split()
    if (
        len(fields) < 4
        or fields[0] != "160000"
        or not GIT_OBJECT_ID.fullmatch(fields[1])
    ):
        raise ValueError(
            "workspace does not track the configuration repository as a gitlink"
        )
    return fields[1]


def _require_workspace_provenance(
    workspace: Path, *, workspace_id: str, current_head: str
) -> None:
    if not GIT_OBJECT_ID.fullmatch(workspace_id):
        raise ValueError("capture workspace provenance is not a commit identity")
    exists = _run(
        workspace,
        ("git", "cat-file", "-e", f"{workspace_id}^{{commit}}"),
        check=False,
    )
    ancestor = _run(
        workspace,
        ("git", "merge-base", "--is-ancestor", workspace_id, current_head),
        check=False,
    )
    if exists.returncode or ancestor.returncode:
        raise ValueError(
            "capture workspace provenance is not an ancestor of this feature branch"
        )


def _build_plan(args: argparse.Namespace, *, require_clean: bool) -> dict[str, Any]:
    workspace = _workspace(args)
    configuration = _configuration_root(args)
    workspace_branch = _branch(workspace)
    configuration_branch = _branch(configuration)
    if workspace_branch != configuration_branch:
        raise ValueError("workspace and configuration feature branches differ")
    workspace_head = _git(workspace, "rev-parse", "HEAD")
    configuration_head = _git(configuration, "rev-parse", "HEAD")
    if _gitlink(workspace) != configuration_head:
        raise ValueError("workspace gitlink and configuration HEAD differ")
    if require_clean:
        if _git(configuration, "status", "--porcelain"):
            raise ValueError("configuration repository must be clean before apply")
        workspace_dirty = _git(workspace, "status", "--porcelain")
        if workspace_dirty:
            raise ValueError(
                "workspace repository must be clean before coordinated apply"
            )

    capture, capture_path = _verified(_capture_root(args), args.capture_id)
    source = capture["source"]
    if source["runtime_profile"] != args.profile:
        raise ValueError("capture and requested promotion profiles differ")
    if source["release_id"] != args.release_id or not SHA256.fullmatch(args.release_id):
        raise ValueError("capture release provenance differs from the explicit release")
    _require_workspace_provenance(
        workspace,
        workspace_id=source["workspace_id"],
        current_head=workspace_head,
    )

    default_path = (
        configuration
        / "config"
        / "parameter_sets"
        / args.profile
        / "tracked"
        / "default.yaml"
    )
    manifest_path = (
        configuration / "config/configuration_contract/package-manifest.json"
    )
    default_raw = _read_regular(default_path, label="tracked default")
    manifest_raw = _read_regular(manifest_path, label="package manifest")
    current_values = _parameter_values(default_raw, label="tracked default")
    if source["manifest_id"] != json.loads(manifest_raw)["manifest_id"]:
        raise ValueError("capture manifest differs from the current source manifest")
    if source["baseline_values"] != current_values:
        raise ValueError(
            "tracked source changed since the field baseline; reconciliation is required"
        )
    capture_values = source["values"]
    if not isinstance(capture_values, dict):
        raise ValueError("capture values are invalid")
    changed_names = sorted(
        name
        for name in set(current_values) | set(capture_values)
        if (name in current_values) != (name in capture_values)
        or current_values.get(name) != capture_values.get(name)
    )
    selected = sorted(set(args.key))
    if not selected or len(selected) != len(args.key):
        raise ValueError("promotion requires unique explicit --key selections")
    if args.classification != "shared-tracked-default":
        raise ValueError("promotion requires --classification shared-tracked-default")
    rejected = sorted(
        name
        for name in changed_names
        if name not in current_values or name not in capture_values
    )
    invalid_selected = sorted(set(selected) - (set(changed_names) - set(rejected)))
    if invalid_selected:
        raise ValueError(
            "selected keys are unchanged, deprecated, removed, or unknown: "
            + ", ".join(invalid_selected)
        )
    replacements = {name: capture_values[name] for name in selected}
    proposed_default = _replace_yaml_values(
        default_raw, old_values=current_values, replacements=replacements
    )
    new_default_sha = _sha256(proposed_default)
    proposed_manifest, old_manifest_id, new_manifest_id = _rewrite_manifest(
        manifest_raw,
        profile=args.profile,
        old_default_sha=_sha256(default_raw),
        new_default_sha=new_default_sha,
    )
    if old_manifest_id != source["manifest_id"]:
        raise ValueError("capture and authenticated source manifest identities differ")
    classifications = [
        {
            "name": name,
            "baseline": current_values.get(name),
            "capture": capture_values.get(name),
            "classification": (
                "rejected"
                if name in rejected
                else (
                    "shared-tracked-default"
                    if name in selected
                    else "retained-capture-evidence"
                )
            ),
        }
        for name in changed_names
    ]
    preview = "".join(
        difflib.unified_diff(
            default_raw.decode("utf-8").splitlines(keepends=True),
            proposed_default.decode("utf-8").splitlines(keepends=True),
            fromfile=f"a/{default_path.relative_to(configuration)}",
            tofile=f"b/{default_path.relative_to(configuration)}",
        )
    )
    plan = {
        "schema": PROMOTION_SCHEMA,
        "plan_id": "",
        "capture_id": capture["capture_id"],
        "capture_path": str(capture_path),
        "profile": args.profile,
        "classification": args.classification,
        "release_id": source["release_id"],
        "workspace_provenance": source["workspace_id"],
        "source_manifest_id": old_manifest_id,
        "new_manifest_id": new_manifest_id,
        "baseline_id": source["baseline_id"],
        "feature_branch": workspace_branch,
        "base_branch": args.base,
        "workspace_old_head": workspace_head,
        "configuration_old_head": configuration_head,
        "default_path": str(default_path),
        "manifest_path": str(manifest_path),
        "old_default_sha256": _sha256(default_raw),
        "new_default_sha256": new_default_sha,
        "old_manifest_sha256": _sha256(manifest_raw),
        "new_manifest_sha256": _sha256(proposed_manifest),
        "selected_keys": selected,
        "classifications": classifications,
        "rejected_keys": rejected,
        "diff": preview,
    }
    plan["plan_id"] = content_identity(
        {key: item for key, item in plan.items() if key != "plan_id"}
    )
    return plan


def _accepted(
    command: str,
    code: str,
    summary: str,
    payload: Mapping[str, Any],
    *,
    profile: str,
    next_actions: Sequence[NextAction] = (),
) -> CommandResult:
    return CommandResult(
        command=command,
        outcome=Outcome.SUCCESS,
        summary=summary,
        code=code,
        profile=profile,
        release_id=(
            str(payload.get("release_id")) if payload.get("release_id") else None
        ),
        payload_schema=str(
            payload.get("schema", "iii.configuration-promotion-result/v1")
        ),
        payload=payload,
        next_actions=tuple(next_actions),
        terminal_reason="Only explicitly classified keys were written to the profile-scoped feature-branch default.",
    )


def _rejected(command: str, code: str, exc: Exception) -> CommandResult:
    return CommandResult(
        command=command,
        outcome=Outcome.REJECTED,
        summary="Configuration source promotion was refused.",
        code=code,
        findings=(Finding(code, str(exc)),),
        terminal_reason="No unverified tuning evidence is allowed to alter a tracked default.",
    )


def plan(args: argparse.Namespace) -> CommandResult:
    command = "iii config promotion plan"
    try:
        value = _build_plan(args, require_clean=False)
    except Exception as exc:
        return _rejected(command, "III_CONFIG_PROMOTION_PLAN_REJECTED", exc)
    return _accepted(
        command,
        "III_CONFIG_PROMOTION_PLANNED",
        f"Classified {len(value['classifications'])} capture difference(s) without mutation.",
        value,
        profile=args.profile,
    )


def apply_preflight(args: argparse.Namespace) -> dict[str, Any]:
    plan_value = _build_plan(args, require_clean=True)
    workspace = _workspace(args)
    record = (
        workspace
        / ".iii"
        / "operations"
        / getattr(args, "_iii_operation_id", "unretained")
        / "configuration-promotion.json"
    )
    return {
        "schema": "iii.configuration-promotion-apply-preflight/v1",
        "plan": plan_value,
        "commit": bool(args.commit),
        "record": str(record),
        "permissions": ["configuration-source-write"]
        + (["git-commit"] if args.commit else []),
        "mutations": [
            plan_value["default_path"],
            plan_value["manifest_path"],
            str(record),
            *(
                [
                    "git:III-Drone-Configuration:commit",
                    "git:III-Drone-ros2-ws:gitlink-and-lock-commit",
                ]
                if args.commit
                else []
            ),
        ],
    }


def _atomic_bytes(path: Path, raw: bytes) -> None:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"promotion target is missing or linked: {path}")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.promotion-partial")
    if temporary.exists() or temporary.is_symlink():
        raise ValueError(f"promotion partial path already exists: {temporary}")
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    if path.parent.is_symlink():
        raise ValueError("promotion record parent is linked")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.partial")
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(canonical_json(value) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _commit_exact(repo: Path, paths: Sequence[str], message: str) -> str:
    _run(repo, ("git", "add", "--", *paths))
    staged = set(
        filter(None, _git(repo, "diff", "--cached", "--name-only").splitlines())
    )
    if staged != set(paths):
        raise ValueError("Git staging scope differs from the retained promotion plan")
    _run(repo, ("git", "commit", "-m", message))
    return _git(repo, "rev-parse", "HEAD")


def _coordinated_commits(
    args: argparse.Namespace, plan_value: Mapping[str, Any], record: dict[str, Any]
) -> tuple[str, str]:
    workspace = _workspace(args)
    configuration = _configuration_root(args)
    profile = str(plan_value["profile"])
    config_paths = [
        str(Path(plan_value["default_path"]).relative_to(configuration)),
        str(Path(plan_value["manifest_path"]).relative_to(configuration)),
    ]
    configuration_commit = _commit_exact(
        configuration,
        config_paths,
        f"feat(config): promote reviewed {profile} tuning defaults",
    )
    record.update(
        status="configuration-committed", configuration_new_head=configuration_commit
    )
    _atomic_json(Path(record["record_path"]), record)

    update = workspace / "scripts/git/update_submodule_lock.sh"
    verify = workspace / "scripts/git/verify_submodule_lock.sh"
    _run(workspace, (str(update),))
    _run(workspace, (str(verify),))
    workspace_commit = _commit_exact(
        workspace,
        ["deps/submodule-lock.txt", "src/III-Drone-Configuration"],
        f"feat(config): integrate reviewed {profile} tuning promotion",
    )
    return configuration_commit, workspace_commit


def apply(args: argparse.Namespace) -> CommandResult:
    command = "iii config promotion apply"
    try:
        retained = getattr(args, "_iii_retained_plan", None)
        if not isinstance(retained, Mapping) or not isinstance(
            retained.get("preflight"), Mapping
        ):
            raise ValueError("an exact retained configuration promotion is required")
        current = apply_preflight(args)
        if current != retained["preflight"]:
            raise ValueError(
                "capture, source, branch, or Git state changed after planning"
            )
        plan_value = current["plan"]
        default_path = Path(plan_value["default_path"])
        manifest_path = Path(plan_value["manifest_path"])
        default_raw = _read_regular(default_path, label="tracked default")
        manifest_raw = _read_regular(manifest_path, label="package manifest")
        capture, _ = _verified(_capture_root(args), args.capture_id)
        replacements = {
            name: capture["source"]["values"][name]
            for name in plan_value["selected_keys"]
        }
        proposed_default = _replace_yaml_values(
            default_raw,
            old_values=capture["source"]["baseline_values"],
            replacements=replacements,
        )
        proposed_manifest, _, new_manifest_id = _rewrite_manifest(
            manifest_raw,
            profile=args.profile,
            old_default_sha=plan_value["old_default_sha256"],
            new_default_sha=plan_value["new_default_sha256"],
        )
        if (
            _sha256(proposed_default) != plan_value["new_default_sha256"]
            or _sha256(proposed_manifest) != plan_value["new_manifest_sha256"]
            or new_manifest_id != plan_value["new_manifest_id"]
        ):
            raise ValueError(
                "promotion output differs from the retained content identities"
            )
        record = {
            "schema": "iii.configuration-promotion-operation/v1",
            "operation_id": args._iii_operation_id,
            "plan_id": plan_value["plan_id"],
            "record_path": current["record"],
            "status": "writing-source",
            "workspace_old_head": plan_value["workspace_old_head"],
            "configuration_old_head": plan_value["configuration_old_head"],
        }
        _atomic_json(Path(current["record"]), record)
        _atomic_bytes(default_path, proposed_default)
        _atomic_bytes(manifest_path, proposed_manifest)
        if _sha256(default_path.read_bytes()) != plan_value["new_default_sha256"]:
            raise ValueError("tracked default write did not verify")
        if _sha256(manifest_path.read_bytes()) != plan_value["new_manifest_sha256"]:
            raise ValueError("package manifest write did not verify")
        configuration_commit = None
        workspace_commit = None
        if args.commit:
            configuration_commit, workspace_commit = _coordinated_commits(
                args, plan_value, record
            )
        record.update(
            status="completed",
            configuration_new_head=configuration_commit,
            workspace_new_head=workspace_commit,
        )
        _atomic_json(Path(current["record"]), record)
        pr_metadata = {
            "schema": "iii.configuration-promotion-pr-metadata/v1",
            "feature_branch": plan_value["feature_branch"],
            "base_branch": plan_value["base_branch"],
            "configuration_commit": configuration_commit,
            "workspace_commit": workspace_commit,
            "stack_command": [
                "./scripts/git/create_stack_prs.sh",
                "--base",
                plan_value["base_branch"],
                "--feature",
                plan_value["feature_branch"],
                "--yes",
            ],
        }
        payload = {
            "schema": "iii.configuration-promotion-result/v1",
            "plan_id": plan_value["plan_id"],
            "capture_id": plan_value["capture_id"],
            "profile": args.profile,
            "release_id": args.release_id,
            "selected_keys": plan_value["selected_keys"],
            "new_manifest_id": plan_value["new_manifest_id"],
            "committed": bool(args.commit),
            "pr_metadata": pr_metadata,
            "record": current["record"],
        }
    except Exception as exc:
        return _rejected(command, "III_CONFIG_PROMOTION_APPLY_REJECTED", exc)
    actions = ()
    if args.commit:
        actions = (
            NextAction(
                tuple(pr_metadata["stack_command"]),
                "Create or update the coordinated feature PR stack.",
                mutating=True,
                prerequisites=(
                    "Review both exact commits and rerun the submodule lock verification.",
                ),
                confirmation_required=True,
            ),
        )
    return _accepted(
        command,
        "III_CONFIG_PROMOTION_APPLIED",
        f"Promoted {len(plan_value['selected_keys'])} reviewed {args.profile} key(s).",
        payload,
        profile=args.profile,
        next_actions=actions,
    )


def _arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--capture-id", required=True)
    parser.add_argument("--profile", choices=("real", "sim"), required=True)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--key", action="append", required=True)
    parser.add_argument(
        "--classification",
        choices=("shared-tracked-default",),
        required=True,
    )
    parser.add_argument("--base", choices=("develop",), default="develop")
    parser.add_argument("--capture-root", type=Path)


def initialize(parser: argparse.ArgumentParser) -> None:
    commands = parser.add_subparsers(
        dest="configuration_promotion_command", required=True
    )
    plan_parser = commands.add_parser(
        "plan", help="compare and classify a capture without changing source"
    )
    _arguments(plan_parser)
    plan_parser.set_defaults(func=plan, _iii_mutating=False)

    apply_parser = commands.add_parser(
        "apply", help="write explicitly classified keys to one tracked default"
    )
    _arguments(apply_parser)
    apply_parser.add_argument(
        "--commit",
        action="store_true",
        help="create coordinated configuration and workspace commits",
    )
    apply_parser.set_defaults(
        func=apply,
        _iii_mutating=True,
        _iii_plan_provider=apply_preflight,
    )
