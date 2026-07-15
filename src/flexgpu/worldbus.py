"""Dependency-free WorldBus v1 reference transport.

This module is intentionally conservative.  It demonstrates a bounded wire
format and receiver policy for moving an AI-generated RGBA atlas plus metadata
to the authoritative world process.  It is suitable for local prototypes and
trusted show networks; it is not an authenticated Internet protocol.
"""

from __future__ import annotations

import collections
import json
import math
import os
import re
import socket
import struct
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator, Mapping, Optional, Tuple


PROTOCOL_VERSION = 1
FRAME_MAGIC = b"WB01"
REPLAY_MAGIC = b"WBR1\n"
FRAME_PREFIX = struct.Struct("!4sII")
MAX_INT64 = (1 << 63) - 1
PIXEL_FORMAT_BYTES = {"rgba8_atlas": 4, "rgba8": 4}
PRODUCER_SESSION_FIELD = "producer_session_id"
_MAX_RETIRED_PRODUCER_SESSIONS = 64
_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


class WorldBusError(Exception):
    """Base class for WorldBus failures."""


class ValidationError(WorldBusError):
    """A message or frame failed protocol validation."""


class SizeLimitError(ValidationError):
    """A bounded protocol item exceeded its configured limit."""


class FramingError(WorldBusError):
    """A TCP or replay byte stream has invalid framing."""


class QueueClosed(WorldBusError):
    """The newest-frame queue was closed while a consumer was waiting."""


@dataclass(frozen=True)
class WorldBusLimits:
    """Safety limits applied before allocating or waiting on remote input."""

    max_metadata_bytes: int = 64 * 1024
    max_payload_bytes: int = 64 * 1024 * 1024
    max_udp_bytes: int = 16 * 1024
    max_width: int = 8192
    max_height: int = 8192
    max_pixels: int = 16 * 1024 * 1024
    max_generation_id_bytes: int = 256
    max_sender_bytes: int = 64
    max_heartbeat_peers: int = 64
    max_json_depth: int = 8
    max_json_items: int = 512
    socket_timeout_s: float = 5.0
    max_queue_wait_s: float = 3600.0
    max_replay_frames: int = 10000
    max_replay_bytes: int = 512 * 1024 * 1024
    min_replay_interval_ns: int = 1_000_000
    max_replay_interval_ns: int = 5_000_000_000


DEFAULT_LIMITS = WorldBusLimits()


@dataclass(frozen=True)
class FrameMetadata:
    """Validated WorldBus v1 metadata for one raw image frame."""

    frame_id: int
    timestamp_ns: int
    width: int
    height: int
    pixel_format: str
    payload_bytes: int
    intrinsics: Tuple[float, float, float, float]
    depth_scale_bias: Tuple[float, float]
    camera_to_world: Tuple[float, ...]
    generation_id: str
    extensions: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "worldbus_version": PROTOCOL_VERSION,
            "frame_id": self.frame_id,
            # A decimal string survives JavaScript and OSC implementations that
            # cannot losslessly represent a nanosecond int64.
            "timestamp_ns": str(self.timestamp_ns),
            "width": self.width,
            "height": self.height,
            "pixel_format": self.pixel_format,
            "payload_bytes": self.payload_bytes,
            "intrinsics": list(self.intrinsics),
            "depth_scale_bias": list(self.depth_scale_bias),
            "camera_to_world": list(self.camera_to_world),
            "generation_id": self.generation_id,
        }
        for key, value in self.extensions.items():
            if key not in result:
                result[key] = value
        return result


@dataclass(frozen=True)
class WorldFrame:
    """One validated metadata record and its immutable raw payload."""

    metadata: FrameMetadata
    payload: bytes


