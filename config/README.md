# FlexShow configuration

`flexgpu.py` reads one JSON configuration—or TOML when the selected runtime
provides `tomllib` (Python 3.11+)—and turns it into a deterministic GPU/process
plan. Config selection precedence is explicit `-Config`, `FLEXSHOW_CONFIG`,
`FLEXGPU_CONFIG`, then `config/flexshow.json`.

The configuration has five independent choices:

- `topology`: `single`, `dual_local`, or `dual_network`
- `experience`: `installation`, `vr`, or `combined`
- `completion`: `fog`, `procedural`, or `hybrid`
- `tier`: `auto`, `3080ti_16gb`, `4090`, `5090`, or `custom`
- `gpu.ai` / `gpu.render`: `auto`, a CUDA index, or an object containing `index`, `uuid`, or `bus_id`

For a network pair, both computers use their own configuration. The AI
computer sets `node_role` to `ai`; the show computer sets it to `render`. Keep
their atlas dimensions/port identical and set each `transport.peer_host` to the
other computer's static address. Control/heartbeat ports matter only after a
full WorldBus adapter is added.

The network presets use the RFC 5737 documentation range `192.0.2.0/24` on
purpose. Replace those non-routable example addresses with static addresses
from your actual show network before launch.

## Safe first run

Start and Stop are previews by default. `-Start` authorizes launch and `-Stop`
authorizes forceful shutdown. Diagnose is always read-only; its legacy
`-Start`/`-Run` switch is accepted but ignored with a warning:

```powershell
.\scripts\Initialize-FlexShow.ps1 -ListOnly
.\scripts\Initialize-FlexShow.ps1 -Topology auto -Experience installation -Completion hybrid
.\scripts\Start-FlexShow.ps1
.\scripts\Diagnose-FlexShow.ps1
.\scripts\Start-FlexShow.ps1 -Config config\presets\single-4090.json -Experience vr
```

`Initialize-FlexShow.ps1` discovers `nvidia-smi`, TouchDesigner, stable GPU
UUIDs, and the local hardware tier. `auto` writes a single-GPU plan for one
card or a dual-local plan for two or more. Its default output is the gitignored
`config/local-flexshow.json`; use `-AIIndex`, `-RenderIndex`, `-Output`, or
`-TouchDesignerExe` when automatic selection is not the desired show layout.
It never starts TouchDesigner and does not generate network-node profiles.
Dual-local output deliberately keeps `tier: auto`: the planner resolves the AI
and world process tiers independently, so a 5090 AI card cannot give its point
budget or geometry limits to a weaker 3080 Ti/4090 world card.

For CI or another dedicated PowerShell process, `-Json -ExitWithCode` produces
clean JSON and preserves controller exit `2` (configuration) or `3`
(diagnostic/runtime). Do not use `-ExitWithCode` in an interactive host you
want to keep open, because an error intentionally exits that host.

After the plan is correct and the `.toe` project paths exist:

```powershell
.\scripts\Start-FlexShow.ps1 -Config config\presets\dual-network-ai-worker-3080ti-16gb.json -Start
.\scripts\Stop-FlexShow.ps1  -Config config\presets\dual-network-ai-worker-3080ti-16gb.json -Stop
```

Repeating Stop is safe. Repeating Start reuses a process only when its command
and injected environment still match; changed experience, completion, tier, or
GPU settings require Stop followed by Start. Process ownership is tracked by a
creation token, executable, command-line hash, environment hash, and retained
Windows process handle rather than by executable name. Windows Stop is forceful,
so save edits in launched TouchDesigner processes first.

## Project paths

The templates target the normal TouchDesigner installation at:

`C:\Program Files\Derivative\TouchDesigner\bin\TouchDesigner.exe`

They expect `projects/FlexShow.toe` at the repository root. The planner injects `FLEXGPU_CONFIG`, `FLEXGPU_ROLE`, and GPU identity through environment variables, allowing one project scaffold to serve every role. Change `executable`, `project`, or `cwd` if your installation differs. An execute request intentionally fails before launching anything when a required executable or project is missing.

