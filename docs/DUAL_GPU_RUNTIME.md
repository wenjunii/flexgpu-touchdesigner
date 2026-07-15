# Dual-GPU and two-machine runtime

Runtime builder source `1.2.0` defines a direct image bridge at
`/project1/flexgpu/WORKING_PIPELINE/ROLE_BRIDGE`. The launcher starts the same
project once per assigned role; startup policy activates only the stages owned
by that role. The tracked `projects/FlexShow.toe` was not rebuilt for this v1.2
update, so rebuild and inspect a local project before relying on the new
calibration, temporal-lifecycle, or auto-load behavior.

| Process role | Cooks | Does not cook |
| --- | --- | --- |
| `ai` | demo or `STREAMDIFFUSION_ADAPTER`, atomic atlas pack and sender | reconstruction, sensor, persistence, completion, point rendering, installation, stereo |
| `world` in a split topology | atlas receiver/unpack, reconstruction, sensor, persistence, completion, selected outputs | demo and `STREAMDIFFUSION_ADAPTER`, sender, unselected output |
| `world` in `single` | local source and complete selected show pipeline | transport endpoints and unselected output |

This preserves the StreamDiffusion replacement boundary. Manual wiring under
`WORKING_PIPELINE/SOURCES/STREAMDIFFUSION_ADAPTER` remains supported. For a
private config-driven integration, set `source.auto_load_tox: true`, point
`streamdiffusion_tox` at an ignored local file, and provide `rgb_operator` plus
any optional depth/mask/confidence operators inside it. In a split plan, role
gates restrict source ownership to AI: the AI process loads/cooks the source
component and the world process does not import it. Sensor `.tox` ownership is
the inverse and is limited to world. The role bridge remains downstream and
needs no model-specific change. A missing file, load error, or unresolved
required output keeps the demo active and records fallback state. Auto-load
does not install the model, Python/CUDA dependencies, prompts, weights, or
licenses.

With `tier: auto`, the launcher resolves each process against its assigned GPU
and injects a separate `FLEXGPU_TIER` plus quality limits. A heterogeneous 5090
AI / 3080 Ti world pair therefore keeps 5090 source budgets on AI and 3080 Ti
geometry/point budgets on world. The planner starts the world/listener process
before its dependent AI sender.

## Atomic image atlas

Before crossing a process boundary, the AI role packs one RGBA16F TOP:

- left half: RGB;
- right half: normalized `0..1` depth copied into the color channels.

The world role receives and unpacks that one TOP. The atlas width is forced
even so both halves have an integer width. RGB and depth therefore come
from the same transmitted frame, including on a network link. Atlas resolution
comes from `transport.atlas_width` and `transport.atlas_height`; cadence comes
from `transport.atlas_fps`.

Single topology bypasses both pack and unpack, so it keeps the direct local TOP
path.

## Choose local GPU placement

Before writing UUIDs into a local preset, capture one read-only hardware
snapshot:

```powershell
python tools/profile_flexshow.py `
  --topology dual_local `
  --output runtime/hardware-profile.json
```

The recommendation considers current VRAM capacity/headroom and display
ownership and reports UUID/PCI identity, load, thermals, clocks, and optional
power values. It is only an initial commissioning hint. It does not benchmark
StreamDiffusionTD or rendering, run a thermal soak, reassign roles dynamically,
or plan `dual_network`. Test both sensible placements with the complete workload,
save the selected UUIDs in a gitignored config, and do not move roles while the
show is running.

## Dual local

The included dual-local presets use one global Shared Mem TOP block named
`<transport.segment_name>_atlas`. The frame-start callback force-cooks Shared
Mem Out at the configured cadence even though all world/render stages are
disabled in the AI process.

