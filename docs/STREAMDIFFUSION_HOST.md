# Single-generator StreamDiffusion front end

The new route needs **one selected generator**, not the old two-generator test.
Normal installation preserves the previous components, but the 3080 working
copy has now explicitly retired that old branch. Its saved pre-deletion TOE is
the rollback. Neither old generator is required for the new route.

```text
choose trusted .tox -> load -> configure / generate / preview OUT_RAW
                                              |
                                   enable Send RGB to 3D
                                              |
                                    optional RGB smoothing
                                              |
                           MoGe-2 OR Depth Anything geometry
                                              |
                            synchronized RGB / depth / mask
                                              |
                         existing point world and wall outputs
```

## Operator workflow

Use the **StreamDiffusion** tab of the main `SHOW_CONTROL`:

Run the TouchDesigner timeline for frame callbacks and dynamic feedback. In the
standalone selected-generator route, the old podcast Play switch no longer
forces the global timeline to pause. Legacy mode or Follow Podcast retains the
podcast's original timeline ownership; audio controls remain unchanged.

1. Keep **Pipeline Source = Selected Generator**. Choose **Trusted Local
   StreamDiffusion .tox**, then press **Load Selected Generator**. Selecting a
   filename alone does not load it. Loading a TOX executes its own code and may
   trigger that version's startup behavior: use trusted local files only.
2. Press **Open Generator Controls**, configure that version and run generation
   there. Set **Public RGB TOP Relative to Component** to its documented output
   (for example `out1`) and inspect `STREAMDIFFUSION_HOST/OUT_RAW`.
   **Generation Preview (Before 3D)** opens that raw output in a reusable,
   bordered floating viewer. It works while Send RGB to 3D is off; opening a
   preview neither starts the generator nor connects a geometry worker.
   Alternatively, map its top-level custom Start/Stop pulse parameter names and
   use the host's buttons. Names are version-specific; no private API is guessed
   or inspected. Start dispatch does not itself connect the stream.
3. After the preview is good, enable **Send RGB to 3D** and start your selected
   geometry worker using the existing worker controls. Both geometry bridges
   share the same RGB boundary; only the chosen provider is needed.

**Pipeline Stage** distinguishes `select_generator`, `configure_rgb`, `preview`
and `connected`. Connected means RGB delivery is enabled, not that model
inference is proven healthy. Check the geometry bridge for fresh synchronized
results. The host does not fabricate source FPS or model-server readiness.

Turning delivery off closes the RGB gate. Geometry listeners remain available,
but capture/publication is paused; black placeholder frames are not sent for
inference. Results from a previous connection cannot become valid on reconnect.
Changing source, file selection, payload or RGB mapping disconnects delivery.
Cold reopen also starts disconnected. Explicitly reconnect after previewing.
This does not automatically launch or stop geometry workers.

**Offload Generator (Stop Server)** disconnects RGB, requests the selected
generator's public Stop Server pulse, and waits asynchronously for its public
Server Active and Stream Active flags to turn off. The `.tox`, its settings,
noise input and files stay loaded for restarting. It does not delete the
component, force-kill a process, stop either geometry worker, or start a model.
Offload on an already-inactive server is harmless and sends no extra stop pulse.

The host recognizes the verified public StreamDiffusionTD contract
(`Startstream` / Start Stream, `Stopstream` / Stop Server, and the two custom
activity toggles) and fills only empty Start/Stop mappings. Explicit manual
mappings are preserved; unrelated/unknown versions are not guessed. The regular
Stop button uses the same offload path for this known contract. For another
version, configure its public Stop Server mapping deliberately.

If TD is paused, shutdown callbacks may not execute. Offload temporarily runs
callbacks, then restores the prior paused playback/frame after confirmation or
a 20-second confirmation timeout. It does not block TD's main thread while
waiting. **Offload Status** distinguishes `offloading`, `offloaded` and
`offload_unconfirmed`; missing activity flags or an unsuccessful stop never
produce a false success claim. Offloaded means the public flags report inactive,
not an independent VRAM measurement. Delivery stays blocked and the generation
preview goes blank until restart; the Noise Input Preview remains available.

