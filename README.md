# FlexGPU TouchDesigner world scaffold

This repository is a deployment and TouchDesigner starter scaffold for a
real-time generated point world.  It supports:

- NVIDIA RTX 3080 Ti Laptop 16 GB, RTX 4090 24 GB, and RTX 5090 32 GB tiers.
- One GPU, two GPUs in one Windows computer, or two networked computers.
- Configuration branches for installation, VR, and combined experiences.
- Selectors for thick-points/fog, procedural backfill, and hybrid completion.
- Calibrated depth, frame-aware confidence/age persistence, and deterministic
  commissioning data for adapter development.
- A role-gated atomic RGB/raw-depth/mask/confidence atlas bridge for dual-GPU
  and two-machine preview pipelines, keeping AI generation off the world/render
  GPU.

It does **not** bundle StreamDiffusionTD, a depth sensor SDK, an OpenXR/OpenVR
headset runtime, or model weights. The built-in demo generators make the point
world visible without those dependencies. Later, replace the labelled source
TOPs with outputs from your own `StreamDiffusionTD.tox`; the downstream TOP
contracts stay the same. Real sensor input, headset submission, SHARP, and
Gaussian inference are likewise user-supplied adapters.

## Prerequisites

- Windows 10/11 with PowerShell 5.1 or newer.
- An NVIDIA driver that provides `nvidia-smi`.
- TouchDesigner 2025. The v1.2.1 builder sources target the 2025 API; rebuild,
  validate, and inspect a local project before using the new runtime foundation
  in a show.
- Python 3.10 or newer, or TouchDesigner's bundled Python runtime. JSON works
  on 3.10; TOML configuration requires a runtime with `tomllib` (Python 3.11+).

## What is included

```text
.github/workflows/  Windows CI for tests, benchmark smoke, and script parsing
config/             validated show profiles and quality presets
docs/               architecture and WorldBus protocol
scripts/            Windows lifecycle/status/recovery plus guarded public sync
src/flexgpu/         planner, adaptive governor, telemetry, and WorldBus reference
tests/               runtime, transport, configuration, and publication tests
tools/               launcher, commissioning/profile, benchmark, WorldBus, sync checks
projects/            tracked FlexShow.toe starter plus ignored local-build path
touchdesigner/       TD 2025 bootstrap source and integration guide
```

## Current implementation status

| Area | Status |
| --- | --- |
| GPU discovery, tier selection, process planning and affinity | Implemented and tested |
| Safe preview/start/identity-verified stop | Implemented and tested on Windows |
| `FlexShow.toe` component layout and runtime parameters | Tracked v1.2.1 synthetic starter rebuilt in TouchDesigner 2025.32820 |
| Calibrated depth, frame-aware temporal lifecycle, metric interaction, fog and procedural backfill | Implemented and synthetic combined-mode validated in TouchDesigner; physical calibration is still required |
| Dual-role direct image bridge | Atomic RGBA32F RGB/raw-depth/mask/confidence atlas; loopback Touch TCP is turnkey for two local GPUs, while Shared Mem is an explicit-metadata advanced path |
| WorldBus validation, newest-frame queue, heartbeat and replay | Dependency-free full-contract Python reference; distinct from the built-in image-only bridge |
| Adaptive quality and telemetry | Offline governor/benchmark plus live TD frame-start bindings, JSONL capture, and final summary |
| Commissioning and calibration | Deterministic synchronized demo/inspection tools and strict calibration contract; no physical sensor or venue calibration claim |
| Local GPU placement | Read-only `nvidia-smi` snapshot and starting recommendation; not a benchmark or scheduler |
| Process status, readiness and AI recovery | Atomic TouchDesigner application heartbeat, read-only alive/ready/stale status, bounded readiness waits, and operator-authorized recovery of a separate AI role; not an autonomous watchdog |
| StreamDiffusionTD and sensor SDK | Labelled manual boundaries plus opt-in private `.tox` loading with safe demo/simulated fallback |
| Installation and VR | Metric installation render plus parallel-camera stereo development textures; projection mapping, headset runtime, pose/input, compositor submission, and physical validation are user-supplied |
| SHARP and Gaussian reconstruction | Disabled external-worker adapter contracts; inference is not bundled |

