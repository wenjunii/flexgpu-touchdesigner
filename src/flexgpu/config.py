"""Configuration loading, alias normalization, and structural validation."""

from __future__ import annotations

import json
import math
import os
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping, MutableMapping, Sequence

from .models import (
    ConfigError,
    FlexConfig,
    GPUSelector,
    ProcessDefinition,
)


TOPOLOGY_ALIASES = {
    "single": "single",
    "single_gpu": "single",
    "one_gpu": "single",
    "dual": "dual_local",
    "dual_gpu": "dual_local",
    "dual_local": "dual_local",
    "dual_same_pc": "dual_local",
    "two_gpu": "dual_local",
    "dual_network": "dual_network",
    "dual_machine": "dual_network",
    "network": "dual_network",
    "two_machine": "dual_network",
}
EXPERIENCE_ALIASES = {
    "installation": "installation",
    "install": "installation",
    "installation_only": "installation",
    "projection": "installation",
    "vr": "vr",
    "vr_only": "vr",
    "combined": "combined",
    "both": "combined",
}
COMPLETION_ALIASES = {
    "fog": "fog",
    "thickness": "fog",
    "thickness_fog": "fog",
    "procedural": "procedural",
    "backfill": "procedural",
    "procedural_backfill": "procedural",
    "hybrid": "hybrid",
    "both": "hybrid",
}
TIER_ALIASES = {
    "auto": "auto",
    "3080ti_16gb": "3080ti_16gb",
    "3080_ti_16gb": "3080ti_16gb",
    "rtx3080ti_16gb": "3080ti_16gb",
    "4090": "4090",
    "rtx4090": "4090",
    "5090": "5090",
    "rtx5090": "5090",
    "custom": "custom",
}
NODE_ROLE_ALIASES = {
    "all": "all",
    "local": "all",
    "ai": "ai",
    "generator": "ai",
    "ai_worker": "ai",
    "render": "render",
    "renderer": "render",
    "world": "render",
}
PROCESS_ROLE_ALIASES = {
    "ai": "ai",
    "generator": "ai",
    "ai_worker": "ai",
    "world": "world",
    "render": "world",
    "renderer": "world",
    "installation": "world",
    "vr": "vr",
    "vr_client": "vr",
}

TRANSPORT_TYPE_ALIASES = {
    "local": "local",
    "in_process": "local",
    "inprocess": "local",
    "shared_memory": "shared_memory",
    "shared_mem": "shared_memory",
    "sharedmem": "shared_memory",
    "touch_tcp": "touch_tcp",
    "touch": "touch_tcp",
    "touch_in_out": "touch_tcp",
    "tcp": "touch_tcp",
}
TRANSPORT_FIELDS = {
    "type",
    "segment_name",
    "bind_host",
    "peer_host",
    "atlas_width",
    "atlas_height",
    "atlas_fps",
    "atlas_port",
    "control_port",
    "heartbeat_port",
    "heartbeat_timeout_ms",
    "drop_stale_frames",
    "hold_last_complete_frame",
}
LOOPBACK_PEERS = {"127.0.0.1", "localhost", "::1"}

TOP_LEVEL_FIELDS = {
    "$schema",
    "topology",
    "experience",
    "experience_mode",
    "completion",
    "completion_mode",
    "tier",
    "profile",
    "node_role",
    "gpu",
    "gpus",
    "processes",
    "process",
    "transport",
    "adaptive",
    "telemetry",
    "source",
    "sensor",
    "render",
    "runtime_dir",
}

