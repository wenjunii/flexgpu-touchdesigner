# 5090 workstation migration

For an **existing** 4090/5090 installation receiving the new single-generator
host, follow [the targeted host upgrade](STREAMDIFFUSION_HOST_UPGRADE.md).
Preserve the destination's latest local TOE and settings; do not replace them
with the 3080 `.46` or rerun workstation initialization. The checklist below is
for original/new-workstation bootstrap; dated checkpoint records are historical.

This checklist moves an accepted FlexGPU working project to an RTX 5090 PC
without publishing private components, credentials, model weights, captures,
or machine-specific runtime state.

## Transfer map

| Material | Transfer channel | Destination action |
|---|---|---|
| Tracked source, scripts, public presets, tests, docs | GitHub | Clone and check out the published branch |
| Accepted working `.toe` | Private local copy | Put in ignored `projects/`; retain the source file and SHA-256 hash |
| Private/paid `.tox` components | Private local copy, only when the licence allows | Put in ignored `local-components/` and reconnect through public adapter TOPs |
| MoGe-2 and Depth Anything weights | Download on destination where possible | Use the pinned initializer scripts; otherwise transfer privately only when redistribution terms allow |
| Venue calibration | Private local copy | Revalidate for the destination projectors, walls, and sensor placement |
| Credentials, tokens, `.env`, key stores | Do not transfer through this project | Recreate through the destination machine's secret manager if actually required |
| `.venv`, `.flexgpu`, runtime manifests, logs, caches, old GPU UUIDs | Do not copy | Rebuild or regenerate on the 5090 PC |

Git is not a backup channel for the ignored working TOE or private assets. Keep
at least two private copies of the accepted TOE and record its hash:

```powershell
Get-FileHash .\projects\FlexShow-moge2-embody-podcast-local-5090.71.toe -Algorithm SHA256
```

After copying it to the new PC, run the same command and compare the complete
hash before opening the file.

## Keep the 3080 and 5090 identities separate

This repository is one hardware-neutral codebase. The 3080 and 5090 are not
separate Git branches and must not publish machine-local state. Keep these
ignored identities distinct:

| Computer | Worker profile | Local config | Working TOE pattern |
| --- | --- | --- | --- |
| RTX 3080 Ti Laptop 16 GB | `3080ti_16gb` | `config/local-3080*.json` | `projects/*-3080*.toe` |
| RTX 5090 32 GB | `5090` | `config/local-5090*.json` | `projects/*-5090*.toe` |

Generate each local config on its destination computer. Do not transfer GPU
UUIDs, absolute project paths, `.flexgpu` manifests, or a saved working TOE
back into the other machine's filename. The worker launchers require an
explicit profile and reject a real GPU/profile mismatch by default.

The known local checkpoints are `.27` for the 3080 and `.71` for the 5090.
These numbers are machine-local histories, not a cross-machine upgrade order.
Never point `config/local-5090.json` at the 3080 `.27` file or use the 5090
`.71` file as a 3080 save target. The `.71` name includes `podcast` because
that local checkpoint combines the two networks; the audio tracks, private
components, and TOE remain outside Git.

## Destination prerequisites

- RTX 5090 with current NVIDIA Studio driver and at least 28 GB reported VRAM.
- TouchDesigner `2025.32820`, the validated project baseline.
- Git and Python 3.11.
- Enough local storage for both isolated worker environments and checkpoints.
- The same private StreamDiffusionTD release used during acceptance, unless a
  deliberate upgrade is being validated separately.

Do not start by upgrading TouchDesigner, StreamDiffusionTD, CUDA/PyTorch, and
the GPU machine simultaneously. First reproduce the accepted software baseline
on the 5090; upgrade one layer at a time after it passes.

## Public repository setup

```powershell
git clone https://github.com/wenjunii/flexgpu-touchdesigner.git
cd .\flexgpu-touchdesigner
git fetch --all --prune
git switch main
git pull --ff-only origin main
```

Use the merged public default branch. Verify the public tree before adding private files:

