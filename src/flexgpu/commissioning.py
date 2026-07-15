"""Calibration, adapter-health, and deterministic commissioning bundles.

The production TouchDesigner project deliberately does not vendor a camera SDK,
model, or private ``.tox``.  This module defines the small, dependency-free data
contract those local adapters can emit and a replay bundle that can be validated
without opening TouchDesigner.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence


CALIBRATION_VERSION = "flexgpu-calibration/v1"
BUNDLE_VERSION = "flexgpu-commissioning/v1"
FRAME_STATE_VERSION = "flexgpu-frame-state/v1"
MAX_DIMENSION = 16_384
MAX_FRAMES = 100_000
MAX_MEDIA_BYTES = 256 * 1024 * 1024
MAX_JSON_BYTES = 128 * 1024 * 1024
MAX_AGGREGATE_PIXELS = 128 * 1024 * 1024

DEPTH_ENCODINGS = frozenset(
    {"normalized", "metres", "millimetres", "disparity", "inverse_depth"}
)
MEDIA_FORMATS = frozenset(
    {"ppm-rgb8", "pgm-u8", "pgm-u16", "raw-rgba8", "raw-r32f-le"}
)
MEDIA_ROLE_FORMATS = {
    "rgb": frozenset({"ppm-rgb8", "raw-rgba8"}),
    "depth": frozenset({"pgm-u8", "pgm-u16", "raw-r32f-le"}),
    "mask": frozenset({"pgm-u8", "pgm-u16", "raw-r32f-le"}),
    "confidence": frozenset({"pgm-u8", "pgm-u16", "raw-r32f-le"}),
}
SESSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class CommissioningError(ValueError):
    """A calibration, adapter state, or commissioning bundle is invalid."""


def _reject_duplicate_pairs(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CommissioningError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise CommissioningError("non-finite JSON number is not allowed")


def load_strict_json(path: str | os.PathLike[str]) -> Mapping[str, Any]:
    """Load one strict UTF-8 JSON object with duplicate-key rejection."""

    source = Path(path)
    try:
        if source.stat().st_size > MAX_JSON_BYTES:
            raise CommissioningError("JSON document exceeds the size limit")
        payload = source.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise CommissioningError("unable to read JSON document") from exc
    try:
        parsed = json.loads(
            payload,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError, CommissioningError) as exc:
        if isinstance(exc, CommissioningError):
            raise
        raise CommissioningError("unable to parse strict JSON document") from exc
    if not isinstance(parsed, Mapping):
        raise CommissioningError("JSON document root must be an object")
    return parsed


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CommissioningError(label + " must be numeric")
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise CommissioningError(label + " must be a finite number") from exc
    if not math.isfinite(result):
        raise CommissioningError(label + " must be finite")
    return result


def _bounded_int(value: Any, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CommissioningError(label + " must be an integer")
    if value < minimum or value > maximum:
        raise CommissioningError(
            "%s must be between %d and %d" % (label, minimum, maximum)
        )
    return value


def _matrix(value: Any, label: str) -> tuple[float, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise CommissioningError(label + " must contain 16 numbers")
    if len(value) != 16:
        raise CommissioningError(label + " must contain exactly 16 numbers")
    result = tuple(_finite_number(item, "%s[%d]" % (label, index)) for index, item in enumerate(value))
    if any(abs(result[index] - expected) > 1e-6 for index, expected in zip((12, 13, 14, 15), (0.0, 0.0, 0.0, 1.0))):
        raise CommissioningError(label + " must use homogeneous final row [0, 0, 0, 1]")
    determinant = (
        result[0] * (result[5] * result[10] - result[6] * result[9])
        - result[1] * (result[4] * result[10] - result[6] * result[8])
        + result[2] * (result[4] * result[9] - result[5] * result[8])
    )
    if determinant <= 1e-8:
        raise CommissioningError(
            label + " must have a non-singular right-handed spatial basis"
        )
    return result


def _identity_matrix() -> tuple[float, ...]:
    return (
        1.0,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
    )


@dataclass(frozen=True)
class CalibrationProfile:
    """Camera and sensor transforms for one metric shared-world epoch."""

    calibration_id: str
    width: int
    height: int
    fx: float
    fy: float
    cx: float
    cy: float
    depth_encoding: str
    depth_scale: float
    depth_bias: float
    near_m: float
    far_m: float
    camera_to_world: tuple[float, ...]
    sensor_to_world: tuple[float, ...]
    coordinate_system: str = "right_handed_y_up_metres"
    version: str = CALIBRATION_VERSION

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "CalibrationProfile":
        if not isinstance(data, Mapping):
            raise CommissioningError("calibration must be an object")
        allowed = {
            "version",
            "calibration_id",
            "image",
            "intrinsics",
            "depth",
            "camera_to_world",
            "sensor_to_world",
            "coordinate_system",
        }
        unknown = sorted(set(data).difference(allowed))
        if unknown:
            raise CommissioningError("unsupported calibration field " + unknown[0])
        version = data.get("version")
        if version != CALIBRATION_VERSION:
            raise CommissioningError("unsupported calibration version")
        calibration_id = data.get("calibration_id")
        if not isinstance(calibration_id, str) or SESSION_PATTERN.fullmatch(calibration_id) is None:
            raise CommissioningError("calibration_id must be a conservative identifier")

        image = data.get("image")
        intrinsics = data.get("intrinsics")
        depth = data.get("depth")
        if not isinstance(image, Mapping):
            raise CommissioningError("calibration.image must be an object")
        if set(image).difference({"width", "height"}):
            raise CommissioningError("calibration.image contains an unsupported field")
        width = _bounded_int(image.get("width"), "image.width", 1, MAX_DIMENSION)
        height = _bounded_int(image.get("height"), "image.height", 1, MAX_DIMENSION)

        if not isinstance(intrinsics, Mapping):
            raise CommissioningError("calibration.intrinsics must be an object")
        if set(intrinsics).difference({"fx", "fy", "cx", "cy"}):
            raise CommissioningError("calibration.intrinsics contains an unsupported field")
        fx = _finite_number(intrinsics.get("fx"), "intrinsics.fx")
        fy = _finite_number(intrinsics.get("fy"), "intrinsics.fy")
        cx = _finite_number(intrinsics.get("cx"), "intrinsics.cx")
        cy = _finite_number(intrinsics.get("cy"), "intrinsics.cy")
        if fx <= 0 or fy <= 0:
            raise CommissioningError("intrinsics focal lengths must be positive")
        if fx > width * 100 or fy > height * 100:
            raise CommissioningError("intrinsics focal lengths are implausibly large")
        if not (-width <= cx <= width * 2 and -height <= cy <= height * 2):
            raise CommissioningError("intrinsics principal point is outside the supported range")

        if not isinstance(depth, Mapping):
            raise CommissioningError("calibration.depth must be an object")
        if set(depth).difference({"encoding", "scale", "bias", "near_m", "far_m"}):
            raise CommissioningError("calibration.depth contains an unsupported field")
        encoding = depth.get("encoding")
        if encoding not in DEPTH_ENCODINGS:
            raise CommissioningError("unsupported depth encoding")
        scale = _finite_number(depth.get("scale", 1.0), "depth.scale")
        bias = _finite_number(depth.get("bias", 0.0), "depth.bias")
        near_m = _finite_number(depth.get("near_m"), "depth.near_m")
        far_m = _finite_number(depth.get("far_m"), "depth.far_m")
        if scale <= 0:
            raise CommissioningError("depth.scale must be positive")
        if near_m <= 0 or far_m <= near_m or far_m > 1000:
            raise CommissioningError("depth near/far range is invalid")

        coordinate_system = data.get("coordinate_system", "right_handed_y_up_metres")
        if coordinate_system != "right_handed_y_up_metres":
            raise CommissioningError("unsupported coordinate system")
        return cls(
            calibration_id=calibration_id,
            width=width,
            height=height,
            fx=fx,
            fy=fy,
            cx=cx,
            cy=cy,
            depth_encoding=str(encoding),
            depth_scale=scale,
            depth_bias=bias,
            near_m=near_m,
            far_m=far_m,
            camera_to_world=_matrix(data.get("camera_to_world"), "camera_to_world"),
            sensor_to_world=_matrix(data.get("sensor_to_world"), "sensor_to_world"),
            coordinate_system=coordinate_system,
            version=str(version),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "calibration_id": self.calibration_id,
            "image": {"width": self.width, "height": self.height},
            "intrinsics": {"fx": self.fx, "fy": self.fy, "cx": self.cx, "cy": self.cy},
            "depth": {
                "encoding": self.depth_encoding,
                "scale": self.depth_scale,
                "bias": self.depth_bias,
                "near_m": self.near_m,
                "far_m": self.far_m,
            },
            "camera_to_world": list(self.camera_to_world),
            "sensor_to_world": list(self.sensor_to_world),
            "coordinate_system": self.coordinate_system,
        }


def demo_calibration(width: int, height: int) -> CalibrationProfile:
    """Return the deterministic 60-degree demo-camera calibration."""

    width = _bounded_int(width, "width", 1, MAX_DIMENSION)
    height = _bounded_int(height, "height", 1, MAX_DIMENSION)
    focal = 0.5 * float(height) / math.tan(math.radians(30.0))
    identity = _identity_matrix()
    return CalibrationProfile(
        calibration_id="demo-camera-v1",
        width=width,
        height=height,
        fx=focal,
        fy=focal,
        cx=(width - 1) * 0.5,
        cy=(height - 1) * 0.5,
        depth_encoding="normalized",
        depth_scale=1.0,
        depth_bias=0.0,
        near_m=0.35,
        far_m=4.5,
        camera_to_world=identity,
        sensor_to_world=identity,
    )


@dataclass(frozen=True)
class AdapterFrameState:
    """Small status record published beside adapter textures."""

    session_id: str
    frame_id: int
    timestamp_ns: int
    width: int
    height: int
    calibration_id: str
    valid_fraction: float
    confidence_mean: float
    version: str = FRAME_STATE_VERSION

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "AdapterFrameState":
        if not isinstance(data, Mapping):
            raise CommissioningError("adapter frame state must be an object")
        allowed = {
            "version",
            "session_id",
            "frame_id",
            "timestamp_ns",
            "width",
            "height",
            "calibration_id",
            "valid_fraction",
            "confidence_mean",
        }
        if set(data).difference(allowed):
            raise CommissioningError("adapter frame state contains an unsupported field")
        if data.get("version") != FRAME_STATE_VERSION:
            raise CommissioningError("unsupported adapter frame-state version")
        session_id = data.get("session_id")
        calibration_id = data.get("calibration_id")
        if not isinstance(session_id, str) or SESSION_PATTERN.fullmatch(session_id) is None:
            raise CommissioningError("session_id must be a conservative identifier")
        if not isinstance(calibration_id, str) or SESSION_PATTERN.fullmatch(calibration_id) is None:
            raise CommissioningError("calibration_id must be a conservative identifier")
        valid = _finite_number(data.get("valid_fraction"), "valid_fraction")
        confidence = _finite_number(data.get("confidence_mean"), "confidence_mean")
        if not 0.0 <= valid <= 1.0 or not 0.0 <= confidence <= 1.0:
            raise CommissioningError("coverage and confidence must be between zero and one")
        return cls(
            session_id=session_id,
            frame_id=_bounded_int(data.get("frame_id"), "frame_id", 0, 2**63 - 1),
            timestamp_ns=_bounded_int(data.get("timestamp_ns"), "timestamp_ns", 1, 2**63 - 1),
            width=_bounded_int(data.get("width"), "width", 1, MAX_DIMENSION),
            height=_bounded_int(data.get("height"), "height", 1, MAX_DIMENSION),
            calibration_id=calibration_id,
            valid_fraction=valid,
            confidence_mean=confidence,
        )

    def freshness(self, now_ns: int | None = None, stale_after_ms: float = 1000.0) -> dict[str, Any]:
        now = time.time_ns() if now_ns is None else _bounded_int(now_ns, "now_ns", 1, 2**63 - 1)
        stale_ms = _finite_number(stale_after_ms, "stale_after_ms")
        if stale_ms <= 0 or stale_ms > 600_000:
            raise CommissioningError("stale_after_ms is outside the supported range")
        age_ms = (now - self.timestamp_ns) / 1_000_000.0
        if age_ms < -100.0:
            state = "future"
        elif age_ms > stale_ms:
            state = "stale"
        else:
            state = "alive"
        return {"state": state, "age_ms": age_ms, "stale_after_ms": stale_ms}

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "session_id": self.session_id,
            "frame_id": self.frame_id,
            "timestamp_ns": self.timestamp_ns,
            "width": self.width,
            "height": self.height,
            "calibration_id": self.calibration_id,
            "valid_fraction": self.valid_fraction,
            "confidence_mean": self.confidence_mean,
        }


def _relative_path(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise CommissioningError(label + " must be a non-empty POSIX relative path")
    pure = PurePosixPath(value)
    if pure.is_absolute() or ".." in pure.parts or "." in pure.parts:
        raise CommissioningError(label + " must stay inside the bundle")
    return pure.as_posix()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_MEDIA_BYTES:
                    raise CommissioningError("bundle media exceeds the per-file size limit")
                digest.update(chunk)
    except OSError as exc:
        raise CommissioningError("unable to read bundle media") from exc
    return digest.hexdigest()


def _safe_bundle_file(root: Path, relative: str) -> Path:
    candidate = (root / Path(*PurePosixPath(relative).parts)).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise CommissioningError("bundle media escapes the bundle directory") from exc
    if not candidate.is_file():
        raise CommissioningError("bundle media file is missing")
    return candidate


def _netpbm_layout(path: Path) -> tuple[str, int, int, int, int]:
    """Return magic, dimensions, max value, and binary payload offset."""

    try:
        with path.open("rb") as handle:
            header = handle.read(65_536)
    except OSError as exc:
        raise CommissioningError("unable to inspect bundle media") from exc
    cursor = 0

    def token() -> bytes:
        nonlocal cursor
        while cursor < len(header):
            if header[cursor] in b" \t\r\n\v\f":
                cursor += 1
                continue
            if header[cursor] == 35:  # '#'
                newline = header.find(b"\n", cursor + 1)
                if newline < 0:
                    raise CommissioningError("Netpbm header comment is too large")
                cursor = newline + 1
                continue
            break
        start = cursor
        while cursor < len(header) and header[cursor] not in b" \t\r\n\v\f#":
            cursor += 1
        if start == cursor:
            raise CommissioningError("Netpbm header is incomplete")
        return header[start:cursor]

    try:
        magic = token().decode("ascii")
        width = int(token().decode("ascii"))
        height = int(token().decode("ascii"))
        maximum = int(token().decode("ascii"))
    except (UnicodeError, ValueError) as exc:
        raise CommissioningError("Netpbm header is invalid") from exc
    if cursor >= len(header) or header[cursor] not in b" \t\r\n\v\f":
        raise CommissioningError("Netpbm header has no raster separator")
    if header[cursor : cursor + 2] == b"\r\n":
        cursor += 2
    else:
        cursor += 1
    return magic, width, height, maximum, cursor


def _validate_media_layout(
    path: Path, media_format: str, width: int, height: int
) -> None:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise CommissioningError("unable to inspect bundle media") from exc
    if size > MAX_MEDIA_BYTES:
        raise CommissioningError("bundle media exceeds the per-file size limit")

    if media_format in {"raw-rgba8", "raw-r32f-le"}:
        expected = width * height * 4
    else:
        magic, encoded_width, encoded_height, maximum, offset = _netpbm_layout(path)
        expected_header = {
            "ppm-rgb8": ("P6", 255, 3),
            "pgm-u8": ("P5", 255, 1),
            "pgm-u16": ("P5", 65_535, 2),
        }[media_format]
        if (magic, maximum) != expected_header[:2]:
            raise CommissioningError("bundle media header does not match its format")
        if encoded_width != width or encoded_height != height:
            raise CommissioningError("bundle media dimensions do not match frame state")
        expected = offset + width * height * expected_header[2]
    if size != expected:
        raise CommissioningError("bundle media byte length does not match its format")


def _validate_media(
    root: Path,
    value: Any,
    label: str,
    role: str,
    verify_hashes: bool,
    width: int,
    height: int,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CommissioningError(label + " must be an object")
    if set(value).difference({"path", "format", "sha256"}):
        raise CommissioningError(label + " contains an unsupported field")
    relative = _relative_path(value.get("path"), label + ".path")
    media_format = value.get("format")
    if media_format not in MEDIA_FORMATS:
        raise CommissioningError(label + " has an unsupported media format")
    if media_format not in MEDIA_ROLE_FORMATS[role]:
        raise CommissioningError(label + " format is incompatible with its media role")
    expected = value.get("sha256")
    if not isinstance(expected, str) or re.fullmatch(r"[0-9a-f]{64}", expected) is None:
        raise CommissioningError(label + ".sha256 must be a lowercase SHA-256 digest")
    path = _safe_bundle_file(root, relative)
    if verify_hashes and _sha256_file(path) != expected:
        raise CommissioningError(label + " failed SHA-256 verification")
    _validate_media_layout(path, str(media_format), width, height)
    return {
        "path": relative,
        "format": media_format,
        "sha256": expected,
        "resolved": os.path.normcase(str(path)),
    }


def validate_bundle(
    manifest_path: str | os.PathLike[str], *, verify_hashes: bool = True
) -> dict[str, Any]:
    """Validate a synchronized replay bundle and return a safe summary."""

    manifest = Path(manifest_path).resolve()
    data = load_strict_json(manifest)
    allowed = {"version", "created_ns", "source", "calibration", "frames"}
    unknown = sorted(set(data).difference(allowed))
    if unknown:
        raise CommissioningError("unsupported bundle field " + unknown[0])
    if data.get("version") != BUNDLE_VERSION:
        raise CommissioningError("unsupported commissioning bundle version")
    _bounded_int(data.get("created_ns"), "created_ns", 1, 2**63 - 1)
    source = data.get("source")
    if not isinstance(source, Mapping) or set(source).difference({"name", "kind"}):
        raise CommissioningError("bundle source must contain only name and kind")
    source_name = source.get("name")
    source_kind = source.get("kind")
    if not isinstance(source_name, str) or SESSION_PATTERN.fullmatch(source_name) is None:
        raise CommissioningError("bundle source name must be a conservative identifier")
    if source_kind not in {"synthetic", "recorded", "replay"}:
        raise CommissioningError("unsupported bundle source kind")

    root = manifest.parent.resolve()
    calibration_ref = data.get("calibration")
    if not isinstance(calibration_ref, Mapping) or set(calibration_ref).difference({"path", "sha256"}):
        raise CommissioningError("bundle calibration reference is invalid")
    calibration_relative = _relative_path(calibration_ref.get("path"), "calibration.path")
    calibration_digest = calibration_ref.get("sha256")
    if not isinstance(calibration_digest, str) or re.fullmatch(r"[0-9a-f]{64}", calibration_digest) is None:
        raise CommissioningError("calibration.sha256 must be a lowercase SHA-256 digest")
    calibration_path = _safe_bundle_file(root, calibration_relative)
    if verify_hashes and _sha256_file(calibration_path) != calibration_digest:
        raise CommissioningError("calibration failed SHA-256 verification")
    calibration = CalibrationProfile.from_mapping(load_strict_json(calibration_path))

    frames = data.get("frames")
    if not isinstance(frames, list) or not frames:
        raise CommissioningError("bundle frames must be a non-empty array")
    if len(frames) > MAX_FRAMES:
        raise CommissioningError("bundle contains too many frames")
    sessions: set[str] = set()
    last_ids: dict[str, int] = {}
    last_timestamp = -1
    total_pixels = 0
    media_files: set[str] = set()
    for index, raw in enumerate(frames):
        label = "frames[%d]" % index
        if not isinstance(raw, Mapping):
            raise CommissioningError(label + " must be an object")
        allowed_frame = {
            "state",
            "rgb",
            "depth",
            "mask",
            "confidence",
        }
        if set(raw).difference(allowed_frame):
            raise CommissioningError(label + " contains an unsupported field")
        state = AdapterFrameState.from_mapping(raw.get("state"))
        if state.calibration_id != calibration.calibration_id:
            raise CommissioningError(label + " calibration_id does not match the bundle")
        if state.width != calibration.width or state.height != calibration.height:
            raise CommissioningError(label + " dimensions do not match calibration")
        previous_id = last_ids.get(state.session_id)
        if previous_id is not None and state.frame_id <= previous_id:
            raise CommissioningError("frame IDs must increase within each session")
        if state.timestamp_ns <= last_timestamp:
            raise CommissioningError("frame timestamps must be globally increasing")
        last_ids[state.session_id] = state.frame_id
        last_timestamp = state.timestamp_ns
        sessions.add(state.session_id)
        total_pixels += state.width * state.height
        if total_pixels > MAX_AGGREGATE_PIXELS:
            raise CommissioningError("bundle exceeds the aggregate-pixel limit")
        for media_name in ("rgb", "depth"):
            media = _validate_media(
                root,
                raw.get(media_name),
                label + "." + media_name,
                media_name,
                verify_hashes,
                state.width,
                state.height,
            )
            if media["resolved"] in media_files:
                raise CommissioningError("bundle media paths must be unique per frame")
            media_files.add(media["resolved"])
        for media_name in ("mask", "confidence"):
            if raw.get(media_name) is not None:
                media = _validate_media(
                    root,
                    raw[media_name],
                    label + "." + media_name,
                    media_name,
                    verify_hashes,
                    state.width,
                    state.height,
                )
                if media["resolved"] in media_files:
                    raise CommissioningError("bundle media paths must be unique per frame")
                media_files.add(media["resolved"])

    duration_ns = max(0, last_timestamp - AdapterFrameState.from_mapping(frames[0]["state"]).timestamp_ns)
    return {
        "status": "valid",
        "version": BUNDLE_VERSION,
        "source": {"name": source_name, "kind": source_kind},
        "calibration_id": calibration.calibration_id,
        "dimensions": {"width": calibration.width, "height": calibration.height},
        "depth_encoding": calibration.depth_encoding,
        "frames": len(frames),
        "sessions": len(sessions),
        "duration_ms": duration_ns / 1_000_000.0,
        "media_files": len(media_files),
        "hashes_verified": bool(verify_hashes),
    }


def _atomic_write(path: Path, payload: bytes) -> None:
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _ppm_payload(width: int, height: int, frame: int) -> bytes:
    pixels = bytearray(width * height * 3)
    cursor = 0
    for y in range(height):
        for x in range(width):
            pixels[cursor] = (x * 255 // max(1, width - 1) + frame * 13) % 256
            pixels[cursor + 1] = (y * 255 // max(1, height - 1) + frame * 7) % 256
            pixels[cursor + 2] = ((x + y) * 127 // max(1, width + height - 2) + frame * 17) % 256
            cursor += 3
    return ("P6\n%d %d\n255\n" % (width, height)).encode("ascii") + bytes(pixels)


def _pgm_u8_payload(width: int, height: int, frame: int, confidence: bool = False) -> bytes:
    pixels = bytearray(width * height)
    cursor = 0
    centre_x = 0.5 + 0.18 * math.sin(frame * 0.31)
    centre_y = 0.5 + 0.14 * math.cos(frame * 0.23)
    for y in range(height):
        v = (y + 0.5) / float(height)
        for x in range(width):
            u = (x + 0.5) / float(width)
            distance = math.sqrt((u - centre_x) ** 2 + (v - centre_y) ** 2)
            if confidence:
                value = int(max(0.0, min(1.0, 1.0 - distance * 1.5)) * 255)
            else:
                value = 255 if distance < 0.24 else 0
            pixels[cursor] = value
            cursor += 1
    return ("P5\n%d %d\n255\n" % (width, height)).encode("ascii") + bytes(pixels)


def _pgm_u16_payload(width: int, height: int, frame: int) -> bytes:
    pixels = bytearray(width * height * 2)
    cursor = 0
    for y in range(height):
        v = (y + 0.5) / float(height)
        for x in range(width):
            u = (x + 0.5) / float(width)
            wave = 0.5 + 0.22 * math.sin(u * math.tau + frame * 0.2) * math.cos(v * math.tau)
            value = int(max(0.002, min(0.998, wave)) * 65535)
            pixels[cursor] = (value >> 8) & 0xFF
            pixels[cursor + 1] = value & 0xFF
            cursor += 2
    return ("P5\n%d %d\n65535\n" % (width, height)).encode("ascii") + bytes(pixels)


def generate_demo_bundle(
    output_directory: str | os.PathLike[str],
    *,
    frames: int = 8,
    width: int = 64,
    height: int = 36,
    interval_ms: float = 100.0,
) -> dict[str, Any]:
    """Create a deterministic synchronized RGB/depth/mask/confidence bundle."""

    frame_count = _bounded_int(frames, "frames", 1, 4096)
    width = _bounded_int(width, "width", 1, 2048)
    height = _bounded_int(height, "height", 1, 2048)
    interval = _finite_number(interval_ms, "interval_ms")
    if interval <= 0 or interval > 60_000:
        raise CommissioningError("interval_ms is outside the supported range")
    interval_ns = int(interval * 1_000_000)
    if interval_ns < 1:
        raise CommissioningError("interval_ms is too small to create unique timestamps")
    if frame_count * width * height > MAX_AGGREGATE_PIXELS:
        raise CommissioningError("demo bundle exceeds the aggregate-pixel limit")

    root = Path(output_directory).resolve()
    if root.exists():
        try:
            if not root.is_dir() or any(root.iterdir()):
                raise CommissioningError("output directory must not exist or must be empty")
        except OSError as exc:
            raise CommissioningError("unable to inspect output directory") from exc
    else:
        try:
            root.mkdir(parents=True)
        except OSError as exc:
            raise CommissioningError("unable to create output directory") from exc

    calibration = demo_calibration(width, height)
    calibration_path = root / "calibration.json"
    _atomic_write(calibration_path, _json_bytes(calibration.to_dict()))
    calibration_hash = _sha256_file(calibration_path)
    started_ns = 1_700_000_000_000_000_000
    records: list[dict[str, Any]] = []
    for index in range(frame_count):
        prefix = "frames/frame-%06d" % index
        media_payloads = {
            "rgb": (prefix + "-rgb.ppm", "ppm-rgb8", _ppm_payload(width, height, index)),
            "depth": (prefix + "-depth.pgm", "pgm-u16", _pgm_u16_payload(width, height, index)),
            "mask": (prefix + "-mask.pgm", "pgm-u8", _pgm_u8_payload(width, height, index)),
            "confidence": (
                prefix + "-confidence.pgm",
                "pgm-u8",
                _pgm_u8_payload(width, height, index, confidence=True),
            ),
        }
        media: dict[str, dict[str, Any]] = {}
        for name, (relative, media_format, payload) in media_payloads.items():
            path = root / Path(*PurePosixPath(relative).parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(path, payload)
            media[name] = {
                "path": relative,
                "format": media_format,
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        state = AdapterFrameState(
            session_id="demo-session",
            frame_id=index,
            timestamp_ns=started_ns + index * interval_ns,
            width=width,
            height=height,
            calibration_id=calibration.calibration_id,
            valid_fraction=0.82,
            confidence_mean=0.74,
        )
        records.append({"state": state.to_dict(), **media})

    manifest = {
        "version": BUNDLE_VERSION,
        "created_ns": started_ns,
        "source": {"name": "deterministic-demo", "kind": "synthetic"},
        "calibration": {"path": "calibration.json", "sha256": calibration_hash},
        "frames": records,
    }
    manifest_path = root / "manifest.json"
    _atomic_write(manifest_path, _json_bytes(manifest))
    summary = validate_bundle(manifest_path)
    summary["manifest"] = str(manifest_path)
    return summary


def write_calibration(
    path: str | os.PathLike[str], profile: CalibrationProfile
) -> None:
    """Atomically write one already validated calibration profile."""

    destination = Path(path)
    if destination.exists():
        raise CommissioningError("calibration destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(destination, _json_bytes(profile.to_dict()))


def validate_frame_sequence(states: Iterable[AdapterFrameState]) -> dict[str, Any]:
    """Validate monotonic adapter state independently from media capture."""

    count = 0
    sessions: set[str] = set()
    last_ids: dict[str, int] = {}
    last_timestamp = -1
    calibration_id: str | None = None
    for state in states:
        if not isinstance(state, AdapterFrameState):
            raise CommissioningError("frame sequence contains a non-frame-state item")
        if calibration_id is None:
            calibration_id = state.calibration_id
        elif state.calibration_id != calibration_id:
            raise CommissioningError("calibration changed without starting a new sequence")
        previous_id = last_ids.get(state.session_id)
        if previous_id is not None and state.frame_id <= previous_id:
            raise CommissioningError("frame IDs must increase within each session")
        if state.timestamp_ns <= last_timestamp:
            raise CommissioningError("frame timestamps must be globally increasing")
        last_ids[state.session_id] = state.frame_id
        last_timestamp = state.timestamp_ns
        sessions.add(state.session_id)
        count += 1
        if count > MAX_FRAMES:
            raise CommissioningError("frame sequence exceeds the supported limit")
    if count == 0:
        raise CommissioningError("frame sequence must not be empty")
    return {"frames": count, "sessions": len(sessions), "calibration_id": calibration_id}


__all__ = [
    "AdapterFrameState",
    "BUNDLE_VERSION",
    "CALIBRATION_VERSION",
    "CommissioningError",
    "CalibrationProfile",
    "FRAME_STATE_VERSION",
    "demo_calibration",
    "generate_demo_bundle",
    "load_strict_json",
    "validate_bundle",
    "validate_frame_sequence",
    "write_calibration",
]