Experience and completion flags select the corresponding branches. The
working pipeline is a practical prototype and integration baseline, not a
calibrated projection system or headset application.

The builder sources and tracked synthetic `projects/FlexShow.toe` are version
`1.2.1`. The canonical project was rebuilt and its combined installation/stereo
branches passed the strict local validator in TouchDesigner 2025.32820 on an
RTX 3080 Ti Laptop 16 GB GPU. This was a short synthetic operator check, not a
performance or venue test. The project has not been tested here with the
private StreamDiffusionTD component, a physical depth sensor, projection/LED
mapping, an OpenXR/OpenVR compositor, or a headset. Treat all GPU budgets as
commissioning starting points until the complete local system passes thermal,
latency, visual, and failover soaks.

## Quick start

Open PowerShell in the repository root. The easiest machine-local setup is to
let the initializer discover NVIDIA GPUs and TouchDesigner, then write a
gitignored UUID-based preset:

```powershell
.\scripts\Initialize-FlexShow.ps1 -ListOnly
.\scripts\Initialize-FlexShow.ps1 `
  -Topology auto `
  -Experience installation `
  -Completion hybrid
```

`auto` chooses `single` for one NVIDIA GPU and `dual_local` for two or more.
Use `-AIIndex` and `-RenderIndex` to override its assignment, `-Output` for a
different `config/local-*.json` name, and `-Force` to replace that local file.
The generated dual-local profile uses loopback Touch TCP on `127.0.0.1`, as do
the shipped dual-local presets. The initializer does not create two-computer
network profiles.

Alternatively, presets are ready to run in place. To make an untracked local
copy, keep it in the same directory so its relative project paths remain
valid:

```powershell
Copy-Item .\config\presets\single-3080ti-16gb.json .\config\presets\local-show.json
```

First inspect the machine without launching anything:

```powershell
.\scripts\Diagnose-FlexShow.ps1 -Config .\config\presets\local-show.json
```

Diagnostics are always read-only. Preview the complete Start preflight,
including paths, GPU affinity and runtime settings:

```powershell
.\scripts\Start-FlexShow.ps1 -Config .\config\presets\local-show.json
```

The Start script remains non-mutating unless `-Start` is present:

```powershell
.\scripts\Start-FlexShow.ps1 -Config .\config\presets\local-show.json -Start
```

Stop only identity-verified processes recorded by that configuration's runtime
manifest:

```powershell
.\scripts\Stop-FlexShow.ps1 -Config .\config\presets\local-show.json -Stop
```

The tracked `projects/FlexShow.toe` contains the public v1.2.1 synthetic
RGB/depth and audience-interaction starter. Build an ignored local copy before
adding any private component or site path. When your `StreamDiffusionTD.tox` is ready, replace
the inputs to `OUT_RGB` and, when available, `OUT_DEPTH` inside
`/project1/flexgpu/WORKING_PIPELINE/SOURCES/STREAMDIFFUSION_ADAPTER`. Then turn
on **Use StreamDiffusion Adapter** in the parent `SOURCES` COMP; turn on **Use
Adapter Depth** only after a valid normalized depth TOP is connected. Do not
rebuild the point pipeline around the component. Connect real sensors and
headset output at their similarly labelled boundaries. A missing required
process path is reported by validation instead of silently launching the wrong
file. See [touchdesigner/README.md](touchdesigner/README.md) for the exact
replacement and feedback-reset sequence.

Add `-Json` to any operator script for compact machine-readable output. Start
and Diagnose also accept `-NvidiaSmi C:\path\to\nvidia-smi.exe`. Config selection
precedence is explicit `-Config`, `FLEXSHOW_CONFIG`, `FLEXGPU_CONFIG`, then
`config/flexshow.json`. Explicit or environment-provided relative paths resolve
from the caller's current PowerShell directory; the default resolves from the
repository root. The CLI accepts JSON, plus TOML when the selected Python
runtime provides `tomllib` (Python 3.11+).

For a dedicated automation process, combine `-Json -ExitWithCode` so
`powershell.exe -File` exits with the controller's `2` (configuration) or `3`
(diagnostic/runtime) status. `-ExitWithCode` deliberately exits that PowerShell
host on error, so omit it during an interactive session you want to keep open.

## Commission, profile, and supervise

Create a small deterministic synchronized RGB/depth/mask/confidence bundle,
verify its hashes and frame/calibration relationships, and validate the public
synthetic calibration example:

```powershell
python tools/commission_flexshow.py demo `
  --output commissioning/demo `
  --frames 8 `
  --width 64 `
  --height 36
