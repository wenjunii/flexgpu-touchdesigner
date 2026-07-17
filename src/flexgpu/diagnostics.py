"""Read-only preflight diagnostics for FlexGPU configurations and plans."""

from __future__ import annotations

import os
import shutil
from typing import Sequence

from .commissioning import (
    CalibrationProfile,
    CommissioningError,
    load_strict_json,
    validate_bundle,
)
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


def _config_path(config: FlexConfig, value: object) -> str:
    expanded = os.path.expandvars(os.path.expanduser(str(value)))
    if os.path.isabs(expanded):
        return os.path.abspath(expanded)
    return os.path.abspath(os.path.join(config.base_dir, expanded))


def _adapter_file_check(
    checks: list[Diagnostic],
    config: FlexConfig,
    section: str,
    key: str,
    *,
    suffix: str | None = None,
) -> str | None:
    mapping = config.raw.get(section)
    if not isinstance(mapping, dict) or not mapping.get(key):
        return None
    path = _config_path(config, mapping[key])
    exists = os.path.isfile(path)
    valid_suffix = suffix is None or path.lower().endswith(suffix.lower())
    if exists and valid_suffix:
        checks.append(
            Diagnostic(
                "pass",
                "%s.%s" % (section, key),
                "%s.%s local file is available" % (section, key),
                {"path": path},
            )
        )
    else:
        reason = "is missing" if not exists else "has the wrong file extension"
        checks.append(
            Diagnostic(
                "fail",
                "%s.%s" % (section, key),
                "%s.%s %s" % (section, key, reason),
                {"path": path},
            )
        )
    return path


