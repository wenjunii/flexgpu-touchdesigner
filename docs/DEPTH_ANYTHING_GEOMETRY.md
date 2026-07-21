# Depth Anything generated-image point cloud

This is a selectable alternative to MoGe-2 for turning the exact
StreamDiffusionTD image into the FlexGPU point world:

```text
                                      +-> MoGe-2 metric depth --------+
StreamDiffusionTD generated RGB ------|                               |
                                      +-> Depth Anything relative ----+-> synchronized RGB/depth/mask
                                                                         -> existing point world

laptop camera / physical depth sensor ---------------------------------> audience interaction
```

The two Depth Anything roles are independent. This geometry path never opens
the webcam. The existing audience-sensor worker never receives or transports
the generated RGB. They have different components, controls, and ports.

## What this path provides

Depth Anything V2 Small estimates relative depth. For each generated-image
session, the worker observes the configured percentiles for 12 frames, freezes
that mapping, and converts it into a stable 0.5-4.0 pseudo-metre slab. The
result is suitable for an artistic point cloud, persistence, interaction, fog,
and procedural completion. It is not measured scene scale and does not recover
hidden or off-camera surfaces.

The worker returns the inference RGB and matching depth/mask in one immutable
atlas. The TouchDesigner bridge publishes nothing until the exact atlas upload
is confirmed. Provider identity, source frame/session, generation, camera,
calibration, size, and payload are validated before all four adapter outputs
switch together.

## Install into an ignored working TOE

Stop the external MoGe and Depth Anything workers first. In the TouchDesigner
Textport of an ignored, saved working copy:

```python
from pathlib import Path; import importlib, sys; root = Path(r'C:\path\to\flexgpu-touchdesigner'); sys.path.insert(0, str(root / 'touchdesigner')); import runtime_pipeline as rp; importlib.reload(rp); rp.install_depth_anything_geometry_bridge(op('/project1/flexgpu'))
```

This bounded installer adds only:

```text
STREAMDIFFUSION_ADAPTER/DEPTH_ANYTHING_GEOMETRY_BRIDGE
STREAMDIFFUSION_ADAPTER/GENERATED_GEOMETRY_*_ROUTE
STREAMDIFFUSION_ADAPTER/DEPTH_ANYTHING_GEOMETRY_FAIL_CLOSED_ZERO
STREAMDIFFUSION_ADAPTER.Geometrysource
```

It does not rebuild the working pipeline, inspect private StreamDiffusionTD
internals, change the selected provider, or touch the audience sensor.

## First mock test

1. Set `STREAMDIFFUSION_ADAPTER/Geometry Source` to `depth_anything`.
2. Enable `DEPTH_ANYTHING_GEOMETRY_BRIDGE`.
3. Confirm its result TCP port `9261` is listening.
4. In a separate PowerShell:

```powershell
.\scripts\Start-DepthAnythingGeometryWorker.ps1 `
  -Profile 3080ti_16gb `
  -Backend mock
.\scripts\Start-DepthAnythingGeometryWorker.ps1 `
  -Profile 3080ti_16gb `
  -Backend mock `
  -MaxFrames 30 `
  -Start
```

Expected bridge state:

- `Synchronized Result Valid` turns on;
- `Source Frame ID` increases;
- `STATUS` says `synchronized atlas uploaded and ready`;
- `geometry_provider` is `depth_anything`;
- `OUT_SOURCE_COLOR`, `OUT_POSITION`, and `OUT_INSTALLATION` update together.

If the selected Depth Anything bridge is disabled, stale, or invalid, its
route deliberately outputs zero rather than borrowing a MoGe or placeholder
frame. Switch `Geometry Source` back to `moge2` for immediate A/B comparison.

## Real worker

The generated geometry path reuses the already installed pinned V2 Small
environment and checkpoint:

```powershell
.\scripts\Start-DepthAnythingGeometryWorker.ps1 `
  -Profile 3080ti_16gb `
  -Backend depth_anything `
  -GpuIndex 0 `
  -InputSize 384 `
  -MaxEdge 384 `
  -CalibrationFrames 12 `
  -Start
```

Default loopback ports are:

| Direction | TCP | UDP |
|---|---:|---:|
| TouchDesigner generated RGB to worker | 9251 | 9250 |
| Worker synchronized atlas to TouchDesigner | 9261 | 9260 reserved |

The first 12 inference frames calibrate the relative-depth session and produce
no atlas. This is expected. A prompt/model/source-session or resolution change
starts a new calibration and output producer session.

Selecting `depth_anything` on `SHOW_CONTROL` enables and initializes its
geometry bridge automatically. The worker waits up to 120 seconds for the
TouchDesigner result listener on port `9261`, so it can be started while the
saved TOE is still completing its cold-start callbacks.
Override the bounded wait only when needed with
`-ListenerWaitSeconds <seconds>`; `0` restores fail-fast diagnosis.
The same control also switches strict `FRAME_STATE` and `CAMERA_METADATA`
lifecycle readers to this bridge and clears the previous provider session.
This prevents a stopped MoGe worker from zero-gating fresh Depth Anything
geometry after a live provider change.
Reconstruction also keeps a separate Depth Anything calibration. Its default
uses the worker's `0.5–4.0 m` pseudo-metric slab with scale `1` and bias `0`;
the commissioned MoGe installation scale is not reused. Reusing the smaller
MoGe scale would move every Depth Anything sample behind the near plane and
produce valid RGB with zero active point alpha.

For an ignored runtime configuration, select the provider and point the
metadata readers at its bridge:

```json
{
  "source": {
    "mode": "streamdiffusion",
    "geometry_provider": "depth_anything",
    "frame_state_operator": "DEPTH_ANYTHING_GEOMETRY_BRIDGE/FRAME_STATE",
    "camera_metadata_operator": "DEPTH_ANYTHING_GEOMETRY_BRIDGE/CAMERA_METADATA",
    "stale_timeout_ms": 1200
  }
}
```

Do not set direct RGB/depth/mask/confidence operators for the installed branch;
the guarded `GENERATED_GEOMETRY_*_ROUTE` switches already own those outputs.

## Acceptance

Compare Depth Anything and MoGe on the same asymmetric generated imagery:

1. recursive errors and bridge freshness;
2. RGB/depth orientation and exact frame synchronization;
3. finite position with visible parallax, not a flat plate;
4. single installation;
5. panoramic left/center/right;
6. artistic left/center/right;
7. stereo preview (development only, not VR);
8. frame time, VRAM, temperature, and a sustained soak.

Depth Anything may produce smoother silhouettes and a different depth style,
but it lacks MoGe-2's camera/metric reconstruction assumptions. Keep both paths
until visual A/B tests establish which better supports the artwork.
