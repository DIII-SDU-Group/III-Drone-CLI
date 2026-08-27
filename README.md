# III-Drone-CLI

`iii` is the command-line entry point for building, deploying, configuring, and operating the III system from host, container, development, or remote environments.

## Package Role

The CLI package provides:

- the top-level `iii` command dispatcher
- subcommands for system control, configuration access, build flows, and deployment flows
- thin environment-specific wrappers around daemon-backed system actions, the
  runtime API remote-control client, tmux sessions, container helpers, and
  fixed key-only deployment transport

## Universal Result And Operation Contract

Every executable parser leaf is dispatched through one versioned
`iii.command-result/v1` envelope. Human and JSON renderings use the same
summary, findings, operation state, context, evidence, payload, and ordered
`next_actions[]`; commands never need to parse terminal decoration to decide
what happened.

Universal controls may appear before or after the command path:

```bash
iii system status --output=json
iii system start --dry-run --output=json
iii system start --operation-id <id> --confirm --non-interactive --output=json
iii system start --operation-id <id> --resume --confirm --non-interactive
```

Mutating commands retain an exact, content-addressed plan and atomic operation
state under `III_OPERATION_STATE_DIR` or the platform state directory.
`--dry-run` performs no mutation. Non-interactive mutation requires
`--confirm`; any nested host/password prompt is rejected as
`III_REQUIRED_INPUT`. Reusing an operation ID with different argv or context is
rejected, while replaying an already completed operation is a no-op. Ctrl-C
returns status 130, retains the checkpoint, and emits an exact reattach command.

Structured stdout contains one JSON object only. Legacy handler output is
captured into the envelope payload, including child-process stdout, while
diagnostics use stderr. Help, parser errors, missing environment setup, and
internal errors use the same result and next-action contract.

## Supported Modes

The CLI behavior depends on `CLI_CONFIGURATION`:

- `host`: forwards many commands into the CLI container or local tmux workflows
- `container`: runs against the local system daemon inside a containerized environment
- `dev`: runs against the local system daemon inside the devcontainer
- `remote`: uses `iii-runtime-api` for runtime-control commands and the key-only
  `iii@iii.local` receiver gateway for deployment

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
export III_RUNTIME_API_CLI_TOKEN="$(cat "$XDG_CONFIG_HOME/iii/credentials/gc-primary/runtime-api-token")"
```

Remote `iii system status`, runtime mutations, entity/service lists, and log
reads use `iii-runtime-api`. They are not implemented by forwarding shell
commands over SSH. Mutating remote CLI commands are rejected while an active
browser GUI session holds the operator lease; read-only status/list/log
operations remain available.

This environment variable is local process input, not an onboard shared token.
Each authorized computer has a different token; the aircraft stores only its
hash. Use `iii access enroll prepare/add/prove`, `iii access list`, and
`iii access revoke` for staged computer replacement without copying private SSH
or field-signing keys.

Deployment SSH uses a user-owned Ed25519 identity, disables passwords and agent
forwarding, accepts the initial lack of server host-key authentication, and
checks the advertised logical target/profile without claiming physical-host
authentication. The forced remote gateway accepts canonical receiver requests
and resumable SFTP into one release-specific incoming partial only. It is not a
general shell, source synchronization, SCP, or remote-administration surface.

Aircraft network changes use the same retained operation contract:

```bash
iii host network apply --input .iii/operator-network.json --target real --dry-run
iii host network confirm --network-operation-id <apply-operation-id> --target real --dry-run
iii host network status --network-operation-id <apply-operation-id> --target real
```

The input must be owner-only and Git-ignored. Plans/results redact SSIDs and
passphrases. Apply always preserves Ethernet DHCP and arms a fixed onboard
90-second monotonic rollback timer; the separately authenticated confirmation
commits the candidate profile after reconnection.

## Ground-Control Host Provisioning

From a stock graphical Ubuntu 22.04/24.04 x86_64 installation containing the
workspace clone, the source wrapper bootstraps a content-addressed, hash-locked
controller and routes convergence through the canonical operation contract:

```bash
sudo -v
tools/III-Drone-CLI/bin/iii gc provision --dry-run --json
tools/III-Drone-CLI/bin/iii gc provision \
  --operation-id <retained-operation-id> --confirm --json
iii gc status --json
```

Use `--offline --offline-cache <path>` only with a complete authenticated cache
for the exact Ubuntu platform. Use `--replacement-archive <path>` only on a fresh
host; the archive is verified and imported before new machine/SSH material is
created, and no private key or runtime credential is restored. `iii gc
start/stop/restart/open/status` owns only local frontend, proxy, discovery,
mirror, clock, and browser behavior. QGroundControl remains exclusively under
`iii qgc start/stop/restart/status`; signed application slots are managed by
`iii gc application stage/activate/rollback/reconcile/prune/status`. The complete
boundary and commissioning limits are in the workspace
`docs/gc-host-provisioning.md` runbook.

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
- `ssh_manager.py`: fixed-endpoint receiver requests and content-bound resumable
  SFTP bundle transfer
- `network.py`: redacted plan/apply, onboard confirmation, and rollback status

## Tests

Tests cover:

- command dispatch from the top-level parser
- daemon-client request/response behavior
- system log selection behavior
- tmux session materialization
- fixed-endpoint key-only SSH, logical-target checks, resumable transfer,
  disconnect recovery, hostile-argument rejection, and the 120-second transfer
  measurement record

Typical package-only commands:

```bash
python3 -m pytest tools/III-Drone-CLI/test -q
```

## Maintenance Guidelines

- keep business logic out of `__main__.py`; it should only dispatch
- prefer thin environment adapters instead of branching everywhere inside a single function
- add tests whenever argument composition changes, because regressions here are easy to miss manually
