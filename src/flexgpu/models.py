"""Data models shared by the FlexGPU discovery, planning, and CLI layers."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
import re
from typing import Any, Mapping, Optional, Sequence, Tuple


REDACTED = "<redacted>"
_SENSITIVE_NAME_RE = re.compile(
    r"(?:^|[_-])(?:access[_-]?(?:key|token)|api[_-]?(?:key|secret)|auth|"
    r"authorization|bearer[_-]?token|client[_-]?secret|cookie|credential|"
    r"connection[_-]?string|dsn|encryption[_-]?key|key|license|password|passwd|"
    r"private[_-]?(?:key|token)|refresh[_-]?token|sas|secret|signature|"
    r"signing[_-]?key|token|webhook[_-]?secret)(?:$|[_-])",
    re.IGNORECASE,
)
_URI_CREDENTIAL_RE = re.compile(r"(?P<prefix>://[^/:@\s]+:)[^@\s]+@")
_INLINE_SECRET_RE = re.compile(
    r"(?P<prefix>(?:^|[?&;,\s])(?:access[_-]?(?:key|token)|"
    r"api[_-]?(?:key|secret)|auth(?:orization|[_-]?token)?|bearer[_-]?token|"
    r"client[_-]?secret|cookie|credential|connection[_-]?string|dsn|"
    r"encryption[_-]?key|key|license(?:[_-]?(?:key|token))?|password|passwd|"
    r"private[_-]?(?:key|token)|refresh[_-]?token|sas|secret|signature|"
    r"signing[_-]?key|token|webhook[_-]?secret)=)"
    r"[^&;,\s]+",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"(?P<prefix>\bbearer\s+)[A-Za-z0-9._~+/=-]+", re.IGNORECASE)


def sensitive_environment_values(environment: Mapping[str, str]) -> tuple[str, ...]:
    """Return configured secret values without exposing them in public models."""

    return tuple(
        str(value)
        for key, value in environment.items()
        if _SENSITIVE_NAME_RE.search(str(key).replace(".", "_")) and str(value)
    )


def redact_environment(
    environment: Mapping[str, str], sensitive_values: Sequence[str] = ()
) -> dict[str, str]:
    """Redact secret-like environment variables for plans and diagnostics."""

    known_values = sensitive_environment_values(environment) + tuple(sensitive_values)
    return {
        str(key): REDACTED
        if _SENSITIVE_NAME_RE.search(str(key).replace(".", "_"))
        else redact_text(value, known_values)
        for key, value in environment.items()
    }


def redact_command(
    command: Sequence[str], sensitive_values: Sequence[str] = ()
) -> list[str]:
    """Return argv safe for logs, manifests, diagnostics, and CLI output.

    The real argv remains on :class:`ProcessSpec` and is used only for launch
    and hashing.  Common secret flags, ``name=value`` forms, URI passwords,
    and values also present in secret environment variables are masked.
    """

    secrets = tuple(sorted({str(value) for value in sensitive_values if value}, key=len, reverse=True))
    result: list[str] = []
    redact_next = False
    for raw in command:
        value = str(raw)
        if redact_next:
            result.append(REDACTED)
            redact_next = False
            continue
        name, separator, assigned = value.partition("=")
        normalized_name = name.lstrip("-/").replace(".", "_")
        if separator and _SENSITIVE_NAME_RE.search(normalized_name):
            result.append(name + "=" + REDACTED)
            continue
        colon_name, colon, _colon_value = value.partition(":")
        if (
            colon
            and colon_name.startswith(("/", "-"))
            and _SENSITIVE_NAME_RE.search(colon_name.lstrip("-/").replace(".", "_"))
        ):
            result.append(colon_name + ":" + REDACTED)
            continue
        if value.startswith(("-", "/")) and _SENSITIVE_NAME_RE.search(normalized_name):
            result.append(value)
            redact_next = True
            continue
        safe = _URI_CREDENTIAL_RE.sub(r"\g<prefix>" + REDACTED + "@", value)
        safe = _INLINE_SECRET_RE.sub(r"\g<prefix>" + REDACTED, safe)
        safe = _BEARER_RE.sub(r"\g<prefix>" + REDACTED, safe)
        for secret in secrets:
            safe = safe.replace(secret, REDACTED)
        result.append(safe)
    return result


def redact_text(value: object, sensitive_values: Sequence[str] = ()) -> str:
    """Scrub known secret values from a user-visible error string."""

    safe = str(value)
    for secret in sorted({str(item) for item in sensitive_values if item}, key=len, reverse=True):
        safe = safe.replace(secret, REDACTED)
    safe = _URI_CREDENTIAL_RE.sub(r"\g<prefix>" + REDACTED + "@", safe)
    safe = _INLINE_SECRET_RE.sub(r"\g<prefix>" + REDACTED, safe)
    return _BEARER_RE.sub(r"\g<prefix>" + REDACTED, safe)


class FlexGPUError(RuntimeError):
    """Base error for failures that should be presented without a traceback."""


class DiscoveryError(FlexGPUError):
    """NVIDIA GPU discovery failed."""


class ConfigError(FlexGPUError):
    """Configuration is invalid."""

    def __init__(self, errors: Sequence[str]):
        self.errors = tuple(errors)
        super().__init__("; ".join(self.errors))


class PlanError(FlexGPUError):
    """A process plan cannot be produced for the available hardware."""


class RuntimeControlError(FlexGPUError):
    """A managed process could not be started or stopped safely."""


@dataclass(frozen=True)
class GPUInfo:
    """One physical NVIDIA GPU as reported by ``nvidia-smi``."""

    index: int
    uuid: str
    bus_id: str
    name: str
    memory_total_mib: int
    driver_version: str = ""

    @property
    def td_bus_id(self) -> str:
        """Return TouchDesigner's domain:bus:device:function representation."""

        from .discovery import touchdesigner_bus_id

        return touchdesigner_bus_id(self.bus_id)

    def to_dict(self) -> dict[str, Any]:
        from .presets import classify_gpu

        return {
            "index": self.index,
            "uuid": self.uuid,
            "bus_id": self.bus_id,
            "td_bus_id": self.td_bus_id,
            "name": self.name,
            "memory_total_mib": self.memory_total_mib,
            "driver_version": self.driver_version,
            "tier": classify_gpu(self),
        }


