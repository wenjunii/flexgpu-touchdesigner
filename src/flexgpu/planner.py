"""Resolve validated configuration into GPU-affined process commands."""

from __future__ import annotations

import os
import re
from typing import Mapping, Sequence

from .config import required_process_roles
from .discovery import resolve_gpu_selector
from .models import (
    DiscoveryError,
    FlexConfig,
    GPUInfo,
    GPUSelector,
    PlanError,
    ProcessDefinition,
    ProcessPlan,
    ProcessSpec,
)
from .presets import auto_tier, preset_for


_PLACEHOLDER_RE = re.compile(
    r"\{(config|role|gpu_index|gpu_uuid|gpu_bus_id|td_bus_id|experience|completion|tier)\}"
)
_TD_BUS_RE = re.compile(r"^(\d+):(\d+):(\d+):(\d+)$")
_PROTECTED_ENV_PREFIXES = ("CUDA_", "FLEXGPU_")


def _is_path_like(value: str) -> bool:
    return (
        os.path.isabs(value)
        or value.startswith(".")
        or "/" in value
        or "\\" in value
    )


def _resolve_path(value: str, base_dir: str, always: bool = False) -> str:
    expanded = os.path.expanduser(os.path.expandvars(value))
    if not expanded:
        return expanded
    if os.path.isabs(expanded):
        return os.path.normpath(expanded)
    if always or _is_path_like(expanded):
        return os.path.normpath(os.path.join(base_dir, expanded))
    return expanded


def _replace_placeholders(value: str, context: Mapping[str, str]) -> str:
    return _PLACEHOLDER_RE.sub(lambda match: context[match.group(1)], value)


def _looks_like_touchdesigner(executable: str) -> bool:
    name = os.path.basename(executable).lower().replace(" ", "")
    return name in {"touchdesigner", "touchdesigner.exe"} or name.startswith("touchdesigner")


def _reject_protected_environment(definition: ProcessDefinition) -> None:
    """Prevent config from replacing launcher-owned identity and affinity."""

    protected = sorted(
        str(key)
        for key in definition.env
        if str(key).upper().startswith(_PROTECTED_ENV_PREFIXES)
    )
    if protected:
        raise PlanError(
            "processes.%s.env may not override launcher-reserved variable%s: %s"
            % (
                definition.role,
                "s" if len(protected) != 1 else "",
                ", ".join(protected),
            )
        )


def _normalize_touchdesigner_affinity(
    command: list[str], gpu: GPUInfo, required: bool
) -> None:
    """Validate one unambiguous TouchDesigner PCI-bus selector in-place."""

    affinity_flags: list[tuple[int, str]] = []
    for index, argument in enumerate(command):
        lowered = argument.lower()
        if lowered in {"-gpubusid", "-gpuformonitor"}:
            affinity_flags.append((index, lowered))
        elif lowered.startswith("-gpubusid=") or lowered.startswith("-gpuformonitor="):
            raise PlanError(
                "TouchDesigner GPU selectors must use a separate value argument"
            )
    if len(affinity_flags) > 1:
        raise PlanError("TouchDesigner command contains duplicate/conflicting GPU selectors")
    if not affinity_flags:
        if required:
            command[1:1] = ["-gpubusid", gpu.td_bus_id]
        return

    index, flag = affinity_flags[0]
    if index + 1 >= len(command) or command[index + 1].startswith("-"):
        raise PlanError("TouchDesigner %s selector is missing its value" % flag)
    if flag == "-gpuformonitor":
        raise PlanError(
            "TouchDesigner -gpuformonitor cannot verify physical GPU affinity; use -gpubusid %s"
            % gpu.td_bus_id
        )
    match = _TD_BUS_RE.fullmatch(command[index + 1].strip())
    if not match:
        raise PlanError("TouchDesigner -gpubusid must be domain:bus:device:function")
    normalized = ":".join(str(int(component)) for component in match.groups())
    if normalized != gpu.td_bus_id:
        raise PlanError(
            "TouchDesigner -gpubusid %s does not match assigned GPU %s"
            % (normalized, gpu.td_bus_id)
        )
    command[index] = "-gpubusid"
    command[index + 1] = normalized