_REQUIRED_METADATA = {
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


def _strict_json_loads(data: str) -> Any:
    def reject_constant(value: str) -> None:
        raise ValueError("non-finite JSON number: " + value)

    try:
        return json.loads(data, parse_constant=reject_constant)
    except (ValueError, RecursionError) as exc:
        raise ValidationError("invalid JSON: " + str(exc)) from exc


def _strict_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValidationError("value is not bounded JSON: " + str(exc)) from exc


def _validate_json_value(
    value: Any,
    limits: WorldBusLimits,
    *,
    depth: int = 0,
    counter: Optional[list[int]] = None,
) -> None:
    if counter is None:
        counter = [0]
    counter[0] += 1
    if counter[0] > limits.max_json_items:
        raise SizeLimitError("JSON value contains too many items")
    if depth > limits.max_json_depth:
        raise SizeLimitError("JSON value is nested too deeply")
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValidationError("JSON numbers must be finite")
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _validate_json_value(item, limits, depth=depth + 1, counter=counter)
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValidationError("JSON object keys must be strings")
            _validate_json_value(item, limits, depth=depth + 1, counter=counter)
        return
    raise ValidationError("unsupported JSON value type: " + type(value).__name__)


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError(label + " must be a JSON object")
    return value


def _require_int(value: Any, label: str, minimum: int = 0, maximum: int = MAX_INT64) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(label + " must be an integer")
    if value < minimum or value > maximum:
        raise ValidationError(f"{label} must be between {minimum} and {maximum}")
    return value


def _require_decimal_int(
    value: Any, label: str, minimum: int = 0, maximum: int = MAX_INT64
) -> int:
    if isinstance(value, str):
        if not value or len(value) > 20 or not value.isdecimal():
            raise ValidationError(label + " must be an unsigned decimal string or integer")
        value = int(value)
    return _require_int(value, label, minimum, maximum)


def _require_finite_sequence(value: Any, label: str, length: int) -> Tuple[float, ...]:
    if not isinstance(value, (list, tuple)) or len(value) != length:
        raise ValidationError(f"{label} must contain exactly {length} numbers")
    result = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ValidationError(label + " must contain only numbers")
        number = float(item)
        if not math.isfinite(number):
            raise ValidationError(label + " must contain only finite numbers")
        result.append(number)
    return tuple(result)


def validate_metadata(
    value: Mapping[str, Any], limits: WorldBusLimits = DEFAULT_LIMITS
) -> FrameMetadata:
    """Validate and normalize a WorldBus v1 frame metadata object.

    Unknown fields are retained as additive extensions.  Required-field or wire
    representation changes require a future protocol version.
    """

    data = _require_mapping(value, "metadata")
    missing = sorted(_REQUIRED_METADATA.difference(data))
    if missing:
        raise ValidationError("metadata is missing required fields: " + ", ".join(missing))
    version = _require_int(data["worldbus_version"], "worldbus_version", 1, 255)
    if version != PROTOCOL_VERSION:
        raise ValidationError(
            f"unsupported worldbus_version {version}; expected {PROTOCOL_VERSION}"
        )
    frame_id = _require_int(data["frame_id"], "frame_id")
    timestamp_ns = _require_decimal_int(data["timestamp_ns"], "timestamp_ns")
    width = _require_int(data["width"], "width", 1, limits.max_width)
    height = _require_int(data["height"], "height", 1, limits.max_height)
    pixels = width * height
    if pixels > limits.max_pixels:
        raise SizeLimitError(
            f"frame has {pixels} pixels; limit is {limits.max_pixels}"
        )
    pixel_format = data["pixel_format"]
    if not isinstance(pixel_format, str) or pixel_format not in PIXEL_FORMAT_BYTES:
        raise ValidationError(
            "pixel_format must be one of: " + ", ".join(sorted(PIXEL_FORMAT_BYTES))
        )
    if pixel_format == "rgba8_atlas" and width % 2:
        raise ValidationError("rgba8_atlas width must be even so its two planes match")
    payload_bytes = _require_int(
        data["payload_bytes"], "payload_bytes", 1, limits.max_payload_bytes
    )
    expected_bytes = pixels * PIXEL_FORMAT_BYTES[pixel_format]
    if payload_bytes != expected_bytes:
        raise ValidationError(
            f"payload_bytes is {payload_bytes}; {pixel_format} {width}x{height} requires "
            f"{expected_bytes}"
        )
    intrinsics = _require_finite_sequence(data["intrinsics"], "intrinsics", 4)
    if intrinsics[0] <= 0 or intrinsics[1] <= 0:
        raise ValidationError("intrinsics fx and fy must be greater than zero")
    depth_scale_bias = _require_finite_sequence(
        data["depth_scale_bias"], "depth_scale_bias", 2
    )
    if depth_scale_bias[0] <= 0:
        raise ValidationError("depth_scale_bias scale must be greater than zero")
    camera_to_world = _require_finite_sequence(
        data["camera_to_world"], "camera_to_world", 16
    )
    generation_id = data["generation_id"]
    if not isinstance(generation_id, str) or not generation_id:
        raise ValidationError("generation_id must be a non-empty string")
    if len(generation_id.encode("utf-8")) > limits.max_generation_id_bytes:
        raise SizeLimitError("generation_id is too long")
    if any(ord(character) < 32 for character in generation_id):
        raise ValidationError("generation_id cannot contain control characters")

    extensions = {key: item for key, item in data.items() if key not in _REQUIRED_METADATA}
    if PRODUCER_SESSION_FIELD in extensions:
        session_id = extensions[PRODUCER_SESSION_FIELD]
        if not isinstance(session_id, str) or not session_id:
            raise ValidationError(PRODUCER_SESSION_FIELD + " must be a non-empty string")
        if len(session_id.encode("utf-8")) > limits.max_generation_id_bytes:
            raise SizeLimitError(PRODUCER_SESSION_FIELD + " is too long")
        if any(ord(character) < 32 for character in session_id):
            raise ValidationError(PRODUCER_SESSION_FIELD + " cannot contain control characters")
    _validate_json_value(extensions, limits)
    normalized = FrameMetadata(
        frame_id=frame_id,
        timestamp_ns=timestamp_ns,
        width=width,
        height=height,
        pixel_format=pixel_format,
        payload_bytes=payload_bytes,
        intrinsics=(intrinsics[0], intrinsics[1], intrinsics[2], intrinsics[3]),
        depth_scale_bias=(depth_scale_bias[0], depth_scale_bias[1]),
        camera_to_world=camera_to_world,
        generation_id=generation_id,
        extensions=extensions,
    )
    encoded = _strict_json_bytes(normalized.to_dict())
    if len(encoded) > limits.max_metadata_bytes:
        raise SizeLimitError(
            f"metadata uses {len(encoded)} bytes; limit is {limits.max_metadata_bytes}"
        )
    return normalized


def validate_frame(frame: WorldFrame, limits: WorldBusLimits = DEFAULT_LIMITS) -> WorldFrame:
    """Revalidate a frame and freeze any bytes-like payload."""

    if not isinstance(frame, WorldFrame):
        raise ValidationError("frame must be a WorldFrame")
    if not isinstance(frame.metadata, FrameMetadata):
        raise ValidationError("frame metadata must be FrameMetadata")
    metadata = validate_metadata(frame.metadata.to_dict(), limits)
    if not isinstance(frame.payload, (bytes, bytearray, memoryview)):
        raise ValidationError("frame payload must be bytes-like")
    payload = bytes(frame.payload)
    if len(payload) != metadata.payload_bytes:
        raise ValidationError(
            f"payload contains {len(payload)} bytes; metadata declares {metadata.payload_bytes}"
        )
    return WorldFrame(metadata=metadata, payload=payload)


def make_frame(
    metadata: Mapping[str, Any], payload: bytes, limits: WorldBusLimits = DEFAULT_LIMITS
) -> WorldFrame:
    """Construct a validated immutable frame from a mapping and payload."""

    normalized = validate_metadata(metadata, limits)
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise ValidationError("frame payload must be bytes-like")
    return validate_frame(WorldFrame(normalized, bytes(payload)), limits)


def encode_frame(frame: WorldFrame, limits: WorldBusLimits = DEFAULT_LIMITS) -> bytes:
    """Encode a frame as ``magic + JSON length + payload length + bytes``."""

    normalized = validate_frame(frame, limits)
    metadata_bytes = _strict_json_bytes(normalized.metadata.to_dict())
    return (
        FRAME_PREFIX.pack(FRAME_MAGIC, len(metadata_bytes), len(normalized.payload))
        + metadata_bytes
        + normalized.payload
    )


def _parse_framed_parts(
    metadata_bytes: bytes,
    payload: bytes,
    declared_payload_bytes: int,
    limits: WorldBusLimits,
) -> WorldFrame:
    try:
        metadata_text = metadata_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FramingError("frame metadata is not UTF-8") from exc
    value = _strict_json_loads(metadata_text)
    try:
        metadata = validate_metadata(_require_mapping(value, "metadata"), limits)
    except ValidationError as exc:
        raise FramingError("invalid frame metadata: " + str(exc)) from exc
    if declared_payload_bytes != metadata.payload_bytes:
        raise FramingError(
            "TCP payload length does not match metadata payload_bytes"
        )
    if len(payload) != declared_payload_bytes:
        raise FramingError("truncated frame payload")
    return WorldFrame(metadata=metadata, payload=payload)


class FrameStreamDecoder:
    """Incrementally decode a TCP byte stream while retaining at most one frame."""

    def __init__(self, limits: WorldBusLimits = DEFAULT_LIMITS) -> None:
        self.limits = limits
        self._buffer = bytearray()

    @property
    def pending_bytes(self) -> int:
        return len(self._buffer)

    def feed(self, data: bytes) -> list[WorldFrame]:
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("decoder input must be bytes-like")
        self._buffer.extend(data)
        frames: list[WorldFrame] = []
        while True:
            if len(self._buffer) < FRAME_PREFIX.size:
                break
            magic, metadata_size, payload_size = FRAME_PREFIX.unpack_from(self._buffer)
            if magic != FRAME_MAGIC:
                raise FramingError("invalid frame magic")
            if metadata_size < 2 or metadata_size > self.limits.max_metadata_bytes:
                raise SizeLimitError(
                    f"metadata length {metadata_size} exceeds protocol limits"
                )
            if payload_size < 1 or payload_size > self.limits.max_payload_bytes:
                raise SizeLimitError(
                    f"payload length {payload_size} exceeds protocol limits"
                )
            total = FRAME_PREFIX.size + metadata_size + payload_size
            if len(self._buffer) < total:
                break
            metadata_bytes = bytes(self._buffer[FRAME_PREFIX.size : FRAME_PREFIX.size + metadata_size])
            payload = bytes(self._buffer[FRAME_PREFIX.size + metadata_size : total])
            del self._buffer[:total]
            frames.append(
                _parse_framed_parts(metadata_bytes, payload, payload_size, self.limits)
            )
        maximum_pending = (
            FRAME_PREFIX.size + self.limits.max_metadata_bytes + self.limits.max_payload_bytes
        )
        if len(self._buffer) > maximum_pending:
            raise SizeLimitError("incomplete frame exceeds maximum wire size")
        return frames

    def finish(self) -> None:
        """Reject a stream that ended between frame boundaries."""

        if self._buffer:
            raise FramingError(f"stream ended with {len(self._buffer)} incomplete bytes")


def decode_frame(data: bytes, limits: WorldBusLimits = DEFAULT_LIMITS) -> WorldFrame:
    """Decode exactly one complete wire frame."""

    decoder = FrameStreamDecoder(limits)
    frames = decoder.feed(data)
    decoder.finish()
    if len(frames) != 1:
        raise FramingError(f"expected exactly one frame, received {len(frames)}")
    return frames[0]


class NewestFrameQueue:
    """A thread-safe one-slot queue that never builds latency.

    A newer pending frame replaces the previous pending frame.  Duplicate or
    decreasing frame IDs are rejected even after the consumer empties the slot.
    Producers that expose ``producer_session_id`` may restart their frame
    counter under a new unique session without waiting to surpass the previous
    process's high-water mark.
    """

    def __init__(self, limits: WorldBusLimits = DEFAULT_LIMITS) -> None:
        self.limits = limits
        self._condition = threading.Condition()
        self._pending: Optional[WorldFrame] = None
        self._highest_frame_id = -1
        self._producer_session: Optional[str] = None
        self._retired_sessions: collections.deque[str] = collections.deque(
            maxlen=_MAX_RETIRED_PRODUCER_SESSIONS
        )
        self._retired_session_set: set[str] = set()
        self._closed = False
        self._accepted = 0
        self._superseded = 0
        self._rejected_stale = 0
        self._session_resets = 0
        self._rejected_retired_session = 0
        self._rejected_missing_session = 0

    def _retire_session(self, session_id: str) -> None:
        if session_id in self._retired_session_set:
            return
        if len(self._retired_sessions) == self._retired_sessions.maxlen:
            oldest = self._retired_sessions.popleft()
            self._retired_session_set.remove(oldest)
        self._retired_sessions.append(session_id)
        self._retired_session_set.add(session_id)

    def put(self, frame: WorldFrame) -> bool:
        normalized = validate_frame(frame, self.limits)
        with self._condition:
            if self._closed:
                raise QueueClosed("newest-frame queue is closed")
            raw_session = normalized.metadata.extensions.get(PRODUCER_SESSION_FIELD)
            session_id = str(raw_session) if raw_session is not None else None
            if self._producer_session is not None and session_id is None:
                self._rejected_stale += 1
                self._rejected_missing_session += 1
                return False
            if session_id is not None:
                if session_id in self._retired_session_set:
                    self._rejected_stale += 1
                    self._rejected_retired_session += 1
                    return False
                if self._producer_session is None:
                    if self._highest_frame_id >= 0:
                        self._highest_frame_id = -1
                        self._session_resets += 1
                    self._producer_session = session_id
                elif session_id != self._producer_session:
                    self._retire_session(self._producer_session)
                    self._producer_session = session_id
                    self._highest_frame_id = -1
                    self._session_resets += 1
            frame_id = normalized.metadata.frame_id
            if frame_id <= self._highest_frame_id:
                self._rejected_stale += 1
                return False
            if self._pending is not None:
                self._superseded += 1
            self._pending = normalized
            self._highest_frame_id = frame_id
            self._accepted += 1
            self._condition.notify()
            return True

    def get(self, timeout: Optional[float] = None) -> WorldFrame:
        if timeout is not None:
            if not math.isfinite(timeout) or timeout < 0 or timeout > self.limits.max_queue_wait_s:
                raise ValueError(
                    f"timeout must be between 0 and {self.limits.max_queue_wait_s} seconds"
                )
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while self._pending is None:
                if self._closed:
                    raise QueueClosed("newest-frame queue is closed")
                if deadline is None:
                    self._condition.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("no WorldBus frame arrived before the timeout")
                self._condition.wait(remaining)
            result = self._pending
            self._pending = None
            return result

    def get_nowait(self) -> WorldFrame:
        return self.get(0.0)

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    @property
    def stats(self) -> dict[str, int]:
        with self._condition:
            return {
                "accepted": self._accepted,
                "superseded": self._superseded,
                "rejected_stale": self._rejected_stale,
                "session_resets": self._session_resets,
                "rejected_retired_session": self._rejected_retired_session,
                "rejected_missing_session": self._rejected_missing_session,
                "highest_frame_id": self._highest_frame_id,
                "pending": int(self._pending is not None),
            }


class HeartbeatMonitor:
    """Track peer freshness using local receive time, never remote clock time.

    Stale peers remain observable for a longer expiry window.  Expired peers
    are removed, and a full table evicts its oldest entry rather than allowing
    arbitrary sender names to permanently lock out the real show peer.
    """

    def __init__(
        self,
        stale_after_s: float = 1.0,
        *,
        expire_after_s: Optional[float] = None,
        clock: Callable[[], float] = time.monotonic,
        limits: WorldBusLimits = DEFAULT_LIMITS,
    ) -> None:
        if not math.isfinite(stale_after_s) or stale_after_s <= 0 or stale_after_s > 3600:
            raise ValueError("stale_after_s must be greater than 0 and at most 3600")
        if (
            isinstance(limits.max_heartbeat_peers, bool)
            or not isinstance(limits.max_heartbeat_peers, int)
            or limits.max_heartbeat_peers < 1
        ):
            raise ValueError("limits.max_heartbeat_peers must be a positive integer")
        self.stale_after_s = stale_after_s
        selected_expiry = (
            max(60.0, stale_after_s * 10.0)
            if expire_after_s is None
            else expire_after_s
        )
        if (
            not isinstance(selected_expiry, (int, float))
            or not math.isfinite(float(selected_expiry))
            or float(selected_expiry) <= stale_after_s
            or float(selected_expiry) > 86400
        ):
            raise ValueError("expire_after_s must exceed stale_after_s and be at most 86400")
        self.expire_after_s = float(selected_expiry)
        self.clock = clock
        self.limits = limits
        self._received: dict[str, float] = {}
        self._lock = threading.Lock()
        self._expired = 0
        self._evicted = 0

    @staticmethod
    def _instant(value: Any, label: str) -> float:
        if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise ValueError(label + " must be a finite monotonic time")
        return float(value)

    def _prune_expired_locked(self, instant: float) -> None:
        expired = [
            sender
            for sender, received in self._received.items()
            if instant - received > self.expire_after_s
        ]
        for sender in expired:
            del self._received[sender]
        self._expired += len(expired)

    def record(self, sender: str, *, received_at: Optional[float] = None) -> None:
        _validate_sender(sender, self.limits)
        instant = self._instant(
            self.clock() if received_at is None else received_at, "received_at"
        )
        with self._lock:
            self._prune_expired_locked(instant)
            if sender not in self._received and len(self._received) >= self.limits.max_heartbeat_peers:
                oldest = min(self._received, key=lambda item: (self._received[item], item))
                del self._received[oldest]
                self._evicted += 1
            self._received[sender] = instant

    def status(self, sender: str, *, now: Optional[float] = None) -> dict[str, Any]:
        _validate_sender(sender, self.limits)
        instant = self._instant(self.clock() if now is None else now, "now")
        with self._lock:
            self._prune_expired_locked(instant)
            received = self._received.get(sender)
        if received is None:
            return {"sender": sender, "state": "missing", "age_s": None}
        age = max(0.0, float(instant) - received)
        return {
            "sender": sender,
            "state": "stale" if age > self.stale_after_s else "alive",
            "age_s": age,
        }

    def is_stale(self, sender: str, *, now: Optional[float] = None) -> bool:
        return self.status(sender, now=now)["state"] != "alive"

    def snapshot(self, *, now: Optional[float] = None) -> dict[str, dict[str, Any]]:
        instant = self._instant(self.clock() if now is None else now, "now")
        with self._lock:
            self._prune_expired_locked(instant)
            received = dict(self._received)
        result: dict[str, dict[str, Any]] = {}
        for sender in sorted(received):
            age = max(0.0, instant - received[sender])
            result[sender] = {
                "sender": sender,
                "state": "stale" if age > self.stale_after_s else "alive",
                "age_s": age,
            }
        return result

    @property
    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "peers": len(self._received),
                "expired": self._expired,
                "evicted": self._evicted,
            }


