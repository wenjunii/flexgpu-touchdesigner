# Show Readiness

`SHOW_CONTROL → Readiness` is a passive preflight for a **local installation**
(single, panoramic, or artistic walls). Use it to tell a generator preview
from a running 3D feed and to find the next check when a wall is blank or
frozen. It does not start or stop generation, change a route, force a render,
or verify the physical projector, audio device, image quality or headset.

| Readout | What it checks |
| --- | --- |
| Show Readiness / Next action | Live RGB/depth routing, generator gate, current geometry and active wall feeds; shows the first condition that needs attention. |
| Generator | The loaded host's public activity flags. A running server by itself is not streaming. |
| Geometry | The actual selected provider and bridge state. `Live` requires a fresh, uploaded atlas whose source frame ID advances and reaches the world. |
| Active wall TOPs | Cooked dimensions for the active layout against the configured width and height. All three feeds must be observed for a triple layout. |
| Audio / Interaction | Audio routing and depth-sensor validity, not listening or physical interaction acceptance. Intentional mute and disabled interaction are allowed. |

The first sample cannot establish progress. Let the selected wall outputs cook
and wait a few samples. Uncooked TOPs have unknown dimensions. A stale sample
shows `Observer not current`, or `Timeline paused — not live` while paused.
Resume the timeline or explicitly sample the observer. Split transport and
VR-only are outside this check. `Live` is a metadata check, not a visual verdict.

## Install in a separate machine-local working copy

If your machine-local rehearsal TOE already has the tab (for example, 3080
`.48`), skip installation. Otherwise preserve the accepted TOE and its launch
config. Open the accepted TOE in TouchDesigner and use **Save As** with a new
staging basename in the same `projects/` folder, such as
`FlexShow-readiness-stage-local-3080.toe`. A filesystem copy alone can reopen
under the original project name. Confirm the title bar shows the staging name
before installing. Keep stage names separate from any unnumbered TOE the
launcher uses; TouchDesigner may also refresh that companion on a numbered
Save As.

In the Textport, import the helper and locate the FlexGPU root (assuming the
working TOE is in the repo's `projects/` folder):

```python
from pathlib import Path
import sys
repo = Path(project.folder).parent
tdpath = str(repo / 'touchdesigner')
sys.path.insert(0, tdpath) if tdpath not in sys.path else None
import show_readiness as rd
root = op('/project1/flexgpu')
```

For a new 3080 staging copy, run the installer once, using the actual accepted
rollback file for your checkout:

```python
rd.install(root, expected_profile='3080ti_16gb', backup_path=repo / 'projects' / 'FlexShow-moge2-embody-podcast-local-3080.47.toe')
```

Paste into the Textport as individual statements (or wrap the complete snippet
in `exec(...)`). The install requires a distinct same-folder saved rollback
whose machine tag matches the live profile. It never saves the project. Open
the **main** Show Control under `WORKING_PIPELINE` (not the nested podcast
controls). Inspect the panel, then Save As a new 3080 version if accepted. Do
not copy that TOE or its local paths to a 4090/5090. On either workstation,
install from its own checkout against its own working copy and rollback using
its own profile (`4090` or `5090`). `rd.refresh(root)` takes a passive sample
while troubleshooting.

## Prepare a later 5090 update

For preparation only, review the source commit and run
`.\scripts\Test-FlexShowRelease.ps1 -Mode Quick` on the current checkout. It
includes the named staging guards and simulated 5090 readiness/recovery for
both providers and all three wall modes. It does not require or test the GPU.

When the 5090 update is authorized, fetch the reviewed source into the 5090's
own checkout. Fast-forward a clean compatible branch; stop for review if
there is local work or the branch has diverged. Preserve the accepted 5090
TOE and launcher; do not copy the 3080 `.48`, configs, or private inputs.

On the 5090, use native **Save As** to make a separate, same-folder staging
TOE with `5090` in its name. Leave audio muted and generation stopped. Use the
Textport import snippet above, then substitute the exact staging basename and
the actual accepted 5090 rollback file:

```python
options = dict(expected_profile='5090',
               working_name='FlexShow-readiness-stage-local-5090.toe',
               backup_path=Path(project.folder) / 'FlexShow-accepted-local-5090.toe')
print(rd.preview_install(root, **options))
```

The preview checks the title's project identity, same-folder rollback, main
Show Control profile, existing generator host, both geometry bridge profiles,
and Readiness ownership. It cannot validate the physical GPU, license, or
wall output. A rejection is a stop signal, not a reason to retune the project
just to pass. The preview does not create operators, start workers, or save.

Only when ready to install the tab, run `rd.install(root, **options)` with
the **same** options. The installer rechecks the guards. Save to a new 5090
candidate, cold-reopen it, and verify the seven read-only fields, recursive
errors/warnings, and the selected wall TOP sizes. Rehearse the real feed and
recovery separately before asking to retarget the 5090 launcher. No source
sync or install preview promotes the candidate automatically.

## Rehearse a show and a recovery

1. Select one geometry provider and the intended layout. Check wall width and
   height (normally 1920 × 1080 per wall). Start the trusted local generator
   in its native controls and inspect **Generation Preview**. Start only the
   selected geometry worker using its existing control. Enable **Send RGB
   to 3D** when the preview is good. Check the Readiness rows, then inspect
   the wall images: direction and perspective, distinct side views, usable
   seams, and no blank/stale feed or unintended duplicate plate.
2. With the show feed stable, import `rd` and set `root` in the current
   Textport session using the first snippet above. Start a bounded metadata
   sample without reinstalling the panel:

   ```python
   rd.start_rehearsal(root, seconds=120)
   ```

   Close the Textport and let the show run. At the end (or to finish early):

   ```python
   rd.save_rehearsal(root, 'runtime/validation/rehearsal-moge2-3080.json')
   ```

   Use a new filename per run. No files are written until export. Sampling is
   at most once a second for 2–3,600 seconds; the recorder captures readiness
   states, frame IDs, result age and existing heartbeat frame timing. The p95s
   describe sampled data, not GPU inference latency. No images, prompts,
   audio, sensor frames, private paths or credentials are recorded.
3. During a **supervised rehearsal**, stop the selected geometry worker with
   the existing control. Check that `Live` clears and the world fails closed;
   check the rehearsed fallback cue yourself. Keep a reviewed still or demo
   cue ready to select manually. Restart only that worker when ready and
   verify the frame ID advances and the wall content recovers. Nothing in the
   Readiness tab switches feeds for you. Stop that worker before rehearsing
   the other provider; use a separate report.
4. After visual and recovery checks, run a supervised thermal soak on the
   real workload. If desired, check the intended GPU index with `nvidia-smi`
   and monitor temperature, memory and utilization locally (replace `0` if
   necessary; Ctrl+C to stop):

   ```powershell
   nvidia-smi --query-gpu=index,name --format=csv
   nvidia-smi -i 0 --query-gpu=timestamp,temperature.gpu,memory.used,utilization.gpu --format=csv -l 2
   ```

Only promote a rehearsal copy to the launcher once it has passed the user's
visual and recovery checks. Keep TOEs, calibration, recordings and rehearsal
reports out of Git.