def _role_gpu_assignments(
    config: FlexConfig, gpus: Sequence[GPUInfo]
) -> dict[str, GPUInfo]:
    if not gpus:
        raise PlanError("no NVIDIA GPU is available for process planning")
    try:
        if config.topology == "single":
            selector = config.gpu["render"]
            if selector.kind == "auto" and config.gpu["ai"].kind != "auto":
                selector = config.gpu["ai"]
            selected = resolve_gpu_selector(selector, gpus)
            if config.gpu["ai"].kind != "auto" and config.gpu["render"].kind != "auto":
                ai_selected = resolve_gpu_selector(config.gpu["ai"], gpus)
                if ai_selected.index != selected.index:
                    raise PlanError("single topology requires gpu.ai and gpu.render to select the same GPU")
            return {"world": selected}

        if config.topology == "dual_local":
            ai_selector = config.gpu["ai"]
            render_selector = config.gpu["render"]
            if ai_selector.kind != "auto":
                ai_gpu = resolve_gpu_selector(ai_selector, gpus)
                render_gpu = resolve_gpu_selector(render_selector, gpus, [ai_gpu.index])
            elif render_selector.kind != "auto":
                render_gpu = resolve_gpu_selector(render_selector, gpus)
                ai_gpu = resolve_gpu_selector(ai_selector, gpus, [render_gpu.index])
            else:
                ai_gpu = resolve_gpu_selector(ai_selector, gpus)
                render_gpu = resolve_gpu_selector(render_selector, gpus, [ai_gpu.index])
            if ai_gpu.index == render_gpu.index:
                raise PlanError("dual_local requires distinct AI and render GPUs")
            return {"ai": ai_gpu, "world": render_gpu}

        if config.node_role == "ai":
            return {"ai": resolve_gpu_selector(config.gpu["ai"], gpus)}
        if config.node_role == "render":
            return {"world": resolve_gpu_selector(config.gpu["render"], gpus)}
    except DiscoveryError as exc:
        raise PlanError(str(exc)) from exc
    raise PlanError("unsupported topology/node_role combination")


def _command_for(
    definition: ProcessDefinition,
    gpu: GPUInfo,
    config: FlexConfig,
    resolved_tier: str,
) -> tuple[tuple[str, ...], str, str]:
    base_dir = config.base_dir
    context = {
        "config": config.source_path,
        "role": definition.role,
        "gpu_index": str(gpu.index),
        "gpu_uuid": gpu.uuid,
        "gpu_bus_id": gpu.bus_id,
        "td_bus_id": gpu.td_bus_id,
        "experience": config.experience,
        "completion": config.completion,
        "tier": resolved_tier,
    }
    project_path = ""
    if definition.command:
        raw_command = [
            _replace_placeholders(os.path.expandvars(item), context)
            for item in definition.command
        ]
        raw_command[0] = _resolve_path(raw_command[0], base_dir)
        command = raw_command
        for index in range(1, len(command)):
            if command[index].lower().endswith(".toe"):
                command[index] = _resolve_path(command[index], base_dir, always=True)
                project_path = command[index]
        executable = command[0]
    else:
        executable = _resolve_path(
            _replace_placeholders(definition.executable, context), base_dir
        )
        command = [executable]

    is_td = (
        definition.touchdesigner
        if definition.touchdesigner is not None
        else _looks_like_touchdesigner(executable)
    )
    if not definition.command:
        if definition.project:
            project_path = _resolve_path(
                _replace_placeholders(definition.project, context), base_dir, always=True
            )
            command.append(project_path)
        command.extend(_replace_placeholders(item, context) for item in definition.args)
    elif definition.args:
        command.extend(_replace_placeholders(item, context) for item in definition.args)

    if is_td:
        _normalize_touchdesigner_affinity(command, gpu, definition.gpu_affinity)

    cwd = _resolve_path(definition.cwd, base_dir, always=True) if definition.cwd else base_dir
    return tuple(command), cwd, project_path