python tools/commission_flexshow.py inspect commissioning/demo/manifest.json
python tools/commission_flexshow.py calibration config/calibration.example.json
```

`demo` and `inspect` exercise the `flexgpu-commissioning/v1`,
`flexgpu-frame-state/v1`, and `flexgpu-calibration/v1` contracts. Hash checking
is on by default. Demo generation is transactional: it builds and fully
validates a private staging directory before atomically publishing the complete
bundle. Inspection also decodes depth/mask/confidence, recomputes validity and
confidence metrics, verifies media byte layout and sample ranges, and binds
every frame to the canonical calibration-content digest. `inspect
--skip-hashes` skips file integrity only; those deep semantic checks remain.
The generated data and public example are synthetic. They
do not prove camera intrinsics, depth scale, sensor-to-world alignment,
projector mapping, or venue accuracy. The stock project does not automatically
play the generated bundle; a production replay/capture-import adapter is still
site-specific.

Use a read-only local snapshot to choose an initial single- or dual-GPU role
placement:

```powershell
python tools/profile_flexshow.py --topology single
python tools/profile_flexshow.py `
  --topology dual_local `
  --output runtime/hardware-profile.json
```

The profiler records current VRAM headroom, utilization, thermals, clocks,
power information when available, PCI/UUID identity, and display ownership. It
is not a throughput benchmark, soak test, dynamic scheduler, or network-pair
planner. Confirm its recommendation with the actual `.tox`, sensor, render
outputs, and thermal conditions; keep the resulting JSON machine-local.

Status is read-only. Recovery previews by default and, when authorized, is
limited to a separately planned `ai` role:

```powershell
.\scripts\Status-FlexShow.ps1 -Config .\config\presets\local-show.json
.\scripts\Status-FlexShow.ps1 -Config .\config\presets\local-show.json -Json

.\scripts\Start-FlexShow.ps1 `
  -Config .\config\presets\local-show.json `
  -WaitReadyMs 15000 `
  -Start

.\scripts\Recover-FlexShow.ps1 -Config .\config\presets\local-show.json
.\scripts\Recover-FlexShow.ps1 `
  -Config .\config\presets\local-show.json `
  -Attempts 2 `
  -WaitReadyMs 15000 `
  -Recover
```

Recovery refuses a unified/single plan and an unhealthy world dependency. It
never implicitly restarts world/render; `-RestartRunning -Recover` is the
explicit operator choice to replace an otherwise healthy AI process. The
launcher injects a private runtime heartbeat path/session into TouchDesigner;
the app atomically publishes build/config identity, cook progress, source and
sensor age, transport state, and output activation. Status distinguishes a live
PID with no ready heartbeat from `ready` and stale/frozen. `-WaitReadyMs` is a
bounded launch/recovery acceptance wait, not a background watchdog, and this
application heartbeat is separate from the WorldBus network heartbeat.
Readiness waiting requires a v1.2.1 `.toe` and a local profile whose process
`project` points to it. The tracked synthetic canonical project contains the
heartbeat writer; an older or privately modified project must be rebuilt before
`require_ready` is enabled.

Private source and sensor components remain manual by default. To let the v1.2.1
runtime load your future component from a local config, set
`source.auto_load_tox` to `true`, provide `streamdiffusion_tox`, and name at
least `rgb_operator`. Paths resolve relative to the selected configuration.
Invalid paths, load errors, or missing required outputs keep the demo active
and record fallback state. In a split plan, only the AI process loads the
source `.tox`; only the world process loads a sensor `.tox`. This loader does
not install CUDA/Python packages, models, SDKs, or licenses. Because a loaded
component can be embedded when the TouchDesigner session is saved, use this
only in an ignored local `.toe` and never overwrite the canonical project. See
[config/README.md](config/README.md) for the complete source/sensor example and
calibration fields.

## Choose a deployment

