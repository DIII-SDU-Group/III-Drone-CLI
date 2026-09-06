from __future__ import annotations

from io import StringIO
import hashlib
import json
from pathlib import Path
import stat
import subprocess

import pytest
import yaml

from iii.__main__ import build_parser, main
from iii.runner import inventory_parser
from iii_drone_contracts.configuration_capture import (
    canonical_json,
    content_identity,
    seal_capture,
)


RELEASE = "a" * 64


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return completed.stdout.strip()


def _default(values: dict) -> bytes:
    return yaml.safe_dump(
        {"/**": {"ros__parameters": values}}, sort_keys=False
    ).encode()


def _identity(value: dict) -> str:
    return content_identity(
        {key: item for key, item in value.items() if key != "manifest_id"}
    )


def _manifest(real: bytes, sim: bytes) -> dict:
    hashes = {
        "real": hashlib.sha256(real).hexdigest(),
        "sim": hashlib.sha256(sim).hexdigest(),
    }
    value = {
        "schema": "iii.configuration-package/v1",
        "manifest_id": "",
        "package_version": "2.2.0",
        "configuration_schema_version": 1,
        "compatibility": {
            "readable": {"minimum": 1, "maximum": 1},
            "upgrade_from": {"minimum": 1, "maximum": 1},
            "downgrade_from": {"minimum": 1, "maximum": 1},
        },
        "artifacts": [
            {
                "path": f"tracked_defaults/{profile}/default.yaml",
                "sha256": hashes[profile],
            }
            for profile in ("real", "sim")
        ],
        "tracked_sets": [
            {
                "profile": profile,
                "set_id": "default",
                "default": True,
                "path": f"tracked_defaults/{profile}/default.yaml",
                "sha256": hashes[profile],
            }
            for profile in ("real", "sim")
        ],
    }
    value["manifest_id"] = _identity(value)
    return value


def _write_manifest(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n")


def _workspace(tmp_path: Path, *, branch: str = "deployment-infrastructure-redesign"):
    workspace = tmp_path / "workspace"
    configuration = workspace / "src/III-Drone-Configuration"
    configuration.mkdir(parents=True)
    _git(configuration, "init", "-b", branch)
    _git(configuration, "config", "user.name", "III Test")
    _git(configuration, "config", "user.email", "iii-test@example.invalid")
    real_values = {"/control/gain": 1.0, "/control/other": 10}
    sim_values = {"/control/gain": 2.0, "/control/other": 20}
    defaults = {"real": _default(real_values), "sim": _default(sim_values)}
    for profile, raw in defaults.items():
        path = configuration / f"config/parameter_sets/{profile}/tracked/default.yaml"
        path.parent.mkdir(parents=True)
        path.write_bytes(raw)
    manifest = _manifest(defaults["real"], defaults["sim"])
    manifest_path = (
        configuration / "config/configuration_contract/package-manifest.json"
    )
    manifest_path.parent.mkdir(parents=True)
    _write_manifest(manifest_path, manifest)
    _git(configuration, "add", ".")
    _git(configuration, "commit", "-m", "fixture configuration")

    _git(workspace, "init", "-b", branch)
    _git(workspace, "config", "user.name", "III Test")
    _git(workspace, "config", "user.email", "iii-test@example.invalid")
    (workspace / "deps").mkdir()
    (workspace / "scripts/git").mkdir(parents=True)
    config_head = _git(configuration, "rev-parse", "HEAD")
    (workspace / "deps/submodule-lock.txt").write_text(
        f"src/III-Drone-Configuration {config_head}\n"
    )
    (workspace / ".gitignore").write_text(".iii/\n")
    update = workspace / "scripts/git/update_submodule_lock.sh"
    update.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "head=$(git -C src/III-Drone-Configuration rev-parse HEAD)\n"
        "printf 'src/III-Drone-Configuration %s\\n' \"$head\" > deps/submodule-lock.txt\n"
    )
    verify = workspace / "scripts/git/verify_submodule_lock.sh"
    verify.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "head=$(git -C src/III-Drone-Configuration rev-parse HEAD)\n"
        'test "$(cat deps/submodule-lock.txt)" = "src/III-Drone-Configuration $head"\n'
    )
    for script in (update, verify):
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
    _git(workspace, "add", ".")
    _git(workspace, "commit", "-m", "fixture workspace")
    return workspace, configuration, manifest, {"real": real_values, "sim": sim_values}


def _head(sequence: int = 2, revision: int = 1) -> dict:
    value = {
        "schema": "iii.configuration-tuning-wal-entry/v1",
        "sequence": sequence,
        "previous_checksum": "1" * 64,
        "checksum": "",
        "kind": "committed",
        "timestamp": "2026-08-27T12:00:03Z",
        "session_id": "2" * 64,
        "transaction_id": "3" * 64,
        "request_id": "promotion-fixture",
        "revision": revision,
        "body": {"operator_id": "test"},
    }
    value["checksum"] = content_identity(
        {key: item for key, item in value.items() if key != "checksum"}
    )
    return value