@dataclass(frozen=True)
class GPUSelector:
    """A normalized GPU selector from configuration."""

    kind: str = "auto"
    value: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {self.kind: self.value} if self.kind != "auto" else {"auto": True}


@dataclass(frozen=True)
class ProcessDefinition:
    """Normalized, not-yet-expanded process configuration."""

    role: str
    command: Tuple[str, ...] = ()
    executable: str = ""
    project: str = ""
    args: Tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    cwd: str = ""
    touchdesigner: Optional[bool] = None
    gpu_affinity: bool = True


@dataclass(frozen=True)
class FlexConfig:
    """Validated project configuration."""

    topology: str
    experience: str
    completion: str
    tier: str
    node_role: str
    gpu: Mapping[str, GPUSelector]
    processes: Mapping[str, ProcessDefinition]
    transport: Mapping[str, Any]
    runtime_dir: str
    supervisor: Mapping[str, Any] = field(default_factory=dict)
    source_path: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def base_dir(self) -> str:
        import os

        if self.source_path:
            return os.path.dirname(os.path.abspath(self.source_path))
        return os.getcwd()


@dataclass(frozen=True)
class ProcessSpec:
    """One fully resolved process launch specification."""

    role: str
    command: Tuple[str, ...]
    env: Mapping[str, str]
    cwd: str
    gpu: Optional[GPUInfo]
    dependencies: Tuple[str, ...] = ()
    project_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        sensitive_values = (
            sensitive_environment_values(self.env)
            + sensitive_environment_values(os.environ)
        )
        return {
            "role": self.role,
            "command": redact_command(self.command, sensitive_values),
            "env": redact_environment(self.env, sensitive_values),
            "cwd": redact_text(self.cwd, sensitive_values),
            "gpu": self.gpu.to_dict() if self.gpu else None,
            "dependencies": list(self.dependencies),
            "project_path": redact_text(self.project_path, sensitive_values)
            if self.project_path
            else None,
        }


@dataclass(frozen=True)
class ProcessPlan:
    """Resolved role and process plan for one local node."""

    topology: str
    experience: str
    completion: str
    tier: str
    node_role: str
    processes: Tuple[ProcessSpec, ...]
    warnings: Tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "topology": self.topology,
            "experience": self.experience,
            "completion": self.completion,
            "tier": self.tier,
            "node_role": self.node_role,
            "processes": [process.to_dict() for process in self.processes],
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class Diagnostic:
    """One machine-readable preflight diagnostic."""

    level: str
    code: str
    message: str
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "code": self.code,
            "message": self.message,
            "details": dict(self.details),
        }
