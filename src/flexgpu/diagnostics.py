"""Read-only preflight diagnostics for FlexGPU configurations and plans."""

from __future__ import annotations

import os
import shutil
from typing import Sequence

from .models import Diagnostic, FlexConfig, GPUInfo, PlanError, ProcessPlan
from .planner import build_process_plan
from .presets import classify_gpu


def _command_exists(command: str, cwd: str) -> bool:
    expanded = os.path.expandvars(os.path.expanduser(command))
    if os.path.isabs(expanded):
        return os.path.isfile(expanded)
    if "/" in expanded or "\\" in expanded:
        return os.path.isfile(os.path.join(cwd, expanded))
    return shutil.which(expanded) is not None


def run_diagnostics(
    config: FlexConfig,
    gpus: Sequence[GPUInfo],
    plan: ProcessPlan | None = None,
) -> tuple[Diagnostic, ...]:
    """Return actionable diagnostics without starting or modifying any process."""

    checks: list[Diagnostic] = []
    if gpus:
        checks.append(
            Diagnostic(
                "pass",
                "gpu.discovery",
                "Discovered %d NVIDIA GPU%s" % (len(gpus), "" if len(gpus) == 1 else "s"),
                {"gpus": [gpu.to_dict() for gpu in gpus]},
            )
        )
    else:
        checks.append(Diagnostic("fail", "gpu.discovery", "No NVIDIA GPU was discovered"))

    minimum = 2 if config.topology == "dual_local" else 1
    level = "pass" if len(gpus) >= minimum else "fail"
    checks.append(
        Diagnostic(
            level,
            "gpu.count",
            "%s needs at least %d local NVIDIA GPU%s; found %d"
            % (config.topology, minimum, "" if minimum == 1 else "s", len(gpus)),
        )
    )

    resolved_plan = plan
    if resolved_plan is None and gpus:
        try:
            resolved_plan = build_process_plan(config, gpus)
        except PlanError as exc:
            checks.append(Diagnostic("fail", "plan.resolve", str(exc)))
    if resolved_plan is not None:
        checks.append(
            Diagnostic(
                "pass",
                "plan.resolve",
                "Resolved %d process%s for this node"
                % (
                    len(resolved_plan.processes),
                    "" if len(resolved_plan.processes) == 1 else "es",
                ),
                {"roles": [process.role for process in resolved_plan.processes]},
            )
        )
        assigned = [process.gpu.index for process in resolved_plan.processes if process.gpu]
        if config.topology == "dual_local":
            distinct = len(set(assigned)) == 2
            checks.append(
                Diagnostic(
                    "pass" if distinct else "fail",
                    "gpu.affinity.distinct",
                    "AI and world roles use distinct GPUs"
                    if distinct
                    else "AI and world roles must use distinct GPUs",
                    {"gpu_indices": assigned},
                )
            )

        for process in resolved_plan.processes:
            role = process.role
            if not process.command:
                checks.append(Diagnostic("fail", "process.%s.command" % role, "Command is empty"))
                continue
            executable_exists = _command_exists(process.command[0], process.cwd)
            checks.append(
                Diagnostic(
                    "pass" if executable_exists else "fail",
                    "process.%s.executable" % role,
                    "Executable is available: %s" % process.command[0]
                    if executable_exists
                    else "Executable is missing: %s" % process.command[0],
                )
            )
            cwd_exists = os.path.isdir(process.cwd)
            checks.append(
                Diagnostic(
                    "pass" if cwd_exists else "fail",
                    "process.%s.cwd" % role,
                    "Working directory exists: %s" % process.cwd
                    if cwd_exists
                    else "Working directory is missing: %s" % process.cwd,
                )
            )
            if process.project_path:
                project_exists = os.path.isfile(process.project_path)
                checks.append(
                    Diagnostic(
                        "pass" if project_exists else "fail",
                        "process.%s.project" % role,
                        "Project exists: %s" % process.project_path
                        if project_exists
                        else "Project is missing: %s" % process.project_path,
                    )
                )
            expected_env = {
                "CUDA_VISIBLE_DEVICES",
                "FLEXGPU_CONFIG",
                "FLEXGPU_ROLE",
                "FLEXGPU_EXPERIENCE",
                "FLEXGPU_COMPLETION",
                "FLEXGPU_TIER",
            }
            missing_env = sorted(expected_env.difference(process.env))
            checks.append(
                Diagnostic(
                    "pass" if not missing_env else "fail",
                    "process.%s.environment" % role,
                    "Role and CUDA environment are complete"
                    if not missing_env
                    else "Missing environment keys: " + ", ".join(missing_env),
                )
            )

        for warning in resolved_plan.warnings:
            checks.append(Diagnostic("warn", "plan.warning", warning))
        assigned_gpu = next(
            (
                process.gpu
                for process in resolved_plan.processes
                if process.role == "ai" and process.gpu
            ),
            None,
        ) or next((process.gpu for process in resolved_plan.processes if process.gpu), None)
        if config.tier != "auto" and assigned_gpu:
            detected = classify_gpu(assigned_gpu)
            if detected != "custom" and detected != config.tier:
                checks.append(
                    Diagnostic(
                        "warn",
                        "tier.mismatch",
                        "Configured tier %s differs from assigned GPU tier %s"
                        % (config.tier, detected),
                    )
                )

    transport_type = str(config.transport.get("type", "")).strip().lower()
    if config.topology == "dual_network":
        network_types = {
            "touch",
            "touch_tcp",
            "touch_network",
            "touch_in_out",
            "touch_tcp",
            "tcp",
            "network",
        }
        if not transport_type:
            checks.append(
                Diagnostic(
                    "warn",
                    "transport.type",
                    "No network transport type is declared; Touch In/Out is recommended",
                )
            )
        elif transport_type not in network_types:
            checks.append(
                Diagnostic(
                    "warn",
                    "transport.type",
                    "Transport %s may not work across machines" % transport_type,
                )
            )
        else:
            checks.append(
                Diagnostic("pass", "transport.type", "Network transport is %s" % transport_type)
            )
    else:
        checks.append(
            Diagnostic(
                "pass",
                "transport.type",
                "Local transport is %s" % (transport_type or "project default"),
            )
        )
    checks.append(
        Diagnostic(
            "pass",
            "completion.mode",
            "Geometry completion mode is %s" % config.completion,
        )
    )
    return tuple(checks)


def diagnostic_summary(checks: Sequence[Diagnostic]) -> dict[str, int | str]:
    counts = {
        level: sum(1 for check in checks if check.level == level)
        for level in ("pass", "warn", "fail")
    }
    counts["status"] = "fail" if counts["fail"] else "pass"
    return counts