def _validate_sender(sender: Any, limits: WorldBusLimits) -> str:
    if not isinstance(sender, str) or not sender or not _NAME_RE.fullmatch(sender):
        raise ValidationError("sender must use only letters, digits, dot, underscore, or dash")
    if len(sender.encode("utf-8")) > limits.max_sender_bytes:
        raise SizeLimitError("sender is too long")
    return sender


def _message_timestamp(timestamp_ns: Optional[int]) -> str:
    instant = time.monotonic_ns() if timestamp_ns is None else timestamp_ns
    return str(_require_decimal_int(instant, "timestamp_ns"))


def make_heartbeat(sender: str, timestamp_ns: Optional[int] = None) -> dict[str, Any]:
    sender = _validate_sender(sender, DEFAULT_LIMITS)
    return {
        "worldbus_version": PROTOCOL_VERSION,
        "kind": "heartbeat",
        "address": f"/flexgpu/v1/heartbeat/{sender}",
        "timestamp_ns": _message_timestamp(timestamp_ns),
        "sender": sender,
    }


def make_control(
    address: str, value: Any, timestamp_ns: Optional[int] = None, request_id: Optional[str] = None
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "worldbus_version": PROTOCOL_VERSION,
        "kind": "control",
        "address": address,
        "timestamp_ns": _message_timestamp(timestamp_ns),
        "value": value,
    }
    if request_id is not None:
        result["request_id"] = request_id
    return validate_udp_message(result)