```powershell
.\scripts\Test-PublicSync.ps1 -Scope Both -SelfTest -ExitWithCode
```

## Regenerate the 5090-local configuration

Copy the accepted working TOE privately into `projects/`, verify its hash, then
run:

```powershell
.\scripts\Initialize-FlexShow.ps1 `
  -Topology single `
  -Experience installation `
  -Completion hybrid `
  -DisplayProfile venue_1080p `
  -DisplayMode panoramic_wrap `
  -GeometryProvider moge2 `
  -Project .\projects\FlexShow-moge2-embody-podcast-local-5090.71.toe `
  -Output .\config\local-5090.json
```

The initializer detects the new GPU UUID and 5090 tier. Use
`-GeometryProvider depth_anything` if that path should be active at cold start.
Provider changes made later in `SHOW_CONTROL` do not require regenerating this
file.

Preview and diagnose before launch:

```powershell
.\scripts\Diagnose-FlexShow.ps1 -Config .\config\local-5090.json
.\scripts\Start-FlexShow.ps1 -Config .\config\local-5090.json
```

The preview must identify the 5090, TouchDesigner `2025.32820`, and the copied
working TOE. Do not add `-Start` if it references an old user path or GPU UUID.

## Rebuild inference environments

```powershell
.\scripts\Initialize-MoGe2.ps1 -Install -DownloadModel
.\scripts\Initialize-DepthAnything.ps1 -Install -DownloadModel
```

Start TouchDesigner first so the selected provider's listener is available,
then start only the worker under test:

```powershell
.\scripts\Start-FlexShow.ps1 -Config .\config\local-5090.json -Start
.\scripts\Start-MoGe2Worker.ps1 -Profile 5090 -Backend moge2 -GpuIndex 0 -Start
```

For the alternative generated-geometry path, stop MoGe-2, select
`depth_anything`, and run:

```powershell
.\scripts\Stop-GeneratedGeometryWorker.ps1 -Stop
.\scripts\Start-DepthAnythingGeometryWorker.ps1 `
  -Profile 5090 `
  -Backend depth_anything `
  -GpuIndex 0 `
  -Start
```

`Start-DepthAnythingWorker.ps1` is the separate webcam/audience-interaction
worker; it is not the generated-image geometry worker.

Use `Stop-GeneratedGeometryWorker.ps1` for provider changes, including workers
launched in a hidden terminal. It matches this checkout's exact generated-worker
path and cannot select the audience-camera worker or another checkout. The
command is preview-only unless `-Stop` is supplied.

## 5090 short live acceptance record

On 2026-07-24, the ignored local 5090 checkpoint `.30` was exercised with
TouchDesigner `2025.32820` and the locally licensed private StreamDiffusionTD
component. Its image was visible, StreamDiffusionTD ran at about 20 FPS, and
MoGe-2 returned current synchronized geometry at 15 FPS capture. The output
contract remained 1920x1080 per wall and 5760x1080 for the three-wall mosaic;
required outputs were valid in the bounded live check. The earlier `.28`
checkpoint had also exercised Depth Anything V2 Small with live changing
geometry at about 12 accepted FPS and approximately 12.3/32.6 GB VRAM at 77%
utilization.

The 5090 `.30`, 3080 `.27`, private components and licence data, worker
weights, logs, and all `config/local-*.json` files remain intentionally
untracked. This record is a short functional migration check, not the required
sustained thermal, projector, interaction, or venue acceptance.

The historical combined 5090 working checkpoint `.71` does not replace the
3080 history. The earlier `.49` audit found and repaired a stale near-black
adapter frame plus an invalid F1 window reference. Subsequent 5090-only
checkpoints added output-local interaction routing, independent webcam/Femto
calibration trims, a selectable native Femto Mega point-cloud source, and a
bounded 32x32 interaction occupancy search. The `.67` live check used a Femto
audience gate of `2.0` to `5.0` metres, a `1.25` metre interaction radius,
neutral `1.0` point-cloud scale, and `1.5` metre artistic side offset. It
reported zero recursive TouchDesigner errors, current generated frames, and
valid 1920x1080 installation plus six wall surfaces. These are local 5090
venue values stored only in the ignored TOE; do not copy them into the 3080
working project or tracked defaults.