RUNTIME_SECTION_FIELDS = {
    "adaptive": {
        "enabled",
        "levels",
        "initial_level",
        "frame_budget_ms",
        "queue_budget_ms",
        "down_window",
        "up_window",
        "cooldown_samples",
        "thresholds",
    },
    "telemetry": {
        "enabled",
        "jsonl_path",
        "summary_path",
        "sample_interval_frames",
        "flush_every",
        "include_operator_metrics",
    },
    "source": {
        "mode",
        "streamdiffusion_tox",
        "replay_path",
        "rgb_operator",
        "depth_operator",
        "mask_operator",
        "confidence_operator",
        "frame_state_operator",
        "camera_metadata_operator",
        "calibration_path",
        "auto_load_tox",
        "stale_timeout_ms",
    },
    "sensor": {
        "mode",
        "adapter_tox",
        "replay_path",
        "mask_operator",
        "position_operator",
        "confidence_operator",
        "frame_state_operator",
        "calibration_path",
        "auto_load_tox",
        "interaction_radius_m",
        "force_gain",
        "stale_timeout_ms",
    },
    "render": {
        "point_size_px",
        "point_budget",
        "installation_width",
        "installation_height",
        "installation_fps",
        "stereo_width",
        "stereo_height",
        "vr_fps",
        "fog_density",
        "procedural_mix",
    },
}

ADAPTIVE_THRESHOLD_FIELDS = {
    "frame_low",
    "frame_high",
    "vram_low",
    "vram_high",
    "queue_low",
    "queue_high",
    "critical_frame",
    "critical_vram",
    "critical_queue",
}


def _choice(
    value: Any,
    aliases: Mapping[str, str],
    field: str,
    errors: list[str],
) -> str:
    key = str(value).strip().lower().replace("-", "_")
    normalized = aliases.get(key)
    if normalized is None:
        errors.append(
            "%s must be one of %s (received %r)"
            % (field, ", ".join(sorted(set(aliases.values()))), value)
        )
        return key
    return normalized


def _transport_integer(
    value: Any,
    field: str,
    errors: list[str],
    minimum: int,
    maximum: int,
    multiple_of: int = 1,
) -> int | None:
    """Validate a transport integer without accepting booleans or coercing strings."""

    if type(value) is not int:
        errors.append("%s must be an integer" % field)
        return None
    if value < minimum or value > maximum:
        errors.append("%s must be between %d and %d" % (field, minimum, maximum))
        return None
    if value % multiple_of:
        errors.append("%s must be a multiple of %d" % (field, multiple_of))
        return None
    return value


def _transport_string(
    value: Any,
    field: str,
    errors: list[str],
    maximum: int,
    allow_internal_whitespace: bool,
) -> str | None:
    """Return a trimmed, non-empty transport identifier or host name."""

    if not isinstance(value, str):
        errors.append("%s must be a string" % field)
        return None
    normalized = value.strip()
    if not normalized:
        errors.append("%s must not be empty" % field)
        return None
    if len(normalized) > maximum:
        errors.append("%s must be at most %d characters" % (field, maximum))
        return None
    if any(ord(character) < 32 for character in normalized):
        errors.append("%s must not contain control characters" % field)
        return None
    if not allow_internal_whitespace and any(character.isspace() for character in normalized):
        errors.append("%s must not contain whitespace" % field)
        return None
    return normalized


