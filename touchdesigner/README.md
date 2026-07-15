# FlexGPU TouchDesigner project

`bootstrap_project.py` and `runtime_pipeline.py` version `1.1.0` build a
labelled TouchDesigner 2025 project at `/project1/flexgpu`. It includes a
stock-operator `WORKING_PIPELINE` that runs
without third-party packages: animated RGB/depth, depth-to-position GLSL,
temporal feedback, simulated audience interaction, fog/procedural completion,
a point-render path, an installation preview, and a desktop stereo preview.

The repository does not bundle your `StreamDiffusionTD.tox`, a production
depth estimator, a sensor SDK/calibration, an OpenXR/OpenVR runtime, projection
mapping, or SHARP/Gaussian inference. Those remain explicit adapter boundaries.
The demo is the fallback for building and testing everything downstream before
those site-specific pieces are available.

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
| `OUT_INSTALLATION` | Center point-world preview with the fog plate |
| `OUT_STEREO_PREVIEW` | Side-by-side desktop stereo preview |
| `OUT_LEFT_EYE`, `OUT_RIGHT_EYE` | Eye textures for a future headset adapter |
| `OUT_POSITION` | RGBA32F position texture: XYZ metres and active alpha |
| `OUT_COLOR` | Color texture aligned with `OUT_POSITION` |
| `OUT_INTERACTION` | RGB force and alpha occupancy texture |

These are inspectable development outputs. `OUT_INSTALLATION` is not projector
mapping, and the eye textures are not submitted to a headset compositor.

## Replace the demo with your StreamDiffusionTD.tox

The exact adapter COMP is:

```text
/project1/flexgpu/WORKING_PIPELINE/SOURCES/STREAMDIFFUSION_ADAPTER
```

Inside it, keep the output operators and replace their placeholder inputs:

