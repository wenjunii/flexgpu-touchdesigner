# FlexGPU architecture

FlexGPU keeps the artistic network independent from the number and model of
GPUs. Configuration decides where a role runs; every deployment preserves the
same internal texture contracts. A synthetic source and sensor make
the TouchDesigner builder pipeline testable before site-specific adapters
arrive. The v1.2 source/configuration foundation is present in this repository,
but the tracked `projects/FlexShow.toe` was not rebuilt or visually validated
for this update.

## Roles

The table below is the production target defined by the v1.2 builder sources.
It does not include a real StreamDiffusion component, sensor SDK, projection
mapping, controller input, or headset compositor.

| Role | Owns | Timing rule |
| --- | --- | --- |
| `ai` | Demo source or user-supplied StreamDiffusionTD/depth adapter and RGB/depth packet production | Configured update-rate budget; production adapter should be asynchronous with queue depth one |
| `world` | Simulated or adapter-supplied replay/sensor boundary, calibrated reconstruction, audience forces, confidence/age lifecycle, persistent point simulation, completion | TouchDesigner frame clock; production sensor capture and measured calibration remain user-supplied |
| `installation` | Development installation texture | Target FPS is metadata until venue output is integrated |
| `vr` | Left/right eye textures and desktop stereo preview | TouchDesigner frame clock; head tracking, controllers, and headset clock are future adapter responsibilities |

In a single-GPU profile these roles share one TouchDesigner process.  In a
dual-local profile the AI role gets one process/GPU and the show roles get a
second process/GPU.  In a dual-network profile the same split crosses a wired
network.

## Data flow

```text
camera / prompt
       |
       v
demo OR private StreamDiffusionTD adapter
 RGB + depth + optional mask/confidence/frame state
                    |
 local TOP (all fields) OR atomic preview atlas (RGB/depth only)
                    |
 calibration ------>+------> depth-to-position GLSL
 intrinsics/depth/  |          + valid confidence
 camera transform  |
                    |
 simulated/adapter-replay/private sensor --> calibrated interaction field
                    |
                    v
 temporal position/color/confidence/age feedback
                    |
          hole-only completion selector
         /             |             \
 thick + fog      procedural       hybrid
         \             |             /
                    v
      point-render contract and fallback
              /              \
 installation texture     stereo development textures
```

The world continues to simulate when an AI frame is late. New target geometry
is cross-faded in; it is never awaited by the render callback. A material
contract change—resolution, source session/calibration epoch, calibration
values, depth convention, transform, or adapter identity—resets temporal
history. Reapplying the same contract does not.

## Working TouchDesigner pipeline

`touchdesigner/runtime_pipeline.py` creates only
`/project1/flexgpu/WORKING_PIPELINE` and preserves unknown operators. It uses
stock TouchDesigner 2025 operators for animated demo color/depth,
calibrated depth-to-position, simulated audience forces, position/color plus
confidence/age feedback, disocclusion fog, hole-only procedural backfill,
hybrid selection, and output previews.
Its role bridge packs RGB/depth into one RGBA16F atlas for Shared Mem or Touch
TCP split-role previews; single topology bypasses that pack/unpack path.
The point-render branch uses TouchDesigner's TOP-to-POP/Render Simple operators
when available and retains an inspectable color fallback if an operator cannot
be created.

Stable outputs are `OUT_POSITION`, `OUT_COLOR`, `OUT_INTERACTION`,
`OUT_INSTALLATION`, `OUT_LEFT_EYE`, `OUT_RIGHT_EYE`, and
`OUT_STEREO_PREVIEW`. These are textures, not a promise that projection
mapping or a headset compositor is configured.

The StreamDiffusionTD replacement is intentionally narrow:

```text
your StreamDiffusionTD.tox RGB ----> STREAMDIFFUSION_ADAPTER/OUT_RGB
your depth output/estimator -------> STREAMDIFFUSION_ADAPTER/OUT_DEPTH
```

Disconnect the labelled placeholder Constant TOPs from those two Out TOPs,
then switch the source adapter after the replacement outputs satisfy the
documented image/depth contracts: normalized depth without a profile, or the
declared normalized/metric/disparity/inverse-depth convention with a validated
calibration profile. The reconstruction, persistence, interaction, completion,
and render branches do not need to move. A camera SDK
replaces the simulated/replay sensor boundary in the same way. Headset
submission consumes the stereo/world contracts through a user-supplied
OpenXR/OpenVR component.

