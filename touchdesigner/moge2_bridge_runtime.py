"""Import-safe TouchDesigner boundary for an external MoGe-2 worker.

TouchDesigner objects are deliberately confined to methods called by the TD
main thread.  Network threads receive and send validated immutable WorldBus
frames only; they never retain an OP, parameter, DAT, NumPy array, model, image
path, credential, or prompt.  NumPy is imported lazily only while converting a
TOP array on the main thread.

The module can be embedded in a Text DAT.  Its module-level ``tick``, ``stop``,
and ``on_script_top_cook`` functions match the callbacks built by
``runtime_pipeline.py``.
"""

from __future__ import annotations

import hashlib
import builtins
import collections
import json
import math
import os
import re
import sys
import threading
import time
import uuid
from dataclasses import dataclass, replace
from typing import Any, Callable, Mapping, Optional, Sequence, Tuple


_EMBEDDED_FLEXGPU_SRC = ""


def _ensure_local_src_path() -> None:
    """Locate the repository ``src`` folder without consulting TouchDesigner."""

    candidates = []
    explicit_src = os.environ.get("FLEXGPU_SRC")
    explicit_root = os.environ.get("FLEXGPU_ROOT")
    explicit_config = os.environ.get("FLEXGPU_CONFIG")
    if explicit_src:
        candidates.append(os.path.abspath(explicit_src))
    if explicit_root:
        candidates.append(os.path.abspath(os.path.join(explicit_root, "src")))
    if _EMBEDDED_FLEXGPU_SRC:
        candidates.append(os.path.abspath(_EMBEDDED_FLEXGPU_SRC))
    if explicit_config:
        config_dir = os.path.dirname(os.path.abspath(explicit_config))
        candidates.append(os.path.abspath(os.path.join(config_dir, "src")))
        candidates.append(os.path.abspath(os.path.join(config_dir, "..", "src")))
        candidates.append(
            os.path.abspath(os.path.join(config_dir, "..", "..", "src"))
        )
    project_value = globals().get("project", getattr(builtins, "project", None))
    try:
        project_folder = str(project_value.folder)
    except Exception:
        project_folder = ""
    if project_folder:
        candidates.append(os.path.abspath(os.path.join(project_folder, "src")))
        candidates.append(os.path.abspath(os.path.join(project_folder, "..", "src")))
    current_op = globals().get("me", getattr(builtins, "me", None))
    try:
        operator_folder = str(current_op.fileFolder)
    except Exception:
        operator_folder = ""
    if operator_folder:
        candidates.append(os.path.abspath(os.path.join(operator_folder, "src")))
        candidates.append(
            os.path.abspath(os.path.join(operator_folder, "..", "src"))
        )
    # A local installer normally adds ``<repo>/touchdesigner`` to sys.path
    # before this source is embedded in a Text DAT. Embedded DAT modules expose
    # an operator path as ``__file__`` and some TD builds do not expose
    # ``project`` through builtins, so use that already-authorized import root
    # as a bounded, non-recursive fallback.
    for search_root in tuple(sys.path)[:128]:
        if not isinstance(search_root, str) or not search_root or len(search_root) > 4096:
            continue
        candidates.append(os.path.abspath(os.path.join(search_root, "src")))
        candidates.append(os.path.abspath(os.path.join(search_root, "..", "src")))
    module_file = globals().get("__file__")
    if isinstance(module_file, str) and module_file:
        candidates.append(os.path.abspath(os.path.join(os.path.dirname(module_file), "..", "src")))
    candidates.append(os.path.abspath(os.path.join(os.getcwd(), "src")))
    for candidate in candidates:
        if (
            isinstance(candidate, str)
            and len(candidate) <= 4096
            and os.path.isfile(os.path.join(candidate, "flexgpu", "worldbus.py"))
        ):
            if candidate not in sys.path:
                sys.path.insert(0, candidate)
            return


try:
    from flexgpu.moge2_transport import (
        ATLAS_CONTRACT,
        CONFIDENCE_SEMANTICS,
        DEPTH_ENCODING,
        DEPTH_SEMANTICS,
        MASK_SEMANTICS,
    )
    from flexgpu.worldbus import (
        NewestFrameQueue,
        QueueClosed,
        TCPFrameSender,
        WorldBusError,
        WorldBusLimits,
        WorldBusReceiver,
        WorldFrame,
        make_frame,
        validate_frame,
    )
except ModuleNotFoundError as exc:
    if not str(getattr(exc, "name", "")).startswith("flexgpu"):
        raise
    _ensure_local_src_path()
    from flexgpu.moge2_transport import (  # type: ignore[no-redef]
        ATLAS_CONTRACT,
        CONFIDENCE_SEMANTICS,
        DEPTH_ENCODING,
        DEPTH_SEMANTICS,
        MASK_SEMANTICS,
    )
    from flexgpu.worldbus import (  # type: ignore[no-redef]
        NewestFrameQueue,
        QueueClosed,
        TCPFrameSender,
        WorldBusError,
        WorldBusLimits,
        WorldBusReceiver,
        WorldFrame,
        make_frame,
        validate_frame,
    )


REQUEST_CONTRACT = "flexgpu-moge2-request/v1"
FRAME_STATE_VERSION = "flexgpu-frame-state/v1"
CAMERA_METADATA_VERSION = "flexgpu-camera-metadata/v1"
FLEXGPU_CAMERA_COORDINATES = "flexgpu_camera_x_right_y_up_z_backward"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.-]+$")
_IDENTITY = (
    1.0, 0.0, 0.0, 0.0,
    0.0, 1.0, 0.0, 0.0,
    0.0, 0.0, 1.0, 0.0,
    0.0, 0.0, 0.0, 1.0,
)
_KNOWN_PROFILES = frozenset(("3080ti_16gb", "4090", "5090"))
_MAX_ISSUED_REQUESTS = 128


class BridgeRuntimeError(RuntimeError):
    """A bounded bridge input, lifecycle action, or result is invalid."""


@dataclass(frozen=True)
class BridgeLimits:
    """Hard limits applied before TOP copies or network allocations."""

    max_source_width: int = 1024
    max_source_height: int = 1024
    max_source_pixels: int = 1024 * 1024
    max_capture_fps: float = 60.0
    max_input_array_bytes: int = 64 * 1024 * 1024
    thread_join_timeout_s: float = 1.5
    socket_timeout_s: float = 0.5
    # The worker opens its persistent output socket before its first CUDA
    # warm-up/inference. Keep this independent of the short connect timeout,
    # but bounded so an idle peer cannot hold the receiver indefinitely.
    result_receive_timeout_s: float = 30.0

    def __post_init__(self) -> None:
        integers = (
            (self.max_source_width, "max_source_width"),
            (self.max_source_height, "max_source_height"),
            (self.max_source_pixels, "max_source_pixels"),
            (self.max_input_array_bytes, "max_input_array_bytes"),
        )
        for value, label in integers:
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise BridgeRuntimeError(label + " must be a positive integer")
        for value, label in (
            (self.max_capture_fps, "max_capture_fps"),
            (self.thread_join_timeout_s, "thread_join_timeout_s"),
            (self.socket_timeout_s, "socket_timeout_s"),
            (self.result_receive_timeout_s, "result_receive_timeout_s"),
        ):
            if not isinstance(value, (int, float)) or not math.isfinite(float(value)) or value <= 0:
                raise BridgeRuntimeError(label + " must be a positive finite number")
        if self.result_receive_timeout_s > 60.0:
            raise BridgeRuntimeError(
                "result_receive_timeout_s must be at most 60 seconds"
            )


