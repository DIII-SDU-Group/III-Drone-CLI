# III-Drone-CLI

`iii` is the command-line entry point for building, deploying, configuring, and operating the III system from host, container, development, or remote environments.

## Package Role

The CLI package provides:

- the top-level `iii` command dispatcher
- subcommands for system control, configuration access, build flows, and deployment flows
- thin environment-specific wrappers around container execution, ROS-native system actions, tmux sessions, and SSH-based remote administration

## Supported Modes

The CLI behavior depends on `CLI_CONFIGURATION`:

- `host`: forwards many commands into the CLI container or local tmux workflows
- `container`: runs against ROS services/actions directly inside the containerized environment
- `dev`: similar to container mode, but intended for development workflows
- `remote`: uses SSH-driven deployment and management helpers

## Module Map

- `__main__.py`: top-level argument parser and subcommand dispatcher
- `system.py`: user-facing system-management command wiring
- `system_handler.py`: ROS-native system-management client used in container/dev modes
- `config.py`: launches or forwards the configuration client
- `build.py`: container-image, workspace, and cross-compilation build entry points
- `deploy.py`: remote deployment/install helpers
- `container_manager.py`: Docker Compose command wrapper used in host mode
- `tmux_handler.py`: tmux/tmuxinator session management
- `ssh_manager.py`: SSH, SCP, and rsync helpers for remote workflows

## Tests

The current tests cover:

- command dispatch from the top-level parser
- host-side system command forwarding
- Docker Compose CLI wrapping
- tmux project/session handling
- supervisor-config based node completion
- build command argument forwarding and remote build routing

Typical package-only commands:

```bash
python3 -m pytest tools/III-Drone-CLI/tests -q
```

## Maintenance Guidelines

- keep business logic out of `__main__.py`; it should only dispatch
- prefer thin environment adapters instead of branching everywhere inside a single function
- add tests whenever argument composition changes, because regressions here are easy to miss manually
