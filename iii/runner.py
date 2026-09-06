"""Universal parser, invocation, operation, and rendering machinery."""

from __future__ import annotations

import argparse
import builtins
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import dataclass, replace
import getpass
from io import StringIO
import os
import re
import sys
import tempfile
from typing import Any, Callable, Iterator, Mapping, Sequence, TextIO

from .operation import (
    OperationConflict,
    OperationError,
    OperationStore,
    create_plan,
    default_state_root,
    operation_id as new_operation_id,
)
from .result import CommandResult, Finding, NextAction, Outcome, internal_error_result

SUPPORTED_CONFIGURATIONS = {"host", "container", "remote", "dev"}
REQUIRED_COMMAND_FAMILIES = {
    "system",
    "build",
    "deploy",
    "release",
    "host",
    "gc",
    "qgc",
    "px4",
    "mission",
    "config",
    "capture",
    "logs",
    "records",
    "governance",
    "field",
    "documentation",
    "access",
}


class ParserSignal(RuntimeError):
    def __init__(self, status: int, message: str | None = None):
        super().__init__(message or "")
        self.status = status
        self.message = message or ""


class ResultArgumentParser(argparse.ArgumentParser):
    """Argument parser which delegates exits and errors to the result envelope."""

    def exit(self, status: int = 0, message: str | None = None) -> None:
        if message:
            self._print_message(message, sys.stderr)
        raise ParserSignal(status, message)

    def error(self, message: str) -> None:
        raise ParserSignal(64, message)


class RequiredInput(RuntimeError):
    def __init__(self, field: str, prompt: str):
        super().__init__(prompt)
        self.field = field
        self.prompt = prompt


@dataclass(frozen=True)
class UniversalOptions:
    output: str = "human"
    non_interactive: bool = False
    dry_run: bool = False
    confirm: bool = False
    operation_id: str | None = None
    resume: bool = False


@dataclass(frozen=True)
class CommandSpec:
    path: tuple[str, ...]
    mutating: bool
    interactive: bool = False
    plan_provider: Callable[[argparse.Namespace], Mapping[str, Any]] | None = None
    operation_finalizer: Callable[[argparse.Namespace], None] | None = None

    @property
    def identity(self) -> str:
        return "iii " + " ".join(self.path)


def extract_universal_options(
    argv: Sequence[str],
) -> tuple[UniversalOptions, list[str]]:
    """Extract universal controls from any position without burdening leaf parsers."""

    output = "human"
    non_interactive = False
    dry_run = False
    confirm = False
    identifier: str | None = None
    resume = False
    remaining: list[str] = []
    index = 0
    passthrough = False
    while index < len(argv):
        token = argv[index]
        if passthrough:
            remaining.append(token)
        elif token == "--":
            passthrough = True
            remaining.append(token)
        elif token in {"--json", "--output=json"}:
            output = "json"
        elif token == "--output":
            index += 1
            if index >= len(argv) or argv[index] not in {"human", "json"}:
                raise ParserSignal(64, "--output requires 'human' or 'json'")
            output = argv[index]
        elif token == "--output=human":
            output = "human"
        elif token == "--non-interactive":
            non_interactive = True
        elif token == "--interactive":
            non_interactive = False
        elif token in {"--dry-run", "--plan"}:
            dry_run = True
        elif token in {"--confirm", "--yes"}:
            confirm = True
        elif token == "--resume":
            resume = True
        elif token.startswith("--operation-id="):
            identifier = token.split("=", 1)[1]
        elif token == "--operation-id":
            index += 1
            if index >= len(argv):
                raise ParserSignal(64, "--operation-id requires a value")
            identifier = argv[index]
        else:
            remaining.append(token)
        index += 1
    if resume and identifier is None:
        raise ParserSignal(64, "--resume requires --operation-id")
    return (
        UniversalOptions(output, non_interactive, dry_run, confirm, identifier, resume),
        remaining,
    )


