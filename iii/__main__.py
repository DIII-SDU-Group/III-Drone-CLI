"""Top-level ``iii`` command dispatcher using the universal result contract."""

from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
from importlib import import_module
from io import StringIO
import os
import sys
from typing import Mapping, Sequence, TextIO

import argcomplete

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


def _run_config() -> None:
    import_module("iii.config").run()


def build_parser() -> argparse.ArgumentParser:
    parser = ResultArgumentParser(prog="iii")
    add_universal_help(parser)
    subparsers = parser.add_subparsers(dest="subcommand")

    system = import_module("iii.system")
    parser_system = subparsers.add_parser("system", help="Commands for system management")
    system.initialize(parser_system)

    parser_config = subparsers.add_parser("config", help="Launches configuration manager")
    parser_config.set_defaults(func=lambda _args: _run_config(), action="run")

    build = import_module("iii.build")
    parser_build = subparsers.add_parser("build", help="Commands for building parts of the system")
    build.initialize(parser_build)

    deploy = import_module("iii.deploy")
    parser_deploy = subparsers.add_parser("deploy", help="Commands for deploying parts of the system")
    deploy.initialize(parser_deploy)

    release = import_module("iii.release")
    parser_release = subparsers.add_parser("release", help="Commands for qualified releases")
    release.initialize(parser_release)

    mission = import_module("iii.mission")
    parser_mission = subparsers.add_parser("mission", help="Commands for installed mission catalogs")
    mission.initialize(parser_mission)

    inventory_parser(parser)
    return parser


def _subparser(parser: argparse.ArgumentParser, argv: Sequence[str]) -> tuple[argparse.ArgumentParser, tuple[str, ...]]:
    current = parser
    path: list[str] = []
    for token in argv:
        selected = None
        for action in current._actions:
            if isinstance(action, argparse._SubParsersAction) and token in action.choices:
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
        result = parser_result(argv=raw_argv, help_text="Run 'iii --help' for usage.", error=exc.message)
        return render(result, output=("json" if "--json" in raw_argv or "--output=json" in raw_argv else "human"), stdout=out, stderr=err)

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
        result = parser_result(argv=parser_argv, help_text=help_text, error=error, path=path)
        return render(result, output=options.output, stdout=out, stderr=err, diagnostics=parser_stderr.getvalue())

    spec = getattr(args, "_iii_command_spec", None)
    if spec is None:
        result = parser_result(argv=parser_argv, help_text=selected_parser.format_help(), error=None, path=path)
        return render(result, output=options.output, stdout=out, stderr=err)

    result, diagnostics = invoke(
        args=args,
        spec=spec,
        argv=parser_argv,
        options=options,
        environment=env,
        input_stream=input_stream,
        error_stream=err,
    )
    return render(result, output=options.output, stdout=out, stderr=err, diagnostics=diagnostics)


if __name__ == "__main__":
    raise SystemExit(main())
