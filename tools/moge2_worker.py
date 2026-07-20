#!/usr/bin/env python3
"""Pinned, local-only live MoGe-2 worker for WorldBus v1.

The module and ``profiles`` action use only the Python standard library.  The
NumPy, Pillow, PyTorch, and MoGe imports required for frame processing are
lazy.  Real inference accepts only a verified local checkpoint and forces the
Hugging Face runtime offline; this worker never downloads a model.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import re
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from flexgpu.moge2_transport import (  # noqa: E402
    make_moge2_worldbus_metadata,
    pack_moge2_atlas_numpy,
)
from flexgpu.worldbus import (  # noqa: E402
    PRODUCER_SESSION_FIELD,
    QueueClosed,
    TCPFrameSender,
    WorldBusError,
    WorldBusReceiver,
    WorldFrame,
    make_frame,
    validate_frame,
)


MOGE_SOURCE_REPOSITORY = "https://github.com/microsoft/MoGe.git"
MOGE_SOURCE_REVISION = "07444410f1e33f402353b99d6ccd26bd31e469e8"
MODEL_REPOSITORY = "Ruicheng/moge-2-vits-normal"
MODEL_REVISION = "679230677b4d282c6f304189a93e98e14f085902"
MODEL_FILENAME = "model.pt"
MODEL_BYTES = 140_550_416
MODEL_SHA256 = "79a16621928c2bf0ed04659218c55c01075e950507f40bb3332fb4c873d3e1dc"
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
MAX_SESSION_ID_BYTES = 64

RUNTIME_ROOT = (REPOSITORY_ROOT / "runtime").resolve()
DEFAULT_MODEL_PATH = RUNTIME_ROOT / "moge2-model" / MODEL_FILENAME
DEFAULT_CACHE_PATH = RUNTIME_ROOT / "moge2-cache"
DEFAULT_DEPTH_ANYTHING_MODEL_DIR = RUNTIME_ROOT / "depth-anything-v2-small"
DEFAULT_DEPTH_ANYTHING_CACHE_DIR = RUNTIME_ROOT / "depth-anything-cache"
GEOMETRY_PROVIDERS = frozenset(("moge2", "depth_anything"))


class WorkerError(RuntimeError):
    """A bounded worker operation failed."""


class DependencyError(WorkerError):
    """The isolated MoGe runtime is incomplete."""


class ModelError(WorkerError):
    """The pinned local checkpoint is missing or invalid."""


class CalibrationPending(WorkerError):
    """A relative-depth provider is still freezing its session calibration."""


@dataclass(frozen=True)
class WorkerProfile:
    tier: str
    model_id: str
    precision: str
    num_tokens: int
    max_edge: int
    note: str


PROFILES: dict[str, WorkerProfile] = {
    "3080ti_16gb": WorkerProfile(
        "3080ti_16gb",
        MODEL_REPOSITORY,
        "fp16",
        1200,
        384,
        "Initial RTX 3080 Ti Laptop profile; benchmark before live use.",
    ),
    "4090": WorkerProfile(
        "4090",
        MODEL_REPOSITORY,
        "fp16",
        1800,
        512,
        "Conservative 4090 profile using the same verified ViT-S checkpoint.",
    ),
    "5090": WorkerProfile(
        "5090",
        MODEL_REPOSITORY,
        "fp16",
        2500,
        512,
        "Conservative 5090 profile; a larger model remains an explicit later A/B test.",
    ),
}


@dataclass(frozen=True)
class BackendOutput:
    depth: Any
    mask: Any
    intrinsics: Any


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, allow_nan=False), flush=True)


def _profile(name: str) -> WorkerProfile:
    try:
        return PROFILES[name]
    except KeyError as exc:
        raise WorkerError("unknown MoGe-2 worker profile: " + name) from exc


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


def verify_model(path: Path) -> None:
    if not path.is_file():
        raise ModelError("pinned MoGe-2 checkpoint is not installed")
    if path.stat().st_size != MODEL_BYTES:
        raise ModelError("pinned MoGe-2 checkpoint has the wrong byte length")
    if _sha256_file(path) != MODEL_SHA256:
        raise ModelError("pinned MoGe-2 checkpoint failed SHA-256 verification")


def _configure_offline_cache(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.environ["HF_HOME"] = str(path)
    os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ["HF_HUB_OFFLINE"] = "1"


def _port(value: Any, label: str, *, allow_zero: bool) -> int:
    minimum = 0 if allow_zero else 1
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= 65535
    ):
        raise WorkerError(f"{label} must be an integer between {minimum} and 65535")
    return value


def _import_arrays() -> tuple[Any, Any]:
    try:
        import numpy as np
        from PIL import Image
    except ImportError as exc:
        raise DependencyError("NumPy and Pillow are required for live frame processing") from exc
    return np, Image


class MockBackend:
    """Deterministic CPU geometry used only for transport and CI tests."""

    model_id = "flexgpu/mock-plane"
    model_revision = "0" * 40
    precision = "mock"
    geometry_provider = "moge2"
    model_source_revision = MOGE_SOURCE_REVISION

    def __init__(self) -> None:
        self.load_count = 1
        self.inference_count = 0
        self.requested_fov_x: list[Optional[float]] = []

    def infer(
        self,
        rgb: Any,
        *,
        num_tokens: int,
        fov_x_deg: Optional[float],
    ) -> BackendOutput:
        del num_tokens
        try:
            import numpy as np
        except ImportError as exc:
            raise DependencyError("NumPy is required for the mock backend") from exc
        height, width = rgb.shape[:2]
        self.inference_count += 1
        self.requested_fov_x.append(fov_x_deg)
        fx = (
            0.85
            if fov_x_deg is None
            else 0.5 / math.tan(math.radians(fov_x_deg) * 0.5)
        )
        fy = fx * width / height
        u = (np.arange(width, dtype=np.float32) + 0.5) / width
        v = (np.arange(height, dtype=np.float32) + 0.5) / height
        uu, vv = np.meshgrid(u, v)
        luminance = rgb.astype(np.float32).mean(axis=-1) / 255.0
        depth = (
            1.25
            + 0.20 * ((uu - 0.5) ** 2 + (vv - 0.5) ** 2)
            + luminance * 0.01
        ).astype(np.float32)
        mask = np.ones((height, width), dtype=bool)
        intrinsics = np.array(
            [[fx, 0.0, 0.5], [0.0, fy, 0.5], [0.0, 0.0, 1.0]],
            dtype=np.float32,
        )
        return BackendOutput(depth=depth, mask=mask, intrinsics=intrinsics)


class MoGe2Backend:
    """One persistent pinned MoGe-2 model on an explicitly selected GPU."""

    model_id = MODEL_REPOSITORY
    model_revision = MODEL_REVISION
    precision = "fp16"
    geometry_provider = "moge2"
    model_source_revision = MOGE_SOURCE_REVISION

    def __init__(
        self,
        *,
        model_path: Path,
        cache_path: Path,
        device: str,
        warmup: int = 1,
    ) -> None:
        if not isinstance(warmup, int) or isinstance(warmup, bool) or not 0 <= warmup <= 20:
            raise WorkerError("warmup must be an integer between 0 and 20")
        verify_model(model_path)
        _configure_offline_cache(cache_path)
        try:
            import numpy as np
            import torch
            from moge.model.v2 import MoGeModel
        except ImportError as exc:
            raise DependencyError(
                "PyTorch, NumPy, and the pinned MoGe source are required"
            ) from exc
        if not isinstance(device, str) or not device.startswith("cuda"):
            raise DependencyError("the live MoGe-2 backend requires a CUDA device")
        if not torch.cuda.is_available():
            raise DependencyError("CUDA is not available to the MoGe-2 worker")
        try:
            selected = torch.device(device)
            torch.cuda.set_device(selected)
        except (RuntimeError, ValueError) as exc:
            raise DependencyError("requested CUDA device is unavailable") from exc
        self.np = np
        self.torch = torch
        self.device = selected
        self.warmup = warmup
        self._warmed = False
        self.model = MoGeModel.from_pretrained(str(model_path)).to(selected).eval().half()
        self.load_count = 1
        self.inference_count = 0

    def infer(
        self,
        rgb: Any,
        *,
        num_tokens: int,
        fov_x_deg: Optional[float],
    ) -> BackendOutput:
        image = self.torch.from_numpy(self.np.ascontiguousarray(rgb)).to(
            device=self.device, dtype=self.torch.float32
        ).permute(2, 0, 1) / 255.0

        def invoke() -> Mapping[str, Any]:
            return self.model.infer(
                image,
                num_tokens=num_tokens,
                use_fp16=True,
                force_projection=True,
                apply_mask=True,
                fov_x=fov_x_deg,
            )

        if not self._warmed:
            for _ in range(self.warmup):
                invoke()
            self.torch.cuda.synchronize(self.device)
            self._warmed = True
        output = invoke()
        self.torch.cuda.synchronize(self.device)
        self.inference_count += 1
        required = {"depth", "mask", "intrinsics"}
        missing = sorted(required.difference(output))
        if missing:
            raise WorkerError("MoGe-2 output is missing: " + ", ".join(missing))
        converted = {
            key: output[key].detach().float().cpu().numpy() for key in required
        }
        return BackendOutput(
            depth=converted["depth"],
            mask=converted["mask"],
            intrinsics=converted["intrinsics"],
        )


def _load_depth_anything_module() -> Any:
    """Load the pinned sensor module lazily without importing its camera path."""

    module_name = "flexgpu_depth_anything_geometry_backend"
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    path = REPOSITORY_ROOT / "tools" / "depth_anything_worker.py"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise DependencyError("Depth Anything worker module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


class DepthAnythingGeometryBackend:
    """Persistent generated-image relative depth mapped to a stable metre slab."""

    geometry_provider = "depth_anything"
    precision = "fp16"

    def __init__(
        self,
        *,
        model_dir: Path,
        cache_dir: Path,
        device: str,
        warmup: int = 1,
        input_size: int = 384,
        calibration_frames: int = 12,
        percentile_low: float = 2.0,
        percentile_high: float = 98.0,
        raw_order: str = "near_is_larger",
        pseudo_near_m: float = 0.5,
        pseudo_far_m: float = 4.0,
        foreground_far_m: float = 4.0,
        default_fov_x_deg: float = 60.0,
    ) -> None:
        if (
            isinstance(input_size, bool)
            or not isinstance(input_size, int)
            or not 196 <= input_size <= 1024
        ):
            raise WorkerError("Depth Anything input_size must be between 196 and 1024")
        if (
            isinstance(default_fov_x_deg, bool)
            or not math.isfinite(float(default_fov_x_deg))
            or not 1.0 <= float(default_fov_x_deg) < 179.0
        ):
            raise WorkerError("Depth Anything default field of view is invalid")
        module = _load_depth_anything_module()
        self._module = module
        self.model_id = str(module.MODEL_REPOSITORY)
        self.model_revision = str(module.MODEL_REVISION)
        self.model_source_revision = str(module.MODEL_REVISION)
        self.input_size = input_size
        self.default_fov_x_deg = float(default_fov_x_deg)
        self._mapper_arguments = {
            "mode": "session_frozen",
            "percentile_low": percentile_low,
            "percentile_high": percentile_high,
            "calibration_frames": calibration_frames,
            "raw_order": raw_order,
            "pseudo_near_m": pseudo_near_m,
            "pseudo_far_m": pseudo_far_m,
            "foreground_far_m": foreground_far_m,
        }
        self._backend = module.DepthAnythingBackend(
            model_dir=model_dir,
            cache_dir=cache_dir,
            device=device,
            warmup=warmup,
        )
        self._mapper = module.FrozenPercentileMapper(**self._mapper_arguments)

    @property
    def load_count(self) -> int:
        return int(self._backend.load_count)

    @property
    def inference_count(self) -> int:
        return int(self._backend.inference_count)

    @property
    def calibration_locked(self) -> bool:
        return bool(self._mapper.locked)

    @property
    def calibration_observed_frames(self) -> int:
        return int(self._mapper.observed_frames)

    def begin_source_session(self) -> None:
        self._mapper = self._module.FrozenPercentileMapper(**self._mapper_arguments)

    def infer(
        self,
        rgb: Any,
        *,
        num_tokens: int,
        fov_x_deg: Optional[float],
    ) -> BackendOutput:
        del num_tokens
        height, width = rgb.shape[:2]
        relative_depth = self._backend.infer(
            rgb,
            input_size=self.input_size,
            output_width=width,
            output_height=height,
        )
        mapped = self._mapper.observe_and_map(relative_depth)
        if mapped is None:
            raise CalibrationPending(
                "Depth Anything session calibration is still collecting frames"
            )
        selected_fov = (
            self.default_fov_x_deg if fov_x_deg is None else float(fov_x_deg)
        )
        fx = 0.5 / math.tan(math.radians(selected_fov) * 0.5)
        fy = fx * width / height
        try:
            import numpy as np
        except ImportError as exc:
            raise DependencyError("NumPy is required for Depth Anything geometry") from exc
        intrinsics = np.asarray(
            [[fx, 0.0, 0.5], [0.0, fy, 0.5], [0.0, 0.0, 1.0]],
            dtype=np.float32,
        )
        return BackendOutput(
            depth=mapped.depth_metres,
            mask=mapped.foreground_mask,
            intrinsics=intrinsics,
        )


def _resize_rgb(frame: WorldFrame, max_edge: int) -> tuple[Any, bytes]:
    if frame.metadata.pixel_format != "rgba8":
        raise WorkerError("MoGe-2 input frames must use WorldBus pixel_format rgba8")
    np, Image = _import_arrays()
    width, height = frame.metadata.width, frame.metadata.height
    rgba = np.frombuffer(frame.payload, dtype=np.uint8).reshape(height, width, 4)
    rgb = np.ascontiguousarray(rgba[..., :3])
    if max(width, height) > max_edge:
        scale = max_edge / max(width, height)
        resized = (
            max(1, int(round(width * scale))),
            max(1, int(round(height * scale))),
        )
        resampling = getattr(Image, "Resampling", Image).LANCZOS
        rgb = np.asarray(
            Image.fromarray(rgb, mode="RGB").resize(resized, resampling),
            dtype=np.uint8,
        ).copy()
    output_rgba = np.concatenate(
        (
            rgb,
            np.full((rgb.shape[0], rgb.shape[1], 1), 255, dtype=np.uint8),
        ),
        axis=-1,
    )
    return rgb, output_rgba.tobytes(order="C")


def _sanitize_output(
    output: BackendOutput, *, width: int, height: int
) -> tuple[Any, Any, tuple[float, float, float, float]]:
    try:
        import numpy as np
    except ImportError as exc:
        raise DependencyError("NumPy is required for MoGe-2 output validation") from exc
    depth = np.asarray(output.depth, dtype=np.float32)
    raw_mask = np.asarray(output.mask)
    intrinsics = np.asarray(output.intrinsics, dtype=np.float32)
    if depth.shape != (height, width) or raw_mask.shape != (height, width):
        raise WorkerError("MoGe-2 depth/mask dimensions do not match the inference RGB")
    if intrinsics.shape != (3, 3) or not np.isfinite(intrinsics).all():
        raise WorkerError("MoGe-2 intrinsics are missing, non-finite, or malformed")
    if raw_mask.dtype == np.bool_:
        valid_mask = raw_mask.copy()
    else:
        numeric_mask = np.asarray(raw_mask, dtype=np.float32)
        valid_mask = np.isfinite(numeric_mask) & (numeric_mask > 0.5)
    valid_mask &= np.isfinite(depth) & (depth > 0.0)
    if not valid_mask.any():
        raise WorkerError("MoGe-2 returned no finite positive geometry")
    depth = np.where(valid_mask, depth, 0.0).astype(np.float32)
    fx, fy = float(intrinsics[0, 0]), float(intrinsics[1, 1])
    cx, cy = float(intrinsics[0, 2]), float(intrinsics[1, 2])
    if fx <= 0.0 or fy <= 0.0 or not 0.0 <= cx <= 1.0 or not 0.0 <= cy <= 1.0:
        raise WorkerError("MoGe-2 returned invalid normalized intrinsics")
    return depth, valid_mask, (fx, fy, cx, cy)


class MoGe2Worker:
    """Transform validated rgba8 input frames into synchronized MoGe atlases."""

    def __init__(
        self,
        backend: Any,
        *,
        profile: WorkerProfile,
        provider: Optional[str] = None,
        num_tokens: Optional[int] = None,
        max_edge: Optional[int] = None,
        depth_scale: float = 0.001,
        producer_session_id: Optional[str] = None,
    ) -> None:
        self.backend = backend
        self.profile = profile
        self.provider = (
            str(getattr(backend, "geometry_provider", "moge2"))
            if provider is None
            else provider
        )
        if self.provider not in GEOMETRY_PROVIDERS:
            raise WorkerError("geometry provider is unsupported")
        backend_provider = str(getattr(backend, "geometry_provider", self.provider))
        if backend_provider != self.provider and not (
            isinstance(backend, MockBackend) and backend_provider == "moge2"
        ):
            raise WorkerError("backend and requested geometry provider disagree")
        self.num_tokens = profile.num_tokens if num_tokens is None else num_tokens
        self.max_edge = profile.max_edge if max_edge is None else max_edge
        if (
            isinstance(self.num_tokens, bool)
            or not isinstance(self.num_tokens, int)
            or not 1200 <= self.num_tokens <= 3600
        ):
            raise WorkerError("num_tokens must be an integer between 1200 and 3600")
        if (
            isinstance(self.max_edge, bool)
            or not isinstance(self.max_edge, int)
            or not 64 <= self.max_edge <= 2048
        ):
            raise WorkerError("max_edge must be an integer between 64 and 2048")
        try:
            self.depth_scale = float(depth_scale)
        except (TypeError, ValueError, OverflowError) as exc:
            raise WorkerError("depth_scale must be a finite positive number") from exc
        if not math.isfinite(self.depth_scale) or self.depth_scale <= 0.0:
            raise WorkerError("depth_scale must be a finite positive number")
        self.producer_session_id = (
            self.provider.replace("_", "-") + "-worker-" + uuid.uuid4().hex
            if producer_session_id is None
            else producer_session_id
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
        self._base_producer_session_id = self.producer_session_id
        self._session_rollovers = 0
        self._next_frame_id = 0
        self._active_source_key: Optional[
            tuple[str, str, int, int, int, int]
        ] = None
        self._fov_key: Optional[tuple[str, str, int, int, int, int]] = None
        self._fov_x_deg: Optional[float] = None
        self._locked_intrinsics: Optional[
            tuple[float, float, float, float]
        ] = None
        self.processed_frames = 0
        self.fov_resets = 0

    @property
    def session_rollovers(self) -> int:
        return self._session_rollovers

    def _roll_output_session(self) -> None:
        self._session_rollovers += 1
        suffix = f"-r{self._session_rollovers}"
        maximum_base = MAX_SESSION_ID_BYTES - len(suffix)
        if maximum_base < 1:
            raise WorkerError("producer session rollover counter exceeded safe bounds")
        self.producer_session_id = self._base_producer_session_id[:maximum_base] + suffix
        self._next_frame_id = 0

    def process_frame(self, source: WorldFrame) -> WorldFrame:
        if not isinstance(source, WorldFrame):
            raise WorkerError("source must be a validated WorldBus frame")
        source = validate_frame(source)
        rgb, source_rgba = _resize_rgb(source, self.max_edge)
        height, width = rgb.shape[:2]
        raw_source_session = source.metadata.extensions.get(PRODUCER_SESSION_FIELD)
        source_session = (
            "" if raw_source_session is None else str(raw_source_session)
        )
        source_provider = str(
            source.metadata.extensions.get("geometry_provider", "moge2")
        )
        if source_provider != self.provider:
            raise WorkerError(
                "source geometry provider does not match this worker"
            )
        fov_key = (
            source_session,
            source.metadata.generation_id,
            source.metadata.width,
            source.metadata.height,
            width,
            height,
        )
        if fov_key != self._active_source_key:
            if self._active_source_key is not None:
                self._roll_output_session()
            self._active_source_key = fov_key
            begin_source_session = getattr(
                self.backend, "begin_source_session", None
            )
            if callable(begin_source_session):
                begin_source_session()
            self._fov_key = None
            self._fov_x_deg = None
            self._locked_intrinsics = None
        if fov_key == self._fov_key:
            locked_fov = self._fov_x_deg
        else:
            locked_fov = None
        started = time.perf_counter()
        raw = self.backend.infer(
            rgb,
            num_tokens=self.num_tokens,
            fov_x_deg=locked_fov,
        )
        inference_ms = (time.perf_counter() - started) * 1000.0
        depth, mask, normalized = _sanitize_output(raw, width=width, height=height)
        if locked_fov is None:
            fov_x_deg = math.degrees(2.0 * math.atan(0.5 / normalized[0]))
            if not math.isfinite(fov_x_deg) or not 1.0 <= fov_x_deg < 179.0:
                raise WorkerError("inferred horizontal field of view is outside safe bounds")
            self._fov_key = fov_key
            self._fov_x_deg = fov_x_deg
            self._locked_intrinsics = normalized
            self.fov_resets += 1
            fov_locked = False
            output_intrinsics = normalized
        else:
            fov_x_deg = locked_fov
            expected_fx = 0.5 / math.tan(math.radians(fov_x_deg) * 0.5)
            if not math.isclose(normalized[0], expected_fx, rel_tol=1.0e-4, abs_tol=1.0e-5):
                raise WorkerError("MoGe-2 did not honor the locked horizontal field of view")
            if self._locked_intrinsics is None or any(
                not math.isclose(actual, expected, rel_tol=1.0e-4, abs_tol=1.0e-5)
                for actual, expected in zip(normalized, self._locked_intrinsics)
            ):
                raise WorkerError("MoGe-2 intrinsics drifted inside a locked source session")
            fov_locked = True
            # Publish the first frame's exact calibration throughout this
            # output producer session so TouchDesigner never recompiles or
            # rejects a harmless float-rounding change.
            output_intrinsics = self._locked_intrinsics

        atlas = pack_moge2_atlas_numpy(
            source_rgba,
            depth,
            mask,
            source_width=width,
            height=height,
            depth_scale=self.depth_scale,
            depth_bias=0.0,
        )
        intrinsics_pixels = (
            output_intrinsics[0] * width,
            output_intrinsics[1] * height,
            output_intrinsics[2] * width,
            output_intrinsics[3] * height,
        )
        safe_extensions = {
            "geometry_provider": self.provider,
            "moge2_profile": self.profile.tier,
            "moge2_precision": str(self.backend.precision),
            "moge2_num_tokens": self.num_tokens,
            "moge2_inference_ms": inference_ms,
            "moge2_source_width": source.metadata.width,
            "moge2_source_height": source.metadata.height,
            "moge2_source_pixel_format": "rgba8",
            "moge2_inference_width": width,
            "moge2_inference_height": height,
            "fov_x_deg": fov_x_deg,
            "fov_locked": fov_locked,
        }
        metadata = make_moge2_worldbus_metadata(
            atlas,
            frame_id=self._next_frame_id,
            timestamp_ns=time.time_ns(),
            intrinsics=intrinsics_pixels,
            camera_to_world=source.metadata.camera_to_world,
            generation_id=source.metadata.generation_id,
            producer_session_id=self.producer_session_id,
            source_frame_id=source.metadata.frame_id,
            source_timestamp_ns=source.metadata.timestamp_ns,
            source_producer_session_id=source_session or None,
            model_id=str(self.backend.model_id),
            model_source_revision=str(
                getattr(self.backend, "model_source_revision", MOGE_SOURCE_REVISION)
            ),
            model_revision=str(self.backend.model_revision),
            extra_extensions=safe_extensions,
        )
        frame = make_frame(metadata, atlas.payload)
        self._next_frame_id += 1
        self.processed_frames += 1
        return frame


class MoGe2WorkerService:
    """Bounded WorldBus input receiver and persistent output TCP connection."""

    def __init__(
        self,
        worker: MoGe2Worker,
        *,
        input_host: str,
        input_tcp_port: int,
        input_udp_port: int,
        output_host: str,
        output_tcp_port: int,
        output_connect_timeout_s: float = 120.0,
        output_connect_retry_s: float = 0.25,
    ) -> None:
        if not isinstance(input_host, str) or not input_host:
            raise WorkerError("input_host must be a non-empty string")
        if not isinstance(output_host, str) or not output_host:
            raise WorkerError("output_host must be a non-empty string")
        input_tcp_port = _port(input_tcp_port, "input_tcp_port", allow_zero=True)
        input_udp_port = _port(input_udp_port, "input_udp_port", allow_zero=True)
        output_tcp_port = _port(output_tcp_port, "output_tcp_port", allow_zero=False)
        try:
            output_connect_timeout_s = float(output_connect_timeout_s)
            output_connect_retry_s = float(output_connect_retry_s)
        except (TypeError, ValueError, OverflowError) as exc:
            raise WorkerError("output connection timing must be numeric") from exc
        if (not math.isfinite(output_connect_timeout_s) or
                not 0.0 <= output_connect_timeout_s <= 300.0):
            raise WorkerError(
                "output_connect_timeout_s must be between 0 and 300 seconds")
        if (not math.isfinite(output_connect_retry_s) or
                not 0.01 <= output_connect_retry_s <= 5.0):
            raise WorkerError(
                "output_connect_retry_s must be between 0.01 and 5 seconds")
        self.worker = worker
        self.receiver = WorldBusReceiver(
            host=input_host,
            tcp_port=input_tcp_port,
            udp_port=input_udp_port,
        )
        self.output_host = output_host
        self.output_tcp_port = output_tcp_port
        self.output_connect_timeout_s = output_connect_timeout_s
        self.output_connect_retry_s = output_connect_retry_s
        self.sender: Optional[TCPFrameSender] = None
        self.failures = 0
        self.skipped = 0
        self.errors: list[str] = []

    def start(self) -> "MoGe2WorkerService":
        if self.sender is not None:
            raise WorkerError("MoGe-2 worker service is already started")
        self.receiver.start()
        deadline = time.monotonic() + self.output_connect_timeout_s
        try:
            while self.sender is None:
                try:
                    self.sender = TCPFrameSender(
                        self.output_host, self.output_tcp_port)
                except OSError as exc:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0.0:
                        raise WorkerError(
                            "TouchDesigner result receiver %s:%d did not become "
                            "ready within %.1f seconds; select the matching "
                            "geometry provider and enable its bridge" % (
                                self.output_host, self.output_tcp_port,
                                self.output_connect_timeout_s)) from exc
                    time.sleep(min(self.output_connect_retry_s, remaining))
        except Exception:
            self.receiver.close()
            raise
        return self

    @property
    def input_tcp_address(self) -> tuple[str, int]:
        return self.receiver.tcp_address

    @property
    def input_udp_address(self) -> tuple[str, int]:
        return self.receiver.udp_address

    def serve(
        self,
        *,
        max_frames: Optional[int] = None,
        duration_s: Optional[float] = None,
    ) -> dict[str, Any]:
        if self.sender is None:
            raise WorkerError("MoGe-2 worker service must be started before serve")
        if max_frames is not None and (
            isinstance(max_frames, bool)
            or not isinstance(max_frames, int)
            or not 1 <= max_frames <= 1_000_000_000
        ):
            raise WorkerError("max_frames must be an integer between 1 and 1000000000")
        if duration_s is not None:
            try:
                duration_s = float(duration_s)
            except (TypeError, ValueError, OverflowError) as exc:
                raise WorkerError("duration_s must be a finite positive number") from exc
            if not math.isfinite(duration_s) or not 0.01 <= duration_s <= 86_400.0:
                raise WorkerError("duration_s must be between 0.01 and 86400 seconds")
        began = time.monotonic()
        sent = 0
        received = 0
        while max_frames is None or received < max_frames:
            if duration_s is not None:
                remaining = duration_s - (time.monotonic() - began)
                if remaining <= 0.0:
                    break
                wait_s = min(0.1, remaining)
            else:
                wait_s = 0.1
            try:
                source = self.receiver.frames.get(timeout=wait_s)
            except TimeoutError:
                continue
            except QueueClosed:
                break
            received += 1
            try:
                output = self.worker.process_frame(source)
            except CalibrationPending:
                self.skipped += 1
                continue
            except (WorkerError, WorldBusError, ValueError) as exc:
                self.failures += 1
                self.errors.append(type(exc).__name__ + ": " + str(exc))
                self.errors = self.errors[-64:]
                continue
            self.sender.send(output)
            sent += 1
        return {
            "status": "ok",
            "received_frames": received,
            "sent_frames": sent,
            "failed_frames": self.failures,
            "skipped_frames": self.skipped,
            "elapsed_s": time.monotonic() - began,
            "input_queue": self.receiver.frames.stats,
            "input_errors": self.receiver.errors,
            "worker_errors": list(self.errors),
            "backend_load_count": int(self.worker.backend.load_count),
            "backend_inference_count": int(self.worker.backend.inference_count),
            "fov_resets": self.worker.fov_resets,
            "session_rollovers": self.worker.session_rollovers,
        }

    def close(self) -> None:
        if self.sender is not None:
            self.sender.close()
            self.sender = None
        self.receiver.close()

    def __enter__(self) -> "MoGe2WorkerService":
        return self.start()

    def __exit__(self, *_: Any) -> None:
        self.close()


def _make_backend(args: argparse.Namespace) -> Any:
    if args.backend == "mock":
        return MockBackend()
    if args.backend == "depth_anything":
        model_dir = _safe_runtime_path(
            args.model_dir or DEFAULT_DEPTH_ANYTHING_MODEL_DIR,
            "Depth Anything model directory",
        )
        cache_dir = _safe_runtime_path(
            args.cache_dir or DEFAULT_DEPTH_ANYTHING_CACHE_DIR,
            "Depth Anything cache directory",
        )
        return DepthAnythingGeometryBackend(
            model_dir=model_dir,
            cache_dir=cache_dir,
            device=args.device,
            warmup=args.warmup,
            input_size=args.input_size,
            calibration_frames=args.calibration_frames,
            percentile_low=args.percentile_low,
            percentile_high=args.percentile_high,
            raw_order=args.raw_order,
            pseudo_near_m=args.pseudo_near_m,
            pseudo_far_m=args.pseudo_far_m,
            foreground_far_m=args.foreground_far_m,
            default_fov_x_deg=args.horizontal_fov_deg,
        )
    model_path = _safe_runtime_path(
        args.model_path or DEFAULT_MODEL_PATH,
        "model path",
    )
    cache_path = _safe_runtime_path(
        args.cache_dir or DEFAULT_CACHE_PATH,
        "cache path",
    )
    return MoGe2Backend(
        model_path=model_path,
        cache_path=cache_path,
        device=args.device,
        warmup=args.warmup,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Pinned local-only live MoGe-2 WorldBus worker"
    )
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("profiles", help="print dependency-free GPU profiles and pins")

    serve = actions.add_parser("serve", help="bind WorldBus input and stream MoGe atlases")
    serve.add_argument("--profile", choices=sorted(PROFILES), default="3080ti_16gb")
    serve.add_argument(
        "--backend", choices=("moge2", "depth_anything", "mock"), default="moge2"
    )
    serve.add_argument("--provider", choices=sorted(GEOMETRY_PROVIDERS))
    serve.add_argument("--model-path")
    serve.add_argument("--model-dir")
    serve.add_argument("--cache-dir")
    serve.add_argument("--device", default="cuda:0")
    serve.add_argument("--warmup", type=int, default=1)
    serve.add_argument("--num-tokens", type=int)
    serve.add_argument("--max-edge", type=int)
    serve.add_argument("--depth-scale", type=float, default=0.001)
    serve.add_argument("--input-size", type=int, default=384)
    serve.add_argument("--calibration-frames", type=int, default=12)
    serve.add_argument("--percentile-low", type=float, default=2.0)
    serve.add_argument("--percentile-high", type=float, default=98.0)
    serve.add_argument(
        "--raw-order",
        choices=("near_is_larger", "near_is_smaller"),
        default="near_is_larger",
    )
    serve.add_argument("--pseudo-near-m", type=float, default=0.5)
    serve.add_argument("--pseudo-far-m", type=float, default=4.0)
    serve.add_argument("--foreground-far-m", type=float, default=4.0)
    serve.add_argument("--horizontal-fov-deg", type=float, default=60.0)
    serve.add_argument("--producer-session-id")
    serve.add_argument("--input-host", default="127.0.0.1")
    serve.add_argument("--input-tcp-port", type=int, default=9211)
    serve.add_argument("--input-udp-port", type=int, default=9210)
    serve.add_argument("--output-host", default="127.0.0.1")
    serve.add_argument("--output-tcp-port", type=int, required=True)
    serve.add_argument("--output-connect-timeout-s", type=float, default=120.0)
    serve.add_argument("--output-connect-retry-s", type=float, default=0.25)
    serve.add_argument("--max-frames", type=int)
    serve.add_argument("--duration-s", type=float)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.action == "profiles":
        _print(
            {
                "status": "ok",
                "profiles": {name: asdict(profile) for name, profile in PROFILES.items()},
                "pins": {
                    "moge_source_revision": MOGE_SOURCE_REVISION,
                    "model_revision": MODEL_REVISION,
                    "model_sha256": MODEL_SHA256,
                },
            }
        )
        return 0

    service: Optional[MoGe2WorkerService] = None
    try:
        profile = _profile(args.profile)
        backend = _make_backend(args)
        provider = args.provider or (
            "depth_anything" if args.backend == "depth_anything" else "moge2"
        )
        worker = MoGe2Worker(
            backend,
            profile=profile,
            provider=provider,
            num_tokens=args.num_tokens,
            max_edge=args.max_edge,
            depth_scale=args.depth_scale,
            producer_session_id=args.producer_session_id,
        )
        service = MoGe2WorkerService(
            worker,
            input_host=args.input_host,
            input_tcp_port=args.input_tcp_port,
            input_udp_port=args.input_udp_port,
            output_host=args.output_host,
            output_tcp_port=args.output_tcp_port,
            output_connect_timeout_s=args.output_connect_timeout_s,
            output_connect_retry_s=args.output_connect_retry_s,
        ).start()
        _print(
            {
                "status": "ready",
                "input_tcp": service.input_tcp_address,
                "input_udp": service.input_udp_address,
                "output_tcp": (args.output_host, args.output_tcp_port),
                "profile": profile.tier,
                "backend": args.backend,
                "geometry_provider": worker.provider,
                "producer_session_id": worker.producer_session_id,
            }
        )
        _print(
            service.serve(max_frames=args.max_frames, duration_s=args.duration_s)
        )
        return 0
    except KeyboardInterrupt:
        return 130
    except (WorkerError, WorldBusError, OSError, ValueError) as exc:
        _print({"status": "error", "type": type(exc).__name__, "error": str(exc)})
        return 3
    finally:
        if service is not None:
            service.close()


if __name__ == "__main__":
    raise SystemExit(main())