## Presets

Hardware/topology presets live in `presets/`. Run them in place. To customize a
preset while preserving its relative paths, copy it within that directory—for
example, `Copy-Item .\config\presets\single-3080ti-16gb.json .\config\presets\local-show.json`.
Local preset names beginning with `local-` are gitignored. Explicit and
environment-selected relative config paths resolve from the caller's current
PowerShell directory; the default config resolves from the repository root.

The three single-GPU examples also demonstrate installation/fog,
VR/procedural, and combined/hybrid. CLI flags can independently override
`experience`, `completion`, and `tier`, so the examples do not need to grow into
a full combination matrix.

`custom` is the explicit conservative fallback for an NVIDIA GPU without a
tuned preset. `auto` selects it automatically when discovery cannot classify
the assigned GPU as one of the three named hardware tiers.

| Preset | Topology | Experience | Completion |
| --- | --- | --- | --- |
| `single-3080ti-16gb.json` | single | installation | fog |
| `single-4090.json` | single | VR | procedural |
| `single-5090.json` | single | combined | hybrid |
| `dual-local-heterogeneous.json` | dual local | combined | hybrid |
| `dual-local-same-4090.json` | dual local | combined | hybrid |
| `dual-network-ai-worker-3080ti-16gb.json` | network AI node | combined | hybrid |
| `dual-network-show-node-4090.json` | network show node | combined | hybrid |
| `dual-network-show-node-5090.json` | network show node | combined | hybrid |

Examples of stable GPU selectors:

```json
{
  "gpu": {
    "ai": { "uuid": "GPU-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx" },
    "render": { "bus_id": "00000000:01:00.0" }
  }
}
```

For stable dual-GPU deployments, replace numeric indexes with GPU UUIDs or PCI bus IDs after running the diagnostic script. Indexes can change after driver updates or docking changes.

## Validate canonical JSON profiles

CI checks the schema itself and every shipped JSON profile with a full JSON
Schema Draft 2020-12 implementation. Run the same check locally with the
optional pinned validation dependency:

```powershell
python -m pip install jsonschema==4.17.3
python tools/validate_configs.py
python tools/validate_configs.py config/local-flexshow.json
```

The tool also applies the launcher's dependency-free semantic validator, so
sibling relationships that JSON Schema cannot express are checked in the same
run. It rejects duplicate object keys, non-standard `NaN`/infinity values, and
a relative `$schema` declaration that resolves to the wrong file. The launcher
itself remains dependency-free; `jsonschema` is needed only for the schema half
of this authoring/CI check.

## Optional runtime contracts

The schema accepts five optional objects for show-specific adapter settings.
They keep machine-local choices next to the process plan without invalidating
the supplied minimal presets:

| Object | Intended consumer | Important fields | v1.2 builder binding |
| --- | --- | --- | --- |
| `adaptive` | Live TD frame-time controller and offline policy tools | `enabled`, `levels`, frame/queue budgets, hysteresis windows, thresholds | Live frame-interval governor; offline governor also consumes VRAM and queue age |
| `telemetry` | Live TD and offline metrics capture | `enabled`, JSONL/summary paths, sampling and flush intervals | Live JSONL/summary capture |
| `source` | RGB/depth source adapter | `mode`, `.tox`/replay/calibration paths, RGB/depth/mask/confidence/frame-state/metadata operators, `auto_load_tox`, stale timeout | Manual wiring is the default; explicit auto-load can import one private `.tox` and falls back to demo on a failed contract |
| `sensor` | Audience sensor adapter | `mode`, adapter/replay/calibration paths, position/mask/confidence/frame-state operators, `auto_load_tox`, radius/gain/stale timeout | Simulated is the safe default; explicit auto-load can import a private sensor `.tox` and falls back to simulated input on failure |
| `render` | Point/output extension | point size/budget, output dimensions/rates, fog density, procedural mix | Point size/budget, output dimensions, fog, and procedural mix are live; FPS values are target metadata and do not change `project.cookRate` |

