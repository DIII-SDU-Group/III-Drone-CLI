# III CLI

The III CLI is a convenience layer for direct developer work on the aircraft.
It intentionally does not implement release bundles, signing, a receiver,
enrollment, replay nonces, or qualification gates.

The standalone workspace installer (`scripts/install_gc.py --profile dev` or
`--profile deploy`) also installs this CLI natively on a Linux x86_64 ground
computer. Its `~/.local/bin/iii` wrapper selects the installed checkout. Use
`--runtime-target sim|hil|opti_track|real` for runtime-facing commands on that
computer; SIM routes into the checkout-labeled devcontainer and the other
targets route to a configured Pi over SSH after a CLI source-identity check.
Set `III_SSH_HOST` or source the matching workstation profile before a Pi
command. Inside the devcontainer or Pi, source the local `setup/` profile and
run `iii` directly without a remote hop. `iii qgc` operates the host-native
pinned QGroundControl service; `iii api` controls the selected runtime API
service, and `iii rosbag` controls the selected runtime recorder.

## Direct aircraft loop

```bash
iii host provision --host iii.local
iii deploy dev --host iii.local --build --restart
iii host inspect --host iii.local
iii px4 inspect --host iii.local
```

`iii deploy dev` uses normal SSH and rsync to synchronize the clean direct
children of `src/`, plus `setup/`, `scripts/`, `tools/`, and `deployment/`, into
`/home/iii/ws`. By default, source-only deploys leave dirty source components
local; `--build` includes all dirty source components to match the cross-built
install tree. Use
`--path src/<component>` to deliberately synchronize a work-in-progress
component. Add `--mirror` only when the remote workspace should exactly match
the selected local source. `--dry-run` previews a command without
connecting or copying.

Human `deploy dev` runs print the target, workspace, and plan first, followed
by stage updates for the cross-build, Pi workspace setup, source sync, install
sync, Pi CLI installation, and restart. After syncing, deployment installs
`/usr/local/bin/iii` and `$HOME/.local/bin/iii` for the SSH login as links to
the workspace entry point (`/home/iii/.local/bin/iii` for the default account),
then verifies `iii --help` over SSH before any requested restart.
Long-running commands emit elapsed-time heartbeats. Command
output is kept in the deployment receipt's adjacent `logs/` directory; failures
show the command, log path, and a short diagnostic tail. `--json` keeps stdout
machine-readable and omits live progress.

`iii host image write --image <image> --device /dev/<device>` writes a supplied
Pi image directly. It has no image signing or staging protocol.

`iii host provision --profile hil|real|opti_track` writes the aircraft
runtime environment, including the stack's ROS 2 domain (`--ros-domain-id`,
default 42; the flight controller's `UXRCE_DDS_DOM_ID` must equal it). For a
lab or field Wi-Fi network, add `--wifi-ssid <ssid>` and either
`--wifi-psk-file <owner-only file outside the checkout>` or the interactive
passphrase prompt, plus `--wifi-country <CC>` where needed. The passphrase
reaches Ansible only through a temporary owner-only variables file and is
stored only on the Pi; `--remove-wifi` removes the Wi-Fi client again.

`iii host clock sync --profile real|opti_track --host <pi> --confirm` runs on
the ground computer (ground control calls it for an aircraft whose clock is
unsettled). It refuses unless the runtime API shows the aircraft disarmed and
landed, steps a Pi that follows a time source, otherwise adds this ground
computer as a runtime-only chrony source on the Pi (its chrony must `allow` the
Pi), and exits 0 only once chrony reports `Leap status: Normal` within 0.1 s,
in at most 40 s.

The system, mission, and configuration commands remain available for normal
runtime inspection and development. PX4 inspection is read-only and uses the
profile provisioned on the Pi unless `--profile` selects one; for real and
OptiTrack it also requires a `/fmu/out/vehicle_status_v1` sample in the
provisioned ROS domain. Explicit PX4 firmware and parameter changes stay in
PX4 and QGroundControl tooling.
