# FlexGPU architecture

FlexGPU keeps the artistic network independent from the number and model of
GPUs.  Configuration decides where a role runs; all roles exchange the same
WorldBus frame.

## Roles

| Role | Owns | Timing rule |
| --- | --- | --- |
| `ai` | StreamDiffusionTD, optional monocular depth/geometry, RGB/depth packet production | Asynchronous, queue depth one |
| `world` | Depth sensor, audience forces, persistent point simulation, completion, calibration | Authoritative simulation clock |
| `installation` | Projection/LED cameras and output | 60 Hz target (30 Hz fallback) |
| `vr` | Head tracking, stereo view, controller forces | Headset clock; never blocked by AI |

In a single-GPU profile these roles share one TouchDesigner process.  In a
dual-local profile the AI role gets one process/GPU and the show roles get a
second process/GPU.  In a dual-network profile the same split crosses a wired
network.

## Data flow

```text
camera / prompt
       |
       v
StreamDiffusionTD ---- optional depth estimator
       |                        |
       +------ WorldBus frame --+
                    |
                    v
        validate, drop stale frames
                    |
                    v
 depth sensor -> persistent point world <- VR/controller/audience forces
                    |
           completion selector
          /         |          \
   fog/thick     procedural    hybrid
          \         |          /
                    v
        installation and/or VR render
```

The world continues to simulate when an AI frame is late.  New target geometry
is cross-faded in; it is never awaited by the render callback.

## Deployment matrix

| Topology | AI placement | Show placement | Transport |
| --- | --- | --- | --- |
| `single` | selected render GPU | same process/GPU | internal TOP/CHOP |
| `dual_local` | selected AI GPU | selected render GPU | Global Shared Memory |
| `dual_network`, `node_role=ai` | this computer | remote show computer | Touch Out TOP + OSC |
| `dual_network`, `node_role=render` | remote AI computer | this computer | Touch In TOP + OSC |

TouchDesigner GPU affinity is process-level.  The launcher therefore starts
one process per assigned GPU using PCI bus IDs rather than assuming Windows GPU
indices are stable.  CUDA selection uses the matching UUID/index before the AI
runtime imports CUDA.

## Quality policy

The quality tier controls only budgets.  It does not alter the network
contract or the interaction design.

| Tier | AI intent | Geometry intent | Experience support |
| --- | --- | --- | --- |
| `3080ti_16gb` | SD-Turbo, 384-512, one step, time-sliced geometry | 50k-250k according to mode | installation, VR, combined-lite |
| `4090` | 512-768, optional extra conditioning when measured safe | 150k-500k | installation, VR, combined |
| `5090` | 512-1024 experiments, higher update rate/reserve | 250k-1M, subject to show FPS | installation, VR, combined |

Budgets are conservative starting points, not benchmark promises.  The
operator should lower AI resolution/rate before lowering installation or VR
render cadence.

## Failure behavior

- Only a complete WorldBus frame becomes current.
- The inbound queue retains at most the newest frame.
- A stale AI feed freezes the last valid target, then lets it dissolve through
  the selected fog/procedural completion while sensor interaction continues.
- A stale world snapshot in VR retains the last point buffer while head pose
  continues to update locally.
- Each process writes its PID and heartbeat separately, so the AI worker can be
  restarted without restarting projection or VR.

## Deliberate boundaries

The scaffold does not vendor StreamDiffusionTD, a depth model, headset runtime,
or camera SDK.  Those are installation-specific and are connected at the
labeled adapter components.  It also does not claim that SHARP, 3D Gaussian
Splatting, Dynamic 3D Gaussians, or 4D Gaussians are frame-by-frame live
reconstructors in the 3080 preset.  Those can be added later as optional slow
workers without changing WorldBus.
