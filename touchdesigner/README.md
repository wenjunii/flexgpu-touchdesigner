# FlexGPU TouchDesigner project

`bootstrap_project.py` and `runtime_pipeline.py` version `1.2.1` build a
labelled TouchDesigner 2025 project at `/project1/flexgpu`. It includes a
stock-operator `WORKING_PIPELINE` that runs
without third-party packages: animated RGB/depth, depth-to-position GLSL,
temporal feedback, simulated audience interaction, fog/procedural completion,
a point-render path, an installation preview, and a desktop stereo preview.
The installation branch retains that single output and also builds panoramic
wrap and artistic multi-angle three-surface views.

The repository does not bundle your `StreamDiffusionTD.tox`, model weights, a
sensor SDK/calibration, an OpenXR/OpenVR runtime, projection mapping, or
SHARP/Gaussian inference. It does include pinned external MoGe-2 and Depth
Anything V2 Small generated-geometry workers with default-off live bridges;
PyTorch and checkpoints remain outside TouchDesigner. The demo is the fallback for building and testing everything
downstream before those site-specific pieces are available.

The builder is safe to run in an existing project:

- It creates or updates only `/project1/flexgpu`.
- It never deletes nodes, including unknown children inside `flexgpu`.
- It repairs builder-owned `WORKING_PIPELINE` connections when rerun, while
  preserving deliberate wiring inside `SOURCES/STREAMDIFFUSION_ADAPTER`.
- It does not create an OpenVR TOP or open output windows.
- Saving writes a copy of the complete current TouchDesigner project to the
  requested `.toe` path.

Bootstrap-owned tables and Text DATs are rewritten when the builder runs. Keep
custom code in separate operators rather than editing generated DAT contents.

## Validated and candidate TouchDesigner builds

TouchDesigner 2025.32820 is the accepted show baseline. TouchDesigner
2025.33060 may remain installed side-by-side, but it is a compatibility
candidate until it passes live validation. Inventory the installations or pin
one exact build when creating a local config. After saving the accepted ignored
project described below, open PowerShell in the repository root and run:

```powershell
.\scripts\Initialize-FlexShow.ps1 -ListTouchDesigner
.\scripts\Initialize-FlexShow.ps1 `
  -Topology single `
  -Experience combined `
  -Completion hybrid `
  -TouchDesignerVersion 2025.32820 `
  -Project .\projects\FlexShow-local.toe `
  -Output .\config\local-td32820-baseline.json
```

