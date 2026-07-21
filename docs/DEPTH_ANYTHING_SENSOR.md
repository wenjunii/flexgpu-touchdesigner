# Temporary webcam audience sensor

This optional branch lets a laptop camera rehearse audience interaction before
a physical depth sensor arrives. It stays separate from generated-image
geometry:

```text
StreamDiffusion RGB -> MoGe-2 OR Depth Anything geometry -> generated point world

laptop webcam -> optional Depth Anything V2 Small -> audience interaction
physical/paid sensor later -------------------------> same interaction adapter
```

It is deliberately default-off. Neither initialization nor a preview command
opens the webcam. The explicit `-Start` switch is required, and the
TouchDesigner result receiver should be active before that command.

The temporary webcam bridge enables **Mirror Horizontal (Webcam)** by default
so an audience member moving left sees the interaction move left. Mirroring is
applied to the packed depth, mask, and confidence together; the uploaded
principal point and temporal session identity are updated at the same boundary.
Turn it off when commissioning an unmirrored physical or paid-app sensor and
use the measured `sensor_to_world` transform for venue alignment.

### Live-accepted laptop rehearsal baseline

The accepted RTX 3080 Ti Laptop starting point is MSMF/automatic 640x480 webcam
capture, 384-pixel model input, 256x144 RGB-free sensor output, and 5 Hz
inference. TouchDesigner uses a 0.55 m interaction radius and 0.35 force gain.
This produced correctly mirrored, visibly responsive interaction in the single
and triple-surface point views. It is a subjective rehearsal acceptance, not a
physical depth, latency, venue, or multi-person calibration claim. Use 3 Hz as
the first thermal fallback on the combined 16 GB workload.

## Install the replaceable TouchDesigner receiver

Run the bounded installer in the TouchDesigner Textport from an ignored local
working `.toe` (not the tracked public starter), changing `root` to this clone:

```python
from pathlib import Path; import importlib, sys; root = Path(r'C:\path\to\flexgpu-touchdesigner'); sys.path.insert(0, str(root / 'touchdesigner')); import runtime_pipeline as rp; importlib.reload(rp); rp.install_depth_anything_sensor_bridge(op('/project1/flexgpu'))
```

It updates only this existing adapter boundary:

```text
/project1/flexgpu/WORKING_PIPELINE/SENSOR_INTERACTION/DEPTH_SENSOR_ADAPTER
```

The nested receiver is `DEPTH_ANYTHING_BRIDGE`. It follows the parent
adapter's existing `Enabled` control and is therefore off by default. The
installer preserves the three previous adapter inputs as disabled fallbacks.
While the adapter is enabled, missing/stale/invalid bridge data selects an
explicit all-zero TOP instead of preserving old occupancy.

Use these local sensor settings when applying a runtime profile:

```json
{
  "sensor": {
    "mode": "depth_sensor",
    "frame_state_operator": "DEPTH_ANYTHING_BRIDGE/FRAME_STATE",
    "stale_timeout_ms": 800
  }
}
```

Do not set `position_operator`, `mask_operator`, or `confidence_operator` for
this installed branch: its guarded route switches already own the adapter's
three public outputs. Those fields are for replacing the route with a separate
local TOP/.tox adapter. Pointing them directly at the nested bridge would
bypass its explicit stale/error zero route.

The pseudo-depth worker publishes its own explicit, session-frozen intrinsics
and depth-mapping identity. Leave `sensor.calibration_path` unset for this
temporary laptop rehearsal unless the producer is deliberately changed to use
the exact same canonical calibration ID/digest. This avoids confusing its
pseudo-depth identity with MoGe's independent generated-image camera. A real
show sensor still needs a measured `sensor_to_world` calibration and a matching
FRAME_STATE identity before its distances can be treated as physical metres.

