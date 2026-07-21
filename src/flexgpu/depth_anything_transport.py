"""Bounded Depth Anything sensor frames carried by WorldBus v1.

The payload is one RGBA8 image with no RGB camera data:

* R/G: big-endian uint16 pseudo-metre depth;
* B: binary foreground mask;
* A: 8-bit heuristic confidence.

Packed zero is reserved for invalid/background pixels.  ``packed * scale +
bias`` converts valid samples to the session's pseudo-metre slab.  This module
uses only the Python standard library so the external worker, TouchDesigner
adapter, diagnostics, and tests can share the exact wire contract.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Sequence, Tuple


SENSOR_FRAME_CONTRACT = "flexgpu-depth-anything-sensor/v1"
SENSOR_CONTRACT = SENSOR_FRAME_CONTRACT
DEPTH_ENCODING = "uint16_big_endian_scale_bias"
DEPTH_SEMANTICS = "sensor_optical_z_pseudo_metres"
MASK_SEMANTICS = "binary_foreground_in_pseudo_metre_slab"
CONFIDENCE_SEMANTICS = "uint8_frozen_range_proxy_not_model_probability"
MAX_UINT16 = 65535
# Keep the producer codec's hard allocation ceiling identical to the
# TouchDesigner receiver.  Width and height checks alone are insufficient: a
# 640x480 frame is the largest supported aggregate allocation.
MAX_WIDTH = 640
MAX_HEIGHT = 480
MAX_PIXELS = 640 * 480
IDENTITY_4X4 = (
    1.0, 0.0, 0.0, 0.0,
    0.0, 1.0, 0.0, 0.0,
    0.0, 0.0, 1.0, 0.0,
    0.0, 0.0, 0.0, 1.0,
)


class DepthAnythingTransportError(ValueError):
    """A sensor payload or metadata field violates the bounded contract."""


@dataclass(frozen=True)
class SensorPackStats:
    total_pixels: int
    valid_pixels: int
    background_pixels: int
    invalid_depth_pixels: int
    confidence_rejected_pixels: int
    near_clipped_pixels: int
    far_clipped_pixels: int

    @property
    def valid_fraction(self) -> float:
        return self.valid_pixels / float(self.total_pixels)

    def to_extensions(self) -> dict[str, Any]:
        return {
            "depth_anything_valid_fraction": self.valid_fraction,
            "depth_anything_valid_pixels": self.valid_pixels,
            "depth_anything_background_pixels": self.background_pixels,
            "depth_anything_invalid_depth_pixels": self.invalid_depth_pixels,
            "depth_anything_confidence_rejected_pixels": self.confidence_rejected_pixels,
            "depth_anything_near_clipped_pixels": self.near_clipped_pixels,
            "depth_anything_far_clipped_pixels": self.far_clipped_pixels,
        }


@dataclass(frozen=True)
class PackedSensorFrame:
    width: int
    height: int
    depth_scale: float
    depth_bias: float
    payload: bytes
    stats: SensorPackStats


@dataclass(frozen=True)
class DecodedSensorFrame:
    width: int
    height: int
    packed_depth: Tuple[int, ...]
    depth_metres: Tuple[float, ...]
    foreground_mask: Tuple[bool, ...]
    confidence: Tuple[float, ...]


def _require_int(value: object, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise DepthAnythingTransportError(f"{label} must be an integer >= {minimum}")
    return value


def _require_finite(value: object, label: str) -> float:
    if isinstance(value, bool):
        raise DepthAnythingTransportError(label + " must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise DepthAnythingTransportError(label + " must be a finite number") from exc
    if not math.isfinite(number):
        raise DepthAnythingTransportError(label + " must be a finite number")
    return number


def _dimensions(width: object, height: object) -> tuple[int, int, int]:
    columns = _require_int(width, "width", 1)
    rows = _require_int(height, "height", 1)
    if columns > MAX_WIDTH:
        raise DepthAnythingTransportError(f"width {columns} exceeds limit {MAX_WIDTH}")
    if rows > MAX_HEIGHT:
        raise DepthAnythingTransportError(f"height {rows} exceeds limit {MAX_HEIGHT}")
    pixels = columns * rows
    if pixels > MAX_PIXELS:
        raise DepthAnythingTransportError(
            f"frame has {pixels} pixels; limit is {MAX_PIXELS}"
        )
    return columns, rows, pixels


def _calibration(scale: object, bias: object) -> tuple[float, float]:
    normalized_scale = _require_finite(scale, "depth_scale")
    normalized_bias = _require_finite(bias, "depth_bias")
    if normalized_scale <= 0.0:
        raise DepthAnythingTransportError("depth_scale must be greater than zero")
    if normalized_bias < 0.0:
        raise DepthAnythingTransportError("depth_bias must be non-negative")
    if not math.isfinite(normalized_bias + normalized_scale * MAX_UINT16):
        raise DepthAnythingTransportError("depth calibration range must remain finite")
    return normalized_scale, normalized_bias


def _iterator(value: object, label: str) -> object:
    if isinstance(value, (str, bytes, bytearray)):
        raise DepthAnythingTransportError(label + " must be an iterable of samples")
    try:
        return iter(value)  # type: ignore[arg-type]
    except TypeError as exc:
        raise DepthAnythingTransportError(label + " must be iterable") from exc


def _next(iterator: object, label: str, index: int) -> object:
    try:
        return next(iterator)  # type: ignore[arg-type]
    except StopIteration as exc:
        raise DepthAnythingTransportError(
            f"{label} contains fewer samples than expected; missing index {index}"
        ) from exc


def _no_extra(iterator: object, label: str, expected: int) -> None:
    try:
        next(iterator)  # type: ignore[arg-type]
    except StopIteration:
        return
    raise DepthAnythingTransportError(
        f"{label} contains more samples than expected {expected}"
    )


def _sample(value: object, *, allow_bool: bool = False) -> Optional[float]:
    if isinstance(value, bool):
        return float(value) if allow_bool else None
    if isinstance(value, (str, bytes, bytearray)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def pack_sensor_frame(
    depth_metres: Iterable[object],
    foreground_mask: Iterable[object],
    confidence: Iterable[object],
    *,
    width: int,
    height: int,
    depth_scale: float = 0.001,
    depth_bias: float = 0.0,
) -> PackedSensorFrame:
    """Encode pseudo-depth, foreground, and confidence without camera RGB."""

    columns, rows, pixel_count = _dimensions(width, height)
    scale, bias = _calibration(depth_scale, depth_bias)
    depth_iter = _iterator(depth_metres, "depth_metres")
    mask_iter = _iterator(foreground_mask, "foreground_mask")
    confidence_iter = _iterator(confidence, "confidence")
    payload = bytearray(pixel_count * 4)
    valid = background = invalid_depth = rejected_confidence = 0
    near_clipped = far_clipped = 0

    for index in range(pixel_count):
        depth_value = _sample(_next(depth_iter, "depth_metres", index))
        mask_value = _sample(
            _next(mask_iter, "foreground_mask", index), allow_bool=True
        )
        confidence_value = _sample(_next(confidence_iter, "confidence", index))
        if mask_value is None or mask_value <= 0.5:
            background += 1
            continue
        if depth_value is None or depth_value <= 0.0:
            invalid_depth += 1
            continue
        if confidence_value is None or confidence_value <= 0.0:
            rejected_confidence += 1
            continue

        unscaled = (depth_value - bias) / scale
        if unscaled < 1.0:
            packed = 1
            near_clipped += 1
        elif unscaled > MAX_UINT16:
            packed = MAX_UINT16
            far_clipped += 1
        else:
            packed = max(1, min(MAX_UINT16, int(math.floor(unscaled + 0.5))))
        confidence_byte = max(
            1, min(255, int(math.floor(min(1.0, confidence_value) * 255.0 + 0.5)))
        )
        cursor = index * 4
        payload[cursor] = (packed >> 8) & 255
        payload[cursor + 1] = packed & 255
        payload[cursor + 2] = 255
        payload[cursor + 3] = confidence_byte
        valid += 1

    _no_extra(depth_iter, "depth_metres", pixel_count)
    _no_extra(mask_iter, "foreground_mask", pixel_count)
    _no_extra(confidence_iter, "confidence", pixel_count)
    stats = SensorPackStats(
        total_pixels=pixel_count,
        valid_pixels=valid,
        background_pixels=background,
        invalid_depth_pixels=invalid_depth,
        confidence_rejected_pixels=rejected_confidence,
        near_clipped_pixels=near_clipped,
        far_clipped_pixels=far_clipped,
    )
    return PackedSensorFrame(
        width=columns,
        height=rows,
        depth_scale=scale,
        depth_bias=bias,
        payload=bytes(payload),
        stats=stats,
    )


def pack_sensor_frame_numpy(
    depth_metres: object,
    foreground_mask: object,
    confidence: object,
    *,
    width: int,
    height: int,
    depth_scale: float = 0.001,
    depth_bias: float = 0.0,
) -> PackedSensorFrame:
    """NumPy-accelerated equivalent used by the external webcam worker."""

    try:
        import numpy as np
    except ImportError as exc:  # pragma: no cover - minimal installation guard
        raise DepthAnythingTransportError(
            "pack_sensor_frame_numpy requires NumPy"
        ) from exc

    columns, rows, pixel_count = _dimensions(width, height)
    scale, bias = _calibration(depth_scale, depth_bias)
    try:
        depths = np.asarray(depth_metres)
        masks = np.asarray(foreground_mask)
        confidences = np.asarray(confidence)
    except (TypeError, ValueError, OverflowError) as exc:
        raise DepthAnythingTransportError("sensor planes must be numeric arrays") from exc
    for label, values in (
        ("depth_metres", depths),
        ("foreground_mask", masks),
        ("confidence", confidences),
    ):
        if values.size != pixel_count:
            raise DepthAnythingTransportError(
                f"{label} contains {values.size} samples; expected {pixel_count}"
            )
        if values.dtype.kind not in "bifu":
            raise DepthAnythingTransportError(label + " must contain numeric samples")
    try:
        depths = depths.reshape(-1).astype(np.float64, copy=False)
        masks = masks.reshape(-1).astype(np.float64, copy=False)
        confidences = confidences.reshape(-1).astype(np.float64, copy=False)
    except (TypeError, ValueError, OverflowError) as exc:
        raise DepthAnythingTransportError("sensor planes must be numeric arrays") from exc

    mask_valid = np.isfinite(masks) & (masks > 0.5)
    depth_valid = np.isfinite(depths) & (depths > 0.0)
    confidence_valid = np.isfinite(confidences) & (confidences > 0.0)
    valid = mask_valid & depth_valid & confidence_valid
    unscaled = np.zeros(pixel_count, dtype=np.float64)
    with np.errstate(over="ignore", invalid="ignore"):
        unscaled[valid] = (depths[valid] - bias) / scale
    near = valid & (unscaled < 1.0)
    far = valid & (unscaled > MAX_UINT16)
    middle = valid & ~near & ~far
    packed = np.zeros(pixel_count, dtype=np.uint16)
    packed[near] = 1
    packed[far] = MAX_UINT16
    if bool(np.any(middle)):
        packed[middle] = np.clip(
            np.floor(unscaled[middle] + 0.5), 1, MAX_UINT16
        ).astype(np.uint16)
    confidence_bytes = np.zeros(pixel_count, dtype=np.uint8)
    if bool(np.any(valid)):
        encoded_confidence = np.floor(
            np.clip(confidences[valid], 0.0, 1.0) * 255.0 + 0.5
        )
        confidence_bytes[valid] = np.maximum(1, encoded_confidence).astype(np.uint8)

    encoded = np.zeros((pixel_count, 4), dtype=np.uint8)
    encoded[:, 0] = (packed >> 8).astype(np.uint8)
    encoded[:, 1] = (packed & 255).astype(np.uint8)
    encoded[:, 2] = valid.astype(np.uint8) * 255
    encoded[:, 3] = confidence_bytes
    stats = SensorPackStats(
        total_pixels=pixel_count,
        valid_pixels=int(np.count_nonzero(valid)),
        background_pixels=int(np.count_nonzero(~mask_valid)),
        invalid_depth_pixels=int(np.count_nonzero(mask_valid & ~depth_valid)),
        confidence_rejected_pixels=int(
            np.count_nonzero(mask_valid & depth_valid & ~confidence_valid)
        ),
        near_clipped_pixels=int(np.count_nonzero(near)),
        far_clipped_pixels=int(np.count_nonzero(far)),
    )
    return PackedSensorFrame(
        width=columns,
        height=rows,
        depth_scale=scale,
        depth_bias=bias,
        payload=encoded.tobytes(order="C"),
        stats=stats,
    )


def decode_sensor_frame(
    payload: bytes | bytearray | memoryview,
    *,
    width: int,
    height: int,
    depth_scale: float = 0.001,
    depth_bias: float = 0.0,
    strict: bool = True,
) -> DecodedSensorFrame:
    """Decode and optionally enforce invalid-pixel fail-closed invariants."""

    columns, rows, pixel_count = _dimensions(width, height)
    scale, bias = _calibration(depth_scale, depth_bias)
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise DepthAnythingTransportError("payload must be bytes-like")
    encoded = bytes(payload)
    if len(encoded) != pixel_count * 4:
        raise DepthAnythingTransportError(
            f"payload contains {len(encoded)} bytes; expected {pixel_count * 4}"
        )
    packed_values: list[int] = []
    depths: list[float] = []
    masks: list[bool] = []
    confidences: list[float] = []
    for index in range(pixel_count):
        cursor = index * 4
        packed = (encoded[cursor] << 8) | encoded[cursor + 1]
        mask_byte = encoded[cursor + 2]
        confidence_byte = encoded[cursor + 3]
        if strict and mask_byte not in (0, 255):
            raise DepthAnythingTransportError("foreground mask must be binary")
        mask = mask_byte != 0
        if strict and not mask and (packed != 0 or confidence_byte != 0):
            raise DepthAnythingTransportError(
                "background pixels must have zero depth and confidence"
            )
        if strict and mask and (packed == 0 or confidence_byte == 0):
            raise DepthAnythingTransportError(
                "foreground pixels must have positive depth and confidence"
            )
        packed_values.append(packed)
        depths.append(packed * scale + bias if mask else 0.0)
        masks.append(mask)
        confidences.append(confidence_byte / 255.0 if mask else 0.0)
    return DecodedSensorFrame(
        width=columns,
        height=rows,
        packed_depth=tuple(packed_values),
        depth_metres=tuple(depths),
        foreground_mask=tuple(masks),
        confidence=tuple(confidences),
    )


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise DepthAnythingTransportError(label + " must be a non-empty string")
    if any(ord(character) < 32 for character in value):
        raise DepthAnythingTransportError(label + " cannot contain control characters")
    return value


def _finite_sequence(value: Sequence[object], label: str, length: int) -> list[float]:
    if isinstance(value, (str, bytes, bytearray)) or len(value) != length:
        raise DepthAnythingTransportError(f"{label} must contain {length} numbers")
    return [_require_finite(item, label) for item in value]


def make_sensor_worldbus_metadata(
    frame: PackedSensorFrame,
    *,
    frame_id: int,
    capture_timestamp_ns: int,
    intrinsics: Sequence[object],
    camera_to_world: Sequence[object],
    generation_id: str,
    producer_session_id: str,
    sensor_calibration_id: str,
    sensor_calibration_digest: str,
    model_id: str,
    model_revision: str,
    calibration_mode: str,
    raw_order: str,
    raw_percentiles: Sequence[object],
    raw_bounds: Sequence[object],
    pseudo_metre_slab: Sequence[object],
    foreground_far_m: float,
    capture_source: str,
    inference_ms: float,
    extra_extensions: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Build WorldBus metadata whose timestamp is the camera capture time."""

    if not isinstance(frame, PackedSensorFrame):
        raise DepthAnythingTransportError("frame must be a PackedSensorFrame")
    output_frame_id = _require_int(frame_id, "frame_id")
    timestamp = _require_int(capture_timestamp_ns, "capture_timestamp_ns", 1)
    intrinsics_values = _finite_sequence(intrinsics, "intrinsics", 4)
    transform_values = _finite_sequence(camera_to_world, "camera_to_world", 16)
    if tuple(transform_values) != IDENTITY_4X4:
        raise DepthAnythingTransportError(
            "camera_to_world must be identity; apply measured sensor placement in TouchDesigner"
        )
    percentile_values = _finite_sequence(raw_percentiles, "raw_percentiles", 2)
    raw_bound_values = _finite_sequence(raw_bounds, "raw_bounds", 2)
    slab_values = _finite_sequence(pseudo_metre_slab, "pseudo_metre_slab", 2)
    foreground_far = _require_finite(foreground_far_m, "foreground_far_m")
    elapsed_ms = _require_finite(inference_ms, "inference_ms")
    mode = _text(calibration_mode, "calibration_mode")
    order = _text(raw_order, "raw_order")
    if mode not in {"fixed", "session_frozen"}:
        raise DepthAnythingTransportError("calibration_mode is unsupported")
    if order not in {"near_is_larger", "near_is_smaller"}:
        raise DepthAnythingTransportError("raw_order is unsupported")
    if not 0.0 <= percentile_values[0] < percentile_values[1] <= 100.0:
        raise DepthAnythingTransportError("raw_percentiles must be increasing in [0, 100]")
    if not raw_bound_values[0] < raw_bound_values[1]:
        raise DepthAnythingTransportError("raw_bounds must be increasing")
    if not 0.0 < slab_values[0] < slab_values[1]:
        raise DepthAnythingTransportError("pseudo_metre_slab must be positive and increasing")
    if not slab_values[0] <= foreground_far <= slab_values[1]:
        raise DepthAnythingTransportError(
            "foreground_far_m must stay inside the pseudo-metre slab"
        )
    if elapsed_ms < 0.0:
        raise DepthAnythingTransportError("inference_ms cannot be negative")

    calibration_id = _text(sensor_calibration_id, "sensor_calibration_id")
    calibration_digest = _text(
        sensor_calibration_digest, "sensor_calibration_digest"
    ).lower()
    if len(calibration_digest) != 64 or any(
        character not in "0123456789abcdef" for character in calibration_digest
    ):
        raise DepthAnythingTransportError(
            "sensor_calibration_digest must be 64 lowercase SHA-256 hex characters"
        )
    extensions: dict[str, Any] = {
        "producer_session_id": _text(producer_session_id, "producer_session_id"),
        "sensor_frame_id": output_frame_id,
        "sensor_capture_timestamp_ns": str(timestamp),
        "sensor_calibration_id": calibration_id,
        "sensor_calibration_digest": calibration_digest,
        "depth_anything_contract": SENSOR_FRAME_CONTRACT,
        "sensor_role": "audience_interaction_only",
        "depth_anything_depth_encoding": DEPTH_ENCODING,
        "depth_anything_depth_semantics": DEPTH_SEMANTICS,
        "depth_anything_mask_semantics": MASK_SEMANTICS,
        "depth_anything_confidence_semantics": CONFIDENCE_SEMANTICS,
        "depth_anything_calibration_mode": mode,
        "depth_anything_raw_order": order,
        "depth_anything_raw_percentiles": percentile_values,
        "depth_anything_raw_bounds": raw_bound_values,
        "depth_anything_pseudo_metre_slab": slab_values,
        "depth_anything_foreground_far_m": foreground_far,
        "depth_anything_capture_source": _text(capture_source, "capture_source"),
        "depth_anything_model_id": _text(model_id, "model_id"),
        "depth_anything_model_revision": _text(model_revision, "model_revision"),
        "depth_anything_inference_ms": elapsed_ms,
        "depth_anything_intrinsics_source": "assumed_horizontal_fov_not_measured",
        "depth_anything_contains_rgb": False,
    }
    extensions.update(frame.stats.to_extensions())
    if extra_extensions is not None:
        if not isinstance(extra_extensions, Mapping):
            raise DepthAnythingTransportError("extra_extensions must be a mapping")
        core_fields = {
            "worldbus_version",
            "frame_id",
            "timestamp_ns",
            "width",
            "height",
            "pixel_format",
            "payload_bytes",
            "intrinsics",
            "depth_scale_bias",
            "camera_to_world",
            "generation_id",
        }
        reserved = (set(extensions) | core_fields).intersection(extra_extensions)
        if reserved:
            raise DepthAnythingTransportError(
                "extra_extensions cannot replace reserved keys: "
                + ", ".join(sorted(reserved))
            )
        extensions.update(dict(extra_extensions))
    return {
        "worldbus_version": 1,
        "frame_id": output_frame_id,
        "timestamp_ns": str(timestamp),
        "width": frame.width,
        "height": frame.height,
        "pixel_format": "rgba8",
        "payload_bytes": len(frame.payload),
        "intrinsics": intrinsics_values,
        "depth_scale_bias": [frame.depth_scale, frame.depth_bias],
        "camera_to_world": transform_values,
        "generation_id": _text(generation_id, "generation_id"),
        **extensions,
    }


__all__ = [
    "CONFIDENCE_SEMANTICS",
    "DEPTH_ENCODING",
    "DEPTH_SEMANTICS",
    "DecodedSensorFrame",
    "DepthAnythingTransportError",
    "MASK_SEMANTICS",
    "MAX_HEIGHT",
    "MAX_PIXELS",
    "MAX_WIDTH",
    "PackedSensorFrame",
    "SENSOR_FRAME_CONTRACT",
    "SENSOR_CONTRACT",
    "SensorPackStats",
    "decode_sensor_frame",
    "make_sensor_worldbus_metadata",
    "pack_sensor_frame",
    "pack_sensor_frame_numpy",
]