def _transport_definition(
    value: Any,
    topology: str,
    supplied: bool,
    errors: list[str],
) -> Mapping[str, Any]:
    """Normalize the transport contract and reject values the TD bridge would ignore.

    A missing transport is meaningful only for a single-process topology, where
    it deterministically becomes ``local``. Split topologies require an explicit
    type and the dimensions/cadence needed by the atomic atlas bridge.
    """

    if not supplied:
        if topology == "single":
            return {"type": "local"}
        errors.append("transport is required for %s topology" % topology)
        return {}

    if isinstance(value, str):
        candidate: dict[str, Any] = {"type": value}
    elif isinstance(value, Mapping):
        candidate = dict(value)
    else:
        errors.append("transport must be a string or object")
        return {}

    unknown = sorted(set(candidate) - TRANSPORT_FIELDS)
    for key in unknown:
        errors.append("transport has unsupported field %r" % key)

    type_value = candidate.get("type")
    normalized_type: str | None = None
    if type_value is None:
        errors.append("transport.type is required when transport is configured")
    elif not isinstance(type_value, str):
        errors.append("transport.type must be a string")
    else:
        type_key = type_value.strip().lower()
        normalized_type = TRANSPORT_TYPE_ALIASES.get(type_key)
        if normalized_type is None:
            errors.append(
                "transport.type must be one of local, shared_memory, or touch_tcp "
                "(received %r)" % type_value
            )

    permitted = {
        "single": {"local"},
        "dual_local": {"shared_memory", "touch_tcp"},
        "dual_network": {"touch_tcp"},
    }.get(topology, set())
    if normalized_type is not None and normalized_type not in permitted:
        errors.append(
            "transport.type %s is incompatible with topology %s"
            % (normalized_type, topology)
        )

    required: set[str] = set()
    if topology in {"dual_local", "dual_network"}:
        required.update({"atlas_width", "atlas_height", "atlas_fps"})
    if normalized_type == "shared_memory":
        required.add("segment_name")
    if normalized_type == "touch_tcp":
        required.update({"peer_host", "atlas_port"})
    if topology == "dual_network":
        required.update({"control_port", "heartbeat_port", "heartbeat_timeout_ms"})
    for key in sorted(required):
        if key not in candidate:
            errors.append("transport.%s is required for %s" % (key, topology))

    normalized: dict[str, Any] = {}
    if normalized_type is not None:
        normalized["type"] = normalized_type

    for key, maximum, whitespace in (
        ("segment_name", 128, True),
        ("bind_host", 255, False),
        ("peer_host", 255, False),
    ):
        if key in candidate:
            text = _transport_string(
                candidate[key], "transport.%s" % key, errors, maximum, whitespace
            )
            if text is not None:
                normalized[key] = text

    peer = normalized.get("peer_host")
    if isinstance(peer, str) and peer.lower() == "localhost":
        normalized["peer_host"] = "localhost"
        peer = "localhost"
    if (
        topology == "dual_local"
        and normalized_type == "touch_tcp"
        and peer is not None
        and peer.lower() not in LOOPBACK_PEERS
    ):
        errors.append(
            "transport.peer_host must be 127.0.0.1, localhost, or ::1 "
            "for dual_local touch_tcp"
        )

    integer_fields = (
        ("atlas_width", 2, 16384, 2),
        ("atlas_height", 1, 16384, 1),
        ("atlas_fps", 1, 240, 1),
        ("atlas_port", 1, 65535, 1),
        ("control_port", 1, 65535, 1),
        ("heartbeat_port", 1, 65535, 1),
        ("heartbeat_timeout_ms", 1, 600000, 1),
    )
    for key, minimum, maximum, multiple_of in integer_fields:
        if key in candidate:
            number = _transport_integer(
                candidate[key],
                "transport.%s" % key,
                errors,
                minimum,
                maximum,
                multiple_of,
            )
            if number is not None:
                normalized[key] = number

    used_ports: dict[int, str] = {}
    for key in ("atlas_port", "control_port", "heartbeat_port"):
        port = normalized.get(key)
        if not isinstance(port, int):
            continue
        previous = used_ports.get(port)
        if previous is not None:
            errors.append(
                "transport.%s must not reuse transport.%s port %d"
                % (key, previous, port)
            )
        else:
            used_ports[port] = key

    for key in ("drop_stale_frames", "hold_last_complete_frame"):
        if key not in candidate:
            continue
        flag = candidate[key]
        if not isinstance(flag, bool):
            errors.append("transport.%s must be true or false" % key)
        else:
            normalized[key] = flag

    return normalized


