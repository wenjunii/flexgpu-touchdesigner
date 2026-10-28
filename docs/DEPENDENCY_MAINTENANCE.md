# Isolated worker security maintenance

This source update does not change local TOEs, GPU assignments, calibration,
model weights, or existing worker environments. It does not modify the 5090
machine. Shared dependency pins apply only when a machine explicitly installs
them; a Git pull is not a package upgrade.

## October 2026 candidate pins

| Package | Pin | Scope |
| --- | --- | --- |
| Pillow | 13.0.0 | Source tests, MoGe-2, Depth Anything |
| Transformers | 5.17.0 | Depth Anything sensor and geometry |
| huggingface-hub | 1.24.0 (unchanged) | Depth Anything |
| safetensors | 0.8.0 | Depth Anything; required by Transformers 5.17.0 |

Pillow 13 fixes the advisories affecting the previous image-library pin and
requires Python 3.11+. See the [upstream release notes](https://pillow.readthedocs.io/en/stable/releasenotes/13.0.0.html).
The source-test CI matrix is now Python 3.11/3.12 on Windows and 3.12 on Linux.
The dependency-free launcher remains usable on Python 3.10; do not install
the image/model requirements into that interpreter or into TouchDesigner.

Transformers 5.17.0 contains the custom-generation consent fix
[`cbc1651`](https://github.com/huggingface/transformers/commit/cbc1651a032b923da7f4b44b3d0e6f68e6ba6b55),
which is absent from Dependabot PR #21's proposed 5.10.1. This is a targeted
compatibility candidate, not a claim that any version will remain free of
future vulnerabilities. Recheck advisories before deployment. Normal depth
inference continues to verify the immutable local model/config hashes, load
safetensors only, and disable remote code and network model downloads.

## Safe staging and verification

1. Use a separate checkout/worktree and fresh Python 3.11 environments. Do not
   run an install into the only working show checkout. Preserve the working
   3080 TOE and environments; never copy local 3080 settings onto the 5090.
2. In that staging checkout, run `Initialize-MoGe2.ps1 -Install` and/or
   `Initialize-DepthAnything.ps1 -Install`. These create environments under
   that checkout's ignored `.venv/`. Model download remains a separate opt-in;
   the smoke test can read an already installed, hash-verified local model.
3. Run the source release gate with `requirements-test.txt` in a separate
   Python 3.11+ test environment. Then run each model smoke sequentially on an
   idle GPU using the matching candidate environment, as below.
4. Before promoting the candidate environments, repeat live source freshness,
   orientation, single/triple-wall, sensor, performance, and thermal checks.
   These synthetic tests do not validate visual quality, live TouchDesigner,
   headset submission, or the 5090. Do not upgrade a running show's environment.

Preview (no model import, GPU work, webcam, or sockets):

```powershell
.\.venv\depth-anything\Scripts\python.exe .\tools\smoke_worker_runtime.py --provider depth_anything
.\.venv\moge2\Scripts\python.exe .\tools\smoke_worker_runtime.py --provider moge2
```

Explicit offline CUDA checks, using existing pinned model paths:

```powershell
.\.venv\depth-anything\Scripts\python.exe .\tools\smoke_worker_runtime.py `
  --provider depth_anything --model C:\path\to\runtime\depth-anything-v2-small --run
.\.venv\moge2\Scripts\python.exe .\tools\smoke_worker_runtime.py `
  --provider moge2 --model C:\path\to\runtime\moge2-model\model.pt --run
```

Without `--model`, the tool uses the staging checkout's standard model path.
`--device cuda:0` is the default; run only on the intended idle test GPU. The
smoke profile is explicitly `3080ti_16gb` and never edits other tier defaults.
It checks installed runtime pins and runs two synthetic frames at each of
512x512, 1024x567, and 1024x576 through one persistent model. Expected geometry
sizes are 384x384, 512x284, and 512x288, with matching packed RGB and nonempty,
positive finite depth. Depth Anything also checks that custom generation with
`trust_remote_code=False` rejects before attempting a code download (the Hub
probes are mocked, so this regression does not contact a model repository).

The test prints JSON only, never opens the camera or a listening socket, and
does not save synthetic RGB. Any cache stays in ignored `runtime/`. Keep real
captures, model files, test reports, and working TOEs out of Git. MoGe's optional
upstream app dependencies remain deliberately excluded; the smoke test checks
its actual pinned worker packages and inference rather than installing unused
apps merely to satisfy `pip check`.

Passing a smoke test is necessary but not sufficient for show acceptance.
Leave the existing working environments available until the live checks pass.
