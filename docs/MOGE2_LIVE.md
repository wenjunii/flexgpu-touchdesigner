# Live MoGe-2 generated-world path

This path turns the exact RGB output of StreamDiffusionTD into a synchronized
RGB, metric-depth, mask, and confidence set for the existing FlexGPU point
world. MoGe-2 runs in an isolated external process; TouchDesigner never imports
PyTorch or the model.

MoGe-2 remains the default generated-geometry provider. A separate selectable
Depth Anything relative-depth path is documented in
[DEPTH_ANYTHING_GEOMETRY.md](DEPTH_ANYTHING_GEOMETRY.md); switching providers
does not rewire the point world or audience sensor.

```text
prompt -> StreamDiffusionTD RGB -> MOGE2_BRIDGE -> MoGe-2 worker
                                      ^                 |
                                      |                 v
point world <- RGB + metric depth + mask + confidence <-+
     |
     +-> installation / LED render
     +-> desktop stereo now, headset adapter later

webcam + temporary depth estimator -> audience interaction only
physical depth sensor at the show  -> audience interaction only
```

Camera-conditioned StreamDiffusion is optional and independent. Leave the
webcam out of StreamDiffusion when the desired world is prompt-only; use the
camera/depth branch only for audience interaction.

## What is implemented

- One persistent, pinned MoGe-2 worker with 3080 Ti 16 GB, 4090, and 5090
  starting profiles.
- Newest-only WorldBus transport in both directions, so a slow geometry stage
  drops old work instead of building latency.
- One RGBA8 atlas containing the exact inference RGB and its matching uint16
  metric depth, binary mask, and binary confidence proxy.
- Strict frame/session, calibration, camera, size, and payload validation.
- A default-off TouchDesigner bridge that uploads the synchronized atlas and
  publishes matching frame-state and camera-metadata DATs.
- Existing thick points, temporal persistence, disocclusion fog/noise, and
  procedural backfill downstream of the bridge.
- Local single-GPU, local two-GPU, and trusted-LAN two-computer endpoints.

The checkpoint, cache, generated images, point clouds, private `.tox`, local
`.toe`, calibration, and audience data remain ignored and must not be
published.

## 3080 Ti Laptop starting point

Use profile `3080ti_16gb`: 384-pixel maximum inference edge, 1,200 tokens, and
5 geometry captures per second. StreamDiffusion and the render may run faster;
the temporal world holds and evolves the most recent accepted geometry between
MoGe updates. These are conservative commissioning values, not a guaranteed
show frame rate.

On one 16 GB laptop GPU, begin with installation mode, the existing 120,000
point budget, no headset runtime, and no second learned depth model. Watch
total VRAM, temperature, clocks, and frame time with the actual
StreamDiffusionTD component. A second GPU can own the MoGe worker without
changing the TouchDesigner network.

## One-time local setup

Preview each operation before authorizing it:

```powershell
.\scripts\Initialize-MoGe2.ps1
.\scripts\Initialize-MoGe2.ps1 -Install
.\scripts\Initialize-MoGe2.ps1 -DownloadModel
```

The download is a separate explicit action. Normal worker startup verifies the
pinned checkpoint hash and runs offline; it never downloads a model at
runtime.

First prove one saved StreamDiffusion-generated frame offline:

```powershell
.\.venv\moge2\Scripts\python.exe .\tools\moge2_probe.py infer `
  --input C:\path\to\generated-frame.png `
  --profile 3080ti_16gb `
  --run-id first-generated-frame
