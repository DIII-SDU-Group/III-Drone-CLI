"""Direct, attended development deployment for the research aircraft.

This is deliberately a normal SSH/rsync workflow.  It is not a release
transport, receiver protocol, authorization token, or qualification gate.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import subprocess
from typing import Any, Mapping, Sequence

from .result import CommandResult, Finding, NextAction, Outcome


def _workspace() -> Path:
    current = Path.cwd().resolve()
    for candidate in (current, *current.parents):
        if (candidate / "deployment").is_dir() and (candidate / "src").is_dir():
            return candidate
    raise ValueError("run this command from the III workspace or one of its children")


def _source_paths(workspace: Path, requested: Sequence[str]) -> tuple[Path, ...]:
    raw_paths = tuple(requested) or ("src", "setup", "tools", "deployment")
    resolved: list[Path] = []
    for raw in raw_paths:
        candidate = (workspace / raw).resolve()
        try:
            candidate.relative_to(workspace)
        except ValueError as exc:
            raise ValueError(f"--path must remain inside {workspace}: {raw}") from exc
        if not candidate.exists():
            raise ValueError(f"deployment path does not exist: {raw}")
        resolved.append(candidate)
    return tuple(resolved)


def _receipt_root(environment: Mapping[str, str], workspace: Path) -> Path:
    configured = environment.get("III_DEVELOPER_DEPLOY_RECEIPTS")
    if configured:
        return Path(configured).expanduser().resolve()
    state_home = Path(environment.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    return state_home / "iii" / "developer-deployments"


def _write_receipt(
    root: Path, receipt: Mapping[str, Any]
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    destination = root / f"developer-deploy-{stamp}.json"
    destination.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return destination


def _run(command: Sequence[str], *, dry_run: bool) -> dict[str, Any]:
    if dry_run:
        return {"command": list(command), "returncode": None, "stdout": "", "stderr": ""}
    completed = subprocess.run(command, check=False, text=True, capture_output=True)
    return {
        "command": list(command),
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def _remote_command(target: str, command: str) -> list[str]:
    return ["ssh", target, command]


def deploy(args: argparse.Namespace) -> CommandResult:
    """Synchronize an editable workspace using ordinary SSH and rsync."""

    environment = getattr(args, "_iii_environment", os.environ)
    dry_run = bool(getattr(args, "_iii_dry_run", False))
    workspace = _workspace()
    try:
        sources = _source_paths(workspace, args.path)
    except ValueError as exc:
        return CommandResult(
            command="iii deploy dev",
            outcome=Outcome.USAGE_ERROR,
            summary="The requested developer deployment paths are invalid.",
            code="III_DEVELOPER_DEPLOY_USAGE_ERROR",
            findings=(Finding("III_DEVELOPER_DEPLOY_USAGE_ERROR", str(exc), field="path"),),
        )

    target = f"{args.user}@{args.host}"
    remote_workspace = args.remote_workspace.rstrip("/")
    if not remote_workspace.startswith("/"):
        return CommandResult(
            command="iii deploy dev",
            outcome=Outcome.USAGE_ERROR,
            summary="The remote workspace must be an absolute path.",
            code="III_DEVELOPER_DEPLOY_USAGE_ERROR",
            findings=(
                Finding(
                    "III_DEVELOPER_DEPLOY_USAGE_ERROR",
                    "--remote-workspace must be absolute",
                    field="remote_workspace",
                ),
            ),
        )

    results: list[dict[str, Any]] = []
    setup = _remote_command(target, f"mkdir -p -- {shlex.quote(remote_workspace)}")
    results.append(_run(setup, dry_run=dry_run))
    if results[-1]["returncode"] not in {0, None}:
        return _result(args, workspace, target, remote_workspace, sources, dry_run, results)

    for source in sources:
        relative = source.relative_to(workspace).as_posix()
        destination = f"{target}:{remote_workspace}/{relative}"
        command = ["rsync", "-az", "--itemize-changes"]
        if args.mirror:
            command.append("--delete")
        command.extend(
            [
            "--exclude=.git",
            "--exclude=__pycache__",
            "--exclude=.pytest_cache",
            "--exclude=*.pyc",
            "--exclude=build",
            "--exclude=install",
            "--exclude=log",
            ]
        )
        if source.is_dir():
            command.extend([f"{source}/", f"{destination}/"])
        else:
            command.extend([str(source), destination])
        results.append(_run(command, dry_run=dry_run))
        if results[-1]["returncode"] not in {0, None}:
            return _result(args, workspace, target, remote_workspace, sources, dry_run, results)

    if args.build:
        build_command = (
            "set -e; "
            "source /opt/ros/jazzy/setup.bash; "
            f"cd {shlex.quote(remote_workspace)}; "
            "python3 -m venv --system-site-packages .venv; "
            ".venv/bin/python -m pip install --upgrade -e src/III-Drone-Runtime; "
            "if [ ! -f /etc/ros/rosdep/sources.list.d/20-default.list ]; then sudo rosdep init; fi; "
            "if [ ! -d \"$HOME/.ros/rosdep/sources.cache\" ]; then rosdep update; fi; "
            "rosdep install --from-paths src --ignore-src --rosdistro jazzy -r -y; "
            "colcon build --base-paths src --packages-skip iii_drone_simulation --symlink-install "
            "--cmake-args -DBUILD_TESTING=OFF -DCMAKE_BUILD_TYPE=Debug "
            "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON"
        )
        results.append(_run(_remote_command(target, build_command), dry_run=dry_run))
        if results[-1]["returncode"] not in {0, None}:
            return _result(args, workspace, target, remote_workspace, sources, dry_run, results)

    if args.restart:
        restart_command = (
            "set -e; "
            f"sudo install -m 0644 {shlex.quote(remote_workspace)}/deployment/systemd/iii-system-daemon.service "
            "/etc/systemd/system/iii-system-daemon.service; "
            f"sudo install -m 0644 {shlex.quote(remote_workspace)}/deployment/systemd/iii-runtime-api.service "
            "/etc/systemd/system/iii-runtime-api.service; "
            "sudo systemctl daemon-reload; "
            "sudo systemctl restart iii-system-daemon.service iii-runtime-api.service"
        )
        results.append(_run(_remote_command(target, restart_command), dry_run=dry_run))

    return _result(args, workspace, target, remote_workspace, sources, dry_run, results)


def status(args: argparse.Namespace) -> CommandResult:
    """Inspect the ordinary systemd services over the developer SSH account."""

    target = f"{args.user}@{args.host}"
    result = _run(
        _remote_command(
            target,
            "systemctl is-active iii-system-daemon.service iii-runtime-api.service",
        ),
        dry_run=False,
    )
    active = result["returncode"] == 0
    return CommandResult(
        command="iii deploy status",
        outcome=Outcome.SUCCESS if active else Outcome.WARNING,
        summary=(
            "The editable-workspace runtime services are active."
            if active
            else "One or more editable-workspace runtime services are inactive."
        ),
        code="III_DEVELOPER_DEPLOY_STATUS_ACTIVE" if active else "III_DEVELOPER_DEPLOY_STATUS_INACTIVE",
        target=target,
        payload_schema="iii.developer-deploy-status/v1",
        payload={"command": result},
        next_actions=(
            NextAction(
                ("iii", "deploy", "dev", "--host", args.host, "--build", "--restart"),
                "Synchronize, build, and restart the editable workspace.",
                target=target,
                mutating=True,
            ),
        ),
    )


def _result(
    args: argparse.Namespace,
    workspace: Path,
    target: str,
    remote_workspace: str,
    sources: Sequence[Path],
    dry_run: bool,
    results: Sequence[Mapping[str, Any]],
) -> CommandResult:
    failed = next((entry for entry in results if entry["returncode"] not in {0, None}), None)
    receipt = {
        "schema": "iii.developer-deploy-receipt/v1",
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "dry_run": dry_run,
        "workspace": str(workspace),
        "target": target,
        "remote_workspace": remote_workspace,
        "paths": [str(path.relative_to(workspace)) for path in sources],
        "mirror": bool(args.mirror),
        "build": bool(args.build),
        "restart": bool(args.restart),
        "commands": list(results),
    }
    receipt_path = _write_receipt(
        _receipt_root(getattr(args, "_iii_environment", os.environ), workspace), receipt
    )
    next_action = NextAction(
        ("iii", "deploy", "dev", "--host", args.host),
        "Synchronize the next editable change directly to the Pi.",
        target=target,
        mutating=True,
    )
    if failed is not None:
        return CommandResult(
            command="iii deploy dev",
            outcome=Outcome.FAILED,
            summary="The direct developer deployment stopped at a failed remote command.",
            code="III_DEVELOPER_DEPLOY_FAILED",
            target=target,
            findings=(
                Finding(
                    "III_DEVELOPER_DEPLOY_FAILED",
                    f"command exited {failed['returncode']}: {' '.join(failed['command'])}",
                ),
            ),
            evidence=(str(receipt_path),),
            payload_schema="iii.developer-deploy-receipt/v1",
            payload={"receipt": str(receipt_path), "commands": list(results)},
            next_actions=(next_action,),
        )
    return CommandResult(
        command="iii deploy dev",
        outcome=Outcome.SUCCESS,
        summary=(
            "Developer deployment preview completed; no remote command was run."
            if dry_run
            else "Developer workspace synchronized directly over SSH."
        ),
        code="III_DEVELOPER_DEPLOY_PREVIEW" if dry_run else "III_DEVELOPER_DEPLOY_COMPLETED",
        target=target,
        evidence=(str(receipt_path),),
        payload_schema="iii.developer-deploy-receipt/v1",
        payload={"receipt": str(receipt_path), "commands": list(results)},
        next_actions=(next_action,),
    )
