# FlexGPU TouchDesigner world scaffold

This repository is a deployment and TouchDesigner starter scaffold for a
real-time generated point world.  It supports:

- NVIDIA RTX 3080 Ti Laptop 16 GB, RTX 4090 24 GB, and RTX 5090 32 GB tiers.
- One GPU, two GPUs in one Windows computer, or two networked computers.
- Installation, VR, and combined experience modes.
- Thick-points/fog, procedural backfill, and hybrid completion modes.
- A non-blocking AI pipeline: audience interaction and rendering continue when
  a diffusion or depth frame is late.

It does **not** bundle StreamDiffusionTD, a depth sensor SDK, a headset runtime,
or model weights.  Their locations are configured as adapters so the same show
network can move between machines. WorldBus networking, particle simulation,
geometry completion, projection mapping, and VR are currently documented
adapter contracts rather than production implementations.

## Prerequisites

- Windows 10/11 with PowerShell 5.1 or newer.
- An NVIDIA driver that provides `nvidia-smi`.
- TouchDesigner 2025; the included project was generated with build 2025.32820.
- Python 3.10 or newer, or TouchDesigner's bundled Python runtime.

## What is included

```text
config/             validated show profiles and quality presets
docs/               architecture and WorldBus protocol
scripts/            one-click Windows start, stop, and diagnosis
src/flexgpu/         dependency-free GPU discovery and process planner
tests/               planner/configuration tests
tools/flexgpu.py     command-line entry point
projects/            generated and validated FlexShow.toe starter
touchdesigner/       TD 2025 bootstrap source and integration guide
```

## Quick start

Open PowerShell in this folder.  First inspect the machine without launching
anything:

```powershell
.\scripts\Diagnose-FlexShow.ps1 -Config .\config\presets\single-3080ti-16gb.json
```

Preview exactly which processes and GPU affinities would be used:

```powershell
.\scripts\Start-FlexShow.ps1 -Config .\config\presets\single-3080ti-16gb.json
```

The start script is intentionally a dry run unless `-Start` is present:

```powershell
.\scripts\Start-FlexShow.ps1 -Config .\config\presets\single-3080ti-16gb.json -Start
```

Stop only processes recorded by this project's runtime manifest:

```powershell
.\scripts\Stop-FlexShow.ps1 -Config .\config\presets\single-3080ti-16gb.json -Stop
```

The included `projects/FlexShow.toe` is launchable now, but its AI, sensor,
particle, projection, and VR modules are labelled integration shells. Copy the
nearest preset to `config/show-config.json`, then connect your real
StreamDiffusionTD and device components at those boundaries. A missing required
path is reported by validation instead of silently launching the wrong file.

## Choose a deployment

| Available hardware | Start from | Recommended assignment |
| --- | --- | --- |
| 3080 Ti Laptop 16 GB | `single-3080ti-16gb.json` | Combined-lite, or installation/VR alone |
| 4090 24 GB | `single-4090.json` | All experience modes |
| 5090 32 GB | `single-5090.json` | All modes with more quality reserve |
| Two different local NVIDIA GPUs | `dual-local-heterogeneous.json` | AI on one GPU, show/VR on the other |
| Two local 4090 GPUs | `dual-local-same-4090.json` | AI on one GPU, show/VR on the other |
| Two Windows computers | worker/show network profiles | AI worker and show node separated by wired Ethernet |

For a mixed pair, put the higher-VRAM card on AI and connect the headset and
projection/LED outputs to the render card.  This is only a starting policy;
`gpu.ai` and `gpu.render` can be swapped in configuration after measuring the
actual show. For a latency-critical combined VR/LED show, benchmark the reverse
assignment as well: keeping the faster card on rendering can matter more than
AI update rate. The network examples intentionally demonstrate a 3080 Ti AI
worker feeding a faster 4090/5090 show node.

## Quality behavior

The chosen tier changes resolution, update-rate, and point-count budgets.  It
does not change the network structure.

- `3080ti_16gb`: SD-Turbo-oriented, asynchronous 384-512 generation, lean
  geometry, and combined-lite defaults.
- `4090`: higher update/geometry budget with room for measured conditioning.
- `5090`: larger reserve for resolution, point count, or model experiments.

AI update rate is independent from world/render rate.  On the 3080 Ti, for
example, a shape may update at 4-10 Hz while particles, sensor forces, and the
headset continue at their own real-time clocks.

## TouchDesigner starter

See [touchdesigner/README.md](touchdesigner/README.md) for building the starter
`.toe`.  The generated network provides labeled adapter points for:

- StreamDiffusionTD RGB and optional depth/confidence.
- Depth-camera audience points and interaction forces.
- Global shared memory or Touch In/Out WorldBus transport.
- Persistent point simulation.
- The three completion choices.
- Installation and VR outputs plus operator status.

The scaffold is deliberately adapter-based.  Connect the exact StreamDiffusionTD
component, camera SDK, and VR component available on the show machine rather
than burying those dependencies in the launcher.

## Testing and security

Run the dependency-free tests with:

```powershell
python -m unittest discover -s tests -v
```

Configuration files may contain arbitrary process commands. Treat downloaded
or shared configurations as executable input: inspect them before using
`-Start`. Shutdown records include process creation time, executable identity,
and a command-line hash so stale PID reuse fails closed. Do not edit or trust a
runtime manifest supplied by another user.

On Windows, an authorized `-Stop` force-terminates only the identity-verified
show processes recorded by this project. Save any interactive TouchDesigner
edits in those launched processes before stopping them.

## Production order

1. Make installation-only work with sensor geometry and generated color.
2. Tune thick points/fog and procedural backfill separately, then hybrid.
3. Add VR-only and keep the headset renderer independent from AI updates.
4. Test combined mode at the 3080 preset before increasing any budget.
5. Move AI to a second GPU/computer using a profile change, without changing
   the artistic world network.

For the process split and failure behavior, read
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).  For the frame/metadata interface,
read [docs/WORLDBUS.md](docs/WORLDBUS.md).

## License

This project is released under the [MIT License](LICENSE).