def make_metadata_message(
    metadata: FrameMetadata | Mapping[str, Any], timestamp_ns: Optional[int] = None
) -> dict[str, Any]:
    normalized = (
        validate_metadata(metadata.to_dict())
        if isinstance(metadata, FrameMetadata)
        else validate_metadata(metadata)
    )
    return {
        "worldbus_version": PROTOCOL_VERSION,
        "kind": "metadata",
        "address": "/flexgpu/v1/frame/metadata",
        "timestamp_ns": _message_timestamp(timestamp_ns),
        "metadata": normalized.to_dict(),
    }


def validate_udp_message(
    value: Mapping[str, Any], limits: WorldBusLimits = DEFAULT_LIMITS
) -> dict[str, Any]:
    """Validate an OSC-like JSON heartbeat, metadata, or control datagram."""

    data = _require_mapping(value, "UDP message")
    version = _require_int(data.get("worldbus_version"), "worldbus_version", 1, 255)
    if version != PROTOCOL_VERSION:
        raise ValidationError(
            f"unsupported worldbus_version {version}; expected {PROTOCOL_VERSION}"
        )
    kind = data.get("kind")
    if not isinstance(kind, str) or kind not in {"heartbeat", "metadata", "control"}:
        raise ValidationError("kind must be heartbeat, metadata, or control")
    address = data.get("address")
    if not isinstance(address, str) or not address.startswith("/flexgpu/v1/"):
        raise ValidationError("address must start with /flexgpu/v1/")
    if len(address.encode("utf-8")) > 256 or any(ord(char) < 33 for char in address):
        raise ValidationError("address is too long or contains whitespace/control characters")
    timestamp_ns = _require_decimal_int(data.get("timestamp_ns"), "timestamp_ns")
    normalized: dict[str, Any] = {
        "worldbus_version": PROTOCOL_VERSION,
        "kind": kind,
        "address": address,
        "timestamp_ns": str(timestamp_ns),
    }
    if kind == "heartbeat":
        sender = _validate_sender(data.get("sender"), limits)
        if address != f"/flexgpu/v1/heartbeat/{sender}":
            raise ValidationError("heartbeat address does not match sender")
        normalized["sender"] = sender
    elif kind == "metadata":
        if address != "/flexgpu/v1/frame/metadata":
            raise ValidationError("metadata address must be /flexgpu/v1/frame/metadata")
        metadata = validate_metadata(
            _require_mapping(data.get("metadata"), "metadata"), limits
        )
        normalized["metadata"] = metadata.to_dict()
    else:
        if not (
            address.startswith("/flexgpu/v1/control/")
            or address.startswith("/flexgpu/v1/interaction/")
        ):
            raise ValidationError("control address must use the control or interaction namespace")
        if "value" not in data:
            raise ValidationError("control message requires value")
        _validate_json_value(data["value"], limits)
        normalized["value"] = data["value"]
        if "request_id" in data:
            request_id = data["request_id"]
            if not isinstance(request_id, str) or len(request_id.encode("utf-8")) > 128:
                raise ValidationError("request_id must be a string of at most 128 bytes")
            normalized["request_id"] = request_id
    encoded = _strict_json_bytes(normalized)
    if len(encoded) > limits.max_udp_bytes:
        raise SizeLimitError(
            f"UDP message uses {len(encoded)} bytes; limit is {limits.max_udp_bytes}"
        )
    return normalized