def _capture(
    workspace: Path,
    manifest: dict,
    baseline: dict,
    *,
    profile: str,
    values: dict | None = None,
    release_id: str = RELEASE,
    workspace_id: str | None = None,
    manifest_id: str | None = None,
) -> str:
    head = _head()
    capture_values = values or {
        **baseline,
        "/control/gain": 3.5,
        "/control/other": 30,
    }
    source = {
        "schema": "iii.configuration-capture-source/v1",
        "snapshot_id": "snapshots/tuned.yaml",
        "snapshot_content_sha256": "4" * 64,
        "values": capture_values,
        "parameter_document": {
            "/**": {"ros__parameters": capture_values},
            "/sensor/example": {"ros__parameters": {"frame_id": "sensor"}},
        },
        "target_id": "drone-1" if profile == "real" else "sim",
        "runtime_profile": profile,
        "release_id": release_id,
        "workspace_id": workspace_id or _git(workspace, "rev-parse", "HEAD"),
        "manifest_id": manifest_id or manifest["manifest_id"],
        "session_id": "2" * 64,
        "baseline_id": "5" * 64,
        "baseline_values": baseline,
        "session_created_at": "2026-08-27T12:00:00Z",
        "journal_updated_at": "2026-08-27T12:00:03Z",
        "journal_revision": 1,
        "journal_sequence": 2,
        "journal_checksum": head["checksum"],
        "journal_head_entry": head,
        "pending_boot_values": {},
        "source_is_active": False,
        "source_is_default": False,
    }
    capture = seal_capture(source)
    path = workspace / ".iii/captures" / capture["capture_id"] / "capture.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(canonical_json(capture) + b"\n")
    return capture["capture_id"]


def _environment(workspace: Path) -> dict[str, str]:
    return {
        "CLI_CONFIGURATION": "dev",
        "WORKSPACE_DIR": str(workspace),
        "III_CAPTURE_ROOT": str(workspace / ".iii/captures"),
        "III_OPERATION_STATE_DIR": str(workspace / ".iii/operations"),
    }


def _argv(leaf: str, capture_id: str, profile: str, *, commit: bool = False):
    value = [
        "config",
        "promotion",
        leaf,
        "--capture-id",
        capture_id,
        "--profile",
        profile,
        "--release-id",
        RELEASE,
        "--classification",
        "shared-tracked-default",
        "--key",
        "/control/gain",
    ]
    if commit:
        value.append("--commit")
    return value


def _invoke(argv, environment):
    stdout, stderr = StringIO(), StringIO()
    status = main(
        [*argv, "--json"],
        stdout=stdout,
        stderr=stderr,
        environment=environment,
    )
    assert stderr.getvalue() == ""
    return status, json.loads(stdout.getvalue())


def test_promotion_plan_and_apply_are_separate_inventory_leaves():
    inventory = inventory_parser(build_parser())
    planned = inventory[("config", "promotion", "plan")]
    applied = inventory[("config", "promotion", "apply")]
    assert planned.mutating is False and planned.plan_provider is None
    assert applied.mutating is True and applied.plan_provider is not None


@pytest.mark.parametrize("profile", ["real", "sim"])
def test_plan_is_read_only_and_classifies_partial_selection(profile, tmp_path):
    workspace, configuration, manifest, baselines = _workspace(tmp_path)
    capture_id = _capture(workspace, manifest, baselines[profile], profile=profile)
    before = _git(configuration, "status", "--porcelain")

    status, result = _invoke(
        _argv("plan", capture_id, profile), _environment(workspace)
    )

    assert status == 0
    assert _git(configuration, "status", "--porcelain") == before == ""
    plan = result["payload"]
    assert plan["profile"] == profile
    assert plan["selected_keys"] == ["/control/gain"]
    classes = {item["name"]: item["classification"] for item in plan["classifications"]}
    assert classes == {
        "/control/gain": "shared-tracked-default",
        "/control/other": "retained-capture-evidence",
    }
    assert "-/control/gain" not in plan["diff"]
    assert "/control/gain" in plan["diff"]


def test_apply_changes_only_selected_real_default_and_reseals_manifest(tmp_path):
    workspace, configuration, manifest, baselines = _workspace(tmp_path)
    capture_id = _capture(workspace, manifest, baselines["real"], profile="real")
    sim_before = (
        configuration / "config/parameter_sets/sim/tracked/default.yaml"
    ).read_bytes()
    argv = [
        *_argv("apply", capture_id, "real"),
        "--operation-id",
        "configuration-promotion-0001",
        "--confirm",
        "--non-interactive",
    ]

    status, result = _invoke(argv, _environment(workspace))

    assert status == 0 and result["payload"]["committed"] is False
    real = yaml.safe_load(
        (configuration / "config/parameter_sets/real/tracked/default.yaml").read_text()
    )["/**"]["ros__parameters"]
    assert real == {"/control/gain": 3.5, "/control/other": 10}
    assert (
        configuration / "config/parameter_sets/sim/tracked/default.yaml"
    ).read_bytes() == sim_before
    changed = _git(configuration, "diff", "--name-only").splitlines()
    assert changed == [
        "config/configuration_contract/package-manifest.json",
        "config/parameter_sets/real/tracked/default.yaml",
    ]
    new_manifest = json.loads(
        (
            configuration / "config/configuration_contract/package-manifest.json"
        ).read_text()
    )
    assert new_manifest["manifest_id"] == result["payload"]["new_manifest_id"]
    assert new_manifest["manifest_id"] == _identity(new_manifest)


