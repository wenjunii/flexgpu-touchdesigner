"""Configuration loading, alias normalization, and structural validation."""

from __future__ import annotations

import json
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
            parsed = json.loads(data.decode("utf-8-sig"))
    except (ValueError, UnicodeError) as exc:
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
