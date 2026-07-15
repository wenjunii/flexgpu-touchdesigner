"""Adaptive quality control and dependency-free telemetry helpers.

The governor is deliberately independent from TouchDesigner.  A TD extension,
benchmark, or replay tool can feed it one sample at a time and apply the
returned settings at a safe frame boundary.
"""

from __future__ import annotations

import json
import math
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from statistics import fmean
from typing import Any, Iterable, Iterator, Mapping, TextIO

from .presets import preset_for


QUALITY_KEYS = (
    "diffusion_resolution",
    "diffusion_hz",
    "geometry_resolution",
    "geometry_hz",
    "max_points",
    "vr_refresh_hz",
)

_CUSTOM_MAXIMUM = {
    "diffusion_resolution": 512,
    "diffusion_hz": 10,
    "geometry_resolution": 384,
    "geometry_hz": 5,
    "max_points": 120_000,
    "vr_refresh_hz": 60,
}

# Minimums are intentionally useful rather than merely technically valid.
# Output refresh is pinned: missing a render/VR frame is more disruptive than
# reducing the asynchronous AI or geometry update rate.
_TIER_MINIMUMS: dict[str, Mapping[str, int]] = {
    "3080ti_16gb": {
        "diffusion_resolution": 384,
        "diffusion_hz": 5,
        "geometry_resolution": 256,
        "geometry_hz": 3,
        "max_points": 60_000,
        "vr_refresh_hz": 72,
    },
    "4090": {
        "diffusion_resolution": 384,
        "diffusion_hz": 8,
        "geometry_resolution": 256,
        "geometry_hz": 5,
        "max_points": 100_000,
        "vr_refresh_hz": 90,
    },
    "5090": {
        "diffusion_resolution": 384,
        "diffusion_hz": 10,
        "geometry_resolution": 256,
        "geometry_hz": 6,
        "max_points": 150_000,
        "vr_refresh_hz": 90,
    },
    "custom": {
        "diffusion_resolution": 256,
        "diffusion_hz": 4,
        "geometry_resolution": 256,
        "geometry_hz": 3,
        "max_points": 50_000,
        "vr_refresh_hz": 60,
    },
}


def _finite_nonnegative(value: Any, field_name: str) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("%s must be a finite non-negative number" % field_name) from exc
    if not math.isfinite(numeric) or numeric < 0:
        raise ValueError("%s must be a finite non-negative number" % field_name)
    return numeric


def _round_multiple(value: float, multiple: int) -> int:
    return max(multiple, int(round(value / multiple)) * multiple)


@dataclass(frozen=True)
class QualityBounds:
    """The useful minimum and maximum workload for one hardware tier."""

    tier: str
    minimum: Mapping[str, int]
    maximum: Mapping[str, int]
    levels: int = 5

    def settings_for_level(self, level: int) -> dict[str, int]:
        if isinstance(level, bool) or not isinstance(level, int):
            raise ValueError("quality level must be an integer")
        if not 0 <= level < self.levels:
            raise ValueError("quality level must be between 0 and %d" % (self.levels - 1))
        ratio = level / float(self.levels - 1)
        settings: dict[str, int] = {}
        for key in QUALITY_KEYS:
            low = self.minimum[key]
            high = self.maximum[key]
            interpolated = low + ((high - low) * ratio)
            if level == 0:
                value = low
            elif level == self.levels - 1:
                value = high
            elif key in {"diffusion_resolution", "geometry_resolution"}:
                value = _round_multiple(interpolated, 64)
            elif key == "max_points":
                value = _round_multiple(interpolated, 1_000)
            else:
                value = int(round(interpolated))
            settings[key] = int(value)
        return settings

    def to_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "levels": self.levels,
            "minimum": dict(self.minimum),
            "maximum": dict(self.maximum),
        }