```text
your StreamDiffusionTD RGB TOP ---> OUT_RGB
your depth-estimate TOP ----------> OUT_DEPTH
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
6. After changing source resolution or calibration, pulse Reset on
   `WORKING_PIPELINE/TEMPORAL_WORLD/POSITION_HISTORY` so feedback does not mix
   incompatible position layouts.

Required contracts:

- `OUT_RGB`: RGBA color with alpha 1; RGBA8 or floating-point color is valid.
- `OUT_DEPTH`: normalized R depth in `0..1`, with near at 0 and far at 1. The
  current unprojection treats values very near 0 or 1 as invalid.

If the estimator emits metric depth, disparity, or inverse depth, insert a
conversion TOP before `OUT_DEPTH`; otherwise the shape will be spatially
incorrect even though the network cooks successfully.

Do not replace the whole `SOURCES` or `WORKING_PIPELINE` COMPs. Keeping the two
adapter outputs lets reconstruction, persistence, completion, and all render
outputs remain unchanged. Rerunning the builder preserves an existing input on
`OUT_RGB` or `OUT_DEPTH` inside this adapter.

The optional `source.streamdiffusion_tox` configuration field is declarative;
the stock project does not import a `.tox` from that path automatically. This
prevents a machine-local component from being silently loaded into the public
project.

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
| `WORKING_PIPELINE/ROLE_BRIDGE` | Atomic RGBA16F direct preview over local, Shared Mem, or Touch TCP routes |
| `WORKING_PIPELINE/RECONSTRUCTION` | Aligned color and depth-to-position GLSL |
| `WORKING_PIPELINE/SENSOR_INTERACTION` | Simulated/replay mask and interaction field |
| `WORKING_PIPELINE/TEMPORAL_WORLD` | Position feedback/persistence and sensor forces |
| `WORKING_PIPELINE/COMPLETION` | Working fog, procedural and hybrid GLSL branches |
| `WORKING_PIPELINE/POINT_RENDER` | TOP-to-POP point renderer with inspectable fallback |
| `WORKING_PIPELINE/INSTALLATION_OUTPUT` | Center render and fog development preview |
| `WORKING_PIPELINE/STEREO_PREVIEW` | Left/right and side-by-side desktop previews |
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
automatically packs RGB plus normalized depth into one atomic RGBA16F atlas for
Shared Mem or Touch TCP preview transport. It deliberately is not WorldBus v1;
a production `WORLD_BUS_IN` adapter remains responsible for the full contract.
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

Explicit environment values override `FLEXGPU_CONFIG`. The helper updates
`CONFIG/runtime_state`, endpoint activity, source/receiver route switches and
stage-COMP `allowCooking` gates. Endpoint TOPs use their Active expressions;
Shared Mem Out is pulsed only at its derived send step. The helper does not
change `project.cookRate` or destroy operators. AI roles cook source plus sender
only; split world roles cook receiver, reconstruction, simulation and only the
selected output module.

The same `.toe` can be launched for each role. `ROLE_BRIDGE` is installed and
configured automatically: one global Shared Mem atlas by default (or a loopback
Touch TCP fallback) for `dual_local`, or one uncompressed Touch TCP atlas for
`dual_network`. Single topology bypasses atlas pack/unpack. See
[`docs/DUAL_GPU_RUNTIME.md`](../docs/DUAL_GPU_RUNTIME.md) for
atlas layout, cadence, inspection and the boundary between this preview bridge
and production WorldBus v1.

With `tier: auto`, the launcher injects `FLEXGPU_TIER` and quality limits per
process from its assigned GPU. On a heterogeneous local pair, the AI process
can therefore use a 5090 tier while a 3080 Ti world process keeps its own lower
geometry and point limits.

The generated dashboard is not a finished control surface: its Apply and
Emergency Reset pulses are not wired to callbacks. Launcher environment values
remain authoritative for `single`, `dual_local`, and `dual_network`.

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
budget, 512-square diffusion at 10 Hz, 384-square geometry at 5 Hz, and a 72 Hz
VR target. The diffusion number is a scheduling budget for your future `.tox`,
not performance measured from the synthetic demo. These values are planning
defaults, not guarantees; laptop power limits, thermals, drivers, model choice,
and output resolution materially change throughput.

For a same-GPU combined run, keep the desktop-stereo render as the timing
priority and target no more than roughly 11-12 GB total use in `nvidia-smi`.
Configure the private AI adapter—or a production WorldBus adapter—to keep its
queue at one frame and drop stale AI frames; the stock direct bridge has no
explicit stale-frame policy. Do not add SDXL, Video Depth Anything, SHARP,
Gaussian reconstruction, expensive shadows, or high MSAA until the actual
target system has ample measured headroom.

## Live-validation status

The runtime source is build `1.1.0`. The canonical project was rebuilt, opened,
rendered, and saved in TouchDesigner 2025.32820 on the RTX 3080 Ti Laptop 16 GB
machine. The final idempotent health pass reused 220 managed operators in the
bootstrap report, including 154 runtime-pipeline operators, with zero builder
warnings and no operator errors. The installation preview and combined stereo
desktop preview rendered;
single-role local, AI sender, and world receiver stage gates were also checked.
This was a functional validation, not a throughput guarantee, and it did not
include the private StreamDiffusionTD `.tox`, a physical sensor, or a headset.

## Hardware and integration limitations

- StreamDiffusionTD was not bundled or run during validation. Its image format,
  model latency, VRAM use, and depth availability must be tested after the
  private `.tox` is connected.
- The sensor branch currently uses a simulated or placeholder replay mask. It
  does not include a depth-camera SDK, body tracking, camera intrinsics, or
  installation calibration. A production sensor adapter must provide
  calibrated positions/forces while retaining the public output contracts.
- `OUT_LEFT_EYE`, `OUT_RIGHT_EYE`, and `OUT_STEREO_PREVIEW` are desktop
  textures. There is no OpenXR/OpenVR TOP, headset pose, controller input, lens
  distortion, compositor submission, or headset timing validation.
- `OUT_INSTALLATION` is a development image. Projector/LED mapping, Window
  COMPs, color calibration, genlock, failover, and venue output tests are not
  included.
- The temporal branch is a compact GPU feedback effect, not a general-purpose
  particle solver. SHARP/Gaussian nodes are disabled contracts and contain no
  model inference.
- The atomic Shared Mem/Touch `.toe` preview bridge is installed automatically,
  but it carries RGB/depth only. It has no mask/confidence plane, frame/session
  IDs, camera metadata, heartbeat/control, replay, or explicit stale/drop/hold
  policy. The separate WorldBus Python reference remains the full contract and
  needs a production TD adapter when those semantics are required.
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
