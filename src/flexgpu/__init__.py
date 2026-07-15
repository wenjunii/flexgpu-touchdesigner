"""Dependency-free GPU role planning for the FlexGPU TouchDesigner project."""

from .adaptive import (
    AdaptiveDecision,
    AdaptiveQualityGovernor,
    GovernorThresholds,
    QualityBounds,
    QualityState,
    TelemetryJsonlWriter,
    TelemetrySample,
    quality_bounds_for_tier,
    read_telemetry_jsonl,
    summarize_telemetry,
    write_telemetry_summary,
)
from .config import load_config, load_config_data, required_process_roles, validate_config
from .diagnostics import diagnostic_summary, run_diagnostics
from .discovery import (
    discover_nvidia_gpus,
    parse_nvidia_smi_csv,
    resolve_gpu_selector,
    touchdesigner_bus_id,
)
from .models import (
    ConfigError,
    Diagnostic,
    DiscoveryError,
    FlexConfig,
    FlexGPUError,
    GPUInfo,
    GPUSelector,
    PlanError,
    ProcessPlan,
    ProcessSpec,
    RuntimeControlError,
)
from .planner import build_process_plan
from .presets import TIER_PRESETS, auto_tier, classify_gpu, preset_for
from .runtime import manifest_path, start_plan, stop_managed

__all__ = [
    "AdaptiveDecision",
    "AdaptiveQualityGovernor",
    "ConfigError",
    "Diagnostic",
    "DiscoveryError",
    "FlexConfig",
    "FlexGPUError",
    "GPUInfo",
    "GPUSelector",
    "GovernorThresholds",
    "PlanError",
    "ProcessPlan",
    "ProcessSpec",
    "QualityBounds",
    "QualityState",
    "RuntimeControlError",
    "TIER_PRESETS",
    "TelemetryJsonlWriter",
    "TelemetrySample",
    "auto_tier",
    "build_process_plan",
    "classify_gpu",
    "diagnostic_summary",
    "discover_nvidia_gpus",
    "load_config",
    "load_config_data",
    "manifest_path",
    "parse_nvidia_smi_csv",
    "preset_for",
    "quality_bounds_for_tier",
    "read_telemetry_jsonl",
    "required_process_roles",
    "resolve_gpu_selector",
    "run_diagnostics",
    "start_plan",
    "stop_managed",
    "summarize_telemetry",
    "touchdesigner_bus_id",
    "validate_config",
    "write_telemetry_summary",
]