def quality_bounds_for_tier(tier: str, levels: int = 5) -> QualityBounds:
    """Return discrete adaptive bounds for a resolved hardware tier.

    ``auto`` cannot be accepted here because the governor must be deterministic;
    resolve it against the selected AI GPU before constructing the governor.
    """

    normalized = str(tier).strip().lower()
    if normalized == "auto":
        raise ValueError("adaptive quality requires a resolved tier, not auto")
    if normalized not in _TIER_MINIMUMS:
        raise ValueError("unsupported adaptive quality tier %r" % tier)
    if isinstance(levels, bool) or not isinstance(levels, int) or levels < 2:
        raise ValueError("levels must be an integer of at least 2")
    preset = preset_for(normalized)
    maximum = dict(preset.settings) if preset.settings else dict(_CUSTOM_MAXIMUM)
    minimum = dict(_TIER_MINIMUMS[normalized])
    for key in QUALITY_KEYS:
        if key not in maximum or key not in minimum:
            raise ValueError("tier %s is missing quality setting %s" % (normalized, key))
        if minimum[key] > maximum[key]:
            raise ValueError("tier %s has inverted bounds for %s" % (normalized, key))
    return QualityBounds(normalized, minimum, maximum, levels)


@dataclass(frozen=True)
class GovernorThresholds:
    """Pressure thresholds expressed as ratios of each configured budget."""

    frame_low: float = 0.82
    frame_high: float = 1.08
    vram_low: float = 0.76
    vram_high: float = 0.90
    queue_low: float = 0.55
    queue_high: float = 1.15
    critical_frame: float = 2.0
    critical_vram: float = 0.97
    critical_queue: float = 3.0

    def __post_init__(self) -> None:
        pairs = (
            ("frame", self.frame_low, self.frame_high),
            ("vram", self.vram_low, self.vram_high),
            ("queue", self.queue_low, self.queue_high),
        )
        for label, low, high in pairs:
            if not (0 <= low < high):
                raise ValueError("%s thresholds must satisfy 0 <= low < high" % label)
        if self.critical_frame <= self.frame_high:
            raise ValueError("critical_frame must exceed frame_high")
        if self.critical_vram <= self.vram_high:
            raise ValueError("critical_vram must exceed vram_high")
        if self.critical_queue <= self.queue_high:
            raise ValueError("critical_queue must exceed queue_high")


@dataclass(frozen=True)
class QualityState:
    tier: str
    level: int
    level_count: int
    settings: Mapping[str, int]

    @property
    def normalized_level(self) -> float:
        return self.level / float(self.level_count - 1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "level": self.level,
            "level_count": self.level_count,
            "normalized_level": self.normalized_level,
            "settings": dict(self.settings),
        }


@dataclass(frozen=True)
class AdaptiveDecision:
    """One governor observation and its resulting quality state."""

    sample_index: int
    timestamp: float
    changed: bool
    direction: str
    reason: str
    overloaded: bool
    healthy: bool
    pressures: Mapping[str, float]
    overload_streak: int
    healthy_streak: int
    cooldown_remaining: int
    state: QualityState

    def to_dict(self) -> dict[str, Any]:
        return {
            "sample_index": self.sample_index,
            "timestamp": self.timestamp,
            "changed": self.changed,
            "direction": self.direction,
            "reason": self.reason,
            "overloaded": self.overloaded,
            "healthy": self.healthy,
            "pressures": dict(self.pressures),
            "overload_streak": self.overload_streak,
            "healthy_streak": self.healthy_streak,
            "cooldown_remaining": self.cooldown_remaining,
            "state": self.state.to_dict(),
        }