Without an exact selector, the unique validated 2025.32820 build remains the
deterministic default. The initializer fails closed instead of promoting a sole
or numerically newest candidate. Use `-TouchDesignerVersion 2025.33060` only
with `-Project` pointing to a separate copied, ignored working `.toe` and a
separate ignored candidate config. Never save the candidate over the accepted
2025.32820 project. The complete copy, preview, and rollback commands are in
the [side-by-side candidate workflow](../README.md#side-by-side-touchdesigner-candidate-test).

Run the timestamped in-process validator below from the candidate build, then
verify application readiness, exact output dimensions, MoGe-2 active/stale
behavior, zero/fail-closed sensor output, installation and stereo/VR previews,
GPU memory, and a thermal soak. Promote the candidate only after all checks
pass. Rollback means stopping the candidate and selecting the untouched
2025.32820 config/project pair. The source-only release script and GitHub CI do
not establish compatibility with a TouchDesigner binary.

## Build in TouchDesigner

Start from a blank project. Run the following as one line in the TouchDesigner
Textport, changing `root` to the clone folder. It deliberately saves to an
ignored local filename so the tracked canonical project is not overwritten:

```python
from pathlib import Path; import sys; root = Path(r'C:\path\to\flexgpu-touchdesigner'); sys.path.insert(0, str(root / 'touchdesigner')); import bootstrap_project as b; b.build(str(root / 'projects' / 'FlexShow-local.toe'), config_path=None, save=True)
```

To load a JSON profile:

```python
from pathlib import Path; import sys; root = Path(r'C:\path\to\flexgpu-touchdesigner'); sys.path.insert(0, str(root / 'touchdesigner')); import bootstrap_project as b; b.build(str(root / 'projects' / 'FlexShow-local.toe'), config_path=str(root / 'config' / 'flexshow.json'), save=True)
```

When adding the scaffold to an existing project, use
`b.build(None, config_path=..., save=False)` and save the full project yourself
to an untracked location. `save=True` saves every operator in the current
TouchDesigner session, including unrelated nodes and external paths.

`build(output_path, config_path=None, save=True)` returns the `flexgpu` COMP.
Use `save=False` to build without saving; in that case `output_path` may be
`None`. Warnings are available as `bootstrap_project.LAST_REPORT.warnings` and
in `flexgpu.fetch('bootstrap_report')`.

The module intentionally does nothing when imported or executed until `build`
is called. Run it with TouchDesigner's Python, not a system Python process,
because node creation requires the TouchDesigner API.

## Open the built-in demo

The default source switches are deliberately safe:

- `WORKING_PIPELINE/SOURCES/DEMO_RGB_GENERATOR` supplies animated color.
- `WORKING_PIPELINE/SOURCES/DEMO_DEPTH_GENERATOR` supplies normalized depth.
- `WORKING_PIPELINE/SENSOR_INTERACTION/SIMULATED_SENSOR_MASK` supplies a moving
  occupancy region and interaction force.
- `WORKING_PIPELINE/COMPLETION` defaults to hybrid fog plus procedural
  backfill.

View these root outputs below `/project1/flexgpu/WORKING_PIPELINE`:

| Output | Use |
| --- | --- |
| `OUT_INSTALLATION` | Center point-world preview with local edge fog; no full-frame source duplicate |
| `OUT_TRIPLE_WRAP_LEFT`, `OUT_TRIPLE_WRAP_CENTER`, `OUT_TRIPLE_WRAP_RIGHT` | Common-origin panoramic feeds for three surfaces |
| `OUT_TRIPLE_WRAP` | Panoramic left-center-right preview/mapping mosaic |
| `OUT_TRIPLE_ARTISTIC_LEFT`, `OUT_TRIPLE_ARTISTIC_CENTER`, `OUT_TRIPLE_ARTISTIC_RIGHT` | Deliberately offset/rotated sculptural feeds |
| `OUT_TRIPLE_ARTISTIC` | Artistic left-center-right preview/mapping mosaic |
| `OUT_DISPLAY_ACTIVE` | Selected single, panoramic, or artistic installation output |
| `OUT_STEREO_PREVIEW` | Side-by-side desktop stereo preview |
| `OUT_LEFT_EYE`, `OUT_RIGHT_EYE` | Eye textures for a future headset adapter |
| `OUT_POSITION` | RGBA32F position texture: XYZ metres and active alpha |
| `OUT_SOURCE_COLOR` | Exact synchronized generated/source image before completion, for visual comparison |
| `OUT_COLOR` | Fog/procedural color aligned with completed `OUT_POSITION` and consumed by the point renderer |
| `OUT_INTERACTION` | RGB force and alpha occupancy texture |
| `OUT_INTERACTION_DEBUG` | Display-only color view of interaction presence and signed force |

These are inspectable development outputs. `OUT_INSTALLATION` is not projector
mapping, and the eye textures are not submitted to a headset compositor.

### Select and tune the installation display

Select `single`, `panoramic_wrap`, or `artistic_multi_angle` with the
**Active Installation Display** menu on `WORKING_PIPELINE`, or set
`render.display_mode` in the launch config. Selection changes only
`OUT_DISPLAY_ACTIVE`; all fixed outputs remain available.

Panoramic left/center/right cameras share one origin. Tune
`POINT_RENDER/Wrapyawdegrees` and `Wrapfovdegrees` to match the physical wall
angles and overlap. `Surfacefovdegrees` is retained for the artistic cameras
only. Artistic side cameras additionally use
`Artisticyawdegrees` and `Artisticoffsetmetres`; this produces parallax and
intentional discontinuities at the seams.

A monocular image-derived point cloud covers only the source camera's frontal
field of view; it is not a captured 360-degree world. On the local 3080 profile,
the panoramic yaw starts at +/-30 degrees so each side feed retains useful
overlap with the center. Larger yaw values honestly reveal empty space unless a
later reconstruction stage creates geometry beyond the source view.

Each surface is rendered independently and receives only local
view-disocclusion fog around point silhouettes. The completion color texture is
never painted behind the whole view, which avoids a stretched, dark duplicate
of the generated image. Panoramic feeds additionally pass through
`COVERAGE_WRAP_LEFT/CENTER/RIGHT`: a procedural atmosphere generated from point
occupancy and one continuous three-panel noise domain. It makes unseen regions
read as fog rather than rectangular black panels, but does not invent hidden
objects or copy the source image. Artistic and single outputs bypass that
stage. The public mosaics simply place left, center, and right horizontally.
Use the individual surface TOPs for three projectors or LED processors; use a
mosaic only when a downstream mapper expects one canvas.

### Use the live show controls

For an existing ignored working TOE, run the bounded upgrade from TouchDesigner
after adding this checkout's `touchdesigner` folder to `sys.path`:

```python
import importlib, runtime_pipeline as rp; importlib.reload(rp); rp.install_show_control_upgrade(op('/project1/flexgpu')); rp.install_output_framing_controls(op('/project1/flexgpu'))
```

Open `/project1/flexgpu/WORKING_PIPELINE/SHOW_CONTROL`. Its three parameter pages
provide:

- MoGe-2 or Depth Anything generated geometry selection;
- single, panoramic-wrap, or artistic display selection;
- completion mode and fog density;
- interaction strength and low-latency smoothing;
- panoramic yaw, independent FOV, coverage, and noise;
- adjustable width/height for every wall feed, with three-wide mosaics;
- a creative point-cloud scale plus independent MoGe-2 and Depth Anything
  provider scales;
- 3080 Ti 16 GB, 4090, and 5090 geometry/point/capture presets.
- visible PowerShell launch buttons for the two generated-geometry workers,
  using the selected quality profile and physical GPU index.

The controls update only public managed operators and never inspect or change
private StreamDiffusionTD parameters. `Wall Width` and `Wall Height` default to
the currently installed output profile; use 1920 and 1080 for the commissioned
walls. `Point Cloud Scale` changes camera framing while preserving metric XYZ;
the provider scales prevent a MoGe correction from changing the accepted Depth
Anything view. `Apply All Show Controls` reapplies the displayed values after
reopening an older working TOE.

Each worker button opens the existing public wrapper in a separate visible
PowerShell console and selects its provider first. Stop that console with
`Ctrl+C` before starting the other provider. A duplicate click from the same
TouchDesigner session is refused. `Workspace Root` must point at this checkout
if the TOE was moved outside its `projects` folder.

Runtime config controls per-surface resolution. Defaults are deliberately
conservative: 640x360 on the 3080 Ti Laptop, 960x540 on the 4090, and 1280x720
on the 5090. A triple mode costs roughly three installation render views, so
commission at the default before raising `triple_surface_width`,
`triple_surface_height`, point count, or point thickness.

For a three-projector venue where every projector is 1920x1080, initialize an
ignored local profile with `-DisplayProfile venue_1080p`. It sets
`OUT_INSTALLATION` and every individual wrap/artistic feed to 1920x1080; the
two horizontal mosaics become 5760x1080. It does not increase diffusion,
MoGe inference, geometry texture resolution, or point count:

```powershell
.\scripts\Initialize-FlexShow.ps1 `
  -Topology single `
  -Experience installation `
  -Completion hybrid `
  -DisplayProfile venue_1080p `
  -DisplayMode single `
  -GeometryProvider moge2 `
  -TouchDesignerVersion 2025.32820 `
  -Project .\projects\FlexShow-local.toe `
  -Output .\config\local-venue-1080p.json
```

Start with `single`, validate the actual source and point detail, then change
`render.display_mode` to `panoramic_wrap` or `artistic_multi_angle`. Use the
individual wall TOPs for projector mapping; the mosaics remain preview or
downstream-mapper inputs. Output resolution alone cannot recover detail missing
from the StreamDiffusion image or the MoGe/geometry sampling grid.

## Replace the demo with your StreamDiffusionTD.tox

The exact adapter COMP is:

```text
/project1/flexgpu/WORKING_PIPELINE/SOURCES/STREAMDIFFUSION_ADAPTER
```

Inside it, keep the output operators and replace their placeholder inputs:

```text
your StreamDiffusionTD RGB TOP ---> OUT_RGB
your depth-estimate TOP ----------> OUT_DEPTH
your validity/confidence TOP -----> OUT_CONFIDENCE (optional; defaults to 1)
your binary/soft valid mask TOP --> OUT_MASK (optional; multiplied with confidence)
```

The current placeholder TOPs are named
`REPLACE_WITH_STREAMDIFFUSION_RGB` and `REPLACE_WITH_DEPTH_ESTIMATE`. A safe
replacement workflow is:

1. Save an untracked working copy such as `projects/FlexShow-local.toe`.
2. Place your `.tox` inside `STREAMDIFFUSION_ADAPTER`.
3. Disconnect `REPLACE_WITH_STREAMDIFFUSION_RGB` from `OUT_RGB`, then connect
   the generated image TOP from your component to `OUT_RGB`.
4. If you have depth, disconnect `REPLACE_WITH_DEPTH_ESTIMATE` from
   `OUT_DEPTH` and connect the depth TOP. If the `.tox` emits RGB only, leave
   the demo-depth source active until a depth model is attached.
5. On `/project1/flexgpu/WORKING_PIPELINE/SOURCES`, enable **Use
   StreamDiffusion Adapter**. Enable **Use Adapter Depth** only after
   `OUT_DEPTH` contains a valid depth texture. RGB and depth switch
   independently.
6. Increment **Source Session / Generation Epoch** when a producer restarts or
   a prompt/model generation changes. Resolution, calibration, adapter identity,
   and session-epoch changes automatically reset position, color, and lifecycle
   feedback. For an untracked manual contract change, pulse Reset on all three
   history TOPs below `WORKING_PIPELINE/TEMPORAL_WORLD`.

`projects/FlexShow-local.toe`, `local-components/`, and every `.tox` are ignored
by the public-sync policy and are the safe places for private integration. Before
updating canonical `projects/FlexShow.toe`, remove the private component and
manually inspect the public project.

Required contracts:

- `OUT_RGB`: RGBA color with alpha 1; RGBA8 or floating-point color is valid.
- `OUT_DEPTH`: normalized R depth in `0..1`, with near at 0 and far at 1.
  Both endpoints are valid samples; mark missing geometry through `OUT_MASK`
  and/or `OUT_CONFIDENCE` instead of reserving depth values as sentinels.
- `OUT_CONFIDENCE`: normalized R confidence/validity aligned with depth. Leaving
  the stock placeholder connected preserves the earlier confidence=1 behavior.
- `OUT_MASK`: normalized R geometry-validity mask aligned with depth. It remains
  separate from confidence through transport and is multiplied exactly once
  before reconstruction.

Without a calibration profile, convert metric depth, millimetres, disparity,
or inverse depth to normalized `0..1` before `OUT_DEPTH`. With a validated
`source.calibration_path`, keep the native values and declare the matching
encoding, scale, bias, and range in the profile; the reconstruction shader
applies that conversion directly.

Do not replace the whole `SOURCES` or `WORKING_PIPELINE` COMPs. Keeping the two
adapter outputs lets reconstruction, persistence, completion, and all render
outputs remain unchanged. Rerunning the builder preserves an existing input on
`OUT_RGB` or `OUT_DEPTH` inside this adapter.

## Add live MoGe-2 generated geometry

The bounded local installer adds only a default-off bridge and four route
switches to the existing StreamDiffusion adapter. It preserves the component
currently feeding each adapter output:

```python
from pathlib import Path; import importlib, sys; root = Path(r'C:\path\to\flexgpu-touchdesigner'); sys.path.insert(0, str(root / 'touchdesigner')); import runtime_pipeline as rp; importlib.reload(rp); rp.install_moge2_bridge(op('/project1/flexgpu'))
```

Run this only in an ignored local working `.toe`, then save that working copy.
The bridge path is
`WORKING_PIPELINE/SOURCES/STREAMDIFFUSION_ADAPTER/MOGE2_BRIDGE`. It sends the
exact `IN_RGB` image to a separate newest-only worker and receives one atomic
RGBA8 atlas. Four shaders unpack matching RGB, metric depth, validity, and
confidence; strict `FRAME_STATE` and `CAMERA_METADATA` DATs drive lifecycle and
camera reconstruction. The bridge is disabled after installation and
TouchDesigner never imports MoGe or Torch.

Select `moge2` on `SHOW_CONTROL`; that enables and initializes the matching
bridge listener. `scripts/Start-MoGe2Worker.ps1` then waits up to 120 seconds
for result port `9221`, so it may be started while TouchDesigner is finishing
its cold-start callbacks. Use the deterministic mock first, then the pinned
real backend. Override the bounded wait with `-ListenerWaitSeconds` only when
needed. Do not use `-WaitReadyMs` for the initial TouchDesigner launch because
readiness depends on the separate worker's first returned frame.

See [docs/MOGE2_LIVE.md](../docs/MOGE2_LIVE.md) for the exact source
configuration, 3080 Ti starting profile, startup order, two-GPU/two-computer
settings, orientation check, and acceptance sequence.

## Add alternative Depth Anything generated geometry

Depth Anything V2 Small can reconstruct the same StreamDiffusionTD image as a
second selectable point-cloud path. It is separate from the webcam audience
sensor and opens no camera. Install its isolated bridge into an ignored working
TOE:

```python
from pathlib import Path; import importlib, sys; root = Path(r'C:\path\to\flexgpu-touchdesigner'); sys.path.insert(0, str(root / 'touchdesigner')); import runtime_pipeline as rp; importlib.reload(rp); rp.install_depth_anything_geometry_bridge(op('/project1/flexgpu'))
```

Select `depth_anything` on `SHOW_CONTROL`; it enables and initializes
`DEPTH_ANYTHING_GEOMETRY_BRIDGE`. Then start
`scripts/Start-DepthAnythingGeometryWorker.ps1` in another PowerShell. The
launcher waits up to 120 seconds for result port `9261`. MoGe remains the
default and can be selected again without rewiring. See
[docs/DEPTH_ANYTHING_GEOMETRY.md](../docs/DEPTH_ANYTHING_GEOMETRY.md).
The generated-geometry path is live-accepted on the 3080 Ti Laptop with
single, panoramic, and artistic outputs at 1920x1080 per surface; reaccept it
after changing GPU, worker quality, TouchDesigner, or the private source.
Both generated-geometry launchers use a 147,456-pixel 3080 budget by default:
512x512 becomes 384x384, 1024x567 becomes 512x284, and 1024x576 becomes
512x288. Install
`runtime_pipeline.install_adaptive_source_resolution(...)` once in an older
working TOE so reconstruction preserves that aspect instead of stretching the
geometry texture to a square. The bounded installer does not save the TOE.
With TouchDesigner Non-Commercial, also call
`runtime_pipeline.install_noncommercial_preview_outputs(...)`: individual wall
previews become 1280x720 and mosaics 1280x240, all inside the 1280x1280 license
limit. `install_venue_1080p_outputs(...)` restores the commissioned 1920x1080
per-wall contract after the appropriate show license is installed.

## Rehearse audience interaction with the laptop webcam

The optional Depth Anything sensor emulator is separate from both generated
geometry providers: this branch estimates only audience interaction from the
laptop camera. Install its default-off,
bounded receiver into an ignored local `.toe`:

```python
from pathlib import Path; import importlib, sys; root = Path(r'C:\path\to\flexgpu-touchdesigner'); sys.path.insert(0, str(root / 'touchdesigner')); import runtime_pipeline as rp; importlib.reload(rp); rp.install_depth_anything_sensor_bridge(op('/project1/flexgpu'))
```

The bridge is below `WORKING_PIPELINE/SENSOR_INTERACTION/DEPTH_SENSOR_ADAPTER`.
It receives no RGB, publishes sensor-local XYZ/mask/confidence plus strict
FRAME_STATE, and selects zero occupancy on stale, error, or disconnect. The
existing `CALIBRATE_SENSOR_POSITION` remains the only sensor-to-world step.
Mirror Horizontal is enabled for intuitive laptop rehearsal and changes packed
depth, mask, confidence, principal point, and temporal session identity
together. `OUT_INTERACTION_DEBUG` is the readable color view; raw
`OUT_INTERACTION` remains signed force plus occupancy and may look dark.

The live-accepted 3080 Ti Laptop rehearsal settings are 640x480 webcam capture,
384 model input, 256x144 sensor output, 5 Hz inference, 0.55 m interaction
radius, and 0.35 force gain. These are adjustable starting values, not a
physical-sensor or multi-person venue calibration.

A paid Depth Anything application or physical sensor may later replace the
temporary worker. It can publish the same packed WorldBus frame, or a local
Spout/NDI/TOP/API adapter can feed the existing `OUT_POSITION`, `OUT_MASK`,
`OUT_CONFIDENCE`, and `FRAME_STATE` boundary. Reconstruction, temporal world,
interaction, installation, and VR outputs do not change. See
[docs/DEPTH_ANYTHING_SENSOR.md](../docs/DEPTH_ANYTHING_SENSOR.md) for the
profile fields, ports, privacy boundary, worker startup, and acceptance test.

Generated MoGe depth and audience depth must occupy the same world scale before
interaction can overlap. Raw synchronized MoGe camera metadata remains the
default. For an installation-specific remap, enable
`RECONSTRUCTION/Installationdepthoverride` and tune the explicit depth scale,
bias, near, and far parameters in an ignored working TOE. Runtime camera
metadata updates preserve that opt-in remap instead of overwriting it on every
new generated frame.

Local `.tox` loading remains off unless `source.auto_load_tox` is explicitly
`true`. In that mode, `streamdiffusion_tox` must resolve to an existing local
`.tox`, `rgb_operator` is required, and configured depth/confidence/metadata
operators must resolve inside the loaded holder. Any load or output-contract
failure leaves the demo active and records a visible warning in runtime state;
the helper does not print component contents. Loading materializes the private
component in the project process, so saving that session can embed it in a
`.toe`. Use auto-load only in an ignored local project and never save that
session over `projects/FlexShow.toe`. With auto-load off, the manual wiring
workflow above is preserved. In split-role operation,
the AI process alone may load the source `.tox`, while the world process alone
may load the sensor `.tox`; the world process still applies the shared camera
calibration needed to reconstruct received depth.

`source.calibration_path` or `sensor.calibration_path` may reference a local
`flexgpu-calibration/v1` JSON profile. The helper validates dimensions,
intrinsics, depth convention/scale/range, homogeneous camera/sensor transforms,
matching calibration IDs, and the canonical calibration-content SHA-256 before
applying it. Transforms must be rigid right-handed matrices with orthonormal
unit axes and final row `[0, 0, 0, 1]`; scaling belongs in depth conversion.
Invalid calibration fails back to the demo or simulated sensor instead of
cooking spatially incorrect data.

For production frame pacing, point `source.frame_state_operator` and optionally
`sensor.frame_state_operator` at a DAT/CHOP/stored mapping containing the full
`flexgpu-frame-state/v1` contract: session/frame/timestamp, dimensions,
calibration ID/digest, valid fraction, and mean confidence. The helper accepts
each advancing pair once and turns it into a one-cook `new_frame` pulse. Held
textures age/decay without being reabsorbed; retired sessions, regressions,
future timestamps, digest mismatches, and stale frames fail closed. Without
explicit metadata it uses an operator cook token when available. The final
`legacy_each_cook` fallback keeps old adapters running but cannot distinguish a
held producer frame from a new one.

That local fallback does not authorize receiver cook frames as producer state.
Touch TCP receivers use Touch In's `num_received_frames` for transport-arrival
preview pacing. Shared Mem In has no corresponding receive counter, so an
unverified metadata-less Shared Mem receiver fails closed. A Shared Mem config
must point `source.frame_state_operator` at a producer-backed sidecar that
actually crosses the process boundary and resolves in both roles; a local
receiver-cook DAT/CHOP is not valid producer metadata.

## Generated component contracts

| Component | Responsibility |
|---|---|
| `CONFIG` | Build profile, flattened JSON and live runtime state |
| `AI_PIPELINE` | Outer-shell contract for a future split AI process |
| `WORLD_CORE` | Outer-shell contract for sensor ingest, calibration and authoritative simulation |
| `WORLD_BUS_IN` | Outer-shell boundary for a future full WorldBus v1 adapter |
| `COMPLETION` | Legacy outer-shell completion selector |
| `WORLD_BUS_OUT` | Placeholder publisher for a future full-contract authoritative world |
| `INSTALLATION_OUT` | Projection/LED output and mapping boundary |
| `VR_OUT` | Stereo PCVR output boundary |
| `OPERATOR_DASHBOARD` | Declarative settings, status and commissioning checklist |
| `STARTUP` | Environment-aware helper module and startup callbacks |
| `WORKING_PIPELINE/SOURCES` | Demo RGB/depth plus private StreamDiffusionTD adapter |
| `WORKING_PIPELINE/SOURCES/STREAMDIFFUSION_ADAPTER/MOGE2_BRIDGE` | Default-off external-worker generated RGB/metric-depth/mask/confidence path with strict frame and camera metadata |
| `WORKING_PIPELINE/SOURCES/STREAMDIFFUSION_ADAPTER/DEPTH_ANYTHING_GEOMETRY_BRIDGE` | Default-off selectable generated RGB/pseudo-metric-depth/mask/confidence path using isolated ports and strict provider/frame metadata |
| `WORKING_PIPELINE/ROLE_BRIDGE` | Atomic RGBA32F RGB/raw-depth/mask/confidence path over local, Shared Mem, or Touch TCP routes |
| `WORKING_PIPELINE/RECONSTRUCTION` | Aligned color and depth-to-position GLSL |
| `WORKING_PIPELINE/SENSOR_INTERACTION` | Calibrated sensor validity plus bounded 8x8 world-space occupancy interaction |
| `WORKING_PIPELINE/SENSOR_INTERACTION/DEPTH_SENSOR_ADAPTER/DEPTH_ANYTHING_BRIDGE` | Default-off replaceable no-RGB depth/mask/confidence receiver for temporary webcam interaction rehearsal |
| `WORKING_PIPELINE/TEMPORAL_WORLD` | One-cook frame-aware confidence/age lifecycle plus dt-integrated position/color feedback and automatic contract resets |
| `WORKING_PIPELINE/COMPLETION` | Working fog, procedural and hybrid GLSL branches |
| `WORKING_PIPELINE/POINT_RENDER` | Metric TOP-to-POP point renderer with per-point color, round glyphs, stable thinning, single/panoramic/artistic/parallel-eye cameras, and honest mono fallback |
| `WORKING_PIPELINE/INSTALLATION_OUTPUT` | Center render and view-space edge-fog development preview |
| `WORKING_PIPELINE/TRIPLE_DISPLAY` | Per-surface completion, independent left/center/right feeds, and panoramic/artistic mosaics |
| `WORKING_PIPELINE/STEREO_PREVIEW` | Per-eye view-space completion plus side-by-side desktop preview |
| `WORKING_PIPELINE/TELEMETRY` | Info CHOP metrics used by live telemetry and adaptive monitoring |
| `WORKING_PIPELINE/EXPERIMENTAL_EXTERNAL_ADAPTERS` | Disabled SHARP/Gaussian worker contracts only |

Both output modules consume the same world. Combined mode therefore adds two
camera/render views; it does not create a second simulation.

The outer show-side adapter layer uses four TOP contracts:

1. `generated_rgb`: generated color.
2. `generated_position`: AI-estimated XYZ with valid alpha.
3. `sensor_position`: calibrated metric XYZ with valid alpha.
4. `interaction_field`: force or occupancy data for the world simulation.

The outer components remain deployment contracts. The immediately runnable
geometry and rendering work is inside `WORKING_PIPELINE`; it also opens without
models, sensors, SteamVR, Spout, or third-party Python packages.

This internal adapter layer is distinct from the wire-level AI frame transport
in [`docs/WORLDBUS.md`](../docs/WORLDBUS.md), which carries RGB, depth, mask,
confidence, metadata and control. The built-in `WORKING_PIPELINE/ROLE_BRIDGE`
automatically packs RGB in the left half and raw depth/confidence/mask in
right-half R/G/B of one RGBA32F atlas for Shared Mem or Touch TCP transport.
Metric, millimetre, disparity and inverse depth are not clamped. It deliberately
is not WorldBus v1: Touch TCP's receive counter reports transport arrival, not
producer generation, while Shared Mem requires a separate strict metadata
sidecar. Producer session/timestamp strings, camera matrices, network heartbeat
and control do not cross in the image atlas. A production `WORLD_BUS_IN`
adapter remains responsible for the full contract.
The authoritative interactive simulation remains on the show node.

## One project, single or dual topology

The same `FlexShow.toe` supports every runtime role:

- `FLEXGPU_ROLE=world` plus `FLEXGPU_TOPOLOGY=single`: one process owns AI,
  sensor/world simulation, and show outputs.
- `FLEXGPU_ROLE=ai` plus `FLEXGPU_TOPOLOGY=dual_local` or `dual_network`: the AI producer process.
- `FLEXGPU_ROLE=world` plus `FLEXGPU_TOPOLOGY=dual_local` or `dual_network`: the sensor/world/show
  process, consuming the built-in atomic atlas receiver.
- `FLEXGPU_ROLE=standalone`: compatibility alias that enables AI and world in
  one process.

The startup helper reads these environment variables:

| Variable | Values |
|---|---|
| `FLEXGPU_ROLE` | `standalone`, `world`, `ai` |
| `FLEXGPU_TOPOLOGY` | `single`, `dual_local`, `dual_network` |
| `FLEXGPU_CONFIG` | Path to a runtime JSON profile |
| `FLEXGPU_EXPERIENCE` | `installation`, `vr`, `combined` |
| `FLEXGPU_COMPLETION` | `fog`, `procedural`, `hybrid` |
| `FLEXGPU_TIER` | `3080ti_16gb`, `4090`, `5090`, `custom` |
| `FLEXGPU_TRANSPORT` | `local`, `shared_memory`, `touch_tcp` |
| `FLEXGPU_TRANSPORT_SEGMENT` | Shared-memory base name (`_atlas` is appended) |
| `FLEXGPU_PEER_HOST` | AI host/IP used by the world Touch In TOP |
| `FLEXGPU_ATLAS_WIDTH`, `FLEXGPU_ATLAS_HEIGHT`, `FLEXGPU_ATLAS_PORT` | Atomic atlas endpoint |
| `FLEXGPU_TRANSPORT_FPS` | Target atlas cadence |

The launcher additionally owns `FLEXGPU_SESSION_ID`,
`FLEXGPU_HEARTBEAT_PATH`, and `FLEXGPU_HEARTBEAT_TIMEOUT_MS`. Do not put those,
or any `CUDA_*`/`FLEXGPU_*` override, in a process config. The helper uses them
to atomically publish application readiness and cook/source/sensor/transport
health under the ignored runtime directory. This is separate from WorldBus
network heartbeat traffic.
Readiness waits therefore require a v1.2.1 project. The tracked synthetic
canonical `.toe` satisfies this heartbeat contract; rebuild older or privately
modified projects before enabling readiness waits. A wait also requires accepted
frames from the selected live source; with StreamDiffusion stopped,
`source_not_accepted` is expected. The managed-health walk checks errors
propagated to an external TOX root without traversing paid/private internals.

Explicit environment values override `FLEXGPU_CONFIG`. The helper updates
`CONFIG/runtime_state`, endpoint activity, source/receiver route switches and
stage-COMP `allowCooking` gates. Endpoint TOPs use their Active expressions;
Shared Mem Out is pulsed only at its derived send step. The helper does not
change `project.cookRate` or destroy operators. AI roles cook source plus sender
only; split world roles cook receiver, reconstruction, simulation and only the
selected output module.

The same `.toe` can be launched for each role. `ROLE_BRIDGE` is installed and
configured automatically: one loopback Touch TCP atlas by default for
`dual_local`, or one uncompressed Touch TCP atlas for `dual_network`. Shared Mem
is an advanced dual-local path requiring an explicit producer frame-state
sidecar. Single topology bypasses atlas pack/unpack. See
[`docs/DUAL_GPU_RUNTIME.md`](../docs/DUAL_GPU_RUNTIME.md) for
atlas layout, cadence, inspection and the boundary between this preview bridge
and production WorldBus v1.

With `tier: auto`, the launcher injects `FLEXGPU_TIER` and quality limits per
process from its assigned GPU. On a heterogeneous local pair, the AI process
can therefore use a 5090 tier while a 3080 Ti world process keeps its own lower
geometry and point limits.

The 5090 preset intentionally defaults to 262,144 points: a 512-square
position texture has exactly that many samples. Raise geometry resolution
explicitly before requesting a larger physically reachable point budget.

The outer generated dashboard is still a status/launcher shell: its Apply and
Emergency Reset pulses are not wired to callbacks. The working pipeline's
`SHOW_CONTROL` COMP is the finished live visual control surface described
above. Launcher environment values remain authoritative for `single`,
`dual_local`, and `dual_network` startup.

To reapply environment values manually:

```python
op('/project1/flexgpu/STARTUP/runtime_helpers').module.apply(op('/project1/flexgpu'))
```

## Live configuration, adaptive quality, and telemetry

`STARTUP/runtime_helpers` applies the selected tier and config to live
operators on create/start. It binds source/depth resolution, reconstruction
resolution, point limit/thickness, completion mode and shader controls, output
dimensions, source/sensor modes, role bridge endpoints, and stage cooking
gates.

Managed launches also set `FLEXGPU_ROOT` and `FLEXGPU_SRC` before
TouchDesigner starts. This is required because saving a `.toe` preserves Text
DAT source but not an interactive Textport's `sys.path` or imported-module
cache. When either local bridge is installed, its generated runtime DAT embeds
a validated absolute hint to this checkout's public `src` tree. The MoGe-2 and
Depth Anything runtimes try that hint plus bounded candidates relative to
`FLEXGPU_CONFIG`, `project.folder`, and their DAT file folder; they never scan
the filesystem recursively. A saved local `.toe` therefore compiles after a
cold TouchDesigner restart without replaying an interactive bootstrap command.
If the repository is moved or renamed, rerun both bounded bridge installers
and save a new incremented `.toe` so the embedded hint follows the checkout.

When `adaptive.enabled` is true, the Execute DAT calls `tick()` at frame start.
The embedded controller uses measured frame interval plus configured
high/low/critical thresholds, windows, and cooldown to move through discrete
tier-bounded quality levels. A level change immediately reapplies source/depth
resolution, reconstruction resolution, and point budget. It records rate
budgets in `CONFIG/runtime_state`, but a private StreamDiffusionTD component
must bind those values itself if it needs explicit generation-rate throttling.

When `telemetry.enabled` is true, the same callback samples frame time and,
optionally, aggregate operator cook time. It appends buffered JSONL records at
`flush_every` and atomically writes the configured summary on exit. The
standalone `src/flexgpu/adaptive.py` governor and benchmark add VRAM and queue
age inputs for offline policy testing; the embedded TD controller currently
uses frame interval only.

## 3080 Ti 16 GB starting limits

The bootstrap and normal `3080ti_16gb` launcher tier start with a 120,000-point
budget, 512-square diffusion at 10 Hz, 384-square geometry at 5 Hz, and 72 Hz
`vr_fps` target metadata. The runtime does not change `project.cookRate` or
validate headset cadence. The diffusion number is likewise a scheduling budget
for your future `.tox`, not performance measured from the synthetic demo. These
values are planning defaults, not guarantees; laptop power limits, thermals,
drivers, model choice, and output resolution materially change throughput.

For a same-GPU combined run, keep the desktop-stereo render as the timing
priority and target no more than roughly 11-12 GB total use in `nvidia-smi`.
Configure the private AI adapter—or a production WorldBus adapter—to keep its
queue at one frame and drop stale AI frames. The stock Touch TCP bridge holds
and ages the last received atlas from its receive counter, but that is only
transport-arrival preview semantics; it cannot reject an old producer
generation. Metadata-less Shared Mem fails closed. Do not add SDXL, Video Depth Anything, SHARP,
Gaussian reconstruction, expensive shadows, or high MSAA until the actual
target system has ample measured headroom.

The v1.2.1 point path preserves calibrated metres through a Geometry/Camera/
Render network; it no longer normalizes geometry to a unit cube. The desktop
left/right views use parallel cameras shifted by plus/minus half the configured
preview IPD, with no toe-in and no world translation. The pre-2025 fallback is
deliberately mono rather than fake stereo. None of these development cameras
consume headset pose or runtime projection matrices, and the SBS texture is not
a compositor submission.

`POINT_RENDER/POSITION_TO_POINTS` also samples the aligned color TOP into the
POP `Color` attribute. `POINT_SPRITE_MATERIAL` therefore uses per-point color
and a managed 64-square circular alpha glyph instead of applying the entire
generated image to every sprite. `VISIBLE_POINT_THIN` uses a fixed seed and a
linear `Pointkeep` control (`0.68` by default) to preserve visible gaps without
temporal sparkle. Tune `Pointsize`, `Pointopacity`, and `Pointkeep` on the
`POINT_RENDER` component. The fog/noise and procedural backfill passes remain
downstream and fill disocclusions without replacing the clean point render.
Set `render.point_keep_fraction` in the runtime profile to persist the visible
fraction across relaunches; use `1.0` when fine image detail matters more than
the sparse-cloud look.

The TOP-to-POP channel mappings are deliberately component-qualified:
`r g b a` maps to `P(0) P(1) P(2) active` for position and to
`Color(0) Color(1) Color(2) Color(3)` for color. Repeating `P` or `Color` four
times asks each scalar TOP channel to provide a complete vector and produces
the `More attribute values than channels specified` warning. Rebuild the
managed `POINT_RENDER` network if an older saved TOE still contains those
repeated vector scopes.

Sensor interaction samples a bounded 8x8 set of calibrated world-space
occupancy primitives for each generated point. Mask and confidence are applied
once, and force in metres/second is integrated with a clamped render delta.
This removes same-UV coupling and frame-rate-dependent acceleration, but it is
still a low-resolution approximation rather than a full SDF, tracked skeleton,
controller volume, or collision solver.

## Live-validation status

The tracked canonical project is build `1.2.1`. It was rebuilt, rendered, and
saved in TouchDesigner 2025.32820 on the RTX 3080 Ti Laptop 16 GB machine. A
combined-mode synthetic check passed managed node-type, exact-resolution,
shader force-cook/compile, operator-error, finite/nonblank readback, metric
camera, and nontrivial capture-file validation for both installation and stereo.
This was a short operator/visual sanity check, not a frame-rate or thermal soak.

After rebuilding the ignored `projects/FlexShow-local.toe`, select the intended
experience and save it. Paste this into a Text DAT in that open project and
choose **Run Script**, or use another in-process TouchDesigner context with the
live `op()` namespace. Do not use system Python or the standalone TouchDesigner
`bin/python.exe`. Preserve each run in a new gitignored evidence directory:

```python
from datetime import datetime, timezone
from pathlib import Path
import sys

root = Path(r'C:\path\to\flexgpu-touchdesigner')
sys.path.insert(0, str(root / 'touchdesigner'))
import validate_project as validator

run = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
report = validator.validate(
    expected_build='1.2.1',
    expected_experience='combined',
    report_path=str(root / 'runtime' / 'td-validation' / run / 'report.json'),
    capture_dir=str(root / 'captures' / 'td-validation' / run),
)
failed = [item for item in report['checks'] if item['status'] != 'pass']
assert report['status'] == 'pass' and not failed, failed
print(report['status'], len(report['checks']), report['report_path'])
```

For a public handoff, remove private/paid components and local paths before the
save, do not change or rebuild the project between saving and validation, and
hash that exact ignored `.toe` after the PASS. Copy only that inspected file to
`projects/FlexShow.toe`, require its SHA-256 to match the validated local file,
then repeat the manual compressed-project inspection before using
`-AllowCanonicalProjectUpdate`. File identity does not prove publication safety.

`validate_project.py` checks build/runtime identity and required operator types,
force-cooks every managed shader and active output, enforces exact active-mode
dimensions, rejects blank/non-finite visual readback, checks managed errors and
metric-camera regressions, and verifies saved synthetic installation/stereo
captures exist with nontrivial size. The report is written atomically. The local
`.toe`, report, and captures are ignored, machine-local artifacts and must not
be synced. A passing report is necessary source/operator validation, not visual
approval, measured metric accuracy, sustained GPU/thermal performance, physical
sensor validation, projector/LED acceptance, or headset/compositor validation.

## Hardware and integration limitations

- The retained 15/15 live-validation baseline used only the RTX 3080 Ti Laptop
  16 GB machine in synthetic combined mode. The 4090 and 5090 presets, other
  single-GPU presets, all dual-GPU presets, and two-machine profiles are
  configuration- and CI-tested starting points, not measured throughput,
  thermal, latency, or failover results.
- StreamDiffusionTD was not bundled or run during validation. Its image format,
  model latency, VRAM use, and depth availability must be tested after the
  private `.tox` is connected.
- The sensor branch currently uses simulated or placeholder replay positions.
  Its local adapter accepts sensor-local XYZ in metres and applies a validated
  sensor-to-world transform, but it does not bundle a depth-camera SDK or body
  tracking. Physical calibration still has to be measured and verified onsite.
- `OUT_LEFT_EYE`, `OUT_RIGHT_EYE`, and `OUT_STEREO_PREVIEW` are desktop
  textures. There is no OpenXR/OpenVR TOP, headset pose, controller input, lens
  distortion, compositor submission, or headset timing validation.
- `OUT_INSTALLATION` is a development image. Projector/LED mapping, Window
  COMPs, color calibration, genlock, failover, and venue output tests are not
  included.
- The temporal branch now retains position, color, confidence, and normalized
  age with automatic contract resets. It remains a compact per-pixel GPU
  lifecycle rather than optical-flow reprojection or a general-purpose particle
  solver. SHARP/Gaussian nodes are disabled contracts and contain no inference.
- Configured frame-state operators are sampled into lifecycle state,
  `LIVE_HEALTH`, and the launcher application heartbeat. A strict
  `flexgpu-camera-metadata/v1` source operator can now apply frame-bound metric
  depth scale, intrinsics, near/far range, and a rigid camera-to-world matrix.
  Unknown fields, mismatched frames, invalid transforms, and same-session
  calibration drift fail closed.
- Every explicit sensor `FRAME_STATE`, including a paid-app direct TOP adapter,
  locks its calibration ID/digest for one producer session. Same-session drift
  zero-gates interaction without replacing the lock. A deliberate identity
  change requires a new unique session and resets temporal history when
  accepted; loaded sensor or legacy shared calibration remains authoritative.
- The atomic Shared Mem/Touch `.toe` preview bridge is installed automatically,
  and it carries RGB/raw depth/mask/confidence atomically. The 32-bit float
  atlas itself has no producer session/timestamp strings, camera matrices,
  heartbeat/control, or replay. Touch TCP's `num_received_frames` supplies
  transport-arrival preview pacing, not producer-exact ordering. Shared Mem has
  no metadata-less receive fallback and requires a separately transported strict
  frame-state sidecar. The separate WorldBus Python reference remains the full
  contract and needs a production TD adapter when those semantics are required.
- JSON loading is tolerant and recognizes simple top-level values plus
  `flexgpu`, `runtime`, `show`, and `profile` sections. Every JSON leaf is still
  exposed in `CONFIG/profile_flat` even when it is not a recognized bootstrap
  setting.
- Startup callbacks are best effort because Execute DAT parameter names can
  differ across experimental builds. Manual `runtime_helpers.module.apply(...)`
  is the reliable fallback.
- The script creates a `.toe` only when run inside TouchDesigner with
  `save=True`. The included `projects/FlexShow.toe` is a generated convenience
  artifact; the human-readable builder remains the source of truth.

For the process split and failure behavior, see
[`docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md). For the transport/frame
contract, see [`docs/WORLDBUS.md`](../docs/WORLDBUS.md).