A deliberate standalone Start resumes TD playback for video callbacks, while
leaving RGB-to-3D delivery off. Native manual restart is also recognized after a
confirmed offload. Podcast playback/audio are not automatically started.
Disconnection or RGB selection alone does not unload a model or free its VRAM;
use Stop Server before another version on the 16 GB GPU. Unsupported mappings
report an error rather than deleting the component or terminating unrelated
processes. Public control reference:
[StreamDiffusionTD parameters](https://dotsimulate.com/docs/streamdiffusiontd/parameters).

## Version replacement and rollback

### Generator noise input

The host now includes `INPUT_NOISE -> GENERATOR_INPUT -> generator public TOP
input 0`. **Feed Noise to Generator Input 0** defaults on. Noise input width and
height default to 512×512, with a separate seed control; `OUT_INPUT` previews the
exact input. These values do not set the generator's output resolution or the
1920×1080 wall resolution. Noise is static by default, like the former test
source; change its seed deliberately rather than adding unwanted temporal jitter.

**Noise Controls (Native TOP Window)** opens the actual `INPUT_NOISE` parameter
dialog, including its regular Noise, Transform, Output and Common pages—not a
limited replica. **Noise Input Preview** opens a reusable viewer for `OUT_INPUT`.
The quick width/height/integer-seed controls synchronize with the native TOP;
native fractional seeds, custom expressions and other native settings remain
authoritative and are preserved on reinstall. Editing a quick size control
explicitly selects Custom output resolution. Native settings beyond the quick
controls' bounds are not silently clamped back to the convenience readouts.
The installer detaches only its previous width/height/seed link expressions,
leaving user-authored expressions intact. Unchanged geometry runtimes are not
restarted just to refresh these UI controls.

Loading or directly dropping a generator automatically wires its public input.
The host refuses to replace another existing input. Disable Feed Noise for a
version that has no public TOP input or is configured for a different source.
Only public component connectors are used; no internal input nodes are searched.

### Explicitly retiring the old branch

The 3080 working copy can retire the old two generators and their duplicated
noise/color/fade/Spout test branch using the reviewed
`touchdesigner/retire_legacy_streamdiffusion.py` helper. This is never called by a
normal install: it requires explicit confirmation and an existing, local 3080
TOE backup. It checks an exact name/type allowlist and rejects unrelated
downstream consumers. Docked callback DATs that TD removes with their owner are
handled without stale operator references.

After retirement, the host has no legacy input and its source menu contains
only Selected Generator. Reinstall remains supported and does not resurrect the
old branch. Podcast audio, sequencing, caption/prompt tables, geometry workers,
point-cloud grading, interactions and wall renderers remain. Public controller
hooks stop targeting the removed two renderers; obsolete per-source color
controls are disabled. Use the main Show Control Color Adjustment tab for the
point cloud. Original external home-podcast files and all 5090 local files are
unchanged. The saved pre-deletion TOE is the rollback, not hidden live generators.
Controller construction also adapts the exact reviewed public two-TOP startup
check so cold construction does not demand the removed rig. A changed/unknown
constructor is refused for review; scene-execution and human-visual approval
checks are retained and tested. No placeholder rig is recreated to fake readiness.

`PAYLOAD_SLOT` starts empty and loads one trusted TOX. Directly dropping a COMP
is also supported; a sole child is selected automatically. An explicit component
name must resolve to a direct child of this slot; the RGB mapping must stay
inside it. The host does not enumerate private internals.

The loader refuses an occupied slot: stop and preserve/remove the previous
version deliberately before loading another. It never silently replaces or
deletes private contents. This is a single active-generator workflow, not an
automatic A/B switcher.

For copies that have not retired the old branch, **Legacy Podcast (Rollback)**
selects the original accepted RGB source and also
requires **Send RGB to 3D**. A missing replacement never silently falls back to
a cached old image. Existing generators and the dual-stream podcast compositor
are preserved but bypassed in Selected Generator mode. Selecting another route
does not automatically stop their external servers.

Enable **Follow Audio / Podcast Prompts** to forward the adapter's public
`prompt_out` table to the selected generator. **Public Prompt Format = auto**
detects the documented StreamDiffusionTD `Promptdict` / `Seeddict` sequences and
writes both audio-crossfade layers, their normalized weights and paired seeds
into that **one** generator. Native prompt-weight normalization/interpolation
settings are preserved. This blends prompt embeddings, not two rendered videos.
Other generator versions can use **simple** format with explicitly mapped
custom Prompt / Seed fields; that fallback uses only the greatest-weight layer.

**Audio Prompt Sync Status** confirms the handoff or explains a missing public
parameter/schema. Forwarding works during generation preview, independently of
**Send RGB to 3D**, and never starts or restarts generation. Follow defaults off
for manual prompts. Invalid tables or all-zero weights hold the previous safe
prompt; active updates are read back, with rollback on write failure. At most
two layers are accepted. Seeds use the existing signed-Int-compatible mapping
(`-1` remains random; nonnegative seeds are reduced modulo 2147483647).
No prompt text is written to validation logs. This route does not modify Noise
TOP seed, audio playback, scene selection, or any 5090 local profile.

## Smoothing and performance

Smoothing defaults off. The native-resolution RGBA16F feedback blend uses a
50 ms response adjustable from 0–250 ms. History resets on source/scene/seed,
dimensions, delivery connection and smoothing changes. Timing follows actual
filter cooks; extra inference frames are not forced. Both providers see the
same filtered RGB before depth estimation, never color-only smoothing after it.
**Generation Preview / OUT_RAW is intentionally unfiltered.** If **Send RGB to
3D** is off and nothing requests `OUTPUT_ROUTE`, the filtered branch can remain
idle; its last-cook frame and history weight do not prove a fault. Preview the
public filtered `OUTPUT_ROUTE` or connect delivery to evaluate smoothing. The
native RGB resolution is preserved; wall output resolution is independent.

This can reduce flicker, but does not increase generation throughput or create
true intermediate motion. Strong settings blur details, ghost moving subjects
and add delay. No Engine COMP is installed. Engine can isolate blocking CPU/TD
work but does not add GPU compute or VRAM; benchmark the exact component first.

## Install and validate

Public source: `touchdesigner/streamdiffusion_host.py`, its import-free embedded
runtime `touchdesigner/streamdiffusion_host_runtime.py`, and the optional source
gate in `touchdesigner/moge2_bridge_runtime.py`. Keep 3080 and 5090 local
TOEs/profiles separate. Back up the saved working copy, then run inside TD:

```python
from pathlib import Path
import importlib, sys
root = Path(project.folder).parent
sys.path.insert(0, str(root / 'touchdesigner'))
import streamdiffusion_host as host
importlib.reload(host)
host.install(op('/project1/flexgpu'))
```

The installer preserves the legacy input and redirects the two geometry input
wires through `STREAMDIFFUSION_HOST/OUT_RGB`. It adds bridge source gates and
refreshes only their public runtime DATs, preserving provider ports/profiles,
renderer, interaction and synchronized adapter outputs. A narrow public podcast
callback guard removes its timeline authority only in standalone generator mode;
the original callback is retained in host storage for rollback. First migration selects
the new generator route with delivery off. Reinstallation preserves values and
loaded payloads. No private component is loaded, moved, deleted, externalized or
started by installation, and no TOE is saved. The main builder repairs an
already-installed host; fresh synthetic builds retain the original layout until
this optional upgrade is requested.

```powershell
& $env:FLEXSHOW_PYTHON -m unittest tests.test_streamdiffusion_host tests.test_moge2_bridge_runtime -v
.\scripts\Test-FlexShowRelease.ps1
```

Use an approved source-test interpreter with the exact `requirements-test.txt`
pins, not an active AI-worker environment. Read-only TD structural gate:

```python
import validate_streamdiffusion_host as validation
print(validation.validate(op('/project1/flexgpu'),
    report_path=root/'runtime'/'validation'/'streamdiffusion-host-3080.json'))
```

This gate is read-only: it checks feedback/preview/gate wiring, bounded history
weight, route selection, shared provider input and Show Control value mirrors.
It fails cleanly for absent/partial hosts and reports generator buttons as
`external_validation_required`; a structural pass is not a generation test.
It never inspects private descendants or pulses native controls. Reports and
prompt/capture data remain local. The full, mutating `validate_show_controls`
audit requires audio prompt following off first, and its PowerShell report
checker now verifies the optional host's value, pulse **and status** inventory.

Tests cover explicit delivery, safe mapped pulses, cold-start disconnection,
missing-output blocking, epoch isolation, listener preservation, square/wide
dimensions, smoothing and unchanged downstream routes. Structural or synthetic
success is not real-imagery acceptance. The 3080 `.45` front end was checked live
on November 10: real generation preview, automatic weighted audio cue changes,
and 120 filtered frames with finite pixels and confirmed previous-frame
blending. Smoothing was on at 50 ms; the 3D delivery switch stayed off. Temporary
probes/captures were removed and the selected generator/audio settings were
preserved. This does not validate every native button of every `.tox` version.
For the following source/publication audit TouchDesigner was closed, and the
user deferred fresh two-provider, single/triple-wall and stereo visuals.
Native button compatibility, FPS/latency, physical inputs and thermal soak
remain separate live gates.
Desktop stereo does not prove headset VR readiness.

Review and save a new incremented local TOE when accepted. Do not overwrite the
saved baseline. Private TOX/TOE files, models, local settings, paths, captures,
runtime state and credentials remain ignored and must never enter GitHub.
