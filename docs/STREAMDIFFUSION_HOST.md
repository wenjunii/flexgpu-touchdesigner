# StreamDiffusion host and optional RGB smoothing

This is an optional source-side upgrade, not a new geometry provider or inference
engine. Its public builders are `touchdesigner/streamdiffusion_host.py` and
`touchdesigner/streamdiffusion_host_runtime.py`; the latter is embedded in a DAT
and has no repository imports, allowing a saved local TOE to reopen cold.

```text
accepted RGB / selected private payload public RGB TOP
                         |
              optional temporal smoothing
                         |
          +--------------+--------------+
          |                             |
       MoGe-2                    Depth Anything geometry
          +--------------+--------------+
            existing synchronized RGB/depth routes
```

Both workers receive the exact same host output. The adapter's `OUT_RGB`,
`OUT_DEPTH`, confidence/mask routes, worker ports, reconstruction, interaction,
single wall, six triple-surface feeds and desktop stereo are not replaced.
The host never applies RGB-only smoothing after depth inference.

## Install in a saved working copy

Keep the machine's saved 3080 or 5090 TOE/profile separate. Make an ignored local
backup first, then run inside TouchDesigner using that checkout's public source:

```python
from pathlib import Path
import importlib, sys
root = Path(project.folder).parent
sys.path.insert(0, str(root / 'touchdesigner'))
import streamdiffusion_host as host
importlib.reload(host)
host.install(op('/project1/flexgpu'))
```

This bounded installer creates
`/project1/flexgpu/WORKING_PIPELINE/SOURCES/STREAMDIFFUSION_ADAPTER/STREAMDIFFUSION_HOST`
and a **StreamDiffusion** tab on the main `SHOW_CONTROL`. Both existing geometry
bridges must share one RGB source; installation fails rather than choosing
between divergent inputs. The accepted source is connected to `EXISTING_RGB`.
Both bridges are then connected to host output zero (`OUT_RGB`). Output one is
`OUT_RAW`, useful for comparison. No private component is moved, inspected,
externalized, deleted, loaded, or started by installation. It does not save a TOE.
Re-running the installer preserves user parameter values and the drop slot.

The main builder repairs host routing when rebuilding an already-upgraded
project. Fresh synthetic projects retain the original source layout until this
optional installer is explicitly run. Do not rebuild the whole working project
just to install this feature.

## Use an existing or replacement version

1. Leave **RGB Source = Existing** to retain the current podcast pipeline and all
   existing StreamDiffusion controls unchanged.
2. Stop/unload the old model server before starting another on a 16 GB GPU.
   Merely switching RGB selection or disabling cooking does not free external
   model VRAM. The host does not guess how a private version stops its server.
3. Drop a trusted private `.tox` inside `PAYLOAD_SLOT`, or choose its local file
   and pulse **Load into Empty Drop Slot**. The button refuses an occupied slot
   and never replaces/deletes its previous contents. Loading executes the
   component's own code, which may have its own startup behavior; use only a
   trusted component. The host itself sends no Start command automatically.
4. Set **Selected Drop-Slot Component Name** if several candidates exist. A sole
   child COMP is selected automatically. Selection is limited to direct children
   of this slot; arbitrary network paths are rejected.
5. Set **Public RGB TOP Relative to Component**, for example `out1`, to that
   version's documented public output TOP. No private DAT or internal operator
   discovery is used. Choose **RGB Source = Slot** after checking the preview.
6. Map public top-level custom parameter names for Prompt/Seed and optional
   Start/Stop pulses. An absent/unmapped parameter reports a status error rather
   than evaluating code or changing a guessed built-in parameter. Pulses require
   a deliberately configured mapping. Actual inference-server behavior remains
   version-specific and must be tested with the user's component.

If the selected payload or RGB output is missing, **Ready** turns off, the status
explicitly reports fallback, and the accepted existing RGB remains connected.
This is not a claim that the requested replacement is running.

The host accepts RGB output; an image-to-image input, if required by a particular
version, still needs its documented input connection. The current private
components and podcast paths are preserved. **Follow Podcast Dominant Prompt /
Seed** is an optional explicit mapping from public `prompt_out` columns
`concept`, `seed`, and `weight`; it chooses the dominant row. It does not recreate
the podcast's dual-generator transition compositor. Leave it off to manage the
replacement version yourself. Existing-source mode retains the original full
podcast workflow.

## Temporal smoothing

- Default: off; bypass is pixel-identical to the existing RGB boundary.
- Response: default 50 ms, bounded 0–250 ms. Zero has no temporal retention.
- Uses a native-input-resolution RGBA16F feedback blend, not another AI model.
- History resets on source, payload/output, scene/seed identity, input dimensions,
  smoothing setting changes, or **Reset History**.
- Response timing follows actual filter cooks, not merely callback frequency.
  No extra source cooks or geometry inference are forced to manufacture frames.
- The host reports routing/readiness, not fabricated source FPS or freshness;
  geometry bridges retain ownership of synchronized frame validity and age.

Start low and compare raw/processed output. This reduces temporal flicker but
does not increase generation throughput or synthesize true intermediate motion.
Large values can blur details, trail moving subjects, increase perceived delay,
and make monocular depth harder to estimate. The point world already has its own
temporal persistence; avoid stacking strong smoothing at both stages. Run the
timeline normally for dynamic feedback and callback/button testing.

## Engine COMP remains a measured follow-up

No Engine COMP is created or launched by this feature. A later independent
producer can feed the same existing RGB boundary, without replacing depth or
rendering. Engine can isolate blocking CPU/TD work, but does not create more GPU
compute or VRAM. It also changes initialization callbacks, path handling,
parameter support, and buffering. Benchmark with the exact private component
and identical generation/depth/display settings before selecting it.

References: [Feedback TOP](https://derivative.ca/UserGuide/Feedback_TOP) and
[Engine COMP](https://derivative.ca/UserGuide/Engine_COMP).

## Validation and current acceptance

```powershell
& $env:FLEXSHOW_PYTHON -m unittest tests.test_streamdiffusion_host -v
.\scripts\Test-FlexShowRelease.ps1
```

Set `FLEXSHOW_PYTHON` to a separate approved source-test interpreter containing
the exact pins in `requirements-test.txt`; do not upgrade an active AI worker's
environment to run this gate. The verification wrapper pipes Python output so
Windows GUI-subsystem launchers are waited for before checking exit status or
deleting temporary files.

Inside TD, the read-only structural gate is:

```python
import validate_streamdiffusion_host as validation
print(validation.validate(op('/project1/flexgpu')))
```

The main Show Control validator accepts and inventories this optional tab.
Structural/synthetic checks cover bypass identity, shader compilation, both
square/wide dimensions, reset behavior, safe missing-output fallback, explicit
public prompt/seed mapping, occupied-slot refusal, idempotence, and unmodified
downstream RGB/depth routing. Disposable fixtures are removed after checking.

Real imagery and model inference were explicitly deferred. Fresh provider frames,
dynamic smoothing quality, real version-specific Start/Stop, every single/triple
and stereo visual, source/depth orientation, FPS/latency comparison and thermal
soak remain separate live acceptance gates. A stale/black wall is not a visual
pass. No claim of headset VR acceptance is made. Save a new incremented local TOE
only after reviewing the installed controls; do not overwrite the saved baseline.

All private TOX/TOE files, local paths/settings, model weights, captures, runtime
state and credentials remain ignored and must never be included in GitHub sync.