| Available hardware | Start from | Recommended assignment |
| --- | --- | --- |
| 3080 Ti Laptop 16 GB | `single-3080ti-16gb.json` | Combined-lite stock preview, or installation/desktop-stereo alone |
| 4090 24 GB | `single-4090.json` | All stock experience branches with more headroom |
| 5090 32 GB | `single-5090.json` | All stock branches with more quality reserve |
| Two different local NVIDIA GPUs | `dual-local-heterogeneous.json` | AI on one GPU, show/desktop-stereo on the other |
| Two local 4090 GPUs | `dual-local-same-4090.json` | AI on one GPU, show/desktop-stereo on the other |
| Two Windows computers | worker/show network profiles | AI worker and show node separated by wired Ethernet |

For a mixed pair, put the higher-VRAM card on AI and connect projection/LED
outputs—and a future headset adapter—to the render card. This is only a
starting policy;
`gpu.ai` and `gpu.render` can be swapped in configuration after measuring the
actual show. For a future latency-critical combined headset/LED show, benchmark
the reverse assignment as well: keeping the faster card on rendering can matter
more than AI update rate. The network examples intentionally demonstrate a 3080
Ti AI worker feeding a faster 4090/5090 show node.

With `tier: auto`, a heterogeneous local pair is resolved per process. The AI
process receives the tier for `gpu.ai`, while the world/render process receives
the tier and point/geometry limits for `gpu.render`; a 5090's budgets are never
silently applied to a 3080 Ti world GPU.

The three single-GPU presets intentionally demonstrate different starting
modes: 3080 Ti uses installation/fog, 4090 uses the desktop-stereo `vr` branch
with procedural completion, and 5090 uses combined/hybrid. The `vr` branch is a
headset-integration scaffold, not an OpenXR/OpenVR runtime. Overrides let you
keep both completion options while testing:

```powershell
.\scripts\Start-FlexShow.ps1 -Config .\config\presets\single-3080ti-16gb.json -Experience combined -Completion hybrid
```

Preview a two-GPU computer with one command:

```powershell
.\scripts\Start-FlexShow.ps1 -Config .\config\presets\dual-local-heterogeneous.json
```

For two networked computers, run the matching profile on each machine. Replace
the RFC 5737 example addresses in local preset copies with the machines' static
show-network addresses first:

```powershell
# AI computer
.\scripts\Start-FlexShow.ps1 -Config .\config\presets\dual-network-ai-worker-3080ti-16gb.json

# Show/desktop-stereo computer; choose the installed render GPU profile
.\scripts\Start-FlexShow.ps1 -Config .\config\presets\dual-network-show-node-4090.json
```

These commands are previews. Add `-Start` only after both plans and diagnostics
are correct. See [config/README.md](config/README.md) for selectors, local copies,
network fields, and all supplied profiles. The built-in direct atlas bridge,
its role gates, and its boundary from full WorldBus v1 are documented in
[docs/DUAL_GPU_RUNTIME.md](docs/DUAL_GPU_RUNTIME.md).

## Quality behavior

The chosen tier changes resolution, update-rate, and point-count budgets. It
does not change the network structure.

- `3080ti_16gb`: SD-Turbo-oriented 512-square diffusion, 384-square geometry,
  and lean point budgets; the supplied single-GPU preset starts in
  installation/fog mode.
- `4090`: higher update/geometry budget with room for measured conditioning.
- `5090`: larger reserve for model experiments; its default 512-square geometry
  limits the reachable stock point budget to 262,144 samples.

The v1.2.1 reconstruction path can apply normalized, metric, millimetre,
disparity, or inverse-depth calibration, including intrinsics and rigid
camera-to-world/sensor-to-world transforms. Each calibration has a canonical
`calibration_digest`; frame state must match both that digest and its
human-readable ID. Mask and confidence participate in validity. Explicit frame
metadata produces a one-cook `new_frame` pulse, rejects retired/out-of-order
state, and lets a held texture age and decay without being reabsorbed every
render cook. Without metadata the helper uses an operator cook token when one
is available; the last-resort `legacy_each_cook` mode preserves old adapters
but cannot prove producer freshness. History resets on a material contract
change such as resolution, source session/calibration identity, depth values,
or adapter identity—not merely because the same config is reapplied.

