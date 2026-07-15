# FlexGPU TouchDesigner scaffold

`bootstrap_project.py` builds a labelled TouchDesigner 2025 integration shell
at `/project1/flexgpu`. It does not contain diffusion, monocular reconstruction,
sensor calibration, a particle solver, projection mapping, or a VR renderer.
Those systems plug into the stable component and WorldBus boundaries created by
the script.

The builder is safe to run in an existing project:

- It creates or updates only `/project1/flexgpu`.
- It never deletes nodes, including unknown children inside `flexgpu`.
- It preserves existing input connections when rerun.
- It does not create an OpenVR TOP or open output windows.
- Saving writes a copy of the complete current TouchDesigner project to the
  requested `.toe` path.

## Build in TouchDesigner

Run the following as one line in the TouchDesigner Textport, changing `root`
to the folder where you cloned this repository:

```python
from pathlib import Path; import sys; root = Path(r'C:\path\to\flexgpu-touchdesigner'); sys.path.insert(0, str(root / 'touchdesigner')); import bootstrap_project as b; b.build(str(root / 'projects' / 'FlexShow.toe'), config_path=None, save=True)
```

To load a JSON profile:

```python
from pathlib import Path; import sys; root = Path(r'C:\path\to\flexgpu-touchdesigner'); sys.path.insert(0, str(root / 'touchdesigner')); import bootstrap_project as b; b.build(str(root / 'projects' / 'FlexShow.toe'), config_path=str(root / 'config' / 'flexshow.json'), save=True)
```

`build(output_path, config_path=None, save=True)` returns the `flexgpu` COMP.
Use `save=False` to build without saving; in that case `output_path` may be
`None`. Warnings are available as `bootstrap_project.LAST_REPORT.warnings` and
in `flexgpu.fetch('bootstrap_report')`.

The module intentionally does nothing when imported or executed until `build`
is called. Run it with TouchDesigner's Python, not a system Python process,
because node creation requires the TouchDesigner API.

## Generated component contracts

| Component | Responsibility |
|---|---|
| `CONFIG` | Build profile, flattened JSON and live runtime state |
| `AI_PIPELINE` | Placeholder producer for generated RGB and generated XYZ |
| `WORLD_CORE` | Sensor ingest, calibration, interactions and persistent point simulation |
| `WORLD_BUS_IN` | Normalizes local or network AI/sensor inputs into stable texture contracts |
| `COMPLETION` | Selects fog, procedural, or hybrid view completion |
| `WORLD_BUS_OUT` | Publishes the one authoritative world to every renderer |
| `INSTALLATION_OUT` | Projection/LED renderer and mapping boundary |
| `VR_OUT` | Stereo PCVR renderer boundary |
| `OPERATOR_DASHBOARD` | Runtime controls, status and a commissioning checklist |
| `STARTUP` | Environment-aware helper module and startup callbacks |

Both output modules consume the same world. Combined mode therefore adds two
camera/render views; it does not create a second simulation.

The WorldBus shell uses four TOP contracts:

1. `generated_rgb`: generated color.
2. `generated_position`: AI-estimated XYZ with valid alpha.
3. `sensor_position`: calibrated metric XYZ with valid alpha.
4. `interaction_field`: force or occupancy data for the world simulation.

The placeholders are Constant/In/Out/Null TOPs so the project opens without
models, sensors, SteamVR, Spout, or third-party Python packages. Replace the
placeholder sources while retaining the named contracts.

## One project, single or dual topology

The same `FlexShow.toe` supports every runtime role:

- `FLEXGPU_ROLE=world` plus `FLEXGPU_TOPOLOGY=single`: one process owns AI,
  sensor/world simulation, and show outputs.
- `FLEXGPU_ROLE=ai` plus `FLEXGPU_TOPOLOGY=dual_local` or `dual_network`: the AI producer process.
- `FLEXGPU_ROLE=world` plus `FLEXGPU_TOPOLOGY=dual_local` or `dual_network`: the sensor/world/show
  process, consuming AI frames through a future transport adapter.
- `FLEXGPU_ROLE=standalone`: compatibility alias that enables AI and world in
  one process.

The startup helper reads these environment variables:

| Variable | Values |
|---|---|
| `FLEXGPU_ROLE` | `standalone`, `world`, `ai` |
| `FLEXGPU_TOPOLOGY` | `single`, `dual_local`, `dual_network` |
| `FLEXGPU_CONFIG` | Path to a runtime JSON profile |
| `FLEXGPU_EXPERIENCE` | `installation`, `vr`, `combined` |
| `FLEXGPU_COMPLETION` | `fog`, `procedural`, `hybrid` |
| `FLEXGPU_TIER` | `3080ti_16gb`, `4090`, `5090` |

Explicit environment values override `FLEXGPU_CONFIG`. The helper updates
declarative `Enabled` parameters and `CONFIG/runtime_state`; it deliberately
does not change `project.cookRate` or destroy/bypass operators. After adding
real networks, use the `Enabled` values to gate cooking.

To reapply environment values manually:

```python
op('/project1/flexgpu/STARTUP/runtime_helpers').module.apply(op('/project1/flexgpu'))
```

## 3080 Ti 16 GB starting limits

The scaffold defaults to the conservative `3080ti_16gb` tier: SD-Turbo-class
AI around 512 square, asynchronous geometry updates, approximately 150,000
points, installation at 60 Hz, and VR at 72 Hz. These are planning defaults,
not measured guarantees. A laptop's thermal/power configuration materially
changes throughput.

For a same-GPU combined run, keep VR as the timing priority, keep queues at one
frame, drop stale AI frames, and target no more than roughly 11-12 GB total use
in `nvidia-smi`. Do not add SDXL, Video Depth Anything, SHARP, Gaussian
reconstruction, expensive shadows, or high MSAA until the actual target system
has ample measured headroom.

## Known limitations

- The scaffold was generated and saved successfully in TouchDesigner
  2025.32820 on the local RTX 3080 Ti Laptop GPU with zero bootstrap warnings.
  Its optional third-party GPU/model adapters remain placeholders until their
  respective packages and devices are installed.
- JSON loading is tolerant and recognizes simple top-level values plus
  `flexgpu`, `runtime`, `show`, and `profile` sections. Every JSON leaf is still
  exposed in `CONFIG/profile_flat` even when it is not a recognized bootstrap
  setting.
- Startup callbacks are best effort because Execute DAT parameter names can
  differ across experimental builds. Manual `runtime_helpers.module.apply(...)`
  is the reliable fallback.
- OpenVR is deliberately absent. Add it only during VR integration because its
  presence can make TouchDesigner follow the headset timing loop.
- The script creates a `.toe` only when run inside TouchDesigner with
  `save=True`. The included `projects/FlexShow.toe` is a generated convenience
  artifact; the human-readable builder remains the source of truth.