For example, add these members to a complete preset:

```json
{
  "adaptive": {
    "enabled": true,
    "levels": 5,
    "frame_budget_ms": 16.667,
    "queue_budget_ms": 200,
    "down_window": 3,
    "up_window": 120,
    "cooldown_samples": 30
  },
  "telemetry": {
    "enabled": true,
    "jsonl_path": "../runtime/show-telemetry.jsonl",
    "summary_path": "../runtime/show-summary.json",
    "sample_interval_frames": 1,
    "flush_every": 60
  },
  "source": {
    "mode": "streamdiffusion",
    "auto_load_tox": true,
    "streamdiffusion_tox": "../local-components/StreamDiffusionTD.tox",
    "rgb_operator": "out_rgb",
    "depth_operator": "out_depth",
    "mask_operator": "out_mask",
    "confidence_operator": "out_confidence",
    "frame_state_operator": "frame_state",
    "camera_metadata_operator": "camera_metadata",
    "calibration_path": "../calibration/source.json"
  },
  "sensor": {
    "mode": "depth_sensor",
    "auto_load_tox": true,
    "adapter_tox": "../local-components/depth-sensor-adapter.tox",
    "position_operator": "out_position",
    "mask_operator": "out_mask",
    "confidence_operator": "out_confidence",
    "frame_state_operator": "frame_state",
    "calibration_path": "../calibration/sensor.json",
    "interaction_radius_m": 0.45,
    "force_gain": 1.0,
    "stale_timeout_ms": 1000
  },
  "render": {
    "point_size_px": 3.0,
    "point_budget": 120000,
    "installation_fps": 60,
    "vr_fps": 72,
    "fog_density": 0.35,
    "procedural_mix": 0.7
  }
}
```

Keep the referenced private `.tox`, SDKs, real calibration, credentials, and
local replay/capture files under the repository's ignored `local-components/`,
`private/`, `calibration/`, `captures/`, `commissioning/`, or `recordings/`
boundaries. They are machine-local inputs and must not be forced into the public
Git index.

These settings do not install external dependencies. In a locally rebuilt v1.2
project, the runtime helper binds resolved tier/adaptive values and the live
render subset described above to source placeholders, reconstruction, point
render, and output resolution. Its frame-start governor can reduce or restore
geometry and point workload with hysteresis, and the telemetry callback can
append frame/operator metrics to JSONL and write a summary on exit. When
`source` or `sensor` is omitted, saved manual adapter selections are preserved;
an explicit unsupported adapter mode falls back safely.
`tools/benchmark_flexshow.py` exercises the same policy independently from
command-line samples. StreamDiffusionTD, a camera SDK, model/runtime
dependencies, and a headset runtime remain user-supplied. The tracked canonical
`.toe` was not rebuilt for this v1.2 update.

### Private `.tox` loading is opt-in

`auto_load_tox` defaults to `false`. With it disabled, the normal manual
wiring inside the labelled source/sensor adapters is unchanged. With it set to
`true`, the runtime resolves the `.tox` path relative to the selected config
(absolute paths and user/environment expansion are also accepted), loads it
into an `AUTO_LOADED_TOX` holder, and resolves the configured TOP names inside
that holder. Source auto-load requires `streamdiffusion_tox` and
`rgb_operator`; sensor auto-load requires `adapter_tox` and
`position_operator`. Optional depth, mask, and confidence TOPs are wired only
when configured and valid. In a split topology, the AI process alone loads the
source component and the world process alone loads the sensor component. The
world receiver does not import the private source `.tox`; it applies the shared
source calibration locally to reconstruct received depth.

