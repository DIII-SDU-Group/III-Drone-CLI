# III CLI

The III CLI is a convenience layer for direct developer work on the aircraft.
It intentionally does not implement release bundles, signing, a receiver,
enrollment, replay nonces, or qualification gates.

## Direct aircraft loop

```bash
iii host provision --host iii.local
iii deploy dev --host iii.local --build --restart
iii host inspect --host iii.local
iii px4 inspect --host iii.local
```

`iii deploy dev` uses normal SSH and rsync to synchronize `src/`, `setup/`, and
`tools/` into `/home/iii/ws`. Add `--mirror` only when the remote workspace
should exactly match local source. `--dry-run` previews a command without
connecting or copying.

`iii host image write --image <image> --device /dev/<device>` writes a supplied
Pi image directly. It has no image signing or staging protocol.

The system, mission, and configuration commands remain available for normal
runtime inspection and development. PX4 inspection is read-only; explicit PX4
firmware and parameter changes stay in PX4 and QGroundControl tooling.
