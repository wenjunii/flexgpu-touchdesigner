# FlexGPU architecture

FlexGPU keeps the artistic network independent from the number and model of
GPUs. Configuration decides where a role runs; every deployment preserves the
same internal texture contracts. A synthetic source and sensor make
the TouchDesigner builder pipeline testable before site-specific adapters
arrive. The v1.2.1 source/configuration foundation and rebuilt public synthetic
`projects/FlexShow.toe` are present in this repository.

## Roles

The table below is the production target defined by the v1.2.1 builder sources.
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
 RGB + raw depth + mask/confidence + optional strict frame state
                    |
 local TOPs OR atomic RGBA32F image atlas (all four image planes)
                    |
 calibration ------>+------> depth-to-position GLSL
 intrinsics/depth/  |          + valid confidence
 camera transform  |
                    |
 simulated/adapter-replay/private sensor --> bounded world-space interaction
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

The world continues to simulate when an AI frame is late. Strict metadata turns
each advancing producer frame into a one-cook pulse; held textures age/decay
instead of being reabsorbed every render cook. For local source adapters, an
operator cook token provides the fallback boundary, while `legacy_each_cook`
preserves metadata-less adapters without claiming producer freshness. Split
receivers use the stricter transport-specific behavior described below. New
target geometry is cross-faded in; it
is never awaited by the render callback. A material
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
Its role bridge packs RGB plus raw depth/mask/confidence into one RGBA32F atlas
for Shared Mem or Touch TCP split roles; single topology bypasses that
pack/unpack path. The point-render branch preserves world metres through a
Geometry/Camera/Render path and uses parallel eye cameras shifted by half-IPD.
If those operators are unavailable it uses an honest non-normalizing mono
fallback instead of fake toe-in stereo.

Stable outputs are `OUT_POSITION`, `OUT_COLOR`, `OUT_INTERACTION`,
`OUT_INSTALLATION`, `OUT_TRIPLE_WRAP_LEFT/CENTER/RIGHT`,
`OUT_TRIPLE_WRAP`, `OUT_TRIPLE_ARTISTIC_LEFT/CENTER/RIGHT`,
`OUT_TRIPLE_ARTISTIC`, `OUT_DISPLAY_ACTIVE`, `OUT_LEFT_EYE`,
`OUT_RIGHT_EYE`, and `OUT_STEREO_PREVIEW`. These are textures, not a promise
that projection mapping or a headset compositor is configured.

The original single output is unchanged. Panoramic wrap uses three cameras at
one common origin with yaw `-A / 0 / +A`; its FOV and yaw must be calibrated to
the physical surfaces for continuous seams. Artistic multi-angle moves and
turns the side cameras, intentionally trading seam continuity for parallax.
Both modes expose independent surface feeds and a horizontal mosaic. A root
switch selects one of the three installation modes for `OUT_DISPLAY_ACTIVE`;
fixed outputs stay addressable. Render TOPs remain demand-driven, so retaining
all modes does not require every view to cook during ordinary playback.

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

The point renderer keeps these concerns separate. The TOP-to-POP conversion
stores aligned image color as a point `Color` attribute, applies a circular
alpha glyph, and uses fixed-seed thinning to leave legible gaps. Fog and
procedural completion are composited afterward; they do not turn the generated
image into the point-sprite texture.

Sensor mask/confidence are applied once after rigid sensor-to-world calibration.
Each generated point samples a bounded 8x8 set of sensor occupancy primitives
in shared-world metres, and force in metres/second is integrated with a clamped
render delta. This is a low-resolution occupancy/SDF approximation, not a full
volume, skeleton tracker, controller collider, or general physics solver.

The parallel-eye textures are headset-independent development views. They do
not consume runtime head pose, asymmetric per-eye projection, predicted display
time, hidden-area mesh, controller state, late-latching, or compositor textures.

## Deployment matrix

| Topology | AI placement | Show placement | Transport |
| --- | --- | --- | --- |
| `single` | selected render GPU | same process/GPU | internal TOP/CHOP |
| `dual_local` | selected AI GPU | selected render GPU | one atomic RGBA32F atlas over loopback Touch TCP by default; Shared Mem is an explicit-metadata advanced path |
| `dual_network`, `node_role=ai` | this computer | remote show computer | one atomic RGBA32F atlas over Touch Out TOP |
| `dual_network`, `node_role=render` | remote AI computer | this computer | one atomic RGBA32F atlas over Touch In TOP |

TouchDesigner GPU affinity is process-level.  The launcher therefore starts
one process per assigned GPU using PCI bus IDs rather than assuming Windows GPU
indices are stable.  CUDA selection uses the matching UUID/index before the AI
runtime imports CUDA.

The built-in atlas is a direct image bridge, not WorldBus v1. Its right half
stores raw depth, confidence, and mask in R/G/B without clamping calibrated
values. Local adapters may publish strict frame state, but producer IDs/clocks,
camera transforms, network heartbeats, and controls do not cross in the image
atlas. Touch TCP uses Touch In's `num_received_frames` for transport-arrival
preview pacing and a local timeout; this does not identify the producer
generation. Shared Mem has no corresponding receive counter, so it fails closed
without a producer-backed frame-state sidecar that resolves in both roles. See
[`DUAL_GPU_RUNTIME.md`](DUAL_GPU_RUNTIME.md) for runtime details and
[`WORLDBUS.md`](WORLDBUS.md) for the production full-contract boundary.