class AdaptiveQualityGovernor:
    """A bounded, hysteretic quality controller.

    Degradation is intentionally quick while recovery is slow.  Samples inside
    the low/high dead-band reset both streaks, preventing a workload close to a
    threshold from continuously toggling between adjacent levels.  Critical
    pressure bypasses cooldown so the process can protect itself immediately.
    """

    def __init__(
        self,
        tier: str,
        *,
        frame_budget_ms: float = 1000.0 / 60.0,
        queue_budget_ms: float = 200.0,
        levels: int = 5,
        initial_level: int | None = None,
        down_window: int = 3,
        up_window: int = 120,
        cooldown_samples: int = 30,
        thresholds: GovernorThresholds | None = None,
    ) -> None:
        self.bounds = quality_bounds_for_tier(tier, levels)
        self.frame_budget_ms = _finite_nonnegative(frame_budget_ms, "frame_budget_ms")
        self.queue_budget_ms = _finite_nonnegative(queue_budget_ms, "queue_budget_ms")
        if self.frame_budget_ms == 0 or self.queue_budget_ms == 0:
            raise ValueError("frame and queue budgets must be greater than zero")
        for name, value in (
            ("down_window", down_window),
            ("up_window", up_window),
            ("cooldown_samples", cooldown_samples),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("%s must be a non-negative integer" % name)
        if down_window == 0 or up_window == 0:
            raise ValueError("down_window and up_window must be greater than zero")
        maximum_level = levels - 1
        selected_level = maximum_level if initial_level is None else initial_level
        if (
            isinstance(selected_level, bool)
            or not isinstance(selected_level, int)
            or not 0 <= selected_level <= maximum_level
        ):
            raise ValueError("initial_level must be between 0 and %d" % maximum_level)
        self.level = selected_level
        self.down_window = down_window
        self.up_window = up_window
        self.cooldown_samples = cooldown_samples
        self.thresholds = thresholds or GovernorThresholds()
        self.sample_index = 0
        self.overload_streak = 0
        self.healthy_streak = 0
        self.cooldown_remaining = 0

    @property
    def state(self) -> QualityState:
        return QualityState(
            tier=self.bounds.tier,
            level=self.level,
            level_count=self.bounds.levels,
            settings=self.bounds.settings_for_level(self.level),
        )

    def reset(self, level: int | None = None) -> QualityState:
        """Clear history, optionally selecting a new valid quality level."""

        if level is not None:
            self.bounds.settings_for_level(level)  # validation
            self.level = level
        self.sample_index = 0
        self.overload_streak = 0
        self.healthy_streak = 0
        self.cooldown_remaining = 0
        return self.state

    def observe(
        self,
        *,
        frame_time_ms: float,
        vram_used_mib: float,
        vram_total_mib: float,
        queue_age_ms: float,
        timestamp: float | None = None,
    ) -> AdaptiveDecision:
        """Consume one metrics sample and return a hold/up/down decision."""

        frame = _finite_nonnegative(frame_time_ms, "frame_time_ms")
        used = _finite_nonnegative(vram_used_mib, "vram_used_mib")
        total = _finite_nonnegative(vram_total_mib, "vram_total_mib")
        queue = _finite_nonnegative(queue_age_ms, "queue_age_ms")
        if total <= 0:
            raise ValueError("vram_total_mib must be greater than zero")
        now = time.time() if timestamp is None else _finite_nonnegative(timestamp, "timestamp")
        self.sample_index += 1
        # Capture cooldown state before evaluating this observation.  A value
        # of N means the next N complete observations are held; decrementing
        # before the gates would make cooldown_samples=1 hold zero samples.
        cooldown_active = self.cooldown_remaining > 0

        pressures = {
            "frame": frame / self.frame_budget_ms,
            "vram": used / total,
            "queue": queue / self.queue_budget_ms,
        }
        threshold = self.thresholds
        overloaded = (
            pressures["frame"] >= threshold.frame_high
            or pressures["vram"] >= threshold.vram_high
            or pressures["queue"] >= threshold.queue_high
        )
        healthy = (
            pressures["frame"] <= threshold.frame_low
            and pressures["vram"] <= threshold.vram_low
            and pressures["queue"] <= threshold.queue_low
        )
        critical_reason = ""
        if pressures["vram"] >= threshold.critical_vram:
            critical_reason = "critical_vram"
        elif pressures["queue"] >= threshold.critical_queue:
            critical_reason = "critical_queue"
        elif pressures["frame"] >= threshold.critical_frame:
            critical_reason = "critical_frame"

        if overloaded:
            self.overload_streak += 1
            self.healthy_streak = 0
        elif healthy:
            self.healthy_streak += 1
            self.overload_streak = 0
        else:
            self.overload_streak = 0
            self.healthy_streak = 0

        changed = False
        direction = "hold"
        reason = "hysteresis"
        maximum_level = self.bounds.levels - 1
        may_degrade = critical_reason or (
            self.overload_streak >= self.down_window and not cooldown_active
        )
        if overloaded and may_degrade:
            if self.level > 0:
                self.level -= 1
                changed = True
                direction = "down"
                reason = critical_reason or "sustained_overload"
                self.cooldown_remaining = self.cooldown_samples
                self.overload_streak = 0
            else:
                reason = "at_minimum"
        elif healthy and self.healthy_streak >= self.up_window:
            if cooldown_active:
                reason = "cooldown"
            elif self.level < maximum_level:
                self.level += 1
                changed = True
                direction = "up"
                reason = "sustained_headroom"
                self.cooldown_remaining = self.cooldown_samples
                self.healthy_streak = 0
            else:
                reason = "at_maximum"
        elif overloaded:
            reason = "cooldown" if cooldown_active else "overload_window"
        elif healthy:
            reason = "cooldown" if cooldown_active else "headroom_window"

        if not changed and self.cooldown_remaining > 0:
            self.cooldown_remaining -= 1

        return AdaptiveDecision(
            sample_index=self.sample_index,
            timestamp=now,
            changed=changed,
            direction=direction,
            reason=reason,
            overloaded=overloaded,
            healthy=healthy,
            pressures=pressures,
            overload_streak=self.overload_streak,
            healthy_streak=self.healthy_streak,
            cooldown_remaining=self.cooldown_remaining,
            state=self.state,
        )


@dataclass(frozen=True)
class TelemetrySample:
    """Portable metrics record used by live capture and offline replay."""

    timestamp: float
    frame_time_ms: float
    vram_used_mib: float
    vram_total_mib: float
    queue_age_ms: float
    quality_level: int
    tier: str = ""
    role: str = ""
    quality_direction: str = "hold"
    quality_reason: str = ""
    quality_settings: Mapping[str, int] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_version": 1,
            "timestamp": self.timestamp,
            "frame_time_ms": self.frame_time_ms,
            "vram_used_mib": self.vram_used_mib,
            "vram_total_mib": self.vram_total_mib,
            "queue_age_ms": self.queue_age_ms,
            "quality_level": self.quality_level,
            "tier": self.tier,
            "role": self.role,
            "quality_direction": self.quality_direction,
            "quality_reason": self.quality_reason,
            "quality_settings": dict(self.quality_settings),
        }
        if self.metadata:
            result["metadata"] = dict(self.metadata)
        return result


