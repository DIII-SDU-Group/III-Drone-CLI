"""Developer-first deployment commands for the research aircraft."""

from __future__ import annotations

import argparse

from . import developer_deploy


def initialize(parser: argparse.ArgumentParser) -> None:
    subparsers = parser.add_subparsers(dest="deploy_command")

    developer_parser = subparsers.add_parser(
        "dev",
        help="synchronize an editable workspace directly over ordinary SSH and rsync",
    )
    developer_parser.add_argument("--host", default="iii.local", help="Pi hostname or IP")
    developer_parser.add_argument("--user", default="iii", help="interactive SSH user")
    developer_parser.add_argument(
        "--remote-workspace", default="/home/iii/ws", help="editable remote workspace"
    )
    developer_parser.add_argument(
        "--path",
        action="append",
        default=[],
        help="workspace-relative path to synchronize (repeatable; defaults to src, setup, tools)",
    )
    developer_parser.add_argument(
        "--mirror",
        action="store_true",
        help="delete remote files absent from each selected local path",
    )
    developer_parser.add_argument("--build", action="store_true", help="build after synchronization")
    developer_parser.add_argument(
        "--restart",
        action="store_true",
        help="restart III runtime services after synchronization/build",
    )
    developer_parser.set_defaults(
        func=developer_deploy.deploy,
        _iii_mutating=False,
        _iii_direct_mutation=True,
    )

    status_parser = subparsers.add_parser(
        "status", help="inspect editable-workspace runtime services over ordinary SSH"
    )
    status_parser.add_argument("--host", default="iii.local", help="Pi hostname or IP")
    status_parser.add_argument("--user", default="iii", help="interactive SSH user")
    status_parser.set_defaults(func=developer_deploy.status, _iii_mutating=False)
