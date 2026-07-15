# FlexGPU architecture

FlexGPU keeps the artistic network independent from the number and model of
GPUs. Configuration decides where a role runs; every deployment preserves the
same internal texture contracts. A synthetic source and sensor make
the stock TouchDesigner pipeline testable before site-specific adapters arrive.

## Roles

The table below is the production target. The stock `.toe` implements the
source/world boundaries, installation texture, and desktop stereo textures; it
does not include a real StreamDiffusion component, sensor SDK, projection
mapping, controller input, or headset compositor.

| Role | Owns | Timing rule |
| --- | --- | --- |
| `ai` | Demo source or user-supplied StreamDiffusionTD/depth adapter and RGB/depth packet production | Configured update-rate budget; production adapter should be asynchronous with queue depth one |
| `world` | Simulated/replay sensor boundary, audience forces, persistent point simulation, completion | TouchDesigner frame clock; production sensor/calibration remains user-supplied |
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
demo RGB/depth OR StreamDiffusionTD adapter
                    |
     local TOP OR atomic RGB/depth preview atlas
                    |
                    v
          depth-to-position GLSL
                    |
 simulated/replay/real sensor -> interaction field
                    |
                    v
       GPU temporal persistence feedback
                    |
          completion selector
         /          |          \
 thick/fog     procedural     hybrid
         \          |          /
                    v
      point-render contract and fallback
              /              \
 installation preview     stereo desktop preview
```

The world continues to simulate when an AI frame is late.  New target geometry
is cross-faded in; it is never awaited by the render callback.

## Working TouchDesigner pipeline

`touchdesigner/runtime_pipeline.py` creates only
`/project1/flexgpu/WORKING_PIPELINE` and preserves unknown operators. It uses
stock TouchDesigner 2025 operators for animated demo color/depth,
depth-to-position, simulated audience forces, feedback persistence,
disocclusion fog, procedural backfill, hybrid selection, and output previews.
Its role bridge packs RGB/depth into one RGBA16F atlas for Shared Mem or Touch
TCP split-role previews; single topology bypasses that pack/unpack path.
The point-render branch uses TouchDesigner's TOP-to-POP/Render Simple operators
when available and retains an inspectable color fallback if an operator cannot
be created.

Stable outputs are `OUT_POSITION`, `OUT_COLOR`, `OUT_INTERACTION`,
`OUT_INSTALLATION`, `OUT_LEFT_EYE`, `OUT_RIGHT_EYE`, and
`OUT_STEREO_PREVIEW`. These are textures, not a promise that projection
mapping or a headset compositor is configured.

The future StreamDiffusionTD replacement is intentionally narrow:

```text
your StreamDiffusionTD.tox RGB ----> STREAMDIFFUSION_ADAPTER/OUT_RGB
your depth output/estimator -------> STREAMDIFFUSION_ADAPTER/OUT_DEPTH
```

Disconnect the labelled placeholder Constant TOPs from those two Out TOPs,
then switch the source adapter after the replacement outputs satisfy the
documented RGB and normalized-depth contracts. The reconstruction, persistence,
interaction, completion, and render branches do not need to move. A camera SDK
replaces the simulated/replay sensor boundary in the same way. Headset
submission consumes the stereo/world contracts through a user-supplied
OpenXR/OpenVR component.

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