def _record_dict(record: TelemetrySample | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(record, TelemetrySample):
        return record.to_dict()
    if isinstance(record, Mapping):
        return dict(record)
    raise TypeError("telemetry record must be TelemetrySample or a mapping")


class TelemetryJsonlWriter:
    """Write newline-delimited telemetry with an explicit lifetime."""

    def __init__(self, path: str | os.PathLike[str], *, append: bool = False) -> None:
        self.path = Path(path)
        self.append = append
        self._handle: TextIO | None = None

    def __enter__(self) -> "TelemetryJsonlWriter":
        self.open()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def open(self) -> None:
        if self._handle is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a" if self.append else "w", encoding="utf-8", newline="\n")

    def write(self, record: TelemetrySample | Mapping[str, Any]) -> None:
        if self._handle is None:
            self.open()
        assert self._handle is not None
        payload = _record_dict(record)
        self._handle.write(json.dumps(payload, separators=(",", ":"), sort_keys=True))
        self._handle.write("\n")
        self._handle.flush()

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None


def read_telemetry_jsonl(path: str | os.PathLike[str]) -> Iterator[dict[str, Any]]:
    """Yield JSON objects, reporting malformed input with its line number."""

    source = Path(path)
    with source.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    "invalid telemetry JSON at %s:%d: %s" % (source, line_number, exc.msg)
                ) from exc
            if not isinstance(record, Mapping):
                raise ValueError(
                    "invalid telemetry JSON at %s:%d: record must be an object"
                    % (source, line_number)
                )
            yield dict(record)


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + ((ordered[upper] - ordered[lower]) * (position - lower))


