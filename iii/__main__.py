"""Top-level ``iii`` command dispatcher using the universal result contract."""

from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from importlib import import_module
from io import StringIO
import os
import sys
from typing import Mapping, Sequence, TextIO

import argcomplete

from . import runtime_routing
from .runner import (
    ParserSignal,
    ResultArgumentParser,
    add_universal_help,
    extract_universal_options,
    inventory_parser,
    invoke,
    parser_result,
    render,
)


def build_parser() -> argparse.ArgumentParser:
    parser = ResultArgumentParser(prog="iii")
    add_universal_help(parser)
    subparsers = parser.add_subparsers(dest="subcommand")

    system = import_module("iii.system")
    parser_system = subparsers.add_parser(
        "system", help="Commands for system management"
    )
    system.initialize(parser_system)

    parser_config = subparsers.add_parser(
        "config", help="Launches configuration manager"
    )
    config_commands = parser_config.add_subparsers(dest="config_command")
    parser_config_sim = config_commands.add_parser(
        "sim", help="inspect and recover this clone's living simulation configuration"
    )
    import_module("iii.config_sim").initialize(parser_config_sim)

    deploy = import_module("iii.deploy")
    parser_deploy = subparsers.add_parser(
        "deploy", help="Commands for deploying parts of the system"
    )
    deploy.initialize(parser_deploy)

    mission = import_module("iii.mission")
    parser_mission = subparsers.add_parser(
        "mission", help="Commands for installed mission catalogs"
    )
    mission.initialize(parser_mission)

    host = import_module("iii.host")
    parser_host = subparsers.add_parser(
        "host", help="Commands for provisioning and maintaining III hosts"
    )
    host.initialize(parser_host)

    qgc = import_module("iii.qgc")
    parser_qgc = subparsers.add_parser(
        "qgc", help="Manage the pinned host QGroundControl user service"
    )
    qgc.initialize(parser_qgc)

    api = import_module("iii.api")
    parser_api = subparsers.add_parser(
        "api", help="Manage the local III Runtime API system service"
    )
    api.initialize(parser_api)

    rosbag = import_module("iii.rosbag")
    parser_rosbag = subparsers.add_parser(
        "rosbag", help="Manage ROS bag recordings on the selected runtime"
    )
    rosbag.initialize(parser_rosbag)

    px4 = import_module("iii.px4")
    parser_px4 = subparsers.add_parser(
        "px4", help="Inspect the Pi-side PX4 network link"
    )
    px4.initialize(parser_px4)

    runtime_routing.add_target_arguments(parser)
    inventory_parser(parser)
    return parser


