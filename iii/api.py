"""Local systemd lifecycle for the III Runtime API service."""

from __future__ import annotations

import argparse
import os
import subprocess
from typing import Mapping, Sequence

from .result import CommandResult, Finding, NextAction, Outcome


SERVICE_DEFAULT = "iii-runtime-api.service"


def _environment(args: argparse.Namespace) -> Mapping[str, str]:
    return getattr(args, "_iii_environment", os.environ)


def _service(environment: Mapping[str, str]) -> str:
    return environment.get("III_RUNTIME_API_SERVICE", SERVICE_DEFAULT)


def _run(command: Sequence[str], environment: Mapping[str, str], *, follow=False):
    if follow:
        return subprocess.run(
            list(command), check=False, env=dict(environment), text=True
        )
    return subprocess.run(
        list(command),
        check=False,
        capture_output=True,
        env=dict(environment),
        text=True,
    )


def _result(
    action: str,
    service: str,
    completed: subprocess.CompletedProcess[str],
    *,
    output: str = "",
    error: str = "",
) -> CommandResult:
    success = completed.returncode == 0
    code = f"III_API_{action.upper()}_{'COMPLETED' if success else 'FAILED'}"
    return CommandResult(
        command=f"iii api {action}",
        outcome=Outcome.SUCCESS if success else Outcome.FAILED,
        summary=(
            f"Runtime API service {action} completed."
            if success
            else f"Runtime API service {action} failed with status {completed.returncode}."
        ),
        code=code,
        findings=(
            ()
            if success
            else (
                Finding(
                    code,
                    error.strip()
                    or output.strip()
                    or f"command exited with status {completed.returncode}",
                    field="service",
                ),
            )
        ),
        payload_schema="iii.runtime-api-service/v1",
        payload={
            "service": service,
            "exit_status": completed.returncode,
            "stdout": output,
            "stderr": error,
            "display": output,
        },
        terminal_reason=(
            "The requested service action completed."
            if success
            else "The requested service action failed; inspect the retained output."
        ),
    )


def _execute(args: argparse.Namespace) -> CommandResult | int:
    action = args.api_action
    environment = _environment(args)
    service = _service(environment)
    if action in {"start", "stop", "restart"}:
        command = ["sudo", "-n", "systemctl", action, service]
    elif action == "status":
        command = ["systemctl", "is-active", service]
    else:
        follow = bool(getattr(args, "follow", False))
        command = ["sudo", "-n", "journalctl", "-u", service]
        if follow:
            command.append("--follow")
        else:
            command.extend(("--no-pager", "-n", "200"))
    if action == "logs" and getattr(args, "follow", False):
        completed = _run(command, environment, follow=True)
        return completed.returncode
    completed = _run(command, environment)
    return _result(
        action,
        service,
        completed,
        output=completed.stdout,
        error=completed.stderr,
    )


def initialize(parser: argparse.ArgumentParser) -> None:
    subparsers = parser.add_subparsers(dest="api_action", required=True)
    for action in ("start", "stop", "restart", "status", "logs"):
        action_parser = subparsers.add_parser(
            action, help=f"Runtime API service {action}"
        )
        action_parser.set_defaults(
            func=_execute,
            _iii_mutating=action in {"start", "stop", "restart"},
        )
        if action == "logs":
            action_parser.add_argument(
                "--follow",
                action="store_true",
                help="follow service logs interactively",
            )
