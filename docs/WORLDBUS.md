# WorldBus v1 contract

WorldBus carries a generated view and the minimum information needed to turn it
into a temporally stable point target.  It deliberately does not carry the
authoritative interactive particle simulation; that lives on the show node.

## Frame payload

| Field | Local format | Network atlas | Meaning |
| --- | --- | --- | --- |
| `rgb` | RGBA8 TOP | left half, RGB | generated color |
| `depth` | mono16F TOP | right half, packed into R/G | normalized or metric depth |
| `mask` | R8 TOP | right-half B | valid generated geometry |
| `confidence` | R8 TOP | right-half A | confidence/disocclusion weight |
| `frame_id` | integer CHOP/OSC | OSC metadata | monotonically increasing frame |
| `timestamp_ns` | integer/string DAT/OSC | OSC metadata | sender monotonic timestamp |
| `intrinsics` | `fx fy cx cy` CHOP | OSC metadata | source camera model |
| `depth_scale_bias` | two-channel CHOP | OSC metadata | unpack/metric conversion |
| `camera_to_world` | 4x4 matrix DAT/CHOP | OSC metadata | calibrated transform |
| `generation_id` | string DAT/OSC | OSC metadata | prompt/seed generation epoch |

For network mode, an initial 512 x 512 RGB/depth source becomes a 1024 x 512
RGBA8 atlas.  Keeping color, packed depth, mask, and confidence in one TOP makes
the image payload atomic.  Metadata is accepted only when its `frame_id`
matches the atlas frame.

## Local names

Global shared-memory names include a configurable show namespace so two
projects can coexist:

```text
<namespace>.worldbus.rgb
<namespace>.worldbus.depth
<namespace>.worldbus.confidence
<namespace>.worldbus.meta
<namespace>.worldbus.heartbeat
```

The receiver double-buffers decoded frames and swaps only after all required
fields validate.

## OSC addresses

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

1. Reject frames with missing fields, invalid dimensions, or non-increasing
   `frame_id`.
2. Keep only the newest pending frame; stale latency is worse than a skipped
   shape.
3. Cross-fade a valid target into the persistent point world over a configurable
   interval instead of replacing the active buffer in one cook.
4. Mark AI stale after the configured warning interval, but never stop the
   world/render clocks.
5. Apply calibration only on the authoritative show node.

## Versioning

`v1` is part of both the OSC path and configuration.  Additive metadata fields
may be ignored by older receivers.  A change to packing, units, or required
fields must use a new version.
