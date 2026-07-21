# 5090 workstation migration

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
Get-FileHash .\projects\FlexShow-moge2-embody-local-5090.28.toe -Algorithm SHA256
```

After copying it to the new PC, run the same command and compare the complete
hash before opening the file.

## Keep the 3080 and 5090 identities separate

This repository is one hardware-neutral codebase. The 3080 and 5090 are not
separate Git branches and must not publish machine-local state. Keep these
ignored identities distinct:

| Computer | Worker profile | Local config | Working TOE pattern |
| --- | --- | --- | --- |
| RTX 3080 Ti Laptop 16 GB | `3080ti_16gb` | `config/local-3080ti.json` | `projects/*-3080ti-*.toe` |
| RTX 5090 32 GB | `5090` | `config/local-5090.json` | `projects/*-5090-*.toe` |

Generate each local config on its destination computer. Do not transfer GPU
UUIDs, absolute project paths, `.flexgpu` manifests, or a saved working TOE
back into the other machine's filename. The worker launchers require an
explicit profile and reject a real GPU/profile mismatch by default.

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
git checkout codex/v1-2-production-foundation
git pull --ff-only
```

If the publication branch has been merged, check out the repository's default
branch instead. Verify the public tree before adding private files:

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
  -Project .\projects\FlexShow-moge2-embody-local-5090.28.toe `
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

On 2026-07-20, an ignored local checkpoint through `.28` was exercised on the
RTX 5090 with TouchDesigner `2025.32820` and the private StreamDiffusionTD
component. MoGe-2 returned synchronized geometry at 15 FPS capture. Depth
Anything V2 Small calibrated and returned live changing geometry at about
12 accepted FPS. Both used the existing 512x512 position/color/interaction
contracts and 5760x1080 panoramic output; all required outputs were valid with
zero managed operator or shader errors in the bounded scan. A GPU sample during
Depth Anything reported about 12.3/32.6 GB VRAM and 77% utilization.

The local `.28` file, private components, worker weights, logs, and
`config/local-5090.json` remain intentionally untracked. This record is a short
functional migration check, not the required sustained thermal, projector,
interaction, or venue acceptance.

## Acceptance order

1. Recursive TouchDesigner errors and warnings.
2. Selected geometry bridge freshness and increasing source frame ID.
3. RGB orientation and synchronized finite position/depth.
4. `OUT_INSTALLATION` at 1920x1080.
5. Panoramic left/center/right, each at 1920x1080.
6. Artistic left/center/right, each at 1920x1080.
7. Stereo development preview; this is not completed VR.
8. Provider switch MoGe-2 -> Depth Anything -> MoGe-2 with one worker at a time.
9. Performance and VRAM at the 5090 quality preset.
10. A sustained thermal soak, followed by projector warp/blend and physical
    sensor calibration at the venue.

Raise StreamDiffusion, generated-geometry resolution, and point budget in
measured steps. Native 1920x1080 output dimensions do not require 1920x1080
depth inference, and increasing only the final TOP size cannot create missing
geometry detail.
