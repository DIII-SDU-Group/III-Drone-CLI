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
import signal
import socket
import subprocess
import sys
import tempfile
from time import monotonic, sleep as _sleep, time_ns
from typing import Any, Mapping, Sequence

from . import vehicle_gate
from .result import CommandResult, Finding, NextAction, Outcome


def _workspace() -> Path:
    current = Path.cwd().resolve()
    for candidate in (current, *current.parents):
        if (candidate / "deployment").is_dir() and (candidate / "src").is_dir():
            return candidate
    raise ValueError("run this command from the III workspace or one of its children")


def _dirty_source_components(workspace: Path) -> frozenset[str]:
    """Return direct ``src`` children with local changes in the workspace.

    A plain developer deployment is intentionally convenient, but it must not
    send an unrelated local experiment simply because it shares this workspace.
    Explicit ``--path`` remains the escape hatch for deploying a work-in-
    progress component on purpose.
    """

    completed = subprocess.run(
        [
            "git",
            "-C",
            str(workspace),
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignore-submodules=none",
        ],
        check=False,
        text=True,
        capture_output=True,
    )
    if completed.returncode:
        # A copied or freshly-created developer workspace may not have Git
        # metadata.  It remains deployable; there simply are no repository
        # changes available to exclude automatically.
        return frozenset()

    dirty: set[str] = set()
    for line in completed.stdout.splitlines():
        if len(line) < 4:
            continue
        paths = line[3:].split(" -> ")
        for path in paths:
            parts = Path(path).parts
            if len(parts) >= 2 and parts[0] == "src":
                dirty.add(parts[1])
    return frozenset(dirty)


def _default_source_paths(
    workspace: Path, *, include_dirty: bool = False
) -> tuple[Path, ...]:
    dirty_components = frozenset() if include_dirty else _dirty_source_components(workspace)
    # Keep workspace-owned operational scripts on the same direct-deploy road
    # as setup, tools, and deployment.  HIL/field drivers live here and must
    # not silently remain stale on the Pi when ``iii deploy dev --build`` is
    # used.
    sources = [
        workspace / relative
        for relative in ("setup", "scripts", "tools", "deployment")
    ]
    source_root = workspace / "src"
    sources.extend(
        component
        for component in sorted(source_root.iterdir())
        if component.name not in dirty_components and component.name != ".git"
    )
    return tuple(sources)


def _source_paths(
    workspace: Path, requested: Sequence[str], *, include_dirty: bool = False
) -> tuple[Path, ...]:
    if not requested:
        return _default_source_paths(workspace, include_dirty=include_dirty)

    raw_paths = tuple(requested)
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


