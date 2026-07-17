#!/usr/bin/env python3
"""Default-off laptop-camera Depth Anything V2 Small sensor emulator.

The webcam and model live in this isolated process.  Camera RGB is used only
in volatile process memory and is never sent, saved, or included in telemetry.
The only output is a bounded WorldBus RGBA8 sensor frame containing uint16
pseudo-metre depth, foreground mask, and heuristic confidence.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import ipaddress
import json
import math
import os
import re
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from flexgpu.depth_anything_transport import (  # noqa: E402
    IDENTITY_4X4,
    MAX_HEIGHT,
    MAX_PIXELS,
    MAX_WIDTH,
    make_sensor_worldbus_metadata,
    pack_sensor_frame_numpy,
)
from flexgpu.worldbus import TCPFrameSender, WorldBusError, make_frame  # noqa: E402


MODEL_REPOSITORY = "depth-anything/Depth-Anything-V2-Small-hf"
MODEL_REVISION = "870a35c76c2bc1d82fbde922d95015496cb7dd6c"
MODEL_FILENAME = "model.safetensors"
MODEL_BYTES = 99_173_660
MODEL_SHA256 = "3152477ce0d8d6978d76b995120de97cb5b928701fd0f817769f59e249a16b70"
MODEL_LICENSE = "Apache-2.0"
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
MAX_SESSION_ID_BYTES = 64
RUNTIME_ROOT = (REPOSITORY_ROOT / "runtime").resolve()
DEFAULT_MODEL_DIR = RUNTIME_ROOT / "depth-anything-v2-small"
DEFAULT_CACHE_DIR = RUNTIME_ROOT / "depth-anything-cache"
DEFAULT_OUTPUT_HOST = "127.0.0.1"
DEFAULT_OUTPUT_TCP_PORT = 9241
RESERVED_OUTPUT_UDP_PORT = 9240


class WorkerError(RuntimeError):
    """A bounded sensor worker operation failed."""


class DependencyError(WorkerError):
    """The isolated runtime is incomplete."""


class ModelError(WorkerError):
    """The pinned model snapshot is missing or changed."""


class CaptureError(WorkerError):
    """The webcam or synthetic capture source stopped producing frames."""


class CalibrationError(WorkerError):
    """Relative-depth mapping could not be frozen safely."""


@dataclass(frozen=True)
class WorkerProfile:
    tier: str
    input_size: int
    output_width: int
    output_height: int
    inference_hz: float
    precision: str
    note: str


PROFILES: dict[str, WorkerProfile] = {
    "3080ti_16gb": WorkerProfile(
        tier="3080ti_16gb",
        input_size=384,
        output_width=256,
        output_height=144,
        inference_hz=3.0,
        precision="fp16",
        note="Conservative RTX 3080 Ti Laptop starting profile; measure with StreamDiffusion running.",
    ),
    "4090": WorkerProfile(
        tier="4090",
        input_size=384,
        output_width=256,
        output_height=144,
        inference_hz=3.0,
        precision="fp16",
        note="Same privacy-first baseline; raise rate only after a combined-workload soak.",
    ),
    "5090": WorkerProfile(
        tier="5090",
        input_size=384,
        output_width=256,
        output_height=144,
        inference_hz=3.0,
        precision="fp16",
        note="Same deterministic baseline; higher settings remain an explicit benchmark decision.",
    ),
}


@dataclass(frozen=True)
class CapturedFrame:
    """One volatile camera image stamped immediately after capture returns."""

    timestamp_ns: int
    bgr: Any


@dataclass(frozen=True)
class MappedDepth:
    depth_metres: Any
    foreground_mask: Any
    confidence: Any


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, allow_nan=False), flush=True)


def _profile(name: str) -> WorkerProfile:
    try:
        return PROFILES[name]
    except KeyError as exc:
        raise WorkerError("unknown sensor worker profile: " + name) from exc


def _validate_output_dimensions(width: object, height: object) -> tuple[int, int]:
    for label, value, low, high in (
        ("output_width", width, 64, MAX_WIDTH),
        ("output_height", height, 64, MAX_HEIGHT),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise WorkerError(f"{label} must be an integer between {low} and {high}")
    assert isinstance(width, int) and isinstance(height, int)
    if width * height > MAX_PIXELS:
        raise WorkerError(
            f"output dimensions exceed the {MAX_PIXELS}-pixel receiver limit"
        )
    return width, height


def _safe_runtime_path(value: str | os.PathLike[str], label: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = REPOSITORY_ROOT / path
    resolved = path.resolve()
    try:
        resolved.relative_to(RUNTIME_ROOT)
    except ValueError as exc:
        raise WorkerError(label + " must stay under the repository runtime directory") from exc
    return resolved


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_model_directory(path: Path) -> None:
    """Require the exact pinned safetensors weight and local config files."""

    required = ("config.json", "preprocessor_config.json", MODEL_FILENAME)
    if not path.is_dir() or any(not (path / name).is_file() for name in required):
        raise ModelError("pinned Depth Anything V2 Small snapshot is not installed")
    weight = path / MODEL_FILENAME
    if weight.stat().st_size != MODEL_BYTES:
        raise ModelError("pinned Depth Anything V2 Small weight has the wrong byte length")
    if _sha256_file(weight) != MODEL_SHA256:
        raise ModelError("pinned Depth Anything V2 Small weight failed SHA-256 verification")
    try:
        config = json.loads((path / "config.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ModelError("pinned model config is unreadable") from exc
    if config.get("architectures") != ["DepthAnythingForDepthEstimation"]:
        raise ModelError("pinned model config has an unexpected architecture")


def _configure_offline_cache(cache_dir: Path) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    os.environ["HF_HOME"] = str(cache_dir)
    os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"


def install_model(model_dir: Path, cache_dir: Path) -> dict[str, Any]:
    """Explicit network action used only by the model-install subcommand."""

    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise DependencyError("huggingface-hub is required for model-install") from exc
    model_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    os.environ["HF_HOME"] = str(cache_dir)
    os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    snapshot_download(
        repo_id=MODEL_REPOSITORY,
        revision=MODEL_REVISION,
        local_dir=str(model_dir),
        cache_dir=str(cache_dir),
        allow_patterns=(
            "config.json",
            "preprocessor_config.json",
            MODEL_FILENAME,
            "README.md",
            "LICENSE*",
        ),
    )
    verify_model_directory(model_dir)
    return {
        "status": "ok",
        "model_id": MODEL_REPOSITORY,
        "model_revision": MODEL_REVISION,
        "model_sha256": MODEL_SHA256,
        "model_path": str(model_dir),
    }


def _package_version(name: str) -> Optional[str]:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


class MockDepthBackend:
    """Deterministic CPU relative depth for CI and transport rehearsal."""

    model_id = "flexgpu/mock-depth-anything-v2-small"
    model_revision = "0" * 40
    precision = "mock"

    def __init__(self) -> None:
        self.load_count = 1
        self.inference_count = 0

    def infer(
        self,
        rgb: Any,
        *,
        input_size: int,
        output_width: int,
        output_height: int,
    ) -> Any:
        del rgb, input_size
        try:
            import numpy as np
        except ImportError as exc:
            raise DependencyError("NumPy is required for the mock backend") from exc
        x = np.linspace(0.0, 1.0, output_width, dtype=np.float32)
        y = np.linspace(0.0, 1.0, output_height, dtype=np.float32)
        xx, yy = np.meshgrid(x, y)
        self.inference_count += 1
        return (0.2 + 0.7 * (1.0 - xx) + 0.1 * yy).astype(np.float32)


class DepthAnythingBackend:
    """Persistent pinned Transformers Depth Anything V2 Small on one GPU."""

    model_id = MODEL_REPOSITORY
    model_revision = MODEL_REVISION
    precision = "fp16"

    def __init__(
        self,
        *,
        model_dir: Path,
        cache_dir: Path,
        device: str,
        warmup: int = 1,
    ) -> None:
        if isinstance(warmup, bool) or not isinstance(warmup, int) or not 0 <= warmup <= 10:
            raise WorkerError("warmup must be an integer between 0 and 10")
        verify_model_directory(model_dir)
        _configure_offline_cache(cache_dir)
        try:
            import numpy as np
            import torch
            from transformers import AutoImageProcessor, AutoModelForDepthEstimation
        except ImportError as exc:
            raise DependencyError(
                "NumPy, PyTorch, and Transformers are required for real inference"
            ) from exc
        if not isinstance(device, str) or not device.startswith("cuda"):
            raise DependencyError("the live Depth Anything backend requires a CUDA device")
        if not torch.cuda.is_available():
            raise DependencyError("CUDA is not available to the sensor worker")
        try:
            selected_device = torch.device(device)
            torch.cuda.set_device(selected_device)
        except (RuntimeError, ValueError) as exc:
            raise DependencyError("requested CUDA device is unavailable") from exc
        self.np = np
        self.torch = torch
        self.device = selected_device
        self.warmup = warmup
        self._warmed = False
        self.processor = AutoImageProcessor.from_pretrained(
            str(model_dir), local_files_only=True, trust_remote_code=False
        )
        self.model = AutoModelForDepthEstimation.from_pretrained(
            str(model_dir),
            local_files_only=True,
            trust_remote_code=False,
            use_safetensors=True,
        ).to(selected_device).eval().half()
        self.load_count = 1
        self.inference_count = 0

    def infer(
        self,
        rgb: Any,
        *,
        input_size: int,
        output_width: int,
        output_height: int,
    ) -> Any:
        inputs = self.processor(
            images=rgb,
            do_resize=True,
            size={"height": input_size, "width": input_size},
            keep_aspect_ratio=True,
            ensure_multiple_of=14,
            return_tensors="pt",
        )
        pixel_values = inputs["pixel_values"].to(
            device=self.device, dtype=self.torch.float16
        )

        def invoke() -> Any:
            with self.torch.inference_mode():
                output = self.model(pixel_values=pixel_values).predicted_depth
                return self.torch.nn.functional.interpolate(
                    output.unsqueeze(1),
                    size=(output_height, output_width),
                    mode="bicubic",
                    align_corners=False,
                ).squeeze(1)

        if not self._warmed:
            for _ in range(self.warmup):
                invoke()
            self.torch.cuda.synchronize(self.device)
            self._warmed = True
        prediction = invoke()
        self.torch.cuda.synchronize(self.device)
        self.inference_count += 1
        return prediction[0].detach().float().cpu().numpy().astype(
            self.np.float32, copy=False
        )


class FrozenPercentileMapper:
    """Map relative depth to a fixed pseudo-metre slab without frame breathing."""

    def __init__(
        self,
        *,
        mode: str = "session_frozen",
        percentile_low: float = 2.0,
        percentile_high: float = 98.0,
        calibration_frames: int = 12,
        raw_low: Optional[float] = None,
        raw_high: Optional[float] = None,
        raw_order: str = "near_is_larger",
        pseudo_near_m: float = 0.5,
        pseudo_far_m: float = 4.0,
        foreground_far_m: float = 3.0,
    ) -> None:
        if mode not in {"fixed", "session_frozen"}:
            raise CalibrationError("mode must be fixed or session_frozen")
        if raw_order not in {"near_is_larger", "near_is_smaller"}:
            raise CalibrationError("raw_order is unsupported")
        numeric = (
            percentile_low,
            percentile_high,
            pseudo_near_m,
            pseudo_far_m,
            foreground_far_m,
        )
        if any(isinstance(value, bool) or not math.isfinite(float(value)) for value in numeric):
            raise CalibrationError("calibration values must be finite numbers")
        if not 0.0 <= percentile_low < percentile_high <= 100.0:
            raise CalibrationError("percentiles must be increasing in [0, 100]")
        if (
            isinstance(calibration_frames, bool)
            or not isinstance(calibration_frames, int)
            or not 1 <= calibration_frames <= 120
        ):
            raise CalibrationError("calibration_frames must be between 1 and 120")
        if not 0.0 < pseudo_near_m < pseudo_far_m:
            raise CalibrationError("pseudo-metre slab must be positive and increasing")
        if not pseudo_near_m <= foreground_far_m <= pseudo_far_m:
            raise CalibrationError("foreground_far_m must stay inside the pseudo-metre slab")
        self.mode = mode
        self.percentile_low = float(percentile_low)
        self.percentile_high = float(percentile_high)
        self.calibration_frames = calibration_frames
        self.raw_order = raw_order
        self.pseudo_near_m = float(pseudo_near_m)
        self.pseudo_far_m = float(pseudo_far_m)
        self.foreground_far_m = float(foreground_far_m)
        self._frame_lows: list[float] = []
        self._frame_highs: list[float] = []
        self.raw_low: Optional[float] = None
        self.raw_high: Optional[float] = None
        self.calibration_id: Optional[str] = None
        self.calibration_digest: Optional[str] = None
        if mode == "fixed":
            if raw_low is None or raw_high is None:
                raise CalibrationError("fixed mode requires raw_low and raw_high")
            self._freeze(float(raw_low), float(raw_high))
        elif raw_low is not None or raw_high is not None:
            raise CalibrationError("raw bounds are only accepted in fixed mode")

    @property
    def locked(self) -> bool:
        return self.raw_low is not None and self.raw_high is not None

    @property
    def observed_frames(self) -> int:
        return len(self._frame_lows)

    def _freeze(self, raw_low: float, raw_high: float) -> None:
        if not math.isfinite(raw_low) or not math.isfinite(raw_high):
            raise CalibrationError("raw bounds must be finite")
        minimum_span = max(1.0e-6, abs(raw_low) * 1.0e-6, abs(raw_high) * 1.0e-6)
        if raw_high - raw_low <= minimum_span:
            raise CalibrationError("raw percentile span is too small to freeze")
        self.raw_low = raw_low
        self.raw_high = raw_high
        canonical = {
            "contract": "flexgpu-depth-anything-calibration/v1",
            "mode": self.mode,
            "percentiles": [self.percentile_low, self.percentile_high],
            "raw_bounds": [raw_low, raw_high],
            "raw_order": self.raw_order,
            "pseudo_metre_slab": [self.pseudo_near_m, self.pseudo_far_m],
            "foreground_far_m": self.foreground_far_m,
        }
        encoded = json.dumps(
            canonical, allow_nan=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        self.calibration_digest = digest
        self.calibration_id = "depth-anything-" + digest[:16]

    def observe_and_map(self, relative_depth: Any) -> Optional[MappedDepth]:
        try:
            import numpy as np
        except ImportError as exc:
            raise DependencyError("NumPy is required for depth calibration") from exc
        values = np.asarray(relative_depth, dtype=np.float32)
        if values.ndim != 2:
            raise CalibrationError("relative depth must be a two-dimensional plane")
        finite = values[np.isfinite(values)]
        if finite.size < 64:
            raise CalibrationError("relative depth contains too few finite samples")
        if not self.locked:
            frame_low, frame_high = np.percentile(
                finite, [self.percentile_low, self.percentile_high]
            )
            self._frame_lows.append(float(frame_low))
            self._frame_highs.append(float(frame_high))
            if len(self._frame_lows) < self.calibration_frames:
                return None
            self._freeze(
                float(np.median(np.asarray(self._frame_lows, dtype=np.float64))),
                float(np.median(np.asarray(self._frame_highs, dtype=np.float64))),
            )
        assert self.raw_low is not None and self.raw_high is not None
        span = self.raw_high - self.raw_low
        normalized_unclipped = (values - self.raw_low) / span
        normalized = np.clip(normalized_unclipped, 0.0, 1.0)
        if self.raw_order == "near_is_larger":
            metres = self.pseudo_far_m - normalized * (
                self.pseudo_far_m - self.pseudo_near_m
            )
        else:
            metres = self.pseudo_near_m + normalized * (
                self.pseudo_far_m - self.pseudo_near_m
            )
        # This is a range proxy, not epistemic/model confidence.  Samples
        # inside the frozen percentile interval remain 1; outliers decay.
        confidence = np.clip(
            1.0 - np.abs(normalized_unclipped - normalized), 0.0, 1.0
        ).astype(np.float32)
        valid = np.isfinite(values) & np.isfinite(metres) & (confidence > 0.0)
        foreground = valid & (metres <= self.foreground_far_m)
        return MappedDepth(
            depth_metres=np.where(foreground, metres, 0.0).astype(np.float32),
            foreground_mask=foreground,
            confidence=np.where(foreground, confidence, 0.0).astype(np.float32),
        )


class LatestCaptureSlot:
    """Thread-safe one-slot image handoff that never builds camera latency."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._pending: Optional[CapturedFrame] = None
        self._closed = False
        self._error: Optional[str] = None
        self._accepted = 0
        self._superseded = 0

    def put(self, frame: CapturedFrame) -> None:
        if not isinstance(frame, CapturedFrame):
            raise TypeError("frame must be CapturedFrame")
        with self._condition:
            if self._closed:
                raise CaptureError("capture slot is closed")
            if self._pending is not None:
                self._superseded += 1
            self._pending = frame
            self._accepted += 1
            self._condition.notify()

    def get(self, timeout: float) -> CapturedFrame:
        if not math.isfinite(timeout) or not 0.0 <= timeout <= 60.0:
            raise ValueError("timeout must be between 0 and 60 seconds")
        deadline = time.monotonic() + timeout
        with self._condition:
            while self._pending is None:
                if self._closed:
                    raise CaptureError(self._error or "capture source is closed")
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    raise TimeoutError("no new camera frame arrived before timeout")
                self._condition.wait(remaining)
            frame = self._pending
            self._pending = None
            return frame

    def close(self, error: Optional[str] = None) -> None:
        with self._condition:
            self._closed = True
            self._error = error
            if error:
                # Never consume a buffered image after camera failure/disconnect.
                self._pending = None
            self._condition.notify_all()

    @property
    def stats(self) -> dict[str, int]:
        with self._condition:
            return {
                "accepted": self._accepted,
                "superseded": self._superseded,
                "pending": int(self._pending is not None),
            }


