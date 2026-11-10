# Single-generator StreamDiffusion front end

The new route needs **one selected generator**, not the old two-generator test.
The previous podcast/private components remain untouched as an explicit rollback
source; they are not required for the new route.

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

**Stop Generator / Disconnect RGB** disconnects first, even if its Stop mapping
is missing or invalid. Only a successful mapped pulse requests the generator to
stop. Disconnection, RGB selection and disabling TD cooking do not unload an
external model or free its VRAM. Stop/unload old servers before running another
version on the 16 GB GPU.

## Version replacement and rollback

`PAYLOAD_SLOT` starts empty and loads one trusted TOX. Directly dropping a COMP
is also supported; a sole child is selected automatically. An explicit component
name must resolve to a direct child of this slot; the RGB mapping must stay
inside it. The host does not enumerate private internals.

The loader refuses an occupied slot: stop and preserve/remove the previous
version deliberately before loading another. It never silently replaces or
deletes private contents. This is a single active-generator workflow, not an
automatic A/B switcher.

**Legacy Podcast (Rollback)** selects the original accepted RGB source and also
requires **Send RGB to 3D**. A missing replacement never silently falls back to
a cached old image. Existing generators and the dual-stream podcast compositor
are preserved but bypassed in Selected Generator mode. Selecting another route
does not automatically stop their external servers.

Optional **Follow Podcast Dominant Prompt / Seed** forwards public `prompt_out`
data to explicitly mapped custom parameters on the single generator. It chooses
the row with greatest weight; it does not reproduce the old two-generator
crossfade. Leave it off for manual prompts or native controls.

## Smoothing and performance

Smoothing defaults off. The native-resolution RGBA16F feedback blend uses a
50 ms response adjustable from 0–250 ms. History resets on source/scene/seed,
dimensions, delivery connection and smoothing changes. Timing follows actual
filter cooks; extra inference frames are not forced. Both providers see the
same filtered RGB before depth estimation, never color-only smoothing after it.

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
print(validation.validate(op('/project1/flexgpu')))
```

Tests cover explicit delivery, safe mapped pulses, cold-start disconnection,
missing-output blocking, epoch isolation, listener preservation, square/wide
dimensions, smoothing and unchanged downstream routes. Structural or synthetic
success is not real-imagery acceptance. The user deferred real generation for
this upgrade. Native Start/Stop behavior, dynamic smoothing, both providers,
single/triple walls, stereo, FPS/latency and thermal soak remain live gates.
Desktop stereo does not prove headset VR readiness.

Review and save a new incremented local TOE when accepted. Do not overwrite the
saved baseline. Private TOX/TOE files, models, local settings, paths, captures,
runtime state and credentials remain ignored and must never enter GitHub.