A missing file, wrong extension, load error, unresolved required output, or bad
calibration leaves the source on demo or the sensor on simulated input and
records the reason in runtime state. Changing an already loaded `.tox` path
requires a process restart. Auto-load does not install Python/CUDA packages,
download weights, configure prompts/scheduling, license a sensor SDK, or verify
the behavior or redistribution rights of the private component. A loaded
component can be embedded if that TouchDesigner session is saved: enable this
only in an ignored local `.toe`, never in a session that will overwrite
`projects/FlexShow.toe`.

### Calibration and commissioning

`source.calibration_path` and `sensor.calibration_path` accept a strict
`flexgpu-calibration/v1` JSON profile. It carries image dimensions, intrinsics,
depth encoding/scale/bias/range, camera-to-world and sensor-to-world transforms,
and a calibration ID in a right-handed, Y-up, metre coordinate system. The
runtime supports normalized, metres, millimetres, disparity, and inverse-depth
encodings. Source and sensor profiles used together must describe the same
calibration ID. Replay modes require `replay_path` in configuration. An invalid
explicit source calibration on a split world receiver disables its world and
output stages instead of rendering a knowingly incorrect remote reconstruction.

Depth conversion is explicit: `calibrated = raw * scale + bias`. For
`normalized`, calibrated `0..1` maps from `near_m` to `far_m`; for `metres` or
`millimetres`, calibrated is treated as metres (so millimetre input normally
uses `scale: 0.001`); for `disparity` or `inverse_depth`, metric Z is
`1 / calibrated`. Intrinsics use pixel coordinates from the declared image.
Unprojection produces camera-local X right, Y up, and forward along negative Z;
the row-major `camera_to_world` matrix maps that basis into the shared world.
The sensor adapter emits sensor-local XYZ metres, and `sensor_to_world` maps it
into the same world. Both transforms must have a non-singular right-handed
spatial basis and a homogeneous final row `[0, 0, 0, 1]`.

Validate the public synthetic example and exercise synchronized replay
contracts before connecting private capture data:

```powershell
python tools/commission_flexshow.py calibration config/calibration.example.json
python tools/commission_flexshow.py demo `
  --output commissioning/demo `
  --frames 8 `
  --width 64 `
  --height 36
python tools/commission_flexshow.py inspect commissioning/demo/manifest.json
```

Inspection validates hashes by default plus media roles, dimensions/formats,
monotonic frame state, session/calibration relationships, and safe bundle
paths. `--skip-hashes` skips payload integrity but retains those structural,
presence, format, layout, and relationship checks. The demo and
`config/calibration.example.json` are
synthetic contract fixtures, not a measured site calibration. They cannot
validate a physical camera, sensor alignment, audience tracking, projector/LED
mapping, or visual quality. The generated bundle is not automatically imported
or played by the stock TouchDesigner network; source/sensor replay remains an
adapter boundary despite the strict configuration and inspection contracts.

Bundle media formats are role-constrained: RGB accepts `ppm-rgb8` or
`raw-rgba8`; depth accepts `pgm-u8`, `pgm-u16`, or `raw-r32f-le`; mask and
confidence accept the same scalar formats. `raw-r32f-le` means one
little-endian IEEE-754 float per pixel. Paths must be unique, relative POSIX
paths that remain inside the bundle.

Real calibration, RGB/depth/mask/confidence captures, commissioning bundles,
and recordings are ignored private data. Collect audience data only under an
appropriate consent, access, retention, and deletion policy. The public-sync
guard helps prevent accidental publication but is not a legal/privacy review.

### Machine-local profiling and process supervision

Capture a read-only starting recommendation, then copy stable UUID selections
into a gitignored local preset after measuring the actual show workload:

```powershell
python tools/profile_flexshow.py --topology single
python tools/profile_flexshow.py `
  --topology dual_local `
  --output runtime/hardware-profile.json
```

The snapshot includes present VRAM headroom/load, temperature, clocks, optional
power values, display ownership, driver, UUID, and PCI identity. It supports
only `single` and `dual_local`; it is not a benchmark, soak, dynamic scheduler,
or two-machine planner. The output is runtime/telemetry data and stays local.

