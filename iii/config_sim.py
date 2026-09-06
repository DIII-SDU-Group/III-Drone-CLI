"""Local simulation configuration inspection and recoverable reset commands."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any, Mapping

from .result import CommandResult, Finding, NextAction, Outcome

CHECKPOINT_ID = re.compile(r"^[0-9a-f]{64}$")


def _environment(args: argparse.Namespace) -> Mapping[str, str]:
    return getattr(args, "_iii_environment", os.environ)


def _workspace(args: argparse.Namespace) -> Path:
    configured = _environment(args).get("WORKSPACE_DIR")
    candidates = [Path(configured)] if configured else []
    candidates.extend((Path.cwd(), *Path.cwd().parents))
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if (resolved / "deps/submodule-lock.txt").is_file() and (
            resolved / "tools/III-Drone-CLI"
        ).is_dir():
            return resolved
    raise ValueError("cannot locate the current III workspace clone")


def _roots(args: argparse.Namespace) -> tuple[Path, Path, Path, Path]:
    env = _environment(args)
    workspace = _workspace(args)
    config_base = Path(env.get("CONFIG_BASE_DIR", workspace / ".config"))
    living = (
        Path(env.get("III_CONFIGURATION_STATE_ROOT", config_base / "iii_drone"))
        .expanduser()
        .absolute()
    )
    checkpoints = (
        Path(
            env.get(
                "III_CONFIGURATION_CHECKPOINT_ROOT",
                workspace / ".iii/configuration-checkpoints",
            )
        )
        .expanduser()
        .absolute()
    )
    operations = (
        Path(env.get("III_OPERATIONS_ROOT", workspace / ".iii/operations"))
        .expanduser()
        .absolute()
    )
    return workspace, living, checkpoints, operations


def _contract(args: argparse.Namespace) -> Path:
    configured = _environment(args).get("III_CONFIGURATION_CONTRACT_ROOT")
    if configured:
        return Path(configured).expanduser().absolute()
    from iii_drone_configuration import resolve_installed_contract_root

    return resolve_installed_contract_root()


def _checkpoint_path(root: Path, checkpoint_id: str) -> Path:
    if not CHECKPOINT_ID.fullmatch(checkpoint_id):
        raise ValueError("configuration checkpoint ID is malformed")
    path = root / checkpoint_id
    if path.parent != root:
        raise ValueError("configuration checkpoint escapes its fixed root")
    return path


def _release_id(args: argparse.Namespace, manifest_id: str) -> str:
    env = _environment(args)
    return (
        env.get("III_ACTIVE_RELEASE_ID")
        or env.get("III_WORKSPACE_RELEASE_ID")
        or manifest_id
    )


def _simulation_plan(args: argparse.Namespace):
    from iii_drone_configuration import (
        load_installed_contract,
        plan_simulation_reconciliation,
    )

    _workspace_path, living, _checkpoint_root, operations = _roots(args)
    contract_root = _contract(args)
    contract = load_installed_contract(contract_root).contract
    release_id = _release_id(args, contract.manifest_id)
    plan = plan_simulation_reconciliation(
        immutable_root=contract_root,
        writable_state_root=living,
        operations_root=operations,
        runtime_profile="sim",
        target_id="sim",
        release_id=release_id,
    )
    return plan, contract_root, release_id


def _accepted(
    *,
    command: str,
    code: str,
    summary: str,
    payload: Mapping[str, Any],
    operation_id: str | None = None,
    state: str | None = None,
) -> CommandResult:
    return CommandResult(
        command=command,
        outcome=Outcome.SUCCESS,
        summary=summary,
        code=code,
        operation_id=operation_id,
        state=state,
        target="sim",
        profile="sim",
        payload_schema=str(payload.get("schema", "iii.config-sim-result/v1")),
        payload=payload,
        terminal_reason="The current clone's Git-ignored simulation state was handled without changing tracked defaults.",
    )


def _rejected(command: str, code: str, exc: Exception) -> CommandResult:
    return CommandResult(
        command=command,
        outcome=Outcome.REJECTED,
        summary="The simulation configuration operation was refused.",
        code=code,
        target="sim",
        profile="sim",
        findings=(Finding(code, str(exc)),),
        next_actions=(
            NextAction(
                ("iii", "config", "sim", "inspect"),
                "Inspect the exact living state, contract, and reconciliation blockers.",
            ),
        ),
    )


def inspect(args: argparse.Namespace) -> CommandResult:
    command = "iii config sim inspect"
    try:
        from iii_drone_configuration import verify_configuration_checkpoint

        _workspace_path, living, checkpoint_root, _operations = _roots(args)
        plan, contract_root, release_id = _simulation_plan(args)
        checkpoints = []
        if checkpoint_root.is_dir() and not checkpoint_root.is_symlink():
            for path in sorted(checkpoint_root.iterdir()):
                if not path.is_dir() or path.is_symlink():
                    continue
                value = verify_configuration_checkpoint(path)
                checkpoints.append(
                    {
                        "checkpoint_id": value["checkpoint_id"],
                        "path": str(path),
                        "files": len(value["files"]),
                    }
                )
        payload = {
            "schema": "iii.config-sim-inspection/v1",
            "living_root": str(living),
            "contract_root": str(contract_root),
            "manifest_id": plan.new_manifest_id,
            "release_id": release_id,
            "reconciliation": plan.as_dict(),
            "checkpoints": checkpoints,
            "tracked_default_mutations": 0,
        }
    except Exception as exc:
        return _rejected(command, "III_CONFIG_SIM_INSPECT_REJECTED", exc)
    return _accepted(
        command=command,
        code="III_CONFIG_SIM_INSPECTED",
        summary="Inspected the current clone's living simulation configuration.",
        payload=payload,
    )


def _decisions(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        key, separator, decision = value.rpartition("=")
        if not separator or not key or decision not in {"use_old", "use_new_default"}:
            raise ValueError(
                "--decision requires SET_REFERENCE:PARAMETER=use_old|use_new_default"
            )
        if key in result:
            raise ValueError(f"decision was repeated: {key}")
        result[key] = decision
    return result


def review_preflight(args: argparse.Namespace) -> dict[str, Any]:
    plan, _contract_root, _release_id_value = _simulation_plan(args)
    if not plan.review_required:
        raise ValueError(
            "current simulation state has no unresolved reintroduction review"
        )
    decisions = _decisions(args.decision)
    expected = {
        f"{item['set_reference']}:{item['parameter']}" for item in plan.review_items
    }
    if set(decisions) != expected:
        raise ValueError(
            "decisions must cover every and only unresolved set/parameter key"
        )
    operation_root = plan.operations_root / plan.operation_id
    return {
        "schema": "iii.config-sim-review-preflight/v1",
        "reconciliation_plan_id": plan.plan_id,
        "reconciliation_operation_id": plan.operation_id,
        "initial_state_id": plan.initial_state_id,
        "decisions": dict(sorted(decisions.items())),
        "review_items": [dict(item) for item in plan.review_items],
        "permissions": ["local-configuration-write"],
        "mutations": [
            str(operation_root / "reconciliation-review.json"),
            str(operation_root / "reconciliation-decisions.json"),
            *[str(plan.writable_state_root / item) for item in plan.mutations],
        ],
        "tracked_default_mutations": 0,
    }


def review(args: argparse.Namespace) -> CommandResult:
    command = "iii config sim review"
    try:
        from iii_drone_configuration import (
            execute_reconciliation,
            write_reintroduction_decisions,
            write_reintroduction_review,
        )

        plan, _contract_root, _release_id_value = _simulation_plan(args)
        retained = args._iii_retained_plan["preflight"]
        if (
            plan.plan_id != retained["reconciliation_plan_id"]
            or plan.operation_id != retained["reconciliation_operation_id"]
            or plan.initial_state_id != retained["initial_state_id"]
        ):
            raise ValueError("simulation reconciliation changed after review planning")
        review_path = write_reintroduction_review(plan)
        decisions_path = write_reintroduction_decisions(
            review_path, retained["decisions"]
        )
        result = execute_reconciliation(plan, decisions_path=decisions_path)
        if result.status != "complete":
            raise ValueError(f"reviewed reconciliation ended {result.status}")
    except Exception as exc:
        return _rejected(command, "III_CONFIG_SIM_REVIEW_REJECTED", exc)
    return _accepted(
        command=command,
        code="III_CONFIG_SIM_REVIEW_APPLIED",
        summary="Sealed the complete reintroduction review and reconciled living simulation state.",
        payload={
            "schema": "iii.config-sim-review-result/v1",
            "reconciliation": result.as_dict(),
            "decisions": retained["decisions"],
        },
        operation_id=args._iii_operation_id,
        state="completed",
    )


def _binding(living: Path) -> dict[str, Any]:
    path = living / "state/sim/contract.json"
    if not path.is_file() or path.is_symlink():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def checkpoint_preflight(args: argparse.Namespace) -> dict[str, Any]:
    from iii_drone_configuration import plan_configuration_checkpoint

    _workspace_path, living, checkpoint_root, _operations = _roots(args)
    binding = _binding(living)
    value = plan_configuration_checkpoint(
        writable_state_root=living,
        checkpoint_root=checkpoint_root,
        target_id="sim",
        runtime_profile="sim",
        schema_version=binding.get("schema_version"),
        release_id=binding.get("release_id"),
        manifest_id=binding.get("manifest_id"),
    )
    return {
        "schema": "iii.config-sim-checkpoint-preflight/v1",
        "checkpoint": value,
        "permissions": ["local-configuration-write"],
        "mutations": value["mutations"],
        "tracked_default_mutations": 0,
    }


def checkpoint(args: argparse.Namespace) -> CommandResult:
    command = "iii config sim checkpoint"
    try:
        from iii_drone_configuration import seal_configuration_checkpoint

        _workspace_path, living, checkpoint_root, _operations = _roots(args)
        retained = args._iii_retained_plan["preflight"]["checkpoint"]
        binding = _binding(living)
        value = seal_configuration_checkpoint(
            writable_state_root=living,
            checkpoint_root=checkpoint_root,
            target_id="sim",
            runtime_profile="sim",
            schema_version=binding.get("schema_version"),
            release_id=binding.get("release_id"),
            manifest_id=binding.get("manifest_id"),
        )
        if value["checkpoint_id"] != retained["checkpoint_id"]:
            raise ValueError(
                "living simulation state changed after checkpoint planning"
            )
    except Exception as exc:
        return _rejected(command, "III_CONFIG_SIM_CHECKPOINT_REJECTED", exc)
    return _accepted(
        command=command,
        code="III_CONFIG_SIM_CHECKPOINTED",
        summary="Sealed a recoverable content-addressed simulation checkpoint.",
        payload={"schema": "iii.config-sim-checkpoint-result/v1", "checkpoint": value},
        operation_id=args._iii_operation_id,
        state="completed",
    )


def reset_preflight(args: argparse.Namespace) -> dict[str, Any]:
    from iii_drone_configuration import (
        load_installed_contract,
        plan_configuration_checkpoint,
        verify_configuration_checkpoint,
    )

    _workspace_path, living, checkpoint_root, _operations = _roots(args)
    binding = _binding(living)
    capture = plan_configuration_checkpoint(
        writable_state_root=living,
        checkpoint_root=checkpoint_root,
        target_id="sim",
        runtime_profile="sim",
        schema_version=binding.get("schema_version"),
        release_id=binding.get("release_id"),
        manifest_id=binding.get("manifest_id"),
    )
    restore = None
    if args.restore:
        restore_path = _checkpoint_path(checkpoint_root, args.restore)
        restore = verify_configuration_checkpoint(restore_path)
        if restore.get("target_id") != "sim" or restore.get("profile") != "sim":
            raise ValueError("restore checkpoint is not simulation configuration")
    contract = load_installed_contract(_contract(args)).contract
    return {
        "schema": "iii.config-sim-reset-preflight/v1",
        "source_state": capture,
        "destination": str(living),
        "restore_checkpoint_id": restore.get("checkpoint_id") if restore else None,
        "new_manifest_id": contract.manifest_id,
        "permissions": ["local-configuration-write"],
        "mutations": [str(living), capture["path"]],
        "tracked_default_mutations": 0,
    }


def _replace_tree(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    backup = destination.parent / f".{destination.name}.reset-previous"
    if backup.exists():
        raise ValueError("an interrupted reset backup requires inspection before retry")
    if destination.exists():
        os.replace(destination, backup)
    try:
        os.replace(source, destination)
        descriptor = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except Exception:
        if backup.exists() and not destination.exists():
            os.replace(backup, destination)
        raise
    shutil.rmtree(backup, ignore_errors=True)


def reset(args: argparse.Namespace) -> CommandResult:
    command = "iii config sim reset"
    temporary: Path | None = None
    try:
        from iii_drone_configuration import (
            execute_reconciliation,
            load_installed_contract,
            plan_reconciliation,
            seal_configuration_checkpoint,
            verify_configuration_checkpoint,
        )

        _workspace_path, living, checkpoint_root, operations = _roots(args)
        retained = args._iii_retained_plan["preflight"]
        binding = _binding(living)
        capture = seal_configuration_checkpoint(
            writable_state_root=living,
            checkpoint_root=checkpoint_root,
            target_id="sim",
            runtime_profile="sim",
            schema_version=binding.get("schema_version"),
            release_id=binding.get("release_id"),
            manifest_id=binding.get("manifest_id"),
        )
        if capture["checkpoint_id"] != retained["source_state"]["checkpoint_id"]:
            raise ValueError("living simulation state changed after reset planning")

        temporary = Path(
            tempfile.mkdtemp(prefix=".iii-config-reset-", dir=living.parent)
        )
        replacement = temporary / "state"
        if args.restore:
            checkpoint_path = _checkpoint_path(checkpoint_root, args.restore)
            verified = verify_configuration_checkpoint(checkpoint_path)
            if verified["checkpoint_id"] != retained["restore_checkpoint_id"]:
                raise ValueError("restore checkpoint differs from the retained plan")
            shutil.copytree(
                checkpoint_path,
                replacement,
                ignore=shutil.ignore_patterns("checkpoint.json"),
            )
            for path in replacement.rglob("*"):
                path.chmod(0o750 if path.is_dir() else 0o640)
            replacement.chmod(0o750)
        else:
            replacement.mkdir()
            for retained_name in ("shadows", "contracts"):
                source = living / retained_name
                if source.is_dir() and not source.is_symlink():
                    shutil.copytree(source, replacement / retained_name)
            contract_root = _contract(args)
            contract = load_installed_contract(contract_root).contract
            release_id = _release_id(args, contract.manifest_id)
            plan = plan_reconciliation(
                old_immutable_root=contract_root,
                new_immutable_root=contract_root,
                writable_state_root=replacement,
                operations_root=operations,
                operation_id=args._iii_operation_id,
                runtime_profile="sim",
                target_id="sim",
                old_release_id=release_id,
                new_release_id=release_id,
                mode="simulation",
                purpose="reset",
            )
            result = execute_reconciliation(plan)
            if result.status != "complete":
                raise ValueError(
                    f"reset reconciliation did not complete: {result.status}"
                )
        _replace_tree(replacement, living)
        shutil.rmtree(temporary, ignore_errors=True)
        temporary = None
    except Exception as exc:
        if temporary is not None:
            shutil.rmtree(temporary, ignore_errors=True)
        return _rejected(command, "III_CONFIG_SIM_RESET_REJECTED", exc)
    return _accepted(
        command=command,
        code="III_CONFIG_SIM_RESET",
        summary=(
            "Restored the selected recoverable simulation checkpoint."
            if args.restore
            else "Reset living simulation configuration to the installed tracked default."
        ),
        payload={
            "schema": "iii.config-sim-reset-result/v1",
            "pre_reset_checkpoint": capture,
            "restored_checkpoint_id": args.restore,
            "living_root": str(living),
            "tracked_default_mutations": 0,
        },
        operation_id=args._iii_operation_id,
        state="completed",
    )


def initialize(parser: argparse.ArgumentParser) -> None:
    commands = parser.add_subparsers(dest="config_sim_command", required=True)
    inspect_parser = commands.add_parser(
        "inspect", help="inspect living sim state and its pending reconciliation"
    )
    inspect_parser.set_defaults(func=inspect, _iii_mutating=False)

    checkpoint_parser = commands.add_parser(
        "checkpoint", help="seal a recoverable checkpoint of living sim state"
    )
    checkpoint_parser.set_defaults(
        func=checkpoint,
        _iii_mutating=True,
        _iii_plan_provider=checkpoint_preflight,
    )

    review_parser = commands.add_parser(
        "review", help="seal every reintroduced-key decision and reconcile sim state"
    )
    review_parser.add_argument(
        "--decision",
        action="append",
        default=[],
        metavar="SET:PARAMETER=CHOICE",
        help="choose use_old or use_new_default for one exact review item",
    )
    review_parser.set_defaults(
        func=review,
        _iii_mutating=True,
        _iii_plan_provider=review_preflight,
    )

    reset_parser = commands.add_parser(
        "reset", help="recoverably reset living sim state or restore a checkpoint"
    )
    reset_parser.add_argument(
        "--restore", metavar="CHECKPOINT_ID", help="restore this verified checkpoint"
    )
    reset_parser.set_defaults(
        func=reset,
        _iii_mutating=True,
        _iii_plan_provider=reset_preflight,
    )
