# FlexGPU live TouchDesigner instructions

This repository builds an interactive 2D-to-3D TouchDesigner world. The public
Python builders and validators are the source of truth; local working `.toe`
files, private components, weights, captures, runtime state, and credentials
must remain untracked.

## MCP and live-project order

1. Call `get_td_project_context` before live work. It supplies the approved
   network roots, output TOPs, validation sequence, and safety boundaries.
2. Use `query_td_knowledge` before relying on TouchDesigner, StreamDiffusionTD,
   or GLSL API details.
3. Use Envoy tools for the live network. Discover `/` first and verify the
   active project and `/project1/flexgpu` before reading or changing operators.
4. Prefer read-only inspection until the user requests a change. Before a
   mutation, inspect the target, claim the managed scope when available, and
   keep the change small and undoable.
5. After each logical change, check recursive errors and capture the affected
   TOP plus its reference TOP. For camera, shader, completion, or renderer
   changes, compare every single and triple-surface output.

## Project boundaries

- Current live baselines use TouchDesigner `2025.32820` and FlexGPU build
  `1.2.1`: the RTX 3080 Ti Laptop 16 GB remains the accepted source machine,
  and an RTX 5090 32 GB workstation has passed a short live MoGe-2/Depth
  Anything migration check. Neither short check replaces a thermal or venue
  soak.
- Treat the tracked repository as hardware-neutral. Keep 3080 and 5090 working
  `.toe` files, `config/local-*.json`, GPU UUIDs, runtime state, components, and
  evidence separate and untracked; never copy one machine's local config over
  the other.
- Managed root: `/project1/flexgpu/WORKING_PIPELINE`.
- Keep `StreamDiffusionTD.tox`, paid Depth Anything components, model weights,
  local profiles, `.toe` working copies, captures, calibration, recordings,
  runtime state, logs, `.env` files, and credentials out of Git.
- Do not inspect or externalize private component internals. Treat the stable
  adapter TOPs as the integration boundary.
- Do not use clear-first network imports, destructive deletes, forced project
  quits, or saves over a working TOE without explicit authorization and an
  accepted backup.
- Embody/TDN externalization remains off for this project unless the user
  explicitly approves a reviewed public subtree.
- Preserve user-authored operators and all unrelated dirty worktree changes.

## Visual acceptance

A TOP quality verdict is only a first gate. Always inspect perspective and
content. Specifically reject black rectangular sprites, a flat image plate,
vertical inversion, blue/pink placeholder glyphs, dark stretched duplicates,
identical wrap-camera views, stale frames, non-finite pixels, or blank output.

Validate in this order: errors, MoGe frame freshness, RGB orientation,
position/depth validity, single output, panoramic left/center/right, artistic
left/center/right, stereo preview, performance, then a thermal soak.

Never describe the desktop stereo TOP as completed VR. It does not validate
OpenXR/OpenVR pose, per-eye projection, compositor submission, or headset
timing.