Manual wiring remains the default. With `source.auto_load_tox: true`, the
runtime can instead load one ignored local `.tox` into an `AUTO_LOADED_TOX`
holder and resolve configured RGB plus optional depth/mask/confidence TOPs.
`rgb_operator` is required. The equivalent sensor path requires
`sensor.auto_load_tox`, `adapter_tox`, and `position_operator`. Config-relative
path resolution and required-output validation occur before switching away
from the demo/simulated input; a failure records fallback state instead of
cooking an incomplete adapter. The loader does not provision model weights,
Python/CUDA packages, prompts, sensor SDKs, or licenses.

Role ownership also applies to imports: in a split topology only the AI process
loads the source `.tox`, and only the world process loads the sensor `.tox`.
The world receiver can apply the shared source calibration to depth arriving
through the bridge without importing the private AI component. If that
explicit remote calibration is invalid, world and output stages stay disabled
rather than rendering a spatially incorrect reconstruction.

The completion branches are visual continuity tools, not hidden-surface
reconstruction. Thick points cover sparse samples. Fog looks for nearby
persistent geometry at disocclusion gaps and adds noise/fog per installation or
eye view. Procedural backfill writes only where original position alpha is
missing; hybrid blends procedural volume with fog in those holes. Fog can hide
seams and backfill can invent plausible shape, but neither recovers ground
truth.

## Deployment matrix

| Topology | AI placement | Show placement | Transport |
| --- | --- | --- | --- |
| `single` | selected render GPU | same process/GPU | internal TOP/CHOP |
| `dual_local` | selected AI GPU | selected render GPU | one atomic RGBA16F atlas over Global Shared Memory by default, or loopback Touch TCP fallback |
| `dual_network`, `node_role=ai` | this computer | remote show computer | one atomic RGBA16F atlas over Touch Out TOP |
| `dual_network`, `node_role=render` | remote AI computer | this computer | one atomic RGBA16F atlas over Touch In TOP |

TouchDesigner GPU affinity is process-level.  The launcher therefore starts
one process per assigned GPU using PCI bus IDs rather than assuming Windows GPU
indices are stable.  CUDA selection uses the matching UUID/index before the AI
runtime imports CUDA.

The built-in atlas is a direct RGB/depth preview bridge, not WorldBus v1. It
does not carry frame metadata, camera transforms, heartbeats or controls. See
[`DUAL_GPU_RUNTIME.md`](DUAL_GPU_RUNTIME.md) for runtime details and
[`WORLDBUS.md`](WORLDBUS.md) for the production full-contract boundary.

## Commissioning and calibration plane

Commissioning data is separate from the show transport. The deterministic
generator writes synchronized RGB, depth, mask, confidence, calibration, and
per-frame state plus a SHA-256 manifest. Inspection validates safe relative
paths, media shape/format, monotonic frame/session state, matching calibration
identity, and hashes by default:

```powershell
python tools/commission_flexshow.py demo --output commissioning/demo --frames 8
python tools/commission_flexshow.py inspect commissioning/demo/manifest.json
python tools/commission_flexshow.py calibration config/calibration.example.json
```

The public example and generated bundle prove parser/adapter contracts only.
A production profile must be measured for the actual camera, depth convention,
intrinsics, depth range/scale, camera-to-world transform, and sensor-to-world
transform. Physical camera/sensor alignment and projector/LED mapping were not
validated in this update. The builder does not automatically play a generated
commissioning bundle; a source/sensor replay adapter must be wired before those
fixtures can drive the world.

Real calibration, audience RGB/depth/mask/confidence, commissioning bundles,
and recordings stay in ignored machine-local paths. Capture requires a
site-appropriate consent, access, retention, and deletion policy. A publication
guard is defense in depth, not a privacy or legal determination.

## Placement and supervisor plane

The local profiler reads one `nvidia-smi` snapshot and recommends an initial
single- or dual-local role placement from current capacity/headroom and display
ownership:

```powershell
python tools/profile_flexshow.py --topology dual_local `
  --output runtime/hardware-profile.json
```

This snapshot is not a benchmark, thermal soak, dynamic scheduler, or
two-machine planner. Verify the recommended UUID assignments under the real
AI, sensor, installation, and stereo workload and never reassign GPUs during a
show.

Launcher ownership state has two operator-facing supervisor primitives:

```powershell
.\scripts\Status-FlexShow.ps1 -Config config\presets\local-show.json
.\scripts\Recover-FlexShow.ps1 -Config config\presets\local-show.json
.\scripts\Recover-FlexShow.ps1 `
  -Config config\presets\local-show.json -Attempts 2 -Recover
```

Status is read-only and reports session/manifest/process ownership states.
Recovery is a dry-run until `-Recover`, is limited to one through three attempts,
and can act only on the separate AI role after world dependencies and preflight
pass. It never implicitly restarts world/render; `-RestartRunning` is the
explicit option for replacing a healthy AI process. This is bounded
operator-authorized recovery, not an autonomous service, a TouchDesigner cook
heartbeat, or the future WorldBus heartbeat policy.

