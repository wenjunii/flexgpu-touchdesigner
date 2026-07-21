"""Dependency-free MoGe-2 packing for the WorldBus ``rgba8_atlas`` format.

The atlas is row-major RGBA8 with two equally sized planes.  The left plane is
the exact RGBA source image used for inference.  The right plane stores
big-endian uint16 depth in R/G, a binary validity mask in B, and the same binary
validity signal as a confidence proxy in A.

Packed depth is converted to metres with ``packed * scale + bias``.  Packed
zero is reserved for invalid pixels, so a valid sample is always in the range
1..65535.  This module deliberately uses only the Python standard library so it
can be shared by the external worker, diagnostics, and tests without importing
the MoGe/PyTorch runtime.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Sequence, Tuple


ATLAS_CONTRACT = "flexgpu-moge2-atlas/v1"
DEPTH_ENCODING = "uint16_big_endian_scale_bias"
DEPTH_SEMANTICS = "positive_optical_z_metres"
MASK_SEMANTICS = "binary_validity"
CONFIDENCE_SEMANTICS = "binary_validity_proxy"
MAX_UINT16 = 65535
MAX_ATLAS_WIDTH = 8192
MAX_ATLAS_HEIGHT = 8192
MAX_ATLAS_PIXELS = 16 * 1024 * 1024


class MoGe2TransportError(ValueError):
    """A MoGe-2 atlas input or calibration value is invalid."""


@dataclass(frozen=True)
class AtlasPackStats:
    """Disjoint validity counters plus valid-pixel clipping counters."""

    total_pixels: int
    valid_pixels: int
    masked_pixels: int
    invalid_depth_pixels: int
    near_clipped_pixels: int
    far_clipped_pixels: int

    @property
    def valid_fraction(self) -> float:
        return self.valid_pixels / float(self.total_pixels)

    def to_extensions(self) -> dict[str, Any]:
        """Return bounded JSON-compatible WorldBus extension fields."""

        return {
            "moge2_valid_fraction": self.valid_fraction,
            "moge2_valid_pixels": self.valid_pixels,
            "moge2_masked_pixels": self.masked_pixels,
            "moge2_invalid_depth_pixels": self.invalid_depth_pixels,
            "moge2_near_clipped_pixels": self.near_clipped_pixels,
            "moge2_far_clipped_pixels": self.far_clipped_pixels,
        }


@dataclass(frozen=True)
class PackedMoGe2Atlas:
    """One encoded atlas and the calibration needed to interpret it."""

    source_width: int
    height: int
    depth_scale: float
    depth_bias: float
    payload: bytes
    stats: AtlasPackStats

    @property
    def atlas_width(self) -> int:
        return self.source_width * 2


@dataclass(frozen=True)
class DecodedMoGe2Atlas:
    """Standard-library representation returned by the test/diagnostic decoder."""

    source_width: int
    height: int
    source_rgba: bytes
    packed_depth: Tuple[int, ...]
    depth_metres: Tuple[float, ...]
    mask: Tuple[bool, ...]
    confidence: Tuple[bool, ...]


def _require_int(value: object, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise MoGe2TransportError(f"{label} must be an integer >= {minimum}")
    return value


def _require_finite(value: object, label: str) -> float:
    if isinstance(value, bool):
        raise MoGe2TransportError(label + " must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MoGe2TransportError(label + " must be a finite number") from exc
    if not math.isfinite(number):
        raise MoGe2TransportError(label + " must be a finite number")
    return number


def _require_dimensions(source_width: object, height: object) -> tuple[int, int, int]:
    width = _require_int(source_width, "source_width", 1)
    rows = _require_int(height, "height", 1)
    atlas_width = width * 2
    if atlas_width > MAX_ATLAS_WIDTH:
        raise MoGe2TransportError(
            f"atlas width {atlas_width} exceeds limit {MAX_ATLAS_WIDTH}"
        )
    if rows > MAX_ATLAS_HEIGHT:
        raise MoGe2TransportError(f"height {rows} exceeds limit {MAX_ATLAS_HEIGHT}")
    atlas_pixels = atlas_width * rows
    if atlas_pixels > MAX_ATLAS_PIXELS:
        raise MoGe2TransportError(
            f"atlas has {atlas_pixels} pixels; limit is {MAX_ATLAS_PIXELS}"
        )
    return width, rows, width * rows


def _require_calibration(
    depth_scale: object, depth_bias: object
) -> tuple[float, float]:
    scale = _require_finite(depth_scale, "depth_scale")
    bias = _require_finite(depth_bias, "depth_bias")
    if scale <= 0.0:
        raise MoGe2TransportError("depth_scale must be greater than zero")
    if bias < 0.0:
        raise MoGe2TransportError(
            "depth_bias must be non-negative for positive optical-Z depth"
        )
    if not math.isfinite(bias + scale * MAX_UINT16):
        raise MoGe2TransportError("depth calibration range must remain finite")
    return scale, bias


def _finite_sample(value: object, *, allow_bool: bool = False) -> Optional[float]:
    if isinstance(value, bool):
        return float(value) if allow_bool else None
    if isinstance(value, (str, bytes, bytearray)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _next_sample(iterator: object, label: str, index: int) -> object:
    try:
        return next(iterator)  # type: ignore[arg-type]
    except StopIteration as exc:
        raise MoGe2TransportError(
            f"{label} contains fewer samples than the expected pixel count; "
            f"missing index {index}"
        ) from exc


def _reject_extra_sample(iterator: object, label: str, expected: int) -> None:
    try:
        next(iterator)  # type: ignore[arg-type]
    except StopIteration:
        return
    raise MoGe2TransportError(
        f"{label} contains more samples than the expected pixel count {expected}"
    )


def pack_moge2_atlas(
    source_rgba: bytes | bytearray | memoryview,
    depth_metres: Iterable[object],
    mask: Iterable[object],
    *,
    source_width: int,
    height: int,
    depth_scale: float = 0.001,
    depth_bias: float = 0.0,
    mask_threshold: float = 0.5,
) -> PackedMoGe2Atlas:
    """Pack one exact source image and MoGe metric depth into WorldBus RGBA8.

    Non-finite/non-positive depths and non-finite or threshold-rejected mask
    samples become fully invalid pixels (packed depth, B, and A are all zero).
    Valid depths outside the uint16 calibration range are safely clamped and
    counted in :class:`AtlasPackStats`.
    """

    width, rows, pixel_count = _require_dimensions(source_width, height)
    scale, bias = _require_calibration(depth_scale, depth_bias)
    threshold = _require_finite(mask_threshold, "mask_threshold")
    if threshold < 0.0 or threshold > 1.0:
        raise MoGe2TransportError("mask_threshold must be between 0 and 1")
    if not isinstance(source_rgba, (bytes, bytearray, memoryview)):
        raise MoGe2TransportError("source_rgba must be bytes-like")
    rgba = bytes(source_rgba)
    expected_rgba_bytes = pixel_count * 4
    if len(rgba) != expected_rgba_bytes:
        raise MoGe2TransportError(
            f"source_rgba contains {len(rgba)} bytes; expected {expected_rgba_bytes}"
        )
    try:
        depth_iterator = iter(depth_metres)
    except TypeError as exc:
        raise MoGe2TransportError("depth_metres must be iterable") from exc
    try:
        mask_iterator = iter(mask)
    except TypeError as exc:
        raise MoGe2TransportError("mask must be iterable") from exc

    payload = bytearray(pixel_count * 8)
    valid_pixels = 0
    masked_pixels = 0
    invalid_depth_pixels = 0
    near_clipped_pixels = 0
    far_clipped_pixels = 0
    source_row_bytes = width * 4
    atlas_row_bytes = width * 8

    for y in range(rows):
        source_start = y * source_row_bytes
        atlas_start = y * atlas_row_bytes
        payload[atlas_start : atlas_start + source_row_bytes] = rgba[
            source_start : source_start + source_row_bytes
        ]
        right_start = atlas_start + source_row_bytes
        for x in range(width):
            index = y * width + x
            depth_sample = _next_sample(depth_iterator, "depth_metres", index)
            mask_sample = _next_sample(mask_iterator, "mask", index)
            mask_value = _finite_sample(mask_sample, allow_bool=True)
            depth_value = _finite_sample(depth_sample)
            cursor = right_start + x * 4

            if mask_value is None or mask_value <= threshold:
                masked_pixels += 1
                continue
            if depth_value is None or depth_value <= 0.0:
                invalid_depth_pixels += 1
                continue

            unscaled = (depth_value - bias) / scale
            if unscaled < 1.0:
                packed_depth = 1
                near_clipped_pixels += 1
            elif unscaled > MAX_UINT16:
                packed_depth = MAX_UINT16
                far_clipped_pixels += 1
            else:
                packed_depth = int(math.floor(unscaled + 0.5))
                packed_depth = max(1, min(MAX_UINT16, packed_depth))
            payload[cursor] = (packed_depth >> 8) & 255
            payload[cursor + 1] = packed_depth & 255
            payload[cursor + 2] = 255
            payload[cursor + 3] = 255
            valid_pixels += 1

    _reject_extra_sample(depth_iterator, "depth_metres", pixel_count)
    _reject_extra_sample(mask_iterator, "mask", pixel_count)
    stats = AtlasPackStats(
        total_pixels=pixel_count,
        valid_pixels=valid_pixels,
        masked_pixels=masked_pixels,
        invalid_depth_pixels=invalid_depth_pixels,
        near_clipped_pixels=near_clipped_pixels,
        far_clipped_pixels=far_clipped_pixels,
    )
    return PackedMoGe2Atlas(
        source_width=width,
        height=rows,
        depth_scale=scale,
        depth_bias=bias,
        payload=bytes(payload),
        stats=stats,
    )


def pack_moge2_atlas_numpy(
    source_rgba: bytes | bytearray | memoryview,
    depth_metres: object,
    mask: object,
    *,
    source_width: int,
    height: int,
    depth_scale: float = 0.001,
    depth_bias: float = 0.0,
    mask_threshold: float = 0.5,
) -> PackedMoGe2Atlas:
    """NumPy-accelerated equivalent of :func:`pack_moge2_atlas`.

    NumPy is imported only when this function is called; it is not a base
    dependency of the codec.  Numeric arrays or finite numeric sequences are
    accepted and flattened in row-major order.  The result is byte-for-byte
    compatible with the standard-library encoder.
    """

    try:
        import numpy as np
    except ImportError as exc:  # pragma: no cover - exercised in minimal installs
        raise MoGe2TransportError(
            "pack_moge2_atlas_numpy requires the optional numpy package"
        ) from exc

    width, rows, pixel_count = _require_dimensions(source_width, height)
    scale, bias = _require_calibration(depth_scale, depth_bias)
    threshold = _require_finite(mask_threshold, "mask_threshold")
    if threshold < 0.0 or threshold > 1.0:
        raise MoGe2TransportError("mask_threshold must be between 0 and 1")
    if not isinstance(source_rgba, (bytes, bytearray, memoryview)):
        raise MoGe2TransportError("source_rgba must be bytes-like")
    rgba = bytes(source_rgba)
    expected_rgba_bytes = pixel_count * 4
    if len(rgba) != expected_rgba_bytes:
        raise MoGe2TransportError(
            f"source_rgba contains {len(rgba)} bytes; expected {expected_rgba_bytes}"
        )

    try:
        depth_values = np.asarray(depth_metres)
        mask_values = np.asarray(mask)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MoGe2TransportError("depth_metres and mask must be numeric arrays") from exc
    if depth_values.size != pixel_count:
        raise MoGe2TransportError(
            f"depth_metres contains {depth_values.size} samples; expected {pixel_count}"
        )
    if mask_values.size != pixel_count:
        raise MoGe2TransportError(
            f"mask contains {mask_values.size} samples; expected {pixel_count}"
        )
    if depth_values.dtype.kind not in "fiu" or mask_values.dtype.kind not in "bfiu":
        raise MoGe2TransportError("depth_metres and mask must contain numeric samples")

    try:
        depth_values = depth_values.reshape(-1).astype(np.float64, copy=False)
        mask_values = mask_values.reshape(-1).astype(np.float64, copy=False)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MoGe2TransportError("depth_metres and mask must contain numeric samples") from exc

    mask_valid = np.isfinite(mask_values) & (mask_values > threshold)
    depth_valid = np.isfinite(depth_values) & (depth_values > 0.0)
    valid = mask_valid & depth_valid
    minimum_depth = bias + scale
    maximum_depth = bias + scale * MAX_UINT16
    near = valid & (depth_values < minimum_depth)
    far = valid & (depth_values > maximum_depth)
    middle = valid & ~near & ~far

    packed_depth = np.zeros(pixel_count, dtype=np.uint16)
    packed_depth[near] = 1
    packed_depth[far] = MAX_UINT16
    if bool(np.any(middle)):
        with np.errstate(over="ignore", invalid="ignore"):
            quantized = np.floor(
                (depth_values[middle] - bias) / scale + 0.5
            )
        packed_depth[middle] = np.clip(quantized, 1, MAX_UINT16).astype(np.uint16)

    right = np.zeros((rows, width, 4), dtype=np.uint8)
    packed_rows = packed_depth.reshape(rows, width)
    right[:, :, 0] = (packed_rows >> 8).astype(np.uint8)
    right[:, :, 1] = (packed_rows & 255).astype(np.uint8)
    binary_validity = valid.reshape(rows, width).astype(np.uint8) * 255
    right[:, :, 2] = binary_validity
    right[:, :, 3] = binary_validity
    encoded = np.empty((rows, width * 2, 4), dtype=np.uint8)
    encoded[:, :width, :] = np.frombuffer(rgba, dtype=np.uint8).reshape(rows, width, 4)
    encoded[:, width:, :] = right

    stats = AtlasPackStats(
        total_pixels=pixel_count,
        valid_pixels=int(np.count_nonzero(valid)),
        masked_pixels=int(np.count_nonzero(~mask_valid)),
        invalid_depth_pixels=int(np.count_nonzero(mask_valid & ~depth_valid)),
        near_clipped_pixels=int(np.count_nonzero(near)),
        far_clipped_pixels=int(np.count_nonzero(far)),
    )
    return PackedMoGe2Atlas(
        source_width=width,
        height=rows,
        depth_scale=scale,
        depth_bias=bias,
        payload=encoded.tobytes(order="C"),
        stats=stats,
    )


def decode_moge2_atlas(
    payload: bytes | bytearray | memoryview,
    *,
    atlas_width: int,
    height: int,
    depth_scale: float = 0.001,
    depth_bias: float = 0.0,
    strict: bool = True,
) -> DecodedMoGe2Atlas:
    """Decode a MoGe-2 atlas for tests and diagnostics.

    Strict mode requires mask/confidence bytes to be binary, requires them to
    agree, and enforces packed zero as the invalid sentinel.
    """

    full_width = _require_int(atlas_width, "atlas_width", 2)
    if full_width % 2:
        raise MoGe2TransportError("atlas_width must be even")
    source_width = full_width // 2
    width, rows, pixel_count = _require_dimensions(source_width, height)
    if full_width != width * 2:
        raise MoGe2TransportError("atlas_width exceeds the supported bounds")
    scale, bias = _require_calibration(depth_scale, depth_bias)
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise MoGe2TransportError("payload must be bytes-like")
    encoded = bytes(payload)
    expected_bytes = full_width * rows * 4
    if len(encoded) != expected_bytes:
        raise MoGe2TransportError(
            f"payload contains {len(encoded)} bytes; expected {expected_bytes}"
        )

    source = bytearray(pixel_count * 4)
    quantized: list[int] = []
    depths: list[float] = []
    masks: list[bool] = []
    confidences: list[bool] = []
    source_row_bytes = source_width * 4
    atlas_row_bytes = full_width * 4
    for y in range(rows):
        atlas_start = y * atlas_row_bytes
        source_start = y * source_row_bytes
        source[source_start : source_start + source_row_bytes] = encoded[
            atlas_start : atlas_start + source_row_bytes
        ]
        right_start = atlas_start + source_row_bytes
        for x in range(source_width):
            cursor = right_start + x * 4
            packed_depth = (encoded[cursor] << 8) | encoded[cursor + 1]
            mask_byte = encoded[cursor + 2]
            confidence_byte = encoded[cursor + 3]
            if strict and mask_byte not in (0, 255):
                raise MoGe2TransportError("mask plane is not binary")
            if strict and confidence_byte not in (0, 255):
                raise MoGe2TransportError("confidence plane is not binary")
            valid_mask = mask_byte != 0
            valid_confidence = confidence_byte != 0
            if strict and valid_mask != valid_confidence:
                raise MoGe2TransportError("mask and confidence validity disagree")
            valid = valid_mask and valid_confidence
            if strict and ((packed_depth == 0) == valid):
                raise MoGe2TransportError(
                    "packed depth zero must be invalid and positive depth must be valid"
                )
            quantized.append(packed_depth)
            depths.append(packed_depth * scale + bias if valid else 0.0)
            masks.append(valid_mask)
            confidences.append(valid_confidence)

    return DecodedMoGe2Atlas(
        source_width=source_width,
        height=rows,
        source_rgba=bytes(source),
        packed_depth=tuple(quantized),
        depth_metres=tuple(depths),
        mask=tuple(masks),
        confidence=tuple(confidences),
    )


def _require_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise MoGe2TransportError(label + " must be a non-empty string")
    if any(ord(character) < 32 for character in value):
        raise MoGe2TransportError(label + " cannot contain control characters")
    return value


def _require_finite_sequence(
    values: Sequence[object], label: str, length: int
) -> list[float]:
    if isinstance(values, (str, bytes, bytearray)):
        raise MoGe2TransportError(f"{label} must contain exactly {length} numbers")
    try:
        actual_length = len(values)
    except TypeError as exc:
        raise MoGe2TransportError(
            f"{label} must contain exactly {length} numbers"
        ) from exc
    if actual_length != length:
        raise MoGe2TransportError(f"{label} must contain exactly {length} numbers")
    return [_require_finite(value, label) for value in values]


def make_moge2_worldbus_metadata(
    atlas: PackedMoGe2Atlas,
    *,
    frame_id: int,
    timestamp_ns: int,
    intrinsics: Sequence[object],
    camera_to_world: Sequence[object],
    generation_id: str,
    producer_session_id: str,
    source_frame_id: int,
    source_timestamp_ns: int,
    model_id: str,
    model_source_revision: str,
    model_revision: str,
    source_producer_session_id: Optional[str] = None,
    extra_extensions: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Build WorldBus-v1 metadata for an encoded MoGe-2 atlas.

    Intrinsics remain in source-plane pixels, not doubled atlas coordinates.
    The returned mapping is intended to be passed through
    :func:`flexgpu.worldbus.make_frame`, which remains the final protocol
    validator.
    """

    if not isinstance(atlas, PackedMoGe2Atlas):
        raise MoGe2TransportError("atlas must be a PackedMoGe2Atlas")
    output_frame = _require_int(frame_id, "frame_id")
    output_timestamp = _require_int(timestamp_ns, "timestamp_ns")
    input_frame = _require_int(source_frame_id, "source_frame_id")
    input_timestamp = _require_int(source_timestamp_ns, "source_timestamp_ns")
    camera = _require_finite_sequence(camera_to_world, "camera_to_world", 16)
    calibration = _require_finite_sequence(intrinsics, "intrinsics", 4)
    if calibration[0] <= 0.0 or calibration[1] <= 0.0:
        raise MoGe2TransportError("intrinsics fx and fy must be greater than zero")
    generation = _require_text(generation_id, "generation_id")
    session = _require_text(producer_session_id, "producer_session_id")
    model_name = _require_text(model_id, "model_id")
    source_revision = _require_text(model_source_revision, "model_source_revision")
    checkpoint_revision = _require_text(model_revision, "model_revision")

    reserved = {
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
        "producer_session_id",
    }
    extensions: dict[str, Any] = {
        "moge2_atlas_contract": ATLAS_CONTRACT,
        "moge2_depth_encoding": DEPTH_ENCODING,
        "moge2_depth_semantics": DEPTH_SEMANTICS,
        "moge2_mask_semantics": MASK_SEMANTICS,
        "moge2_confidence_semantics": CONFIDENCE_SEMANTICS,
        "moge2_source_frame_id": input_frame,
        "moge2_source_timestamp_ns": str(input_timestamp),
        "moge2_model_id": model_name,
        "moge2_model_source_revision": source_revision,
        "moge2_model_revision": checkpoint_revision,
    }
    if source_producer_session_id is not None:
        extensions["moge2_source_producer_session_id"] = _require_text(
            source_producer_session_id, "source_producer_session_id"
        )
    extensions.update(atlas.stats.to_extensions())
    if extra_extensions is not None:
        if not isinstance(extra_extensions, Mapping):
            raise MoGe2TransportError("extra_extensions must be a mapping")
        for key, value in extra_extensions.items():
            if not isinstance(key, str) or not key:
                raise MoGe2TransportError("extension keys must be non-empty strings")
            if key in reserved or key in extensions:
                raise MoGe2TransportError("extension key is reserved: " + key)
            extensions[key] = value

    metadata: dict[str, Any] = {
        "worldbus_version": 1,
        "frame_id": output_frame,
        "timestamp_ns": str(output_timestamp),
        "width": atlas.atlas_width,
        "height": atlas.height,
        "pixel_format": "rgba8_atlas",
        "payload_bytes": len(atlas.payload),
        "intrinsics": calibration,
        "depth_scale_bias": [atlas.depth_scale, atlas.depth_bias],
        "camera_to_world": camera,
        "generation_id": generation,
        "producer_session_id": session,
    }
    metadata.update(extensions)
    return metadata