In a split process, the fallback is transport-specific. Touch TCP exposes
`num_received_frames`, so the world process can pulse once per received atlas
for preview pacing. That counter means only that Touch In received a transport
frame; it does not identify the producer generation, session, or timestamp.
Shared Mem exposes no corresponding producer/receive counter, so its
metadata-less receiver fails closed. An advanced Shared Mem config must provide
`source.frame_state_operator` backed by a metadata sidecar that actually crosses
the process boundary and resolves in both roles. Naming a local receiver-cook
operator is not valid. Use explicit transported metadata or WorldBus for exact
lifecycle semantics.

Audience force is evaluated in shared-world metres using a bounded 8x8 sensor
occupancy sample, rather than matching generated and sensor pixels by UV.
Motion integrates a clamped render delta, so force is frame-rate aware and a
debugger pause cannot launch the cloud. This is a practical low-resolution
occupancy/SDF approximation, not body tracking or a full volumetric solver.

Completion remains deliberately artistic. Thick point size helps cover sparse
samples. The fog branch uses nearby persistent geometry to identify
disocclusions and adds view-specific noise/fog around those gaps. Procedural
backfill writes only into holes left by the original position alpha; hybrid
mixes the two strategies there. Fog conceals temporal seams and procedural
backfill invents plausible volume—neither recovers ground-truth hidden
geometry. Point geometry remains in metres through rendering; parallel
development cameras shift by plus/minus half the preview IPD without toe-in or
moving the world. Installation and left/right views can tune their fog
independently, but the stereo textures still have no headset pose, runtime
projection, late-latching, hidden-area mesh, or compositor submission.

The target architecture decouples AI updates from world/render cadence. On the
3080 Ti, for example, the configured 5-10 Hz diffusion-update range is a scheduling
budget for a future AI adapter; the stock demo does not prove that cadence. The
point feedback and desktop-stereo render continue on TouchDesigner's frame
clock, while headset timing requires a user-supplied runtime.

When `adaptive.enabled` is true, the embedded TouchDesigner frame-start helper
measures frame interval, applies hysteresis/cooldown, and rebinds source/depth
resolution, reconstruction resolution, and point budget at a safe frame
boundary. Render/desktop-stereo cadence remains the priority. Live telemetry can
append frame/operator timing to JSONL and write its summary on exit. The
dependency-free offline governor additionally accepts VRAM and queue-age
pressure. Exercise that policy without TouchDesigner or a GPU:

The live helper records diffusion/geometry rate budgets in runtime state, but
it cannot retime an arbitrary private `.tox`; bind those values inside that
adapter if it needs explicit generation scheduling.

```powershell
python tools/benchmark_flexshow.py synthetic `
  --tier 3080ti_16gb `
  --samples 600 `
  --pattern cycle `
  --output-jsonl runtime/adaptive-cycle.jsonl `
  --summary-json runtime/adaptive-cycle-summary.json

python tools/benchmark_flexshow.py replay runtime/adaptive-cycle.jsonl `
  --tier 3080ti_16gb `
  --summary-json runtime/adaptive-replay-summary.json
```

Synthetic results test policy behavior; they are not GPU performance numbers.
Measure the actual `.tox`, sensor, outputs, and headset on the show machine
before choosing final budgets.

## WorldBus development tools

The standard-library WorldBus reference provides strict frame validation, a
newest-only queue, stale-heartbeat state, TCP frame payloads, UDP JSON
metadata/controls, and portable `.wbr` replay. Verify the entire local path:

```powershell
python tools/worldbus_node.py loopback

python tools/worldbus_node.py replay-generate `
  --output runtime/worldbus-demo.wbr `
  --frames 8
