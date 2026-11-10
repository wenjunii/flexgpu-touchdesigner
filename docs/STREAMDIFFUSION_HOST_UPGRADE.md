# Upgrade an existing 3080, 4090 or 5090 project safely

Sync **public code**, not a machine image. Keep the accepted working TOE on
each destination and install only the RGB host update there. Do not replace a
4090/5090 TOE with the 3080 `.46`, or run the full world builder to get this UI.
This procedure does not reinstall or upgrade any model environment.

| Machine tag in TOE/config names | Quality/worker profile |
| --- | --- |
| `3080` (or `3080ti`) | `3080ti_16gb` |
| `4090` | `4090` |
| `5090` | `5090` |

Local TOEs, private/paid TOXs, weights, GPU UUIDs, config/local JSON files,
runtime state, calibration, media, credentials and model environments stay
ignored. Transfer private assets separately only if needed; never publish them.
The 3080 source checkpoint is `.46`, saved with an empty/offloaded generator
slot. It is not a destination template. No 4090/5090 live acceptance is implied
by source tests of their identity guards.

## 1. Prepare on each destination later

1. Stop generation through the generator's native **Stop Server** control.
   Both public `Serveractive` and `Streamactive` flags must be OFF. Stop that
   checkout's geometry worker normally. Stop other local model servers too;
   the upgrade cannot prove that external processes released VRAM.
2. Save the accepted destination TOE. Keep that file as rollback and use native
   **Save As** to make a *new*, machine-tagged working TOE in the same folder.
   Do not overwrite the rollback. Use the actual destination's latest file,
   not a historical checkpoint named in another machine's documentation.
3. With a clean Git checkout, update the shared source:

   ```powershell
   git status --short
   git fetch origin --prune
   git switch main
   git pull --ff-only origin main
   ```

   If there are local source edits or conflicts, stop and reconcile them first.
   Do not reset, force-pull, or copy another machine's ignored config over them.
4. Retarget **only that destination's** local config to the new working TOE
   using `Set-FlexShowLocalProject.ps1 -ExpectedTier ... -WhatIf`; review it,
   then repeat without `-WhatIf`. Stop an old managed runtime first if it still
   owns the active process. Do not delete its manifest to bypass ownership.

## 2. Read-only file/config preflight

Example **for the 4090 only**; replace both TOE names with your real files:

```powershell
.\scripts\Test-StreamDiffusionHostUpgrade.ps1 `
  -Config .\config\local-4090.json `
  -Project .\projects\FlexShow-4090-host-upgrade.toe `
  -Backup .\projects\FlexShow-4090-accepted.toe `
  -ExpectedTier 4090
```

For the 5090 use its own files and `-ExpectedTier 5090`; for the laptop use
`-ExpectedTier 3080ti_16gb`. This script has **no write mode**. It checks ignored
repository-local files, distinct same-folder saved TOEs, matching machine tags,
config tier and current launch target. It hashes the rollback but does not open
TD or validate the physical GPU, private generator, or visual quality.

## 3. Targeted live preflight and install

Open the new working TOE, not the rollback. In TD Textport, use this setup
(change `machine` and `backup` to the destination's actual values):

```python
from pathlib import Path
import importlib, sys
root = Path(project.folder).parent
for path in (root / 'src', root / 'touchdesigner'):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
import runtime_pipeline as rp
import streamdiffusion_host as sh
import streamdiffusion_host_runtime as sr
import upgrade_streamdiffusion_host as u
for module in (rp, sh, sr, u):
    importlib.reload(module)
machine = '4090'
backup = Path(project.folder) / 'FlexShow-4090-accepted.toe'
print(u.upgrade(op('/project1/flexgpu'), machine, backup))
```

The default is read-only. It requires the correct root, matching TOE/rollback
tags, the matching Show Control and **both** geometry bridge profiles, and
inactive known legacy/slot generators with the verified public flag contract.
Unknown activity flags fail closed: stop/review that version manually first.

After `preflight_pass`, apply deliberately:

```python
print(u.upgrade(op('/project1/flexgpu'), machine, backup, apply=True))
```

This calls only the optional host installer. It adds/refreshes the host,
StreamDiffusion-tab controls and the two public geometry source gates/runtimes.
It does **not** call `runtime_pipeline.build()`, load/start a TOX or worker,
delete a generator, change a local config, or save the TOE. RGB delivery is
deliberately OFF afterward. Existing host/noise settings are retained by the
builder; unrelated Show Control values, public component-root settings and
geometry profile/capture controls are fingerprinted before/after. Reports
contain hashes, not private paths or prompts. This fingerprint is a bounded
settings check, not an audit of every operator or private component.

If installation or preservation checks fail, **do not save**. The operation is
not a transaction: reopen the accepted rollback to undo partial changes.

The historical two-generator branch may still exist on a destination. Normal
installation preserves it and leaves servers stopped. The destructive legacy
retirement helper is **3080-only** and must not be used on the 4090/5090.
Review their legacy branch separately before deleting anything. Offload's
optional selected-slot removal defaults OFF; enable it only after accepting a
same-machine saved rollback. Stop always retains the loaded component.

## 4. Destination acceptance before saving

1. Run `validate_streamdiffusion_host.validate(op('/project1/flexgpu'))` after
   importing that module; require `status == 'pass'`. Check recursive errors.
2. Select a trusted local `.tox` and load only into an empty slot. If the slot
   already has a generator, stop it and review the version-replacement workflow
   in [STREAMDIFFUSION_HOST.md](STREAMDIFFUSION_HOST.md). Never silently replace it.
3. Generate and inspect **Generation Preview (Before 3D)**. Verify the selected
   version, public RGB output, noise input, audio prompt forwarding if used,
   and smoothing OFF/ON. Smoothing blends frames; it adds latency/ghosting and
   does not increase generator inference FPS.
4. Enable **Send RGB to 3D**. Select one provider and start only its own
   machine-profile worker. Confirm fresh synchronized RGB/depth results.
   A `ready` worker with RGB delivery OFF is not a connected pipeline.
5. Check RGB orientation, position/depth validity, single installation,
   panoramic left/center/right, artistic left/center/right, then desktop stereo.
   The seven individual outputs remain 1920×1080; both triple mosaics 5760×1080.
   Reject stale/blank output, black rectangular sprites, inverted imagery,
   stretched duplicate plates or identical wrap views.
6. Test version selection, Stop, Offload and reload with that version's public
   contract. Check FPS/VRAM/temperatures and perform a sustained thermal soak.
   Desktop stereo is not completed Quest/OpenXR VR acceptance.
7. Save a **new machine-tagged checkpoint** only after acceptance; retarget only
   that machine's local launcher. Keep the accepted rollback until then.

No GitHub workflow dispatch is needed for this local migration. Ordinary PR
checks verify source/publication safety, not private TOEs or destination GPUs.
