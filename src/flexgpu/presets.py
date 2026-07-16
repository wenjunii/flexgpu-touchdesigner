"""Hardware-tier classification and conservative quality defaults."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .models import GPUInfo


SUPPORTED_TIERS = ("auto", "3080ti_16gb", "4090", "5090", "custom")


@dataclass(frozen=True)
class TierPreset:
    id: str
    label: str
    settings: Mapping[str, Any]


TIER_PRESETS: dict[str, TierPreset] = {
    "3080ti_16gb": TierPreset(
        "3080ti_16gb",
        "RTX 3080 Ti Laptop 16 GB",
        {
            "diffusion_resolution": 512,
            "diffusion_hz": 10,
            "geometry_resolution": 384,
            "geometry_hz": 5,
            "max_points": 120_000,
            "vr_refresh_hz": 72,
        },
    ),
    "4090": TierPreset(
        "4090",
        "RTX 4090 24 GB",
        {
            "diffusion_resolution": 512,
            "diffusion_hz": 15,
            "geometry_resolution": 512,
            "geometry_hz": 10,
            "max_points": 250_000,
            "vr_refresh_hz": 90,
        },
    ),
    "5090": TierPreset(
        "5090",
        "RTX 5090 32 GB",
        {
            "diffusion_resolution": 512,
            "diffusion_hz": 20,
            "geometry_resolution": 512,
            "geometry_hz": 15,
            # A 512-square position texture contains at most 262,144 samples.
            # Keep the advertised point budget physically reachable; profiles
            # that intentionally raise geometry resolution may override it.
            "max_points": 262_144,
            "vr_refresh_hz": 90,
        },
    ),
    "custom": TierPreset("custom", "Custom NVIDIA GPU", {}),
}


def _normalized_name(name: str) -> str:
    return re.sub(r"[^A-Z0-9]+", " ", name.upper()).strip()


def classify_gpu(gpu: GPUInfo) -> str:
    """Classify only known VRAM-equivalent targets; otherwise return custom."""

    name = _normalized_name(gpu.name)
    memory = gpu.memory_total_mib
    if "RTX 5090" in name and (memory == 0 or memory >= 28_000):
        return "5090"
    if "RTX 4090" in name and "LAPTOP" not in name and (memory == 0 or memory >= 20_000):
        return "4090"
    if (
        "RTX 3080 TI" in name
        and "LAPTOP" in name
        and 15_000 <= memory <= 17_500
    ):
        return "3080ti_16gb"
    return "custom"


def preset_for(tier: str) -> TierPreset:
    if tier == "auto":
        raise ValueError("auto must be resolved against a GPU before selecting a preset")
    return TIER_PRESETS.get(tier, TIER_PRESETS["custom"])


def auto_tier(gpus: Sequence[GPUInfo], preferred: GPUInfo | None = None) -> str:
    """Resolve auto from the assigned AI GPU, or from the largest local GPU."""

    if preferred is not None:
        return classify_gpu(preferred)
    if not gpus:
        return "custom"
    return classify_gpu(max(gpus, key=lambda gpu: gpu.memory_total_mib))