## Commissioning and calibration plane

Commissioning data is separate from the show transport. The deterministic
generator writes synchronized RGB, depth, mask, confidence, calibration, and
per-frame state plus a SHA-256 manifest. Inspection validates safe relative
paths, exact media layout and scalar samples, monotonic frame/session state,
matching calibration ID plus canonical content digest, recomputed validity and
confidence metrics, and hashes by default. Generation occurs in a private
staging directory and is atomically published only after deep validation:

```powershell
python tools/commission_flexshow.py demo --output commissioning/demo --frames 8
python tools/commission_flexshow.py inspect commissioning/demo/manifest.json
python tools/commission_flexshow.py calibration config/calibration.example.json
```

The public example and generated bundle prove parser/adapter contracts only.
A production profile must be measured for the actual camera, depth convention,
intrinsics, depth range/scale, camera-to-world transform, and sensor-to-world
transform. The two transforms must be rigid, orthonormal and right-handed;
metric scale is represented in depth conversion. A canonical
`calibration_digest` binds semantic calibration content independently of its
filename and human-readable ID. Physical camera/sensor alignment and
projector/LED mapping were not
validated in this update. The builder does not automatically play a generated
commissioning bundle; a source/sensor replay adapter must be wired before those
fixtures can drive the world.

Real calibration, audience RGB/depth/mask/confidence, commissioning bundles,
and recordings stay in ignored machine-local paths. Capture requires a
site-appropriate consent, access, retention, and deletion policy. A publication
guard is defense in depth, not a privacy or legal determination.
It also blocks recognized machine-local calibration, frame-state,
commissioning, hardware, runtime, telemetry, validation, support, and capture
JSON/JSONL by content after a rename. The sole calibration exception is the
exact synthetic public fixture at `config/calibration.example.json`.

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
explicit option for replacing a healthy AI process.

The launcher gives each child a session-bound heartbeat path in the ignored
runtime directory. TouchDesigner atomically publishes role/PID, build/config
identity, cook count/timing, source/sensor age, transport state, and active
outputs. Read-only status classifies an identity-matched child as `alive`,
`ready`, or `stale`; `supervisor` configuration or `-WaitReadyMs` can require a
bounded ready state during Start/recovery. A readiness failure stops the newly
launched child. This remains operator-authorized supervision, not an autonomous
service, and application readiness is separate from WorldBus network heartbeat.
Required readiness applies to a v1.2.1 project. The tracked synthetic canonical
`.toe` publishes this heartbeat; older or privately modified projects must be
rebuilt before readiness is required. Readiness also requires accepted live
source frames, so a stopped StreamDiffusion source intentionally reports
`source_not_accepted`. External TOX roots remain visible to propagated-error
inspection, while their paid/private internals are opaque to the bounded
managed-operator traversal.

Config cannot override launcher-owned `CUDA_*`/`FLEXGPU_*` values. Secret-like
environment and command values are redacted from public plans, diagnostics,
manifests, and errors, though credentials must still remain outside Git and
private `.tox`/paid components remain outside the repository.

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
| `5090` | 384-512 diffusion budget, 10-20 Hz target state | 256-512 geometry, up to 262,144 points by default | installation, desktop-stereo (`vr`), combined |

Budgets are conservative starting points, not benchmark promises.  The
operator should lower AI resolution/rate before lowering installation or VR
render cadence. The 5090 default matches the physical sample count of a
512-square position texture; a larger point budget requires an explicit larger
geometry resolution.

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

The built-in direct role bridge keeps RGB/raw-depth/mask/confidence atomic and
never enables the local generator in a split world process. Local strict frame
state rejects regressions and produces one-cook pulses. Because producer
metadata is not serialized into the atlas, Touch TCP's
`num_received_frames` counter provides transport-arrival preview semantics only;
it is not producer-exact newest-frame rejection. Shared Mem without explicit
producer metadata fails closed rather than treating receiver cooks as new
frames. A transport interruption remains visible as receiver state rather than
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
production integration step; the already implemented Touch bridge and advanced
sidecar-backed Shared Mem path are intentionally smaller direct-preview paths. See
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

The rebuilt v1.2.1 canonical synthetic project passed strict combined-mode
operator, shader, resolution, signal, and capture checks in TouchDesigner
2025.32820 on an RTX 3080 Ti Laptop 16 GB GPU. No result here demonstrates a
private StreamDiffusionTD model running at its configured cadence, physically
measured sensor calibration, audience tracking, projector/LED mapping, headset
pose or controller input, compositor submission, stereo comfort, or sustained
single-/dual-GPU show performance. Each remains an explicit commissioning and
acceptance test on the target hardware and venue.

After rebuilding an ignored `projects/FlexShow-local.toe` inside TouchDesigner,
`touchdesigner/validate_project.py` can force-cook managed shaders and active
outputs, enforce exact dimensions/types, inspect errors and signal health,
check metric-render regressions, and verify optional synthetic captures. Its
report, captures, and local `.toe` stay ignored and are blocked from public
sync. This source/operator validation is not a substitute for visual, physical,
thermal, latency, headset, or venue acceptance.
