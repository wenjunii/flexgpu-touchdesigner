"""Data models shared by the FlexGPU discovery, planning, and CLI layers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence, Tuple


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
        return {
            "role": self.role,
            "command": list(self.command),
            "env": dict(self.env),
            "cwd": self.cwd,
            "gpu": self.gpu.to_dict() if self.gpu else None,
            "dependencies": list(self.dependencies),
            "project_path": self.project_path or None,
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
