# III-Drone-CLI

`iii` is the command-line entry point for building, deploying, configuring, and operating the III system from host, container, development, or remote environments.

## Package Role

The CLI package provides:

- the top-level `iii` command dispatcher
- subcommands for system control, configuration access, build flows, and deployment flows
- thin environment-specific wrappers around daemon-backed system actions, tmux sessions, container helpers, and SSH-based remote administration

## Supported Modes

The CLI behavior depends on `CLI_CONFIGURATION`:

- `host`: forwards many commands into the CLI container or local tmux workflows
- `container`: runs against the local system daemon inside a containerized environment
- `dev`: runs against the local system daemon inside the devcontainer
- `remote`: uses SSH-driven deployment and management helpers

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

## Module Map

- `__main__.py`: top-level argument parser and subcommand dispatcher
- `system.py`: user-facing system-management command wiring
- `system_client.py`: Unix-socket client for the system daemon
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
