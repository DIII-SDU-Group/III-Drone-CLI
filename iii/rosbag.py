"""CLI surface for the guarded local rosbag recorder helper."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
from typing import Mapping

from .result import CommandResult, Finding, NextAction, Outcome


SCRIPT_DEFAULT = Path(__file__).resolve().parents[3] / "scripts/workspace/iii_rosbag.sh"


def _environment(args: argparse.Namespace) -> Mapping[str, str]:
    return getattr(args, "_iii_environment", os.environ)


def _script(environment: Mapping[str, str]) -> Path:
    configured = environment.get("III_ROSBAG_SCRIPT")
    return Path(configured).expanduser() if configured else SCRIPT_DEFAULT


def _argv(args: argparse.Namespace) -> list[str]:
    action = args.rosbag_action
    command = [str(_script(_environment(args))), action]
    if action == "start":
        if args.recording_id:
            command.extend(("--id", args.recording_id))
        for topic in args.topic:
            command.extend(("--topic", topic))
        if args.include_hidden:
            command.append("--include-hidden")
    elif action == "stop":
        if args.recording_id:
            command.extend(("--id", args.recording_id))
        command.extend(("--timeout", str(args.timeout)))
    elif action == "delete":
        command.append(args.recording_id)
    elif action == "clear":
        command.append("--force")
    return command


def _run(args: argparse.Namespace) -> CommandResult:
    environment = dict(_environment(args))
    command = _argv(args)
    script = Path(command[0])
    if not script.is_file() or not os.access(script, os.X_OK):
        completed = subprocess.CompletedProcess(
            command, 127, "", f"rosbag helper is unavailable: {script}"
        )
    else:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            env=environment,
            text=True,
        )
    action = args.rosbag_action
    success = completed.returncode == 0
    code = f"III_ROSBAG_{action.upper()}_{'COMPLETED' if success else 'FAILED'}"
    return CommandResult(
        command="iii rosbag " + action,
        outcome=Outcome.SUCCESS if success else Outcome.FAILED,
        summary=(
            f"Rosbag {action} completed."
            if success
            else f"Rosbag {action} failed with status {completed.returncode}."
        ),
        code=code,
        findings=(
            ()
            if success
            else (
                Finding(
                    code,
                    completed.stderr.strip()
                    or completed.stdout.strip()
                    or f"script exited with status {completed.returncode}",
                    field="recording",
                ),
            )
        ),
        payload_schema="iii.rosbag-command/v1",
        payload={
            "argv": command,
            "exit_status": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "display": completed.stdout,
        },
        next_actions=(
            NextAction(
                ("iii", "rosbag", "status"),
                "Inspect active recording state and retained artifacts.",
            ),
        ),
    )


def initialize(parser: argparse.ArgumentParser) -> None:
    subparsers = parser.add_subparsers(dest="rosbag_action", required=True)
    for action in ("status", "list"):
        action_parser = subparsers.add_parser(action, help=f"Rosbag {action}")
        action_parser.set_defaults(func=_run, _iii_mutating=False)

    start_parser = subparsers.add_parser("start", help="Start recording ROS topics")
    start_parser.set_defaults(func=_run, _iii_mutating=True)
    start_parser.add_argument("--id", dest="recording_id", default="")
    start_parser.add_argument("--topic", action="append", default=[])
    start_parser.add_argument("--include-hidden", action="store_true")

    stop_parser = subparsers.add_parser("stop", help="Stop the active recording")
    stop_parser.set_defaults(func=_run, _iii_mutating=True)
    stop_parser.add_argument("--id", dest="recording_id", default="")
    stop_parser.add_argument("--timeout", default="10")

    delete_parser = subparsers.add_parser("delete", help="Delete one stopped recording")
    delete_parser.set_defaults(func=_run, _iii_mutating=True)
    delete_parser.add_argument("recording_id")

    clear_parser = subparsers.add_parser("clear", help="Delete every stopped recording")
    clear_parser.set_defaults(func=_run, _iii_mutating=True)
    clear_parser.add_argument(
        "--force",
        action="store_true",
        required=True,
        help="confirm all recordings may be deleted",
    )
