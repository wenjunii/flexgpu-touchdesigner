# 5090 podcast receiver parity and control audit

This is a bounded receiver migration, not a machine-image or TOE replacement.
The accepted 3080 `.46` is a read-only reference for public playback behavior.
Keep each machine's GPU tier, working TOE, camera/particle settings, profiles,
ports, sensor calibration, components, media and local paths separate.

## Preconditions and scope

- Read `AGENTS.md`, inspect/preserve Git changes and fast-forward shared source
  without resetting local work. Do not overwrite any other machine's config.
- Confirm the physical RTX 5090 and the destination's actual latest accepted
  TOE. Save an ignored same-folder, 5090-tagged rollback and a distinct working
  copy. Stop generation through native Stop Server and stop geometry normally.
- Discover the live root through the project-scoped MCP context and claim the
  managed scope when available. Verify `/project1/flexgpu` before editing.
  Do not change shared MCP settings or externalize private component internals.
- Main Show Control and **both** geometry bridge profiles must be `5090`.
  The reviewed home-podcast data/helpers and stereo 48 kHz audio must exist
  locally. Do not run any 3080-only installer or retirement helper here.

The reviewed edition has 136 stories, 619 scenes and 2,862 captions per visual
plan, three visual paths, three audio modes and an 11,938.720-second timeline.
Images hold through gaps; scene/story interpolation defaults to three seconds.
The cached playback profile is called `3080_safe` even on this receiver. It is
not a GPU tier: live variations/regeneration remain disabled, while FlexGPU's
5090 inference/render settings stay separate.

## Dry-run and explicit installation

In the stopped 5090 working project, after adding this checkout's `src` and
`touchdesigner` directories to `sys.path`:

```python
from pathlib import Path
import importlib
import sync_combined_podcast_5090 as migration
importlib.reload(migration)
repo = Path(project.folder).parent
backup = Path(project.folder) / 'FlexShow-podcast-local-5090-accepted.toe'
podcast = repo.parent / 'home-podcast'
print(migration.upgrade(op('/project1/flexgpu'), podcast, backup))
```

The default is read-only; require `preflight_pass`. It verifies destination
tags/profiles, saved rollback, inactive public server flags, selected host slot,
reviewed plan hashes/counts and WAV headers, and known public callback contracts.

After reviewing/approving the exact legacy targets, apply deliberately:

```python
print(migration.upgrade(op('/project1/flexgpu'), podcast, backup,
                        apply=True, confirm_legacy=True))
```

`confirm_legacy` is not implicit permission to delete anything. Review first.
The only eligible deletions are the adapter-level `StreamDiffusionTD`,
`StreamDiffusionTD4` and their two verified docked callback DATs. Their native
flags must be inactive and outgoing consumers must match the reviewed public
legacy chains. The selected generator inside the managed host is retained.
Public old test TOPs are retained as previews of that single host, with no
feedback into its slot-only RGB route. The rollback TOE recovers the retired
components; source TOXs and models are not deleted.

The helper extends public audio/playlist controls in place, retargets only the
reviewed media/helper paths to the **local** checkout, installs the targeted
host, and refreshes the known public startup
resolution restore while retaining the explicit `5090` tier override.
It also patches only the recognized public runtime apply/tick initialization
to retain saved geometry-provider and display-mode selections. This covers
first-tick initialization before Execute DAT startup. Local source/render
configuration fields are merged, not replaced; an absent source section stays
absent so manual adapter routing is not reset to demo. Explicit builder calls
retain their deterministic defaults. Unknown callback/preamble code is refused.
It does not run `runtime_pipeline.build()`, install weights, start models,
enable Engine COMP, alter local launchers or save a TOE. If preservation or
installation fails, do not save; return to the accepted rollback.

## Audit and acceptance

With generation stopped, audio muted/paused, following OFF and RGB disconnected:

```python
import validate_show_controls as controls
import validate_podcast_5090 as playlist
import audit_streamdiffusion_controls as host_controls
root = op('/project1/flexgpu')
print(controls.validate(expected_profile='5090', operator_lookup=op,
                        allow_absent_optional=True)['summary'])
print(playlist.validate(root, podcast, lookup=op))
print(host_controls.validate(root))
```