The generic TouchDesigner sensor lifecycle enforces the same boundary for a
paid-app Spout/NDI/TOP adapter: the first accepted explicit `FRAME_STATE` locks
its calibration ID and digest for that producer session. A mid-session identity
change fails closed and selects the zero interaction route; it never replaces
the accepted lock. A deliberate recalibration must start a new, unique producer
session. Once accepted, its new identity enters the temporal contract signature
and resets retained history before interaction resumes. If
`sensor.calibration_path` (or a legacy shared calibration) is configured, that
file remains authoritative across every producer session.

## Privacy boundary

The worker uses webcam RGB only in volatile process memory for inference. It:

- does not serialize or transport RGB;
- does not save frames, previews, replays, or thumbnails;
- does not put RGB samples, hashes, prompts, or image summaries in telemetry;
- sends only depth, foreground mask, confidence, and bounded numeric metadata;
- stops producing interaction frames on stale capture, inference error, camera
  disconnect, or TCP disconnect.

The receiving adapter must enforce its stale timeout and invalidate interaction
when frames stop. Do not treat this engineering boundary as a complete venue
privacy policy; establish notice, retention, access, and deletion rules before
using any audience camera.

## Result-only WorldBus contract

The worker connects to `127.0.0.1:9241` by default and sends its complete result
over TCP only. UDP port `9240` is a reserved metadata value: worker v1 neither
sends to it nor binds it, and the TouchDesigner receiver does not open a UDP
socket for this branch. Non-loopback worker output requires
`-AllowTrustedNetwork`; a non-loopback TouchDesigner bind independently requires
the bridge's **Allow Trusted Network Bind** toggle. Both sides default to
loopback. WorldBus is not authenticated or encrypted and must never be exposed
to an untrusted network.

Each result is a WorldBus v1 frame:

| Field | Contract |
|---|---|
| `pixel_format` | `rgba8` |
| width x height | 256x144 default; at most 640x480 and 307200 pixels total |
| R/G | big-endian uint16 pseudo-metre depth; packed zero is invalid |
| B | binary foreground mask, 0 or 255 |
| A | 8-bit frozen-range confidence proxy; zero is invalid |
| `depth_scale_bias` | `[0.001, 0.0]` by default |
| `intrinsics` | assumed output-pixel `fx, fy, cx, cy`, not a measured calibration |
| `camera_to_world` | identity; the TouchDesigner sensor adapter applies placement |
| `timestamp_ns` | time the camera `read()` returned, not inference/send time |

Required correlation extensions are:

- `producer_session_id`;
- `sensor_frame_id`, exactly equal to WorldBus `frame_id`;
- `sensor_capture_timestamp_ns`, exactly equal to WorldBus `timestamp_ns`;
- `sensor_calibration_id` and its canonical
  `sensor_calibration_digest` SHA-256;
- `depth_anything_contract = flexgpu-depth-anything-sensor/v1`;
- depth/mask/confidence semantics and the frozen mapping parameters;
- `depth_anything_contains_rgb = false`.

The reference codec and validator are
`src/flexgpu/depth_anything_transport.py`. A later paid app or physical-sensor
adapter can replace the worker by honoring this same output contract.

There are two replacement paths, with no downstream world changes:

1. Have the paid app/service publish this packed WorldBus contract to the
   existing bridge on TCP 9241.
2. If the paid app exposes Spout, NDI, a TouchDesigner TOP, or an API, adapt its
   data locally to sensor-local `OUT_POSITION` (RGBA32F XYZ, A occupancy),
   `OUT_MASK`, `OUT_CONFIDENCE`, and a strict `FRAME_STATE`; connect those at
   `DEPTH_SENSOR_ADAPTER` and leave `DEPTH_ANYTHING_BRIDGE` disabled.

Do not commit the paid app, license files, credentials, private `.tox`, or
machine-local paths. Only the free adapter contract belongs in the public repo.

## Stable relative-depth mapping

Depth Anything V2 Small estimates **relative**, not metric, depth. The default
worker maps its raw output into a configurable 0.5-4.0 pseudo-metre slab. These
numbers are interaction coordinates for rehearsal, not physical measurement.