def _selector(value: Any, field: str, errors: list[str]) -> GPUSelector:
    if value is None or value == "auto":
        return GPUSelector()
    if isinstance(value, bool):
        errors.append("%s must not be a boolean" % field)
        return GPUSelector()
    if isinstance(value, int):
        if value < 0:
            errors.append("%s index must be non-negative" % field)
        return GPUSelector("index", value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            errors.append("%s must not be empty" % field)
            return GPUSelector()
        if stripped.lower() == "auto":
            return GPUSelector()
        if stripped.isdigit():
            return GPUSelector("index", int(stripped))
        if stripped.upper().startswith("GPU-"):
            return GPUSelector("uuid", stripped)
        if ":" in stripped:
            return GPUSelector("bus_id", stripped)
        errors.append(
            "%s string must be auto, a numeric index, a GPU UUID, or a PCI bus ID" % field
        )
        return GPUSelector()
    if isinstance(value, Mapping):
        aliases = {"td_bus_id": "bus_id", "pci_bus_id": "bus_id"}
        def has_selector_value(key: str) -> bool:
            item = value.get(key)
            return item is not None and item != "" and not isinstance(item, bool)

        if value.get("auto") is True and not any(
            has_selector_value(key)
            for key in ("index", "uuid", "bus_id", "td_bus_id", "pci_bus_id")
        ):
            return GPUSelector()
        populated = [key for key in value if key != "auto" and has_selector_value(key)]
        populated = [key for key in populated if key != "auto"]
        if len(populated) != 1:
            errors.append("%s must specify exactly one of index, uuid, or bus_id" % field)
            return GPUSelector()
        key = aliases.get(populated[0], populated[0])
        if key not in {"index", "uuid", "bus_id"}:
            errors.append("%s has unsupported selector %r" % (field, populated[0]))
            return GPUSelector()
        selected = value[populated[0]]
        if key == "index":
            try:
                selected = int(selected)
                if selected < 0:
                    raise ValueError
            except (TypeError, ValueError):
                errors.append("%s.index must be a non-negative integer" % field)
                selected = 0
        return GPUSelector(key, selected)
    errors.append("%s has unsupported type %s" % (field, type(value).__name__))
    return GPUSelector()


def required_process_roles(topology: str, node_role: str) -> tuple[str, ...]:
    """Return processes that must exist on this node.

    A single-GPU process is deliberately unified: its ``world`` process owns AI,
    simulation, and selected outputs to avoid duplicate TD/CUDA overhead.
    """

    if topology == "single":
        return ("world",)
    if topology == "dual_local":
        return ("world", "ai")
    if node_role == "ai":
        return ("ai",)
    if node_role == "render":
        return ("world",)
    return ()


def _string_tuple(value: Any, field: str, errors: list[str]) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        errors.append("%s must be an array of command arguments" % field)
        return ()
    result: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, (str, int, float)) or isinstance(item, bool):
            errors.append("%s[%d] must be a string or number" % (field, index))
        else:
            result.append(str(item))
    return tuple(result)


def _process_definition(
    role: str,
    value: Any,
    defaults: Mapping[str, Any],
    errors: list[str],
) -> ProcessDefinition | None:
    if not isinstance(value, Mapping):
        errors.append("processes.%s must be an object" % role)
        return None
    merged = dict(defaults)
    merged.update(value)
    command = _string_tuple(merged.get("command"), "processes.%s.command" % role, errors)
    executable = merged.get("executable", "")
    project = merged.get("project", "")
    if executable and not isinstance(executable, str):
        errors.append("processes.%s.executable must be a string" % role)
        executable = ""
    if project and not isinstance(project, str):
        errors.append("processes.%s.project must be a string" % role)
        project = ""
    if not command and not executable:
        errors.append(
            "processes.%s requires command[] or executable (with optional project)" % role
        )
    if command and executable:
        errors.append("processes.%s cannot set both command and executable" % role)
    args = _string_tuple(merged.get("args"), "processes.%s.args" % role, errors)
    env_raw = merged.get("env", {})
    env: dict[str, str] = {}
    if not isinstance(env_raw, Mapping):
        errors.append("processes.%s.env must be an object" % role)
    else:
        for key, item in env_raw.items():
            if item is None or isinstance(item, (dict, list)):
                errors.append("processes.%s.env.%s must be a scalar" % (role, key))
            else:
                env[str(key)] = str(item)
    cwd = merged.get("cwd", "")
    if cwd and not isinstance(cwd, str):
        errors.append("processes.%s.cwd must be a string" % role)
        cwd = ""
    touchdesigner = merged.get("touchdesigner")
    if touchdesigner is not None and not isinstance(touchdesigner, bool):
        errors.append("processes.%s.touchdesigner must be true or false" % role)
        touchdesigner = None
    gpu_affinity = merged.get("gpu_affinity", merged.get("affinity", True))
    if not isinstance(gpu_affinity, bool):
        errors.append("processes.%s.gpu_affinity must be true or false" % role)
        gpu_affinity = True
    return ProcessDefinition(
        role=role,
        command=command,
        executable=executable,
        project=project,
        args=args,
        env=env,
        cwd=cwd,
        touchdesigner=touchdesigner,
        gpu_affinity=gpu_affinity,
    )