Shared Mem TOPs require a TouchDesigner Educational, Commercial, or Pro
license. If that path is unavailable, set the dual-local `transport.type` to
`touch_tcp` and `transport.peer_host` to `127.0.0.1`; the same two-process plan
uses one loopback Touch stream. See the official [Shared Mem Out TOP](https://docs.derivative.ca/Shared_Mem_Out_TOP)
and [Shared Memory](https://docs.derivative.ca/Shared_Memory) documentation.

## Two machines

`dual_network` uses one uncompressed Touch TCP stream on
`transport.atlas_port`. The world process points `RX_TCP_ATLAS` at
`transport.peer_host`, which must be the AI machine's reachable address. Permit
that port through Windows Firewall. Start at `atlas_fps: 5`; raise it only after
checking bandwidth and end-to-end latency. See the official
[Touch Out TOP](https://docs.derivative.ca/Touch_Out_TOP) and
[Touch In TOP](https://docs.derivative.ca/Touch_In_TOP) documentation.

Touch Out's parameter named `fps` is a frame-step value. Runtime derives it
from `project.cookRate / transport.atlas_fps` and records both
`transport_send_step` and `transport_effective_fps` in runtime state.

## Scope: direct preview bridge, not WorldBus v1

`ROLE_BRIDGE` intentionally transports only the atomic RGB/depth atlas. It does
not implement WorldBus v1 frame/session IDs, camera intrinsics/transforms,
mask/confidence, heartbeats, interaction/control messages, replay, sender
authentication, newest-frame rejection, or an explicit stale/drop/hold policy.
The implemented Python reference in
[`WORLDBUS_REFERENCE.md`](WORLDBUS_REFERENCE.md) remains the production
contract for an adapter that needs those fields. Do not describe the built-in
Touch/Shared-Mem bridge as WorldBus v1.

## World-side calibration and completion

Reconstruction, temporal persistence, interaction, fog, procedural backfill,
and point rendering cook only in the `world` process. A local validated
`flexgpu-calibration/v1` profile can supply depth convention/range, intrinsics,
and camera-to-world/sensor-to-world transforms. Position, color, confidence,
and normalized age then persist between received AI updates, and a material
resolution/session/calibration/adapter contract change resets that history.

The direct atlas carries only RGB and depth. Source mask, confidence, frame
state, camera metadata, and calibration are **not** transmitted. A split show
that needs those fields must provide matching local calibration/defaults on the
world side or implement the full WorldBus adapter; never assume they crossed
with the atlas. This is especially important for two-machine profiles, where
private config-relative files must exist on the node that consumes them. A
world receiver applies an explicit shared source calibration without importing
the AI `.tox`; if that calibration is invalid, world and output stages are
disabled instead of rendering a knowingly wrong reconstruction.

Thick point size and view-specific disocclusion fog help cover temporal gaps;
procedural backfill writes invented volume only into missing-position holes,
and hybrid blends those approaches. These are continuity effects, not true
hidden-surface recovery. Fog can conceal a seam but cannot restore missing
geometry or make stale transport correct.

## Runtime inspection

Inspect these operators while both processes are running:

- `ROLE_BRIDGE/TRANSPORT_CONTRACT` for atlas layout and transport scope;
- `CONFIG/runtime_state` for `bridge_mode`, sender/receiver flags, cadence and
  every stage gate;
- `ROLE_BRIDGE/RX_TCP_ATLAS_INFO` for connection, receive FPS and queue status
  when using TCP.

The split `world` role never falls back to its local generator. That prevents a
transport interruption from silently duplicating AI work on the render GPU.

## Status and bounded AI recovery

Inspect launcher-owned state without creating a runtime directory, acquiring a
mutation lock, starting a process, or sending a shutdown signal:

```powershell
.\scripts\Status-FlexShow.ps1 `
  -Config config\presets\local-show.json
.\scripts\Status-FlexShow.ps1 `
  -Config config\presets\local-show.json `
  -Json
```

Status distinguishes stopped, running, transitional, degraded, and ownership
error session state and reports each role/PID/launch state with identity
verification. It is manifest/process status, not a proof that TouchDesigner is
cooking useful frames.

Recovery is also a preview unless explicitly authorized:

```powershell
.\scripts\Recover-FlexShow.ps1 `
  -Config config\presets\local-show.json
.\scripts\Recover-FlexShow.ps1 `
  -Config config\presets\local-show.json `
  -Attempts 2 `
  -Recover
```

It supports one through three bounded attempts and only a separately planned
AI role. It requires the world dependency to be healthy, reruns preflight, and
never implicitly restarts world/render. A healthy AI is reused unless the
operator explicitly adds `-RestartRunning -Recover`. This is not a background
watchdog and does not add heartbeats to the direct atlas bridge.

## Live-validation status

Source tests cover atlas configuration, role policy/stage gates, per-role tier
injection, configuration contracts, and launcher ownership/recovery behavior.
The v1.2 source foundation has **not** been rebuilt into or visually validated
through the tracked `projects/FlexShow.toe` for this update. It has not been run
here with the private StreamDiffusionTD `.tox`, a simultaneous physical
dual-GPU workload, a two-machine Touch TCP soak, a depth sensor, projection/LED
outputs, or a headset. No throughput, failover-time, calibration, stereo
comfort, or venue-readiness claim should be inferred from the automated tests.
