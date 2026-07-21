# Embody and TD Knowledge MCP

This integration gives an MCP client two coordinated views of FlexGPU:

- the TD Knowledge MCP supplies grounded TouchDesigner/StreamDiffusion
  documentation and the checked-in FlexGPU project contract;
- Embody Envoy supplies live operators, parameters, errors, performance, TOP
  captures, undoable mutations, and multi-session coordination.

The project contract is
`integrations/embody/flexgpu-project-context.json`. It contains only public,
stable paths and operating rules. It deliberately excludes credentials,
private component internals, absolute machine paths, runtime data, and model
material.

## One-time local setup

1. Work on an ignored copy such as
   `projects/FlexShow-moge2-embody-local.toe`.
2. Drop the current `Embody-v6.0.131.tox` into that local project.
3. Set Embody's AI Project Root to a custom ignored folder such as
   `runtime/embody-ai`. This keeps Embody's generated AI configuration away
   from the public repository root.
4. Enable Envoy on `127.0.0.1`. Port `9870` is the initial fallback; Embody
   may advance to another local port during a rapid restart.
5. Leave Embody/TDN externalization off. The private StreamDiffusionTD
   component and working pipeline must not be exported automatically.
6. Save as a new ignored working TOE only after the live project is healthy.
   TouchDesigner's versioned-save option may create a `.1.toe` successor.
7. Copy `.codex/config.toml.example` to the ignored
   `.codex/config.toml` and replace every placeholder with an absolute local
   path. Keep the `--envoy-config` argument pointed at
   `runtime/embody-ai/.embody/envoy.json` and `--project-context` pointed at
   `integrations/embody/flexgpu-project-context.json`.
8. Restart the MCP client so it loads the project-scoped server configuration.

The local `.codex/config.toml` is ignored because it contains machine-specific
absolute paths. It launches the sibling `td-knowledge-mcp`, points it at the
sibling knowledge index, and supplies the checked-in FlexGPU project context.
The server follows the active instance in the Envoy registry instead of
assuming that fallback port `9870` remained available. It then verifies
`/project1/flexgpu` before exposing live tools. This keeps FlexGPU isolated
when another TouchDesigner project is open. Do not configure a second direct
Envoy server unless intentionally debugging the proxy.

## Connection check

Validate the checked-in contract and local wiring from the repository root:

```powershell
.\scripts\Test-TDKnowledgeBridge.ps1
.\scripts\Test-TDKnowledgeBridge.ps1 -RequireEnvoy -WaitReadyMs 30000
```

The first command validates local paths and reports whether the registered
Envoy instance is ready. The second makes the live TouchDesigner process and
its loopback listener mandatory and follows registry changes for up to 30
seconds, which covers a normal cold-start registration window. Neither command
starts or changes TouchDesigner, and neither publishes local paths.

With TouchDesigner closed, the MCP should still expose:

- `get_td_project_context`
- `query_td_knowledge`
- `search_td_docs`
- `get_knowledge_stats`

With the local FlexGPU TOE open and Envoy enabled, the same server should also
expose Envoy tools such as `get_td_info`, `query_network`, `get_op_errors`,
`capture_top`, and `get_project_performance`.

The PowerShell preflight validates configuration, registry following, process
identity, and TCP reachability. The MCP tool checks above remain the
end-to-end proof that the knowledge library and live Envoy proxy negotiated
successfully.

For the first live audit:

1. Read the complete project context.
2. Confirm `get_td_info` reports TouchDesigner 2025.32820.
3. Discover `/` and verify `/project1/flexgpu`.
4. Check recursive errors before making changes.
5. Capture the source color, completed color, position, installation, and all
   wrap/artistic surface outputs.
6. Confirm visual perspective as well as the automated pixel-quality verdict.
7. Read project performance only after outputs are visually correct.

## Safety

Envoy must remain bound to `127.0.0.1`. Do not externalize or inspect the
private `StreamDiffusionTD.tox`; use its public TOP boundary. Do not allow
clear-first imports, destructive deletes, forced quits, or project saves
without explicit approval. The public-sync gate remains authoritative for
GitHub publication.