def test_apply_can_create_exact_coordinated_commits_and_stack_metadata(tmp_path):
    workspace, configuration, manifest, baselines = _workspace(tmp_path)
    capture_id = _capture(workspace, manifest, baselines["sim"], profile="sim")
    workspace_old = _git(workspace, "rev-parse", "HEAD")
    config_old = _git(configuration, "rev-parse", "HEAD")
    argv = [
        *_argv("apply", capture_id, "sim", commit=True),
        "--operation-id",
        "configuration-promotion-commit-0001",
        "--confirm",
        "--non-interactive",
    ]

    status, result = _invoke(argv, _environment(workspace))

    assert status == 0 and result["payload"]["committed"] is True
    metadata = result["payload"]["pr_metadata"]
    assert metadata["configuration_commit"] != config_old
    assert metadata["workspace_commit"] != workspace_old
    assert metadata["stack_command"] == [
        "./scripts/git/create_stack_prs.sh",
        "--base",
        "develop",
        "--feature",
        "deployment-infrastructure-redesign",
        "--yes",
    ]
    assert _git(configuration, "status", "--porcelain") == ""
    assert _git(workspace, "status", "--porcelain") == ""
    assert (
        _git(workspace, "ls-files", "--stage", "src/III-Drone-Configuration").split()[1]
        == metadata["configuration_commit"]
    )


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("release", "release provenance"),
        ("profile", "profiles differ"),
        ("manifest", "manifest differs"),
        ("workspace", "workspace provenance"),
        ("deprecated", "deprecated, removed, or unknown"),
    ],
)
def test_wrong_provenance_or_deprecated_selected_key_fails_closed(
    case, message, tmp_path
):
    workspace, _configuration, manifest, baselines = _workspace(tmp_path)
    kwargs = {}
    profile = "real"
    values = {**baselines[profile], "/control/gain": 3.5}
    selected = "/control/gain"
    if case == "profile":
        profile = "sim"
        values = {**baselines["sim"], "/control/gain": 3.5}
    elif case == "manifest":
        kwargs["manifest_id"] = "f" * 64
    elif case == "workspace":
        kwargs["workspace_id"] = "f" * 64
    elif case == "deprecated":
        values["/control/removed"] = 7
        selected = "/control/removed"
    capture_id = _capture(
        workspace,
        manifest,
        baselines[profile],
        profile=profile,
        values=values,
        **kwargs,
    )
    argv = _argv("plan", capture_id, "real")
    argv[argv.index("/control/gain")] = selected
    if case == "release":
        argv[argv.index(RELEASE)] = "e" * 64

    status, result = _invoke(argv, _environment(workspace))

    assert status == 20
    assert message in result["findings"][0]["message"]


def test_concurrent_source_change_requires_reconciliation(tmp_path):
    workspace, configuration, manifest, baselines = _workspace(tmp_path)
    capture_id = _capture(workspace, manifest, baselines["real"], profile="real")
    path = configuration / "config/parameter_sets/real/tracked/default.yaml"
    path.write_bytes(_default({**baselines["real"], "/control/other": 11}))

    status, result = _invoke(_argv("plan", capture_id, "real"), _environment(workspace))

    assert status == 20
    assert "reconciliation is required" in result["findings"][0]["message"]


def test_capture_schema_mismatch_fails_before_source_comparison(tmp_path):
    workspace, _configuration, manifest, baselines = _workspace(tmp_path)
    capture_id = _capture(workspace, manifest, baselines["real"], profile="real")
    old = workspace / ".iii/captures" / capture_id
    value = json.loads((old / "capture.json").read_text())
    value["schema"] = "iii.configuration-capture/v99"
    value["capture_id"] = content_identity(
        {key: item for key, item in value.items() if key != "capture_id"}
    )
    replacement = workspace / ".iii/captures" / value["capture_id"]
    old.rename(replacement)
    (replacement / "capture.json").write_bytes(canonical_json(value) + b"\n")

    status, result = _invoke(
        _argv("plan", value["capture_id"], "real"), _environment(workspace)
    )

    assert status == 20
    assert "capture fields are invalid" in result["findings"][0]["message"]


def test_protected_or_codex_branch_is_never_a_promotion_target(tmp_path):
    workspace, _configuration, manifest, baselines = _workspace(
        tmp_path, branch="develop"
    )
    capture_id = _capture(workspace, manifest, baselines["real"], profile="real")

    status, result = _invoke(_argv("plan", capture_id, "real"), _environment(workspace))

    assert status == 20
    assert "normal feature branch" in result["findings"][0]["message"]