```

Inspect the RGB/depth/normal/point previews below the ignored
`runtime/moge2-runs/first-generated-frame/` directory before enabling live
transport. Generated imagery has no physical ground-truth scale; judge shape
coherence and temporal behavior artistically.

## Install into an ignored working `.toe`

Do not install into the saved StreamDiffusion baseline or the tracked
`projects/FlexShow.toe`. Make and open an ignored working copy, then run this as
one line in the TouchDesigner Textport:

```python
from pathlib import Path; import importlib, sys; root = Path(r'C:\path\to\flexgpu-touchdesigner'); sys.path.insert(0, str(root / 'touchdesigner')); import runtime_pipeline as rp; importlib.reload(rp); rp.install_moge2_bridge(op('/project1/flexgpu'))
```

The bounded installer preserves the four current inputs to
`STREAMDIFFUSION_ADAPTER`, adds only `MOGE2_BRIDGE` and four route switches,
and leaves the bridge disabled. Save the working copy under another ignored
local filename.

The bridge is located at:

```text
/project1/flexgpu/WORKING_PIPELINE/SOURCES/
  STREAMDIFFUSION_ADAPTER/MOGE2_BRIDGE
```

Its `IN_RGB` input must resolve to the exact image currently feeding
`STREAMDIFFUSION_ADAPTER/OUT_RGB`.

For runtime camera calibration and producer-exact frame pacing, use this
source section in the ignored local show configuration:

```json
{
  "source": {
    "mode": "streamdiffusion",
    "rgb_operator": "MOGE2_RGB_ROUTE",
    "depth_operator": "MOGE2_DEPTH_ROUTE",
    "mask_operator": "MOGE2_MASK_ROUTE",
    "confidence_operator": "MOGE2_CONFIDENCE_ROUTE",
    "frame_state_operator": "MOGE2_BRIDGE/FRAME_STATE",
    "camera_metadata_operator": "MOGE2_BRIDGE/CAMERA_METADATA",
    "stale_timeout_ms": 1200
  }
}
```

Do not add a static source calibration file for this image-derived path; the
bridge supplies the per-session MoGe camera contract. A physical audience
sensor still needs its own measured sensor-to-world calibration. Configure
that file as `sensor.calibration_path`; its `calibration_id` and
`calibration_digest` are intentionally independent from the MoGe source-camera
identity. The sensor `frame_state_operator` must report the exact identity of
the sensor file, while the MoGe `FRAME_STATE` and `CAMERA_METADATA` continue to
match each other. A mismatch on either stream is rejected without borrowing the
other stream's calibration. When an explicit sensor frame-state operator is
configured, the interaction route stays disabled until that sensor contract is
valid and disables itself again on mismatch or staleness; the adapter remains
enabled so a later valid frame can recover it.

If the generated scene occupies tens of inferred metres while the audience
sensor occupies a room-scale volume, use the explicit
`RECONSTRUCTION/Installationdepthoverride` controls to map generated depth into
the interaction world. Scale, bias, near, and far remain local calibration
values. With the override disabled, synchronized MoGe camera metadata is
authoritative. With it enabled, the frame controller preserves the explicit
installation mapping across every new synchronized result.

## First live test

Use two PowerShell windows. Start TouchDesigner without a readiness wait,
because the source cannot become ready until the separate worker returns its
first frame. Open the local MoGe `.toe`, confirm StreamDiffusion RGB is moving,
and enable `MOGE2_BRIDGE/Enabled`. This starts the result receiver first.
Launch through `Start-FlexShow.ps1`: the managed process environment supplies
the repository import root before embedded DAT modules compile. A one-time
Textport `sys.path` edit is not preserved in a saved `.toe`.

In the second window, preview a deterministic mock worker:

```powershell
.\scripts\Start-MoGe2Worker.ps1 -Backend mock
.\scripts\Start-MoGe2Worker.ps1 -Backend mock -Start
```

Selecting `moge2` on `SHOW_CONTROL` now enables and initializes the matching
bridge automatically. The worker also waits up to 120 seconds for TouchDesigner
to finish opening result port `9221`, eliminating the normal cold-start race.
If the listener still does not appear, the timeout names the provider/bridge
action instead of returning an immediate Windows `10061` error.
Override the bounded wait only when needed with
`-ListenerWaitSeconds <seconds>`; `0` restores fail-fast diagnosis.
Live provider changes also move strict `FRAME_STATE` and `CAMERA_METADATA`
lifecycle readers to the selected bridge and retire the previous session, so
stale Depth Anything metadata cannot zero-gate a fresh MoGe result.

The expected bridge status is `synchronized atlas uploaded and ready`;
`Resultvalid` turns on only after the Script TOP confirms the exact staged
atlas upload, `FRAME_STATE` and `CAMERA_METADATA` become populated, and all
four route switches change together. Stop the mock with Ctrl+C.

The show worker is intended to remain running. If a command includes
`-DurationSeconds`, it is a bounded test: after that duration the process exits,
the last result exceeds the freshness window, and `Resultvalid` correctly turns
off. The bridge then reports `last synchronized atlas expired; keep the
external worker running`. A later `send_failed` means no worker is listening on
the input port; it does not mean the previously accepted atlas upload failed.
For an installation run, omit `-DurationSeconds` and leave the worker window
open. `latest_*_frame_id` describes only the currently fresh result, while
`last_accepted_*_frame_id` preserves the final accepted IDs for diagnosis after
worker loss.

If both `bridge_runtime` and `sensor_runtime` report
`ModuleNotFoundError: No module named 'flexgpu'` immediately after reopening a
saved project, the project was opened without its managed import environment or
with an older launcher. Stop TouchDesigner normally and relaunch it through the
current `Start-FlexShow.ps1`. The launcher exports `FLEXGPU_SRC`,
`FLEXGPU_ROOT`, and `FLEXGPU_CONFIG`; current embedded runtimes also derive a
bounded `src` candidate from the configuration path. Do not treat a temporary
Textport import as a persistent repair.

Then run the pinned real worker:

```powershell
.\scripts\Start-MoGe2Worker.ps1 `
  -Profile 3080ti_16gb `
  -GpuIndex 0 `
  -Start
```