def _metric_summary(values: list[float]) -> dict[str, float]:
    return {
        "min": min(values),
        "max": max(values),
        "mean": fmean(values),
        "p50": _percentile(values, 0.50),
        "p95": _percentile(values, 0.95),
        "p99": _percentile(values, 0.99),
    }


def summarize_telemetry(
    records: Iterable[TelemetrySample | Mapping[str, Any]],
) -> dict[str, Any]:
    """Summarize live or replay telemetry without NumPy or pandas."""

    rows = [_record_dict(record) for record in records]
    metric_names = (
        "frame_time_ms",
        "vram_used_mib",
        "vram_total_mib",
        "queue_age_ms",
        "quality_level",
    )
    values: dict[str, list[float]] = {name: [] for name in metric_names}
    timestamps: list[float] = []
    vram_utilization: list[float] = []
    quality_changes = 0
    prior_level: float | None = None
    for index, row in enumerate(rows, 1):
        row_values: dict[str, float] = {}
        for name in metric_names:
            if name not in row:
                continue
            try:
                value = float(row[name])
            except (TypeError, ValueError) as exc:
                raise ValueError("telemetry record %d field %s is not numeric" % (index, name)) from exc
            if not math.isfinite(value):
                raise ValueError("telemetry record %d field %s is not finite" % (index, name))
            values[name].append(value)
            row_values[name] = value
            if name == "quality_level":
                if prior_level is not None and value != prior_level:
                    quality_changes += 1
                prior_level = value
        if "timestamp" in row:
            timestamp = float(row["timestamp"])
            if math.isfinite(timestamp):
                timestamps.append(timestamp)
        if row_values.get("vram_total_mib", 0) > 0 and "vram_used_mib" in row_values:
            vram_utilization.append(
                row_values["vram_used_mib"] / row_values["vram_total_mib"]
            )

    metrics = {name: _metric_summary(items) for name, items in values.items() if items}
    frame_times = values["frame_time_ms"]
    if frame_times:
        fps_values = [1000.0 / value for value in frame_times if value > 0]
        if fps_values:
            metrics["estimated_fps"] = _metric_summary(fps_values)
    if vram_utilization:
        metrics["vram_utilization"] = _metric_summary(vram_utilization)
    return {
        "schema_version": 1,
        "count": len(rows),
        "started_at": min(timestamps) if timestamps else None,
        "ended_at": max(timestamps) if timestamps else None,
        "duration_s": (max(timestamps) - min(timestamps)) if len(timestamps) >= 2 else 0.0,
        "quality_changes": quality_changes,
        "metrics": metrics,
    }


def write_telemetry_summary(
    path: str | os.PathLike[str], summary: Mapping[str, Any]
) -> None:
    """Atomically write a JSON summary next to its final destination."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(".%s.%s.tmp" % (destination.name, uuid.uuid4().hex))
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(dict(summary), handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


__all__ = [
    "AdaptiveDecision",
    "AdaptiveQualityGovernor",
    "GovernorThresholds",
    "QualityBounds",
    "QualityState",
    "TelemetryJsonlWriter",
    "TelemetrySample",
    "quality_bounds_for_tier",
    "read_telemetry_jsonl",
    "summarize_telemetry",
    "write_telemetry_summary",
]