def build_process_plan(
    config: FlexConfig, gpus: Sequence[GPUInfo]
) -> ProcessPlan:
    """Build a deterministic process plan for the current local node."""

    roles = required_process_roles(config.topology, config.node_role)
    for role in roles:
        definition = config.processes.get(role)
        if definition is None:
            raise PlanError("missing process definition for role %s" % role)
        _reject_protected_environment(definition)
    assignments = _role_gpu_assignments(config, gpus)
    preferred = assignments.get("ai") or assignments.get("world")
    # ``tier`` is a per-process hardware fact when it is automatic.  A local
    # heterogeneous pair must never inherit the AI card's larger workload on
    # its weaker world/render card (for example, 5090 AI + 3080 Ti world).
    # Keep ProcessPlan.tier as the preferred/AI tier for API compatibility,
    # while each ProcessSpec receives its assigned GPU's tier and limits.
    role_tiers = {
        role: auto_tier(gpus, gpu) if config.tier == "auto" else config.tier
        for role, gpu in assignments.items()
    }
    resolved_tier = (
        role_tiers.get("ai")
        or role_tiers.get("world")
        or (auto_tier(gpus, preferred) if config.tier == "auto" else config.tier)
    )
    warnings: list[str] = []
    for role, role_tier in role_tiers.items():
        if role_tier == "custom":
            warnings.append(
                "%s GPU does not match a tuned 3080ti_16gb, 4090, or 5090 preset"
                % role
            )
    transport_type = str(config.transport.get("type", "")).lower()
    if config.topology == "dual_network" and transport_type in {
        "shared_memory",
        "shared_mem",
        "spout",
    }:
        warnings.append("dual_network should use Touch In/Out or another network transport")

    specs: list[ProcessSpec] = []
    # World/listener first on a local two-process system; AI can reconnect without
    # ever restarting the frame-critical process.
    ordered_roles = tuple(role for role in ("world", "ai") if role in roles)
    for role in ordered_roles:
        definition = config.processes.get(role)
        if definition is None:
            raise PlanError("missing process definition for role %s" % role)
        gpu = assignments[role]
        role_tier = role_tiers[role]
        quality = preset_for(role_tier).settings
        command, cwd, project_path = _command_for(definition, gpu, config, role_tier)
        env = {
            "CUDA_DEVICE_ORDER": "PCI_BUS_ID",
            "CUDA_VISIBLE_DEVICES": gpu.uuid or str(gpu.index),
            "FLEXGPU_CONFIG": config.source_path,
            "FLEXGPU_ROLE": role,
            "FLEXGPU_TOPOLOGY": config.topology,
            "FLEXGPU_EXPERIENCE": config.experience,
            "FLEXGPU_COMPLETION": config.completion,
            "FLEXGPU_TIER": role_tier,
            "FLEXGPU_GPU_INDEX": str(gpu.index),
            "FLEXGPU_GPU_UUID": gpu.uuid,
            "FLEXGPU_GPU_BUS_ID": gpu.bus_id,
            "FLEXGPU_TD_BUS_ID": gpu.td_bus_id,
            "FLEXGPU_DIFFUSION_RESOLUTION": str(quality.get("diffusion_resolution", "")),
            "FLEXGPU_DIFFUSION_HZ": str(quality.get("diffusion_hz", "")),
            "FLEXGPU_GEOMETRY_RESOLUTION": str(quality.get("geometry_resolution", "")),
            "FLEXGPU_GEOMETRY_HZ": str(quality.get("geometry_hz", "")),
            "FLEXGPU_MAX_POINTS": str(quality.get("max_points", "")),
            "FLEXGPU_VR_REFRESH_HZ": str(quality.get("vr_refresh_hz", "")),
        }
        env.update(definition.env)
        dependencies = ("world",) if role == "ai" and "world" in ordered_roles else ()
        specs.append(
            ProcessSpec(
                role=role,
                command=command,
                env=env,
                cwd=cwd,
                gpu=gpu,
                dependencies=dependencies,
                project_path=project_path,
            )
        )

    return ProcessPlan(
        topology=config.topology,
        experience=config.experience,
        completion=config.completion,
        tier=resolved_tier,
        node_role=config.node_role,
        processes=tuple(specs),
        warnings=tuple(warnings),
    )