```powershell
.\scripts\Status-FlexShow.ps1 -Config config\presets\local-show.json
.\scripts\Recover-FlexShow.ps1 -Config config\presets\local-show.json
.\scripts\Recover-FlexShow.ps1 `
  -Config config\presets\local-show.json `
  -Attempts 2 `
  -Recover
```

Status is strictly read-only. Recovery is a dry-run without `-Recover`, permits
one to three bounded attempts, and can recover only a separate AI role after
its world dependency and preflight pass. It refuses single/unified plans and
never implicitly restarts world/render. Add `-RestartRunning -Recover` only for
an intentional replacement of a healthy AI process. These commands inspect
and mutate launcher ownership state; they are not an automatic watchdog and do
not provide a TouchDesigner or transport heartbeat.

The embedded TD governor currently makes decisions from frame interval. It
rebinds source/depth resolution, reconstruction resolution, and point count;
diffusion/geometry rate values are exposed in runtime state but cannot retime a
private `.tox` unless that component explicitly consumes them. The standalone
Python governor additionally evaluates VRAM and queue-age pressure.

The schema bounds every numeric field. It cannot express all relationships:
adaptive low thresholds must still be below their matching high thresholds,
and `initial_level` must be lower than `levels`. The launcher and
`tools/validate_configs.py` check those relationships in addition to the
adaptive Python API.

## Benchmark and replay commands

Create deterministic governor pressure, write every evaluated sample, and
atomically write its summary:

```powershell
python tools/benchmark_flexshow.py synthetic `
  --tier 3080ti_16gb `
  --pattern cycle `
  --samples 600 `
  --output-jsonl runtime/adaptive.jsonl `
  --summary-json runtime/adaptive-summary.json

python tools/benchmark_flexshow.py replay runtime/adaptive.jsonl `
  --tier 3080ti_16gb `
  --summary-json runtime/adaptive-replay-summary.json
```

The synthetic command does not measure GPU speed. It makes quality changes and
hysteresis reproducible outside the live TouchDesigner session.

## Direct role-bridge transport

The shipped split-role runtime binds `transport` directly to
`WORKING_PIPELINE/ROLE_BRIDGE`:

- `dual_local` plus `shared_memory` uses one global
  `<segment_name>_atlas` Shared Mem block;
- `dual_local` plus `touch_tcp` uses a loopback Touch stream;
- `dual_network` uses one uncompressed Touch TCP stream to `peer_host` on
  `atlas_port`;
- `single` bypasses atlas pack/unpack.

The atlas is RGBA16F: generated RGB fills the left half and normalized depth
fills the right half. A 1024x512 atlas at 10 Hz is roughly 336 Mbit/s before
transport overhead. Start with the preset's lower cadence, use wired Ethernet,
and measure the actual link. `atlas_fps` is converted to an integer frame step
from TouchDesigner's cook rate.

This direct bridge does not use `control_port` or `heartbeat_port` and has no
WorldBus frame/session IDs, camera metadata, mask/confidence, controls, replay,
or explicit stale/drop/hold policy. Those ports remain available to a future
full WorldBus adapter. See
[`docs/DUAL_GPU_RUNTIME.md`](../docs/DUAL_GPU_RUNTIME.md).

The dependency-free WorldBus reference is available independently of
TouchDesigner:

```powershell
python tools/worldbus_node.py loopback
python tools/worldbus_node.py replay-generate --output runtime/worldbus-demo.wbr
python tools/worldbus_node.py replay-inspect runtime/worldbus-demo.wbr
```

Its full-contract image framing uses bounded TCP and its low-volume messages
are UDP JSON with OSC-like addresses—not binary OSC. It is separate from the
already installed direct RGBA16F bridge. See
[`docs/WORLDBUS_REFERENCE.md`](../docs/WORLDBUS_REFERENCE.md) before adapting it
to Touch In/Out, shared memory, or an OSC bridge.