def _subparser(
    parser: argparse.ArgumentParser, argv: Sequence[str]
) -> tuple[argparse.ArgumentParser, tuple[str, ...]]:
    current = parser
    path: list[str] = []
    for token in argv:
        selected = None
        for action in current._actions:
            if (
                isinstance(action, argparse._SubParsersAction)
                and token in action.choices
            ):
                selected = action.choices[token]
                break
        if selected is None:
            if token.startswith("-"):
                continue
            break
        current = selected
        path.append(token)
    return current, tuple(path)


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    stdin: TextIO | None = None,
    environment: Mapping[str, str] | None = None,
) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr
    input_stream = sys.stdin if stdin is None else stdin
    env = os.environ if environment is None else environment

    try:
        options, parser_argv = extract_universal_options(raw_argv)
    except ParserSignal as exc:
        result = parser_result(
            argv=raw_argv, help_text="Run 'iii --help' for usage.", error=exc.message
        )
        return render(
            result,
            output=(
                "json"
                if "--json" in raw_argv or "--output=json" in raw_argv
                else "human"
            ),
            stdout=out,
            stderr=err,
        )

    parser = build_parser()
    selected_parser, path = _subparser(parser, parser_argv)
    parser_stdout = StringIO()
    parser_stderr = StringIO()
    try:
        with redirect_stdout(parser_stdout), redirect_stderr(parser_stderr):
            argcomplete.autocomplete(parser)
            args = parser.parse_args(parser_argv)
    except ParserSignal as exc:
        help_text = parser_stdout.getvalue() or selected_parser.format_help()
        error = None if exc.status == 0 else exc.message
        result = parser_result(
            argv=parser_argv, help_text=help_text, error=error, path=path
        )
        return render(
            result,
            output=options.output,
            stdout=out,
            stderr=err,
            diagnostics=parser_stderr.getvalue(),
        )

    spec = getattr(args, "_iii_command_spec", None)
    if spec is None:
        result = parser_result(
            argv=parser_argv,
            help_text=selected_parser.format_help(),
            error=None,
            path=path,
        )
        return render(result, output=options.output, stdout=out, stderr=err)

    # The parser's command spec is authoritative.  A root option with a value
    # (for example ``--runtime-target sim``) can precede the command, where
    # the lightweight help-path scanner cannot identify its subparser.
    path = spec.path

    runtime_host = getattr(args, "runtime_host", None)
    if runtime_host is not None:
        if not runtime_routing.is_runtime_path(path):
            result = parser_result(
                argv=parser_argv,
                help_text=selected_parser.format_help(),
                error="--host selects a runtime only for system, api, config, and rosbag commands.",
                path=path,
            )
            return render(result, output=options.output, stdout=out, stderr=err)
        try:
            env = runtime_routing.with_runtime_host(env, runtime_host)
        except runtime_routing.RuntimeRoutingError as exc:
            result = runtime_routing.rejection(path, exc)
            return render(result, output=options.output, stdout=out, stderr=err)

    if (
        path == ("api", "logs")
        and getattr(args, "follow", False)
        and options.output == "json"
    ):
        result = parser_result(
            argv=parser_argv,
            help_text=selected_parser.format_help(),
            error="iii api logs --follow streams live output and cannot use --json.",
            path=path,
        )
        return render(result, output=options.output, stdout=out, stderr=err)

    try:
        route = runtime_routing.prepare_route(path, args, env)
    except runtime_routing.RuntimeRoutingError as exc:
        result = runtime_routing.rejection(path, exc)
        return render(result, output=options.output, stdout=out, stderr=err)

    invoke_environment = env
    if route is not None:
        args.target = route.target_host
        if getattr(args, "profile", None) is None:
            args.profile = route.target

        interactive = tuple(path) == ("system", "attach") or (
            tuple(path) == ("api", "logs") and bool(getattr(args, "follow", False))
        )
        if interactive:
            tty = bool(getattr(input_stream, "isatty", lambda: False)())
            return route.execute(
                parser_argv,
                output=options.output,
                mutating=spec.mutating,
                tty=tty,
                stream=tuple(path) == ("api", "logs"),
                stdout_stream=out,
                stderr_stream=err,
                stdin_stream=input_stream,
            )

        original_spec = spec
        spec = replace(
            original_spec,
            plan_provider=(
                runtime_routing.add_route_preflight(route)
                if original_spec.mutating
                else None
            ),
        )
        setattr(args, "_iii_command_spec", spec)
        setattr(
            args,
            "func",
            lambda _args: route.execute(
                parser_argv,
                output=options.output,
                mutating=spec.mutating,
                stdout_stream=out,
                stderr_stream=err,
            ),
        )
        native_environment = dict(env)
        native_environment["CLI_CONFIGURATION"] = "host"
        invoke_environment = native_environment

    # Direct attended deployment progress must bypass invoke()'s diagnostic
    # capture so the operator sees stage updates while commands are running.
    # JSON stdout remains a single machine-readable result.
    setattr(
        args,
        "_iii_progress_stream",
        err if options.output == "human" and spec.path == ("deploy", "dev") else None,
    )
    result, diagnostics = invoke(
        args=args,
        spec=spec,
        argv=parser_argv,
        options=options,
        environment=invoke_environment,
        input_stream=input_stream,
        error_stream=err,
    )
    return render(
        result, output=options.output, stdout=out, stderr=err, diagnostics=diagnostics
    )


if __name__ == "__main__":
    raise SystemExit(main())