The default `session_frozen` mapper observes per-frame 2nd and 98th
percentiles for 12 inference frames, freezes the median low/high bounds once,
and never recomputes them during that process session. It sends no result before
the calibration locks. This avoids per-frame min/max breathing.

For a repeatable controlled scene, `fixed` mode accepts explicit raw bounds:

```powershell
.\scripts\Start-DepthAnythingWorker.ps1 `
  -CalibrationMode fixed `
  -RawLow 0.2 `
  -RawHigh 1.1 `
  -Start
```

Do not copy those example bounds blindly; measure the chosen backend and scene.
`near_is_larger` is the default raw ordering and can be changed explicitly.
The foreground mask keeps samples up to 3.0 pseudo-metres by default. The
confidence channel is only a frozen-range/outlier proxy, not model uncertainty
or a probability.

## Newest-only capture and failure behavior

A capture thread continuously drains the webcam into a one-slot handoff. A new
camera frame replaces any unprocessed frame, so inference cannot accumulate
camera latency. At 5 Hz, the inference loop always consumes the newest available
capture and preserves that frame's capture timestamp.

Failure behavior is closed:

- result-receiver availability is verified before the webcam opens, then the
  connection is refreshed after a potentially slow backend open and before the
  capture pump reads its first RGB frame;
- a stale camera frame is discarded and never inferred or sent;
- repeated capture failure/disconnect clears the pending slot and terminates;
- any unexpected capture-thread `OSError` or ordinary exception also closes the
  one-slot handoff immediately, so the service cannot poll an orphaned camera
  thread forever;
- malformed inference or calibration output terminates without sending a stale
  replacement;
- an empty foreground is sent as a current all-invalid frame, immediately
  clearing interaction rather than preserving old audience forces;
- the TouchDesigner adapter additionally rejects stale, reordered, changed-
  calibration, or mismatched frame/timestamp metadata.

## Acceptance sequence

1. Install the TouchDesigner sensor receiver but leave it disabled.
2. Run the mock worker with `-CalibrationFrames 1 -MaxFrames 3 -Start`.
3. Confirm three correlated RGB-free frames and then stale invalidation.
4. Inspect `OUT_INTERACTION_DEBUG`, not raw `OUT_INTERACTION`, for a readable
   color view. The raw TOP intentionally remains signed RGB force plus alpha
   occupancy and may look black in a normal image viewer.
5. Run `-Backend mock -Capture webcam` to validate camera access without loading
   the learned model. On Windows, `-CameraBackend auto` prefers MSMF; use the
   explicit `msmf`, `dshow`, or `any` value only while diagnosing a device.
6. Install/download the pinned Small model as separate explicit actions.
7. Run the real backend alone at the 3080 laptop defaults.
8. Run it alongside StreamDiffusion and MoGe; watch GPU memory, temperature,
   inference age, dropped camera frames, and TouchDesigner frame time.
9. Replace pseudo-metre placement with a measured physical sensor calibration
   before using audience distance as a real-world quantity.

Mock mode does not require the optional model environment: when that environment
is absent, the wrapper uses `python` from `PATH` and requires only NumPy. It does
not open the webcam unless `-Capture webcam -Start` is explicitly supplied.
Preview and ready JSON report the requested/preferred or selected camera backend.
The ready and final reports also include bounded camera-open timing and backend
attempts; they never contain camera pixels. The result connection is still
verified before any backend opens the webcam and refreshed before capture starts.

If the combined 16 GB laptop workload is unstable, reduce the sensor to 3 Hz,
reduce model input first, then move MoGe or the sensor worker to a second GPU.

## Licensing

The official project states that Depth Anything V2 Small is Apache-2.0 while
Base/Large/Giant are CC-BY-NC-4.0. This integration pins only Small and does not
redistribute weights. Retain applicable upstream notices and review the model,
software, camera, and venue terms for the intended deployment.
