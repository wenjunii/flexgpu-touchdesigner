# WorldBus v1 AI-frame transport contract

This document defines the logical transport from the AI producer into the show
node. It carries a generated view and the minimum information needed to turn it
into a temporally stable point target. It deliberately does not carry the
authoritative interactive point simulation; that lives on the show node.

There are three distinct layers:

1. `WORKING_PIPELINE` uses aligned RGB, depth, position, color, and interaction
   TOP contracts inside TouchDesigner.
2. `WORKING_PIPELINE/ROLE_BRIDGE` is already implemented for split-role direct
   preview. It uses one RGBA32F loopback Touch TCP atlas by default in
   `dual_local`, or one uncompressed Touch TCP atlas in `dual_network`; it
   contains RGB plus raw depth, mask, and confidence. Shared Mem is an advanced
   dual-local option requiring a separately transported producer frame-state
   sidecar.
3. `src/flexgpu/worldbus.py` is an implemented, dependency-free reference using
   bounded length-prefixed TCP for RGBA payloads and UDP JSON for
   metadata/control/heartbeats. Its `.wbr` files replay the same TCP records.

The direct role bridge is not WorldBus v1. Its lifecycle helper can sample
strict frame state from a local adapter, but producer session/timestamp strings,
intrinsics/transforms, and heartbeat/control are not serialized into the image
atlas. Touch TCP exposes `num_received_frames`, which is useful
transport-arrival preview state but not producer-generation identity. Shared
Mem has no equivalent counter and fails closed without explicit producer frame
state. The direct path also has no replay framing.
The full reference is intentionally not auto-connected to the `.toe`; it
validates that richer contract and gives production adapters a concrete
interoperability target. See
[DUAL_GPU_RUNTIME.md](DUAL_GPU_RUNTIME.md) for the direct bridge.

| Property | Direct `ROLE_BRIDGE` | Full WorldBus v1 reference |
| --- | --- | --- |
| Image format | RGBA32F; left RGB, right R=raw depth, G=confidence, B=mask | RGBA8/atlas with packed depth, mask and confidence |
| Transport | Shared Mem TOP or uncompressed Touch TCP | Length-prefixed TCP frame records plus UDP JSON |
| Metadata/control | Local adapter or explicit sidecar state only; none serialized with the atlas | IDs/session, timestamps, camera fields, heartbeat and controls |
| Replay/freshness | Touch receive-count/stale-timeout preview; Shared Mem requires a producer sidecar; no replay | `.wbr`, validation, newest-only queue and stale heartbeat state |

There is also a third heartbeat scope: the launcher gives each TouchDesigner
process an atomic machine-local application-heartbeat file for alive/ready/stale
supervision. It reports cook/source/sensor/transport/output health but neither
travels in the direct atlas nor replaces WorldBus's peer/network heartbeat.

The v1.2.1 TouchDesigner adapter contract `flexgpu-frame-state/v1` binds an
accepted frame to a producer session, monotonic frame/timestamp pair,
dimensions, validity metrics, and both `calibration_id` and canonical
`calibration_digest`. It drives a one-cook new-frame pulse locally. That mapping
must be explicitly transported by a production adapter if the receiver needs
producer-exact lifecycle semantics; the built-in atlas cannot carry its string
identity fields.

## Frame payload

| Field | Local format | Network atlas | Meaning |
| --- | --- | --- | --- |
| `rgb` | RGBA8 TOP | left half, RGB | generated color |
| `depth` | mono16F TOP | right half, packed into R/G | normalized or metric depth |
| `mask` | R8 TOP | right-half B | valid generated geometry |
| `confidence` | R8 TOP | right-half A | confidence/disocclusion weight |
| `worldbus_version` | integer DAT | UDP/TCP metadata | must equal `1` |
| `frame_id` | integer CHOP/DAT | UDP metadata | increasing within producer session |
| `timestamp_ns` | integer/string DAT | UDP metadata | sender monotonic timestamp |
| `intrinsics` | `fx fy cx cy` CHOP | UDP metadata | source camera model |
| `depth_scale_bias` | two-channel CHOP | UDP metadata | unpack/metric conversion |
| `camera_to_world` | 4x4 matrix DAT/CHOP | UDP metadata | calibrated transform |
| `generation_id` | string DAT | UDP metadata | prompt/seed generation epoch |
| `producer_session_id` | string DAT | optional UDP/TCP metadata | unique AI-process/session epoch |

This table and its RGBA8 network packing belong to full WorldBus v1, not the
built-in RGBA32F direct bridge. For a WorldBus adapter, an initial 512 x 512 RGB/depth source
can be packed as a 1024 x 512 RGBA8 atlas. Keeping color, packed depth, mask, and
confidence in one payload makes the image update atomic. The Python reference
accepts an already packed `rgba8_atlas` or a plain `rgba8` payload and requires
an even atlas width and exactly `width * height * 4` bytes. The right plane uses
big-endian uint16 depth in R/G, mask in B, and confidence in A. It does not
perform the TouchDesigner TOP packing itself. A TouchDesigner adapter should
pair out-of-band UDP metadata
with a payload only when their frame IDs match; the TCP record also carries its
own validated metadata.

