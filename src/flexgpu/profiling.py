"""Read-only NVIDIA runtime profiling and role-placement recommendations."""

from __future__ import annotations

import csv
import json
import math
import os
import subprocess
import time
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Any, Sequence

from .discovery import find_nvidia_smi
from .models import DiscoveryError


PROFILE_VERSION = "flexgpu-hardware-profile/v1"
PROFILE_FIELDS = (
    "index",
    "uuid",
    "pci.bus_id",
    "name",
    "memory.total",
    "memory.used",
    "utilization.gpu",
    "temperature.gpu",
    "power.draw",
    "power.limit",
    "clocks.gr",
    "clocks.mem",
    "display_active",
    "pstate",
    "driver_version",
)


def _number(value: str, label: str, *, optional: bool = False) -> float | None:
    normalized = value.strip().replace(",", "")
    if optional and normalized.casefold() in {"", "n/a", "[n/a]", "not supported"}:
        return None
    try:
        result = float(normalized)
    except ValueError as exc:
        raise DiscoveryError("invalid %s in nvidia-smi profile" % label) from exc
    if not math.isfinite(result):
        raise DiscoveryError("non-finite %s in nvidia-smi profile" % label)
    return result


def _integer(value: str, label: str) -> int:
    parsed = _number(value, label)
    assert parsed is not None
    if not parsed.is_integer():
        raise DiscoveryError("non-integer %s in nvidia-smi profile" % label)
    return int(parsed)


@dataclass(frozen=True)
class RuntimeGPUProfile:
    index: int
    uuid: str
    bus_id: str
    name: str
    memory_total_mib: int
    memory_used_mib: int
    utilization_gpu_percent: float
    temperature_c: float
    power_draw_w: float | None
    power_limit_w: float | None
    graphics_clock_mhz: float
    memory_clock_mhz: float
    display_active: bool
    pstate: str
    driver_version: str

    @property
    def memory_headroom_mib(self) -> int:
        return max(0, self.memory_total_mib - self.memory_used_mib)

    @property
    def memory_headroom_ratio(self) -> float:
        return self.memory_headroom_mib / float(max(1, self.memory_total_mib))

    def score(self, role: str) -> float:
        """Return a bounded snapshot score, never a show-time reassignment rule."""

        if role not in {"ai", "render"}:
            raise ValueError("role must be ai or render")
        capacity = min(2.5, self.memory_total_mib / 16_384.0)
        idle = max(0.0, min(1.0, 1.0 - self.utilization_gpu_percent / 100.0))
        thermal = max(0.0, min(1.0, (95.0 - self.temperature_c) / 45.0))
        headroom = self.memory_headroom_ratio
        if role == "ai":
            score = 0.38 * capacity + 0.37 * headroom + 0.15 * idle + 0.10 * thermal
            if self.display_active:
                score -= 0.05
        else:
            score = 0.28 * capacity + 0.34 * headroom + 0.18 * idle + 0.10 * thermal
            score += 0.10 if self.display_active else 0.0
        return round(score, 6)

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "uuid": self.uuid,
            "bus_id": self.bus_id,
            "name": self.name,
            "memory_total_mib": self.memory_total_mib,
            "memory_used_mib": self.memory_used_mib,
            "memory_headroom_mib": self.memory_headroom_mib,
            "memory_headroom_ratio": self.memory_headroom_ratio,
            "utilization_gpu_percent": self.utilization_gpu_percent,
            "temperature_c": self.temperature_c,
            "power_draw_w": self.power_draw_w,
            "power_limit_w": self.power_limit_w,
            "graphics_clock_mhz": self.graphics_clock_mhz,
            "memory_clock_mhz": self.memory_clock_mhz,
            "display_active": self.display_active,
            "pstate": self.pstate,
            "driver_version": self.driver_version,
            "snapshot_scores": {"ai": self.score("ai"), "render": self.score("render")},
        }