def _cross_output_dir(environment: Mapping[str, str], workspace: Path) -> Path:
    configured = environment.get("III_CROSS_OUTPUT_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    return workspace / ".cache" / "iii" / "arm64-cross"


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
    stamp = str(receipt.get("receipt_id") or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ"))
    destination = root / f"developer-deploy-{stamp}.json"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=root,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except BaseException:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    return destination


class _Progress:
    """Best-effort stream for progress written outside a running child."""

    def __init__(self, stream: Any):
        self.stream = stream
        self.enabled = stream is not None

    @property
    def raw_stream(self) -> Any:
        return self.stream if self.enabled else None

    def disable(self) -> None:
        self.enabled = False

    def write(self, value: str) -> None:
        if not self.enabled:
            return
        try:
            self.stream.write(value)
            self.flush()
        except (OSError, ValueError):
            self.disable()

    def flush(self) -> None:
        if not self.enabled:
            return
        try:
            self.stream.flush()
        except (OSError, ValueError):
            self.disable()


def _tail(path: Path, limit: int = 4000, *, start_offset: int = 0) -> str:
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(start_offset, size - limit))
            return stream.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _stop_process_group(child: subprocess.Popen[Any]) -> None:
    """Stop only the isolated session created for this command and reap it."""

    try:
        previous_sigint = signal.signal(signal.SIGINT, signal.SIG_IGN)
    except ValueError:
        previous_sigint = None
    process_group = child.pid
    try:
        try:
            os.killpg(process_group, signal.SIGTERM)
        except ProcessLookupError:
            pass
        cleanup_deadline = monotonic() + 1.5
        while _process_group_exists(process_group) and monotonic() < cleanup_deadline:
            child.poll()  # Reap an exited direct child while descendants settle.
            _sleep(0.05)
        if _process_group_exists(process_group):
            try:
                os.killpg(process_group, signal.SIGKILL)
            except ProcessLookupError:
                pass
            kill_deadline = monotonic() + 0.5
            while _process_group_exists(process_group) and monotonic() < kill_deadline:
                child.poll()
                _sleep(0.02)
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
    finally:
        if previous_sigint is not None:
            signal.signal(signal.SIGINT, previous_sigint)


def _run(
    command: Sequence[str],
    *,
    dry_run: bool,
    stage: str,
    log_root: Path,
    log_path: Path | None = None,
    append_log: bool = False,
    progress: Any = None,
    heartbeat_seconds: float = 20.0,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    if dry_run:
        return {"command": list(command), "returncode": None, "stdout": "", "stderr": ""}
    log_root.mkdir(parents=True, exist_ok=True)
    log_path = log_path or log_root / f"command-{time_ns()}.log"
    started = monotonic()
    interrupted = False
    timed_out = False
    with log_path.open("ab" if append_log else "wb") as log:
        command_log_start = log.tell()
        progress_error = None
        deferred_sigint = False
        previous_sigint = None
        sigint_handler_may_be_installed = False
        child: subprocess.Popen[Any] | None = None

        def defer_sigint(_signum: int, _frame: Any) -> None:
            nonlocal deferred_sigint
            deferred_sigint = True

        try:
            try:
                previous_sigint = signal.getsignal(signal.SIGINT)
                sigint_handler_may_be_installed = True
                signal.signal(signal.SIGINT, defer_sigint)
            except ValueError:
                # Signal handlers can only be installed from the main thread;
                # the CLI invokes deployment there.
                sigint_handler_may_be_installed = False
            try:
                child = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            except OSError as exc:
                if deferred_sigint:
                    raise KeyboardInterrupt() from exc
                diagnostic = f"could not start command: {exc}"
                log.write((diagnostic + "\n").encode("utf-8", errors="replace"))
                return {
                    "command": list(command),
                    "returncode": 127,
                    "stdout": "",
                    "stderr": diagnostic,
                    "log": str(log_path),
                    "interrupted": False,
                }
            finally:
                if sigint_handler_may_be_installed:
                    signal.signal(signal.SIGINT, previous_sigint)
                    sigint_handler_may_be_installed = False

            # Popen has returned and child is now registered for cleanup. A
            # launch-window Ctrl-C is replayed only inside this owned region.
            if deferred_sigint:
                raise KeyboardInterrupt()
            while child.poll() is None:
                if timeout_seconds is not None and monotonic() - started >= timeout_seconds:
                    timed_out = True
                    _stop_process_group(child)
                    returncode = 124
                    break
                _sleep(min(0.25, heartbeat_seconds))
                elapsed = monotonic() - started
                if child.poll() is None and progress is not None and elapsed >= heartbeat_seconds:
                    progress.write(f"  … {stage} still running ({int(elapsed)}s elapsed)\n")
                    progress.flush()
                    heartbeat_seconds += 20.0
            if not timed_out:
                returncode = child.returncode
        except KeyboardInterrupt:
            interrupted = True
            if child is not None:
                _stop_process_group(child)
            returncode = 130
        except (OSError, ValueError) as exc:
            progress_error = f"progress stream failed: {exc}"
            if child is not None:
                _stop_process_group(child)
            returncode = 125
        finally:
            if sigint_handler_may_be_installed:
                signal.signal(signal.SIGINT, previous_sigint)
        log.flush()
    diagnostic = _tail(log_path, start_offset=command_log_start)
    if progress_error:
        diagnostic = f"{diagnostic.rstrip()}\n{progress_error}".lstrip()
    return {
        "command": list(command),
        "returncode": returncode,
        "stdout": "",
        "stderr": diagnostic,
        "log": str(log_path),
        "interrupted": interrupted,
        "timed_out": timed_out,
        **({"progress_error": progress_error} if progress_error else {}),
    }


def _run_status(command: Sequence[str]) -> dict[str, Any]:
    """Capture read-only status output with its historical stream semantics."""

    completed = subprocess.run(command, check=False, text=True, capture_output=True)
    return {
        "command": list(command),
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def _reachable_canonical_peer(
    host: str,
    *,
    dry_run: bool,
    run_lookup: Any,
) -> str | None:
    """Resolve and probe one IPv4 peer for the canonical multi-homed Pi name."""

    if dry_run or host.lower().rstrip(".") != "iii.local":
        return None
    resolver = (
        "import json,socket,sys; "
        "print(json.dumps(list(dict.fromkeys(item[4][0] for item in "
        "socket.getaddrinfo(sys.argv[1],22,socket.AF_INET,socket.SOCK_STREAM)))))"
    )
    completed = run_lookup(
        [sys.executable, "-c", resolver, host],
        "Pi peer DNS lookup",
        timeout_seconds=8.0,
    )
    if completed.get("interrupted"):
        return None
    if completed.get("returncode") != 0:
        if completed.get("timed_out"):
            completed["stderr"] = "IPv4 lookup exceeded the 8-second timeout"
        reason = completed.get("stderr", "").strip()
        raise ValueError(f"Could not resolve {host} to IPv4 addresses: {reason or 'lookup failed'}")
    try:
        candidates = json.loads(Path(completed["log"]).read_text(encoding="utf-8"))
    except (KeyError, OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read IPv4 addresses for {host}: {exc}") from exc
    attempted: set[str] = set()
    for peer in candidates:
        if not isinstance(peer, str) or peer in attempted:
            continue
        attempted.add(peer)
        try:
            with socket.create_connection((peer, 22), timeout=0.75):
                return peer
        except OSError:
            continue
    raise ValueError(f"No reachable IPv4 address for {host} on SSH port 22")


def _deploy_ssh_options(host: str, peer: str | None) -> tuple[str, ...]:
    options = ["-o", "ConnectTimeout=8", "-o", "ConnectionAttempts=1"]
    if peer is not None:
        options.extend(["-o", f"HostName={peer}", "-o", f"HostKeyAlias={host}"])
    return tuple(options)


def _remote_command(
    target: str, command: str, *, ssh_options: Sequence[str] = ()
) -> list[str]:
    return ["ssh", *ssh_options, target, command]


def _pi_cli_install_command(remote_workspace: str) -> str:
    """Build an idempotent Pi-side CLI install and PATH verification."""

    source = f"{remote_workspace}/tools/III-Drone-CLI/bin/iii"
    quoted_source = shlex.quote(source)
    return (
        "set -e; "
        f"if [ ! -f {quoted_source} ] || [ ! -x {quoted_source} ]; then "
        f"printf '%s\\n' {shlex.quote(f'III CLI source is missing or not executable: {source}')} >&2; "
        "exit 31; fi; "
        'mkdir -p -- "$HOME/.local/bin"; '
        f"sudo ln -sfnT -- {quoted_source} /usr/local/bin/iii; "
        f'ln -sfnT -- {quoted_source} "$HOME/.local/bin/iii"; '
        "test \"$(command -v iii)\" = /usr/local/bin/iii; "
        "iii --help >/dev/null"
    )


def deploy(args: argparse.Namespace) -> CommandResult:
    state: dict[str, Any] = {"results": []}
    setattr(args, "_iii_deploy_state", state)
    try:
        return _deploy(args, state)
    except KeyboardInterrupt:
        try:
            previous_sigint = signal.signal(signal.SIGINT, signal.SIG_IGN)
        except ValueError:
            previous_sigint = None
        try:
            return _interrupted_result(args, state)
        finally:
            if previous_sigint is not None:
                signal.signal(signal.SIGINT, previous_sigint)
    finally:
        delattr(args, "_iii_deploy_state")


def _interrupted_result(
    args: argparse.Namespace, state: dict[str, Any]
) -> CommandResult:
    workspace = state.get("workspace")
    if workspace is None:
        try:
            workspace = _workspace()
        except ValueError:
            workspace = Path.cwd().resolve()
    target = state.get("target", f"{args.user}@{args.host}")
    remote_workspace = state.get(
        "remote_workspace", args.remote_workspace.rstrip("/")
    )
    sources = state.get("sources", ())
    results = state.setdefault("results", [])
    stage = state.get("current_stage") or "between deployment stages"
    interrupted_result = {
        "command": state.get("current_command", []),
        "returncode": 130,
        "stdout": "",
        "stderr": f"interrupted during {stage}",
        "log": state.get("current_log"),
        "interrupted": True,
        "stage": stage,
    }
    if (
        not results
        or not results[-1].get("interrupted")
        or results[-1].get("stage") != stage
    ):
        results.append(interrupted_result)
    return _result(
        args,
        workspace,
        target,
        remote_workspace,
        sources,
        bool(getattr(args, "_iii_dry_run", False)),
        results,
        cross_install=state.get("cross_install"),
        interrupted_stage=stage,
    )


def _deploy(args: argparse.Namespace, state: dict[str, Any]) -> CommandResult:
    """Synchronize an editable workspace using ordinary SSH and rsync."""

    environment = getattr(args, "_iii_environment", os.environ)
    dry_run = bool(getattr(args, "_iii_dry_run", False))
    workspace = _workspace()
    state["workspace"] = workspace
    try:
        sources = _source_paths(workspace, args.path, include_dirty=bool(args.build))
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
    state.update(target=target, remote_workspace=remote_workspace, sources=sources)
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

    progress_stream = getattr(args, "_iii_progress_stream", None)
    progress = _Progress(progress_stream)
    if progress is not None:
        progress.write(
            f"deploy dev{' preview' if dry_run else ''}: target={target} workspace={workspace} plan="
            f"{len(sources)} source paths"
            f"{' + cross-build/install' if args.build else ''}"
            " + Pi CLI installation + restart\n"
        )
        progress.flush()
    logs = _receipt_root(environment, workspace) / "logs"

    def run_stage(
        command: Sequence[str],
        stage: str,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        log_path = logs / f"command-{time_ns()}.log"
        state.update(
            current_stage=stage,
            current_command=list(command),
            current_log=str(log_path),
        )
        if progress is not None and not dry_run:
            progress.write(f"[start] {stage} log={log_path}\n")
            progress.flush()
        result = _run(
            command,
            dry_run=dry_run,
            stage=stage,
            log_root=logs,
            log_path=log_path,
            progress=progress.raw_stream if not dry_run else None,
            timeout_seconds=timeout_seconds,
        )
        result.setdefault("stage", stage)
        try:
            state["results"].append(result)
        finally:
            # List append is atomic. Clear the active marker even if Ctrl-C
            # lands immediately after it, so the receipt cannot repeat the
            # completed command as a second interrupted command.
            state.update(current_command=[], current_log=None)
        if result.get("progress_error"):
            progress.disable()
        if progress is not None and not dry_run:
            status = "interrupted" if result.get("interrupted") else (
                "done" if result["returncode"] == 0 else "fail"
            )
            progress.write(f"[{status}] {stage}\n")
            if status == "fail":
                reason = result.get("stderr", "").strip().splitlines()
                progress.write(
                    f"  exit={result['returncode']} command={shlex.join(command)}"
                    + (f"; log={result['log']}" if result.get("log") else "")
                    + (f"; detail={reason[-1]}" if reason else "")
                    + "\n"
                )
            progress.flush()
        state.update(current_stage=None, current_command=[], current_log=None)
        return result

    results: list[dict[str, Any]] = state["results"]
    cross_install: Path | None = None
    state["cross_install"] = None
    peer: str | None = None
    if not dry_run and args.host.lower().rstrip(".") == "iii.local":
        if progress is not None:
            progress.write("[start] Pi peer selection host=iii.local\n")
            progress.flush()
        state["current_stage"] = "Pi peer selection"

        def run_peer_lookup(
            command: Sequence[str], stage: str, **kwargs: Any
        ) -> dict[str, Any]:
            result = run_stage(command, stage, **kwargs)
            # Keep the receipt stage accurate through the socket probe. Preserve
            # the DNS stage if Ctrl-C stopped the resolver child.
            state["current_stage"] = (
                stage if result.get("interrupted") else "Pi peer selection"
            )
            return result

        try:
            peer = _reachable_canonical_peer(
                args.host,
                dry_run=dry_run,
                run_lookup=run_peer_lookup,
            )
            if results and results[-1].get("interrupted"):
                return _result(
                    args, workspace, target, remote_workspace, sources, dry_run, results
                )
        except KeyboardInterrupt:
            raise
        except ValueError as exc:
            if not results or results[-1].get("returncode") in {0, None}:
                results.append({
                    "command": ["probe", "iii.local:22"],
                    "returncode": 1,
                    "stdout": "",
                    "stderr": str(exc),
                    "stage": "Pi peer selection",
                })
            state["current_stage"] = None
            if progress is not None:
                progress.write(f"[fail] Pi peer selection: {exc}\n")
                progress.flush()
            return _result(
                args, workspace, target, remote_workspace, sources, dry_run, results
            )
        state["current_stage"] = None
        if progress is not None:
            progress.write(f"[done] Pi peer selection peer={peer}\n")
            progress.flush()
    ssh_options = _deploy_ssh_options(args.host, peer)
    rsync_ssh = "--rsh=" + shlex.join(["ssh", *ssh_options])

    def gate_rejection(stage: str) -> CommandResult | None:
        """Deployment replaces the runtime's files and restarts its services."""

        if dry_run:
            return None
        state["current_stage"] = stage
        gate = vehicle_gate.evaluate(
            args.host,
            args.user,
            force=bool(getattr(args, "force", False)),
            ssh_options=ssh_options,
            api_host=peer,
        )
        state.update(current_stage=None, vehicle_gate=gate.as_dict())
        if progress is not None:
            progress.write(
                f"[{'done' if gate.allowed else 'fail'}] {stage}: {gate.reason}\n"
            )
            progress.flush()
        if gate.allowed:
            return None
        return vehicle_gate.rejection("iii deploy dev", gate, target=target)

    rejected = gate_rejection("vehicle gate")
    if rejected is not None:
        return rejected
    if args.build:
        cross_output = _cross_output_dir(environment, workspace)
        cross_install = cross_output / "install"
        state["cross_install"] = cross_install
        cross_command = [
            str(workspace / "scripts" / "build" / "cross_compile_arm64.sh"),
            "--workspace",
            str(workspace),
            "--output-dir",
            str(cross_output),
        ]
        run_stage(cross_command, "cross-build")
        if results[-1]["returncode"] not in {0, None}:
            return _result(
                args,
                workspace,
                target,
                remote_workspace,
                sources,
                dry_run,
                results,
                cross_install=cross_install,
            )
        if not dry_run and not (cross_install / "setup.bash").is_file():
            validation_failure = {
                "command": cross_command,
                "returncode": 31,
                "stdout": "",
                "stderr": f"cross-build output is missing {cross_install / 'setup.bash'}",
                "stage": "cross-build output validation",
            }
            results.append(validation_failure)
            return _result(
                args,
                workspace,
                target,
                remote_workspace,
                sources,
                dry_run,
                results,
                cross_install=cross_install,
            )

    setup = _remote_command(
        target, f"mkdir -p -- {shlex.quote(remote_workspace)}", ssh_options=ssh_options
    )
    run_stage(setup, "Pi workspace setup")
    if results[-1]["returncode"] not in {0, None}:
        return _result(
            args,
            workspace,
            target,
            remote_workspace,
            sources,
            dry_run,
            results,
            cross_install=cross_install,
        )

    if args.build:
        # The build took minutes; judge the aircraft again before touching the Pi.
        rejected = gate_rejection("vehicle gate before synchronization")
        if rejected is not None:
            return rejected

    source_log = logs / f"source-sync-{time_ns()}.log"
    state.update(
        current_stage="source sync",
        current_command=[],
        current_log=str(source_log),
    )
    if progress_stream is not None and not dry_run:
        progress.write(f"[start] source sync ({len(sources)} paths) log={source_log}\n")
        progress.flush()
    synced = 0
    for source in sources:
        relative = source.relative_to(workspace).as_posix()
        destination = f"{target}:{remote_workspace}/{relative}"
        command = ["rsync", "-az", "--itemize-changes"]
        if args.mirror:
            command.append("--delete")
        command.append(rsync_ssh)
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
        state.update(current_command=list(command), current_log=str(source_log))
        source_result = _run(
            command,
            dry_run=dry_run,
            stage="source sync",
            log_root=logs,
            log_path=source_log,
            append_log=True,
            progress=progress.raw_stream if not dry_run else None,
        )
        source_result.setdefault("stage", "source sync")
        try:
            results.append(source_result)
        finally:
            state.update(current_command=[], current_log=None)
        if source_result.get("progress_error"):
            progress.disable()
        if results[-1]["returncode"] not in {0, None}:
            if progress_stream is not None and not dry_run:
                failure = results[-1]
                reason = failure.get("stderr", "").strip().splitlines()
                label = "interrupted" if failure.get("interrupted") else "fail"
                progress.write(
                    f"[{label}] source sync ({synced}/{len(sources)} paths)\n"
                    f"  exit={failure['returncode']} command={shlex.join(command)}"
                    + (f"; log={failure['log']}" if failure.get("log") else "")
                    + (f"; detail={reason[-1]}" if reason else "")
                    + "\n"
                )
                progress.flush()
            return _result(
                args,
                workspace,
                target,
                remote_workspace,
                sources,
                dry_run,
                results,
                cross_install=cross_install,
            )
        synced += 1
        if progress is not None and not dry_run and synced % 5 == 0 and synced < len(sources):
            progress.write(f"[progress] source sync ({synced}/{len(sources)} paths)\n")
            progress.flush()

    if progress is not None and not dry_run:
        progress.write(f"[done] source sync ({synced}/{len(sources)} paths)\n")
        progress.flush()
    state.update(current_stage=None, current_command=[], current_log=None)

    if cross_install is not None:
        install_command = ["rsync", "-az", "--delete", "--itemize-changes"]
        install_command.append(rsync_ssh)
        install_command.extend(
            [
                "--exclude=__pycache__",
                "--exclude=*.pyc",
                f"{cross_install}/",
                f"{target}:{remote_workspace}/install/",
            ]
        )
        run_stage(install_command, "install sync")
        if results[-1]["returncode"] not in {0, None}:
            return _result(
                args,
                workspace,
                target,
                remote_workspace,
                sources,
                dry_run,
                results,
                cross_install=cross_install,
            )

    run_stage(
        _remote_command(
            target, _pi_cli_install_command(remote_workspace), ssh_options=ssh_options
        ),
        "Pi CLI install",
    )
    if results[-1]["returncode"] not in {0, None}:
        return _result(
            args,
            workspace,
            target,
            remote_workspace,
            sources,
            dry_run,
            results,
            cross_install=cross_install,
        )

    # Every deployment ends with freshly started services, so the Pi never
    # runs a mix of old processes and new files.
    if True:
        restart_command = (
            "set -e; "
            f"sudo install -m 0644 {shlex.quote(remote_workspace)}/deployment/systemd/iii-system-daemon.service "
            "/etc/systemd/system/iii-system-daemon.service; "
            f"sudo install -m 0644 {shlex.quote(remote_workspace)}/deployment/systemd/iii-runtime-api.service "
            "/etc/systemd/system/iii-runtime-api.service; "
            "sudo systemctl daemon-reload; "
            "sudo systemctl restart iii-system-daemon.service iii-runtime-api.service"
        )
        run_stage(
            _remote_command(target, restart_command, ssh_options=ssh_options), "restart"
        )

    state["current_stage"] = "final receipt"
    return _result(
        args,
        workspace,
        target,
        remote_workspace,
        sources,
        dry_run,
        results,
        cross_install=cross_install,
    )


def status(args: argparse.Namespace) -> CommandResult:
    """Inspect the ordinary systemd services over the developer SSH account."""

    target = f"{args.user}@{args.host}"
    result = _run_status(
        _remote_command(
            target,
            "systemctl is-active iii-system-daemon.service iii-runtime-api.service",
        )
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
    *,
    cross_install: Path | None = None,
    interrupted_stage: str | None = None,
) -> CommandResult:
    state = getattr(args, "_iii_deploy_state", None)
    if state is not None:
        state["current_stage"] = "final receipt"
    failed = next(
        (entry for entry in results if entry.get("interrupted")),
        next((entry for entry in results if entry["returncode"] not in {0, None}), None),
    )
    interrupted = bool(failed and failed.get("interrupted"))
    if interrupted_stage is None and interrupted:
        interrupted_stage = failed.get("stage")
        if interrupted_stage is None and state is not None:
            interrupted_stage = state.get("current_stage")
    if state is not None:
        receipt_id = state.setdefault(
            "receipt_id",
            f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')}-{os.getpid()}",
        )
    else:
        receipt_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')}-{os.getpid()}"
    receipt = {
        "schema": "iii.developer-deploy-receipt/v1",
        "receipt_id": receipt_id,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "dry_run": dry_run,
        "workspace": str(workspace),
        "target": target,
        "remote_workspace": remote_workspace,
        "paths": [str(path.relative_to(workspace)) for path in sources],
        "mirror": bool(args.mirror),
        "build": bool(args.build),
        "cross_build": bool(args.build),
        "cross_install": str(cross_install) if cross_install is not None else None,
        "restart": True,
        "vehicle_gate": (state or {}).get("vehicle_gate"),
        "interrupted_stage": interrupted_stage,
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
        failure_detail = " ".join(failed.get("stderr", "").strip().splitlines()[-1:])
        command_status = (
            "was interrupted" if interrupted else f"exited {failed['returncode']}"
        )
        return CommandResult(
            command="iii deploy dev",
            outcome=Outcome.INTERRUPTED if interrupted else Outcome.FAILED,
            summary=(
                "The direct developer deployment was interrupted and its child command was stopped."
                if interrupted
                else "The direct developer deployment stopped at a failed command."
            ),
            code="III_DEVELOPER_DEPLOY_INTERRUPTED" if interrupted else "III_DEVELOPER_DEPLOY_FAILED",
            target=target,
            findings=(
                Finding(
                    "III_DEVELOPER_DEPLOY_INTERRUPTED" if interrupted else "III_DEVELOPER_DEPLOY_FAILED",
                    f"command {command_status}: {' '.join(failed['command'])}"
                    + (f"; log: {failed['log']}" if failed.get("log") else "")
                    + (f"; detail: {failure_detail}" if failure_detail else ""),
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
            else (
                "ARM64 runtime cross-built on the workstation and synchronized directly over SSH."
                if args.build
                else "Developer workspace synchronized directly over SSH."
            )
        ),
        code="III_DEVELOPER_DEPLOY_PREVIEW" if dry_run else "III_DEVELOPER_DEPLOY_COMPLETED",
        target=target,
        evidence=(str(receipt_path),),
        payload_schema="iii.developer-deploy-receipt/v1",
        payload={"receipt": str(receipt_path), "commands": list(results)},
        next_actions=(next_action,),
    )
