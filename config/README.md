# FlexShow configuration

`flexgpu.py` reads one JSON or TOML configuration and turns it into a deterministic GPU/process plan. Config selection precedence is explicit `-Config`, `FLEXSHOW_CONFIG`, `FLEXGPU_CONFIG`, then `config/flexshow.json`.

The configuration has five independent choices:

- `topology`: `single`, `dual_local`, or `dual_network`
- `experience`: `installation`, `vr`, or `combined`
- `completion`: `fog`, `procedural`, or `hybrid`
- `tier`: `auto`, `3080ti_16gb`, `4090`, or `5090`
- `gpu.ai` / `gpu.render`: `auto`, a CUDA index, or an object containing `index`, `uuid`, or `bus_id`

For a network pair, both computers use their own configuration. The AI computer sets `node_role` to `ai`; the show computer sets it to `render`. Keep their atlas/control ports identical and set each `transport.peer_host` to the other computer's static address.

The network presets use the RFC 5737 documentation range `192.0.2.0/24` on
purpose. Replace those non-routable example addresses with static addresses
from your actual show network before launch.

## Safe first run

Start and Stop are previews by default. `-Start` authorizes launch and `-Stop`
authorizes forceful shutdown. Diagnose is always read-only; its legacy
`-Start`/`-Run` switch is accepted but ignored with a warning:

```powershell
.\scripts\Start-FlexShow.ps1
.\scripts\Diagnose-FlexShow.ps1
.\scripts\Start-FlexShow.ps1 -Config config\presets\single-4090.json -Experience vr
```

For CI or another dedicated PowerShell process, `-Json -ExitWithCode` produces
clean JSON and preserves controller exit `2` (configuration) or `3`
(diagnostic/runtime). Do not use `-ExitWithCode` in an interactive host you
want to keep open, because an error intentionally exits that host.

After the plan is correct and the `.toe` project paths exist:

```powershell
.\scripts\Start-FlexShow.ps1 -Config config\presets\dual-network-ai-worker-3080ti-16gb.json -Start
.\scripts\Stop-FlexShow.ps1  -Config config\presets\dual-network-ai-worker-3080ti-16gb.json -Stop
```

Repeating Stop is safe. Repeating Start reuses a process only when its command
and injected environment still match; changed experience, completion, tier, or
GPU settings require Stop followed by Start. Process ownership is tracked by a
creation token, executable, command-line hash, environment hash, and retained
Windows process handle rather than by executable name. Windows Stop is forceful,
so save edits in launched TouchDesigner processes first.

## Project paths

The templates target the normal TouchDesigner installation at:

`C:\Program Files\Derivative\TouchDesigner\bin\TouchDesigner.exe`

They expect `projects/FlexShow.toe` at the repository root. The planner injects `FLEXGPU_CONFIG`, `FLEXGPU_ROLE`, and GPU identity through environment variables, allowing one project scaffold to serve every role. Change `executable`, `project`, or `cwd` if your installation differs. An execute request intentionally fails before launching anything when a required executable or project is missing.

## Presets

Hardware/topology presets live in `presets/`. Run them in place. To customize a
preset while preserving its relative paths, copy it within that directory—for
example, `Copy-Item .\config\presets\single-3080ti-16gb.json .\config\presets\local-show.json`.
Local preset names beginning with `local-` are gitignored. Explicit and
environment-selected relative config paths resolve from the caller's current
PowerShell directory; the default config resolves from the repository root.

The three single-GPU examples also demonstrate installation/fog,
VR/procedural, and combined/hybrid. CLI flags can independently override
`experience`, `completion`, and `tier`, so the examples do not need to grow into
a full combination matrix.

| Preset | Topology | Experience | Completion |
| --- | --- | --- | --- |
| `single-3080ti-16gb.json` | single | installation | fog |
| `single-4090.json` | single | VR | procedural |
| `single-5090.json` | single | combined | hybrid |
| `dual-local-heterogeneous.json` | dual local | combined | hybrid |
| `dual-local-same-4090.json` | dual local | combined | hybrid |
| `dual-network-ai-worker-3080ti-16gb.json` | network AI node | combined | hybrid |
| `dual-network-show-node-4090.json` | network show node | combined | hybrid |
| `dual-network-show-node-5090.json` | network show node | combined | hybrid |

Examples of stable GPU selectors:

```json
{
  "gpu": {
    "ai": { "uuid": "GPU-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx" },
    "render": { "bus_id": "00000000:01:00.0" }
  }
}
```

For stable dual-GPU deployments, replace numeric indexes with GPU UUIDs or PCI bus IDs after running the diagnostic script. Indexes can change after driver updates or docking changes.

## Network payload

The network templates describe one 1024x512 uncompressed atlas at 5-15 Hz: generated RGB on the left and packed depth/mask/confidence on the right. At 10 Hz this is roughly 168 Mbit/s before protocol overhead. Use wired 2.5 GbE when available; 1 GbE is adequate for the provided 512-pixel presets. OSC carries low-volume controls and heartbeat data. The show node must retain the last complete atlas when the AI node disconnects; its render loop never waits for AI.