class OpenCVCamera:
    """Volatile webcam source; this class exposes no recording method."""

    source_name = "webcam"

    def __init__(self, *, index: int, width: int, height: int) -> None:
        try:
            import cv2
        except ImportError as exc:
            raise DependencyError("opencv-python is required for webcam capture") from exc
        self.cv2 = cv2
        backend = getattr(cv2, "CAP_DSHOW", 0) if os.name == "nt" else 0
        self.capture = cv2.VideoCapture(index, backend)
        if not self.capture.isOpened():
            self.capture.release()
            raise CaptureError(f"unable to open webcam index {index}")
        self.capture.set(cv2.CAP_PROP_FRAME_WIDTH, float(width))
        self.capture.set(cv2.CAP_PROP_FRAME_HEIGHT, float(height))
        self.capture.set(cv2.CAP_PROP_BUFFERSIZE, 1.0)

    def read(self) -> tuple[bool, Any]:
        return self.capture.read()

    def release(self) -> None:
        self.capture.release()


class SyntheticCapture:
    """Bounded non-camera source used by CI; produces no persistent RGB."""

    source_name = "synthetic"

    def __init__(self, *, width: int = 256, height: int = 144, fps: float = 30.0) -> None:
        try:
            import numpy as np
        except ImportError as exc:
            raise DependencyError("NumPy is required for synthetic capture") from exc
        self.np = np
        self.width = width
        self.height = height
        self.interval = 1.0 / fps
        self.frame_id = 0
        self.closed = False

    def read(self) -> tuple[bool, Any]:
        if self.closed:
            return False, None
        time.sleep(self.interval)
        x = self.np.arange(self.width, dtype=self.np.uint16)[None, :]
        y = self.np.arange(self.height, dtype=self.np.uint16)[:, None]
        phase = self.frame_id & 255
        frame = self.np.empty((self.height, self.width, 3), dtype=self.np.uint8)
        frame[..., 0] = ((x + phase) & 255).astype(self.np.uint8)
        frame[..., 1] = ((y + phase * 2) & 255).astype(self.np.uint8)
        frame[..., 2] = (((x // 2) + (y // 2) + phase) & 255).astype(self.np.uint8)
        self.frame_id += 1
        return True, frame

    def release(self) -> None:
        self.closed = True


class CapturePump:
    """Continuously drain the device into a newest-only slot."""

    def __init__(self, source: Any, *, max_consecutive_failures: int = 5) -> None:
        self.source = source
        self.slot = LatestCaptureSlot()
        self.max_consecutive_failures = max_consecutive_failures
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="depth-anything-capture", daemon=True
        )

    def start(self) -> "CapturePump":
        self._thread.start()
        return self

    def _run(self) -> None:
        failures = 0
        try:
            while not self._stop.is_set():
                ok, image = self.source.read()
                # Capture timestamp means the time read() returned this image.
                capture_timestamp_ns = time.time_ns()
                if not ok or image is None:
                    failures += 1
                    if failures >= self.max_consecutive_failures:
                        raise CaptureError("camera disconnected or repeatedly failed to capture")
                    time.sleep(0.02)
                    continue
                failures = 0
                self.slot.put(CapturedFrame(capture_timestamp_ns, image))
        # Device backends can surface disconnects as OSError or vendor-specific
        # Exception subclasses.  Close the one-slot handoff for every ordinary
        # capture-thread failure so serve() terminates instead of polling an
        # orphaned thread forever.
        except Exception as exc:
            self.slot.close(type(exc).__name__ + ": " + str(exc))
        finally:
            try:
                self.source.release()
            except Exception:
                pass

    def close(self) -> None:
        self._stop.set()
        try:
            self.source.release()
        except Exception:
            pass
        self.slot.close()
        if self._thread.ident is not None:
            self._thread.join(timeout=2.0)


def _resize_camera_rgb(bgr: Any, *, width: int, height: int) -> Any:
    try:
        import numpy as np
    except ImportError as exc:
        raise DependencyError("NumPy is required for frame preparation") from exc
    image = np.asarray(bgr)
    if image.ndim != 3 or image.shape[2] != 3 or image.dtype != np.uint8:
        raise CaptureError("camera returned a malformed uint8 BGR frame")
    source_height, source_width = image.shape[:2]
    if source_width < 2 or source_height < 2:
        raise CaptureError("camera frame dimensions are too small")
    target_aspect = width / float(height)
    source_aspect = source_width / float(source_height)
    if source_aspect > target_aspect:
        crop_width = max(2, int(round(source_height * target_aspect)))
        x0 = (source_width - crop_width) // 2
        image = image[:, x0 : x0 + crop_width]
    elif source_aspect < target_aspect:
        crop_height = max(2, int(round(source_width / target_aspect)))
        y0 = (source_height - crop_height) // 2
        image = image[y0 : y0 + crop_height, :]
    try:
        import cv2
    except ImportError:
        # The deterministic mock remains usable in minimal CI. Real webcam
        # capture already requires OpenCV and therefore uses the filtered path.
        y_indices = np.linspace(0, image.shape[0] - 1, height).astype(np.intp)
        x_indices = np.linspace(0, image.shape[1] - 1, width).astype(np.intp)
        resized = image[y_indices][:, x_indices]
    else:
        resized = cv2.resize(image, (width, height), interpolation=cv2.INTER_AREA)
    return np.ascontiguousarray(resized[..., ::-1])


class DepthAnythingSensorWorker:
    """Convert volatile captures into correlated, RGB-free WorldBus frames."""

    def __init__(
        self,
        backend: Any,
        mapper: FrozenPercentileMapper,
        *,
        profile: WorkerProfile,
        input_size: Optional[int] = None,
        output_width: Optional[int] = None,
        output_height: Optional[int] = None,
        inference_hz: Optional[float] = None,
        depth_scale: float = 0.001,
        horizontal_fov_deg: float = 70.0,
        capture_source: str = "webcam",
        producer_session_id: Optional[str] = None,
    ) -> None:
        self.backend = backend
        self.mapper = mapper
        self.profile = profile
        self.input_size = profile.input_size if input_size is None else input_size
        self.output_width = profile.output_width if output_width is None else output_width
        self.output_height = profile.output_height if output_height is None else output_height
        self.inference_hz = profile.inference_hz if inference_hz is None else inference_hz
        if (
            isinstance(self.input_size, bool)
            or not isinstance(self.input_size, int)
            or not 196 <= self.input_size <= 1024
        ):
            raise WorkerError("input_size must be an integer between 196 and 1024")
        _validate_output_dimensions(self.output_width, self.output_height)
        for label, value, low, high in (
            ("inference_hz", self.inference_hz, 0.1, 60.0),
            ("depth_scale", depth_scale, 1.0e-6, 1.0),
            ("horizontal_fov_deg", horizontal_fov_deg, 1.0, 179.0),
        ):
            if isinstance(value, bool) or not math.isfinite(float(value)) or not low <= float(value) <= high:
                raise WorkerError(f"{label} must be between {low} and {high}")
        self.depth_scale = float(depth_scale)
        self.horizontal_fov_deg = float(horizontal_fov_deg)
        self.capture_source = capture_source
        self.producer_session_id = producer_session_id or (
            "depth-anything-worker-" + uuid.uuid4().hex
        )
        if (
            not isinstance(self.producer_session_id, str)
            or not self.producer_session_id
            or len(self.producer_session_id.encode("utf-8")) > MAX_SESSION_ID_BYTES
            or SESSION_ID_RE.fullmatch(self.producer_session_id) is None
        ):
            raise WorkerError(
                "producer_session_id must use 1..64 safe ASCII letters, digits, '.', '_', or '-'"
            )
        self._next_frame_id = 0
        self.processed_captures = 0
        self.emitted_frames = 0

    def process_capture(self, capture: CapturedFrame) -> Optional[Any]:
        if not isinstance(capture, CapturedFrame):
            raise WorkerError("capture must be CapturedFrame")
        if isinstance(capture.timestamp_ns, bool) or capture.timestamp_ns <= 0:
            raise CaptureError("capture timestamp must be a positive integer")
        rgb = _resize_camera_rgb(
            capture.bgr, width=self.output_width, height=self.output_height
        )
        started = time.perf_counter()
        relative_depth = self.backend.infer(
            rgb,
            input_size=self.input_size,
            output_width=self.output_width,
            output_height=self.output_height,
        )
        inference_ms = (time.perf_counter() - started) * 1000.0
        mapped = self.mapper.observe_and_map(relative_depth)
        self.processed_captures += 1
        if mapped is None:
            return None
        if self.mapper.calibration_id is None or self.mapper.calibration_digest is None:
            raise CalibrationError("depth mapper reported locked without an identity")
        packed = pack_sensor_frame_numpy(
            mapped.depth_metres,
            mapped.foreground_mask,
            mapped.confidence,
            width=self.output_width,
            height=self.output_height,
            depth_scale=self.depth_scale,
            depth_bias=0.0,
        )
        focal = 0.5 * self.output_width / math.tan(
            math.radians(self.horizontal_fov_deg) * 0.5
        )
        intrinsics = (
            focal,
            focal,
            self.output_width * 0.5,
            self.output_height * 0.5,
        )
        generation_id = (
            "depth-anything-sensor-" + self.mapper.calibration_digest[:16]
        )
        metadata = make_sensor_worldbus_metadata(
            packed,
            frame_id=self._next_frame_id,
            capture_timestamp_ns=capture.timestamp_ns,
            intrinsics=intrinsics,
            camera_to_world=IDENTITY_4X4,
            generation_id=generation_id,
            producer_session_id=self.producer_session_id,
            sensor_calibration_id=self.mapper.calibration_id,
            sensor_calibration_digest=self.mapper.calibration_digest,
            model_id=str(self.backend.model_id),
            model_revision=str(self.backend.model_revision),
            calibration_mode=self.mapper.mode,
            raw_order=self.mapper.raw_order,
            raw_percentiles=(
                self.mapper.percentile_low,
                self.mapper.percentile_high,
            ),
            raw_bounds=(self.mapper.raw_low, self.mapper.raw_high),
            pseudo_metre_slab=(
                self.mapper.pseudo_near_m,
                self.mapper.pseudo_far_m,
            ),
            foreground_far_m=self.mapper.foreground_far_m,
            capture_source=self.capture_source,
            inference_ms=inference_ms,
            extra_extensions={
                "depth_anything_profile": self.profile.tier,
                "depth_anything_precision": str(self.backend.precision),
                "depth_anything_input_size": self.input_size,
                "depth_anything_output_width": self.output_width,
                "depth_anything_output_height": self.output_height,
            },
        )
        output = make_frame(metadata, packed.payload)
        self._next_frame_id += 1
        self.emitted_frames += 1
        return output


class SensorWorkerService:
    """Connect result receiver first, then open and drain the selected camera."""

    def __init__(
        self,
        worker: DepthAnythingSensorWorker,
        capture_factory: Callable[[], Any],
        *,
        output_host: str = DEFAULT_OUTPUT_HOST,
        output_tcp_port: int = DEFAULT_OUTPUT_TCP_PORT,
        stale_after_ms: int = 800,
    ) -> None:
        if (
            isinstance(output_tcp_port, bool)
            or not isinstance(output_tcp_port, int)
            or not 1 <= output_tcp_port <= 65535
        ):
            raise WorkerError("output_tcp_port must be between 1 and 65535")
        if (
            isinstance(stale_after_ms, bool)
            or not isinstance(stale_after_ms, int)
            or not 50 <= stale_after_ms <= 60_000
        ):
            raise WorkerError("stale_after_ms must be between 50 and 60000")
        self.worker = worker
        self.capture_factory = capture_factory
        self.output_host = output_host
        self.output_tcp_port = output_tcp_port
        self.stale_after_ns = stale_after_ms * 1_000_000
        self.sender: Optional[TCPFrameSender] = None
        self.capture: Optional[CapturePump] = None
        self.stale_captures = 0
        self.timeout_count = 0

    def start(self) -> "SensorWorkerService":
        if self.sender is not None or self.capture is not None:
            raise WorkerError("sensor service is already started")
        # If TouchDesigner is not receiving, fail before opening the webcam.
        sender = TCPFrameSender(self.output_host, self.output_tcp_port)
        try:
            source = self.capture_factory()
            capture = CapturePump(source).start()
        except Exception:
            sender.close()
            raise
        self.sender = sender
        self.capture = capture
        return self

    def serve(
        self,
        *,
        max_frames: Optional[int] = None,
        duration_s: Optional[float] = None,
    ) -> dict[str, Any]:
        if self.sender is None or self.capture is None:
            raise WorkerError("sensor service must be started before serve")
        if max_frames is not None and (
            isinstance(max_frames, bool)
            or not isinstance(max_frames, int)
            or not 1 <= max_frames <= 1_000_000_000
        ):
            raise WorkerError("max_frames must be between 1 and 1000000000")
        if duration_s is not None and (
            isinstance(duration_s, bool)
            or not math.isfinite(float(duration_s))
            or not 0.01 <= float(duration_s) <= 86_400.0
        ):
            raise WorkerError("duration_s must be between 0.01 and 86400 seconds")
        began = time.monotonic()
        attempts = sent = 0
        interval = 1.0 / self.worker.inference_hz
        next_inference = began
        while max_frames is None or attempts < max_frames:
            if duration_s is not None:
                remaining = float(duration_s) - (time.monotonic() - began)
                if remaining <= 0.0:
                    break
            else:
                remaining = 60.0
            delay = next_inference - time.monotonic()
            if delay > 0.0:
                time.sleep(min(delay, remaining, 0.05))
                continue
            try:
                capture = self.capture.slot.get(min(1.0, max(0.01, remaining)))
            except TimeoutError:
                self.timeout_count += 1
                continue
            age_ns = max(0, time.time_ns() - capture.timestamp_ns)
            attempts += 1
            next_inference = max(next_inference + interval, time.monotonic())
            if age_ns > self.stale_after_ns:
                self.stale_captures += 1
                continue
            output = self.worker.process_capture(capture)
            if output is not None:
                self.sender.send(output)
                sent += 1
        return {
            "status": "ok",
            "inferred_captures": attempts,
            "sent_frames": sent,
            "calibration_locked": self.worker.mapper.locked,
            "calibration_observed_frames": self.worker.mapper.observed_frames,
            "stale_captures": self.stale_captures,
            "capture_timeouts": self.timeout_count,
            "capture_queue": self.capture.slot.stats,
            "backend_load_count": int(self.worker.backend.load_count),
            "backend_inference_count": int(self.worker.backend.inference_count),
            "elapsed_s": time.monotonic() - began,
            "contains_rgb": False,
        }

    def close(self) -> None:
        if self.capture is not None:
            self.capture.close()
            self.capture = None
        if self.sender is not None:
            self.sender.close()
            self.sender = None

    def __enter__(self) -> "SensorWorkerService":
        return self.start()

    def __exit__(self, *_: Any) -> None:
        self.close()


def _loopback_host(value: str) -> bool:
    if value.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _make_backend(args: argparse.Namespace) -> Any:
    if args.backend == "mock":
        return MockDepthBackend()
    model_dir = _safe_runtime_path(args.model_dir or DEFAULT_MODEL_DIR, "model directory")
    cache_dir = _safe_runtime_path(args.cache_dir or DEFAULT_CACHE_DIR, "cache directory")
    return DepthAnythingBackend(
        model_dir=model_dir,
        cache_dir=cache_dir,
        device=args.device,
        warmup=args.warmup,
    )


def _capture_factory(args: argparse.Namespace, capture_name: str) -> Callable[[], Any]:
    if capture_name == "mock":
        return lambda: SyntheticCapture(
            width=args.camera_width, height=args.camera_height
        )
    return lambda: OpenCVCamera(
        index=args.camera_index,
        width=args.camera_width,
        height=args.camera_height,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Default-off RGB-private Depth Anything V2 Small sensor worker"
    )
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("profiles", help="print dependency-free profiles and immutable pins")

    install = actions.add_parser("model-install", help="explicitly download the pinned model")
    install.add_argument("--model-dir")
    install.add_argument("--cache-dir")

    doctor = actions.add_parser("doctor", help="inspect the isolated runtime and model")
    doctor.add_argument("--model-dir")

    serve = actions.add_parser("serve", help="preview or explicitly start webcam inference")
    serve.add_argument("--start", action="store_true")
    serve.add_argument("--profile", choices=sorted(PROFILES), default="3080ti_16gb")
    serve.add_argument("--backend", choices=("depth_anything", "mock"), default="depth_anything")
    serve.add_argument("--capture", choices=("auto", "webcam", "mock"), default="auto")
    serve.add_argument("--model-dir")
    serve.add_argument("--cache-dir")
    serve.add_argument("--device", default="cuda:0")
    serve.add_argument("--warmup", type=int, default=1)
    serve.add_argument("--camera-index", type=int, default=0)
    serve.add_argument("--camera-width", type=int, default=1280)
    serve.add_argument("--camera-height", type=int, default=720)
    serve.add_argument("--input-size", type=int)
    serve.add_argument("--output-width", type=int)
    serve.add_argument("--output-height", type=int)
    serve.add_argument("--inference-hz", type=float)
    serve.add_argument("--horizontal-fov-deg", type=float, default=70.0)
    serve.add_argument("--depth-scale", type=float, default=0.001)
    serve.add_argument("--calibration-mode", choices=("session_frozen", "fixed"), default="session_frozen")
    serve.add_argument("--percentile-low", type=float, default=2.0)
    serve.add_argument("--percentile-high", type=float, default=98.0)
    serve.add_argument("--calibration-frames", type=int, default=12)
    serve.add_argument("--raw-low", type=float)
    serve.add_argument("--raw-high", type=float)
    serve.add_argument("--raw-order", choices=("near_is_larger", "near_is_smaller"), default="near_is_larger")
    serve.add_argument("--pseudo-near-m", type=float, default=0.5)
    serve.add_argument("--pseudo-far-m", type=float, default=4.0)
    serve.add_argument("--foreground-far-m", type=float, default=3.0)
    serve.add_argument("--producer-session-id")
    serve.add_argument("--output-host", default=DEFAULT_OUTPUT_HOST)
    serve.add_argument("--output-tcp-port", type=int, default=DEFAULT_OUTPUT_TCP_PORT)
    serve.add_argument("--allow-trusted-network", action="store_true")
    serve.add_argument("--stale-after-ms", type=int, default=800)
    serve.add_argument("--max-frames", type=int)
    serve.add_argument("--duration-s", type=float)
    return parser


def _serve_preview(args: argparse.Namespace, profile: WorkerProfile) -> dict[str, Any]:
    capture_name = (
        "mock" if args.backend == "mock" else "webcam"
    ) if args.capture == "auto" else args.capture
    return {
        "status": "authorized" if args.start else "preview",
        "profile": profile.tier,
        "backend": args.backend,
        "capture": capture_name,
        "webcam_will_open": bool(args.start and capture_name == "webcam"),
        "camera_index": args.camera_index,
        "input_size": args.input_size or profile.input_size,
        "output_size": [
            profile.output_width if args.output_width is None else args.output_width,
            profile.output_height if args.output_height is None else args.output_height,
        ],
        "output_limits": {
            "max_width": MAX_WIDTH,
            "max_height": MAX_HEIGHT,
            "max_pixels": MAX_PIXELS,
        },
        "inference_hz": args.inference_hz or profile.inference_hz,
        "output_tcp": [args.output_host, args.output_tcp_port],
        "reserved_output_udp_metadata": {
            "host": args.output_host,
            "port": RESERVED_OUTPUT_UDP_PORT,
            "opened": False,
        },
        "calibration_mode": args.calibration_mode,
        "contains_rgb": False,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.action == "profiles":
        _print(
            {
                "status": "ok",
                "profiles": {name: asdict(profile) for name, profile in PROFILES.items()},
                "output_limits": {
                    "max_width": MAX_WIDTH,
                    "max_height": MAX_HEIGHT,
                    "max_pixels": MAX_PIXELS,
                },
                "pins": {
                    "model_id": MODEL_REPOSITORY,
                    "model_revision": MODEL_REVISION,
                    "model_sha256": MODEL_SHA256,
                    "model_license": MODEL_LICENSE,
                },
                "contains_rgb": False,
            }
        )
        return 0
    try:
        if args.action == "model-install":
            model_dir = _safe_runtime_path(args.model_dir or DEFAULT_MODEL_DIR, "model directory")
            cache_dir = _safe_runtime_path(args.cache_dir or DEFAULT_CACHE_DIR, "cache directory")
            _print(install_model(model_dir, cache_dir))
            return 0
        if args.action == "doctor":
            model_dir = _safe_runtime_path(args.model_dir or DEFAULT_MODEL_DIR, "model directory")
            model_error: Optional[str] = None
            try:
                verify_model_directory(model_dir)
            except ModelError as exc:
                model_error = str(exc)
            packages = {
                name: _package_version(name)
                for name in ("torch", "torchvision", "transformers", "numpy", "opencv-python", "safetensors")
            }
            ok = model_error is None and all(packages.values())
            _print(
                {
                    "status": "ok" if ok else "incomplete",
                    "model_error": model_error,
                    "packages": packages,
                    "model_path": str(model_dir),
                    "contains_rgb": False,
                }
            )
            return 0 if ok else 2

        profile = _profile(args.profile)
        _validate_output_dimensions(
            profile.output_width if args.output_width is None else args.output_width,
            profile.output_height if args.output_height is None else args.output_height,
        )
        preview = _serve_preview(args, profile)
        _print(preview)
        if not args.start:
            return 0
        if not _loopback_host(args.output_host) and not args.allow_trusted_network:
            raise WorkerError(
                "non-loopback output requires --allow-trusted-network; WorldBus is not authenticated or encrypted"
            )
        capture_name = str(preview["capture"])
        mapper = FrozenPercentileMapper(
            mode=args.calibration_mode,
            percentile_low=args.percentile_low,
            percentile_high=args.percentile_high,
            calibration_frames=args.calibration_frames,
            raw_low=args.raw_low,
            raw_high=args.raw_high,
            raw_order=args.raw_order,
            pseudo_near_m=args.pseudo_near_m,
            pseudo_far_m=args.pseudo_far_m,
            foreground_far_m=args.foreground_far_m,
        )
        backend = _make_backend(args)
        worker = DepthAnythingSensorWorker(
            backend,
            mapper,
            profile=profile,
            input_size=args.input_size,
            output_width=args.output_width,
            output_height=args.output_height,
            inference_hz=args.inference_hz,
            depth_scale=args.depth_scale,
            horizontal_fov_deg=args.horizontal_fov_deg,
            capture_source="synthetic" if capture_name == "mock" else "webcam",
            producer_session_id=args.producer_session_id,
        )
        service = SensorWorkerService(
            worker,
            _capture_factory(args, capture_name),
            output_host=args.output_host,
            output_tcp_port=args.output_tcp_port,
            stale_after_ms=args.stale_after_ms,
        )
        try:
            service.start()
            _print(
                {
                    "status": "ready",
                    "output_tcp": [args.output_host, args.output_tcp_port],
                    "capture": capture_name,
                    "producer_session_id": worker.producer_session_id,
                    "contains_rgb": False,
                }
            )
            _print(service.serve(max_frames=args.max_frames, duration_s=args.duration_s))
            return 0
        finally:
            service.close()
    except KeyboardInterrupt:
        return 130
    except (WorkerError, WorldBusError, OSError, ValueError) as exc:
        _print({"status": "error", "type": type(exc).__name__, "error": str(exc)})
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