The `.71` checkpoint was saved on 2026-07-29 after a disconnected-device
control audit. The public validator passed 252 of 252 checks with zero
restoration failures. The Show Control start and stop pairs for MoGe-2,
generated-geometry Depth Anything, and Camera Depth all changed to a real
checkout-scoped PID and returned to PID zero. The managed pipeline and all
seven 1920x1080 output TOPs reported zero errors. Because the webcam and Femto
Mega were physically disconnected and StreamDiffusionTD was intentionally left
stopped, this record does not replace the `.67` live sensor/image acceptance or
the remaining StreamDiffusionTD scene-advance and visual-content check.

## Saved .80 host and control audit

On 2026-11-11, the user saved
`FlexShow-moge2-embody-podcast-local-5090.80.toe` after the targeted PR #33 host
upgrade of the accepted 5090 `.64` baseline. The 3080 `.46` was not used as a
replacement TOE. No legacy generator was deleted, no private component was
inspected internally, and neither inference environment was upgraded.

The installed-control mapping/dimension audit passed 233 checks with zero
failures or restoration failures. The main panel contains 86 older core
controls plus all 32 host controls; its older podcast panel contains 18
controls. Desktop-VR, separate Femto trims and the podcast Visualpath selector
are not installed in this baseline. Do not install these unrelated features
merely to satisfy the current full-inventory validator. Explicit older-feature
mode only permits wholly absent known groups and rejects partial installations.

After saving a rollback and stopping generation, run in TD Textport:

```python
from pathlib import Path
import importlib, sys
repo = Path(project.folder).parent
sys.path.insert(0, str(repo / 'touchdesigner'))
import validate_show_controls as controls
importlib.reload(controls)
report = controls.validate(
    report_path=repo / '.flexgpu' / 'evidence' / 'show-controls-5090.json',
    expected_profile='5090', operator_lookup=op,
    allow_absent_optional=True)
print(report['status'], report['summary'], report['features'])
```

Keep Follow Audio / Podcast Prompts off for this mutating audit. It exercises
public value mappings and safe reset behavior, restores original values, and
does not start workers or exercise generator shutdown/removal. Wrong Show
Control or geometry-bridge profiles are refused before mutations. Do not save
a failed audit; reopen the saved same-machine rollback instead.

Check the resulting ignored report from PowerShell:

```powershell
.\scripts\Test-FlexShowControlReport.ps1 `
  -Report .\.flexgpu\evidence\show-controls-5090.json `
  -ExpectedProfile 5090
```

The `.80` audit also compared all nine public camera parameter states exactly
before/after (no changes), captured the source/position/single/triple outputs,
and confirmed 1920x1080 for every wall plus 5760x1080 for both mosaics. Separate
MoGe-2 and generated-geometry Depth Anything Start/Stop tests verified each
checkout-owned process command line used `--profile 5090`, with no worker
remaining afterward. RGB delivery was off, so this does not verify frame
freshness or real depth. The disconnected Femto path failed closed; the final
recursive error scan was empty. No audit change was saved over `.80`.

The generator slot remains empty. Trusted generator loading, native
start/stop/confirmed offload/version replacement, audio scene/timeline pulses,
prompt forwarding into a running generator, changing imagery, per-provider
freshness, artistic bouncing/parallax, physical sensors, stereo and thermal/
venue acceptance remain deferred. Do not treat dark/procedural output with RGB
delivery off as a visual pass. The launcher still targets accepted `.64` until
real-imagery acceptance; open the saved `.80` manually for continuation.

