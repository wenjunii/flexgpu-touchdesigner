# WorldBus v1 reference implementation

`src/flexgpu/worldbus.py` is a dependency-free reference for the WorldBus
contract in [WORLDBUS.md](WORLDBUS.md). It transports the AI target image and
its camera metadata to the process that owns the persistent point world. It
does not replace StreamDiffusionTD, a sensor adapter, or TouchDesigner's render
network.

The reference has four intentionally small pieces:

1. Raw RGBA atlas frames and their metadata use bounded, length-prefixed TCP.
2. Heartbeats, matching frame metadata, and controls use small OSC-like JSON
   UDP datagrams.
3. The receiver keeps only the newest pending frame. It never accumulates an
   image queue that increases interaction latency.
4. `.wbr` replay files use exactly the same frame records as TCP, so transport
   testing does not need StreamDiffusionTD to be running.

Only Python's standard library is required. Python 3.10 or newer is supported,
including the Python 3.11 runtime bundled with the tested TouchDesigner build.

## Verify locally

From the repository root:

```powershell
python tools/worldbus_node.py loopback
python -m unittest tests.test_worldbus -v
```

`loopback` binds only to `127.0.0.1`, sends four frames through one TCP
connection, sends UDP heartbeat/control/metadata messages, and reports the
newest received frame. A healthy result has `status: "pass"`, no errors, and a
queue with one or more `superseded` frames. Superseding is expected: it proves
that slow geometry cooking receives the freshest target instead of an old
backlog.

## Frame metadata

Every TCP frame contains one UTF-8 JSON metadata object. These v1 fields are
required:

| Field | Type | Validation |
| --- | --- | --- |
| `worldbus_version` | integer | Must equal `1` |
| `frame_id` | integer | Increasing within one producer session, `0..2^63-1` |
| `timestamp_ns` | decimal string or integer | Sender monotonic time, normalized to a string on output |
| `width`, `height` | integer | Positive and inside configured dimension/pixel limits |
| `pixel_format` | string | `rgba8_atlas` or `rgba8` in v1 |
| `payload_bytes` | integer | Exactly `width * height * 4` |
| `intrinsics` | four finite numbers | `fx fy cx cy`; focal lengths must be positive |
| `depth_scale_bias` | two finite numbers | Positive scale followed by bias |
| `camera_to_world` | 16 finite numbers | Row-major 4x4 transform |
| `generation_id` | non-empty string | Maximum 256 UTF-8 bytes by default |

`producer_session_id` is an optional additive string field. A live AI producer
should generate one unique value per process/session and keep it stable for that
session. It is separate from `generation_id`, which may change with prompt/seed.
It lets a restarted worker begin again at frame 0 without waiting to exceed the
previous process's frame counter.

Unknown JSON fields are retained as additive extensions. Changing a required
field, raw packing, or units requires WorldBus v2. A receiver rejects any other
`worldbus_version`; there is no silent downgrade.

The decimal-string form of `timestamp_ns` avoids precision loss in tools whose
JSON/OSC number path uses IEEE-754 doubles. It is a sender timestamp for replay
pacing and diagnostics, not a clock-synchronization mechanism. Heartbeat age is
always calculated from the receiver's local monotonic arrival time.

## TCP frame wire format

All integer lengths are unsigned 32-bit network byte order (big endian):

```text
0               4               8              12
+---------------+---------------+---------------+
| magic "WB01"  | metadata_len  | payload_len   |
+---------------+---------------+---------------+
| metadata_len bytes of UTF-8 JSON              |
+-----------------------------------------------+
| payload_len bytes of raw RGBA                  |
+-----------------------------------------------+
```

`FrameStreamDecoder` accepts arbitrary TCP fragmentation and multiple records
per connection. It validates declared lengths before waiting for or allocating
the corresponding body. Defaults are:

- 64 KiB metadata
- 64 MiB payload
- 8192 pixels per dimension and 16,777,216 total pixels
- one pending decoded frame

The limits are represented by `WorldBusLimits` and can be made smaller for a
specific show. Increasing them should be an explicit deployment decision.

Example sender:

```python
from flexgpu.worldbus import TCPFrameSender, generate_replay_frames

with TCPFrameSender("127.0.0.1", 9101) as sender:
    for frame in generate_replay_frames(8, 32, 16):
        sender.send(frame)
```

## OSC-like UDP JSON

These datagrams deliberately resemble OSC addresses while remaining readable
and dependency-free. They are UTF-8 JSON, not binary OSC packets, so they are
not consumed directly by an OSC In CHOP. Use the Python reference, a small
sidecar, or translate the validated message into TouchDesigner CHOP/DAT values.

Heartbeat:

```json
{
  "worldbus_version": 1,
  "kind": "heartbeat",
  "address": "/flexgpu/v1/heartbeat/ai",
  "timestamp_ns": "1234567890",
  "sender": "ai"
}
```

Control:

