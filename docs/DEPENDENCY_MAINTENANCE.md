# Isolated worker security maintenance

This source update does not change local TOEs, GPU assignments, calibration,
model weights, or existing worker environments. It does not modify the 5090
machine. Shared dependency pins apply only when a machine explicitly installs
them; a Git pull is not a package upgrade.

## October 2026 candidate pins

| Package | Pin | Scope |
| --- | --- | --- |
| Pillow | 13.0.0 | Source tests, MoGe-2, Depth Anything |
| Transformers | 5.20.2 | Depth Anything sensor and geometry |
| huggingface-hub | 2.3.0 | MoGe-2 and Depth Anything |
| safetensors | 0.8.0 | Depth Anything |
| NumPy | 2.4.6 | Source tests, MoGe-2, Depth Anything |
| SciPy | 1.17.1 | MoGe-2 |
| OpenCV | 5.0.0.93 | MoGe-2 and Depth Anything |
| Matplotlib | 3.11.2 | MoGe-2 |
| jsonschema | 4.26.0 | Source/schema tests only |

Dependabot PR #26 originally proposed NumPy 2.5.4 and SciPy 1.18.1, which
require Python 3.12 and cannot install on our 3.11 baseline. The repaired
candidate uses [NumPy 2.4.6](https://pypi.org/project/numpy/2.4.6/) and
[SciPy 1.17.1](https://pypi.org/project/scipy/1.17.1/), both with Python 3.11
Windows wheels. Keep these compatibility-series limits under review rather
than disabling dependency security alerts. The newer Hub/OpenCV APIs and
Transformers are still a coherent runtime upgrade: a source CI pass alone
does not approve them for the working show.

Candidate evidence on an RTX 3080 Ti Laptop (28 October 2026): fresh Python
3.11 environments installed successfully; the Full source gate passed 495
tests with four platform-specific skips. Both pinned models passed six
offline CUDA inferences (one model load each) across all three source sizes,
with synchronized atlas RGB and positive finite valid depth. The
custom-generation consent regression passed. OpenCV 5 camera API symbols and
synthetic resize were checked without opening a camera. No working environment
was activated, no TOE was changed, and live sensor/visual/thermal/5090
acceptance remains separate.

Pillow 13 fixes the advisories affecting the previous image-library pin and
requires Python 3.11+. See the [upstream release notes](https://pillow.readthedocs.io/en/stable/releasenotes/13.0.0.html).
The full manual/release CI matrix is Python 3.11/3.12 on Windows and 3.12 on
Linux. Ordinary code PRs run the full source gate on Windows/Python 3.11;
post-merge and documentation-only checks use the explicitly limited Quick gate.
The dependency-free launcher remains usable on Python 3.10; do not install
the image/model requirements into that interpreter or into TouchDesigner.

The runtime retains the custom-generation consent fix introduced in
Transformers 5.17.0,
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

## Local candidate workflow: test, accept explicitly, roll back

There is no global environment selector, silent package upgrade, venv copying,
or automatic activation. The working `.venv/moge2` and `.venv/depth-anything`
stay in place. Use a fresh machine-tagged candidate name; trying to install
into an existing candidate is rejected, including a partially installed one.

```powershell
# Preview first. Add -Install explicitly to create this separate environment.
.\scripts\Initialize-DepthAnything.ps1 -CandidateName 3080-security-oct2026
.\scripts\Initialize-DepthAnything.ps1 -CandidateName 3080-security-oct2026 -Install
$candidate = (Resolve-Path .\.venv\depth-anything-candidate-3080-security-oct2026\Scripts\python.exe).Path

# No GPU/model/network/camera work; checks installed package/source pins.
.\scripts\Test-WorkerCandidate.ps1 -Provider depth_anything `
  -Profile 3080ti_16gb -WorkerPython $candidate -AuditOnly

# Only on an idle 3080, with the show's model workers stopped.
# Uses existing verified model files; it never downloads a checkpoint.
.\scripts\Test-WorkerCandidate.ps1 -Provider depth_anything `
  -Profile 3080ti_16gb -WorkerPython $candidate -RunGpuSmoke

# Rehearsal/acceptance uses the candidate for THIS launch only.
# Select the matching provider in TD and stop the previous worker first.
.\scripts\Start-DepthAnythingGeometryWorker.ps1 -Profile 3080ti_16gb `
  -WorkerPython $candidate
# Add -Start after checking the preview, then perform live and thermal checks.
```

MoGe uses `Initialize-MoGe2.ps1 -CandidateName 3080-security-oct2026`, the
resulting `.venv/moge2-candidate-3080-security-oct2026/Scripts/python.exe`,
`-Provider moge2`, and `Start-MoGe2Worker.ps1`. The audience-camera
`Start-DepthAnythingWorker.ps1` also accepts `-WorkerPython`; test its camera
and interaction path separately. These flags never change the Show Control
buttons' saved launch behavior. Continue launching the accepted candidate
with its explicit path until a separate, reviewed live integration is made.

With neither `-AuditOnly` nor `-RunGpuSmoke`, the candidate checker only previews
the plan. Its ignored report is
`runtime/validation/worker-<profile>-<provider>-latest.json`; `-Report` can
retain a timestamped checkpoint under `runtime/`. Failed checks replace stale
pass evidence with failure. An audit pass certifies pins only, not GPU work or
show readiness. The GPU test checks physical GPU/profile identity and refuses
4090/5090 certification because its synthetic geometry contract is 3080-only.
It refuses a generated-geometry worker already running from the same checkout,
but cannot detect every GPU consumer: stop diffusion/other GPU jobs yourself.

**Accept:** after the source Full check, candidate audit, GPU smoke, and real
freshness/orientation/wall/sensor/performance/soak checks pass, record the exact
candidate path and evidence locally and continue passing that path explicitly.
Reports are evidence, not authorization to switch a running show.

**Rollback:** stop the candidate through the existing checkout-scoped stop
control, then rerun the same launch command without `-WorkerPython` (or with
the previously accepted explicit path). The default environment was not
modified. Rolling back may restore older vulnerable pins: use only as a
temporary operational fallback and retain the security remediation plan.
Never delete the previous environment before accepting its replacement.

Do not move/copy a virtual environment between the 3080 and 5090. Candidate
names, reports, working TOEs, local configs, GPU UUIDs, and calibration stay
machine-local. No CI result substitutes for a physical show check.