def encode_udp_message(
    value: Mapping[str, Any], limits: WorldBusLimits = DEFAULT_LIMITS
) -> bytes:
    return _strict_json_bytes(validate_udp_message(value, limits))


def decode_udp_message(data: bytes, limits: WorldBusLimits = DEFAULT_LIMITS) -> dict[str, Any]:
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("UDP datagram must be bytes-like")
    wire = bytes(data)
    if len(wire) > limits.max_udp_bytes:
        raise SizeLimitError(
            f"UDP datagram uses {len(wire)} bytes; limit is {limits.max_udp_bytes}"
        )
    try:
        text = wire.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError("UDP datagram is not UTF-8") from exc
    return validate_udp_message(_require_mapping(_strict_json_loads(text), "UDP message"), limits)


class UDPJsonEndpoint:
    """Small UDP endpoint for validated WorldBus JSON datagrams."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        *,
        limits: WorldBusLimits = DEFAULT_LIMITS,
    ) -> None:
        self.limits = limits
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind((host, port))
        bound = self.socket.getsockname()
        self.address = (str(bound[0]), int(bound[1]))

    def send(self, message: Mapping[str, Any], host: str, port: int) -> int:
        data = encode_udp_message(message, self.limits)
        return self.socket.sendto(data, (host, port))

    def receive(
        self, timeout: Optional[float] = None
    ) -> tuple[dict[str, Any], tuple[str, int]]:
        if timeout is not None and (
            not math.isfinite(timeout) or timeout < 0 or timeout > self.limits.socket_timeout_s
        ):
            raise ValueError(
                f"timeout must be between 0 and {self.limits.socket_timeout_s} seconds"
            )
        self.socket.settimeout(timeout)
        data, address = self.socket.recvfrom(self.limits.max_udp_bytes + 1)
        if len(data) > self.limits.max_udp_bytes:
            raise SizeLimitError("UDP datagram exceeds the receive limit")
        return decode_udp_message(data, self.limits), (str(address[0]), int(address[1]))

    def close(self) -> None:
        self.socket.close()

    def __enter__(self) -> "UDPJsonEndpoint":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


class TCPFrameSender:
    """Persistent TCP sender for a sequence of length-prefixed frames."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        timeout: Optional[float] = None,
        limits: WorldBusLimits = DEFAULT_LIMITS,
    ) -> None:
        self.limits = limits
        selected_timeout = limits.socket_timeout_s if timeout is None else timeout
        if (
            not math.isfinite(selected_timeout)
            or selected_timeout <= 0
            or selected_timeout > limits.socket_timeout_s
        ):
            raise ValueError(
                f"timeout must be greater than 0 and at most {limits.socket_timeout_s} seconds"
            )
        self.socket = socket.create_connection((host, port), timeout=selected_timeout)
        self.socket.settimeout(selected_timeout)

    def send(self, frame: WorldFrame) -> int:
        wire = encode_frame(frame, self.limits)
        self.socket.sendall(wire)
        return len(wire)

    def close(self) -> None:
        try:
            self.socket.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        self.socket.close()

    def __enter__(self) -> "TCPFrameSender":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


def send_tcp_frame(
    host: str,
    port: int,
    frame: WorldFrame,
    *,
    timeout: Optional[float] = None,
    limits: WorldBusLimits = DEFAULT_LIMITS,
) -> int:
    with TCPFrameSender(host, port, timeout=timeout, limits=limits) as sender:
        return sender.send(frame)


