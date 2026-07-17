"""Dependency-free geometry-frame artifact contract.

The contract is intentionally model-neutral.  MoGe-2 is the first producer,
but the same bounded manifest can be used by future depth or point-map
backends.  Heavy array validation stays in the producing worker; this module
validates identity, dimensions, coordinate conventions, plane descriptors,
and on-disk integrity without importing NumPy or a model runtime.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence, Tuple


CONTRACT = "flexgpu-geometry-frame/v1"
MAX_JSON_BYTES = 256 * 1024
MAX_PLANE_BYTES = 256 * 1024 * 1024
MAX_DIMENSION = 8192
MAX_PIXELS = 16 * 1024 * 1024
MAX_PLANES = 16
MAX_ID_BYTES = 256
TRANSFORM_TOLERANCE = 1.0e-4

COORDINATE_SYSTEM = "flexgpu_camera_x_right_y_up_z_backward"
SOURCE_COORDINATE_SYSTEM = "opencv_camera_x_right_y_down_z_forward"
ALLOWED_DTYPES = frozenset({"float32", "uint8"})
REQUIRED_PLANES = frozenset(
    {
        "rgb",
        "position_camera",
        "depth",
        "normal_camera",
        "mask",
        "confidence",
    }
)

_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")
_REVISION_RE = re.compile(r"^[0-9A-Fa-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PLANE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_FILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class GeometryFrameError(ValueError):
    """A geometry artifact failed its bounded public contract."""


@dataclass(frozen=True)
class PlaneDescriptor:
    name: str
    filename: str
    dtype: str
    shape: Tuple[int, ...]
    byte_length: int
    sha256: str
    semantics: str


@dataclass(frozen=True)
class GeometryFrame:
    producer_session_id: str
    frame_id: int
    source_session_id: str
    source_frame_id: int
    source_timestamp_ns: int
    completed_timestamp_ns: int
    generation_id: str
    width: int
    height: int
    model_id: str
    source_revision: str
    model_revision: str
    precision: str
    num_tokens: int
    inference_ms: float
    intrinsics_normalized: Tuple[float, float, float, float]
    intrinsics_pixels: Tuple[float, float, float, float]
    camera_to_world: Tuple[float, ...]
    valid_fraction: float
    confidence_mean: float
    confidence_semantics: str
    planes: Mapping[str, PlaneDescriptor]


def _strict_json_loads(payload: str) -> Any:
    def reject_duplicate(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise GeometryFrameError("duplicate JSON key: " + key)
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise GeometryFrameError("non-finite JSON number: " + value)

    try:
        return json.loads(
            payload,
            object_pairs_hook=reject_duplicate,
            parse_constant=reject_constant,
        )
    except GeometryFrameError:
        raise
    except (ValueError, RecursionError) as exc:
        raise GeometryFrameError("invalid geometry manifest JSON: " + str(exc)) from exc


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise GeometryFrameError(label + " must be an object")
    if any(not isinstance(key, str) for key in value):
        raise GeometryFrameError(label + " keys must be strings")
    return value


def _exact_keys(
    value: Mapping[str, Any], required: set[str] | frozenset[str], label: str
) -> None:
    missing = sorted(required.difference(value))
    unknown = sorted(set(value).difference(required))
    if missing:
        raise GeometryFrameError(label + " is missing: " + ", ".join(missing))
    if unknown:
        raise GeometryFrameError(label + " has unknown fields: " + ", ".join(unknown))


def _integer(value: Any, label: str, minimum: int = 0, maximum: int = (1 << 63) - 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise GeometryFrameError(label + " must be an integer")
    if value < minimum or value > maximum:
        raise GeometryFrameError(f"{label} must be between {minimum} and {maximum}")
    return value


def _decimal_integer(value: Any, label: str) -> int:
    if isinstance(value, str):
        if (
            not value
            or len(value) > 20
            or not value.isascii()
            or not value.isdecimal()
        ):
            raise GeometryFrameError(label + " must be an unsigned decimal string")
        value = int(value)
    return _integer(value, label)


def _number(value: Any, label: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GeometryFrameError(label + " must be a number")
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise GeometryFrameError(label + " must be a finite number") from exc
    if not math.isfinite(result) or result < minimum or result > maximum:
        raise GeometryFrameError(f"{label} must be finite and between {minimum} and {maximum}")
    return result


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise GeometryFrameError(label + " must be a non-empty string")
    if len(value.encode("utf-8")) > MAX_ID_BYTES or _ID_RE.fullmatch(value) is None:
        raise GeometryFrameError(label + " contains unsupported characters or is too long")
    return value


def _revision(value: Any, label: str) -> str:
    if not isinstance(value, str) or _REVISION_RE.fullmatch(value) is None:
        raise GeometryFrameError(label + " must be exactly 40 hexadecimal characters")
    return value.lower()


def _finite_tuple(value: Any, label: str, length: int) -> Tuple[float, ...]:
    if not isinstance(value, (list, tuple)) or len(value) != length:
        raise GeometryFrameError(f"{label} must contain exactly {length} numbers")
    result = []
    for item in value:
        result.append(_number(item, label, -1.0e12, 1.0e12))
    return tuple(result)


def _shape(value: Any, label: str) -> Tuple[int, ...]:
    if not isinstance(value, (list, tuple)) or not 2 <= len(value) <= 3:
        raise GeometryFrameError(label + " must contain two or three dimensions")
    return tuple(_integer(item, label, 1, MAX_DIMENSION) for item in value)


def _camera_transform(value: Any) -> Tuple[float, ...]:
    result = _finite_tuple(value, "camera_to_world", 16)
    if any(
        abs(result[index] - expected) > 1.0e-6
        for index, expected in zip((12, 13, 14, 15), (0.0, 0.0, 0.0, 1.0))
    ):
        raise GeometryFrameError("camera_to_world must have a homogeneous final row")

    basis = (result[0:3], result[4:7], result[8:11])
    for axis in basis:
        length_squared = sum(component * component for component in axis)
        if abs(length_squared - 1.0) > TRANSFORM_TOLERANCE:
            raise GeometryFrameError("camera_to_world must have a rigid spatial basis")
    for first, second in ((0, 1), (0, 2), (1, 2)):
        dot = sum(
            basis[first][component] * basis[second][component]
            for component in range(3)
        )
        if abs(dot) > TRANSFORM_TOLERANCE:
            raise GeometryFrameError("camera_to_world must have a rigid spatial basis")
    determinant = (
        result[0] * (result[5] * result[10] - result[6] * result[9])
        - result[1] * (result[4] * result[10] - result[6] * result[8])
        + result[2] * (result[4] * result[9] - result[5] * result[8])
    )
    if determinant <= 0.0 or abs(determinant - 1.0) > TRANSFORM_TOLERANCE * 4:
        raise GeometryFrameError(
            "camera_to_world must have a right-handed rigid spatial basis"
        )
    return result


def _plane_descriptor(
    name: str, value: Any, *, width: int, height: int
) -> PlaneDescriptor:
    if (
        _PLANE_NAME_RE.fullmatch(name) is None
        or len(name.encode("utf-8")) > MAX_ID_BYTES
    ):
        raise GeometryFrameError("invalid plane name: " + str(name))
    data = _mapping(value, "plane " + name)
    required = {"filename", "dtype", "shape", "byte_length", "sha256", "semantics"}
    _exact_keys(data, required, "plane " + name)

    filename = data["filename"]
    if (
        not isinstance(filename, str)
        or len(filename.encode("utf-8")) > MAX_ID_BYTES
        or _FILE_NAME_RE.fullmatch(filename) is None
    ):
        raise GeometryFrameError("plane filename must be one safe basename")
    if Path(filename).name != filename or os.path.isabs(filename):
        raise GeometryFrameError("plane filename must not contain a path")

    dtype = data["dtype"]
    if not isinstance(dtype, str) or dtype not in ALLOWED_DTYPES:
        raise GeometryFrameError("unsupported plane dtype: " + str(dtype))
    shape = _shape(data["shape"], "plane shape")
    if shape[0] != height or shape[1] != width:
        raise GeometryFrameError(f"plane {name} dimensions do not match the frame")

    expected_shapes = {
        "rgb": (height, width, 4),
        "position_camera": (height, width, 4),
        "depth": (height, width),
        "normal_camera": (height, width, 4),
        "mask": (height, width),
        "confidence": (height, width),
    }
    if name in expected_shapes and shape != expected_shapes[name]:
        raise GeometryFrameError(
            f"plane {name} has shape {shape}; expected {expected_shapes[name]}"
        )

    byte_length = _integer(data["byte_length"], "plane byte_length", 1, MAX_PLANE_BYTES)
    digest = data["sha256"]
    if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
        raise GeometryFrameError("plane sha256 must be lowercase hexadecimal")
    semantics = data["semantics"]
    if not isinstance(semantics, str) or not semantics or len(semantics) > 512:
        raise GeometryFrameError("plane semantics must be a bounded non-empty string")
    return PlaneDescriptor(name, filename, dtype, shape, byte_length, digest, semantics)


def validate_geometry_manifest(value: Any) -> GeometryFrame:
    data = _mapping(value, "geometry manifest")
    required = {
        "contract",
        "producer_session_id",
        "frame_id",
        "source_session_id",
        "source_frame_id",
        "source_timestamp_ns",
        "completed_timestamp_ns",
        "generation_id",
        "width",
        "height",
        "model",
        "intrinsics_normalized",
        "intrinsics_pixels",
        "camera_to_world",
        "coordinate_system",
        "source_coordinate_system",
        "valid_fraction",
        "confidence_mean",
        "confidence_semantics",
        "planes",
    }
    _exact_keys(data, required, "geometry manifest")
    if data["contract"] != CONTRACT:
        raise GeometryFrameError("unsupported geometry contract")
    if data["coordinate_system"] != COORDINATE_SYSTEM:
        raise GeometryFrameError("unsupported geometry coordinate system")
    if data["source_coordinate_system"] != SOURCE_COORDINATE_SYSTEM:
        raise GeometryFrameError("unsupported source coordinate system")

    width = _integer(data["width"], "width", 1, MAX_DIMENSION)
    height = _integer(data["height"], "height", 1, MAX_DIMENSION)
    if width * height > MAX_PIXELS:
        raise GeometryFrameError("geometry frame exceeds the pixel limit")

    source_timestamp = _decimal_integer(data["source_timestamp_ns"], "source_timestamp_ns")
    completed_timestamp = _decimal_integer(
        data["completed_timestamp_ns"], "completed_timestamp_ns"
    )
    if completed_timestamp < source_timestamp:
        raise GeometryFrameError("completed_timestamp_ns precedes source_timestamp_ns")

    model = _mapping(data["model"], "model")
    _exact_keys(
        model,
        {
            "id",
            "source_revision",
            "model_revision",
            "precision",
            "num_tokens",
            "inference_ms",
        },
        "model",
    )
    source_revision = _revision(model["source_revision"], "model.source_revision")
    model_revision = _revision(model["model_revision"], "model.model_revision")
    precision = model["precision"]
    if not isinstance(precision, str) or precision not in {"fp16", "fp32", "mock"}:
        raise GeometryFrameError("unsupported model precision")

    intrinsics_normalized = _finite_tuple(
        data["intrinsics_normalized"], "intrinsics_normalized", 4
    )
    fx, fy, cx, cy = intrinsics_normalized
    if fx <= 0 or fy <= 0 or not 0 <= cx <= 1 or not 0 <= cy <= 1:
        raise GeometryFrameError("normalized intrinsics are outside their valid range")
    intrinsics_pixels = _finite_tuple(data["intrinsics_pixels"], "intrinsics_pixels", 4)
    pfx, pfy, pcx, pcy = intrinsics_pixels
    if pfx <= 0 or pfy <= 0 or not 0 <= pcx <= width or not 0 <= pcy <= height:
        raise GeometryFrameError("pixel intrinsics are outside their valid range")
    expected_pixels = (fx * width, fy * height, cx * width, cy * height)
    if any(
        not math.isclose(actual, expected, rel_tol=1.0e-6, abs_tol=1.0e-6)
        for actual, expected in zip(intrinsics_pixels, expected_pixels)
    ):
        raise GeometryFrameError(
            "pixel intrinsics do not match the normalized intrinsics and frame dimensions"
        )

    camera_to_world = _camera_transform(data["camera_to_world"])

    planes_data = _mapping(data["planes"], "planes")
    if len(planes_data) > MAX_PLANES:
        raise GeometryFrameError("geometry manifest contains too many planes")
    missing_planes = sorted(REQUIRED_PLANES.difference(planes_data))
    if missing_planes:
        raise GeometryFrameError(
            "geometry manifest is missing planes: " + ", ".join(missing_planes)
        )
    planes = {
        name: _plane_descriptor(name, descriptor, width=width, height=height)
        for name, descriptor in planes_data.items()
    }
    filenames = [plane.filename.casefold() for plane in planes.values()]
    if len(set(filenames)) != len(filenames):
        raise GeometryFrameError("geometry planes must use distinct filenames")

    return GeometryFrame(
        producer_session_id=_identifier(data["producer_session_id"], "producer_session_id"),
        frame_id=_integer(data["frame_id"], "frame_id"),
        source_session_id=_identifier(data["source_session_id"], "source_session_id"),
        source_frame_id=_integer(data["source_frame_id"], "source_frame_id"),
        source_timestamp_ns=source_timestamp,
        completed_timestamp_ns=completed_timestamp,
        generation_id=_identifier(data["generation_id"], "generation_id"),
        width=width,
        height=height,
        model_id=_identifier(model["id"], "model.id"),
        source_revision=source_revision,
        model_revision=model_revision,
        precision=precision,
        num_tokens=_integer(model["num_tokens"], "model.num_tokens", 1, 100_000),
        inference_ms=_number(model["inference_ms"], "model.inference_ms", 0.0, 3_600_000.0),
        intrinsics_normalized=intrinsics_normalized,
        intrinsics_pixels=intrinsics_pixels,
        camera_to_world=camera_to_world,
        valid_fraction=_number(data["valid_fraction"], "valid_fraction", 0.0, 1.0),
        confidence_mean=_number(data["confidence_mean"], "confidence_mean", 0.0, 1.0),
        confidence_semantics=_identifier(data["confidence_semantics"], "confidence_semantics"),
        planes=planes,
    )


def load_geometry_manifest(
    path: str | os.PathLike[str],
) -> tuple[GeometryFrame, Mapping[str, Any]]:
    manifest_path = Path(path)
    try:
        size = manifest_path.stat().st_size
    except OSError as exc:
        raise GeometryFrameError("geometry manifest is not readable") from exc
    if size < 2 or size > MAX_JSON_BYTES:
        raise GeometryFrameError("geometry manifest size is outside the allowed range")
    try:
        payload = manifest_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise GeometryFrameError("geometry manifest is not valid UTF-8") from exc
    raw = _strict_json_loads(payload)
    return validate_geometry_manifest(raw), _mapping(raw, "geometry manifest")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_geometry_bundle(path: str | os.PathLike[str]) -> GeometryFrame:
    manifest_path = Path(path).resolve()
    frame, _ = load_geometry_manifest(manifest_path)
    root = manifest_path.parent
    for plane in frame.planes.values():
        plane_path = (root / plane.filename).resolve()
        if plane_path.parent != root:
            raise GeometryFrameError("plane escaped the geometry bundle")
        try:
            size = plane_path.stat().st_size
        except OSError as exc:
            raise GeometryFrameError("geometry plane is missing: " + plane.name) from exc
        if size != plane.byte_length:
            raise GeometryFrameError("geometry plane size mismatch: " + plane.name)
        if _sha256_file(plane_path) != plane.sha256:
            raise GeometryFrameError("geometry plane digest mismatch: " + plane.name)
    return frame