def parse_runtime_profile_csv(text: str) -> list[RuntimeGPUProfile]:
    """Parse the exact query used by :func:`query_runtime_gpu_profiles`."""

    profiles: list[RuntimeGPUProfile] = []
    errors: list[str] = []
    for line_number, row in enumerate(csv.reader(StringIO(text), skipinitialspace=True), 1):
        row = [cell.strip() for cell in row]
        if not row or not any(row):
            continue
        if row[0].casefold() in {"index", "gpu index"}:
            continue
        if len(row) != len(PROFILE_FIELDS):
            errors.append(
                "line %d: expected %d fields, received %d"
                % (line_number, len(PROFILE_FIELDS), len(row))
            )
            continue
        try:
            display = row[12].casefold()
            if display not in {"enabled", "disabled", "yes", "no", "true", "false"}:
                raise DiscoveryError("invalid display_active in nvidia-smi profile")
            total = _integer(row[4], "memory.total")
            used = _integer(row[5], "memory.used")
            if total <= 0 or used < 0 or used > total:
                raise DiscoveryError("invalid memory usage in nvidia-smi profile")
            utilization = float(_number(row[6], "utilization.gpu") or 0)
            temperature = float(_number(row[7], "temperature.gpu") or 0)
            graphics_clock = float(_number(row[10], "clocks.gr") or 0)
            memory_clock = float(_number(row[11], "clocks.mem") or 0)
            if not 0 <= utilization <= 100:
                raise DiscoveryError("GPU utilization must be between 0 and 100")
            if not -50 <= temperature <= 150:
                raise DiscoveryError("GPU temperature is outside the supported range")
            if graphics_clock < 0 or memory_clock < 0:
                raise DiscoveryError("GPU clocks must not be negative")
            if not row[1] or not row[2] or not row[3] or not row[13] or not row[14]:
                raise DiscoveryError("runtime GPU identity fields must not be empty")
            profiles.append(
                RuntimeGPUProfile(
                    index=_integer(row[0], "index"),
                    uuid=row[1],
                    bus_id=row[2].upper(),
                    name=row[3],
                    memory_total_mib=total,
                    memory_used_mib=used,
                    utilization_gpu_percent=utilization,
                    temperature_c=temperature,
                    power_draw_w=_number(row[8], "power.draw", optional=True),
                    power_limit_w=_number(row[9], "power.limit", optional=True),
                    graphics_clock_mhz=graphics_clock,
                    memory_clock_mhz=memory_clock,
                    display_active=display in {"enabled", "yes", "true"},
                    pstate=row[13],
                    driver_version=row[14],
                )
            )
        except (DiscoveryError, ValueError) as exc:
            errors.append("line %d: %s" % (line_number, exc))
    if errors:
        raise DiscoveryError("Invalid nvidia-smi runtime profile: " + "; ".join(errors))
    if not profiles:
        raise DiscoveryError("nvidia-smi reported no runtime GPU profiles")
    indices = [profile.index for profile in profiles]
    uuids = [profile.uuid.casefold() for profile in profiles]
    if len(set(indices)) != len(indices) or len(set(uuids)) != len(uuids):
        raise DiscoveryError("nvidia-smi runtime profile contains duplicate GPUs")
    return sorted(profiles, key=lambda profile: profile.index)


def query_runtime_gpu_profiles(
    nvidia_smi: str | None = None, timeout_seconds: float = 8.0
) -> list[RuntimeGPUProfile]:
    executable = nvidia_smi or find_nvidia_smi()
    try:
        completed = subprocess.run(
            [
                executable,
                "--query-gpu=" + ",".join(PROFILE_FIELDS),
                "--format=csv,noheader,nounits",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DiscoveryError("Unable to collect the NVIDIA runtime profile") from exc
    if completed.returncode != 0:
        raise DiscoveryError(
            "nvidia-smi runtime profile failed with exit code %d" % completed.returncode
        )
    return parse_runtime_profile_csv(completed.stdout)


def recommend_role_placement(
    profiles: Sequence[RuntimeGPUProfile], topology: str
) -> dict[str, Any]:
    """Recommend a stable starting assignment from one read-only snapshot."""

    if topology not in {"single", "dual_local"}:
        raise ValueError("profiling topology must be single or dual_local")
    if not profiles:
        raise ValueError("at least one GPU profile is required")
    if topology == "dual_local" and len(profiles) < 2:
        raise ValueError("dual_local profiling requires at least two GPUs")
    if topology == "single":
        selected = max(
            profiles,
            key=lambda item: (item.score("ai") + item.score("render"), -item.index),
        )
        assignment = {"ai_uuid": selected.uuid, "render_uuid": selected.uuid}
    else:
        candidates = [
            (ai.score("ai") + render.score("render"), ai, render)
            for ai in profiles
            for render in profiles
            if ai.uuid.casefold() != render.uuid.casefold()
        ]
        _, ai, render = max(candidates, key=lambda item: (item[0], -item[1].index, -item[2].index))
        assignment = {"ai_uuid": ai.uuid, "render_uuid": render.uuid}
    return {
        "topology": topology,
        "assignment": assignment,
        "caveat": (
            "Snapshot recommendation only. Confirm with StreamDiffusionTD and render "
            "soaks, save UUIDs in a local preset, and never reassign during a show."
        ),
    }


def build_hardware_profile(
    profiles: Sequence[RuntimeGPUProfile], topology: str
) -> dict[str, Any]:
    return {
        "version": PROFILE_VERSION,
        "captured_ns": time.time_ns(),
        "gpus": [profile.to_dict() for profile in profiles],
        "recommendation": recommend_role_placement(profiles, topology),
    }


def write_hardware_profile(
    path: str | os.PathLike[str], payload: dict[str, Any], *, overwrite: bool = False
) -> None:
    destination = Path(path).resolve()
    if destination.exists() and not overwrite:
        raise FileExistsError("hardware profile already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except OSError:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


__all__ = [
    "PROFILE_FIELDS",
    "PROFILE_VERSION",
    "RuntimeGPUProfile",
    "build_hardware_profile",
    "parse_runtime_profile_csv",
    "query_runtime_gpu_profiles",
    "recommend_role_placement",
    "write_hardware_profile",
]