DEFAULT_BRIDGE_LIMITS = BridgeLimits()


def _host(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise BridgeRuntimeError(label + " must be a host name or address")
    selected = value.strip()
    if (
        not selected
        or len(selected) > 255
        or any(ord(character) < 33 for character in selected)
        or "://" in selected
        or "@" in selected
    ):
        raise BridgeRuntimeError(label + " must be a host name or address without credentials")
    return selected


def _port(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise BridgeRuntimeError(label + " must be an integer between 1 and 65535")
    return value


def _safe_text(value: object, label: str, maximum_bytes: int = 128) -> str:
    if not isinstance(value, str):
        raise BridgeRuntimeError(label + " must be a string")
    selected = value.strip()
    if (
        not selected
        or len(selected.encode("utf-8")) > maximum_bytes
        or any(ord(character) < 32 for character in selected)
    ):
        raise BridgeRuntimeError(label + " is empty, too long, or contains control characters")
    return selected


@dataclass(frozen=True)
class BridgeConfig:
    """Network and cadence settings mirrored by ``MOGE2_BRIDGE`` parameters."""

    profile: str = "3080ti_16gb"
    worker_host: str = "127.0.0.1"
    worker_input_tcp: int = 9211
    worker_input_udp: int = 9210
    result_bind_host: str = "127.0.0.1"
    result_tcp: int = 9221
    result_udp: int = 9220
    capture_fps: float = 5.0
    flip_vertical: bool = True

    def __post_init__(self) -> None:
        if self.profile not in _KNOWN_PROFILES:
            raise BridgeRuntimeError("profile is unsupported")
        object.__setattr__(self, "worker_host", _host(self.worker_host, "worker_host"))
        object.__setattr__(
            self, "result_bind_host", _host(self.result_bind_host, "result_bind_host")
        )
        for value, label in (
            (self.worker_input_tcp, "worker_input_tcp"),
            (self.worker_input_udp, "worker_input_udp"),
            (self.result_tcp, "result_tcp"),
            (self.result_udp, "result_udp"),
        ):
            _port(value, label)
        if (
            isinstance(self.capture_fps, bool)
            or not isinstance(self.capture_fps, (int, float))
            or not math.isfinite(float(self.capture_fps))
            or self.capture_fps <= 0
            or self.capture_fps > DEFAULT_BRIDGE_LIMITS.max_capture_fps
        ):
            raise BridgeRuntimeError("capture_fps must be greater than zero and at most 60")
        if not isinstance(self.flip_vertical, bool):
            raise BridgeRuntimeError("flip_vertical must be boolean")


@dataclass(frozen=True)
class CaptureReceipt:
    frame_id: int
    timestamp_ns: int
    width: int
    height: int
    queued: bool


@dataclass(frozen=True)
class ImmutableAtlasResult:
    """Validated result state safe to share from a worker thread to TD."""

    frame_id: int
    timestamp_ns: int
    width: int
    height: int
    payload: bytes
    intrinsics: Tuple[float, float, float, float]
    depth_scale_bias: Tuple[float, float]
    camera_to_world: Tuple[float, ...]
    generation_id: str
    producer_session_id: str
    source_frame_id: int
    source_timestamp_ns: int
    source_producer_session_id: Optional[str]
    valid_fraction: float
    model_id: str
    model_source_revision: str
    model_revision: str
    profile: str
    original_source_width: int
    original_source_height: int

    @property
    def source_width(self) -> int:
        return self.width // 2

    @property
    def key(self) -> tuple[str, int]:
        return self.producer_session_id, self.frame_id


def _require_int(value: object, label: str, low: int, high: int) -> int:
    if isinstance(value, bool):
        raise BridgeRuntimeError(label + " must be an integer")
    if isinstance(value, str):
        if re.fullmatch(r"0|[1-9][0-9]*", value) is None:
            raise BridgeRuntimeError(label + " must be a decimal integer")
        parsed = int(value)
    elif isinstance(value, int):
        parsed = value
    else:
        raise BridgeRuntimeError(label + " must be an integer")
    if parsed < low or parsed > high:
        raise BridgeRuntimeError(label + " is outside the supported range")
    return parsed


def _finite_fraction(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BridgeRuntimeError(label + " must be numeric")
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise BridgeRuntimeError(label + " must be between zero and one")
    return number


def _extension_text(extensions: Mapping[str, Any], field: str) -> str:
    return _safe_text(extensions.get(field), field, 256)


def validate_moge2_result_frame(
    frame: WorldFrame,
    *,
    limits: BridgeLimits = DEFAULT_BRIDGE_LIMITS,
) -> ImmutableAtlasResult:
    """Validate the MoGe atlas contract without decoding or retaining arrays."""

    worldbus_limits = _worldbus_limits(limits)
    normalized = validate_frame(frame, worldbus_limits)
    metadata = normalized.metadata
    if metadata.pixel_format != "rgba8_atlas":
        raise BridgeRuntimeError("result pixel_format must be rgba8_atlas")
    if metadata.width % 2:
        raise BridgeRuntimeError("result atlas width must be even")
    source_width = metadata.width // 2
    source_pixels = source_width * metadata.height
    if source_width > limits.max_source_width or metadata.height > limits.max_source_height:
        raise BridgeRuntimeError("result dimensions exceed bridge limits")
    if source_pixels > limits.max_source_pixels:
        raise BridgeRuntimeError("result source plane exceeds bridge pixel limit")
    extensions = metadata.extensions
    expected_strings = {
        "moge2_atlas_contract": ATLAS_CONTRACT,
        "moge2_depth_encoding": DEPTH_ENCODING,
        "moge2_depth_semantics": DEPTH_SEMANTICS,
        "moge2_mask_semantics": MASK_SEMANTICS,
        "moge2_confidence_semantics": CONFIDENCE_SEMANTICS,
    }
    for field, expected in expected_strings.items():
        if extensions.get(field) != expected:
            raise BridgeRuntimeError(field + " does not match the live atlas contract")
    producer_session = _safe_text(
        extensions.get("producer_session_id"), "producer_session_id", 256
    )
    source_frame_id = _require_int(
        extensions.get("moge2_source_frame_id"),
        "moge2_source_frame_id",
        0,
        (1 << 63) - 1,
    )
    source_timestamp = _require_int(
        extensions.get("moge2_source_timestamp_ns"),
        "moge2_source_timestamp_ns",
        1,
        (1 << 63) - 1,
    )
    valid_fraction = _finite_fraction(
        extensions.get("moge2_valid_fraction"), "moge2_valid_fraction"
    )
    valid_pixels = _require_int(
        extensions.get("moge2_valid_pixels"), "moge2_valid_pixels", 0, source_pixels
    )
    masked_pixels = _require_int(
        extensions.get("moge2_masked_pixels"), "moge2_masked_pixels", 0, source_pixels
    )
    invalid_depth_pixels = _require_int(
        extensions.get("moge2_invalid_depth_pixels"),
        "moge2_invalid_depth_pixels",
        0,
        source_pixels,
    )
    if valid_pixels + masked_pixels + invalid_depth_pixels != source_pixels:
        raise BridgeRuntimeError("MoGe validity counters do not cover the source plane")
    if not math.isclose(valid_fraction, valid_pixels / float(source_pixels), abs_tol=1e-9):
        raise BridgeRuntimeError("MoGe valid fraction disagrees with its pixel count")
    _require_int(
        extensions.get("moge2_near_clipped_pixels"),
        "moge2_near_clipped_pixels",
        0,
        valid_pixels,
    )
    _require_int(
        extensions.get("moge2_far_clipped_pixels"),
        "moge2_far_clipped_pixels",
        0,
        valid_pixels,
    )
    fx, fy, cx, cy = metadata.intrinsics
    if fx > source_width * 100.0 or fy > metadata.height * 100.0:
        raise BridgeRuntimeError("result focal lengths exceed consumer bounds")
    if not (
        -source_width <= cx <= source_width * 2.0
        and -metadata.height <= cy <= metadata.height * 2.0
    ):
        raise BridgeRuntimeError("result principal point lies outside the source plane")
    scale, bias = metadata.depth_scale_bias
    near_metres = bias + scale
    far_metres = bias + scale * 65535.0
    if (
        scale > 1000.0
        or bias < 0.0
        or bias > 1000.0
        or not math.isfinite(far_metres)
        or near_metres <= 0.0
        or far_metres <= near_metres
        or far_metres > 1000.0
    ):
        raise BridgeRuntimeError("result depth calibration is unsupported")
    camera_to_world = _rigid_camera_matrix(metadata.camera_to_world)
    source_session_value = extensions.get("moge2_source_producer_session_id")
    source_session = (
        None
        if source_session_value is None
        else _safe_text(source_session_value, "moge2_source_producer_session_id", 256)
    )
    profile = _extension_text(extensions, "moge2_profile")
    if profile not in _KNOWN_PROFILES:
        raise BridgeRuntimeError("moge2_profile is unsupported")
    original_width = _require_int(
        extensions.get("moge2_source_width"),
        "moge2_source_width",
        1,
        limits.max_source_width,
    )
    original_height = _require_int(
        extensions.get("moge2_source_height"),
        "moge2_source_height",
        1,
        limits.max_source_height,
    )
    if original_width * original_height > limits.max_source_pixels:
        raise BridgeRuntimeError("original source dimensions exceed the bridge pixel limit")
    if extensions.get("moge2_source_pixel_format") != "rgba8":
        raise BridgeRuntimeError("moge2_source_pixel_format must be rgba8")
    return ImmutableAtlasResult(
        frame_id=metadata.frame_id,
        timestamp_ns=metadata.timestamp_ns,
        width=metadata.width,
        height=metadata.height,
        payload=bytes(normalized.payload),
        intrinsics=(float(fx), float(fy), float(cx), float(cy)),
        depth_scale_bias=(float(scale), float(bias)),
        camera_to_world=camera_to_world,
        generation_id=str(metadata.generation_id),
        producer_session_id=producer_session,
        source_frame_id=source_frame_id,
        source_timestamp_ns=source_timestamp,
        source_producer_session_id=source_session,
        valid_fraction=valid_fraction,
        model_id=_extension_text(extensions, "moge2_model_id"),
        model_source_revision=_extension_text(extensions, "moge2_model_source_revision"),
        model_revision=_extension_text(extensions, "moge2_model_revision"),
        profile=profile,
        original_source_width=original_width,
        original_source_height=original_height,
    )


def _worldbus_limits(limits: BridgeLimits) -> WorldBusLimits:
    atlas_width = limits.max_source_width * 2
    atlas_pixels = limits.max_source_pixels * 2
    return WorldBusLimits(
        max_payload_bytes=atlas_pixels * 4,
        max_width=atlas_width,
        max_height=limits.max_source_height,
        max_pixels=atlas_pixels,
        socket_timeout_s=float(limits.socket_timeout_s),
    )


def _result_worldbus_limits(limits: BridgeLimits) -> WorldBusLimits:
    """Use a bounded idle window independent of short sender connect attempts."""

    return replace(
        _worldbus_limits(limits),
        socket_timeout_s=float(limits.result_receive_timeout_s),
    )


def _rigid_camera_matrix(values: Sequence[object]) -> Tuple[float, ...]:
    if isinstance(values, (str, bytes, bytearray)) or len(values) != 16:
        raise BridgeRuntimeError("camera_to_world must contain 16 numbers")
    matrix = tuple(float(value) for value in values)
    if not all(math.isfinite(value) for value in matrix):
        raise BridgeRuntimeError("camera_to_world must contain finite numbers")
    tolerance = 1e-3
    if any(abs(matrix[index]) > tolerance for index in (12, 13, 14)):
        raise BridgeRuntimeError("camera_to_world final row must be homogeneous")
    if abs(matrix[15] - 1.0) > tolerance:
        raise BridgeRuntimeError("camera_to_world final row must be homogeneous")
    basis = (
        (matrix[0], matrix[1], matrix[2]),
        (matrix[4], matrix[5], matrix[6]),
        (matrix[8], matrix[9], matrix[10]),
    )
    for axis in basis:
        length = math.sqrt(sum(component * component for component in axis))
        if abs(length - 1.0) > tolerance:
            raise BridgeRuntimeError("camera_to_world basis must use unit axes")
    for first, second in ((0, 1), (0, 2), (1, 2)):
        dot = sum(basis[first][index] * basis[second][index] for index in range(3))
        if abs(dot) > tolerance:
            raise BridgeRuntimeError("camera_to_world basis must be orthonormal")
    determinant = (
        matrix[0] * (matrix[5] * matrix[10] - matrix[6] * matrix[9])
        - matrix[1] * (matrix[4] * matrix[10] - matrix[6] * matrix[8])
        + matrix[2] * (matrix[4] * matrix[9] - matrix[5] * matrix[8])
    )
    if determinant <= 0.0 or abs(determinant - 1.0) > tolerance * 4.0:
        raise BridgeRuntimeError("camera_to_world basis must be rigid and right-handed")
    return matrix


def rgba_numpy_to_top_left_bytes(
    value: object,
    *,
    flip_vertical: bool = True,
    limits: BridgeLimits = DEFAULT_BRIDGE_LIMITS,
) -> tuple[bytes, int, int]:
    """Normalize a TD RGBA NumPy array to bounded top-left-origin RGBA8 bytes."""

    if not isinstance(flip_vertical, bool):
        raise BridgeRuntimeError("flip_vertical must be boolean")
    try:
        import numpy as np
    except ImportError as exc:  # pragma: no cover - TouchDesigner ships NumPy
        raise BridgeRuntimeError("NumPy is required only for TOP conversion") from exc
    try:
        array = np.asarray(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise BridgeRuntimeError("RGBA input cannot be converted to an array") from exc
    if array.ndim != 3 or array.shape[2] != 4:
        raise BridgeRuntimeError("RGBA input must have shape height x width x 4")
    height, width, _ = (int(item) for item in array.shape)
    if width < 1 or height < 1:
        raise BridgeRuntimeError("RGBA input dimensions must be positive")
    if width > limits.max_source_width or height > limits.max_source_height:
        raise BridgeRuntimeError("RGBA input dimensions exceed bridge limits")
    if width * height > limits.max_source_pixels:
        raise BridgeRuntimeError("RGBA input exceeds the bridge pixel limit")
    if int(array.nbytes) > limits.max_input_array_bytes:
        raise BridgeRuntimeError("RGBA input exceeds the bridge byte limit")
    if array.dtype == np.uint8:
        normalized = array
    elif array.dtype.kind == "f":
        normalized_float = np.nan_to_num(
            array.astype(np.float32, copy=False),
            copy=True,
            nan=0.0,
            posinf=1.0,
            neginf=0.0,
        )
        np.clip(normalized_float, 0.0, 1.0, out=normalized_float)
        normalized = np.floor(normalized_float * 255.0 + 0.5).astype(np.uint8)
    else:
        raise BridgeRuntimeError("RGBA input must use uint8 or a floating dtype")
    if flip_vertical:
        normalized = np.flip(normalized, axis=0)
    return np.ascontiguousarray(normalized).tobytes(order="C"), width, height


def atlas_result_to_touchdesigner_numpy(
    result: ImmutableAtlasResult,
    *,
    flip_vertical: bool = True,
) -> object:
    """Convert immutable top-left RGBA8 atlas bytes to TD float32 orientation."""

    if not isinstance(result, ImmutableAtlasResult):
        raise BridgeRuntimeError("result must be an ImmutableAtlasResult")
    if not isinstance(flip_vertical, bool):
        raise BridgeRuntimeError("flip_vertical must be boolean")
    try:
        import numpy as np
    except ImportError as exc:  # pragma: no cover - TouchDesigner ships NumPy
        raise BridgeRuntimeError("NumPy is required only for TOP conversion") from exc
    expected = result.width * result.height * 4
    if len(result.payload) != expected:
        raise BridgeRuntimeError("atlas payload length changed after validation")
    array = np.frombuffer(result.payload, dtype=np.uint8).reshape(
        result.height, result.width, 4
    )
    converted = array.astype(np.float32) * (1.0 / 255.0)
    if flip_vertical:
        converted = np.flip(converted, axis=0)
    return np.ascontiguousarray(converted)


def _conservative_identifier(value: str, prefix: str) -> str:
    if len(value) <= 64 and _IDENTIFIER.fullmatch(value) is not None:
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]
    return prefix + "-" + digest


def _consumer_session_id(producer_session_id: str, calibration_digest: str) -> str:
    payload = (producer_session_id + "\0" + calibration_digest).encode("utf-8")
    return "moge2-session-" + hashlib.sha256(payload).hexdigest()[:24]


def derive_result_mappings(
    result: ImmutableAtlasResult,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Derive frame-state and camera metadata mappings for the same atlas."""

    if not isinstance(result, ImmutableAtlasResult):
        raise BridgeRuntimeError("result must be an ImmutableAtlasResult")
    near_metres = result.depth_scale_bias[1] + result.depth_scale_bias[0]
    far_metres = result.depth_scale_bias[1] + result.depth_scale_bias[0] * 65535.0
    calibration_content = {
        "width": result.source_width,
        "height": result.height,
        "intrinsics_pixels": list(result.intrinsics),
        "depth_scale_bias": list(result.depth_scale_bias),
        "camera_to_world": list(result.camera_to_world),
        "near_metres": near_metres,
        "far_metres": far_metres,
    }
    canonical = json.dumps(
        calibration_content,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    calibration_digest = hashlib.sha256(canonical).hexdigest()
    calibration_id = "moge2-" + calibration_digest[:16]
    session_id = _consumer_session_id(result.producer_session_id, calibration_digest)
    frame_state = {
        "version": FRAME_STATE_VERSION,
        "session_id": session_id,
        "frame_id": result.frame_id,
        "timestamp_ns": result.timestamp_ns,
        "width": result.source_width,
        "height": result.height,
        "calibration_id": calibration_id,
        "calibration_digest": calibration_digest,
        "valid_fraction": result.valid_fraction,
        "confidence_mean": result.valid_fraction,
    }
    generation_id = _conservative_identifier(
        _safe_text(result.generation_id, "generation_id", 256), "generation"
    )
    camera_metadata = {
        "version": CAMERA_METADATA_VERSION,
        "session_id": session_id,
        "frame_id": result.frame_id,
        "timestamp_ns": result.timestamp_ns,
        "width": result.source_width,
        "height": result.height,
        "generation_id": generation_id,
        "intrinsics_pixels": list(result.intrinsics),
        "depth_scale_bias": list(result.depth_scale_bias),
        "camera_to_world": list(result.camera_to_world),
        "near_metres": near_metres,
        "far_metres": far_metres,
        "calibration_id": calibration_id,
        "calibration_digest": calibration_digest,
    }
    return frame_state, camera_metadata


@dataclass(frozen=True)
class _IssuedRequest:
    timestamp_ns: int
    width: int
    height: int
    generation_id: str
    profile: str


class BridgeRuntime:
    """Threaded byte transport with an explicit TD-main-thread API boundary."""

    def __init__(
        self,
        config: BridgeConfig = BridgeConfig(),
        *,
        limits: BridgeLimits = DEFAULT_BRIDGE_LIMITS,
        sender_factory: Callable[..., Any] = TCPFrameSender,
        receiver_factory: Callable[..., Any] = WorldBusReceiver,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
        timestamp_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        if not isinstance(config, BridgeConfig):
            raise BridgeRuntimeError("config must be a BridgeConfig")
        if not isinstance(limits, BridgeLimits):
            raise BridgeRuntimeError("limits must be BridgeLimits")
        if config.capture_fps > limits.max_capture_fps:
            raise BridgeRuntimeError("capture_fps exceeds the selected bridge limit")
        self.config = config
        self.limits = limits
        self._sender_factory = sender_factory
        self._receiver_factory = receiver_factory
        self._monotonic_ns = monotonic_ns
        self._timestamp_ns = timestamp_ns
        self._owner_thread_id = threading.get_ident()
        self._lifecycle_lock = threading.RLock()
        self._state_lock = threading.Lock()
        self._running = False
        self._stop_event: Optional[threading.Event] = None
        self._outgoing: Optional[NewestFrameQueue] = None
        self._receiver: Optional[Any] = None
        self._sender_thread: Optional[threading.Thread] = None
        self._result_thread: Optional[threading.Thread] = None
        self._session_id = ""
        self._next_frame_id = 1
        self._last_capture_monotonic_ns: Optional[int] = None
        self._latest_result: Optional[ImmutableAtlasResult] = None
        self._latest_received_monotonic_ns: Optional[int] = None
        self._highest_accepted_source_frame_id = -1
        self._issued_requests: collections.OrderedDict[int, _IssuedRequest] = (
            collections.OrderedDict()
        )
        self._last_error = "none"
        self._error_counts: dict[str, int] = {}
        self._sent_frames = 0
        self._accepted_results = 0
        self._rejected_results = 0

    def _require_main_thread(self) -> None:
        if threading.get_ident() != self._owner_thread_id:
            raise BridgeRuntimeError("TouchDesigner conversion must run on the owner thread")

    def _record_error(self, code: str) -> None:
        if _IDENTIFIER.fullmatch(code) is None:
            code = "internal_error"
        with self._state_lock:
            self._last_error = code
            self._error_counts[code] = self._error_counts.get(code, 0) + 1

    def start(self) -> "BridgeRuntime":
        """Start once; repeated calls are harmless and stop/start creates fresh queues."""

        with self._lifecycle_lock:
            if self._running:
                return self
            worldbus_limits = _worldbus_limits(self.limits)
            result_worldbus_limits = _result_worldbus_limits(self.limits)
            stop_event = threading.Event()
            outgoing = NewestFrameQueue(worldbus_limits)
            receiver = self._receiver_factory(
                host=self.config.result_bind_host,
                tcp_port=self.config.result_tcp,
                udp_port=self.config.result_udp,
                stale_after_s=max(1.0, 3.0 / float(self.config.capture_fps)),
                limits=result_worldbus_limits,
            )
            try:
                receiver.start()
            except Exception:
                outgoing.close()
                try:
                    receiver.close()
                except Exception:
                    pass
                self._record_error("receiver_start_failed")
                raise
            self._stop_event = stop_event
            self._outgoing = outgoing
            self._receiver = receiver
            self._session_id = "td-moge2-" + uuid.uuid4().hex
            self._next_frame_id = 1
            self._last_capture_monotonic_ns = None
            with self._state_lock:
                self._latest_result = None
                self._latest_received_monotonic_ns = None
                self._highest_accepted_source_frame_id = -1
                self._issued_requests.clear()
            self._sender_thread = threading.Thread(
                target=self._sender_loop,
                args=(stop_event, outgoing),
                name="moge2-td-sender",
                daemon=True,
            )
            self._result_thread = threading.Thread(
                target=self._result_loop,
                args=(stop_event, receiver),
                name="moge2-td-results",
                daemon=True,
            )
            with self._state_lock:
                self._running = True
            try:
                self._sender_thread.start()
                self._result_thread.start()
            except Exception:
                with self._state_lock:
                    self._running = False
                stop_event.set()
                outgoing.close()
                receiver.close()
                for thread in (self._sender_thread, self._result_thread):
                    if thread is not None and thread.ident is not None:
                        thread.join(timeout=float(self.limits.thread_join_timeout_s))
                self._stop_event = None
                self._outgoing = None
                self._receiver = None
                self._sender_thread = None
                self._result_thread = None
                self._record_error("bridge_thread_start_failed")
                raise
        return self

    def stop(self) -> None:
        """Stop all owned sockets/threads; repeated calls are harmless."""

        with self._lifecycle_lock:
            if not self._running and self._receiver is None and self._outgoing is None:
                return
            with self._state_lock:
                self._running = False
            stop_event = self._stop_event
            outgoing = self._outgoing
            receiver = self._receiver
            sender_thread = self._sender_thread
            result_thread = self._result_thread
            if stop_event is not None:
                stop_event.set()
            if outgoing is not None:
                outgoing.close()
            if receiver is not None:
                try:
                    receiver.close()
                except Exception:
                    self._record_error("receiver_stop_failed")
            for thread in (sender_thread, result_thread):
                if thread is not None and thread.ident is not None:
                    thread.join(timeout=float(self.limits.thread_join_timeout_s))
            self._stop_event = None
            self._outgoing = None
            self._receiver = None
            self._sender_thread = None
            self._result_thread = None

    def _sender_loop(self, stop_event: threading.Event, outgoing: NewestFrameQueue) -> None:
        sender: Optional[Any] = None
        try:
            while not stop_event.is_set():
                try:
                    frame = outgoing.get(timeout=0.1)
                except TimeoutError:
                    continue
                except QueueClosed:
                    break
                try:
                    if sender is None:
                        sender = self._sender_factory(
                            self.config.worker_host,
                            self.config.worker_input_tcp,
                            timeout=float(self.limits.socket_timeout_s),
                            limits=_worldbus_limits(self.limits),
                        )
                    sender.send(frame)
                    with self._state_lock:
                        self._sent_frames += 1
                except Exception:
                    self._record_error("send_failed")
                    if sender is not None:
                        try:
                            sender.close()
                        except Exception:
                            pass
                    sender = None
        finally:
            if sender is not None:
                try:
                    sender.close()
                except Exception:
                    pass

    def _result_loop(self, stop_event: threading.Event, receiver: Any) -> None:
        while not stop_event.is_set():
            try:
                frame = receiver.frames.get(timeout=0.1)
            except TimeoutError:
                continue
            except QueueClosed:
                break
            try:
                result = validate_moge2_result_frame(frame, limits=self.limits)
            except (BridgeRuntimeError, WorldBusError, ValueError):
                with self._state_lock:
                    self._rejected_results += 1
                self._record_error("result_rejected")
                continue
            with self._state_lock:
                source_session = self._session_id
                highest_source_frame = self._highest_accepted_source_frame_id
                issued = self._issued_requests.get(result.source_frame_id)
            if result.source_producer_session_id != source_session:
                with self._state_lock:
                    self._rejected_results += 1
                self._record_error("foreign_source_session")
                continue
            if issued is None:
                with self._state_lock:
                    self._rejected_results += 1
                self._record_error("unissued_source_frame")
                continue
            if result.source_frame_id <= highest_source_frame:
                with self._state_lock:
                    self._rejected_results += 1
                self._record_error("stale_source_frame")
                continue
            if (
                result.source_timestamp_ns != issued.timestamp_ns
                or result.original_source_width != issued.width
                or result.original_source_height != issued.height
                or result.generation_id != issued.generation_id
                or result.profile != issued.profile
            ):
                with self._state_lock:
                    self._rejected_results += 1
                self._record_error("source_correlation_failed")
                continue
            try:
                local_timestamp = _require_int(
                    self._timestamp_ns(), "local_timestamp_ns", 1, (1 << 63) - 1
                )
                local_monotonic = self._monotonic_ns()
            except (BridgeRuntimeError, TypeError, ValueError):
                with self._state_lock:
                    self._rejected_results += 1
                self._record_error("local_clock_invalid")
                continue
            result = replace(result, timestamp_ns=local_timestamp)
            with self._state_lock:
                self._latest_result = result
                self._latest_received_monotonic_ns = local_monotonic
                self._highest_accepted_source_frame_id = result.source_frame_id
                for issued_frame_id in tuple(self._issued_requests):
                    if issued_frame_id <= result.source_frame_id:
                        del self._issued_requests[issued_frame_id]
                self._accepted_results += 1

    def capture_due(self) -> bool:
        with self._state_lock:
            if not self._running:
                return False
            last = self._last_capture_monotonic_ns
        if last is None:
            return True
        interval = int(1_000_000_000 / float(self.config.capture_fps))
        return self._monotonic_ns() - last >= interval

    def capture_numpy(
        self,
        rgba_array: object,
        *,
        generation_id: str = "streamdiffusion",
        timestamp_ns: Optional[int] = None,
        flip_vertical: Optional[bool] = None,
    ) -> Optional[CaptureReceipt]:
        """Main-thread capture from an already supplied NumPy array."""

        self._require_main_thread()
        now = self._monotonic_ns()
        interval = int(1_000_000_000 / float(self.config.capture_fps))
        with self._state_lock:
            if not self._running:
                return None
            last = self._last_capture_monotonic_ns
            if last is not None and now - last < interval:
                return None
        selected_flip = self.config.flip_vertical if flip_vertical is None else flip_vertical
        payload, width, height = rgba_numpy_to_top_left_bytes(
            rgba_array, flip_vertical=selected_flip, limits=self.limits
        )
        generation = _conservative_identifier(
            _safe_text(generation_id, "generation_id", 256), "generation"
        )
        selected_timestamp = self._timestamp_ns() if timestamp_ns is None else timestamp_ns
        selected_timestamp = _require_int(
            selected_timestamp, "timestamp_ns", 1, (1 << 63) - 1
        )
        with self._state_lock:
            frame_id = self._next_frame_id
            self._next_frame_id += 1
            self._last_capture_monotonic_ns = now
            session_id = self._session_id
            outgoing = self._outgoing
        focal = float(max(width, height))
        metadata = {
            "worldbus_version": 1,
            "frame_id": frame_id,
            "timestamp_ns": str(selected_timestamp),
            "width": width,
            "height": height,
            "pixel_format": "rgba8",
            "payload_bytes": len(payload),
            "intrinsics": [focal, focal, width / 2.0, height / 2.0],
            "depth_scale_bias": [1.0, 0.0],
            "camera_to_world": list(_IDENTITY),
            "generation_id": generation,
            "producer_session_id": session_id,
            "moge2_request_contract": REQUEST_CONTRACT,
            "moge2_profile": self.config.profile,
            "moge2_source_orientation": "top_left" if selected_flip else "touchdesigner_native",
        }
        frame = make_frame(metadata, payload, _worldbus_limits(self.limits))
        queued = False
        if outgoing is not None:
            issued = _IssuedRequest(
                timestamp_ns=selected_timestamp,
                width=width,
                height=height,
                generation_id=generation,
                profile=self.config.profile,
            )
            with self._state_lock:
                self._issued_requests[frame_id] = issued
                self._issued_requests.move_to_end(frame_id)
                while len(self._issued_requests) > _MAX_ISSUED_REQUESTS:
                    self._issued_requests.popitem(last=False)
            try:
                queued = outgoing.put(frame)
            except QueueClosed:
                self._record_error("outgoing_closed")
            if not queued:
                with self._state_lock:
                    self._issued_requests.pop(frame_id, None)
        return CaptureReceipt(
            frame_id=frame_id,
            timestamp_ns=selected_timestamp,
            width=width,
            height=height,
            queued=queued,
        )

    def capture_top(
        self,
        top: object,
        *,
        generation_id: str = "streamdiffusion",
        flip_vertical: Optional[bool] = None,
    ) -> Optional[CaptureReceipt]:
        """Main-thread convenience wrapper around ``TOP.numpyArray()``."""

        self._require_main_thread()
        method = getattr(top, "numpyArray", None)
        if not callable(method):
            raise BridgeRuntimeError("IN_RGB does not expose numpyArray()")
        try:
            array = method(delayed=True)
        except TypeError:
            array = method()
        return self.capture_numpy(
            array, generation_id=generation_id, flip_vertical=flip_vertical
        )

    @property
    def producer_session_id(self) -> str:
        with self._state_lock:
            return self._session_id

    def _result_ttl_ns(self) -> int:
        return int(max(1.0, 3.0 / float(self.config.capture_fps)) * 1_000_000_000)

    def latest_result(self) -> Optional[ImmutableAtlasResult]:
        with self._state_lock:
            result = self._latest_result
            received = self._latest_received_monotonic_ns
        if result is None or received is None:
            return None
        if self._monotonic_ns() - received > self._result_ttl_ns():
            return None
        return result

    def upload_latest_to_top(self, script_top: object) -> bool:
        """Main-thread conversion and ``copyNumpyArray`` call for RESULT_ATLAS."""

        self._require_main_thread()
        result = self.latest_result()
        if result is None:
            return False
        return self.upload_result_to_top(script_top, result)

    def upload_result_to_top(
        self, script_top: object, result: ImmutableAtlasResult
    ) -> bool:
        """Upload one exact immutable snapshot selected by the main-thread tick."""

        self._require_main_thread()
        if not isinstance(result, ImmutableAtlasResult):
            raise BridgeRuntimeError("result must be an ImmutableAtlasResult")
        method = getattr(script_top, "copyNumpyArray", None)
        if not callable(method):
            raise BridgeRuntimeError("RESULT_ATLAS does not expose copyNumpyArray()")
        array = atlas_result_to_touchdesigner_numpy(
            result, flip_vertical=self.config.flip_vertical
        )
        method(array)
        return True

    def status(self) -> dict[str, Any]:
        with self._state_lock:
            running = self._running
            stored_latest = self._latest_result
            received = self._latest_received_monotonic_ns
            sent = self._sent_frames
            accepted = self._accepted_results
            rejected = self._rejected_results
            last_error = self._last_error
            error_count = sum(self._error_counts.values())
            outgoing = self._outgoing
        now = self._monotonic_ns()
        result_age_ns = None if received is None else max(0, now - received)
        result_fresh = (
            stored_latest is not None
            and result_age_ns is not None
            and result_age_ns <= self._result_ttl_ns()
        )
        latest = stored_latest if result_fresh else None
        queue_stats = outgoing.stats if outgoing is not None else {}
        bridge_state = "stopped"
        if running:
            bridge_state = "running" if stored_latest is None or result_fresh else "stale"
        return {
            "state": bridge_state,
            "profile": self.config.profile,
            "capture_fps": float(self.config.capture_fps),
            "sent_frames": sent,
            "accepted_results": accepted,
            "rejected_results": rejected,
            "outgoing_superseded": int(queue_stats.get("superseded", 0)),
            "outgoing_pending": int(queue_stats.get("pending", 0)),
            "latest_result_frame_id": -1 if latest is None else latest.frame_id,
            "latest_source_frame_id": -1 if latest is None else latest.source_frame_id,
            "result_fresh": result_fresh,
            "result_age_ms": -1.0 if result_age_ns is None else result_age_ns / 1_000_000.0,
            "last_error": last_error,
            "error_count": error_count,
        }


def _find_parameter(owner: object, name: str) -> Optional[Any]:
    parameters = getattr(owner, "par", None)
    if parameters is not None:
        for candidate in (name, name.lower(), name[:1].upper() + name[1:]):
            try:
                value = getattr(parameters, candidate)
            except (AttributeError, TypeError):
                continue
            if value is not None:
                return value
    method = getattr(owner, "pars", None)
    if callable(method):
        try:
            for parameter in method():
                if str(getattr(parameter, "name", "")).lower() == name.lower():
                    return parameter
        except Exception:
            pass
    return None


def _parameter_value(owner: object, name: str, default: object) -> object:
    parameter = _find_parameter(owner, name)
    if parameter is None:
        return default
    evaluator = getattr(parameter, "eval", None)
    if callable(evaluator):
        try:
            return evaluator()
        except Exception:
            return default
    return getattr(parameter, "val", default)


def _set_parameter(owner: object, name: str, value: object) -> bool:
    parameter = _find_parameter(owner, name)
    if parameter is None:
        return False
    try:
        parameter.val = value
        return True
    except Exception:
        return False


def _child(owner: object, name: str) -> Optional[Any]:
    method = getattr(owner, "op", None)
    if not callable(method):
        return None
    try:
        return method(name)
    except Exception:
        return None


def _write_text_mapping(dat: object, mapping: Mapping[str, Any]) -> bool:
    encoded = json.dumps(
        dict(mapping), allow_nan=False, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ) + "\n"
    try:
        dat.text = encoded
        return True
    except Exception:
        pass
    clear = getattr(dat, "clear", None)
    append = getattr(dat, "appendRow", None)
    if callable(clear) and callable(append):
        clear()
        append(("key", "value"))
        for key in sorted(mapping):
            value = mapping[key]
            if isinstance(value, (dict, list, tuple)):
                value = json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True)
            append((key, str(value)))
        return True
    return False


def _write_status(dat: object, mapping: Mapping[str, Any]) -> bool:
    clear = getattr(dat, "clear", None)
    append = getattr(dat, "appendRow", None)
    if callable(clear) and callable(append):
        clear()
        append(("metric", "value"))
        for key in sorted(mapping):
            append((key, str(mapping[key])))
        return True
    return _write_text_mapping(dat, mapping)


def _update_scale_bias(bridge_comp: object, scale: float, bias: float) -> bool:
    node = _child(bridge_comp, "DEPTH_SCALE_BIAS")
    if node is None:
        return False
    required_written = []
    for names, value in (
        (("colorr", "color1r"), scale),
        (("colorg", "color1g"), bias),
        (("colorb", "color1b"), 0.0),
        (("colora", "color1a", "alpha"), 1.0),
    ):
        written = False
        for name in names:
            if _set_parameter(node, name, value):
                written = True
                break
        required_written.append(written)
    return required_written[0] and required_written[1]


def _component_config(bridge_comp: object) -> BridgeConfig:
    capture_fps = _parameter_value(bridge_comp, "Capturefps", 5.0)
    if isinstance(capture_fps, int):
        capture_fps = float(capture_fps)
    return BridgeConfig(
        profile=str(_parameter_value(bridge_comp, "Profile", "3080ti_16gb")),
        worker_host=str(_parameter_value(bridge_comp, "Workerhost", "127.0.0.1")),
        worker_input_tcp=int(_parameter_value(bridge_comp, "Workerinputtcp", 9211)),
        worker_input_udp=int(_parameter_value(bridge_comp, "Workerinputudp", 9210)),
        result_bind_host=str(
            _parameter_value(bridge_comp, "Resultbindhost", "127.0.0.1")
        ),
        result_tcp=int(_parameter_value(bridge_comp, "Resulttcp", 9221)),
        result_udp=int(_parameter_value(bridge_comp, "Resultudp", 9220)),
        capture_fps=float(capture_fps),
        flip_vertical=bool(_parameter_value(bridge_comp, "Flipvertical", True)),
    )


_GLOBAL_LOCK = threading.RLock()
_GLOBAL_RUNTIME: Optional[BridgeRuntime] = None
_GLOBAL_SIGNATURE: Optional[tuple[int, BridgeConfig]] = None
_GLOBAL_PUBLISHED_KEY: Optional[tuple[str, int]] = None
_GLOBAL_UPLOADED_KEY: Optional[tuple[str, int]] = None
_GLOBAL_STAGED_RESULT: Optional[ImmutableAtlasResult] = None
_GLOBAL_BRIDGE_COMP: Optional[object] = None
_GLOBAL_DISABLED_STATUS = {
    "state": "disabled",
    "detail": "enable the bridge first, then start the external worker",
    "last_error": "none",
}


def tick(bridge_comp: object) -> dict[str, Any]:
    """Execute-DAT callback: atomically upload and publish one exact result.

    ``RESULT_ATLAS`` is a Script TOP and TouchDesigner 2025 does not expose an
    ``alwayscook`` parameter for it.  The Execute DAT therefore stages the
    immutable result and forces that TOP to cook on the main thread.  The
    Script TOP callback records the exact key it copied; routes remain invalid
    until that key and the metadata/calibration writes all match.
    """

    global _GLOBAL_RUNTIME, _GLOBAL_SIGNATURE, _GLOBAL_PUBLISHED_KEY
    global _GLOBAL_STAGED_RESULT, _GLOBAL_UPLOADED_KEY
    global _GLOBAL_BRIDGE_COMP
    _GLOBAL_BRIDGE_COMP = bridge_comp
    enabled = bool(_parameter_value(bridge_comp, "Enabled", False))
    if not enabled:
        stop(bridge_comp)
        return dict(_GLOBAL_DISABLED_STATUS)
    try:
        config = _component_config(bridge_comp)
    except (BridgeRuntimeError, TypeError, ValueError):
        failed = {
            "state": "error",
            "detail": "invalid bridge configuration",
            "last_error": "config_invalid",
        }
        _set_parameter(bridge_comp, "Resultvalid", False)
        status_dat = _child(bridge_comp, "STATUS")
        if status_dat is not None:
            _write_status(status_dat, failed)
        return failed
    signature = (id(bridge_comp), config)
    with _GLOBAL_LOCK:
        if _GLOBAL_RUNTIME is None or _GLOBAL_SIGNATURE != signature:
            if _GLOBAL_RUNTIME is not None:
                _GLOBAL_RUNTIME.stop()
            try:
                _GLOBAL_RUNTIME = BridgeRuntime(config).start()
            except Exception:
                _GLOBAL_RUNTIME = None
                _GLOBAL_SIGNATURE = None
                failed = {
                    "state": "error",
                    "detail": "result receiver could not start",
                    "last_error": "receiver_start_failed",
                }
                _set_parameter(bridge_comp, "Resultvalid", False)
                status_dat = _child(bridge_comp, "STATUS")
                if status_dat is not None:
                    _write_status(status_dat, failed)
                return failed
            _GLOBAL_SIGNATURE = signature
            _GLOBAL_PUBLISHED_KEY = None
            _GLOBAL_UPLOADED_KEY = None
            _GLOBAL_STAGED_RESULT = None
        runtime = _GLOBAL_RUNTIME
    assert runtime is not None
    generation_id = str(_parameter_value(bridge_comp, "Generationid", "streamdiffusion"))
    if runtime.capture_due():
        input_top = _child(bridge_comp, "IN_RGB")
        if input_top is None:
            runtime._record_error("source_unavailable")
        else:
            try:
                receipt = runtime.capture_top(input_top, generation_id=generation_id)
                if receipt is not None:
                    _set_parameter(bridge_comp, "Sourceframeid", receipt.frame_id)
            except (BridgeRuntimeError, ValueError, TypeError):
                runtime._record_error("capture_failed")
    result = runtime.latest_result()
    if result is None:
        with _GLOBAL_LOCK:
            if runtime is _GLOBAL_RUNTIME:
                _GLOBAL_STAGED_RESULT = None
                _GLOBAL_UPLOADED_KEY = None
                _GLOBAL_PUBLISHED_KEY = None
    if result is not None:
        with _GLOBAL_LOCK:
            ready = (
                runtime is _GLOBAL_RUNTIME
                and _GLOBAL_STAGED_RESULT is not None
                and _GLOBAL_STAGED_RESULT.key == result.key
                and _GLOBAL_UPLOADED_KEY == result.key
                and _GLOBAL_PUBLISHED_KEY == result.key
            )
        if not ready:
            # Invalidate the four downstream route switches before the atlas
            # can change.  Publishing is committed only after the forced cook
            # confirms that this exact immutable result was copied.
            _set_parameter(bridge_comp, "Resultvalid", False)
            with _GLOBAL_LOCK:
                if runtime is _GLOBAL_RUNTIME:
                    _GLOBAL_STAGED_RESULT = result
                    _GLOBAL_UPLOADED_KEY = None
                    _GLOBAL_PUBLISHED_KEY = None
            atlas = _child(bridge_comp, "RESULT_ATLAS")
            cook = getattr(atlas, "cook", None) if atlas is not None else None
            cook_attempted = False
            if callable(cook):
                try:
                    cook(force=True)
                    cook_attempted = True
                except Exception:
                    runtime._record_error("atlas_cook_failed")
            else:
                runtime._record_error("atlas_cook_unavailable")
            with _GLOBAL_LOCK:
                uploaded = (
                    cook_attempted
                    and runtime is _GLOBAL_RUNTIME
                    and _GLOBAL_STAGED_RESULT is not None
                    and _GLOBAL_STAGED_RESULT.key == result.key
                    and _GLOBAL_UPLOADED_KEY == result.key
                )
            if not uploaded:
                if cook_attempted:
                    runtime._record_error("atlas_upload_unconfirmed")
                with _GLOBAL_LOCK:
                    if runtime is _GLOBAL_RUNTIME:
                        _GLOBAL_STAGED_RESULT = None
                        _GLOBAL_UPLOADED_KEY = None
                        _GLOBAL_PUBLISHED_KEY = None
            else:
                frame_state, camera_metadata = derive_result_mappings(result)
                frame_dat = _child(bridge_comp, "FRAME_STATE")
                camera_dat = _child(bridge_comp, "CAMERA_METADATA")
                frame_written = (
                    frame_dat is not None and _write_text_mapping(frame_dat, frame_state)
                )
                camera_written = (
                    camera_dat is not None
                    and _write_text_mapping(camera_dat, camera_metadata)
                )
                calibration_written = _update_scale_bias(
                    bridge_comp, *result.depth_scale_bias
                )
                if frame_written and camera_written and calibration_written:
                    with _GLOBAL_LOCK:
                        if (
                            runtime is _GLOBAL_RUNTIME
                            and _GLOBAL_STAGED_RESULT is not None
                            and _GLOBAL_STAGED_RESULT.key == result.key
                            and _GLOBAL_UPLOADED_KEY == result.key
                        ):
                            _GLOBAL_PUBLISHED_KEY = result.key
                else:
                    runtime._record_error("metadata_publish_failed")
                    with _GLOBAL_LOCK:
                        if runtime is _GLOBAL_RUNTIME:
                            _GLOBAL_PUBLISHED_KEY = None
    with _GLOBAL_LOCK:
        published = (
            runtime is _GLOBAL_RUNTIME
            and result is not None
            and _GLOBAL_STAGED_RESULT is not None
            and _GLOBAL_STAGED_RESULT.key == result.key
            and _GLOBAL_UPLOADED_KEY == result.key
            and _GLOBAL_PUBLISHED_KEY == result.key
        )
    _set_parameter(bridge_comp, "Resultvalid", published)
    current = runtime.status()
    detail = (
        "synchronized atlas uploaded and ready"
        if published
        else "waiting for confirmed synchronized atlas upload"
    )
    current["detail"] = detail
    status_dat = _child(bridge_comp, "STATUS")
    if status_dat is not None:
        _write_status(status_dat, current)
    return current


def on_script_top_cook(script_op: object) -> bool:
    """Upload the staged snapshot and atomically confirm its exact result key."""

    global _GLOBAL_STAGED_RESULT, _GLOBAL_UPLOADED_KEY, _GLOBAL_PUBLISHED_KEY
    with _GLOBAL_LOCK:
        runtime = _GLOBAL_RUNTIME
        staged = _GLOBAL_STAGED_RESULT
        bridge_comp = _GLOBAL_BRIDGE_COMP
    if runtime is None or staged is None:
        return False
    try:
        copied = bool(runtime.upload_result_to_top(script_op, staged))
    except Exception:
        copied = False
    if copied:
        with _GLOBAL_LOCK:
            if (
                runtime is _GLOBAL_RUNTIME
                and _GLOBAL_STAGED_RESULT is staged
                and _GLOBAL_STAGED_RESULT.key == staged.key
            ):
                _GLOBAL_UPLOADED_KEY = staged.key
                return True
        runtime._record_error("upload_superseded")
        return False
    runtime._record_error("upload_failed")
    with _GLOBAL_LOCK:
        if runtime is _GLOBAL_RUNTIME:
            _GLOBAL_STAGED_RESULT = None
            _GLOBAL_UPLOADED_KEY = None
            _GLOBAL_PUBLISHED_KEY = None
    if bridge_comp is not None:
        _set_parameter(bridge_comp, "Resultvalid", False)
    return False


def stop(bridge_comp: Optional[object] = None) -> None:
    """Execute-DAT exit callback; safe to call repeatedly."""

    global _GLOBAL_RUNTIME, _GLOBAL_SIGNATURE, _GLOBAL_PUBLISHED_KEY
    global _GLOBAL_STAGED_RESULT, _GLOBAL_UPLOADED_KEY
    global _GLOBAL_BRIDGE_COMP
    with _GLOBAL_LOCK:
        runtime = _GLOBAL_RUNTIME
        _GLOBAL_RUNTIME = None
        _GLOBAL_SIGNATURE = None
        _GLOBAL_PUBLISHED_KEY = None
        _GLOBAL_UPLOADED_KEY = None
        _GLOBAL_STAGED_RESULT = None
        _GLOBAL_BRIDGE_COMP = None
    if runtime is not None:
        runtime.stop()
    if bridge_comp is not None:
        _set_parameter(bridge_comp, "Resultvalid", False)
        status_dat = _child(bridge_comp, "STATUS")
        if status_dat is not None:
            _write_status(status_dat, _GLOBAL_DISABLED_STATUS)


def status() -> dict[str, Any]:
    """Return bounded counters only; never image bytes, prompts, or endpoints."""

    with _GLOBAL_LOCK:
        runtime = _GLOBAL_RUNTIME
    return dict(_GLOBAL_DISABLED_STATUS) if runtime is None else runtime.status()


__all__ = [
    "ATLAS_CONTRACT",
    "BridgeConfig",
    "BridgeLimits",
    "BridgeRuntime",
    "BridgeRuntimeError",
    "CAMERA_METADATA_VERSION",
    "CaptureReceipt",
    "DEFAULT_BRIDGE_LIMITS",
    "FRAME_STATE_VERSION",
    "ImmutableAtlasResult",
    "REQUEST_CONTRACT",
    "atlas_result_to_touchdesigner_numpy",
    "derive_result_mappings",
    "on_script_top_cook",
    "rgba_numpy_to_top_left_bytes",
    "status",
    "stop",
    "tick",
    "validate_moge2_result_frame",
]