def add_universal_help(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("universal result and operation controls")
    group.add_argument(
        "--output",
        choices=("human", "json"),
        help="render the same result envelope as human text or JSON",
    )
    group.add_argument("--json", action="store_true", help="shortcut for --output=json")
    group.add_argument(
        "--non-interactive",
        action="store_true",
        help="refuse every prompt and return III_REQUIRED_INPUT",
    )
    group.add_argument(
        "--dry-run",
        action="store_true",
        help="retain and render an exact operation plan without mutation",
    )
    group.add_argument(
        "--operation-id", metavar="ID", help="bind or resume durable operation state"
    )
    group.add_argument(
        "--resume", action="store_true", help="resume the exact retained operation plan"
    )
    group.add_argument(
        "--confirm",
        action="store_true",
        help="confirm the exact mutating operation plan",
    )


def _is_mutating(path: tuple[str, ...]) -> bool:
    if not path:
        return False
    if path[0] in {
        "build",
        "deploy",
        "release",
        "host",
        "governance",
        "field",
        "documentation",
    }:
        return True
    if path[0] != "system":
        return False
    if len(path) >= 2 and path[1] in {
        "start",
        "stop",
        "restart",
        "shutdown",
        "boot",
        "kill-session",
    }:
        return True
    return (
        len(path) >= 3
        and path[1] in {"service", "daemon"}
        and path[2] in {"start", "stop", "restart"}
    )


def _is_interactive(path: tuple[str, ...]) -> bool:
    return path in {("config",), ("system", "attach"), ("deploy", "ssh")}


def inventory_parser(
    parser: argparse.ArgumentParser,
) -> dict[tuple[str, ...], CommandSpec]:
    """Annotate every executable leaf and return the coverage inventory."""

    inventory: dict[tuple[str, ...], CommandSpec] = {}

    def visit(current: argparse.ArgumentParser, prefix: tuple[str, ...]) -> None:
        choices: dict[str, argparse.ArgumentParser] = {}
        for action in current._actions:
            if isinstance(action, argparse._SubParsersAction):
                choices.update(action.choices)
        if "func" in current._defaults:
            spec = CommandSpec(
                prefix,
                bool(current._defaults.get("_iii_mutating", _is_mutating(prefix))),
                bool(
                    current._defaults.get("_iii_interactive", _is_interactive(prefix))
                ),
                current._defaults.get("_iii_plan_provider"),
                current._defaults.get("_iii_operation_finalizer"),
            )
            current.set_defaults(_iii_command_spec=spec)
            inventory[prefix] = spec
        if choices:
            for name, child in choices.items():
                visit(child, (*prefix, name))
            return

    visit(parser, ())
    return inventory


def _help_action(path: Sequence[str] = ()) -> NextAction:
    command = ("iii", *path, "--help") if path else ("iii", "--help")
    return NextAction(command, "Inspect valid commands and required arguments.")


def parser_result(
    *,
    argv: Sequence[str],
    help_text: str,
    error: str | None,
    path: Sequence[str] = (),
) -> CommandResult:
    command = "iii" if not argv else "iii " + " ".join(argv)
    if error is None:
        next_path = ("system",) if not path else tuple(path[:-1])
        return CommandResult(
            command=command,
            outcome=Outcome.SUCCESS,
            summary="Command help is available.",
            code="III_HELP",
            payload_schema="iii.help/v1",
            payload={"help": help_text},
            next_actions=(_help_action(next_path),),
        )
    return CommandResult(
        command=command,
        outcome=Outcome.USAGE_ERROR,
        summary="The command arguments are invalid.",
        code="III_USAGE_ERROR",
        findings=(Finding("III_USAGE_ERROR", error, field="argv"),),
        payload_schema="iii.usage-error/v1",
        payload={"help": help_text},
        next_actions=(_help_action(path),),
    )


def _context(
    args: argparse.Namespace,
    environment: Mapping[str, str],
    *,
    command_path: Sequence[str] = (),
) -> tuple[str | None, str | None, str | None]:
    target = next(
        (
            str(getattr(args, name))
            for name in ("target", "host", "entity_id")
            if getattr(args, name, None)
        ),
        None,
    )
    explicit_profile = getattr(args, "profile", None)
    # A remote status/start/stop command acts on the runtime API's current
    # profile. The local shell's profile is only its workstation default and
    # may be unrelated (for example, local sim controlling remote HIL). Never
    # stamp that unrelated value into retained plans or result context.
    inherit_environment_profile = not (
        environment.get("CLI_CONFIGURATION") == "remote"
        and tuple(command_path[:1]) == ("system",)
        and explicit_profile is None
    )
    profile = explicit_profile or (
        environment.get("III_SYSTEM_PROFILE") if inherit_environment_profile else None
    )
    release_id = getattr(args, "release_id", None) or getattr(args, "version", None)
    return (
        target,
        str(profile) if profile else None,
        str(release_id) if release_id else None,
    )


def _action_for(
    spec: CommandSpec,
    *,
    target: str | None,
    profile: str | None,
    operation_id: str | None = None,
) -> NextAction:
    if spec.path and spec.path[0] == "system" and spec.path[1:] != ("status",):
        command = ("iii", "system", "status")
        reason = "Verify the resulting runtime state."
    else:
        command = (
            ("iii", *spec.path[:-1], "--help")
            if len(spec.path) > 1
            else ("iii", spec.path[0], "--help")
        )
        reason = "Inspect related commands and the next operation for this context."
    return NextAction(
        command,
        reason,
        target=target,
        profile=profile,
        operation_id=operation_id,
        arguments={
            key: value
            for key, value in {
                "target": target,
                "profile": profile,
                "operation_id": operation_id,
            }.items()
            if value is not None
        },
    )


def _configuration_failure(
    spec: CommandSpec, environment: Mapping[str, str]
) -> CommandResult | None:
    if not spec.path or spec.path[0] not in {"system", "build", "deploy", "config"}:
        return None
    configuration = environment.get("CLI_CONFIGURATION")
    if configuration in SUPPORTED_CONFIGURATIONS:
        return None
    detail = (
        "CLI_CONFIGURATION is not set"
        if configuration is None
        else f"CLI_CONFIGURATION={configuration!r} is unsupported"
    )
    return CommandResult(
        command=spec.identity,
        outcome=Outcome.REJECTED,
        summary="The III environment profile is not ready.",
        code="III_CONFIGURATION_REQUIRED",
        findings=(
            Finding("III_CONFIGURATION_REQUIRED", detail, field="CLI_CONFIGURATION"),
        ),
        next_actions=(
            NextAction(
                ("bash", "-lc", "source setup/setup_dev.bash && iii --help"),
                "Load a supported III environment before retrying.",
                prerequisites=("Run from the III workspace root.",),
                arguments={"configuration": configuration},
            ),
        ),
    )


@contextmanager
def _prompt_policy(non_interactive: bool) -> Iterator[None]:
    if not non_interactive:
        yield
        return
    original_input = builtins.input
    original_getpass = getpass.getpass

    def reject_input(prompt: str = "") -> str:
        raise RequiredInput("input", prompt or "interactive input")

    def reject_password(
        prompt: str = "Password: ", stream: TextIO | None = None
    ) -> str:
        del stream
        raise RequiredInput("password", prompt)

    builtins.input = reject_input
    getpass.getpass = reject_password
    try:
        yield
    finally:
        builtins.input = original_input
        getpass.getpass = original_getpass


@contextmanager
def _capture_child_stdout(destination: StringIO) -> Iterator[None]:
    """Capture writes which bypass ``sys.stdout``, including child processes."""

    try:
        sys.stdout.flush()
        saved_stdout = os.dup(1)
    except (AttributeError, OSError):
        yield
        return
    try:
        with tempfile.TemporaryFile(mode="w+b") as child_output:
            os.dup2(child_output.fileno(), 1)
            try:
                yield
            finally:
                try:
                    sys.stdout.flush()
                finally:
                    os.dup2(saved_stdout, 1)
            child_output.seek(0)
            destination.write(child_output.read().decode("utf-8", errors="replace"))
    finally:
        os.close(saved_stdout)


def _retained_plan(
    *,
    store: OperationStore,
    identifier: str,
    argv: Sequence[str],
    spec: CommandSpec,
    target: str | None,
    profile: str | None,
    release_id: str | None,
    preflight: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    existing = store.load_plan(identifier)
    if existing is not None:
        expected = {
            "command": spec.identity,
            "argv": list(argv),
            "mutating": spec.mutating,
            "context": {"target": target, "profile": profile, "release_id": release_id},
        }
        if preflight is not None:
            expected["preflight"] = dict(preflight)
        observed = {key: existing.get(key) for key in expected}
        if observed != expected:
            raise OperationConflict(
                "operation ID is bound to a different exact command or context"
            )
        state = store.load_state(identifier)
        if state is None:
            state = store.retain_plan(existing)
        elif state.get("plan_id") != existing.get("plan_id"):
            raise OperationConflict(
                "retained operation state is bound to a different plan"
            )
        return existing, state
    plan = create_plan(
        identifier=identifier,
        argv=argv,
        command=spec.identity,
        mutating=spec.mutating,
        target=target,
        profile=profile,
        release_id=release_id,
        preflight=preflight,
    )
    return plan, store.retain_plan(plan)


def _plan_action(argv: Sequence[str], identifier: str) -> NextAction:
    command = (
        "iii",
        *argv,
        "--operation-id",
        identifier,
        "--confirm",
        "--non-interactive",
    )
    return NextAction(
        command,
        "Apply this exact retained plan.",
        mutating=True,
        prerequisites=("Review the exact argv, context, and retained plan ID.",),
        confirmation_required=True,
        operation_id=identifier,
        arguments={"operation_id": identifier},
    )


def _resume_action(
    argv: Sequence[str], identifier: str, *, target: str | None, profile: str | None
) -> NextAction:
    return NextAction(
        (
            "iii",
            *argv,
            "--operation-id",
            identifier,
            "--resume",
            "--confirm",
            "--non-interactive",
        ),
        "Reattach to the exact retained operation without changing its plan.",
        mutating=True,
        prerequisites=(
            "Verify retained state and external side effects before resuming.",
        ),
        confirmation_required=True,
        target=target,
        profile=profile,
        operation_id=identifier,
        arguments={
            "operation_id": identifier,
            **({"target": target} if target else {}),
            **({"profile": profile} if profile else {}),
        },
    )


def _required_input_result(
    spec: CommandSpec,
    *,
    field: str,
    detail: str,
    action: NextAction,
    identifier: str | None,
    state: str | None,
    target: str | None,
    profile: str | None,
) -> CommandResult:
    return CommandResult(
        command=spec.identity,
        outcome=Outcome.REJECTED,
        summary="Non-interactive execution requires explicit input.",
        code="III_REQUIRED_INPUT",
        findings=(Finding("III_REQUIRED_INPUT", detail, field=field),),
        operation_id=identifier,
        state=state,
        target=target,
        profile=profile,
        next_actions=(action,),
    )


def invoke(
    *,
    args: argparse.Namespace,
    spec: CommandSpec,
    argv: Sequence[str],
    options: UniversalOptions,
    environment: Mapping[str, str] | None = None,
    input_stream: TextIO | None = None,
    error_stream: TextIO | None = None,
) -> tuple[CommandResult, str]:
    """Invoke a leaf through the universal contract and return diagnostics."""

    env = os.environ if environment is None else environment
    stdin = sys.stdin if input_stream is None else input_stream
    prompt_stream = sys.stderr if error_stream is None else error_stream
    target, profile, release_id = _context(args, env, command_path=spec.path)
    config_failure = _configuration_failure(spec, env)
    if config_failure is not None:
        return config_failure, ""

    store = OperationStore(default_state_root(env))
    identifier = options.operation_id
    setattr(args, "_iii_environment", env)
    plan: dict[str, Any] | None = None
    state: dict[str, Any] | None = None
    try:
        if spec.mutating:
            identifier = identifier or new_operation_id()
            setattr(args, "_iii_operation_id", identifier)
            try:
                preflight = (
                    spec.plan_provider(args) if spec.plan_provider is not None else None
                )
            except Exception as exc:
                raise OperationError(f"operation preflight rejected: {exc}") from exc
            plan, state = _retained_plan(
                store=store,
                identifier=identifier,
                argv=argv,
                spec=spec,
                target=target,
                profile=profile,
                release_id=release_id,
                preflight=preflight,
            )
            if state.get("state") == "completed":
                return (
                    CommandResult(
                        command=spec.identity,
                        outcome=Outcome.SUCCESS,
                        summary="The retained operation is already complete; no mutation was repeated.",
                        code="III_OPERATION_ALREADY_COMPLETE",
                        operation_id=identifier,
                        state="completed",
                        target=target,
                        profile=profile,
                        release_id=release_id,
                        evidence=tuple(state.get("evidence", [])),
                        payload_schema="iii.cli-operation-plan/v1",
                        payload={"plan": plan},
                        next_actions=(
                            _action_for(
                                spec,
                                target=target,
                                profile=profile,
                                operation_id=identifier,
                            ),
                        ),
                    ),
                    "",
                )
            if options.dry_run:
                return (
                    CommandResult(
                        command=spec.identity,
                        outcome=Outcome.SUCCESS,
                        summary="The exact operation plan is retained; no mutation was performed.",
                        code="III_OPERATION_PLAN_READY",
                        operation_id=identifier,
                        state="planned",
                        target=target,
                        profile=profile,
                        release_id=release_id,
                        payload_schema="iii.cli-operation-plan/v1",
                        payload={"plan": plan},
                        next_actions=(_plan_action(argv, identifier),),
                    ),
                    "",
                )
            action = _plan_action(argv, identifier)
            if not options.confirm:
                if options.non_interactive:
                    return (
                        _required_input_result(
                            spec,
                            field="confirmation",
                            detail="--confirm is required for this mutating operation",
                            action=action,
                            identifier=identifier,
                            state="planned",
                            target=target,
                            profile=profile,
                        ),
                        "",
                    )
                prompt_stream.write(
                    f"Apply operation {identifier} for {spec.identity}? [y/N] "
                )
                prompt_stream.flush()
                answer = stdin.readline().strip().lower()
                if answer not in {"y", "yes"}:
                    store.transition(
                        identifier,
                        "cancelled",
                        exit_code=130,
                        result_code="III_OPERATION_CANCELLED",
                    )
                    return (
                        CommandResult(
                            command=spec.identity,
                            outcome=Outcome.CANCELLED,
                            summary="The operation was cancelled before mutation.",
                            code="III_OPERATION_CANCELLED",
                            operation_id=identifier,
                            state="cancelled",
                            target=target,
                            profile=profile,
                            release_id=release_id,
                            next_actions=(action,),
                        ),
                        "",
                    )
            store.transition(identifier, "running", increment_attempt=True)
            setattr(args, "_iii_retained_plan", plan)
        elif (
            options.dry_run
            or options.confirm
            or options.resume
            or identifier is not None
        ):
            return (
                CommandResult(
                    command=spec.identity,
                    outcome=Outcome.USAGE_ERROR,
                    summary="Operation controls were supplied to a read-only command.",
                    code="III_OPERATION_NOT_MUTATING",
                    findings=(
                        Finding(
                            "III_OPERATION_NOT_MUTATING",
                            "remove dry-run, confirmation, resume, and operation ID options",
                            field="argv",
                        ),
                    ),
                    next_actions=(_help_action(spec.path),),
                ),
                "",
            )
    except OperationError as exc:
        return (
            CommandResult(
                command=spec.identity,
                outcome=Outcome.REJECTED,
                summary="The retained operation plan cannot be used.",
                code=exc.code,
                findings=(Finding(exc.code, str(exc), field="operation_id"),),
                next_actions=(_help_action(spec.path),),
            ),
            "",
        )

    stdout_buffer = StringIO()
    stderr_buffer = StringIO()
    exit_status = 0
    returned: Any = None
    try:
        # Providers may use the exact environment and retained operation ID,
        # without bypassing the universal parser or result contract.
        setattr(args, "_iii_environment", env)
        setattr(args, "_iii_operation_id", identifier)
        with _capture_child_stdout(stdout_buffer), _prompt_policy(
            options.non_interactive
        ), redirect_stdout(stdout_buffer), redirect_stderr(stderr_buffer):
            returned = args.func(args)
    except SystemExit as exc:
        if isinstance(exc.code, int):
            exit_status = exc.code
        elif exc.code is None:
            exit_status = 0
        else:
            exit_status = 30
            stderr_buffer.write(str(exc.code) + "\n")
    except RequiredInput as exc:
        action = (
            _resume_action(argv, identifier, target=target, profile=profile)
            if identifier
            else _help_action(spec.path)
        )
        if identifier:
            store.transition(
                identifier, "rejected", exit_code=20, result_code="III_REQUIRED_INPUT"
            )
        return (
            _required_input_result(
                spec,
                field=exc.field,
                detail=exc.prompt,
                action=action,
                identifier=identifier,
                state="rejected" if identifier else None,
                target=target,
                profile=profile,
            ),
            stderr_buffer.getvalue(),
        )
    except KeyboardInterrupt:
        if identifier:
            store.transition(
                identifier,
                "interrupted",
                exit_code=130,
                result_code="III_OPERATION_INTERRUPTED",
            )
        return (
            CommandResult(
                command=spec.identity,
                outcome=Outcome.INTERRUPTED,
                summary="The client detached after the last durable operation checkpoint.",
                code="III_OPERATION_INTERRUPTED",
                operation_id=identifier,
                state="interrupted" if identifier else None,
                target=target,
                profile=profile,
                release_id=release_id,
                payload_schema="iii.command-transcript/v1",
                payload={"display": stdout_buffer.getvalue().rstrip()},
                next_actions=(
                    (
                        _resume_action(argv, identifier, target=target, profile=profile)
                        if identifier
                        else _help_action(spec.path)
                    ),
                ),
            ),
            stderr_buffer.getvalue(),
        )
    except BaseException as exc:
        result = internal_error_result(spec.identity, exc)
        if identifier:
            store.transition(
                identifier,
                "failed",
                exit_code=result.exit_code,
                result_code=result.code,
            )
            result = CommandResult(
                **{
                    **result.__dict__,
                    "operation_id": identifier,
                    "state": "failed",
                    "target": target,
                    "profile": profile,
                    "release_id": release_id,
                }
            )
        return result, stderr_buffer.getvalue()

    if isinstance(returned, CommandResult):
        result = returned
        if identifier and result.operation_id is None:
            result = replace(
                result,
                operation_id=identifier,
                state=(
                    "completed"
                    if result.outcome is Outcome.SUCCESS
                    else result.outcome.value
                ),
                target=result.target or target,
                profile=result.profile or profile,
                release_id=result.release_id or release_id,
            )
    else:
        if isinstance(returned, bool):
            exit_status = 0 if returned else 30
        elif isinstance(returned, int):
            exit_status = returned
        code_stem = re.sub(r"[^A-Z0-9]+", "_", "_".join(spec.path).upper()).strip("_")
        outcome = Outcome.SUCCESS if exit_status == 0 else Outcome.FAILED
        result = CommandResult(
            command=spec.identity,
            outcome=outcome,
            summary=(
                f"{spec.identity} completed."
                if exit_status == 0
                else f"{spec.identity} failed."
            ),
            code=f"III_{code_stem}_{'COMPLETED' if exit_status == 0 else 'FAILED'}",
            findings=(
                ()
                if exit_status == 0
                else (
                    Finding(
                        f"III_{code_stem}_FAILED",
                        f"legacy handler exited with status {exit_status}",
                    ),
                )
            ),
            operation_id=identifier,
            state=(
                ("completed" if exit_status == 0 else "failed") if identifier else None
            ),
            target=target,
            profile=profile,
            release_id=release_id,
            payload_schema="iii.command-transcript/v1",
            payload={"display": stdout_buffer.getvalue().rstrip()},
            next_actions=(
                _action_for(
                    spec, target=target, profile=profile, operation_id=identifier
                ),
            ),
        )
    if identifier:
        state_name = {
            Outcome.SUCCESS: "completed",
            Outcome.WARNING: "warning",
            Outcome.REJECTED: "rejected",
            Outcome.FAILED: "failed",
            Outcome.PARTIAL: "partial",
            Outcome.INTERRUPTED: "interrupted",
            Outcome.CANCELLED: "cancelled",
            Outcome.USAGE_ERROR: "rejected",
            Outcome.INTERNAL_ERROR: "failed",
        }[result.outcome]
        store.transition(
            identifier,
            state_name,
            exit_code=result.exit_code,
            result_code=result.code,
            evidence=result.evidence,
        )
        if spec.operation_finalizer is not None:
            try:
                spec.operation_finalizer(args)
            except Exception as exc:
                result = replace(
                    result,
                    outcome=Outcome.WARNING,
                    summary=(
                        f"{result.summary} The operation postcondition could not "
                        "be finalized."
                    ),
                    code="III_OPERATION_POSTCONDITION_FAILED",
                    findings=(
                        *result.findings,
                        Finding(
                            "III_OPERATION_POSTCONDITION_FAILED",
                            str(exc),
                        ),
                    ),
                )
    return result, stderr_buffer.getvalue()


def render(
    result: CommandResult,
    *,
    output: str,
    stdout: TextIO,
    stderr: TextIO,
    diagnostics: str = "",
) -> int:
    if diagnostics:
        stderr.write(diagnostics)
        if not diagnostics.endswith("\n"):
            stderr.write("\n")
    stdout.write(result.render_json() if output == "json" else result.render_human())
    stdout.write("\n")
    stdout.flush()
    return result.exit_code