The Full release gate requires the exact `requirements-test.txt` pins in an
isolated source-test interpreter. Initial package-index DNS failures recovered
during this audit, allowing that source-only environment to be prepared without
changing either model environment. Run the release script with a process-local
PowerShell execution-policy override if required by the local script policy;
do not change the machine-wide policy. Source-release and live-show gates
remain distinct, and raw reports stay ignored.

## Saved .90 receiver and live-control audit

The later 2026-11-11 receiver migration supersedes the `.80` pending-image
status above. The 5090's own world now includes the reviewed 3080 `.46`
playlist/public controls, not the 3080 TOE or its GPU configuration. The
selected trusted local generator is retained. Only the two separately reviewed
stopped legacy roots/docked callbacks were retired with approval and rollback.

The new local checkpoint is
`FlexShow-moge2-embody-podcast-local-5090.90.toe`. Only
`config/local-5090.json`'s `processes.world.project` was retargeted. The 3080
configs/TOEs, model environments, calibration, camera motion expressions,
ports, profiles and accepted rollback checkpoints remain unchanged.

Passed: 241 installed control/dimension checks, 1,897 muted playlist checks,
31 safe host mappings, asynchronous native generator/playlist/worker buttons,
confirmed offload with component retained, smoothing/RGB routing, actual
prompt/seed forwarding, and separate real-image MoGe-2/Depth Anything freshness
tests with profile `5090`. Seven surfaces stayed 1920x1080 and both mosaics
5760x1080; orientation, depth/parallax and camera differences were inspected.
All inference was stopped afterward. Same-process reopen matched all audited
public values and all nine camera parameter states, retired old registered
receivers, and reported zero recursive operator errors. The startup fix keeps
saved provider/display selections and manual source routing even if first-tick
initialization runs early.

Not claimed: audible listening, real TOX version replacement/removal, physical
webcam/Femto interaction while disconnected, disabled desktop stereo/headset
VR, or sustained thermal/venue acceptance. See
[the receiver recipe](5090_PODCAST_RECEIVER_SYNC.md) for bounded installation,
audit evidence scope and reload safety. Local TOEs, configs, media, captures
and runtime reports remain ignored; only public source/docs are synchronized.

## Historical .71 live validation recipe

In the TouchDesigner Textport:

```python
from pathlib import Path
import importlib, sys
root = Path(r'C:\path\to\flexgpu-touchdesigner')
sys.path.insert(0, str(root / 'touchdesigner'))

import runtime_pipeline as rp
importlib.reload(rp)
rp.install_perform_window(op('/project1/flexgpu'))

import validate_project as vp
importlib.reload(vp)
first = vp.validate(
    expected_build='1.2.1',
    expected_experience='installation',
    require_live_source=True,
    report_path=root / 'runtime' / 'validation' / '5090-live-first.json')
print(first['status'], first['live_source'])
```

After the visible prompt/scene changes, run a second audit using
`previous_live_source_digest=first['live_source']['digest']`. The second call
must show a different digest and visible-range content. Stop StreamDiffusionTD
and both generated-geometry workers after the check.

## Acceptance order

1. Recursive TouchDesigner errors and warnings.
2. Canonical Perform Mode window exists and F1 opens
   `WORKING_PIPELINE/OUT_DISPLAY_ACTIVE`.
3. Licensed adapter RGB is visible and changes across two separated samples.
4. Selected geometry bridge freshness and increasing source frame ID.
5. RGB orientation and synchronized finite position/depth.
6. `OUT_INSTALLATION` at 1920x1080.
7. Panoramic left/center/right, each at 1920x1080.
8. Artistic left/center/right, each at 1920x1080.
9. Stereo development preview; this is not completed VR.
10. Provider switch MoGe-2 -> Depth Anything -> MoGe-2 with one worker at a time.
11. Performance and VRAM at the 5090 quality preset.
12. A sustained thermal soak, followed by projector warp/blend and physical
    sensor calibration at the venue.

Raise StreamDiffusion, generated-geometry resolution, and point budget in
measured steps. Native 1920x1080 output dimensions do not require 1920x1080
depth inference, and increasing only the final TOP size cannot create missing
geometry detail.