## Local names

Global shared-memory names include a configurable show namespace so two
projects can coexist:

```text
<namespace>.worldbus.rgb
<namespace>.worldbus.depth
<namespace>.worldbus.mask
<namespace>.worldbus.confidence
<namespace>.worldbus.meta
<namespace>.worldbus.heartbeat
```

These are naming conventions for a future full-WorldBus shared-memory adapter.
The Python reference does not create those segments. The built-in direct bridge
instead uses one `<transport.segment_name>_atlas` block and carries none of the
metadata names above. That direct Shared Mem path is therefore advanced-only
and needs the separately configured producer-backed frame-state sidecar. A full
receiver should double-buffer decoded frames and swap only after all required
fields validate.

## Address namespace

The paths below are the canonical semantic addresses. A TouchDesigner OSC
adapter may map them to binary OSC. The standard-library reference carries
them as bounded UTF-8 JSON datagrams, so it is not directly consumable by an
OSC In CHOP without a bridge.

```text
/flexgpu/v1/frame/id
/flexgpu/v1/frame/timestamp_ns
/flexgpu/v1/camera/intrinsics
/flexgpu/v1/camera/depth_scale_bias
/flexgpu/v1/camera/to_world/row0
/flexgpu/v1/camera/to_world/row1
/flexgpu/v1/camera/to_world/row2
/flexgpu/v1/camera/to_world/row3
/flexgpu/v1/generation/id
/flexgpu/v1/heartbeat/ai
/flexgpu/v1/heartbeat/show
/flexgpu/v1/control/freeze_ai
/flexgpu/v1/control/restart_ai
/flexgpu/v1/interaction/forces
```

## Receiver acceptance rules

1. Reject frames with missing fields, invalid dimensions/payload length, an
   unsupported version, or a non-increasing `frame_id` within one producer
   session. A new unique `producer_session_id` resets the high-water mark after
   an AI worker restart; retired sessions cannot roll the queue backward.
2. Keep only the newest pending frame; stale latency is worse than a skipped
   shape.
3. Cross-fade a valid target into the persistent point world over a configurable
   interval instead of replacing the active buffer in one cook.
4. Mark AI stale after the configured warning interval, but never stop the
   world/render clocks.
5. Apply calibration only on the authoritative show node.

The reference receiver is intentionally one-shot: after `close()`, create a new
receiver instead of restarting the same instance. An incomplete TCP frame has
an absolute receive deadline, so trickled bytes cannot reserve the sole producer
connection indefinitely. Heartbeat peers first become stale, then expire; when
the bounded peer table is full, its oldest entry is evicted.

## Versioning

`v1` is part of the address path and this protocol. The built-in direct bridge
does not implement version negotiation because it is not WorldBus. A full
adapter should expose `transport.worldbus_version: 1`. The reference requires
`worldbus_version: 1` on every frame/message and rejects another version rather
than silently downgrading. Additive metadata fields may be retained or ignored
by older receivers. A change to packing, units, or required fields must use a
new version.

## Reference loopback and replay

Run a complete local TCP/UDP exchange without TouchDesigner:

```powershell
python tools/worldbus_node.py loopback
```

Create and validate deterministic replay data:

```powershell
python tools/worldbus_node.py replay-generate `
  --output runtime/worldbus-demo.wbr `
  --frames 8 `
  --width 32 `
  --height 16

python tools/worldbus_node.py replay-inspect runtime/worldbus-demo.wbr
```

To test two processes, start a bounded receiver in one terminal:

```powershell
python tools/worldbus_node.py receive `
  --host 127.0.0.1 `
  --tcp-port 9101 `
  --udp-port 9100 `
  --duration 30
```

Then send the replay from another:

```powershell
python tools/worldbus_node.py replay-send runtime/worldbus-demo.wbr `
  --host 127.0.0.1 `
  --tcp-port 9101
```

The receiver queue retains at most the newest pending frame, so seeing
`superseded` frames in a fast loopback is expected. Replay pacing follows
recorded timestamps by default; `--speed 2` doubles playback speed and
`--no-pacing` sends as fast as the socket accepts. For framing details, limits,
and Python API examples, read
[WORLDBUS_REFERENCE.md](WORLDBUS_REFERENCE.md).

The reference has strict parsing and bounded memory but no authentication,
encryption, discovery, or retransmission. Keep loopback for development; use a
trusted wired show network, firewall the selected ports, and add an
authenticated tunnel before exposing the service beyond that network.
