"""Fail-closed routing for native GC commands that control an III runtime."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shlex
import subprocess
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from .result import CommandResult, Finding, NextAction, Outcome


TARGETS = ("sim", "hil", "real", "opti_track")
TARGET_PROFILES = {target: target for target in TARGETS}
ALLOWED_TARGETS = {"dev": {"sim", "hil"}, "deploy": {"real", "opti_track"}}
RUNTIME_PATHS = {"system", "mission", "api", "rosbag"}
CLI_TREE_RELATIVE = "tools/III-Drone-CLI"
REMOTE_WORKSPACE = "/home/iii/ws"
REMOTE_CLI = f"{REMOTE_WORKSPACE}/{CLI_TREE_RELATIVE}/bin/iii"

_TREE_IDENTITY_SCRIPT = r"""
import hashlib, json, os, sys
root = sys.argv[1]
records = []
for directory, subdirectories, filenames in os.walk(root):
    subdirectories[:] = [name for name in subdirectories if name not in {'.git', '__pycache__', 'node_modules', 'dist', 'build', '.pytest_cache'} and not name.endswith('.egg-info')]
    for filename in filenames:
        if filename in {'.git', '.package-lock.json'} or filename.endswith('.pyc'):
            continue
        path = os.path.join(directory, filename)
        if os.path.isfile(path):
            relative = os.path.relpath(path, root).replace(os.sep, '/')
            digest = hashlib.sha256(open(path, 'rb').read()).hexdigest()
            records.append({'path': relative, 'sha256': digest})