def _strict_object_pairs(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError("non-finite JSON number is not allowed")


def _runtime_mapping(
    raw: Mapping[str, Any], section: str, errors: list[str]
) -> Mapping[str, Any] | None:
    if section not in raw:
        return None
    value = raw.get(section)
    if not isinstance(value, Mapping):
        errors.append("%s must be an object" % section)
        return None
    for key in sorted(
        set(value).difference(RUNTIME_SECTION_FIELDS[section]), key=lambda item: repr(item)
    ):
        errors.append("%s has unsupported field %r" % (section, key))
    return value


def _runtime_bool(
    section: Mapping[str, Any], key: str, prefix: str, errors: list[str]
) -> None:
    if key in section and type(section[key]) is not bool:
        errors.append("%s.%s must be true or false" % (prefix, key))


def _runtime_string(
    section: Mapping[str, Any], key: str, prefix: str, errors: list[str]
) -> None:
    if key not in section:
        return
    value = section[key]
    if not isinstance(value, str) or not value.strip():
        errors.append("%s.%s must be a non-empty string" % (prefix, key))
    elif any(ord(character) < 32 for character in value):
        errors.append("%s.%s must not contain control characters" % (prefix, key))


def _runtime_integer(
    section: Mapping[str, Any],
    key: str,
    prefix: str,
    errors: list[str],
    minimum: int,
    maximum: int,
) -> int | None:
    if key not in section:
        return None
    value = section[key]
    if type(value) is not int:
        errors.append("%s.%s must be an integer" % (prefix, key))
        return None
    if not minimum <= value <= maximum:
        errors.append(
            "%s.%s must be between %d and %d"
            % (prefix, key, minimum, maximum)
        )
        return None
    return value


def _runtime_number(
    section: Mapping[str, Any],
    key: str,
    prefix: str,
    errors: list[str],
    minimum: float,
    maximum: float,
    *,
    exclusive_minimum: bool = False,
) -> float | None:
    if key not in section:
        return None
    value = section[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        errors.append("%s.%s must be numeric" % (prefix, key))
        return None
    try:
        normalized = float(value)
    except (OverflowError, ValueError):
        errors.append("%s.%s must be finite" % (prefix, key))
        return None
    if not math.isfinite(normalized):
        errors.append("%s.%s must be finite" % (prefix, key))
        return None
    below = normalized <= minimum if exclusive_minimum else normalized < minimum
    if below or normalized > maximum:
        relation = "greater than" if exclusive_minimum else "at least"
        errors.append(
            "%s.%s must be %s %g and at most %g"
            % (prefix, key, relation, minimum, maximum)
        )
        return None
    return normalized


def _runtime_output_path(value: object, source_path: str) -> str:
    expanded = os.path.expandvars(os.path.expanduser(str(value)))
    if not os.path.isabs(expanded):
        base = os.path.dirname(os.path.abspath(source_path)) if source_path else os.getcwd()
        expanded = os.path.join(base, expanded)
    return os.path.normcase(os.path.abspath(os.path.normpath(expanded)))


def _validate_runtime_sections(
    raw: Mapping[str, Any], errors: list[str], source_path: str = ""
) -> None:
    adaptive = _runtime_mapping(raw, "adaptive", errors)
    if adaptive is not None:
        _runtime_bool(adaptive, "enabled", "adaptive", errors)
        levels = _runtime_integer(adaptive, "levels", "adaptive", errors, 2, 16)
        initial = _runtime_integer(adaptive, "initial_level", "adaptive", errors, 0, 15)
        _runtime_number(
            adaptive, "frame_budget_ms", "adaptive", errors, 0, 1000,
            exclusive_minimum=True,
        )
        _runtime_number(
            adaptive, "queue_budget_ms", "adaptive", errors, 0, 60000,
            exclusive_minimum=True,
        )
        _runtime_integer(adaptive, "down_window", "adaptive", errors, 1, 100000)
        _runtime_integer(adaptive, "up_window", "adaptive", errors, 1, 100000)
        _runtime_integer(
            adaptive, "cooldown_samples", "adaptive", errors, 0, 100000
        )
        selected_levels = 5 if levels is None else levels
        if initial is not None and initial >= selected_levels:
            errors.append("adaptive.initial_level must be lower than adaptive.levels")

        thresholds = adaptive.get("thresholds")
        if thresholds is not None:
            if not isinstance(thresholds, Mapping):
                errors.append("adaptive.thresholds must be an object")
            else:
                for key in sorted(set(thresholds).difference(ADAPTIVE_THRESHOLD_FIELDS)):
                    errors.append("adaptive.thresholds has unsupported field %r" % key)
                bounds = {
                    "frame_low": (0.0, 10.0, False),
                    "frame_high": (0.0, 10.0, True),
                    "vram_low": (0.0, 1.0, False),
                    "vram_high": (0.0, 1.0, True),
                    "queue_low": (0.0, 10.0, False),
                    "queue_high": (0.0, 10.0, True),
                    "critical_frame": (0.0, 20.0, True),
                    "critical_vram": (0.0, 1.0, True),
                    "critical_queue": (0.0, 20.0, True),
                }
                values: dict[str, float] = {
                    "frame_low": 0.82,
                    "frame_high": 1.08,
                    "vram_low": 0.76,
                    "vram_high": 0.90,
                    "queue_low": 0.55,
                    "queue_high": 1.15,
                    "critical_frame": 2.0,
                    "critical_vram": 0.97,
                    "critical_queue": 3.0,
                }
                for key, (minimum, maximum, exclusive) in bounds.items():
                    parsed = _runtime_number(
                        thresholds,
                        key,
                        "adaptive.thresholds",
                        errors,
                        minimum,
                        maximum,
                        exclusive_minimum=exclusive,
                    )
                    if parsed is not None:
                        values[key] = parsed
                for prefix in ("frame", "vram", "queue"):
                    if not values[prefix + "_low"] < values[prefix + "_high"]:
                        errors.append(
                            "adaptive.thresholds.%s_low must be below %s_high"
                            % (prefix, prefix)
                        )
                    if not values["critical_" + prefix] > values[prefix + "_high"]:
                        errors.append(
                            "adaptive.thresholds.critical_%s must exceed %s_high"
                            % (prefix, prefix)
                        )

    telemetry = _runtime_mapping(raw, "telemetry", errors)
    if telemetry is not None:
        for key in ("enabled", "include_operator_metrics"):
            _runtime_bool(telemetry, key, "telemetry", errors)
        for key in ("jsonl_path", "summary_path"):
            _runtime_string(telemetry, key, "telemetry", errors)
        _runtime_integer(
            telemetry, "sample_interval_frames", "telemetry", errors, 1, 100000
        )
        _runtime_integer(telemetry, "flush_every", "telemetry", errors, 1, 100000)
        if (
            telemetry.get("jsonl_path")
            and telemetry.get("summary_path")
            and _runtime_output_path(telemetry["jsonl_path"], source_path)
            == _runtime_output_path(telemetry["summary_path"], source_path)
        ):
            errors.append("telemetry JSONL and summary paths must be different")

    source = _runtime_mapping(raw, "source", errors)
    if source is not None:
        mode = source.get("mode")
        if mode is not None and mode not in {"demo", "streamdiffusion", "worldbus", "replay"}:
            errors.append("source.mode is unsupported")
        for key in sorted(
            RUNTIME_SECTION_FIELDS["source"].difference(
                {"mode", "auto_load_tox", "stale_timeout_ms"}
            )
        ):
            _runtime_string(source, key, "source", errors)
        _runtime_bool(source, "auto_load_tox", "source", errors)
        _runtime_integer(source, "stale_timeout_ms", "source", errors, 1, 600000)
        if mode == "replay" and not source.get("replay_path"):
            errors.append("source.replay_path is required when source.mode is replay")
        if source.get("auto_load_tox") is True:
            if not source.get("streamdiffusion_tox"):
                errors.append(
                    "source.streamdiffusion_tox is required when source.auto_load_tox is true"
                )
            if not source.get("rgb_operator"):
                errors.append(
                    "source.rgb_operator is required when source.auto_load_tox is true"
                )

    sensor = _runtime_mapping(raw, "sensor", errors)
    if sensor is not None:
        mode = sensor.get("mode")
        if mode is not None and mode not in {"simulated", "depth_sensor", "replay", "disabled"}:
            errors.append("sensor.mode is unsupported")
        for key in (
            "adapter_tox",
            "replay_path",
            "mask_operator",
            "position_operator",
            "confidence_operator",
            "frame_state_operator",
            "calibration_path",
        ):
            _runtime_string(sensor, key, "sensor", errors)
        _runtime_bool(sensor, "auto_load_tox", "sensor", errors)
        _runtime_number(
            sensor, "interaction_radius_m", "sensor", errors, 0, 20,
            exclusive_minimum=True,
        )
        _runtime_number(sensor, "force_gain", "sensor", errors, 0, 100)
        _runtime_integer(sensor, "stale_timeout_ms", "sensor", errors, 1, 600000)
        if mode == "replay" and not sensor.get("replay_path"):
            errors.append("sensor.replay_path is required when sensor.mode is replay")
        if sensor.get("auto_load_tox") is True:
            if not sensor.get("adapter_tox"):
                errors.append(
                    "sensor.adapter_tox is required when sensor.auto_load_tox is true"
                )
            if not sensor.get("position_operator"):
                errors.append(
                    "sensor.position_operator is required when sensor.auto_load_tox is true"
                )

    render = _runtime_mapping(raw, "render", errors)
    if render is not None:
        _runtime_number(
            render, "point_size_px", "render", errors, 0, 128,
            exclusive_minimum=True,
        )
        _runtime_integer(render, "point_budget", "render", errors, 1000, 10000000)
        for key in (
            "installation_width",
            "installation_height",
            "stereo_width",
            "stereo_height",
        ):
            _runtime_integer(render, key, "render", errors, 64, 16384)
        for key in ("installation_fps", "vr_fps"):
            _runtime_integer(render, key, "render", errors, 1, 240)
        _runtime_number(render, "fog_density", "render", errors, 0, 10)
        _runtime_number(render, "procedural_mix", "render", errors, 0, 1)


def _validate_json_compatible(value: Any, path: str, errors: list[str]) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                errors.append("%s contains a non-string object key" % path)
                continue
            _validate_json_compatible(item, "%s.%s" % (path, key), errors)
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _validate_json_compatible(item, "%s[%d]" % (path, index), errors)
        return
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float) and math.isfinite(value):
        return
    errors.append("%s must be a finite JSON-compatible value" % path)


def load_config_data(path: str | os.PathLike[str]) -> Mapping[str, Any]:
    """Load JSON, or TOML when the embedded Python provides ``tomllib``."""

    source = Path(path)
    try:
        data = source.read_bytes()
    except OSError as exc:
        raise ConfigError(["unable to read config %s: %s" % (source, exc)]) from exc
    try:
        if source.suffix.lower() == ".toml":
            import tomllib

            parsed = tomllib.loads(data.decode("utf-8"))
        else:
            parsed = json.loads(
                data.decode("utf-8-sig"),
                object_pairs_hook=_strict_object_pairs,
                parse_constant=_reject_json_constant,
            )
    except (ImportError, ValueError, UnicodeError) as exc:
        raise ConfigError(["unable to parse config %s: %s" % (source, exc)]) from exc
    if not isinstance(parsed, Mapping):
        raise ConfigError(["config root must be an object"])
    return parsed


def validate_config(
    data: Mapping[str, Any],
    source_path: str = "",
    overrides: Mapping[str, Any] | None = None,
) -> FlexConfig:
    """Normalize and validate a dictionary, returning an immutable config."""

    if not isinstance(data, Mapping):
        raise ConfigError(["config root must be an object"])
    raw: MutableMapping[str, Any] = deepcopy(dict(data))
    if overrides:
        for key, value in overrides.items():
            if value is not None:
                raw[key] = value
    errors: list[str] = []
    _validate_json_compatible(raw, "config", errors)
    for key in sorted(set(raw).difference(TOP_LEVEL_FIELDS), key=lambda item: repr(item)):
        errors.append("config has unsupported top-level field %r" % key)
    for canonical, alias in (
        ("experience", "experience_mode"),
        ("completion", "completion_mode"),
        ("tier", "profile"),
        ("gpu", "gpus"),
        ("processes", "process"),
    ):
        if canonical in raw and alias in raw:
            errors.append("config cannot set both %s and %s" % (canonical, alias))
    if "$schema" in raw and not isinstance(raw["$schema"], str):
        errors.append("$schema must be a string")
    _validate_runtime_sections(raw, errors, source_path)
    topology = _choice(raw.get("topology", "single"), TOPOLOGY_ALIASES, "topology", errors)
    experience = _choice(
        raw.get("experience", raw.get("experience_mode", "installation")),
        EXPERIENCE_ALIASES,
        "experience",
        errors,
    )
    completion = _choice(
        raw.get("completion", raw.get("completion_mode", "hybrid")),
        COMPLETION_ALIASES,
        "completion",
        errors,
    )
    tier = _choice(raw.get("tier", raw.get("profile", "auto")), TIER_ALIASES, "tier", errors)
    node_role_value = raw.get("node_role")
    if topology == "dual_network" and node_role_value is None:
        errors.append("node_role is required for dual_network and must be ai or render")
        node_role_value = "all"
    node_role = _choice(node_role_value or "all", NODE_ROLE_ALIASES, "node_role", errors)
    if topology == "dual_network" and node_role not in {"ai", "render"}:
        errors.append("node_role must be ai or render for dual_network")
    if topology != "dual_network" and node_role != "all":
        errors.append("node_role is only ai/render for dual_network; use all for local topologies")

    gpu_raw = raw.get("gpu", raw.get("gpus", {}))
    if gpu_raw is None:
        gpu_raw = {}
    if not isinstance(gpu_raw, Mapping):
        errors.append("gpu must be an object containing ai and render selectors")
        gpu_raw = {}
    gpu = {
        "ai": _selector(gpu_raw.get("ai", "auto"), "gpu.ai", errors),
        "render": _selector(gpu_raw.get("render", "auto"), "gpu.render", errors),
    }

    processes_raw = raw.get("processes", raw.get("process", {}))
    if not isinstance(processes_raw, Mapping):
        errors.append("processes must be an object")
        processes_raw = {}
    defaults = processes_raw.get("defaults", processes_raw.get("default", {}))
    if not isinstance(defaults, Mapping):
        errors.append("processes.defaults must be an object")
        defaults = {}
    normalized_process_raw: dict[str, Any] = {}
    for key, value in processes_raw.items():
        if key in {"default", "defaults"}:
            continue
        canonical = PROCESS_ROLE_ALIASES.get(str(key).lower().replace("-", "_"))
        if canonical is None:
            errors.append("unsupported process role %r" % key)
        elif canonical in normalized_process_raw:
            errors.append("process role %s is configured more than once" % canonical)
        else:
            normalized_process_raw[canonical] = value
    required = required_process_roles(topology, node_role)
    for role in required:
        if role not in normalized_process_raw:
            errors.append("processes.%s is required for this topology/node_role" % role)
    processes: dict[str, ProcessDefinition] = {}
    for role, value in normalized_process_raw.items():
        process = _process_definition(role, value, defaults, errors)
        if process:
            processes[role] = process

    transport = _transport_definition(
        raw.get("transport"), topology, "transport" in raw, errors
    )
    runtime_dir = raw.get("runtime_dir", "runtime")
    if not isinstance(runtime_dir, str) or not runtime_dir.strip():
        errors.append("runtime_dir must be a non-empty path string")
        runtime_dir = "runtime"

    if errors:
        raise ConfigError(errors)
    return FlexConfig(
        topology=topology,
        experience=experience,
        completion=completion,
        tier=tier,
        node_role=node_role,
        gpu=gpu,
        processes=processes,
        transport=transport,
        runtime_dir=runtime_dir,
        source_path=os.path.abspath(source_path) if source_path else "",
        raw=raw,
    )


def load_config(
    path: str | os.PathLike[str], overrides: Mapping[str, Any] | None = None
) -> FlexConfig:
    absolute = os.path.abspath(os.fspath(path))
    return validate_config(load_config_data(absolute), absolute, overrides)