Use `allow_absent_optional=True` only for wholly absent known features in an
older destination. It does not excuse partial installations or install VR/Femto
features merely to satisfy a checklist. Current 5090 control evidence includes
241 installed mapping/dimension checks, 1,897 muted playlist checks and 31 safe
host mapping checks. Exact accepted camera modes/expressions/bindings are
restored, not replaced by evaluated samples. Keep raw JSON/captures ignored.

Test native UI buttons asynchronously so TD can keep cooking. Verify Play/Pause,
story navigation, Restart, Reload and New Seeds; generator/noise previews and
controls; invalid-file refusal; native Start/Stop; confirmed Offload **without
removal**; and smoothing/RGB delivery OFF/ON. Confirm public audio prompt/seed
weights match the public podcast table without logging prompt text. Do not
claim real TOX version replacement or private component removal from a safe
mapping/guard test. Do not delete/reload a working paid generator just for audit
coverage. Those remain explicit version-specific acceptance steps.

Start MoGe-2 and Depth Anything separately with this machine's `5090` profile.
Verify checkout-owned processes, duplicate-start refusal, native Stop, fresh
synchronized frame IDs and finite position data. Compare source/position, the
installation and all six wall outputs: seven surfaces at 1920x1080, two mosaics
at 5760x1080. Inspect orientation, real depth/parallax and distinct camera views;
reject blank/stale/placeholder output. Physical sensor tests require connected
hardware. A disabled desktop stereo texture is not headset VR acceptance, and
short runs do not replace thermal/venue soak tests.

The 2026-11-11 local acceptance was saved as
`FlexShow-moge2-embody-podcast-local-5090.90.toe` and reopened in the same TD
process. All audited public values and all nine camera parameter states
matched the pre-audit snapshot. Old registered receivers stopped; replacement
receivers bound without errors. The recursive operator-error scan was empty.
The local launcher changed only `processes.world.project`; 3080 configs and
accepted `.64`, `.80` and `.81` rollback hashes were unchanged.

Both geometry providers produced changing synchronized frames using `5090`,
tested one at a time. Samples were approximately 15 ms (Depth Anything) and
47 ms (MoGe-2) old, with finite position data. Generator public status sampled
about 29 FPS; 15 FPS was the geometry capture target, not a measured sustained
throughput guarantee. A short GPU sample was about 13.9/32.6 GB VRAM and 76 C.
All inference was stopped after testing. Audio file decoding/routing and
public prompt/seed readback passed; audible listening remains a user test.
Stereo was disabled and therefore not visually accepted. Real TOX replacement
or removal, connected webcam/Femto interaction and thermal/venue soak remain
unverified. No model environment, private internals or other machine's TOE
was copied or changed.

## Receiver reload safety

The public geometry runtime uses a process-local registry keyed by the embedded
checkout and approved bridge operator path. It holds transport objects only,
not TD operators. After an in-process TOE/DAT reload, the new instance closes
only its registered predecessor before binding. Normal Stop removes its own
entry by object identity; an old module cannot unregister its replacement.
Other checkouts, operator roots and unknown port owners are never stopped.

An existing saved TOE does not acquire this receiver fix merely by pulling Git
or running the podcast upgrader. In the stopped, backed-up 5090 working copy,
the audit separately refreshed only the two public `bridge_runtime` DATs from
the reviewed `moge2_bridge_runtime.py` source, stopping each old runtime first.
It did not rebuild the world or change bridge profiles/ports. Validate recursive
errors and fresh synchronized results for both providers before saving; then
reopen and confirm the old registered transports are stopped and the new ones
bind without errors.

If an older, unregistered runtime still owns a return port, do not kill TD or
change ports blindly. Preserve unsaved work, stop generation/workers normally,
inspect the owning process and approved bridge scope, then recover only the
confirmed stale owned runtime or restart TD normally. Always stop generation
and workers before changing projects, even with the reload guard installed.

After acceptance, save a **new 5090-tagged TOE** and retarget only the 5090
launcher with `Set-FlexShowLocalProject.ps1 -ExpectedTier 5090`. Keep rollbacks.
TOEs, local configs, private components/media, calibration, runtime JSON,
captures and model environments remain outside Git. A local migration alone
does not require a PR or manually dispatched workflow; public script/doc
changes follow normal source/publication checks and PR review.
