"""Import-safe TouchDesigner receiver for the webcam Depth Anything sensor.

The webcam and inference runtime stay in an external process.  This module
receives only bounded immutable WorldBus bytes containing uint16 pseudo-metre
depth, a foreground mask, and heuristic confidence.  TouchDesigner OP access,
NumPy conversion, Script TOP upload, and DAT/parameter writes happen only from
``tick``/``on_script_top_cook`` on TouchDesigner's main thread.

The source is designed to be embedded in a Text DAT.  It intentionally does
not import OpenCV, PyTorch, Depth Anything, camera SDKs, or TouchDesigner at
module import time.
"""

from __future__ import annotations

import builtins
import collections
import hashlib
import ipaddress
import json
import math
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, replace
from typing import Any, Callable, Mapping, Optional, Sequence, Tuple


_EMBEDDED_FLEXGPU_SRC = ""


def _ensure_local_src_path() -> None:
    """Find the checked-out ``src`` tree without recursive filesystem scans."""

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
    # A local installer imports runtime_pipeline from <repo>/touchdesigner.
    # Embedded DAT modules use an OP path as __file__, so that authorized
    # sys.path entry is the reliable bounded fallback in TouchDesigner 2025.
    for search_root in tuple(sys.path)[:128]:
        if not isinstance(search_root, str) or not search_root or len(search_root) > 4096:
            continue
        candidates.append(os.path.abspath(os.path.join(search_root, "src")))
        candidates.append(os.path.abspath(os.path.join(search_root, "..", "src")))
    module_file = globals().get("__file__")
    if isinstance(module_file, str) and module_file:
        candidates.append(
            os.path.abspath(os.path.join(os.path.dirname(module_file), "..", "src"))
        )
    candidates.append(os.path.abspath(os.path.join(os.getcwd(), "src")))
    for candidate in candidates:
        if (
            isinstance(candidate, str)
            and len(candidate) <= 4096
            and os.path.isfile(os.path.join(candidate, "flexgpu", "worldbus.py"))
            and os.path.isfile(
                os.path.join(candidate, "flexgpu", "depth_anything_transport.py")
            )
        ):
            if candidate not in sys.path:
                sys.path.insert(0, candidate)
            return


try:
    from flexgpu.depth_anything_transport import (
        CONFIDENCE_SEMANTICS,
        DEPTH_ENCODING,
        DEPTH_SEMANTICS,
        MASK_SEMANTICS,
        MAX_HEIGHT,
        MAX_PIXELS,
        MAX_WIDTH,
        SENSOR_CONTRACT,
    )
    from flexgpu.worldbus import (
        QueueClosed,
        WorldBusError,
        WorldBusLimits,
        WorldBusReceiver,
        WorldFrame,
        validate_frame,
    )
except ModuleNotFoundError as exc:
    if not str(getattr(exc, "name", "")).startswith("flexgpu"):
        raise
    _ensure_local_src_path()
    from flexgpu.depth_anything_transport import (  # type: ignore[no-redef]
        CONFIDENCE_SEMANTICS,
        DEPTH_ENCODING,
        DEPTH_SEMANTICS,
        MASK_SEMANTICS,
        MAX_HEIGHT,
        MAX_PIXELS,
        MAX_WIDTH,
        SENSOR_CONTRACT,
    )
    from flexgpu.worldbus import (  # type: ignore[no-redef]
        QueueClosed,
        WorldBusError,
        WorldBusLimits,
        WorldBusReceiver,
        WorldFrame,
        validate_frame,
    )


FRAME_STATE_VERSION = "flexgpu-frame-state/v1"
SENSOR_COORDINATES = "sensor_local_x_right_y_up_z_backward_metres"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.-]+$")
_CALIBRATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IDENTITY = (
    1.0, 0.0, 0.0, 0.0,
    0.0, 1.0, 0.0, 0.0,
    0.0, 0.0, 1.0, 0.0,
    0.0, 0.0, 0.0, 1.0,
)
_MAX_RETIRED_SESSIONS = 64


class SensorBridgeError(RuntimeError):
    """A sensor result, configuration, or lifecycle action is invalid."""