python tools/worldbus_node.py replay-inspect runtime/worldbus-demo.wbr
```

To exercise a sender and receiver in separate terminals, run `receive`, then
`replay-send` with the reported TCP port. Its UDP messages are JSON with
OSC-like addresses, not binary OSC. The built-in TouchDesigner `ROLE_BRIDGE`
already handles direct RGBA32F RGB, raw depth, mask, and confidence transport
without clamping metric or disparity values. Its local lifecycle can use
adapter metadata. Touch TCP can use Touch In's `num_received_frames` as
transport-arrival preview freshness, but that is not producer-generation
identity. Metadata-less Shared Mem fails closed because Shared Mem In exposes no
equivalent receive counter. The atlas itself does not carry producer
string/clock identity, so the direct bridge does not implement this reference's
producer-exact metadata, camera matrices, network heartbeat, control, or replay
semantics. See
[docs/WORLDBUS_REFERENCE.md](docs/WORLDBUS_REFERENCE.md).

## TouchDesigner starter

See [touchdesigner/README.md](touchdesigner/README.md) for building the starter
`.toe`. The generated network provides a `WORKING_PIPELINE` with:

- Synthetic RGB/depth and simulated audience input that run immediately.
- A stable StreamDiffusionTD RGB/depth adapter for your later `.tox`.
- GPU depth-to-position, one-cook frame-aware temporal persistence, and bounded
  world-space interaction fields.
- Thick/disocclusion fog, procedural backfill, and hybrid completion.
- A metric point-render contract, installation preview, and parallel-camera
  stereo desktop preview.
- Role-gated loopback/network Touch TCP RGB/raw-depth/mask/confidence transport,
  plus an explicit-metadata advanced Shared Mem path.
- Live adaptive/telemetry bindings and disabled SHARP/Gaussian worker adapters.

The scaffold is deliberately adapter-based. Connect the exact StreamDiffusionTD
component, camera SDK, and VR component available on the show machine rather
than burying those dependencies in the launcher.

After building `projects/FlexShow-local.toe`, select the intended experience
and run the machine-local validator inside TouchDesigner's Python runtime:

```python
from pathlib import Path; import sys; root = Path(r'C:\path\to\flexgpu-touchdesigner'); sys.path.insert(0, str(root / 'touchdesigner')); import validate_project as v; report = v.validate(expected_experience='combined', report_path=str(root / 'runtime' / 'td-validation-v1.2.1.json'), capture_dir=str(root / 'captures' / 'td-validation-v1.2.1')); print(report)
```

It force-cooks managed shaders and active outputs, checks managed operator
types/errors, enforces exact active-mode dimensions, rejects blank/non-finite
visual readbacks, and verifies nontrivial synthetic installation/stereo capture
files. The local `.toe`, report, and captures are ignored and blocked from
public sync. Passing this validator still does not establish artistic quality,
calibrated scale, physical sensor behavior, headset comfort, sustained frame
rate, or venue readiness.

## Testing and security

### Public GitHub sync policy

Sync project-owned source, documentation, tests, public configurations, CI,
the stock `projects/FlexShow.toe`, and original or explicitly redistributable
assets. Do **not** sync any of the following:

- credentials, API keys, access tokens, passwords, private keys, certificates,
  credential-bearing URLs, or non-placeholder `.env` files;
- the private `StreamDiffusionTD.tox`, machine-local components/configuration,
  site calibration, audience captures, depth/mask/confidence recordings,
  commissioning bundles, hardware profiles, telemetry, logs, or runtime state.
  All `.tox` files and every `.toe` except `projects/FlexShow.toe` are
  deliberately local by default; relax the `.tox` rule only after you explicitly
  confirm a component is project-owned or redistributable;
- model weights, paid SDKs/plugins/assets, or anything whose license does not
  explicitly permit redistribution. Free-to-use does not necessarily mean
  GitHub-redistributable; include the required license or notice for any
  redistributable third-party item;
- opaque archives (`.zip`, `.7z`, `.rar`, compressed tar files, and similar
  bundles). The guard does not unpack them, so distribute reviewed public
  contents in inspectable form or use a separately reviewed release process.

Keep excluded material in an ignored `private/`, `paid/`, `licensed/`,
`local-components/`, `models/`, `weights/`, `calibration/`, `captures/`,
`commissioning/`, `recordings/`, or `runtime/` directory. The public
`config/calibration.example.json` is synthetic documentation data, not a venue
profile. Never use `git add -f` to bypass this boundary. `.env.example` may
contain placeholders only.

The guard also recognizes structured local JSON/JSONL by content even after a
rename: hardware profiles, commissioning/frame-state data, real calibration,
runtime manifests, telemetry, TouchDesigner validation reports, support
bundles, and audience/sensor captures are blocked. Only the exact synthetic
`config/calibration.example.json` at that public path is exempt. This is defense
in depth; private `.tox`, credentials, paid/licensed material, local `.toe`
files, and captured audience data must never enter Git in the first place.

For audience-facing capture, define consent, purpose, retention, access, and
deletion rules before recording any RGB, depth, mask, confidence, or inferred
body data. The repository guard reduces accidental publication; it is not a
legal/privacy review and cannot make a capture safe to collect or retain.

Run the read-only guard at any time:

```powershell
.\scripts\Test-PublicSync.ps1 -SelfTest
.\scripts\Sync-PublicRepo.ps1
```

The first command scans every non-ignored sync candidate, the exact Git index,
all local ref and annotated-tag metadata, and every historical blob reachable
from local refs (including a stash). It reports only path, rule, and line,
never the matched value; secret-bearing filenames are replaced with a hash.
Files above the 100 MiB scan ceiling fail closed and require a separate reviewed
distribution plan. The second command is also read-only without action
switches.
For an intentional full update:

```powershell
.\scripts\Sync-PublicRepo.ps1 `
  -Stage `
  -Commit `
  -Message "Describe the public update" `
  -Push
