# Dual-GPU and two-machine runtime

Runtime builder source `1.2.1` defines a direct image bridge at
`/project1/flexgpu/WORKING_PIPELINE/ROLE_BRIDGE`. The launcher starts the same
project once per assigned role; startup policy activates only the stages owned
by that role. The tracked `projects/FlexShow.toe` is the rebuilt public v1.2.1
synthetic starter. Keep private adapters and site paths in a separate ignored
local project.

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

The stock 5090 point budget is 262,144, matching every sample in its default
512-square geometry texture. A larger point count requires an explicit larger
geometry resolution; VRAM headroom alone does not create more source samples.

## Atomic image atlas

Before crossing a process boundary, the AI role packs one RGBA32F TOP:

- left half: RGB;
- right-half R: raw depth in the calibration's declared encoding;
- right-half G: normalized confidence;
- right-half B: normalized mask.

The world role receives and unpacks that one TOP. Raw depth is never clamped,
so metres, millimetres, disparity, inverse depth, and normalized values survive
the bridge. The atlas width is forced even so both halves have an integer
width. All four image planes therefore come from the same transmitted frame,
including on a network link. Atlas resolution
comes from `transport.atlas_width` and `transport.atlas_height`; cadence comes
from `transport.atlas_fps`.

RGBA32F preserves raw metric, millimetre, disparity, and inverse-depth values,
but it doubles the bytes of a 16-bit atlas. The default 1024x512 atlas is 8 MiB
per frame: about 40 MiB/s at 5 Hz or 80 MiB/s at 10 Hz before protocol overhead.
Treat those figures as a network and PCIe budgeting floor, not measured show
throughput.

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

The initializer and included dual-local presets use one loopback Touch TCP
stream with `transport.peer_host: 127.0.0.1`. This is the turnkey two-process
path because Touch In exposes `num_received_frames` through its Info CHOP. The
world process can therefore distinguish a newly received atlas from another
local receiver cook and hold/age the last atlas between arrivals.

`num_received_frames` is deliberately a preview contract: it establishes
transport arrival, not producer generation. It does not carry the producer
session ID, generation timestamp, calibration identity, or camera metadata.
Use explicit transported frame state or WorldBus for exact lifecycle semantics.

Shared Mem remains supported as an advanced strict integration using one global
`<transport.segment_name>_atlas` block. Shared Mem In exposes no equivalent
producer/receive counter, so its metadata-less path fails closed. A
`dual_local` Shared Mem config must set `source.frame_state_operator` to a
producer-backed metadata sidecar that crosses the process boundary and resolves
in both roles. Merely naming a DAT/CHOP that advances when the receiver cooks is
invalid. The frame-start callback force-cooks Shared Mem Out at the configured
cadence, but that local action is not evidence of arrival in the world process.

