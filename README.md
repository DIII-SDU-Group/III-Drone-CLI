# III-Drone-CLI

`iii` is the command-line entry point for building, deploying, configuring, and operating the III system from host, container, development, or remote environments.

## Package Role

The CLI package provides:

- the top-level `iii` command dispatcher
- subcommands for system control, configuration access, build flows, and deployment flows
- thin environment-specific wrappers around daemon-backed system actions, the
  runtime API remote-control client, tmux sessions, container helpers, and
  SSH-based deployment/administration

## Supported Modes

The CLI behavior depends on `CLI_CONFIGURATION`:

- `host`: forwards many commands into the CLI container or local tmux workflows
- `container`: runs against the local system daemon inside a containerized environment
- `dev`: runs against the local system daemon inside the devcontainer
- `remote`: uses `iii-runtime-api` for runtime-control commands and SSH-driven
  helpers for deployment, sync, install, and explicit admin workflows

## System Commands

`iii system ...` is the operator-facing control surface for the supervision daemon. `iii system boot` requires systemd and starts `iii-system-daemon.service` when the daemon is not already running.

Core commands:

```bash
iii system boot
iii system attach
iii system start
iii system stop
iii system restart
iii system status
iii system logs <entity_id>
```

Systemd daemon ownership is exposed through:

```bash
iii system daemon start
iii system daemon stop
iii system daemon restart
iii system daemon status
iii system daemon logs
iii system daemon logs --follow
```

Daemon-managed services use an explicit service scope:

```bash
iii system service list
iii system service start micro_ros_agent
iii system service stop micro_ros_agent
iii system service restart micro_ros_agent
```

Service logs use the same log command as launched entities:

```bash
iii system logs micro_ros_agent
```

## Remote Runtime API Mode

Set these on the operator machine for remote runtime-control commands:

```bash
export CLI_CONFIGURATION=remote
export III_RUNTIME_API_URL=http://<runtime-host>:8765
export III_RUNTIME_API_CLI_TOKEN=<remote-cli-token>
```

Remote `iii system status`, runtime mutations, entity/service lists, and log
reads use `iii-runtime-api`. They are not implemented by forwarding shell
commands over SSH. Mutating remote CLI commands are rejected while an active
browser GUI session holds the operator lease; read-only status/list/log
operations remain available.

SSH remains available for deployment and administration commands such as
workspace sync, install, and `iii deploy ssh`.

## Module Map

- `__main__.py`: top-level argument parser and subcommand dispatcher
- `system.py`: user-facing system-management command wiring
- `system_client.py`: compatibility wrapper for the runtime-owned Unix-socket
  daemon client
- `runtime_api_client.py`: HTTP client for remote `iii-runtime-api` runtime
  control and logs
- `config.py`: launches or forwards the configuration client
- `build.py`: container-image, workspace, and cross-compilation build entry points
- `deploy.py`: remote deployment/install helpers
- `container_manager.py`: Docker Compose command wrapper used in host mode
- `tmux_handler.py`: tmux session management
- `ssh_manager.py`: SSH, SCP, and rsync helpers for remote workflows

## Tests

Tests cover:

- command dispatch from the top-level parser
- daemon-client request/response behavior
- system log selection behavior
- tmux session materialization

Typical package-only commands:

```bash
python3 -m pytest tools/III-Drone-CLI/test -q
```

## Maintenance Guidelines

- keep business logic out of `__main__.py`; it should only dispatch
- prefer thin environment adapters instead of branching everywhere inside a single function
- add tests whenever argument composition changes, because regressions here are easy to miss manually