`GpuIndex` is the physical NVIDIA index exposed to the worker through
`CUDA_VISIBLE_DEVICES`; the worker itself uses relative device `cuda:0`.
Change `Generationid` only when intentionally starting a new prompt/model
epoch. Use a short identifier such as `scene-002`, never the actual prompt.

Before an artistic soak, show an asymmetric test image with different marks in
all four corners. Confirm RGB and depth have the same orientation and no
horizontal or vertical mirror. Then check:

1. moving generated forms remain synchronized with their color;
2. stopped geometry does not accumulate a latency queue;
3. holes become thick points/fog/procedural material rather than hard tears;
4. a worker restart resets the producer session without reviving old frames;
5. worker loss fails stale instead of treating a held texture as a new frame;
6. VRAM, thermals, frame time, and visual quality remain stable during a soak.

## Two GPUs or two computers

For two local GPUs, keep TouchDesigner/StreamDiffusion on its assigned render
GPU and start MoGe on the other physical index:

```powershell
.\scripts\Start-MoGe2Worker.ps1 -Profile 3080ti_16gb -GpuIndex 1 -Start
```

The launcher and NVIDIA/Windows affinity determine TouchDesigner's GPU; the
worker switch controls only MoGe. Verify the actual placement with
`nvidia-smi`, because laptop display routing and GPU numbering vary.

For two computers, set the bridge's Worker Host to the AI machine, bind its
Result Host to the show machine's private-LAN interface, and pass matching
`-InputHost`, `-OutputHost`, and ports to the worker script. WorldBus v1 is not
authenticated or encrypted. Use only a trusted isolated show network, bind the
narrowest interface possible, and firewall the ports. Do not expose them to the
internet.

## Current boundary

MoGe-2 reconstructs a visible single-image surface, not a complete watertight
object and not a temporally solved 4D Gaussian scene. Persistence, fog/noise,
and procedural backfill intentionally make disocclusions graceful. SHARP and
Gaussian methods remain later offline/asynchronous A/B candidates after this
lower-latency path is artistically validated.

The installation and VR views consume the same generated world. Either output
can run alone, or both can be enabled; adding a headset runtime, pose,
controllers, compositor submission, and comfort testing remains a separate
VR integration step.
