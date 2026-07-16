# FlexGPU TouchDesigner project

`bootstrap_project.py` and `runtime_pipeline.py` version `1.2.1` build a
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
| `WORKING_PIPELINE/ROLE_BRIDGE` | Atomic RGBA32F RGB/raw-depth/mask/confidence path over local, Shared Mem, or Touch TCP routes |
| `WORKING_PIPELINE/RECONSTRUCTION` | Aligned color and depth-to-position GLSL |
| `WORKING_PIPELINE/SENSOR_INTERACTION` | Calibrated sensor validity plus bounded 8x8 world-space occupancy interaction |
| `WORKING_PIPELINE/TEMPORAL_WORLD` | One-cook frame-aware confidence/age lifecycle plus dt-integrated position/color feedback and automatic contract resets |
| `WORKING_PIPELINE/COMPLETION` | Working fog, procedural and hybrid GLSL branches |
| `WORKING_PIPELINE/POINT_RENDER` | Metric TOP-to-POP point renderer, center/parallel-eye cameras, and honest mono fallback |
| `WORKING_PIPELINE/INSTALLATION_OUTPUT` | Center render and view-space edge-fog development preview |
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
modified projects before enabling readiness waits.

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
  `LIVE_HEALTH`, and the launcher application heartbeat. The separate
  camera-metadata operator is still only a resolved adapter boundary; runtime
  projection/intrinsic changes must come through validated calibration or a
  production metadata adapter.
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
