# MoGe-2 generated-world integration

This integration turns a saved or live StreamDiffusionTD RGB frame into
bounded, verified geometry. The offline probe is the first gate; the persistent
worker and default-off TouchDesigner bridge provide the live path without
loading PyTorch inside TouchDesigner.

The implementation pins:

- Microsoft MoGe source revision
  `07444410f1e33f402353b99d6ccd26bd31e469e8`;
- official `Ruicheng/moge-2-vits-normal` model revision
  `679230677b4d282c6f304189a93e98e14f085902`;
- official checkpoint SHA-256
  `79a16621928c2bf0ed04659218c55c01075e950507f40bb3332fb4c873d3e1dc`.

The public repository contains the integration and integrity manifest only.
The virtual environment, model, Hugging Face cache, source images, point
clouds, and reports remain below ignored `.venv/` or `runtime/` paths.

## Install the isolated runtime

Preview the actions first:

```powershell
.\scripts\Initialize-MoGe2.ps1
```

Create the Python 3.11 environment and install the pinned CUDA runtime:

```powershell
.\scripts\Initialize-MoGe2.ps1 -Install
```

Model acquisition is a separate, explicit network action:

```powershell
.\scripts\Initialize-MoGe2.ps1 -DownloadModel
```

Normal inference never contacts Hugging Face.  It verifies the local model
size and SHA-256 before `torch.load`, sets offline mode, and refuses a missing
or changed checkpoint.

## Inspect profiles and runtime

```powershell
.\.venv\moge2\Scripts\python.exe .\tools\moge2_probe.py profiles
.\.venv\moge2\Scripts\python.exe .\tools\moge2_probe.py doctor `
  --profile 3080ti_16gb
```

The 4090 and 5090 entries are conservative starting profiles using the same
verified ViT-S checkpoint with more tokens.  They are not performance claims.
Larger checkpoints remain an explicit later A/B decision.

## Run the offline gate

Export one generated RGB image from StreamDiffusionTD, then run:

```powershell
.\.venv\moge2\Scripts\python.exe .\tools\moge2_probe.py infer `
  --input C:\path\to\generated-frame.png `
  --profile 3080ti_16gb `
  --run-id first-generated-frame
```

The run is written beneath `runtime/moge2-runs/` and contains:

- the exact resized RGB input;
- metric optical-axis depth;
- a binary valid-geometry mask and explicit binary confidence proxy;
- camera-space position and normal maps converted from OpenCV coordinates to
  FlexGPU coordinates;
- normalized and pixel intrinsics;
- RGB/depth/normal previews, an oblique point-cloud preview, a binary PLY,
  timing/VRAM evidence, and SHA-256 verified NumPy planes.

MoGe-2 is a single-image visible-surface estimator.  Generated imagery has no
physical ground-truth scale, and estimated scale/FoV may drift between frames.
Temporal alignment, point persistence, thick points, fog/noise, and procedural
backfill remain part of the world pipeline.

## Run the live worker

Install `MOGE2_BRIDGE` into an ignored local `.toe` and enable its result
receiver before starting the worker. Preview-only is the default:

```powershell
.\scripts\Start-MoGe2Worker.ps1 -Backend mock
.\scripts\Start-MoGe2Worker.ps1 -Backend mock -Start

.\scripts\Start-MoGe2Worker.ps1 `
  -Profile 3080ti_16gb `
  -GpuIndex 0 `
  -Start
```

The worker is foreground by design; Ctrl+C stops it. It loads one verified
local checkpoint, accepts only bounded WorldBus RGBA8 frames, processes the
newest pending frame, locks FoV/intrinsics for a source session, and returns one
synchronized RGBA8 atlas. Unknown input extensions, local paths, credentials,
and prompt text are not forwarded.

Use `-GpuIndex 1` to place MoGe on the second physical NVIDIA GPU. Host and port
switches support a two-computer trusted-LAN layout. WorldBus is not encrypted
or authenticated, so never expose the endpoints to an untrusted network.

The complete TouchDesigner install, source-configuration, startup-order, and
acceptance sequence is in [docs/MOGE2_LIVE.md](../../docs/MOGE2_LIVE.md).

## Licensing

MoGe is MIT licensed; its bundled DINOv2 code is Apache-2.0.  The official
model repository is tagged MIT.  Retain upstream notices when distributing an
installation.  This repository does not redistribute the checkpoint.