def _runtime_adapter_diagnostics(
    config: FlexConfig, checks: list[Diagnostic]
) -> None:
    source_calibration_id: str | None = None
    source_calibration_digest: str | None = None
    sensor_calibration_id: str | None = None
    sensor_calibration_digest: str | None = None
    replay_calibration_id: str | None = None
    replay_calibration_digest: str | None = None
    sensor_replay_calibration_id: str | None = None
    sensor_replay_calibration_digest: str | None = None
    source = config.raw.get("source")
    if isinstance(source, dict):
        mode = str(source.get("mode", "demo"))
        if mode == "streamdiffusion" and source.get("auto_load_tox"):
            _adapter_file_check(
                checks, config, "source", "streamdiffusion_tox", suffix=".tox"
            )
        if mode == "replay":
            replay = _adapter_file_check(checks, config, "source", "replay_path")
            if replay and os.path.isfile(replay):
                try:
                    summary = validate_bundle(replay)
                except CommissioningError as exc:
                    checks.append(
                        Diagnostic(
                            "fail",
                            "source.replay.contract",
                            "Source replay bundle is invalid: %s" % exc,
                        )
                    )
                else:
                    replay_calibration_id = str(summary["calibration_id"])
                    replay_calibration_digest = str(summary["calibration_digest"])
                    checks.append(
                        Diagnostic(
                            "pass",
                            "source.replay.contract",
                            "Source replay bundle passed synchronized contract validation",
                            summary,
                        )
                    )
            checks.append(
                Diagnostic(
                    "warn",
                    "source.replay.binding",
                    "Replay is a validated adapter fixture; the stock TouchDesigner network does not play it automatically",
                )
            )
        elif mode == "worldbus":
            checks.append(
                Diagnostic(
                    "warn",
                    "source.worldbus.binding",
                    "WorldBus is a reference contract; the stock TouchDesigner network has no full WorldBus adapter",
                )
            )
        calibration = _adapter_file_check(
            checks, config, "source", "calibration_path", suffix=".json"
        )
        if calibration and os.path.isfile(calibration):
            try:
                profile = CalibrationProfile.from_mapping(load_strict_json(calibration))
            except CommissioningError as exc:
                checks.append(
                    Diagnostic(
                        "fail",
                        "source.calibration.contract",
                        "Source calibration is invalid: %s" % exc,
                    )
                )
            else:
                source_calibration_id = profile.calibration_id
                source_calibration_digest = profile.calibration_digest
                checks.append(
                    Diagnostic(
                        "pass",
                        "source.calibration.contract",
                        "Source calibration is valid",
                        {
                            "calibration_id": profile.calibration_id,
                            "calibration_digest": profile.calibration_digest,
                            "width": profile.width,
                            "height": profile.height,
                            "depth_encoding": profile.depth_encoding,
                        },
                    )
                )

    sensor = config.raw.get("sensor")
    if isinstance(sensor, dict):
        mode = str(sensor.get("mode", "simulated"))
        if mode == "depth_sensor" and sensor.get("auto_load_tox"):
            _adapter_file_check(checks, config, "sensor", "adapter_tox", suffix=".tox")
        if mode == "replay":
            replay = _adapter_file_check(checks, config, "sensor", "replay_path")
            if replay and os.path.isfile(replay):
                try:
                    summary = validate_bundle(replay)
                except CommissioningError as exc:
                    checks.append(
                        Diagnostic(
                            "fail",
                            "sensor.replay.contract",
                            "Sensor replay bundle is invalid: %s" % exc,
                        )
                    )
                else:
                    sensor_replay_calibration_id = str(summary["calibration_id"])
                    sensor_replay_calibration_digest = str(
                        summary["calibration_digest"]
                    )
                    checks.append(
                        Diagnostic(
                            "pass",
                            "sensor.replay.contract",
                            "Sensor replay bundle passed synchronized contract validation",
                            summary,
                        )
                    )
            checks.append(
                Diagnostic(
                    "warn",
                    "sensor.replay.binding",
                    "Sensor replay is an adapter boundary; the stock TouchDesigner network keeps a placeholder input",
                )
            )
        calibration = _adapter_file_check(
            checks, config, "sensor", "calibration_path", suffix=".json"
        )
        if calibration and os.path.isfile(calibration):
            try:
                profile = CalibrationProfile.from_mapping(load_strict_json(calibration))
            except CommissioningError as exc:
                checks.append(
                    Diagnostic(
                        "fail",
                        "sensor.calibration.contract",
                        "Sensor calibration is invalid: %s" % exc,
                    )
                )
            else:
                sensor_calibration_id = profile.calibration_id
                sensor_calibration_digest = profile.calibration_digest
                checks.append(
                    Diagnostic(
                        "pass",
                        "sensor.calibration.contract",
                        "Sensor calibration is valid",
                        {
                            "calibration_id": profile.calibration_id,
                            "calibration_digest": profile.calibration_digest,
                        },
                    )
                )

    if (
        source_calibration_id
        and replay_calibration_id
        and (
            source_calibration_id != replay_calibration_id
            or source_calibration_digest != replay_calibration_digest
        )
    ):
        checks.append(
            Diagnostic(
                "fail",
                "source.calibration.consistency",
                "Configured source calibration does not match the replay bundle",
                {
                    "source_calibration_id": source_calibration_id,
                    "source_calibration_digest": source_calibration_digest,
                    "replay_calibration_id": replay_calibration_id,
                    "replay_calibration_digest": replay_calibration_digest,
                },
            )
        )
    if (
        sensor_calibration_id
        and sensor_replay_calibration_id
        and (
            sensor_calibration_id != sensor_replay_calibration_id
            or sensor_calibration_digest != sensor_replay_calibration_digest
        )
    ):
        checks.append(
            Diagnostic(
                "fail",
                "sensor.calibration.consistency",
                "Configured sensor calibration does not match the replay bundle",
                {
                    "sensor_calibration_id": sensor_calibration_id,
                    "sensor_calibration_digest": sensor_calibration_digest,
                    "replay_calibration_id": sensor_replay_calibration_id,
                    "replay_calibration_digest": sensor_replay_calibration_digest,
                },
            )
        )

    identities = {
        name: (calibration_id, digest)
        for name, calibration_id, digest in (
            ("source", source_calibration_id, source_calibration_digest),
            ("source_replay", replay_calibration_id, replay_calibration_digest),
            ("sensor", sensor_calibration_id, sensor_calibration_digest),
            (
                "sensor_replay",
                sensor_replay_calibration_id,
                sensor_replay_calibration_digest,
            ),
        )
        if calibration_id and digest
    }
    if len(set(identities.values())) > 1:
        checks.append(
            Diagnostic(
                "fail",
                "calibration.shared_world",
                "Configured adapters do not share the same calibration ID and content digest",
                {
                    "identities": {
                        name: {
                            "calibration_id": identity[0],
                            "calibration_digest": identity[1],
                        }
                        for name, identity in identities.items()
                    }
                },
            )
        )


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
                "FLEXGPU_ROOT",
                "FLEXGPU_SRC",
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
                    "Role, CUDA, and bridge import environment are complete"
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
    _runtime_adapter_diagnostics(config, checks)
    return tuple(checks)


def diagnostic_summary(checks: Sequence[Diagnostic]) -> dict[str, int | str]:
    counts = {
        level: sum(1 for check in checks if check.level == level)
        for level in ("pass", "warn", "fail")
    }
    counts["status"] = "fail" if counts["fail"] else "pass"
    return counts