class WorldBusReceiver:
    """Reference TCP/UDP receiver with a newest-only frame queue."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        tcp_port: int = 0,
        udp_port: int = 0,
        *,
        stale_after_s: float = 1.0,
        limits: WorldBusLimits = DEFAULT_LIMITS,
    ) -> None:
        if (
            not isinstance(limits.socket_timeout_s, (int, float))
            or not math.isfinite(float(limits.socket_timeout_s))
            or limits.socket_timeout_s <= 0
        ):
            raise ValueError("limits.socket_timeout_s must be a positive finite number")
        self.host = host
        self.requested_tcp_port = tcp_port
        self.requested_udp_port = udp_port
        self.limits = limits
        self.frames = NewestFrameQueue(limits)
        self.heartbeats = HeartbeatMonitor(stale_after_s, limits=limits)
        self._controls: collections.deque[dict[str, Any]] = collections.deque(maxlen=128)
        self._metadata: collections.OrderedDict[
            tuple[Optional[str], int], FrameMetadata
        ] = collections.OrderedDict()
        self._errors: collections.deque[str] = collections.deque(maxlen=64)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._tcp_socket: Optional[socket.socket] = None
        self._udp_endpoint: Optional[UDPJsonEndpoint] = None
        self._tcp_thread: Optional[threading.Thread] = None
        self._udp_thread: Optional[threading.Thread] = None
        self._active_connection: Optional[socket.socket] = None
        self._closed = False

    @property
    def tcp_address(self) -> tuple[str, int]:
        if self._tcp_socket is None:
            raise WorldBusError("receiver is not started")
        address = self._tcp_socket.getsockname()
        return str(address[0]), int(address[1])

    @property
    def udp_address(self) -> tuple[str, int]:
        if self._udp_endpoint is None:
            raise WorldBusError("receiver is not started")
        return self._udp_endpoint.address

    def start(self) -> "WorldBusReceiver":
        if self._closed:
            raise WorldBusError("receiver is closed and cannot be restarted")
        if self._tcp_socket is not None:
            raise WorldBusError("receiver is already started")
        tcp_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        tcp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            tcp_socket.bind((self.host, self.requested_tcp_port))
            tcp_socket.listen(4)
            poll_timeout = min(0.2, float(self.limits.socket_timeout_s))
            tcp_socket.settimeout(poll_timeout)
            udp_endpoint = UDPJsonEndpoint(
                self.host, self.requested_udp_port, limits=self.limits
            )
        except Exception:
            tcp_socket.close()
            raise
        self._tcp_socket = tcp_socket
        self._udp_endpoint = udp_endpoint
        self._stop.clear()
        self._tcp_thread = threading.Thread(
            target=self._tcp_loop, name="worldbus-tcp", daemon=True
        )
        self._udp_thread = threading.Thread(
            target=self._udp_loop, name="worldbus-udp", daemon=True
        )
        try:
            self._tcp_thread.start()
            self._udp_thread.start()
        except Exception:
            self._closed = True
            self._stop.set()
            tcp_socket.close()
            udp_endpoint.close()
            for thread in (self._tcp_thread, self._udp_thread):
                if thread is not None and thread.ident is not None:
                    thread.join(timeout=1.0)
            self.frames.close()
            self._tcp_socket = None
            self._udp_endpoint = None
            raise
        return self

    def _record_error(self, exc: BaseException) -> None:
        with self._lock:
            self._errors.append(type(exc).__name__ + ": " + str(exc))

    def _tcp_loop(self) -> None:
        assert self._tcp_socket is not None
        while not self._stop.is_set():
            try:
                connection, _ = self._tcp_socket.accept()
            except socket.timeout:
                continue
            except OSError as exc:
                if not self._stop.is_set():
                    self._record_error(exc)
                break
            with connection:
                poll_timeout = min(0.2, float(self.limits.socket_timeout_s))
                connection.settimeout(poll_timeout)
                decoder = FrameStreamDecoder(self.limits)
                last_activity = time.monotonic()
                frame_started_at: Optional[float] = None
                with self._lock:
                    self._active_connection = connection
                try:
                    while not self._stop.is_set():
                        now = time.monotonic()
                        if (
                            frame_started_at is not None
                            and now - frame_started_at >= self.limits.socket_timeout_s
                        ):
                            raise FramingError("incomplete frame exceeded receive deadline")
                        try:
                            chunk = connection.recv(64 * 1024)
                        except socket.timeout:
                            if time.monotonic() - last_activity >= self.limits.socket_timeout_s:
                                break
                            continue
                        if not chunk:
                            decoder.finish()
                            break
                        last_activity = time.monotonic()
                        for frame in decoder.feed(chunk):
                            self.frames.put(frame)
                        now = time.monotonic()
                        if decoder.pending_bytes:
                            if frame_started_at is None:
                                frame_started_at = now
                            elif now - frame_started_at >= self.limits.socket_timeout_s:
                                raise FramingError("incomplete frame exceeded receive deadline")
                        else:
                            frame_started_at = None
                except (OSError, WorldBusError) as exc:
                    if not self._stop.is_set():
                        self._record_error(exc)
                finally:
                    with self._lock:
                        self._active_connection = None

    def _udp_loop(self) -> None:
        assert self._udp_endpoint is not None
        poll_timeout = min(0.2, float(self.limits.socket_timeout_s))
        while not self._stop.is_set():
            try:
                message, _ = self._udp_endpoint.receive(poll_timeout)
            except socket.timeout:
                continue
            except (OSError, WorldBusError) as exc:
                if not self._stop.is_set():
                    self._record_error(exc)
                continue
            try:
                kind = message["kind"]
                if kind == "heartbeat":
                    self.heartbeats.record(message["sender"])
                elif kind == "control":
                    with self._lock:
                        self._controls.append(message)
                else:
                    metadata = validate_metadata(message["metadata"], self.limits)
                    raw_session = metadata.extensions.get(PRODUCER_SESSION_FIELD)
                    session_id = str(raw_session) if raw_session is not None else None
                    key = (session_id, metadata.frame_id)
                    with self._lock:
                        self._metadata[key] = metadata
                        self._metadata.move_to_end(key)
                        while len(self._metadata) > 16:
                            self._metadata.popitem(last=False)
            except WorldBusError as exc:
                self._record_error(exc)

    def metadata_for(
        self, frame_id: int, producer_session_id: Optional[str] = None
    ) -> Optional[FrameMetadata]:
        with self._lock:
            if producer_session_id is not None:
                return self._metadata.get((producer_session_id, frame_id))
            for (_session_id, stored_frame_id), metadata in reversed(self._metadata.items()):
                if stored_frame_id == frame_id:
                    return metadata
        return None

    def pop_controls(self) -> list[dict[str, Any]]:
        with self._lock:
            result = list(self._controls)
            self._controls.clear()
            return result

    @property
    def errors(self) -> list[str]:
        with self._lock:
            return list(self._errors)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        with self._lock:
            active = self._active_connection
        if active is not None:
            try:
                active.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                active.close()
            except OSError:
                pass
        if self._tcp_socket is not None:
            self._tcp_socket.close()
        if self._udp_endpoint is not None:
            self._udp_endpoint.close()
        for thread in (self._tcp_thread, self._udp_thread):
            if thread is not None and thread.ident is not None:
                thread.join(timeout=1.0)
        self.frames.close()
        self._tcp_socket = None
        self._udp_endpoint = None

    def __enter__(self) -> "WorldBusReceiver":
        return self.start()

    def __exit__(self, *_: Any) -> None:
        self.close()


def generate_replay_frames(
    count: int = 8,
    width: int = 32,
    height: int = 16,
    *,
    start_frame_id: int = 1,
    timestamp_start_ns: Optional[int] = None,
    interval_ns: int = 100_000_000,
    generation_id: str = "worldbus-replay",
    producer_session_id: Optional[str] = None,
    limits: WorldBusLimits = DEFAULT_LIMITS,
) -> Iterator[WorldFrame]:
    """Generate a deterministic, semantically valid moving RGBA atlas lazily."""

    count = _require_int(count, "count", 1, limits.max_replay_frames)
    start_frame_id = _require_int(start_frame_id, "start_frame_id")
    if start_frame_id + count - 1 > MAX_INT64:
        raise ValidationError("generated frame IDs exceed int64")
    interval_ns = _require_int(
        interval_ns,
        "interval_ns",
        limits.min_replay_interval_ns,
        limits.max_replay_interval_ns,
    )
    initial_timestamp = (
        time.monotonic_ns() if timestamp_start_ns is None else timestamp_start_ns
    )
    initial_timestamp = _require_decimal_int(initial_timestamp, "timestamp_start_ns")
    selected_session_id = (
        "worldbus-replay-%x" % initial_timestamp
        if producer_session_id is None
        else producer_session_id
    )
    if initial_timestamp + (count - 1) * interval_ns > MAX_INT64:
        raise ValidationError("generated timestamps exceed int64")
    width = _require_int(width, "width", 1, limits.max_width)
    height = _require_int(height, "height", 1, limits.max_height)
    if width % 2:
        raise ValidationError("rgba8_atlas width must be even")
    if width * height > limits.max_pixels:
        raise SizeLimitError("generated frame exceeds max_pixels")
    payload_size = width * height * 4
    if payload_size > limits.max_payload_bytes:
        raise SizeLimitError("generated frame exceeds max_payload_bytes")
    source_width = width // 2
    focal = float(max(source_width, height))
    identity = (
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
    for offset in range(count):
        payload = bytearray(payload_size)
        cursor = 0
        for y in range(height):
            for x in range(width):
                if x < source_width:
                    # Left plane: generated RGBA color.
                    payload[cursor] = (x * 7 + offset * 17) & 255
                    payload[cursor + 1] = (y * 11 + offset * 9) & 255
                    payload[cursor + 2] = ((x + y) * 5 + offset * 23) & 255
                    payload[cursor + 3] = 255
                else:
                    # Right plane: normalized uint16 depth in big-endian R/G,
                    # validity mask in B, and confidence in A.
                    source_x = x - source_width
                    denominator = max(1, (source_width - 1) + (height - 1))
                    normalized_depth = (
                        (source_x + y + (offset % max(1, source_width))) % (denominator + 1)
                    ) / float(denominator)
                    packed_depth = int(round(normalized_depth * 65535.0))
                    disoccluded = ((source_x + y + offset) % 11) == 10
                    payload[cursor] = (packed_depth >> 8) & 255
                    payload[cursor + 1] = packed_depth & 255
                    payload[cursor + 2] = 0 if disoccluded else 255
                    payload[cursor + 3] = 48 if disoccluded else 224
                cursor += 4
        metadata = {
            "worldbus_version": PROTOCOL_VERSION,
            "frame_id": start_frame_id + offset,
            "timestamp_ns": str(initial_timestamp + offset * interval_ns),
            "width": width,
            "height": height,
            "pixel_format": "rgba8_atlas",
            "payload_bytes": payload_size,
            "intrinsics": [focal, focal, source_width / 2.0, height / 2.0],
            "depth_scale_bias": [1.0, 0.0],
            "camera_to_world": list(identity),
            "generation_id": generation_id,
            PRODUCER_SESSION_FIELD: selected_session_id,
            "replay_pattern": "moving-atlas-v1",
        }
        yield make_frame(metadata, bytes(payload), limits)


def write_replay(
    path: str | os.PathLike[str],
    frames: Iterable[WorldFrame],
    *,
    overwrite: bool = False,
    limits: WorldBusLimits = DEFAULT_LIMITS,
) -> dict[str, Any]:
    """Atomically write a bounded concatenated-frame ``.wbr`` replay."""

    destination = os.path.abspath(os.fspath(path))
    if os.path.exists(destination) and not overwrite:
        raise FileExistsError(destination)
    directory = os.path.dirname(destination) or os.curdir
    os.makedirs(directory, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".worldbus-", suffix=".tmp", dir=directory)
    count = 0
    total = len(REPLAY_MAGIC)
    first_frame_id: Optional[int] = None
    last_frame_id: Optional[int] = None
    last_timestamp_ns: Optional[int] = None
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(REPLAY_MAGIC)
            for frame in frames:
                if count >= limits.max_replay_frames:
                    raise SizeLimitError("replay contains too many frames")
                wire = encode_frame(frame, limits)
                frame_id = frame.metadata.frame_id
                timestamp_ns = frame.metadata.timestamp_ns
                if last_frame_id is not None and frame_id <= last_frame_id:
                    raise ValidationError("replay frame IDs must be strictly increasing")
                if last_timestamp_ns is not None and timestamp_ns < last_timestamp_ns:
                    raise ValidationError("replay timestamps must be non-decreasing")
                total += len(wire)
                if total > limits.max_replay_bytes:
                    raise SizeLimitError("replay exceeds max_replay_bytes")
                handle.write(wire)
                first_frame_id = frame_id if first_frame_id is None else first_frame_id
                last_frame_id = frame_id
                last_timestamp_ns = timestamp_ns
                count += 1
            handle.flush()
            os.fsync(handle.fileno())
        if count == 0:
            raise ValidationError("replay must contain at least one frame")
        if not overwrite and os.path.exists(destination):
            raise FileExistsError(destination)
        os.replace(temporary, destination)
    except Exception:
        try:
            os.remove(temporary)
        except OSError:
            pass
        raise
    return {
        "path": destination,
        "frames": count,
        "bytes": total,
        "first_frame_id": first_frame_id,
        "last_frame_id": last_frame_id,
    }


def _read_exact_file(handle: Any, size: int, *, allow_clean_eof: bool = False) -> Optional[bytes]:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = handle.read(size - len(chunks))
        if not chunk:
            if allow_clean_eof and not chunks:
                return None
            raise FramingError("replay ended in the middle of a frame")
        chunks.extend(chunk)
    return bytes(chunks)


def iter_replay(
    path: str | os.PathLike[str], limits: WorldBusLimits = DEFAULT_LIMITS
) -> Iterator[WorldFrame]:
    """Stream a bounded replay without loading the entire file into memory."""

    source = os.path.abspath(os.fspath(path))
    size = os.path.getsize(source)
    if size > limits.max_replay_bytes:
        raise SizeLimitError("replay exceeds max_replay_bytes")
    with open(source, "rb") as handle:
        if handle.read(len(REPLAY_MAGIC)) != REPLAY_MAGIC:
            raise FramingError("invalid replay magic or version")
        count = 0
        previous_frame_id: Optional[int] = None
        previous_timestamp_ns: Optional[int] = None
        while True:
            prefix = _read_exact_file(handle, FRAME_PREFIX.size, allow_clean_eof=True)
            if prefix is None:
                break
            magic, metadata_size, payload_size = FRAME_PREFIX.unpack(prefix)
            if magic != FRAME_MAGIC:
                raise FramingError("invalid frame magic in replay")
            if metadata_size < 2 or metadata_size > limits.max_metadata_bytes:
                raise SizeLimitError("replay metadata length exceeds protocol limits")
            if payload_size < 1 or payload_size > limits.max_payload_bytes:
                raise SizeLimitError("replay payload length exceeds protocol limits")
            metadata_bytes = _read_exact_file(handle, metadata_size)
            payload = _read_exact_file(handle, payload_size)
            assert metadata_bytes is not None and payload is not None
            count += 1
            if count > limits.max_replay_frames:
                raise SizeLimitError("replay contains too many frames")
            frame = _parse_framed_parts(metadata_bytes, payload, payload_size, limits)
            if previous_frame_id is not None and frame.metadata.frame_id <= previous_frame_id:
                raise ValidationError("replay frame IDs must be strictly increasing")
            if (
                previous_timestamp_ns is not None
                and frame.metadata.timestamp_ns < previous_timestamp_ns
            ):
                raise ValidationError("replay timestamps must be non-decreasing")
            previous_frame_id = frame.metadata.frame_id
            previous_timestamp_ns = frame.metadata.timestamp_ns
            yield frame


def replay_summary(
    path: str | os.PathLike[str], limits: WorldBusLimits = DEFAULT_LIMITS
) -> dict[str, Any]:
    count = 0
    first: Optional[FrameMetadata] = None
    last: Optional[FrameMetadata] = None
    for frame in iter_replay(path, limits):
        first = frame.metadata if first is None else first
        last = frame.metadata
        count += 1
    if first is None or last is None:
        raise ValidationError("replay contains no frames")
    return {
        "path": os.path.abspath(os.fspath(path)),
        "frames": count,
        "first_frame_id": first.frame_id,
        "last_frame_id": last.frame_id,
        "width": first.width,
        "height": first.height,
        "pixel_format": first.pixel_format,
        "duration_ns": max(0, last.timestamp_ns - first.timestamp_ns),
        "generation_id": first.generation_id,
        "producer_session_id": first.extensions.get(PRODUCER_SESSION_FIELD),
    }


def send_replay(
    path: str | os.PathLike[str],
    host: str,
    port: int,
    *,
    realtime: bool = True,
    speed: float = 1.0,
    limits: WorldBusLimits = DEFAULT_LIMITS,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Send a replay over one persistent TCP connection with bounded pacing."""

    if not isinstance(speed, (int, float)) or not math.isfinite(float(speed)):
        raise ValueError("speed must be finite")
    speed = float(speed)
    if speed < 0.05 or speed > 100.0:
        raise ValueError("speed must be between 0.05 and 100")
    count = 0
    bytes_sent = 0
    previous_timestamp: Optional[int] = None
    with TCPFrameSender(host, port, limits=limits) as sender:
        for frame in iter_replay(path, limits):
            if realtime and previous_timestamp is not None:
                interval = frame.metadata.timestamp_ns - previous_timestamp
                interval = max(0, min(interval, limits.max_replay_interval_ns))
                if interval:
                    sleeper(interval / 1_000_000_000.0 / speed)
            bytes_sent += sender.send(frame)
            previous_timestamp = frame.metadata.timestamp_ns
            count += 1
    if count == 0:
        raise ValidationError("replay contains no frames")
    return {"frames": count, "wire_bytes": bytes_sent, "host": host, "port": port}


__all__ = [
    "DEFAULT_LIMITS",
    "FRAME_MAGIC",
    "FRAME_PREFIX",
    "PROTOCOL_VERSION",
    "PRODUCER_SESSION_FIELD",
    "REPLAY_MAGIC",
    "FrameMetadata",
    "FrameStreamDecoder",
    "FramingError",
    "HeartbeatMonitor",
    "NewestFrameQueue",
    "QueueClosed",
    "SizeLimitError",
    "TCPFrameSender",
    "UDPJsonEndpoint",
    "ValidationError",
    "WorldBusError",
    "WorldBusLimits",
    "WorldBusReceiver",
    "WorldFrame",
    "decode_frame",
    "decode_udp_message",
    "encode_frame",
    "encode_udp_message",
    "generate_replay_frames",
    "iter_replay",
    "make_control",
    "make_frame",
    "make_heartbeat",
    "make_metadata_message",
    "replay_summary",
    "send_replay",
    "send_tcp_frame",
    "validate_frame",
    "validate_metadata",
    "validate_udp_message",
    "write_replay",
]