```json
{
  "worldbus_version": 1,
  "kind": "control",
  "address": "/flexgpu/v1/control/freeze_ai",
  "timestamp_ns": "1234567890",
  "value": true,
  "request_id": "operator-42"
}
```

Metadata uses `/flexgpu/v1/frame/metadata` and a `metadata` object containing
the fields in the previous section. The receiver stores the 16 newest UDP
metadata records by producer session and `frame_id`. Consumers can match one with
`receiver.metadata_for(frame_id, producer_session_id)` before swapping a
decoded atlas into the active target. The session-qualified lookup prevents a
delayed UDP record from a retired producer from matching a restarted worker's
reused frame ID.

Datagrams are limited to 16 KiB. JSON nesting and item counts are also bounded;
non-finite numbers, invalid addresses, and unknown message kinds are rejected.
Allowed control paths begin with `/flexgpu/v1/control/` or
`/flexgpu/v1/interaction/`.

## Receiver behavior

```python
from flexgpu.worldbus import WorldBusReceiver

with WorldBusReceiver("127.0.0.1", tcp_port=9101, udp_port=9100) as receiver:
    frame = receiver.frames.get(timeout=2.0)
    session_id = frame.metadata.extensions.get("producer_session_id")
    matching_metadata = receiver.metadata_for(frame.metadata.frame_id, session_id)
    ai_status = receiver.heartbeats.status("ai")
```

`NewestFrameQueue.put()` accepts only a frame ID greater than every previously
accepted ID in the current producer session. A new, unique
`producer_session_id` resets that session's high-water mark; frames from a
retired session and sessionless frames after negotiation are rejected. A newer
arrival replaces the pending frame and increments `superseded`; duplicate or
decreasing IDs increment `rejected_stale`. The show
renderer and existing particle world should continue when the AI heartbeat is
`stale` or `missing`. Staleness is a status signal, not a stop command.

`WorldBusReceiver` is one-shot: `close()` is terminal and a subsequent `start()`
raises. Create a fresh instance to bind again. Incomplete frames also have an
absolute receive deadline in addition to the idle timeout, preventing a client
that trickles bytes from holding the producer slot forever. Heartbeat entries
remain visible while stale, expire later, and evict the oldest peer if their
bounded table fills.

For a standalone receiver:

```powershell
python tools/worldbus_node.py receive `
  --host 127.0.0.1 `
  --tcp-port 9101 `
  --udp-port 9100 `
  --duration 30
```

Binding to port `0` asks the operating system for an available port, which is
useful in tests. The command prints the selected endpoints before it waits.

## Generate, inspect, and send replay data

Create a deterministic eight-frame moving gradient:

```powershell
python tools/worldbus_node.py replay-generate `
  --output runtime/worldbus-demo.wbr `
  --frames 8 `
  --width 32 `
  --height 16

python tools/worldbus_node.py replay-inspect runtime/worldbus-demo.wbr
```

The generator writes a real side-by-side atlas: moving RGB on the left and
big-endian uint16 depth (R/G), mask (B), and confidence (A) on the right. Its
intrinsics describe the source plane, whose width is half the atlas width.

With a receiver listening in another terminal:

```powershell
python tools/worldbus_node.py replay-send runtime/worldbus-demo.wbr `
  --host 127.0.0.1 `
  --tcp-port 9101
```

Replay timestamps pace frames by default. `--speed 2` plays twice as fast and
`--no-pacing` sends as fast as the socket accepts. Speed is restricted to
`0.05..100`; any single recorded delay is capped at five seconds. A replay is
limited by default to 10,000 frames and 512 MiB. Files are written through a
temporary file and atomically replaced only after every frame validates.

The `.wbr` structure is:

```text
magic "WBR1\n"
WorldBus TCP frame
WorldBus TCP frame
...
```

This makes a capture portable and streamable without a second serialization
layer. The included generator creates synthetic frames; a future
StreamDiffusionTD adapter can call `write_replay()` with its actual `WorldFrame`
objects.

## TouchDesigner adapter boundary

The intended adapter on the AI side is:

```text
StreamDiffusionTD RGB + depth/mask/confidence
                         |
                         v
                RGBA8 atlas packer
                         |
          TCP WorldFrame + UDP metadata/heartbeat
```

On the world side:

```text
WorldBusReceiver.frames (newest only)
                         |
                         v
              atlas unpack + calibration
                         |
                         v
       persistent points / fog / procedural fill
```

The authoritative simulation remains on the world/show process. Do not send a
complete mutable particle simulation across WorldBus every render frame.

## Network safety and production hardening

The reference has strict parsing and bounded memory, but no authentication,
encryption, retransmission, congestion control, or discovery. Keep the default
loopback binding during development. On a two-computer show, use an isolated
wired VLAN or trusted direct link and firewall the selected TCP/UDP ports.

For an exposed or permanent deployment, place an authenticated tunnel in front
of WorldBus, add sender identity/authorization, and collect receiver error and
drop counters. The renderer should degrade to its persistent world when the AI
peer disappears; it should never black out merely because this transport is
stale.
