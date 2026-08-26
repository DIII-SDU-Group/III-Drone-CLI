"""System-management command wiring for the III CLI."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import time

from .runtime_api_client import RuntimeApiClient, RuntimeApiError
from .system_client import DaemonClient
from .tmux_handler import TmuxHandler


RUNTIME_BOOT = "runtime.boot"
RUNTIME_START = "runtime.start"
RUNTIME_STOP = "runtime.stop"
RUNTIME_RESTART = "runtime.restart"
RUNTIME_SHUTDOWN = "runtime.shutdown"
RUNTIME_STATUS = "runtime.status"
RUNTIME_LIST_ENTITIES = "runtime.list_entities"
RUNTIME_LIST_SERVICES = "runtime.list_services"
RUNTIME_SERVICE_START = "runtime.service.start"
RUNTIME_SERVICE_STOP = "runtime.service.stop"
RUNTIME_SERVICE_RESTART = "runtime.service.restart"


def _configuration() -> str | None:
    """Return the active execution boundary without caching process state."""

    return os.getenv("CLI_CONFIGURATION")


def _profile_name() -> str:
    return os.environ.get("III_SYSTEM_PROFILE", "sim")


def _session_name() -> str:
    return f"iii_{_profile_name()}"


def _local_client() -> "DaemonClient":
    return DaemonClient()


def _host_forward(command: str, args: list[str]) -> bool:
    # Docker is an optional dependency outside the host wrapper. Keep it lazy so
    # local and remote CLI invocations do not require the Docker SDK.
    from .container_manager import ContainerManager

    container_manager = ContainerManager()
    return container_manager.execute_cli(command, args)


def _remote_runtime_client() -> "RuntimeApiClient":
    return RuntimeApiClient.from_env()


def _remote_command(command_id: str, parameters: dict | None = None) -> dict:
    try:
        return _remote_runtime_client().command(command_id, parameters or {})
    except RuntimeApiError as exc:
        print(f"Runtime API error: {exc}")
        exit(1)


def _remote_log_tail(source_id: str, *, lines: int = 200) -> dict:
    try:
        return _remote_runtime_client().log_tail(source_id, lines=lines)
    except RuntimeApiError as exc:
        print(f"Runtime API error: {exc}")
        exit(1)


def _remote_daemon_result(response: dict) -> dict:
    result = response.get("result") or {}
    daemon = result.get("daemon")
    return daemon if isinstance(daemon, dict) else result


def _print_remote_rejection(response: dict) -> None:
    rejection = response.get("rejection") or {}
    message = rejection.get("message") or response.get("message") or "Remote runtime command rejected."
    print(message)


def _exit_remote_response(response: dict, *, operation: str | None = None, success_message: str | None = None) -> None:
    if not response.get("accepted"):
        _print_remote_rejection(response)
        exit(1)
    daemon_result = _remote_daemon_result(response)
    if operation is not None:
        _print_result_summary(daemon_result, operation=operation)
    if success_message:
        print(success_message)
    exit(0)


def _filter_args(parts: list[str]) -> list[str]:
    return [part for part in parts if part]


def _ensure_local_daemon() -> "DaemonClient":
    client = _local_client()
    client.ensure_running()
    return client


def _systemd_service_name() -> str:
    return os.environ.get("III_SYSTEMD_DAEMON_SERVICE", "iii-system-daemon.service")


def _systemctl_command(*args: str) -> list[str]:
    command = ["systemctl", *args]
    if os.geteuid() != 0:
        command = ["sudo", "-n", *command]
    return command


def _journalctl_command(*args: str) -> list[str]:
    return ["journalctl", *args]


def SelectNodesCompleter(**kwargs):
    del kwargs
    try:
        from iii_drone_supervision.system_spec import get_system_profile

        return list(get_system_profile(_profile_name()).build_supervision_config()["managed_nodes"].keys())
    except Exception:
        return []


def SelectServicesCompleter(**kwargs):
    del kwargs
    try:
        from iii_drone_supervision.system_spec import get_system_profile

        return list(get_system_profile(_profile_name()).service_map().keys())
    except Exception:
        return []


def _select_log_file(log_dir: str, *, history: bool) -> Path | None:
    directory = Path(log_dir)
    preferred = directory / ("process.log" if history else "current.log")
    if preferred.exists():
        return preferred
    candidates = [path for path in directory.glob("*.log") if path.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime_ns)


def _tail_latest_log(log_dir: str, follow: bool, history: bool = False, lines: int = 200) -> int:
    directory = Path(log_dir)
    while True:
        target = _select_log_file(str(directory), history=history)
        if target is not None:
            return _tail_file(target, follow, lines=lines)
        if not follow:
            print(f"No log files found in {directory}")
            return 1
        print(f"Waiting for log files in {directory} ...")
        time.sleep(1.0)


def _tail_file(path: str | Path, follow: bool, lines: int = 200) -> int:
    target = str(path)
    cmd = ["tail", "-f", target] if follow else ["tail", "-n", str(max(1, lines)), target]
    return subprocess.run(cmd, check=False).returncode


def _scope_text(select_nodes: list[str], include_dependencies: bool) -> str:
    if not select_nodes:
        return "all managed nodes"
    scope = ", ".join(select_nodes)
    if include_dependencies:
        scope += " with dependencies"
    return scope


def _print_managed_summary(result: dict, *, operation: str) -> None:
    managed_nodes = result.get("managed_nodes", [])
    if not managed_nodes:
        print("Managed nodes: none changed")
        return

    labels = {
        "start": {"active": "active", "config": "configured"},
        "stop": {"active": "inactive", "config": "unconfigured"},
        "restart": {"active": "active", "config": "configured"},
        "shutdown": {"active": "inactive", "config": "unconfigured"},
    }[operation]
    grouped: dict[str, list[str]] = {}
    for managed_node in managed_nodes:
        label = labels.get(managed_node.get("transition"), managed_node.get("transition", "changed"))
        grouped.setdefault(label, [])
        node_id = managed_node.get("key", "<unknown>")
        if node_id not in grouped[label]:
            grouped[label].append(node_id)

    print("Managed nodes:")
    for label, node_ids in sorted(grouped.items()):
        print(f"  {label}: {', '.join(sorted(node_ids))}")


def _print_services_summary(services: dict | None) -> None:
    if not services:
        return

    print("Services:")
    for service_id, state in sorted(services.items()):
        if not state.get("success", True):
            print(f"  {service_id}: failed - {state.get('error', 'unknown error')}")
            continue

        if "alive" in state or "ready" in state:
            alive = "alive" if state.get("alive") else "dead"
            ready = "ready" if state.get("ready") else "waiting"
            detail = f"{alive}, {ready}"
            if state.get("pid"):
                detail += f", pid={state['pid']}"
            if state.get("reason") and not state.get("ready"):
                detail += f" - {state['reason']}"
            print(f"  {service_id}: {detail}")
            continue

        if state.get("already_running"):
            print(f"  {service_id}: already running")
        elif state.get("already_stopped"):
            print(f"  {service_id}: already stopped")
        else:
            print(f"  {service_id}: ok")


def _print_blocked_summary(blocked_nodes: dict | None) -> None:
    if not blocked_nodes:
        return

    print("Blocked nodes:")
    for node_id, service_errors in sorted(blocked_nodes.items()):
        reasons = ", ".join(
            f"{service_id}: {reason}" for service_id, reason in sorted(service_errors.items())
        )
        print(f"  {node_id}: {reasons}")


def _print_result_summary(result: dict, *, operation: str) -> None:
    if result.get("services"):
        _print_services_summary(result.get("services"))
    _print_managed_summary(result, operation=operation)
    _print_blocked_summary(result.get("blocked_nodes"))


def _print_status_result(result: dict) -> None:
    print(f"Booted: {result['booted']}")
    print(f"Profile: {result.get('profile')}")
    print("\nManaged nodes:")
    for key, state in sorted(result["managed_nodes"].items()):
        print(f"  {key}: {state}")
    print("\nServices:")
    for key, state in sorted(result.get("services", {}).items()):
        alive = "alive" if state["alive"] else "dead"
        ready = "ready" if state["ready"] else "waiting"
        print(f"  {key}: {alive}, {ready} (starts={state['starts']}, exits={state['exits']})")
        if not state["ready"]:
            print(f"    {state['reason']}")
    print("\nProcesses:")
    for key, state in sorted(result["processes"].items()):
        alive = "alive" if state["alive"] else "dead"
        print(f"  {key}: {alive} (starts={state['start_count']}, exits={state['exit_count']})")


def start(args):
    if _configuration() == "host":
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
    if _configuration() == "remote":
        response = _remote_command(
            RUNTIME_START,
            {
                "activate": not args.skip_activate,
                "select_nodes": args.select_nodes,
                "include_dependencies": args.include_dependencies,
            },
        )
        _exit_remote_response(response, operation="start", success_message="System start complete.")

    client = _local_client()
    if not client.ping():
        print('System daemon not running. Use "iii system boot" first.')
        exit(1)
    target = "configured" if args.skip_activate else "active"
    print(f"Starting system: target={target}, scope={_scope_text(args.select_nodes, args.include_dependencies)} ...", flush=True)
    try:
        result = client.start(
            activate=not args.skip_activate,
            select_nodes=args.select_nodes,
            include_dependencies=args.include_dependencies,
        )
    except RuntimeError as exc:
        if str(exc) == "System is not booted.":
            print('System is not booted. Use "iii system boot" first.')
            exit(1)
        raise
    _print_result_summary(result, operation="start")
    if not result["success"] and result.get("error"):
        print(result["error"])
    elif result.get("warning"):
        print(result["warning"])
    elif result["success"]:
        print("System start complete.")
    exit(0 if result["success"] else 1)


def stop(args):
    if _configuration() == "host":
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
    if _configuration() == "remote":
        response = _remote_command(
            RUNTIME_STOP,
            {
                "cleanup": not args.skip_cleanup,
                "select_nodes": args.select_nodes,
                "include_dependencies": args.include_dependencies,
            },
        )
        _exit_remote_response(response, operation="stop", success_message="System stop complete.")

    client = _local_client()
    if not client.ping():
        print('System daemon not running. Use "iii system boot" first.')
        exit(1)
    target = "inactive" if args.skip_cleanup else "unconfigured"
    print(f"Stopping system: target={target}, scope={_scope_text(args.select_nodes, args.include_dependencies)} ...", flush=True)
    result = client.stop(
        cleanup=not args.skip_cleanup,
        select_nodes=args.select_nodes,
        include_dependencies=args.include_dependencies,
    )
    _print_result_summary(result, operation="stop")
    if not result["success"] and result.get("error"):
        print(result["error"])
    elif result["success"]:
        print("System stop complete.")
    exit(0 if result["success"] else 1)


def restart(args):
    if _configuration() == "host":
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
    if _configuration() == "remote":
        response = _remote_command(
            RUNTIME_RESTART,
            {
                "cold": args.cold,
                "select_nodes": args.select_nodes,
                "include_dependencies": args.include_dependencies,
            },
        )
        _exit_remote_response(response, operation="restart", success_message="System restart complete.")

    client = _local_client()
    if not client.ping():
        print('System daemon not running. Use "iii system boot" first.')
        exit(1)
    mode = "cold" if args.cold else "warm"
    print(f"Restarting system: mode={mode}, scope={_scope_text(args.select_nodes, args.include_dependencies)} ...", flush=True)
    result = client.restart(
        cold=args.cold,
        select_nodes=args.select_nodes,
        include_dependencies=args.include_dependencies,
    )
    summary_operation = "stop" if (
        not result["success"] and result.get("error", "").startswith("System stop failed")
    ) else "restart"
    _print_result_summary(result, operation=summary_operation)
    if not result["success"] and result.get("error"):
        print(result["error"])
    elif result.get("warning"):
        print(result["warning"])
    elif result["success"]:
        print("System restart complete.")
    exit(0 if result["success"] else 1)


def status(args):
    if _configuration() == "host":
        success = _host_forward(
            "/home/iii/.local/bin/iii system status",
            _filter_args(["--watch" if args.watch else ""]),
        )
        exit(0 if success else 1)
    if _configuration() == "remote":
        while True:
            response = _remote_command(RUNTIME_STATUS)
            if not response.get("accepted"):
                _print_remote_rejection(response)
                exit(1)
            _print_status_result(_remote_daemon_result(response))
            if not args.watch:
                exit(0)
            time.sleep(1.0)
            print("\033[2J\033[H", end="")

    client = _local_client()
    if not client.ping():
        print("System daemon not running.")
        exit(1)

    while True:
        result = client.status()
        _print_status_result(result)
        if not args.watch:
            exit(0)
        time.sleep(1.0)
        print("\033[2J\033[H", end="")


def shutdown(args):
    if _configuration() == "host":
        success = _host_forward(
            "/home/iii/.local/bin/iii system shutdown",
            _filter_args(
                [
                    "--keep-session" if args.keep_session else "",
                    "--include-dependencies" if args.include_dependencies else "",
                    "--select-nodes" if args.select_nodes else "",
                    *args.select_nodes,
                ]
            ),
        )
        exit(0 if success else 1)
    if _configuration() == "remote":
        response = _remote_command(
            RUNTIME_SHUTDOWN,
            {
                "select_nodes": args.select_nodes,
                "include_dependencies": args.include_dependencies,
            },
        )
        _exit_remote_response(response, success_message="System runtime shutdown complete.")

    client = _local_client()
    if not client.ping():
        print("System daemon not running.")
        exit(1)
    try:
        print(f"Shutting down system runtime: scope={_scope_text(args.select_nodes, args.include_dependencies)} ...", flush=True)
        result = client.shutdown(
            select_nodes=args.select_nodes,
            include_dependencies=args.include_dependencies,
        )
    except RuntimeError as exc:
        if str(exc) != "System is not booted.":
            raise
        result = {"success": True, "message": "System runtime is not booted."}
        print(result["message"])
    if not result["success"] and result.get("error"):
        print(result["error"])
    elif result["success"] and not result.get("message"):
        print("System runtime shutdown complete.")
    should_kill_session = (
        result["success"]
        and not args.keep_session
        and not args.select_nodes
    )
    if should_kill_session:
        TmuxHandler().kill_session(_session_name())
    exit(0 if result["success"] else 1)


def boot(args):
    if _configuration() == "host":
        success = _host_forward(
            "/home/iii/.local/bin/iii system boot",
            _filter_args(["--attach" if args.attach else ""]),
        )
        exit(0 if success else 1)
    if _configuration() == "remote":
        response = _remote_command(RUNTIME_BOOT, {"profile": _profile_name()})
        message = "System boot request accepted."
        if args.attach:
            message += " Use an explicit SSH workflow to attach to the remote tmux session."
        _exit_remote_response(response, success_message=message)

    client = _ensure_local_daemon()
    response = client.boot(_profile_name())
    tmux_handler = TmuxHandler()
    session_spec = response["tmux"]
    session_running = tmux_handler.session_running(session_spec["session_name"])
    if response.get("booted") and session_running and args.attach:
        success = tmux_handler.attach(session_spec["session_name"])
        exit(0 if success else 1)
    if response.get("booted") and session_running:
        print('System already booted. Use "iii system attach" to attach to the tmux session.')
        exit(0)
    if session_running:
        tmux_handler.kill_session(session_spec["session_name"])
    if not tmux_handler.session_running(session_spec["session_name"]):
        success = tmux_handler.start(session_spec, attach=args.attach)
        exit(0 if success else 1)
    exit(1)


def attach(args):
    del args
    if _configuration() == "host":
        success = _host_forward("/home/iii/.local/bin/iii system attach", [])
        exit(0 if success else 1)
    if _configuration() == "remote":
        print("Remote tmux attach is no longer forwarded through runtime-control commands. Use an explicit SSH workflow.")
        exit(1)

    success = TmuxHandler().attach(_session_name())
    exit(0 if success else 1)


def list_nodes(args):
    del args
    if _configuration() == "host":
        success = _host_forward("/home/iii/.local/bin/iii system list-nodes", [])
        exit(0 if success else 1)
    if _configuration() == "remote":
        response = _remote_command(RUNTIME_LIST_ENTITIES)
        if not response.get("accepted"):
            _print_remote_rejection(response)
            exit(1)
        for node in _remote_daemon_result(response).get("managed_nodes", []):
            print(node)
        exit(0)

    client = _local_client()
    if not client.ping():
        print("System daemon not running.")
        exit(1)
    for node in client.list_nodes():
        print(node)
    exit(0)


def list_services(args):
    del args
    if _configuration() == "host":
        success = _host_forward("/home/iii/.local/bin/iii system list-services", [])
        exit(0 if success else 1)
    if _configuration() == "remote":
        response = _remote_command(RUNTIME_LIST_SERVICES)
        if not response.get("accepted"):
            _print_remote_rejection(response)
            exit(1)
        for service_id in _remote_daemon_result(response).get("services", []):
            print(service_id)
        exit(0)

    client = _local_client()
    if not client.ping():
        print("System daemon not running.")
        exit(1)
    for service_id in client.list_services():
        print(service_id)
    exit(0)


def service(args):
    if _configuration() == "host":
        forwarded_args = [args.service_action]
        if getattr(args, "service_id", None):
            forwarded_args.append(args.service_id)
        success = _host_forward("/home/iii/.local/bin/iii system service", forwarded_args)
        exit(0 if success else 1)
    if _configuration() == "remote":
        if args.service_action == "list":
            list_services(args)
        command_id = {
            "start": RUNTIME_SERVICE_START,
            "stop": RUNTIME_SERVICE_STOP,
            "restart": RUNTIME_SERVICE_RESTART,
        }[args.service_action]
        response = _remote_command(command_id, {"service_id": args.service_id})
        if not response.get("accepted"):
            _print_remote_rejection(response)
            exit(1)
        result = _remote_daemon_result(response)
        alive = "alive" if result.get("alive") else "dead"
        ready = "ready" if result.get("ready") else "waiting"
        print(f"{args.service_id}: {alive}, {ready}")
        if result.get("reason"):
            print(result["reason"])
        if result.get("error"):
            print(result["error"])
        exit(0 if result.get("success", False) else 1)

    client = _local_client()
    if not client.ping():
        print('System daemon not running. Use "iii system boot" first.')
        exit(1)

    if args.service_action == "list":
        for service_id in client.list_services():
            print(service_id)
        exit(0)

    method = {
        "start": client.service_start,
        "stop": client.service_stop,
        "restart": client.service_restart,
    }[args.service_action]
    service_verbs = {
        "start": "Starting",
        "stop": "Stopping",
        "restart": "Restarting",
    }
    print(f"{service_verbs[args.service_action]} service {args.service_id} ...", flush=True)
    result = method(args.service_id)
    alive = "alive" if result.get("alive") else "dead"
    ready = "ready" if result.get("ready") else "waiting"
    print(f"{args.service_id}: {alive}, {ready}")
    if result.get("reason"):
        print(result["reason"])
    if result.get("error"):
        print(result["error"])
    exit(0 if result.get("success", False) else 1)


def daemon(args):
    service_name = _systemd_service_name()

    if _configuration() == "host":
        forwarded_args = [args.daemon_action]
        if getattr(args, "follow", False):
            forwarded_args.append("--follow")
        success = _host_forward("/home/iii/.local/bin/iii system daemon", forwarded_args)
        exit(0 if success else 1)
    if _configuration() == "remote":
        print("Remote daemon systemd control is not forwarded over SSH. Use the runtime API service locally or an explicit SSH workflow.")
        exit(1)

    if args.daemon_action in {"start", "stop", "restart", "status"}:
        result = subprocess.run(
            _systemctl_command(args.daemon_action, service_name),
            check=False,
        )
        exit(result.returncode)

    if args.daemon_action == "logs":
        command = _journalctl_command("-u", service_name)
        if args.follow:
            command.append("-f")
        result = subprocess.run(command, check=False)
        exit(result.returncode)

    raise RuntimeError(f"Unknown daemon action: {args.daemon_action}")


def kill_session(args):
    del args
    if _configuration() == "host":
        success = _host_forward("/home/iii/.local/bin/iii system kill-session", [])
        exit(0 if success else 1)
    if _configuration() == "remote":
        print("Remote tmux session control is not forwarded through runtime-control commands. Use an explicit SSH workflow.")
        exit(1)

    success = TmuxHandler().kill_session(_session_name())
    exit(0 if success else 1)


def logs(args):
    follow = getattr(args, "follow", False)
    history = getattr(args, "history", False)
    lines = getattr(args, "lines", 200)
    if _configuration() == "host":
        success = _host_forward(
            f"/home/iii/.local/bin/iii system logs {args.entity_id}",
            _filter_args([
                "--follow" if follow else "",
                "--history" if history else "",
                "--lines",
                str(lines),
            ]),
        )
        exit(0 if success else 1)
    if _configuration() == "remote":
        seen = 0
        while True:
            response = _remote_log_tail(args.entity_id, lines=lines)
            rows = response.get("lines", [])
            new_rows = rows[seen:] if seen <= len(rows) else rows
            for row in new_rows:
                print(f"[{row.get('source_id', args.entity_id)}] {row.get('line', '')}")
            seen = len(rows)
            if not follow:
                exit(0)
            time.sleep(1.0)

    client = _local_client()
    if not client.ping():
        print("System daemon not running.")
        exit(1)
    if args.entity_id == "daemon":
        exit(_tail_file(client.daemon_log, follow, lines=lines))
    exit(_tail_latest_log(
        client.log_dir(args.entity_id),
        follow,
        history=history,
        lines=lines,
    ))


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
        "--keep-session",
        action="store_true",
        help="Keep the tmux session after shutting down the full system runtime.",
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

    list_services_parser = subparsers.add_parser("list-services", help="Lists all daemon-managed services")
    list_services_parser.set_defaults(func=list_services)

    service_parser = subparsers.add_parser("service", help="Controls daemon-managed services")
    service_subparsers = service_parser.add_subparsers(dest="service_action", required=True)

    service_list_parser = service_subparsers.add_parser("list", help="Lists daemon-managed services")
    service_list_parser.set_defaults(func=service)

    for action in ("start", "stop", "restart"):
        service_action_parser = service_subparsers.add_parser(action, help=f"{action.capitalize()} a service")
        service_action_parser.set_defaults(func=service)
        service_action_parser.add_argument(
            "service_id",
            help="Service identifier from the system specification.",
        ).completer = SelectServicesCompleter

    daemon_parser = subparsers.add_parser("daemon", help="Controls the systemd-owned III daemon")
    daemon_subparsers = daemon_parser.add_subparsers(dest="daemon_action", required=True)

    daemon_help = {
        "start": "Start the daemon",
        "stop": "Stop the daemon",
        "restart": "Restart the daemon",
        "status": "Show daemon status",
    }
    for action, help_text in daemon_help.items():
        daemon_action_parser = daemon_subparsers.add_parser(action, help=help_text)
        daemon_action_parser.set_defaults(func=daemon)

    daemon_logs_parser = daemon_subparsers.add_parser("logs", help="Shows daemon journal logs")
    daemon_logs_parser.set_defaults(func=daemon)
    daemon_logs_parser.add_argument("--follow", action="store_true", help="Follow daemon journal logs.")

    kill_session_parser = subparsers.add_parser("kill-session", help="Kills the system tmux session")
    kill_session_parser.set_defaults(func=kill_session)

    logs_parser = subparsers.add_parser("logs", help="Tails logs for a system entity")
    logs_parser.set_defaults(func=logs)
    logs_parser.add_argument("entity_id", help="Entity identifier from the system specification.")
    logs_parser.add_argument("--follow", action="store_true", help="Follow the latest log file.")
    logs_parser.add_argument(
        "--history",
        action="store_true",
        help="Show the accumulated process history instead of the current process run.",
    )
    logs_parser.add_argument(
        "--lines",
        type=int,
        default=200,
        help="Number of trailing log lines to print when not following.",
    )