records.sort(key=lambda item: item['path'])
encoded = json.dumps(records, sort_keys=True, separators=(',', ':')).encode()
print(json.dumps({'sha256': hashlib.sha256(encoded).hexdigest()}))
""".strip()


class RuntimeRoutingError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def add_target_arguments(parser: argparse.ArgumentParser) -> None:
    """Accept target selection before, between, or after runtime subcommands."""

    parser.add_argument(
        "--runtime-target",
        choices=TARGETS,
        default=None,
        help="select the SIM, HIL, real, or OptiTrack runtime",
    )
    parser.add_argument(
        "--host",
        dest="runtime_host",
        default=None,
        help="override the selected remote runtime hostname or IPv4 address",
    )

    def visit(current: argparse.ArgumentParser, path: tuple[str, ...]) -> None:
        if path and (path[0] in RUNTIME_PATHS or path[:2] == ("config", "sim")):
            current.add_argument(
                "--runtime-target",
                choices=TARGETS,
                default=argparse.SUPPRESS,
                help=argparse.SUPPRESS,
            )
            current.add_argument(
                "--host",
                dest="runtime_host",
                default=argparse.SUPPRESS,
                help=argparse.SUPPRESS,
            )
        for action in current._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, child in action.choices.items():
                    visit(child, (*path, name))

    visit(parser, ())


def is_runtime_path(path: Sequence[str]) -> bool:
    return bool(path) and (
        path[0] in RUNTIME_PATHS or tuple(path[:2]) == ("config", "sim")
    )


def _load_install(environment: Mapping[str, str]) -> tuple[Path, dict[str, Any]] | None:
    configured = environment.get("III_GC_INSTALL_ROOT")
    if not configured:
        return None
    root = Path(configured).expanduser()
    manifest_path = root / "install.json"
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeRoutingError(
            "III_GC_INSTALL_IDENTITY_UNAVAILABLE",
            f"native GC install metadata is missing or invalid at {manifest_path}: {exc}",
        ) from exc
    if not isinstance(value, dict) or value.get("schema") != "iii.gc-install/v1":
        raise RuntimeRoutingError(
            "III_GC_INSTALL_IDENTITY_UNAVAILABLE",
            f"native GC install metadata has an unsupported schema: {manifest_path}",
        )
    profile = value.get("profile")
    if profile not in ALLOWED_TARGETS:
        raise RuntimeRoutingError(
            "III_GC_INSTALL_PROFILE_INVALID",
            f"native GC install profile must be dev or deploy, got {profile!r}",
        )
    environment_profile = environment.get("III_GC_INSTALL_PROFILE")
    if environment_profile and environment_profile != profile:
        raise RuntimeRoutingError(
            "III_GC_INSTALL_PROFILE_CONFLICT",
            "III_GC_INSTALL_PROFILE conflicts with install.json; reinstall or correct the wrapper environment.",
        )
    checkout = value.get("checkout")
    content_hashes = (
        checkout.get("content_sha256") if isinstance(checkout, dict) else None
    )
    cli_hash = (
        content_hashes.get(CLI_TREE_RELATIVE)
        if isinstance(content_hashes, dict)
        else None
    )
    if (
        not isinstance(cli_hash, str)
        or len(cli_hash) != 64
        or any(character not in "0123456789abcdef" for character in cli_hash.lower())
    ):
        raise RuntimeRoutingError(
            "III_GC_INSTALL_IDENTITY_UNAVAILABLE",
            "install.json does not contain the checkout-bound III CLI content hash.",
        )
    return root, value


def _selected_target(
    args: argparse.Namespace,
    environment: Mapping[str, str],
    *,
    install_profile: str,
) -> str:
    command_target = getattr(args, "runtime_target", None)
    environment_target = environment.get("III_RUNTIME_TARGET")
    # A sourced shell profile supplies a default. The operator's explicit
    # per-command selection takes precedence (e.g. field real -> OptiTrack).
    target = command_target or environment_target
    if not target:
        safe_sim_default = (
            install_profile == "dev"
            and environment.get("CLI_CONFIGURATION") == "dev"
            and environment.get("III_SYSTEM_PROFILE") == "sim"
            and environment.get("III_DEFAULT_TARGET") == "sim"
        )
        if safe_sim_default:
            target = "sim"
        else:
            raise RuntimeRoutingError(
                "III_RUNTIME_TARGET_REQUIRED",
                "select a runtime with --runtime-target sim|hil|real|opti_track or set III_RUNTIME_TARGET.",
            )
    if target not in ALLOWED_TARGETS[install_profile]:
        allowed = ", ".join(sorted(ALLOWED_TARGETS[install_profile]))
        raise RuntimeRoutingError(
            "III_RUNTIME_TARGET_PROFILE_MISMATCH",
            f"runtime target {target!r} is not allowed by the native {install_profile} install profile (allowed: {allowed}).",
        )
    requested_profile = getattr(args, "profile", None)
    if requested_profile and requested_profile != TARGET_PROFILES[target]:
        raise RuntimeRoutingError(
            "III_RUNTIME_PROFILE_CONFLICT",
            f"--profile={requested_profile} conflicts with runtime target {target!r}.",
        )
    return target


def _command_target(environment: Mapping[str, str]) -> tuple[str, str]:
    aliases: list[tuple[str, str]] = []
    named_host_profile = environment.get("III_RUNTIME_HOST_PROFILE") in {"field", "hil"}
    for key in (
        "III_SSH_HOST",
        "III_RUNTIME_HOST",
        "III_RUNTIME_API_HOST",
        "III_HIL_PI_ADDRESS",
    ):
        value = environment.get(key, "").strip()
        if (
            key == "III_RUNTIME_API_HOST"
            and value.lower() == "iii.local"
            and not named_host_profile
        ):
            continue
        if value:
            aliases.append((key, value))
    runtime_api_url = environment.get("III_RUNTIME_API_URL", "").strip()
    if runtime_api_url:
        try:
            parsed = urlsplit(runtime_api_url)
            api_host = parsed.hostname
        except ValueError as exc:
            raise RuntimeRoutingError(
                "III_RUNTIME_HOST_INVALID",
                "III_RUNTIME_API_URL is not a valid URL and cannot identify the runtime host.",
            ) from exc
        if not api_host:
            raise RuntimeRoutingError(
                "III_RUNTIME_HOST_INVALID",
                "III_RUNTIME_API_URL must include a hostname when selecting an SSH runtime.",
            )
        if (
            aliases
            or api_host.lower() != "iii.local"
            or environment.get("III_RUNTIME_HOST_PROFILE") in {"field", "hil"}
        ):
            aliases.append(("III_RUNTIME_API_URL", api_host))
    if not aliases:
        return environment.get("III_SSH_USER") or "iii", "iii.local"
    normalized = {value.rstrip(".").lower() for _key, value in aliases}
    if len(normalized) != 1:
        details = ", ".join(f"{key}={value}" for key, value in aliases)
        raise RuntimeRoutingError(
            "III_RUNTIME_HOST_CONFLICT",
            f"runtime host aliases disagree ({details}); correct the environment before routing.",
        )
    host = aliases[0][1]
    if (
        any(character.isspace() for character in host)
        or "@" in host
        or host.startswith("-")
    ):
        raise RuntimeRoutingError(
            "III_RUNTIME_HOST_INVALID",
            "runtime host aliases must contain a hostname or address only; configure SSH user separately with III_SSH_USER.",
        )
    user = environment.get("III_SSH_USER") or "iii"
    return user, host


def with_runtime_host(environment: Mapping[str, str], host: str) -> dict[str, str]:
    """Give an explicit command argument precedence over inherited target aliases."""
    if not host or not host[0].isalnum() or not all(
        character.isascii() and (character.isalnum() or character in ".-")
        for character in host
    ):
        raise RuntimeRoutingError(
            "III_RUNTIME_HOST_INVALID", "--host must be a hostname or IPv4 address."
        )
    selected = dict(environment)
    selected.update({
        "III_HIL_PI_ENDPOINT": host,
        "III_SSH_HOST": host,
        "III_RUNTIME_HOST": host,
        "III_RUNTIME_API_HOST": host,
        "III_RUNTIME_API_URL": f"http://{host}:8765",
    })
    selected.pop("III_HIL_PI_ADDRESS", None)
    return selected


def _json_object(output: str, description: str) -> dict[str, Any]:
    try:
        value = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeRoutingError(
            "III_RUNTIME_IDENTITY_INVALID",
            f"{description} returned invalid identity JSON: {exc}",
        ) from exc
    if not isinstance(value, dict):
        raise RuntimeRoutingError(
            "III_RUNTIME_IDENTITY_INVALID",
            f"{description} returned a non-object identity response.",
        )
    return value


def _run(command: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(command), check=False, text=True, **kwargs)


@dataclass(frozen=True)
class RuntimeRoute:
    install_root: Path
    install_profile: str
    target: str
    route: str
    endpoint: str
    target_host: str
    user: str
    checkout: str
    expected_cli_hash: str
    observed_cli_hash: str

    @property
    def plan(self) -> dict[str, str]:
        return {
            "schema": "iii.runtime-route/v1",
            "runtime_target": self.target,
            "route": self.route,
            "target_host": self.target_host,
            "checkout": self.checkout,
            "checkout_cli_sha256": self.expected_cli_hash,
            "observed_cli_sha256": self.observed_cli_hash,
        }

    def _command(
        self, argv: Sequence[str], *, output: str, mutating: bool
    ) -> list[str]:
        forwarded = _strip_runtime_target(argv)
        if output == "json":
            forwarded.append("--json")
        if mutating:
            # The native host's retained plan already captured the confirmation.
            # Avoid prompting twice; the runtime CLI still records its own plan.
            forwarded.extend(("--confirm", "--non-interactive"))
        return forwarded

    def _execution_command(self, child_args: Sequence[str], *, tty: bool) -> list[str]:
        if self.route == "container":
            setup_and_exec = (
                f"source {shlex.quote(REMOTE_WORKSPACE + '/setup/setup_dev.bash')}; "
                "unset III_GC_INSTALL_ROOT III_GC_INSTALL_PROFILE; "
                "export CLI_CONFIGURATION=dev III_SYSTEM_PROFILE=sim III_ENVIRONMENT_PROFILE=dev; "
                f"exec {shlex.join([REMOTE_CLI, *child_args])}"
            )
            return [
                "docker",
                "exec",
                *(["-i"] if tty else []),
                *(["-t"] if tty else []),
                "--user",
                "iii",
                "--workdir",
                REMOTE_WORKSPACE,
                self.endpoint,
                "env",
                "-u",
                "III_GC_INSTALL_ROOT",
                "-u",
                "III_GC_INSTALL_PROFILE",
                "-u",
                "III_RUNTIME_TARGET",
                "bash",
                "-lc",
                setup_and_exec,
            ]

        setup_profile = {
            "hil": "setup_hil.bash",
            "real": "setup_real.bash",
            "opti_track": "setup_opti_track.bash",
        }[self.target]
        profile_path = f"{REMOTE_WORKSPACE}/setup/{setup_profile}"
        setup_and_exec = (
            "unset III_GC_INSTALL_ROOT III_GC_INSTALL_PROFILE III_RUNTIME_TARGET "
            "III_RUNTIME_API_URL III_RUNTIME_API_HOST; "
            f"source {shlex.quote(profile_path)}; "
            "export CLI_CONFIGURATION=dev; "
            f"export III_SYSTEM_PROFILE={shlex.quote(self.target)}; "
            f"export III_ENVIRONMENT_PROFILE={shlex.quote(self.target)}; "
            f"exec {shlex.join([REMOTE_CLI, *child_args])}"
        )
        return [
            "ssh",
            "-tt" if tty else "-T",
            f"{self.user}@{self.endpoint}",
            "bash -lc " + shlex.quote(setup_and_exec),
        ]

    def execute(
        self,
        argv: Sequence[str],
        *,
        output: str,
        mutating: bool,
        tty: bool = False,
        stream: bool = False,
        stdout_stream=None,
        stderr_stream=None,
        stdin_stream=None,
    ) -> CommandResult | int:
        child_args = self._command(argv, output=output, mutating=mutating)
        command = self._execution_command(child_args, tty=tty)
        if tty or stream:
            return _run(
                command,
                stdin=stdin_stream,
                stdout=stdout_stream,
                stderr=stderr_stream,
            ).returncode
        completed = _run(command, capture_output=True)
        if completed.stderr and stderr_stream is not None:
            stderr_stream.write(completed.stderr)
        if output == "human" and completed.stdout and stdout_stream is not None:
            stdout_stream.write(completed.stdout)
        remote_result: dict[str, Any] | None = None
        if output == "json" and completed.stdout:
            try:
                value = json.loads(completed.stdout)
                if isinstance(value, dict):
                    remote_result = value
            except json.JSONDecodeError:
                remote_result = None
        route_payload: dict[str, Any] = {
            **self.plan,
            "exit_status": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
        if remote_result is not None:
            route_payload["runtime_result"] = remote_result
        if completed.returncode:
            message = f"runtime command exited with status {completed.returncode}"
            return CommandResult(
                command="iii " + " ".join(argv),
                outcome=Outcome.FAILED,
                summary="The selected runtime command failed.",
                code="III_RUNTIME_ROUTE_FAILED",
                target=self.target_host,
                profile=self.target,
                findings=(Finding("III_RUNTIME_ROUTE_FAILED", message),),
                payload_schema="iii.runtime-route-result/v1",
                payload={**route_payload, "display": ""},
                terminal_reason="The routed runtime command failed and its exit status and streams were retained.",
            )
        return CommandResult(
            command="iii " + " ".join(argv),
            outcome=Outcome.SUCCESS,
            summary=f"Runtime command completed on target {self.target_host} ({self.target}).",
            code="III_RUNTIME_ROUTE_COMPLETED",
            target=self.target_host,
            profile=self.target,
            payload_schema="iii.runtime-route-result/v1",
            payload={**route_payload, "display": ""},
            terminal_reason="The selected runtime completed and its result was retained.",
        )


def _strip_runtime_target(argv: Sequence[str]) -> list[str]:
    result: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--runtime-target":
            index += 2
            continue
        if token.startswith("--runtime-target="):
            index += 1
            continue
        if token == "--host":
            index += 2
            continue
        if token.startswith("--host="):
            index += 1
            continue
        result.append(token)
        index += 1
    return result


def _verify_identity(*, route: str, endpoint: str, user: str, expected: str) -> str:
    if route == "container":
        command = [
            "docker",
            "exec",
            "--user",
            "iii",
            "--workdir",
            REMOTE_WORKSPACE,
            endpoint,
            "python3",
            "-c",
            _TREE_IDENTITY_SCRIPT,
            f"{REMOTE_WORKSPACE}/{CLI_TREE_RELATIVE}",
        ]
    else:
        identity_command = shlex.join(
            [
                "python3",
                "-c",
                _TREE_IDENTITY_SCRIPT,
                f"{REMOTE_WORKSPACE}/{CLI_TREE_RELATIVE}",
            ]
        )
        command = ["ssh", "-T", f"{user}@{endpoint}", identity_command]
    completed = _run(command, capture_output=True)
    if completed.returncode:
        detail = (
            completed.stderr.strip()
            or f"identity check exited with status {completed.returncode}"
        )
        raise RuntimeRoutingError(
            "III_RUNTIME_IDENTITY_UNVERIFIABLE",
            f"could not verify the selected runtime CLI checkout: {detail}",
        )
    response = _json_object(completed.stdout, "selected runtime")
    observed = response.get("sha256")
    if not isinstance(observed, str) or observed.lower() != expected.lower():
        raise RuntimeRoutingError(
            "III_RUNTIME_CHECKOUT_MISMATCH",
            "selected runtime CLI content does not match the native GC install checkout "
            f"(expected {expected}, observed {observed!r}).",
        )
    return observed


def prepare_route(
    path: Sequence[str],
    args: argparse.Namespace,
    environment: Mapping[str, str],
) -> RuntimeRoute | None:
    if not is_runtime_path(path):
        return None
    install = _load_install(environment)
    if install is None:
        # Existing direct local and HTTP remote consumers retain their current
        # command paths unless they are using the native GC install wrapper.
        return None
    install_root, metadata = install
    install_profile = str(metadata["profile"])
    target = _selected_target(args, environment, install_profile=install_profile)
    checkout = metadata["checkout"].get("checkout", "")
    expected_hash = metadata["checkout"]["content_sha256"][CLI_TREE_RELATIVE]
    if target == "sim":
        completed = _run(
            [
                "docker",
                "ps",
                "--filter",
                f"label=devcontainer.local_folder={checkout}",
                "--format",
                "{{.ID}}",
            ],
            capture_output=True,
        )
        if completed.returncode:
            raise RuntimeRoutingError(
                "III_RUNTIME_CONTAINER_LOOKUP_FAILED",
                completed.stderr.strip()
                or "could not query Docker for the checkout devcontainer.",
            )
        containers = [
            line.strip() for line in completed.stdout.splitlines() if line.strip()
        ]
        if len(containers) != 1:
            raise RuntimeRoutingError(
                "III_RUNTIME_CONTAINER_UNAVAILABLE",
                f"expected one running devcontainer labeled for checkout {checkout}, found {len(containers)}.",
            )
        route, endpoint = "container", containers[0]
        user, host = "iii", f"devcontainer:{endpoint}"
    else:
        route = "ssh"
        user, endpoint = _command_target(environment)
        host = f"{user}@{endpoint}"
    observed_hash = _verify_identity(
        route=route, endpoint=endpoint, user=user, expected=expected_hash
    )
    return RuntimeRoute(
        install_root=install_root,
        install_profile=install_profile,
        target=target,
        route=route,
        endpoint=endpoint,
        target_host=host,
        user=user,
        checkout=str(checkout),
        expected_cli_hash=expected_hash,
        observed_cli_hash=observed_hash,
    )


def rejection(command_path: Sequence[str], error: RuntimeRoutingError) -> CommandResult:
    command = "iii " + " ".join(command_path)
    return CommandResult(
        command=command,
        outcome=Outcome.REJECTED,
        summary="The selected III runtime route was refused.",
        code=error.code,
        findings=(Finding(error.code, str(error), field="runtime_target"),),
        next_actions=(
            NextAction(
                ("iii", *command_path, "--help"),
                "Review the target and native install profile before retrying.",
            ),
        ),
    )


def add_route_preflight(route: RuntimeRoute):
    return lambda _args: route.plan
