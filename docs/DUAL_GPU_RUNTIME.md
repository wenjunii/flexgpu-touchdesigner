# Dual-GPU and two-machine runtime

Runtime builder `1.1.0` creates a direct image bridge at
`/project1/flexgpu/WORKING_PIPELINE/ROLE_BRIDGE`. The launcher starts the same
project once per assigned role; startup policy activates only the stages owned
by that role.

| Process role | Cooks | Does not cook |
| --- | --- | --- |
| `ai` | demo or `STREAMDIFFUSION_ADAPTER`, atomic atlas pack and sender | reconstruction, sensor, persistence, completion, point rendering, installation, stereo |
| `world` in a split topology | atlas receiver/unpack, reconstruction, sensor, persistence, completion, selected outputs | demo and `STREAMDIFFUSION_ADAPTER`, sender, unselected output |
| `world` in `single` | local source and complete selected show pipeline | transport endpoints and unselected output |

This preserves the StreamDiffusion replacement boundary. Put the real `.tox`
under `WORKING_PIPELINE/SOURCES/STREAMDIFFUSION_ADAPTER` and keep its normalized
RGB and depth connections on `OUT_RGB` and `OUT_DEPTH`; the role bridge is
downstream and needs no model-specific change.

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

## Runtime inspection

Inspect these operators while both processes are running:

- `ROLE_BRIDGE/TRANSPORT_CONTRACT` for atlas layout and transport scope;
- `CONFIG/runtime_state` for `bridge_mode`, sender/receiver flags, cadence and
  every stage gate;
- `ROLE_BRIDGE/RX_TCP_ATLAS_INFO` for connection, receive FPS and queue status
  when using TCP.

The split `world` role never falls back to its local generator. That prevents a
transport interruption from silently duplicating AI work on the render GPU.

## Live-validation status

Source tests cover atlas packing, endpoint configuration, role policy, stage
gates, and per-role tier injection. Build `1.1.0` was rebuilt and health-checked
in TouchDesigner 2025.32820 on the RTX 3080 Ti Laptop 16 GB machine. Sequential
AI-role and world-role checks verified the Shared Mem sender/receiver gates,
atomic atlas dimensions, and clean operator state. A simultaneous two-process
Shared Mem soak and a two-machine Touch TCP soak remain deployment tests; no
dual-GPU throughput claim is made from the sequential check.