```

The guarded sync scans before staging, scans the resulting index and commit
message, and scans the complete history reachable from the exact `HEAD` it will
publish. A private local stash or unrelated private branch is therefore not
published and does not block that branch's sync. The script requires an
explicit commit message and refuses to push a dirty or uncommitted tree. It
pushes only the current branch with automatic tag following disabled. A change
to `projects/FlexShow.toe` additionally requires
`-AllowCanonicalProjectUpdate` after manual inspection: a compressed `.toe` can
embed a private component that a text scanner cannot reliably see. Keep the
working integration in ignored `projects/FlexShow-local.toe`; publish the
canonical project only after removing private `.tox`, credentials, paid assets,
and private paths.

These checks are defense in depth, not a license oracle. If a credential is ever
committed, revoke or rotate it immediately and purge it from Git history;
deleting it only from the latest revision is insufficient.

### Test suite

Run the dependency-free tests with:

```powershell
python -m unittest discover -s tests -v
```

GitHub Actions repeats Python compilation (including the TouchDesigner build
sources), Draft 2020-12 validation of every shipped JSON profile, the unit
suite, a synthetic benchmark, PowerShell syntax parsing, a full-history
publication-safety scan, and a real initializer write/read/validation smoke
test on `windows-latest`.

Configuration files may contain arbitrary process commands. Treat downloaded
or shared configurations as executable input: inspect them before using
`-Start`. Shutdown records include process creation time, executable identity,
and a command-line hash so stale PID reuse fails closed. Do not edit or trust a
runtime manifest supplied by another user.

Process configuration cannot override launcher-owned `CUDA_*` or `FLEXGPU_*`
variables. Secret-like environment values, command arguments, credentialed
URLs, plans, manifests, diagnostics, and CLI errors are redacted for display;
the real launch command/environment still receives values where needed.
Redaction is not permission to store credentials in a public configuration—use
machine-local environment/secret management and keep credentials out of Git.

If `-Experience`, `-Completion`, `-Tier`, GPU selection, or another injected
launch setting differs from an already running process, Start refuses to reuse
the old environment. Stop the owned process, preview again, and then restart.

On Windows, an authorized `-Stop` force-terminates only the identity-verified
show processes recorded by this project. Save any interactive TouchDesigner
edits in those launched processes before stopping them.

## Production order

1. Rebuild an ignored v1.2.1 local `.toe`, run `validate_project.py`, and inspect
   its synthetic captures without publishing the local report or project.
2. Generate/inspect a synthetic commissioning bundle, then measure and validate
   the real camera/sensor calibration locally under an explicit privacy plan.
3. Profile the target GPU layout and make installation-only work with sensor
   geometry and generated color.
4. Tune thick points/fog and procedural backfill separately, then hybrid.
5. Add VR-only and keep the headset renderer independent from AI updates.
6. Test combined mode at the 3080 preset before increasing any budget.
7. Exercise readiness-aware status, controlled AI recovery, and thermal/transport
   soaks before an audience run.
8. Move AI to a second GPU/computer using a profile change, without changing
   the artistic world network.

For the process split and failure behavior, read
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).  For the frame/metadata interface,
read [docs/WORLDBUS.md](docs/WORLDBUS.md).

## License

This project is released under the [MIT License](LICENSE).
