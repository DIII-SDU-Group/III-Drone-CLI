"""System-management command wiring for the III CLI."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import time

CLI_CONFIGURATION = os.getenv("CLI_CONFIGURATION")

if CLI_CONFIGURATION is None:
    print("CLI_CONFIGURATION environment variable is not set. Have you sourced the setup scripts?")
    exit(1)

if CLI_CONFIGURATION not in ["host", "container", "remote", "dev"]:
    print('Invalid configuration. Please set CLI_CONFIGURATION to "host", "container", "remote", or "dev"')
    exit(1)

if CLI_CONFIGURATION in ["host", "remote"]:
    from .container_manager import ContainerManager
else:
    from .system_client import DaemonClient
    from .tmux_handler import TmuxHandler


def _profile_name() -> str:
    return os.environ.get("III_SYSTEM_PROFILE", "sim")


def _session_name() -> str:
    return f"iii_{_profile_name()}"


def _local_client() -> "DaemonClient":
    return DaemonClient()


def _host_forward(command: str, args: list[str]) -> bool:
    container_manager = ContainerManager()
    return container_manager.execute_cli(command, args)


def _filter_args(parts: list[str]) -> list[str]:
    return [part for part in parts if part]


def _ensure_local_daemon() -> "DaemonClient":
    client = _local_client()
    client.ensure_running()
    return client


def SelectNodesCompleter(**kwargs):
    del kwargs
    try:
        from iii_drone_supervision.system_spec import get_system_profile

        return list(get_system_profile(_profile_name()).build_supervision_config()["managed_nodes"].keys())
    except Exception:
        return []


def _tail_latest_log(log_dir: str, follow: bool) -> int:
    directory = Path(log_dir)
    while True:
        candidates = sorted(directory.glob("*.log"))
        if candidates:
            target = str(candidates[-1])
            cmd = ["tail", "-f", target] if follow else ["tail", "-n", "200", target]
            return subprocess.run(cmd, check=False).returncode
        if not follow:
            print(f"No log files found in {directory}")
            return 1
        print(f"Waiting for log files in {directory} ...")
        time.sleep(1.0)


def start(args):
    if CLI_CONFIGURATION in ["host", "remote"]:
        success = _host_forward(
            "/home/iii/.local/bin/iii system start",
            _filter_args(
                [
                    "--skip-activate" if args.skip_activate else "",
                    "--include-dependencies" if args.include_dependencies else "",
                    "--select-nodes" if args.select_nodes else "",
                    *args.select_nodes,
                ]
            ),
        )
        exit(0 if success else 1)

    client = _local_client()
    if not client.ping():
        print('System daemon not running. Use "iii system boot" first.')
        exit(1)
    result = client.start(
        activate=not args.skip_activate,
        select_nodes=args.select_nodes,
        include_dependencies=args.include_dependencies,
    )
    exit(0 if result["success"] else 1)


def stop(args):
    if CLI_CONFIGURATION in ["host", "remote"]:
        success = _host_forward(
            "/home/iii/.local/bin/iii system stop",
            _filter_args(
                [
                    "--skip-cleanup" if args.skip_cleanup else "",
                    "--include-dependencies" if args.include_dependencies else "",
                    "--select-nodes" if args.select_nodes else "",
                    *args.select_nodes,
                ]
            ),
        )
        exit(0 if success else 1)

    client = _local_client()
    if not client.ping():
        print('System daemon not running. Use "iii system boot" first.')
        exit(1)
    result = client.stop(
        cleanup=not args.skip_cleanup,
        select_nodes=args.select_nodes,
        include_dependencies=args.include_dependencies,
    )
    exit(0 if result["success"] else 1)


def restart(args):
    if CLI_CONFIGURATION in ["host", "remote"]:
        success = _host_forward(
            "/home/iii/.local/bin/iii system restart",
            _filter_args(
                [
                    "--cold" if args.cold else "",
                    "--include-dependencies" if args.include_dependencies else "",
                    "--select-nodes" if args.select_nodes else "",
                    *args.select_nodes,
                ]
            ),
        )
        exit(0 if success else 1)

    client = _local_client()
    if not client.ping():
        print('System daemon not running. Use "iii system boot" first.')
        exit(1)
    result = client.restart(
        cold=args.cold,
        select_nodes=args.select_nodes,
        include_dependencies=args.include_dependencies,
    )
    exit(0 if result["success"] else 1)


def status(args):
    if CLI_CONFIGURATION in ["host", "remote"]:
        success = _host_forward(
            "/home/iii/.local/bin/iii system status",
            _filter_args(["--watch" if args.watch else ""]),
        )
        exit(0 if success else 1)

    client = _local_client()
    if not client.ping():
        print("System daemon not running.")
        exit(1)

    while True:
        result = client.status()
        print(f"Booted: {result['booted']}")
        print(f"Profile: {result.get('profile')}")
        print("\nManaged nodes:")
        for key, state in sorted(result["managed_nodes"].items()):
            print(f"  {key}: {state}")
        print("\nProcesses:")
        for key, state in sorted(result["processes"].items()):
            alive = "alive" if state["alive"] else "dead"
            print(f"  {key}: {alive} (starts={state['start_count']}, exits={state['exit_count']})")
        if not args.watch:
            exit(0)
        time.sleep(1.0)
        print("\033[2J\033[H", end="")


def shutdown(args):
    if CLI_CONFIGURATION in ["host", "remote"]:
        success = _host_forward(
            "/home/iii/.local/bin/iii system shutdown",
            _filter_args(
                [
                    "--kill-session" if args.kill_session else "",
                    "--include-dependencies" if args.include_dependencies else "",
                    "--select-nodes" if args.select_nodes else "",
                    *args.select_nodes,
                ]
            ),
        )
        exit(0 if success else 1)

    client = _local_client()
    if not client.ping():
        print("System daemon not running.")
        exit(1)
    result = client.shutdown(
        select_nodes=args.select_nodes,
        include_dependencies=args.include_dependencies,
    )
    if args.kill_session:
        TmuxHandler().kill_session(_session_name())
    exit(0 if result["success"] else 1)


def boot(args):
    if CLI_CONFIGURATION in ["host", "remote"]:
        success = _host_forward(
            "/home/iii/.local/bin/iii system boot",
            _filter_args(["--attach" if args.attach else ""]),
        )
        exit(0 if success else 1)

    client = _ensure_local_daemon()
    response = client.boot(_profile_name())
    tmux_handler = TmuxHandler()
    session_spec = response["tmux"]
    if not tmux_handler.session_running(session_spec["session_name"]):
        success = tmux_handler.start(session_spec, attach=args.attach)
        exit(0 if success else 1)
    if args.attach:
        success = tmux_handler.attach(session_spec["session_name"])
        exit(0 if success else 1)
    print('System already booted. Use "iii system attach" to attach to the tmux session.')
    exit(1)


def attach(args):
    del args
    if CLI_CONFIGURATION in ["host", "remote"]:
        success = _host_forward("/home/iii/.local/bin/iii system attach", [])
        exit(0 if success else 1)

    success = TmuxHandler().attach(_session_name())
    exit(0 if success else 1)


def list_nodes(args):
    del args
    if CLI_CONFIGURATION in ["host", "remote"]:
        success = _host_forward("/home/iii/.local/bin/iii system list-nodes", [])
        exit(0 if success else 1)

    client = _local_client()
    if not client.ping():
        print("System daemon not running.")
        exit(1)
    for node in client.list_nodes():
        print(node)
    exit(0)


def kill_session(args):
    del args
    if CLI_CONFIGURATION in ["host", "remote"]:
        success = _host_forward("/home/iii/.local/bin/iii system kill-session", [])
        exit(0 if success else 1)

    success = TmuxHandler().kill_session(_session_name())
    exit(0 if success else 1)


def logs(args):
    if CLI_CONFIGURATION in ["host", "remote"]:
        success = _host_forward(
            f"/home/iii/.local/bin/iii system logs {args.entity_id}",
            _filter_args(["--follow" if args.follow else ""]),
        )
        exit(0 if success else 1)

    client = _local_client()
    if not client.ping():
        print("System daemon not running.")
        exit(1)
    exit(_tail_latest_log(client.log_dir(args.entity_id), args.follow))


def initialize(parser):
    subparsers = parser.add_subparsers(
        dest="action",
        title="Actions",
        description="Available actions for system management",
    )

    start_parser = subparsers.add_parser("start", help="Starts the system")
    start_parser.set_defaults(func=start)
    start_parser.add_argument(
        "--skip-activate",
        action="store_true",
        help="Will only configure the system without activating it.",
    )
    start_parser.add_argument(
        "--select-nodes",
        nargs="+",
        default=[],
        help="Start the specified nodes.",
    ).completer = SelectNodesCompleter
    start_parser.add_argument(
        "--include-dependencies",
        action="store_true",
        help="Will also start selected nodes dependencies.",
    )

    stop_parser = subparsers.add_parser("stop", help="Stops the system")
    stop_parser.set_defaults(func=stop)
    stop_parser.add_argument(
        "--skip-cleanup",
        action="store_true",
        help="Will only deactivate the system without cleaning it up.",
    )
    stop_parser.add_argument(
        "--select-nodes",
        nargs="+",
        default=[],
        help="Stop the specified nodes.",
    ).completer = SelectNodesCompleter
    stop_parser.add_argument(
        "--include-dependencies",
        action="store_true",
        help="Will also stop selected nodes dependencies.",
    )

    restart_parser = subparsers.add_parser("restart", help="Restarts the system")
    restart_parser.set_defaults(func=restart)
    restart_parser.add_argument(
        "--cold",
        action="store_true",
        help="Cleanup before starting again.",
    )
    restart_parser.add_argument(
        "--select-nodes",
        nargs="+",
        default=[],
        help="Restart the specified nodes.",
    ).completer = SelectNodesCompleter
    restart_parser.add_argument(
        "--include-dependencies",
        action="store_true",
        help="Will also restart selected nodes dependencies.",
    )

    status_parser = subparsers.add_parser("status", help="Displays the system status")
    status_parser.set_defaults(func=status)
    status_parser.add_argument("--watch", action="store_true", help="Refresh status continuously.")

    shutdown_parser = subparsers.add_parser("shutdown", help="Shuts down the system runtime")
    shutdown_parser.set_defaults(func=shutdown)
    shutdown_parser.add_argument(
        "--kill-session",
        action="store_true",
        help="Kill the tmux session after shutting down the system.",
    )
    shutdown_parser.add_argument(
        "--select-nodes",
        nargs="+",
        default=[],
        help="Shutdown the specified nodes.",
    ).completer = SelectNodesCompleter
    shutdown_parser.add_argument(
        "--include-dependencies",
        action="store_true",
        help="Will also shutdown selected nodes dependencies.",
    )

    boot_parser = subparsers.add_parser("boot", help="Boots the system")
    boot_parser.set_defaults(func=boot)
    boot_parser.add_argument(
        "--attach",
        action="store_true",
        help="Attach to the tmux session after booting.",
    )

    attach_parser = subparsers.add_parser("attach", help="Attaches to the system tmux session")
    attach_parser.set_defaults(func=attach)

    list_nodes_parser = subparsers.add_parser("list-nodes", help="Lists all managed nodes in the system")
    list_nodes_parser.set_defaults(func=list_nodes)

    kill_session_parser = subparsers.add_parser("kill-session", help="Kills the system tmux session")
    kill_session_parser.set_defaults(func=kill_session)
    kill_session_parser.add_argument("--force", action="store_true", help="Unused compatibility flag.")

    logs_parser = subparsers.add_parser("logs", help="Tails logs for a system entity")
    logs_parser.set_defaults(func=logs)
    logs_parser.add_argument("entity_id", help="Entity identifier from the system specification.")
    logs_parser.add_argument("--follow", action="store_true", help="Follow the latest log file.")