Shared Mem TOPs require a TouchDesigner Educational, Commercial, or Pro
license. See the official [Shared Mem Out TOP](https://docs.derivative.ca/Shared_Mem_Out_TOP)
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

`ROLE_BRIDGE` intentionally transports only the atomic image atlas. A local
adapter can publish strict frame/session/timestamp and calibration identity to
the lifecycle helper, but those producer strings/clocks and camera matrices are
not encoded in Shared Mem/Touch pixels. A Touch TCP receiver uses
`num_received_frames` plus its local timeout for transport-arrival preview
freshness; it cannot attribute that arrival to an exact producer generation. A
Shared Mem receiver has no safe metadata-less fallback and fails closed unless
the required producer-backed sidecar supplies strict state. Neither path alone
implements WorldBus v1 producer-exact metadata, network heartbeats,
interaction/control messages, replay, sender authentication, or newest-frame
rejection.

The implemented Python reference in
[`WORLDBUS_REFERENCE.md`](WORLDBUS_REFERENCE.md) remains the production
contract for an adapter that needs those fields. Do not describe the built-in
Touch/Shared-Mem bridge as WorldBus v1.

## World-side calibration and completion

Reconstruction, temporal persistence, interaction, fog, procedural backfill,
and point rendering cook only in the `world` process. A local validated
`flexgpu-calibration/v1` profile can supply depth convention/range, intrinsics,
and camera-to-world/sensor-to-world transforms. Position, color, confidence,
and normalized age then persist between received AI updates. Each accepted
frame produces a one-cook pulse; a held frame ages and decays without being
reabsorbed. A material resolution/session/calibration/adapter contract change
resets that history.

The direct atlas carries RGB, raw depth, mask, and confidence. Producer frame
state, camera metadata, and calibration are **not** transmitted in its pixels. A
split show must provide matching local calibration on the world side and, for
strict Shared Mem, separately transport producer frame state, or implement the
full WorldBus adapter; never assume those semantic fields crossed with the
atlas.
This is especially important for two-machine profiles, where
private config-relative files must exist on the node that consumes them. A
world receiver applies an explicit shared source calibration without importing
the AI `.tox`; if that calibration is invalid, world and output stages are
disabled instead of rendering a knowingly wrong reconstruction.

Calibration identity includes a canonical `calibration_digest`, not only a
human-readable ID. Camera/sensor transforms must have rigid orthonormal,
right-handed bases; scale belongs in depth conversion. Sensor forces use a
bounded 8x8 world-space occupancy sample and a clamped render delta. The
world-side renderer keeps XYZ in metres and uses parallel development cameras
at plus/minus half-IPD; it neither normalizes/moves the world nor supplies a
headset pose, runtime projection, or compositor.

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
- `ROLE_BRIDGE/RX_TCP_ATLAS_INFO` for connection, receive FPS, queue status, and
  `num_received_frames` when using TCP. Treat that counter as transport-arrival
  preview state, not producer-generation identity.

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

Status distinguishes stopped, transitional, degraded, and ownership-error
session state. An identity-matched child is further classified as `alive`
before a current ready heartbeat, `ready`, or `stale` when its application
heartbeat is missing/malformed/expired. The atomic heartbeat binds session,
role and PID and reports build/config identity, cook progress, source/sensor
age, transport state, and active outputs.

Recovery is also a preview unless explicitly authorized:

```powershell
.\scripts\Recover-FlexShow.ps1 `
  -Config config\presets\local-show.json
.\scripts\Recover-FlexShow.ps1 `
  -Config config\presets\local-show.json `
  -Attempts 2 `
  -WaitReadyMs 15000 `
  -Recover
```

It supports one through three bounded attempts and only a separately planned
AI role. It requires the world dependency to be healthy, reruns preflight, and
never implicitly restarts world/render. A healthy AI is reused unless the
operator explicitly adds `-RestartRunning -Recover`. This is not a background
watchdog. `supervisor.readiness_timeout_ms`, `require_ready`, or the per-command
`-WaitReadyMs` adds a bounded readiness acceptance step; a failed new launch is
terminated. Application readiness is a launcher/runtime-file contract and is
separate from both the direct image atlas and WorldBus network heartbeat.
Use it only after the local process profile points to a v1.2.1 `.toe`. The
tracked synthetic canonical project publishes the heartbeat; older or privately
modified projects must be rebuilt before readiness is required.

Launcher-owned `CUDA_*`/`FLEXGPU_*` values cannot be overridden by process
configuration. Secret-like environment/command values are redacted from public
plans, manifests, diagnostics, and errors. Credentials and private or paid
components must still stay out of Git and out of the canonical `.toe`.

## Live-validation status

Source tests cover atlas configuration, role policy/stage gates, per-role tier
injection, configuration contracts, and launcher ownership/recovery behavior.
The tracked v1.2.1 synthetic project was rebuilt and its combined installation
and stereo branches passed the strict local validator in TouchDesigner
2025.32820 on an RTX 3080 Ti Laptop 16 GB GPU. It has not been run here with the
private StreamDiffusionTD `.tox`, a simultaneous physical dual-GPU workload, a
two-machine Touch TCP soak, a depth sensor, projection/LED outputs, or a
headset. No throughput, failover-time, calibration, stereo-comfort, or
venue-readiness claim should be inferred from that short synthetic check.

After rebuilding the ignored `projects/FlexShow-local.toe`, run
`touchdesigner/validate_project.py` inside TouchDesigner to force-cook managed
shaders and active outputs, enforce exact active-mode dimensions and node types,
inspect signal/readback health, and optionally write a local report and
synthetic captures under ignored `runtime/` and `captures/`. Those artifacts are
blocked from public sync. This check still does not replace visual, physical,
thermal, network-soak, headset, or venue acceptance.
