"""Install mission specifications and behaviour trees on the Pi without a build.

The runtime loads only the installed, content-addressed mission catalog, which
the Mission package build generates with ``mission_catalog.py generate``. That
generator is plain Python; its only compiled input is the behaviour-node
contract exported from the Mission library. ``iii deploy missions`` therefore
fetches the contract (and the catalog's node model and interface contracts)
from the Pi, regenerates the catalog here from the checkout's specifications,
trees and registrations, and installs it on the Pi in place of the old one.

It refuses a change that is not soft: when any source the installed catalog
records as a build input (C++ sources, CMake files, the generator, the runtime
contract) differs in this checkout, the Pi's binary may not match the trees,
and ``iii deploy dev --build`` is needed instead.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tarfile
import tempfile
from types import ModuleType
from typing import Any, Mapping, Sequence

from . import vehicle_gate
from .result import CommandResult, Finding, NextAction, Outcome

COMMAND = "iii deploy missions"
SCHEMA = "iii.deploy-missions/v1"
MISSION_SOURCE = "src/III-Drone-Mission"
REGISTRATIONS = "config/mission_registrations.json"
RUNTIME_CONTRACT = "config/mission_runtime_contract.json"
ASSET_DIRECTORIES = ("mission_specification", "behavior_trees")
CATALOG_DIRECTORIES = {
    "local": "mission_catalog",
    "qualified": "mission_catalog_variants/qualified",
    "field-candidates": "mission_catalog_variants/field-candidates",
}
NODE_CONTRACT = "behavior-node-contract.json"
INTERFACE_CONTRACTS = (
    "msg/MissionModeStatus.msg",
    "srv/GetMissionCatalog.srv",
    "srv/SelectMissionCatalogEntry.srv",
)
REGISTRATIONS_SCHEMA = "iii.mission-registrations/v1"
BUILD_NEEDED = "III_DEPLOY_MISSIONS_BUILD_REQUIRED"


class MissionDeployError(RuntimeError):
    """The catalog cannot be generated or installed; nothing on the Pi changed."""

    def __init__(self, message: str, code: str = "III_DEPLOY_MISSIONS_REJECTED"):
        super().__init__(message)
        self.code = code


def _environment(args: argparse.Namespace) -> Mapping[str, str]:
    return getattr(args, "_iii_environment", os.environ)


def _workspace(environment: Mapping[str, str]) -> Path:
    configured = environment.get("WORKSPACE_DIR")
    candidates = [Path(configured)] if configured else []
    candidates.extend((Path.cwd(), *Path.cwd().parents))
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if (resolved / MISSION_SOURCE / REGISTRATIONS).is_file():
            return resolved
    raise MissionDeployError(
        f"run this command from the III workspace ({MISSION_SOURCE}/{REGISTRATIONS} not found)"
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _generator(source_root: Path) -> ModuleType:
    """The checkout's catalog generator; the guard proves it equals the Pi's."""

    path = source_root / "iii_drone_mission" / "mission_catalog.py"
    spec = importlib.util.spec_from_file_location("iii_deploy_mission_catalog", path)
    if spec is None or spec.loader is None:
        raise MissionDeployError(f"the catalog generator cannot be loaded from {path}")
    module = importlib.util.module_from_spec(spec)
    # Dataclasses resolve their module through sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# --- Pi access -------------------------------------------------------------

def _target(args: argparse.Namespace) -> str:
    return f"{args.user}@{args.host}"


def _ssh_options() -> list[str]:
    return ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "ConnectionAttempts=1"]


def _ssh(args: argparse.Namespace, script: str, *, capture_bytes: bool = False) -> subprocess.CompletedProcess:
    """Run a bash script on the Pi."""

    return subprocess.run(
        ["ssh", *_ssh_options(), _target(args), "bash -c " + shlex.quote(script)],
        check=False,
        capture_output=True,
        text=not capture_bytes,
        timeout=300,
    )


def _rsync(args: argparse.Namespace, source: Path, remote_path: str) -> subprocess.CompletedProcess:
    """Copy a local directory's contents into a directory on the Pi."""

    return subprocess.run(
        [
            "rsync",
            "-a",
            "--copy-links",
            "--mkpath",
            "--rsh=" + shlex.join(["ssh", *_ssh_options()]),
            f"{source}/",
            f"{_target(args)}:{remote_path}/",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
    )


def _remote_roots(workspace: str) -> str:
    """Shell that finds the Mission and Interfaces share directories (merged or isolated install)."""

    w = shlex.quote(workspace)
    return (
        f"mission=; interfaces=; "
        f"for d in {w}/install/share/iii_drone_mission {w}/install/iii_drone_mission/share/iii_drone_mission; "
        'do [ -d "$d" ] && { mission=$d; break; }; done; '
        f"for d in {w}/install/share/iii_drone_interfaces {w}/install/iii_drone_interfaces/share/iii_drone_interfaces; "
        'do [ -d "$d" ] && { interfaces=$d; break; }; done; '
        'test -n "$mission" || { echo "no installed iii_drone_mission under the workspace" >&2; exit 3; }; '
        'test -n "$interfaces" || { echo "no installed iii_drone_interfaces under the workspace" >&2; exit 3; }; '
    )


def fetch_installed(args: argparse.Namespace, destination: Path) -> Path:
    """Copy what the Pi's catalog was built from (and the catalog itself) to destination."""

    files = [
        f"mission/{NODE_CONTRACT}",
        "mission/mission_catalog/models.xml",
        "mission/mission_catalog/source-state.json",
        "mission/mission_catalog/catalog.json",
        *(f"interfaces/{relative}" for relative in INTERFACE_CONTRACTS),
    ]
    script = (
        "set -e; "
        + _remote_roots(args.remote_workspace)
        + 'stage=$(mktemp -d); trap "rm -rf $stage" EXIT; '
        + 'ln -s "$mission" "$stage/mission"; ln -s "$interfaces" "$stage/interfaces"; '
        + f'if [ ! -f "$mission/{NODE_CONTRACT}" ]; then echo "MISSING_NODE_CONTRACT" >&2; exit 4; fi; '
        + 'echo "$mission" >&2; '
        + 'tar -C "$stage" -chf - ' + " ".join(shlex.quote(item) for item in files)
    )
    completed = _ssh(args, script, capture_bytes=True)
    stderr = completed.stderr.decode("utf-8", "replace")
    if completed.returncode == 4 or "MISSING_NODE_CONTRACT" in stderr:
        raise MissionDeployError(
            "the Pi's Mission install predates soft mission deployment (it has no "
            f"{NODE_CONTRACT}); run iii deploy dev --build once",
            BUILD_NEEDED,
        )
    if completed.returncode:
        raise MissionDeployError(
            f"the Pi's installed catalog could not be read ({completed.returncode}): {stderr.strip()}"
        )
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / "installed.tar"
    archive.write_bytes(completed.stdout)
    with tarfile.open(archive) as bundle:
        for member in bundle.getmembers():
            if not member.isfile() or member.name not in files:
                raise MissionDeployError(f"unexpected entry in the Pi's catalog bundle: {member.name}")
        bundle.extractall(destination, filter="data")
    archive.unlink()
    lines = [line for line in stderr.splitlines() if line.startswith("/")]
    return Path(lines[-1]) if lines else Path()


# --- Local generation ------------------------------------------------------

def write_registrations(source_root: Path, output: Path) -> list[dict[str, Any]]:
    """The generator's registration manifest from config/mission_registrations.json."""

    document = json.loads((source_root / REGISTRATIONS).read_text(encoding="utf-8"))
    if document.get("schema") != REGISTRATIONS_SCHEMA:
        raise MissionDeployError(f"{REGISTRATIONS} must use {REGISTRATIONS_SCHEMA}")
    missions = document.get("missions")
    if not isinstance(missions, list) or not missions:
        raise MissionDeployError(f"{REGISTRATIONS} declares no missions")
    records = []
    for mission in missions:
        specification = (source_root / mission["specification"]).resolve()
        records.append(
            "\t".join(
                [
                    mission["id"],
                    str(specification),
                    mission["classification"],
                    mission.get("status", "active"),
                    ",".join(sorted(mission["profiles"])),
                    ",".join(sorted(mission.get("default_for", []))),
                ]
            )
        )
    output.write_text("".join(f"{record}\n" for record in sorted(records)), encoding="utf-8")
    return missions


def _is_asset(logical: str) -> bool:
    return logical.split("/", 1)[0] in ASSET_DIRECTORIES


def build_input_drift(source_root: Path, installed_state: Mapping[str, Any]) -> list[str]:
    """Build inputs of the installed catalog that differ in this checkout."""

    drift = []
    for logical, expected in sorted(installed_state.get("source_files", {}).items()):
        if _is_asset(logical):
            continue
        path = source_root / logical
        if not path.is_file():
            drift.append(f"missing in this checkout: {logical}")
        elif "sha256:" + _sha256(path) != expected:
            drift.append(f"changed in this checkout: {logical}")
    return drift


def _check_fetched(fetched: Path, installed_state: Mapping[str, Any]) -> None:
    """The fetched contract, model and interfaces are the ones the installed catalog used."""

    pairs = [
        (fetched / "mission" / NODE_CONTRACT, installed_state.get("behavior_node_contract_sha256")),
        (fetched / "mission/mission_catalog/models.xml", installed_state.get("models_xml_sha256")),
    ]
    for relative in INTERFACE_CONTRACTS:
        pairs.append(
            (fetched / "interfaces" / relative,
             installed_state.get("interface_contracts", {}).get(Path(relative).name))
        )
    for path, expected in pairs:
        if expected is not None and "sha256:" + _sha256(path) != expected:
            raise MissionDeployError(
                f"the Pi's {path.name} does not match its installed catalog; run iii deploy dev --build",
                BUILD_NEEDED,
            )


def generate(source_root: Path, fetched: Path, output: Path) -> dict[str, Any]:
    """Generate all three catalogs from the checkout and the Pi's build contracts."""

    installed_state = json.loads((fetched / "mission/mission_catalog/source-state.json").read_text())
    drift = build_input_drift(source_root, installed_state)
    if drift:
        raise MissionDeployError(
            "this checkout changes inputs the Pi's binary was built from, so the trees "
            "may not match it; deploy with iii deploy dev --build. " + "; ".join(drift),
            BUILD_NEEDED,
        )
    _check_fetched(fetched, installed_state)
    generator = _generator(source_root)
    registrations = output / "registrations.tsv"
    output.mkdir(parents=True, exist_ok=True)
    write_registrations(source_root, registrations)
    build_contracts = [
        source_root / logical
        for logical in sorted(installed_state["source_files"])
        if not _is_asset(logical) and logical != RUNTIME_CONTRACT
    ]
    try:
        hashes = generator.generate_catalogs(
            source_root=source_root,
            registrations_path=registrations,
            node_contract_path=fetched / "mission" / NODE_CONTRACT,
            interface_contract_paths=[fetched / "interfaces" / relative for relative in INTERFACE_CONTRACTS],
            runtime_contract_path=source_root / RUNTIME_CONTRACT,
            models_xml_path=fetched / "mission/mission_catalog/models.xml",
            build_contract_paths=build_contracts,
            local_output=output / CATALOG_DIRECTORIES["local"],
            qualified_output=output / CATALOG_DIRECTORIES["qualified"],
            field_candidates_output=output / CATALOG_DIRECTORIES["field-candidates"],
        )
    except generator.CatalogError as exc:
        raise MissionDeployError(f"the mission catalog is invalid: {exc}") from None
    for scope, relative in CATALOG_DIRECTORIES.items():
        generator.verify_catalog(output / relative, expected_scope=scope)
    return hashes


def _entry_content(entry: Mapping[str, Any]) -> dict[str, Any]:
    # The compatibility block and entry hash change with every source change;
    # what is compared is what the mission is.
    return {key: value for key, value in entry.items() if key not in {"compatibility", "entry_hash"}}


def catalog_changes(installed: Mapping[str, Any], generated: Mapping[str, Any]) -> list[dict[str, str]]:
    old = {entry["id"]: entry for entry in installed.get("entries", [])}
    new = {entry["id"]: entry for entry in generated.get("entries", [])}
    changes = []
    for catalog_id in sorted(set(old) | set(new)):
        if catalog_id not in old:
            changes.append({"id": catalog_id, "change": "added"})
        elif catalog_id not in new:
            changes.append({"id": catalog_id, "change": "removed"})
        elif _entry_content(old[catalog_id]) != _entry_content(new[catalog_id]):
            old_assets = {asset["logical_name"]: asset["content_hash"] for asset in old[catalog_id]["assets"]}
            new_assets = {asset["logical_name"]: asset["content_hash"] for asset in new[catalog_id]["assets"]}
            files = sorted(
                name for name in set(old_assets) | set(new_assets) if old_assets.get(name) != new_assets.get(name)
            )
            changes.append({"id": catalog_id, "change": "changed", "files": ", ".join(files) or "registration"})
    return changes


# --- Installation on the Pi --------------------------------------------------

def _swap_script(workspace: str, stamp: str) -> str:
    """Verify the staged catalogs, keep the installed ones, swap the staged ones in."""

    return (
        "set -e; "
        + _remote_roots(workspace)
        + f'stage="$mission/.catalog-staging-{stamp}"; backup="$mission/catalog-backups/{stamp}"; '
        + f'generator=$(find {shlex.quote(workspace)}/install -path "*site-packages/iii_drone_mission/mission_catalog.py" | head -n 1); '
        + 'test -n "$generator" || { echo "the installed catalog verifier is missing" >&2; exit 3; }; '
        + 'for scope in local:mission_catalog qualified:mission_catalog_variants/qualified '
        + 'field-candidates:mission_catalog_variants/field-candidates; do '
        + 'python3 "$generator" verify --scope "${scope%%:*}" "$stage/${scope#*:}" >/dev/null; done; '
        + 'mkdir -p "$backup"; '
        + 'mv "$mission/mission_catalog" "$mission/mission_catalog_variants" "$backup/"; '
        + 'mv "$stage/mission_catalog" "$stage/mission_catalog_variants" "$mission/"; '
        + 'rmdir "$stage"; '
        + 'python3 "$generator" verify --scope local "$mission/mission_catalog" >/dev/null; '
        + 'echo "BACKUP=$backup"'
    )


RESTART = "sudo systemctl restart iii-system-daemon.service iii-runtime-api.service"


def _result(
    outcome: Outcome,
    code: str,
    summary: str,
    payload: Mapping[str, Any],
    *,
    detail: str | None = None,
    target: str | None = None,
) -> CommandResult:
    return CommandResult(
        command=COMMAND,
        outcome=outcome,
        summary=summary,
        code=code,
        target=target,
        findings=(Finding(code, detail),) if detail else (),
        payload_schema=SCHEMA,
        payload=payload,
        next_actions=(
            NextAction(
                ("iii", "deploy", "missions", "--host", str(payload.get("host")), "--dry-run"),
                "Compare the checkout's missions with the Pi's installed catalog.",
            ),
        ),
    )


def _show(changes: Sequence[Mapping[str, str]], stream: Any) -> None:
    if stream is None:
        return
    if not changes:
        print("The Pi's installed missions already match this checkout.", file=stream, flush=True)
        return
    print("Missions that differ from the Pi's installed catalog:", file=stream)
    for item in changes:
        files = f" ({item['files']})" if item.get("files") else ""
        print(f"  {item['change']:<8} {item['id']}{files}", file=stream)
    stream.flush()


def deploy(args: argparse.Namespace) -> CommandResult:
    dry_run = bool(getattr(args, "_iii_dry_run", False))
    stream = getattr(args, "_iii_progress_stream", None)
    target = _target(args)
    payload: dict[str, Any] = {"schema": SCHEMA, "host": args.host, "dry_run": dry_run}
    try:
        workspace = _workspace(_environment(args))
        source_root = workspace / MISSION_SOURCE
        with tempfile.TemporaryDirectory(prefix="iii-deploy-missions-") as directory:
            work = Path(directory)
            payload["remote_share"] = str(fetch_installed(args, work / "installed"))
            payload["catalog_hashes"] = generate(source_root, work / "installed", work / "generated")
            installed = json.loads((work / "installed/mission/mission_catalog/catalog.json").read_text())
            generated = json.loads((work / "generated/mission_catalog/catalog.json").read_text())
            changes = catalog_changes(installed, generated)
            payload["changes"] = changes
            _show(changes, stream)
            if not changes:
                return _result(
                    Outcome.SUCCESS, "III_DEPLOY_MISSIONS_UNCHANGED",
                    "The Pi's installed missions already match this checkout; nothing was changed.",
                    payload, target=target,
                )
            if dry_run:
                return _result(
                    Outcome.SUCCESS, "III_DEPLOY_MISSIONS_PREVIEW",
                    f"{len(changes)} missions would be installed; nothing was changed.",
                    payload, target=target,
                )
            gate = vehicle_gate.evaluate(
                args.host, args.user, force=bool(getattr(args, "force", False)), ssh_options=_ssh_options()
            )
            payload["vehicle_gate"] = gate.as_dict()
            if not gate.allowed:
                return vehicle_gate.rejection(COMMAND, gate, target=target)

            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            share = payload["remote_share"]
            staging = f"{share}/.catalog-staging-{stamp}"
            for relative in CATALOG_DIRECTORIES.values():
                copied = _rsync(args, work / "generated" / relative, f"{staging}/{relative}")
                if copied.returncode:
                    raise MissionDeployError(f"copying the catalog to the Pi failed: {copied.stderr.strip()}")
            swapped = _ssh(args, _swap_script(args.remote_workspace, stamp))
            if swapped.returncode:
                _ssh(args, f"rm -rf {shlex.quote(staging)}")
                raise MissionDeployError(
                    "the Pi refused the new catalog and kept the installed one: "
                    + swapped.stderr.strip()
                )
            payload["backup"] = next(
                (line.split("=", 1)[1] for line in swapped.stdout.splitlines() if line.startswith("BACKUP=")),
                None,
            )
            # The Pi's source copy follows its install, as after a build deploy.
            for relative in (*ASSET_DIRECTORIES, "config"):
                _rsync(args, source_root / relative, f"{args.remote_workspace}/{MISSION_SOURCE}/{relative}")
            restarted = _ssh(args, RESTART)
            payload["restarted"] = restarted.returncode == 0
            if restarted.returncode:
                return _result(
                    Outcome.FAILED, "III_DEPLOY_MISSIONS_RESTART_FAILED",
                    "The new catalog is installed, but the runtime services did not restart.",
                    payload, detail=restarted.stderr.strip(), target=target,
                )
    except MissionDeployError as exc:
        return _result(
            Outcome.REJECTED, exc.code, "The missions were not installed; the Pi is unchanged.",
            payload, detail=str(exc), target=target,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return _result(
            Outcome.FAILED, "III_DEPLOY_MISSIONS_FAILED",
            "The missions were not installed.", payload, detail=str(exc), target=target,
        )
    return _result(
        Outcome.SUCCESS, "III_DEPLOY_MISSIONS_INSTALLED",
        f"{len(changes)} missions were installed on the Pi and the runtime services restarted; "
        f"the previous catalog is kept at {payload['backup']}.",
        payload, target=target,
    )


def initialize(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "missions",
        help="install mission specifications and behaviour trees on the Pi without a build",
        description=(
            "Regenerate the mission catalog from this checkout's mission specifications, "
            "behaviour trees and registrations (src/III-Drone-Mission/config/"
            "mission_registrations.json) against the behaviour-node contract installed on "
            "the Pi, install it there in place of the old catalog (kept as a backup), and "
            "restart the runtime services. Refused when the change is not soft (C++ "
            "sources, CMake files or the generator differ from what the Pi was built from) "
            "and unless the aircraft is disarmed and landed. --dry-run lists the missions "
            "that would change."
        ),
    )
    parser.add_argument("--host", default="iii.local", help="Pi hostname or IP")
    parser.add_argument("--user", default="iii", help="SSH user")
    parser.add_argument(
        "--remote-workspace", default="/home/iii/ws", help="editable workspace on the Pi"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "proceed although the aircraft's disarmed and landed state cannot be read "
            "(never overrides an armed or flying aircraft)"
        ),
    )
    parser.set_defaults(func=deploy, _iii_mutating=False, _iii_direct_mutation=True)