## Quality policy

The quality tier controls only budgets. It does not alter the network contract
or the interaction design. With `tier: auto`, the planner resolves a tier for
each assigned process/GPU: a heterogeneous 5090 AI plus 3080 Ti world pair uses
different source and world/render limits rather than inheriting the AI tier in
both processes.

| Tier | AI intent | Geometry intent | Stock branch budget |
| --- | --- | --- | --- |
| `3080ti_16gb` | 384-512 diffusion budget, 5-10 Hz target state | 256-384 geometry, 60k-120k points | installation, desktop-stereo (`vr`), combined-lite |
| `4090` | 384-512 diffusion budget, 8-15 Hz target state | 256-512 geometry, 100k-250k points | installation, desktop-stereo (`vr`), combined |
| `5090` | 384-512 diffusion budget, 10-20 Hz target state | 256-512 geometry, 150k-400k points | installation, desktop-stereo (`vr`), combined |

Budgets are conservative starting points, not benchmark promises.  The
operator should lower AI resolution/rate before lowering installation or VR
render cadence.

## Adaptive quality and telemetry

There are two implementations of the same bounded, hysteretic quality policy:

- `src/flexgpu/adaptive.py` is the dependency-free offline/reference governor.
  Each observation combines frame time, VRAM, and newest-frame queue age. It is
  used by the deterministic benchmark and telemetry replay tools.
- The embedded `STARTUP/runtime_helpers` controller runs at TouchDesigner frame
  start. It currently measures frame interval, applies configured
  high/low/critical thresholds, windows, and cooldown, and moves through
  tier-bounded discrete levels.

At startup and after every live level change, the runtime binds source/depth
resolution, reconstruction resolution, and point budget to the working
operators. It also applies static point thickness, fog/procedural controls,
output sizes, source/sensor selection, role transport, and stage gates. Output
refresh remains the priority. Diffusion/geometry rate values are recorded in
runtime state, but retiming a private `.tox` requires that adapter to consume
them explicitly.

```text
TD frame interval --> embedded frame-start controller --------+
                                                              |
offline frame/VRAM/queue --> AdaptiveQualityGovernor.observe() +
                                                              |
                                                              v
                                                  discrete quality state
                                                              |
                                                              v
                                           apply at a safe update boundary
```

`WORKING_PIPELINE/TELEMETRY` supplies operator timing to the embedded callback.
When enabled, the callback buffers JSONL frame/operator samples, flushes them
at the configured interval, and writes a final summary on exit. The standalone
writer/reader, summaries, and benchmark/replay CLI remain useful for testing
policy without pretending a synthetic run measures a particular GPU.

## Failure behavior

The built-in direct role bridge keeps RGB/depth atomic and never enables the
local generator in a split world process. It does not implement frame IDs,
heartbeats, newest-frame rejection, or an explicit stale/drop/hold policy; a
transport interruption therefore remains visible as receiver state rather than
silently moving AI work onto the render GPU.

The full WorldBus v1 Python reference provides the richer behavior:

- Only a complete validated WorldBus frame becomes current.
- The inbound queue retains at most the newest frame.
- Heartbeats expose alive/stale/expired state without stopping render clocks.
- Frame/session IDs prevent an old producer from rolling the target backward.
- Each process has separate lifetime/heartbeat state, so an AI worker can be
  restarted without redefining the point-world contract.

That reference uses bounded TCP frames, UDP JSON metadata/control/heartbeats,
and `.wbr` replay. Adapting its full contract into TouchDesigner remains a
production integration step; the already implemented Shared Mem/Touch bridge
is intentionally the smaller direct preview path. See
[WORLDBUS_REFERENCE.md](WORLDBUS_REFERENCE.md).

## Deliberate boundaries

The scaffold does not vendor StreamDiffusionTD, a learned depth model, headset
runtime, or camera SDK. Those are installation-specific and connect at labelled
adapter components. SHARP and Gaussian adapters are disabled by default and do
not contain inference code or model weights. The project does not claim that
SHARP, 3D Gaussian Splatting, Dynamic 3D Gaussians, or 4D Gaussians are
frame-by-frame live reconstructors in the 3080 preset. They can be evaluated
later as asynchronous external workers without changing the point-world or
WorldBus contracts.

The v1.2 builder/configuration foundation has not been rebuilt into the tracked
canonical `.toe` for this update. No result here demonstrates a private
StreamDiffusionTD model running at its configured cadence, physically measured
sensor calibration, audience tracking, projector/LED mapping, headset pose or
controller input, compositor submission, stereo comfort, or sustained
single-/dual-GPU show performance. Each remains an explicit commissioning and
acceptance test on the target hardware and venue.