@dataclass(frozen=True)
class SensorBridgeLimits:
    """Hard allocation, geometry, time, and thread bounds."""

    max_width: int = MAX_WIDTH
    max_height: int = MAX_HEIGHT
    max_pixels: int = MAX_PIXELS
    max_payload_bytes: int = MAX_PIXELS * 4
    min_stale_ms: float = 100.0
    max_stale_ms: float = 10_000.0
    max_future_capture_ms: float = 2_000.0
    socket_timeout_s: float = 30.0
    thread_join_timeout_s: float = 1.5

    def __post_init__(self) -> None:
        for value, label in (
            (self.max_width, "max_width"),
            (self.max_height, "max_height"),
            (self.max_pixels, "max_pixels"),
            (self.max_payload_bytes, "max_payload_bytes"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise SensorBridgeError(label + " must be a positive integer")
        for value, label in (
            (self.min_stale_ms, "min_stale_ms"),
            (self.max_stale_ms, "max_stale_ms"),
            (self.max_future_capture_ms, "max_future_capture_ms"),
            (self.socket_timeout_s, "socket_timeout_s"),
            (self.thread_join_timeout_s, "thread_join_timeout_s"),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) <= 0.0
            ):
                raise SensorBridgeError(label + " must be positive and finite")
        if self.max_stale_ms < self.min_stale_ms:
            raise SensorBridgeError("max_stale_ms must not be below min_stale_ms")
        if self.socket_timeout_s > 60.0:
            raise SensorBridgeError("socket_timeout_s must be at most 60 seconds")


DEFAULT_SENSOR_LIMITS = SensorBridgeLimits()


def _host(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise SensorBridgeError(label + " must be a host name or address")
    selected = value.strip()
    if (
        not selected
        or len(selected) > 255
        or any(ord(character) < 33 for character in selected)
        or "://" in selected
        or "@" in selected
    ):
        raise SensorBridgeError(label + " must not contain whitespace or credentials")
    return selected


def _port(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise SensorBridgeError(label + " must be an integer between 1 and 65535")
    return value


def _loopback_host(value: str) -> bool:
    if value.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _safe_text(value: object, label: str, maximum_bytes: int = 256) -> str:
    if not isinstance(value, str):
        raise SensorBridgeError(label + " must be a string")
    selected = value.strip()
    if (
        not selected
        or len(selected.encode("utf-8")) > maximum_bytes
        or any(ord(character) < 32 for character in selected)
    ):
        raise SensorBridgeError(label + " is empty, too long, or contains controls")
    return selected


def _require_int(value: object, label: str, low: int, high: int) -> int:
    if isinstance(value, bool):
        raise SensorBridgeError(label + " must be an integer")
    if isinstance(value, str):
        if re.fullmatch(r"0|[1-9][0-9]*", value) is None:
            raise SensorBridgeError(label + " must be a decimal integer")
        parsed = int(value)
    elif isinstance(value, int):
        parsed = value
    else:
        raise SensorBridgeError(label + " must be an integer")
    if parsed < low or parsed > high:
        raise SensorBridgeError(label + " is outside the supported range")
    return parsed


def _finite(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SensorBridgeError(label + " must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise SensorBridgeError(label + " must be finite")
    return number


def _finite_pair(value: object, label: str) -> Tuple[float, float]:
    if isinstance(value, (str, bytes, bytearray)):
        raise SensorBridgeError(label + " must contain two numbers")
    try:
        if len(value) != 2:  # type: ignore[arg-type]
            raise SensorBridgeError(label + " must contain two numbers")
        first = _finite(value[0], label)  # type: ignore[index]
        second = _finite(value[1], label)  # type: ignore[index]
    except TypeError as exc:
        raise SensorBridgeError(label + " must contain two numbers") from exc
    return first, second


@dataclass(frozen=True)
class SensorBridgeConfig:
    """Result-only receiver settings mirrored by bridge parameters.

    ``result_udp`` is reserved metadata for future compatibility; v1 binds
    only ``result_tcp``.  A non-loopback TCP bind is possible only with the
    explicit trusted-network opt-in because WorldBus has no authentication or
    encryption.
    """

    bind_host: str = "127.0.0.1"
    result_tcp: int = 9241
    result_udp: int = 9240
    stale_ms: float = 800.0
    flip_vertical: bool = True
    mirror_horizontal: bool = True
    allow_trusted_network: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "bind_host", _host(self.bind_host, "bind_host"))
        _port(self.result_tcp, "result_tcp")
        _port(self.result_udp, "result_udp")
        if not isinstance(self.allow_trusted_network, bool):
            raise SensorBridgeError("allow_trusted_network must be boolean")
        if not _loopback_host(self.bind_host) and not self.allow_trusted_network:
            raise SensorBridgeError(
                "non-loopback bind_host requires explicit trusted-network opt-in"
            )
        if (
            isinstance(self.stale_ms, bool)
            or not isinstance(self.stale_ms, (int, float))
            or not math.isfinite(float(self.stale_ms))
            or not DEFAULT_SENSOR_LIMITS.min_stale_ms
            <= float(self.stale_ms)
            <= DEFAULT_SENSOR_LIMITS.max_stale_ms
        ):
            raise SensorBridgeError("stale_ms is outside the supported range")
        if not isinstance(self.flip_vertical, bool):
            raise SensorBridgeError("flip_vertical must be boolean")
        if not isinstance(self.mirror_horizontal, bool):
            raise SensorBridgeError("mirror_horizontal must be boolean")


@dataclass(frozen=True)
class ImmutableSensorResult:
    """A fully validated frame that network threads may retain as bytes only."""

    frame_id: int
    capture_timestamp_ns: int
    width: int
    height: int
    payload: bytes
    intrinsics: Tuple[float, float, float, float]
    depth_scale_bias: Tuple[float, float]
    producer_session_id: str
    calibration_id: str
    calibration_digest: str
    valid_fraction: float
    confidence_mean: float
    calibration_mode: str
    pseudo_metre_slab: Tuple[float, float]
    foreground_far_m: float
    model_id: str
    model_revision: str

    @property
    def key(self) -> tuple[str, int, str]:
        return self.producer_session_id, self.frame_id, self.calibration_digest


def _worldbus_limits(limits: SensorBridgeLimits) -> WorldBusLimits:
    return WorldBusLimits(
        max_payload_bytes=limits.max_payload_bytes,
        max_width=limits.max_width,
        max_height=limits.max_height,
        max_pixels=limits.max_pixels,
        socket_timeout_s=float(limits.socket_timeout_s),
    )


def _identity_camera(values: Sequence[object]) -> None:
    if isinstance(values, (str, bytes, bytearray)) or len(values) != 16:
        raise SensorBridgeError("camera_to_world must contain 16 numbers")
    matrix = tuple(_finite(value, "camera_to_world") for value in values)
    if any(abs(actual - expected) > 1e-6 for actual, expected in zip(matrix, _IDENTITY)):
        raise SensorBridgeError(
            "camera_to_world must be identity; TouchDesigner owns sensor_to_world"
        )


def _validate_packed_payload(payload: bytes, pixels: int) -> tuple[int, float]:
    """Validate mask/depth/confidence coupling without constructing image arrays."""

    valid = 0
    confidence_sum = 0
    for cursor in range(0, pixels * 4, 4):
        packed = (payload[cursor] << 8) | payload[cursor + 1]
        mask = payload[cursor + 2]
        confidence = payload[cursor + 3]
        if mask not in (0, 255):
            raise SensorBridgeError("foreground mask must be binary")
        if mask == 0:
            if packed != 0 or confidence != 0:
                raise SensorBridgeError("background pixels must be fully zero")
        else:
            if packed == 0 or confidence == 0:
                raise SensorBridgeError(
                    "foreground pixels require positive depth and confidence"
                )
            valid += 1
            confidence_sum += confidence
    mean = confidence_sum / float(max(1, valid) * 255)
    return valid, mean


def validate_sensor_result_frame(
    frame: WorldFrame,
    *,
    limits: SensorBridgeLimits = DEFAULT_SENSOR_LIMITS,
) -> ImmutableSensorResult:
    """Validate the complete no-RGB sensor contract before publishing bytes."""

    normalized = validate_frame(frame, _worldbus_limits(limits))
    metadata = normalized.metadata
    if metadata.pixel_format != "rgba8":
        raise SensorBridgeError("sensor result pixel_format must be rgba8")
    pixels = metadata.width * metadata.height
    payload = bytes(normalized.payload)
    if len(payload) != pixels * 4:
        raise SensorBridgeError("sensor payload size does not match its image")
    extensions = metadata.extensions
    expected = {
        "depth_anything_contract": SENSOR_CONTRACT,
        "sensor_role": "audience_interaction_only",
        "depth_anything_depth_encoding": DEPTH_ENCODING,
        "depth_anything_depth_semantics": DEPTH_SEMANTICS,
        "depth_anything_mask_semantics": MASK_SEMANTICS,
        "depth_anything_confidence_semantics": CONFIDENCE_SEMANTICS,
        "depth_anything_intrinsics_source": "assumed_horizontal_fov_not_measured",
    }
    for field, value in expected.items():
        if extensions.get(field) != value:
            raise SensorBridgeError(field + " does not match the sensor contract")
    if extensions.get("depth_anything_contains_rgb") is not False:
        raise SensorBridgeError("sensor transport must not contain camera RGB")
    frame_id = _require_int(
        extensions.get("sensor_frame_id"), "sensor_frame_id", 0, (1 << 63) - 1
    )
    capture_timestamp_ns = _require_int(
        extensions.get("sensor_capture_timestamp_ns"),
        "sensor_capture_timestamp_ns",
        1,
        (1 << 63) - 1,
    )
    if frame_id != metadata.frame_id:
        raise SensorBridgeError("sensor frame id does not match WorldBus frame id")
    if capture_timestamp_ns != metadata.timestamp_ns:
        raise SensorBridgeError(
            "sensor capture timestamp does not match WorldBus timestamp"
        )
    producer_session = _safe_text(
        extensions.get("producer_session_id"), "producer_session_id"
    )
    calibration_id = _safe_text(
        extensions.get("sensor_calibration_id"), "sensor_calibration_id", 64
    )
    if _CALIBRATION_ID.fullmatch(calibration_id) is None:
        raise SensorBridgeError("sensor_calibration_id is not a conservative identifier")
    calibration_digest = _safe_text(
        extensions.get("sensor_calibration_digest"),
        "sensor_calibration_digest",
        64,
    ).lower()
    if _SHA256.fullmatch(calibration_digest) is None:
        raise SensorBridgeError("sensor_calibration_digest must be lowercase SHA-256")
    calibration_mode = _safe_text(
        extensions.get("depth_anything_calibration_mode"),
        "depth_anything_calibration_mode",
        32,
    )
    if calibration_mode not in ("fixed", "session_frozen"):
        raise SensorBridgeError("depth calibration mode is unsupported")
    raw_order = extensions.get("depth_anything_raw_order")
    if raw_order not in ("near_is_larger", "near_is_smaller"):
        raise SensorBridgeError("raw depth order is unsupported")
    percentiles = _finite_pair(
        extensions.get("depth_anything_raw_percentiles"), "raw percentiles"
    )
    if not 0.0 <= percentiles[0] < percentiles[1] <= 100.0:
        raise SensorBridgeError("raw percentiles must be increasing in [0,100]")
    raw_bounds = _finite_pair(
        extensions.get("depth_anything_raw_bounds"), "raw bounds"
    )
    if raw_bounds[0] >= raw_bounds[1]:
        raise SensorBridgeError("raw bounds must be increasing")
    slab = _finite_pair(
        extensions.get("depth_anything_pseudo_metre_slab"), "pseudo-metre slab"
    )
    foreground_far = _finite(
        extensions.get("depth_anything_foreground_far_m"), "foreground far"
    )
    if not 0.0 < slab[0] < slab[1] <= 1000.0:
        raise SensorBridgeError("pseudo-metre slab must be positive and increasing")
    if not slab[0] <= foreground_far <= slab[1]:
        raise SensorBridgeError("foreground far must stay inside the pseudo-metre slab")
    _safe_text(
        extensions.get("depth_anything_capture_source"), "capture source", 128
    )
    model_id = _safe_text(
        extensions.get("depth_anything_model_id"), "model id", 256
    )
    model_revision = _safe_text(
        extensions.get("depth_anything_model_revision"), "model revision", 256
    )
    inference_ms = _finite(
        extensions.get("depth_anything_inference_ms"), "inference milliseconds"
    )
    if inference_ms < 0.0 or inference_ms > 600_000.0:
        raise SensorBridgeError("inference milliseconds is outside bounds")
    fx, fy, cx, cy = metadata.intrinsics
    if fx <= 0.0 or fy <= 0.0:
        raise SensorBridgeError("sensor focal lengths must be positive")
    if fx > metadata.width * 100.0 or fy > metadata.height * 100.0:
        raise SensorBridgeError("sensor focal lengths exceed consumer bounds")
    if not (
        -metadata.width <= cx <= metadata.width * 2.0
        and -metadata.height <= cy <= metadata.height * 2.0
    ):
        raise SensorBridgeError("sensor principal point is outside consumer bounds")
    scale, bias = metadata.depth_scale_bias
    far_depth = bias + scale * 65535.0
    if scale <= 0.0 or bias < 0.0 or not math.isfinite(far_depth) or far_depth > 1000.0:
        raise SensorBridgeError("depth scale/bias exceeds consumer bounds")
    _identity_camera(metadata.camera_to_world)
    valid_pixels, confidence_mean = _validate_packed_payload(payload, pixels)
    valid_fraction = valid_pixels / float(pixels)
    declared_fraction = _finite(
        extensions.get("depth_anything_valid_fraction"), "valid fraction"
    )
    declared_valid = _require_int(
        extensions.get("depth_anything_valid_pixels"),
        "valid pixels",
        0,
        pixels,
    )
    counters = [declared_valid]
    for field in (
        "depth_anything_background_pixels",
        "depth_anything_invalid_depth_pixels",
        "depth_anything_confidence_rejected_pixels",
    ):
        counters.append(_require_int(extensions.get(field), field, 0, pixels))
    if sum(counters) != pixels:
        raise SensorBridgeError("sensor validity counters do not cover the frame")
    if declared_valid != valid_pixels or not math.isclose(
        declared_fraction, valid_fraction, abs_tol=1e-9
    ):
        raise SensorBridgeError("sensor validity metadata disagrees with payload")
    for field in (
        "depth_anything_near_clipped_pixels",
        "depth_anything_far_clipped_pixels",
    ):
        _require_int(extensions.get(field), field, 0, declared_valid)
    return ImmutableSensorResult(
        frame_id=frame_id,
        capture_timestamp_ns=capture_timestamp_ns,
        width=metadata.width,
        height=metadata.height,
        payload=payload,
        intrinsics=(float(fx), float(fy), float(cx), float(cy)),
        depth_scale_bias=(float(scale), float(bias)),
        producer_session_id=producer_session,
        calibration_id=calibration_id,
        calibration_digest=calibration_digest,
        valid_fraction=valid_fraction,
        confidence_mean=confidence_mean,
        calibration_mode=calibration_mode,
        pseudo_metre_slab=slab,
        foreground_far_m=foreground_far,
        model_id=model_id,
        model_revision=model_revision,
    )


def sensor_result_to_touchdesigner_numpy(
    result: ImmutableSensorResult,
    *,
    flip_vertical: bool = True,
    mirror_horizontal: bool = False,
) -> object:
    """Convert packed bytes to TD orientation with optional audience mirroring."""

    if not isinstance(result, ImmutableSensorResult):
        raise SensorBridgeError("result must be an ImmutableSensorResult")
    if not isinstance(flip_vertical, bool):
        raise SensorBridgeError("flip_vertical must be boolean")
    if not isinstance(mirror_horizontal, bool):
        raise SensorBridgeError("mirror_horizontal must be boolean")
    try:
        import numpy as np
    except ImportError as exc:  # pragma: no cover - TouchDesigner ships NumPy
        raise SensorBridgeError("NumPy is required only for TOP upload") from exc
    expected = result.width * result.height * 4
    if len(result.payload) != expected:
        raise SensorBridgeError("sensor payload length changed after validation")
    array = np.frombuffer(result.payload, dtype=np.uint8).reshape(
        result.height, result.width, 4
    )
    converted = array.astype(np.float32) * (1.0 / 255.0)
    if flip_vertical:
        converted = np.flip(converted, axis=0)
    if mirror_horizontal:
        converted = np.flip(converted, axis=1)
    return np.ascontiguousarray(converted)


def _normalized_intrinsics_for_upload(
    result: ImmutableSensorResult,
    *,
    mirror_horizontal: bool = False,
) -> tuple[float, float, float, float]:
    """Return normalized intrinsics matching the locally oriented packed TOP."""

    if not isinstance(result, ImmutableSensorResult):
        raise SensorBridgeError("result must be an ImmutableSensorResult")
    if not isinstance(mirror_horizontal, bool):
        raise SensorBridgeError("mirror_horizontal must be boolean")
    cx = result.intrinsics[2] / result.width
    if mirror_horizontal:
        cx = 1.0 - cx
    return (
        result.intrinsics[0] / result.width,
        result.intrinsics[1] / result.height,
        cx,
        result.intrinsics[3] / result.height,
    )


def derive_frame_state(
    result: ImmutableSensorResult,
    *,
    mirror_horizontal: bool = False,
) -> dict[str, Any]:
    """Publish the worker's exact sensor calibration identity and capture key."""

    if not isinstance(result, ImmutableSensorResult):
        raise SensorBridgeError("result must be an ImmutableSensorResult")
    if not isinstance(mirror_horizontal, bool):
        raise SensorBridgeError("mirror_horizontal must be boolean")
    session_payload = (
        result.producer_session_id + "\0" + result.calibration_digest
    ).encode("utf-8")
    if mirror_horizontal:
        # Local mirroring changes sensor-space X. Give the temporal pipeline a
        # distinct session identity without altering the worker's calibration
        # digest or extending the strict FRAME_STATE schema.
        session_payload += b"\0mirror-horizontal=1"
    session_id = "depth-anything-" + hashlib.sha256(session_payload).hexdigest()[:24]
    return {
        "version": FRAME_STATE_VERSION,
        "session_id": session_id,
        "frame_id": result.frame_id,
        "timestamp_ns": result.capture_timestamp_ns,
        "width": result.width,
        "height": result.height,
        "calibration_id": result.calibration_id,
        "calibration_digest": result.calibration_digest,
        "valid_fraction": result.valid_fraction,
        "confidence_mean": result.confidence_mean,
    }


class SensorBridgeRuntime:
    """Newest-only receiver with a strict main-thread upload boundary."""

    def __init__(
        self,
        config: SensorBridgeConfig = SensorBridgeConfig(),
        *,
        limits: SensorBridgeLimits = DEFAULT_SENSOR_LIMITS,
        receiver_factory: Callable[..., Any] = WorldBusReceiver,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
        timestamp_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        if not isinstance(config, SensorBridgeConfig):
            raise SensorBridgeError("config must be SensorBridgeConfig")
        if not isinstance(limits, SensorBridgeLimits):
            raise SensorBridgeError("limits must be SensorBridgeLimits")
        if not limits.min_stale_ms <= config.stale_ms <= limits.max_stale_ms:
            raise SensorBridgeError("stale_ms exceeds the selected limits")
        self.config = config
        self.limits = limits
        self._receiver_factory = receiver_factory
        self._monotonic_ns = monotonic_ns
        self._timestamp_ns = timestamp_ns
        self._owner_thread_id = threading.get_ident()
        self._lifecycle_lock = threading.RLock()
        self._state_lock = threading.Lock()
        self._running = False
        self._receiver: Optional[Any] = None
        self._stop_event: Optional[threading.Event] = None
        self._result_thread: Optional[threading.Thread] = None
        self._latest_result: Optional[ImmutableSensorResult] = None
        self._latest_expiry_monotonic_ns: Optional[int] = None
        self._active_session_id: Optional[str] = None
        self._active_calibration: Optional[tuple[str, str]] = None
        self._highest_frame_id = -1
        self._retired_sessions: collections.deque[str] = collections.deque(
            maxlen=_MAX_RETIRED_SESSIONS
        )
        self._last_error = "none"
        self._error_counts: dict[str, int] = {}
        self._accepted_results = 0
        self._rejected_results = 0
        self._receiver_error_count = 0

    def _require_main_thread(self) -> None:
        if threading.get_ident() != self._owner_thread_id:
            raise SensorBridgeError("TouchDesigner upload must run on the owner thread")

    def _record_error(self, code: str, *, invalidate: bool = False) -> None:
        if _IDENTIFIER.fullmatch(code) is None:
            code = "internal_error"
        with self._state_lock:
            self._last_error = code
            self._error_counts[code] = self._error_counts.get(code, 0) + 1
            if invalidate:
                self._latest_result = None
                self._latest_expiry_monotonic_ns = None

    def start(self) -> "SensorBridgeRuntime":
        with self._lifecycle_lock:
            if self._running:
                return self
            stop_event = threading.Event()
            receiver = self._receiver_factory(
                host=self.config.bind_host,
                tcp_port=self.config.result_tcp,
                # Worker v1 is TCP result-only. Port 9240 remains reserved
                # metadata and must not be bound by this bridge.
                udp_port=None,
                stale_after_s=float(self.config.stale_ms) / 1000.0,
                limits=_worldbus_limits(self.limits),
            )
            try:
                receiver.start()
            except Exception:
                try:
                    receiver.close()
                except Exception:
                    pass
                self._record_error("receiver_start_failed", invalidate=True)
                raise
            self._receiver = receiver
            self._stop_event = stop_event
            with self._state_lock:
                self._running = True
                self._latest_result = None
                self._latest_expiry_monotonic_ns = None
                self._active_session_id = None
                self._active_calibration = None
                self._highest_frame_id = -1
                self._retired_sessions.clear()
                self._receiver_error_count = 0
            thread = threading.Thread(
                target=self._result_loop,
                args=(stop_event, receiver),
                name="depth-anything-td-results",
                daemon=True,
            )
            self._result_thread = thread
            try:
                thread.start()
            except Exception:
                with self._state_lock:
                    self._running = False
                stop_event.set()
                receiver.close()
                self._receiver = None
                self._stop_event = None
                self._result_thread = None
                self._record_error("bridge_thread_start_failed", invalidate=True)
                raise
        return self

    def stop(self) -> None:
        with self._lifecycle_lock:
            if not self._running and self._receiver is None:
                return
            with self._state_lock:
                self._running = False
                self._latest_result = None
                self._latest_expiry_monotonic_ns = None
            stop_event = self._stop_event
            receiver = self._receiver
            thread = self._result_thread
            if stop_event is not None:
                stop_event.set()
            if receiver is not None:
                try:
                    receiver.close()
                except Exception:
                    self._record_error("receiver_stop_failed", invalidate=True)
            if thread is not None and thread.ident is not None:
                thread.join(timeout=float(self.limits.thread_join_timeout_s))
            self._receiver = None
            self._stop_event = None
            self._result_thread = None

    def _reject(self, code: str) -> None:
        with self._state_lock:
            self._rejected_results += 1
        self._record_error(code, invalidate=True)

    def _receiver_error_delta(self, receiver: Any) -> None:
        try:
            count = len(receiver.errors)
        except Exception:
            return
        with self._state_lock:
            previous = self._receiver_error_count
            self._receiver_error_count = count
        if count > previous:
            self._record_error("receiver_error", invalidate=True)

    def _result_loop(self, stop_event: threading.Event, receiver: Any) -> None:
        stale_ns = int(float(self.config.stale_ms) * 1_000_000.0)
        future_ns = int(float(self.limits.max_future_capture_ms) * 1_000_000.0)
        while not stop_event.is_set():
            try:
                frame = receiver.frames.get(timeout=0.1)
            except TimeoutError:
                self._receiver_error_delta(receiver)
                continue
            except QueueClosed:
                if not stop_event.is_set():
                    self._record_error("receiver_disconnected", invalidate=True)
                break
            try:
                result = validate_sensor_result_frame(frame, limits=self.limits)
            except (SensorBridgeError, WorldBusError, ValueError):
                self._reject("result_rejected")
                continue
            now_wall = self._timestamp_ns()
            now_monotonic = self._monotonic_ns()
            capture_age_ns = now_wall - result.capture_timestamp_ns
            if capture_age_ns < -future_ns:
                self._reject("future_capture_timestamp")
                continue
            initial_age_ns = max(0, capture_age_ns)
            if initial_age_ns > stale_ns:
                self._reject("stale_capture_timestamp")
                continue
            calibration = (result.calibration_id, result.calibration_digest)
            with self._state_lock:
                active_session = self._active_session_id
                active_calibration = self._active_calibration
                highest = self._highest_frame_id
                retired = tuple(self._retired_sessions)
            if result.producer_session_id in retired:
                self._reject("retired_session")
                continue
            if active_session == result.producer_session_id:
                if calibration != active_calibration:
                    self._reject("session_calibration_changed")
                    continue
                if result.frame_id <= highest:
                    self._reject("out_of_order_frame")
                    continue
            expiry = now_monotonic + max(0, stale_ns - initial_age_ns)
            with self._state_lock:
                if active_session is not None and active_session != result.producer_session_id:
                    self._retired_sessions.append(active_session)
                self._active_session_id = result.producer_session_id
                self._active_calibration = calibration
                self._highest_frame_id = result.frame_id
                self._latest_result = result
                self._latest_expiry_monotonic_ns = expiry
                self._accepted_results += 1
                self._last_error = "none"
            self._receiver_error_delta(receiver)

    def latest_result(self) -> Optional[ImmutableSensorResult]:
        with self._state_lock:
            result = self._latest_result
            expiry = self._latest_expiry_monotonic_ns
        if result is None or expiry is None:
            return None
        if self._monotonic_ns() > expiry:
            return None
        return result

    def upload_result_to_top(
        self, script_top: object, result: ImmutableSensorResult
    ) -> bool:
        self._require_main_thread()
        if not isinstance(result, ImmutableSensorResult):
            raise SensorBridgeError("result must be an ImmutableSensorResult")
        copier = getattr(script_top, "copyNumpyArray", None)
        if not callable(copier):
            raise SensorBridgeError("RESULT_PACKED does not expose copyNumpyArray()")
        copier(
            sensor_result_to_touchdesigner_numpy(
                result,
                flip_vertical=self.config.flip_vertical,
                mirror_horizontal=self.config.mirror_horizontal,
            )
        )
        return True

    def status(self) -> dict[str, Any]:
        with self._state_lock:
            running = self._running
            stored = self._latest_result
            expiry = self._latest_expiry_monotonic_ns
            accepted = self._accepted_results
            rejected = self._rejected_results
            last_error = self._last_error
            error_count = sum(self._error_counts.values())
            receiver_errors = self._receiver_error_count
        now = self._monotonic_ns()
        fresh = stored is not None and expiry is not None and now <= expiry
        remaining_ns = None if expiry is None else max(0, expiry - now)
        state = "stopped"
        if running:
            state = "running" if stored is None or fresh else "stale"
        return {
            "state": state,
            "bind_host": self.config.bind_host,
            "result_tcp": self.config.result_tcp,
            "reserved_result_udp": self.config.result_udp,
            "trusted_network_opt_in": self.config.allow_trusted_network,
            "stale_ms": float(self.config.stale_ms),
            "mirror_horizontal": self.config.mirror_horizontal,
            "accepted_results": accepted,
            "rejected_results": rejected,
            "receiver_errors": receiver_errors,
            "latest_frame_id": stored.frame_id if fresh and stored is not None else -1,
            "result_fresh": fresh,
            "freshness_remaining_ms": (
                -1.0 if remaining_ns is None else remaining_ns / 1_000_000.0
            ),
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


def _toggle_parameter_value(owner: object, name: str, default: bool) -> bool:
    value = _parameter_value(owner, name, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    raise SensorBridgeError(name + " must be an explicit boolean toggle")


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
            append((key, str(mapping[key])))
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


def _update_constant(node: object, values: Sequence[float]) -> bool:
    if node is None or len(values) != 4:
        return False
    results = []
    for names, value in zip(
        (("colorr", "color1r"), ("colorg", "color1g"),
         ("colorb", "color1b"), ("colora", "color1a", "alpha")),
        values,
    ):
        written = False
        for name in names:
            if _set_parameter(node, name, float(value)):
                written = True
                break
        results.append(written)
    return all(results)


def _component_config(bridge_comp: object) -> SensorBridgeConfig:
    return SensorBridgeConfig(
        bind_host=str(_parameter_value(bridge_comp, "Resultbindhost", "127.0.0.1")),
        result_tcp=int(_parameter_value(bridge_comp, "Resulttcp", 9241)),
        result_udp=int(_parameter_value(bridge_comp, "Resultudp", 9240)),
        stale_ms=float(_parameter_value(bridge_comp, "Stalems", 800.0)),
        flip_vertical=_toggle_parameter_value(bridge_comp, "Flipvertical", True),
        mirror_horizontal=_toggle_parameter_value(
            bridge_comp, "Mirrorhorizontal", True
        ),
        allow_trusted_network=_toggle_parameter_value(
            bridge_comp, "Allowtrustednetwork", False
        ),
    )


_GLOBAL_LOCK = threading.RLock()
_GLOBAL_RUNTIME: Optional[SensorBridgeRuntime] = None
_GLOBAL_SIGNATURE: Optional[tuple[int, SensorBridgeConfig]] = None
_GLOBAL_PUBLISHED_KEY: Optional[tuple[str, int, str]] = None
_GLOBAL_UPLOADED_KEY: Optional[tuple[str, int, str]] = None
_GLOBAL_STAGED_RESULT: Optional[ImmutableSensorResult] = None
_GLOBAL_BRIDGE_COMP: Optional[object] = None
_GLOBAL_DISABLED_STATUS = {
    "state": "disabled",
    "detail": "enable DEPTH_SENSOR_ADAPTER, then start the webcam worker",
    "last_error": "none",
}


def tick(bridge_comp: object) -> dict[str, Any]:
    """Force-cook and publish only a fully correlated packed-frame snapshot."""

    global _GLOBAL_RUNTIME, _GLOBAL_SIGNATURE, _GLOBAL_PUBLISHED_KEY
    global _GLOBAL_STAGED_RESULT, _GLOBAL_UPLOADED_KEY, _GLOBAL_BRIDGE_COMP
    _GLOBAL_BRIDGE_COMP = bridge_comp
    enabled = bool(_parameter_value(bridge_comp, "Enabled", False))
    if not enabled:
        stop(bridge_comp)
        return dict(_GLOBAL_DISABLED_STATUS)
    try:
        config = _component_config(bridge_comp)
    except (SensorBridgeError, TypeError, ValueError):
        failed = {
            "state": "error",
            "detail": "invalid sensor bridge configuration",
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
                _GLOBAL_RUNTIME = SensorBridgeRuntime(config).start()
            except Exception:
                _GLOBAL_RUNTIME = None
                _GLOBAL_SIGNATURE = None
                failed = {
                    "state": "error",
                    "detail": "sensor receiver could not start",
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
    result = runtime.latest_result()
    if result is None:
        _set_parameter(bridge_comp, "Resultvalid", False)
        with _GLOBAL_LOCK:
            if runtime is _GLOBAL_RUNTIME:
                _GLOBAL_STAGED_RESULT = None
                _GLOBAL_UPLOADED_KEY = None
                _GLOBAL_PUBLISHED_KEY = None
    else:
        with _GLOBAL_LOCK:
            ready = (
                runtime is _GLOBAL_RUNTIME
                and _GLOBAL_STAGED_RESULT is not None
                and _GLOBAL_STAGED_RESULT.key == result.key
                and _GLOBAL_UPLOADED_KEY == result.key
                and _GLOBAL_PUBLISHED_KEY == result.key
            )
        if not ready:
            _set_parameter(bridge_comp, "Resultvalid", False)
            with _GLOBAL_LOCK:
                if runtime is _GLOBAL_RUNTIME:
                    _GLOBAL_STAGED_RESULT = result
                    _GLOBAL_UPLOADED_KEY = None
                    _GLOBAL_PUBLISHED_KEY = None
            packed_top = _child(bridge_comp, "RESULT_PACKED")
            cook = getattr(packed_top, "cook", None) if packed_top is not None else None
            attempted = False
            if callable(cook):
                try:
                    cook(force=True)
                    attempted = True
                except Exception:
                    runtime._record_error("packed_cook_failed", invalidate=True)
            else:
                runtime._record_error("packed_cook_unavailable", invalidate=True)
            with _GLOBAL_LOCK:
                uploaded = (
                    attempted
                    and runtime is _GLOBAL_RUNTIME
                    and _GLOBAL_STAGED_RESULT is not None
                    and _GLOBAL_STAGED_RESULT.key == result.key
                    and _GLOBAL_UPLOADED_KEY == result.key
                )
            if not uploaded:
                if attempted:
                    runtime._record_error("packed_upload_unconfirmed", invalidate=True)
                with _GLOBAL_LOCK:
                    if runtime is _GLOBAL_RUNTIME:
                        _GLOBAL_STAGED_RESULT = None
                        _GLOBAL_UPLOADED_KEY = None
                        _GLOBAL_PUBLISHED_KEY = None
            else:
                frame_written = _write_text_mapping(
                    _child(bridge_comp, "FRAME_STATE"),
                    derive_frame_state(
                        result,
                        mirror_horizontal=config.mirror_horizontal,
                    ),
                ) if _child(bridge_comp, "FRAME_STATE") is not None else False
                calibration_written = _update_constant(
                    _child(bridge_comp, "DEPTH_CALIBRATION"),
                    (
                        result.depth_scale_bias[0],
                        result.depth_scale_bias[1],
                        result.pseudo_metre_slab[0],
                        result.pseudo_metre_slab[1],
                    ),
                )
                intrinsics_written = _update_constant(
                    _child(bridge_comp, "INTRINSICS_NORMALIZED"),
                    _normalized_intrinsics_for_upload(
                        result,
                        mirror_horizontal=config.mirror_horizontal,
                    ),
                )
                if frame_written and calibration_written and intrinsics_written:
                    with _GLOBAL_LOCK:
                        if (
                            runtime is _GLOBAL_RUNTIME
                            and _GLOBAL_STAGED_RESULT is not None
                            and _GLOBAL_STAGED_RESULT.key == result.key
                            and _GLOBAL_UPLOADED_KEY == result.key
                        ):
                            _GLOBAL_PUBLISHED_KEY = result.key
                else:
                    runtime._record_error("metadata_publish_failed", invalidate=True)
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
            and runtime.latest_result() is not None
        )
    _set_parameter(bridge_comp, "Resultvalid", published)
    current = runtime.status()
    current["detail"] = (
        "sensor-local XYZ is ready"
        if published
        else "waiting for a fresh, confirmed packed-frame upload"
    )
    status_dat = _child(bridge_comp, "STATUS")
    if status_dat is not None:
        _write_status(status_dat, current)
    return current


def on_script_top_cook(script_op: object) -> bool:
    """Copy the exact staged bytes and record the matching uploaded key."""

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
        runtime._record_error("upload_superseded", invalidate=True)
        return False
    runtime._record_error("upload_failed", invalidate=True)
    with _GLOBAL_LOCK:
        if runtime is _GLOBAL_RUNTIME:
            _GLOBAL_STAGED_RESULT = None
            _GLOBAL_UPLOADED_KEY = None
            _GLOBAL_PUBLISHED_KEY = None
    if bridge_comp is not None:
        _set_parameter(bridge_comp, "Resultvalid", False)
    return False


def stop(bridge_comp: Optional[object] = None) -> None:
    """Stop sockets/threads and invalidate the adapter; repeated calls are safe."""

    global _GLOBAL_RUNTIME, _GLOBAL_SIGNATURE, _GLOBAL_PUBLISHED_KEY
    global _GLOBAL_STAGED_RESULT, _GLOBAL_UPLOADED_KEY, _GLOBAL_BRIDGE_COMP
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
    """Return bounded counters only; never image bytes or camera imagery."""

    with _GLOBAL_LOCK:
        runtime = _GLOBAL_RUNTIME
    return dict(_GLOBAL_DISABLED_STATUS) if runtime is None else runtime.status()


__all__ = [
    "CONFIDENCE_SEMANTICS",
    "DEFAULT_SENSOR_LIMITS",
    "DEPTH_ENCODING",
    "DEPTH_SEMANTICS",
    "FRAME_STATE_VERSION",
    "ImmutableSensorResult",
    "MASK_SEMANTICS",
    "SENSOR_CONTRACT",
    "SENSOR_COORDINATES",
    "SensorBridgeConfig",
    "SensorBridgeError",
    "SensorBridgeLimits",
    "SensorBridgeRuntime",
    "derive_frame_state",
    "on_script_top_cook",
    "sensor_result_to_touchdesigner_numpy",
    "status",
    "stop",
    "tick",
    "validate_sensor_result_frame",
]
