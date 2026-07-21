#!/usr/bin/env python3
"""Pinned, local-only MoGe-2 offline probe for FlexGPU.

The tool never downloads a model during inference.  ``model-install`` is the
only networked action; it pins and verifies the official checkpoint before
placing it beneath the ignored runtime directory.  Heavy imports are lazy so
``profiles`` and source tests remain dependency-free.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from flexgpu.geometry_frame import (  # noqa: E402
    CONTRACT,
    COORDINATE_SYSTEM,
    SOURCE_COORDINATE_SYSTEM,
    validate_geometry_manifest,
    verify_geometry_bundle,
)


MOGE_SOURCE_REPOSITORY = "https://github.com/microsoft/MoGe.git"
MOGE_SOURCE_REVISION = "07444410f1e33f402353b99d6ccd26bd31e469e8"
MODEL_REPOSITORY = "Ruicheng/moge-2-vits-normal"
MODEL_REVISION = "679230677b4d282c6f304189a93e98e14f085902"
MODEL_FILENAME = "model.pt"
MODEL_BYTES = 140_550_416
MODEL_SHA256 = "79a16621928c2bf0ed04659218c55c01075e950507f40bb3332fb4c873d3e1dc"
MAX_INPUT_BYTES = 64 * 1024 * 1024
RUNTIME_ROOT = (REPOSITORY_ROOT / "runtime").resolve()
DEFAULT_MODEL_PATH = RUNTIME_ROOT / "moge2-model" / MODEL_FILENAME
DEFAULT_CACHE_PATH = RUNTIME_ROOT / "moge2-cache"
DEFAULT_RUNS_PATH = RUNTIME_ROOT / "moge2-runs"


class ProbeError(RuntimeError):
    """A bounded probe operation failed."""


class DependencyError(ProbeError):
    """The isolated model environment is incomplete."""


class ModelError(ProbeError):
    """The pinned model is missing or failed integrity verification."""


@dataclass(frozen=True)
class ProbeProfile:
    tier: str
    model_id: str
    precision: str
    num_tokens: int
    max_edge: int
    note: str


# These are conservative starting profiles, not performance guarantees.  One
# verified checkpoint is shared across tiers until local A/B evidence justifies
# pinning larger weights.
PROFILES: dict[str, ProbeProfile] = {
    "3080ti_16gb": ProbeProfile(
        "3080ti_16gb",
        MODEL_REPOSITORY,
        "fp16",
        1200,
        384,
        "Initial RTX 3080 Ti Laptop profile; benchmark before live use.",
    ),
    "4090": ProbeProfile(
        "4090",
        MODEL_REPOSITORY,
        "fp16",
        1800,
        512,
        "Conservative 4090 profile using the same verified ViT-S checkpoint.",
    ),
    "5090": ProbeProfile(
        "5090",
        MODEL_REPOSITORY,
        "fp16",
        2500,
        512,
        "Conservative 5090 profile; a larger model remains an explicit later A/B test.",
    ),
}


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, allow_nan=False))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_runtime_path(value: str | os.PathLike[str], label: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = REPOSITORY_ROOT / path
    resolved = path.resolve()
    try:
        resolved.relative_to(RUNTIME_ROOT)
    except ValueError as exc:
        raise ProbeError(label + " must stay under the repository runtime directory") from exc
    return resolved


def _model_path(value: str | None) -> Path:
    return _safe_runtime_path(value or DEFAULT_MODEL_PATH, "model path")


def _cache_path(value: str | None) -> Path:
    return _safe_runtime_path(value or DEFAULT_CACHE_PATH, "cache path")


def verify_model(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ModelError("pinned MoGe-2 checkpoint is not installed")
    size = path.stat().st_size
    if size != MODEL_BYTES:
        raise ModelError("pinned MoGe-2 checkpoint has the wrong byte length")
    digest = _sha256_file(path)
    if digest != MODEL_SHA256:
        raise ModelError("pinned MoGe-2 checkpoint failed SHA-256 verification")
    return {
        "status": "verified",
        "repository": MODEL_REPOSITORY,
        "revision": MODEL_REVISION,
        "filename": MODEL_FILENAME,
        "bytes": size,
        "sha256": digest,
    }


def _configure_huggingface(cache: Path, *, offline: bool) -> None:
    cache.mkdir(parents=True, exist_ok=True)
    os.environ["HF_HOME"] = str(cache)
    os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ["HF_HUB_OFFLINE"] = "1" if offline else "0"


def install_model(args: argparse.Namespace) -> dict[str, Any]:
    destination = _model_path(args.model_path)
    cache = _cache_path(args.cache_dir)
    if destination.exists() and not args.replace:
        result = verify_model(destination)
        result["status"] = "already_installed"
        return result
    _configure_huggingface(cache, offline=False)
    try:
        from huggingface_hub import hf_hub_download
    except ImportError as exc:
        raise DependencyError("huggingface_hub is not installed in the MoGe environment") from exc

    downloaded = Path(
        hf_hub_download(
            repo_id=MODEL_REPOSITORY,
            repo_type="model",
            filename=MODEL_FILENAME,
            revision=MODEL_REVISION,
            cache_dir=str(cache),
            token=False,
            local_files_only=False,
        )
    )
    if downloaded.stat().st_size != MODEL_BYTES or _sha256_file(downloaded) != MODEL_SHA256:
        raise ModelError("downloaded checkpoint does not match the pinned manifest")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".partial")
    if temporary.exists():
        temporary.unlink()
    shutil.copyfile(downloaded, temporary)
    if temporary.stat().st_size != MODEL_BYTES or _sha256_file(temporary) != MODEL_SHA256:
        temporary.unlink(missing_ok=True)
        raise ModelError("copied checkpoint failed integrity verification")
    if destination.exists():
        if not args.replace:
            temporary.unlink(missing_ok=True)
            raise ModelError("model destination already exists")
        destination.unlink()
    os.replace(temporary, destination)
    return verify_model(destination)


def _profile(name: str) -> ProbeProfile:
    try:
        return PROFILES[name]
    except KeyError as exc:
        raise ProbeError("unknown MoGe profile: " + name) from exc


def _import_arrays() -> tuple[Any, Any, Any]:
    try:
        import numpy as np
        from PIL import Image, ImageOps
    except ImportError as exc:
        raise DependencyError("NumPy and Pillow are required for image probing") from exc
    return np, Image, ImageOps


def _load_rgb(path: Path, max_edge: int) -> tuple[Any, str]:
    np, Image, ImageOps = _import_arrays()
    if not path.is_file():
        raise ProbeError("input image does not exist")
    size = path.stat().st_size
    if size < 1 or size > MAX_INPUT_BYTES:
        raise ProbeError("input image size is outside the allowed range")
    try:
        with Image.open(path) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
            if max(image.size) > max_edge:
                scale = max_edge / max(image.size)
                resized = (
                    max(1, int(round(image.width * scale))),
                    max(1, int(round(image.height * scale))),
                )
                image = image.resize(resized, Image.Resampling.LANCZOS)
            rgb = np.asarray(image, dtype=np.uint8).copy()
    except (OSError, ValueError) as exc:
        raise ProbeError("input is not a supported, readable image") from exc
    if rgb.ndim != 3 or rgb.shape[2] != 3:
        raise ProbeError("decoded input is not an RGB image")
    return rgb, _sha256_file(path)


def _mock_infer(rgb: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    np, _, _ = _import_arrays()
    height, width = rgb.shape[:2]
    fx = fy = 0.85
    cx = cy = 0.5
    u = (np.arange(width, dtype=np.float32) + 0.5) / width
    v = (np.arange(height, dtype=np.float32) + 0.5) / height
    uu, vv = np.meshgrid(u, v)
    depth = (1.25 + 0.20 * ((uu - cx) ** 2 + (vv - cy) ** 2)).astype(np.float32)
    x = ((uu - cx) / fx * depth).astype(np.float32)
    y = ((vv - cy) / fy * depth).astype(np.float32)
    points = np.stack((x, y, depth), axis=-1)
    normal = np.zeros_like(points, dtype=np.float32)
    normal[..., 2] = -1.0
    mask = np.ones((height, width), dtype=bool)
    intrinsics = np.array(
        [[fx, 0.0, cx], [0.0, fy, cy], [0.0, 0.0, 1.0]], dtype=np.float32
    )
    return (
        {
            "points": points,
            "depth": depth,
            "normal": normal,
            "mask": mask,
            "intrinsics": intrinsics,
        },
        {"inference_samples_ms": [0.0], "peak_cuda_memory_mib": 0.0},
    )


def _real_infer(
    rgb: Any,
    *,
    model_path: Path,
    device: str,
    num_tokens: int,
    warmup: int,
    repeat: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        import numpy as np
        import torch
        from moge.model.v2 import MoGeModel
    except ImportError as exc:
        raise DependencyError("PyTorch, NumPy, and the pinned MoGe source are required") from exc
    if not torch.cuda.is_available() or not device.startswith("cuda"):
        raise DependencyError("the initial MoGe-2 probe requires an available CUDA device")
    try:
        selected = torch.device(device)
        torch.cuda.set_device(selected)
    except (RuntimeError, ValueError) as exc:
        raise DependencyError("requested CUDA device is unavailable") from exc

    model = MoGeModel.from_pretrained(str(model_path)).to(selected).eval().half()
    image = torch.from_numpy(np.ascontiguousarray(rgb)).to(
        device=selected, dtype=torch.float32
    ).permute(2, 0, 1) / 255.0

    def invoke() -> dict[str, Any]:
        return model.infer(
            image,
            num_tokens=num_tokens,
            use_fp16=True,
            force_projection=True,
            apply_mask=True,
        )

    for _ in range(warmup):
        invoke()
    torch.cuda.synchronize(selected)
    torch.cuda.reset_peak_memory_stats(selected)
    samples = []
    output: Mapping[str, Any] | None = None
    for _ in range(repeat):
        started = time.perf_counter()
        output = invoke()
        torch.cuda.synchronize(selected)
        samples.append((time.perf_counter() - started) * 1000.0)
    assert output is not None
    result = {
        key: value.detach().float().cpu().numpy()
        for key, value in output.items()
        if key in {"points", "depth", "normal", "mask", "intrinsics"}
    }
    return result, {
        "inference_samples_ms": samples,
        "peak_cuda_memory_mib": torch.cuda.max_memory_allocated(selected) / (1024 * 1024),
        "torch_version": torch.__version__,
        "cuda_runtime": torch.version.cuda,
        "device_name": torch.cuda.get_device_name(selected),
    }


def _sanitize_geometry(rgb: Any, output: Mapping[str, Any]) -> dict[str, Any]:
    np, _, _ = _import_arrays()
    required = {"points", "depth", "normal", "mask", "intrinsics"}
    missing = sorted(required.difference(output))
    if missing:
        raise ProbeError("MoGe output is missing: " + ", ".join(missing))
    height, width = rgb.shape[:2]
    points = np.asarray(output["points"], dtype=np.float32)
    depth = np.asarray(output["depth"], dtype=np.float32)
    normal = np.asarray(output["normal"], dtype=np.float32)
    mask = np.asarray(output["mask"], dtype=bool)
    intrinsics = np.asarray(output["intrinsics"], dtype=np.float32)
    if points.shape != (height, width, 3):
        raise ProbeError("MoGe point-map dimensions do not match RGB")
    if depth.shape != (height, width) or mask.shape != (height, width):
        raise ProbeError("MoGe depth/mask dimensions do not match RGB")
    if normal.shape != (height, width, 3) or intrinsics.shape != (3, 3):
        raise ProbeError("MoGe normal/intrinsics dimensions are invalid")

    finite = (
        np.isfinite(points).all(axis=-1)
        & np.isfinite(depth)
        & np.isfinite(normal).all(axis=-1)
        & (depth > 0)
    )
    mask &= finite
    if not mask.any():
        raise ProbeError("MoGe returned no finite positive geometry")

    points = np.where(mask[..., None], points, 0.0).astype(np.float32)
    depth = np.where(mask, depth, 0.0).astype(np.float32)
    normal = np.where(mask[..., None], normal, 0.0).astype(np.float32)
    if not np.isfinite(intrinsics).all():
        raise ProbeError("MoGe returned non-finite intrinsics")
    if intrinsics[0, 0] <= 0 or intrinsics[1, 1] <= 0:
        raise ProbeError("MoGe returned invalid focal lengths")

    # Convert the native OpenCV camera convention to the stable FlexGPU
    # camera convention while retaining positive optical-axis depth separately.
    position_flex = points.copy()
    position_flex[..., 1] *= -1.0
    position_flex[..., 2] *= -1.0
    normal_flex = normal.copy()
    normal_flex[..., 1] *= -1.0
    normal_flex[..., 2] *= -1.0
    if not np.allclose(position_flex[..., 2][mask], -depth[mask], rtol=1e-4, atol=1e-5):
        raise ProbeError("MoGe points and depth are not projection-consistent")

    alpha = mask.astype(np.float32)[..., None]
    position_rgba = np.concatenate((position_flex, alpha), axis=-1).astype(np.float32)
    normal_rgba = np.concatenate((normal_flex, alpha), axis=-1).astype(np.float32)
    rgb_rgba = np.concatenate(
        (rgb.astype(np.uint8), np.full((height, width, 1), 255, dtype=np.uint8)),
        axis=-1,
    )
    return {
        "rgb": rgb_rgba,
        "position_camera": position_rgba,
        "depth": depth,
        "normal_camera": normal_rgba,
        "mask": mask.astype(np.uint8),
        "confidence": mask.astype(np.float32),
        "intrinsics": intrinsics,
    }


def _save_png(path: Path, array: Any) -> None:
    _, Image, _ = _import_arrays()
    Image.fromarray(array).save(path, format="PNG", optimize=False)


def _depth_preview(depth: Any, mask: Any) -> Any:
    np, _, _ = _import_arrays()
    valid = depth[mask]
    low, high = np.percentile(valid, [2.0, 98.0])
    if high <= low:
        high = low + 1.0
    t = np.clip((depth - low) / (high - low), 0.0, 1.0)
    # Near = warm, far = cool; black remains invalid.
    red = (255.0 * (1.0 - t)).astype(np.uint8)
    green = (255.0 * (1.0 - np.abs(t * 2.0 - 1.0))).astype(np.uint8)
    blue = (255.0 * t).astype(np.uint8)
    preview = np.stack((red, green, blue), axis=-1)
    preview[~mask] = 0
    return preview


def _write_ply(path: Path, position: Any, rgb: Any, mask: Any) -> None:
    np, _, _ = _import_arrays()
    vertices = np.ascontiguousarray(position[..., :3][mask], dtype=np.float32)
    colors = np.ascontiguousarray(rgb[..., :3][mask], dtype=np.uint8)
    structured = np.empty(
        vertices.shape[0],
        dtype=[("x", "<f4"), ("y", "<f4"), ("z", "<f4"), ("red", "u1"), ("green", "u1"), ("blue", "u1")],
    )
    structured["x"], structured["y"], structured["z"] = vertices.T
    structured["red"], structured["green"], structured["blue"] = colors.T
    header = (
        "ply\nformat binary_little_endian 1.0\n"
        f"element vertex {len(structured)}\n"
        "property float x\nproperty float y\nproperty float z\n"
        "property uchar red\nproperty uchar green\nproperty uchar blue\n"
        "end_header\n"
    ).encode("ascii")
    with path.open("wb") as stream:
        stream.write(header)
        stream.write(structured.tobytes())


def _point_preview(position: Any, rgb: Any, mask: Any, size: int = 768) -> Any:
    """Render a dependency-free oblique point preview with a small z-buffer."""

    np, _, _ = _import_arrays()
    points = np.asarray(position[..., :3][mask], dtype=np.float32)
    colors = np.asarray(rgb[..., :3][mask], dtype=np.uint8)
    center = np.median(points, axis=0)
    points = points - center

    yaw = math.radians(22.0)
    pitch = math.radians(-8.0)
    cy, sy = math.cos(yaw), math.sin(yaw)
    cp, sp = math.cos(pitch), math.sin(pitch)
    x1 = cy * points[:, 0] + sy * points[:, 2]
    z1 = -sy * points[:, 0] + cy * points[:, 2]
    y2 = cp * points[:, 1] - sp * z1
    z2 = sp * points[:, 1] + cp * z1

    x_low, x_high = np.percentile(x1, [1.0, 99.0])
    y_low, y_high = np.percentile(y2, [1.0, 99.0])
    extent = max(float(x_high - x_low), float(y_high - y_low), 1.0e-6)
    x_center = float((x_low + x_high) * 0.5)
    y_center = float((y_low + y_high) * 0.5)
    px = np.rint((x1 - x_center) / extent * (size * 0.88) + size * 0.5).astype(np.int32)
    py = np.rint(size * 0.5 - (y2 - y_center) / extent * (size * 0.88)).astype(np.int32)
    inside = (px >= 1) & (px < size - 1) & (py >= 1) & (py < size - 1)
    px, py, z2, colors = px[inside], py[inside], z2[inside], colors[inside]
    # FlexGPU camera Z is backward, so larger rotated Z is closer here.  Draw
    # far-to-near and let later writes win.
    order = np.argsort(z2)
    px, py, colors = px[order], py[order], colors[order]
    canvas = np.full((size, size, 3), (7, 8, 12), dtype=np.uint8)
    for offset_x, offset_y in ((0, 0), (1, 0), (-1, 0), (0, 1), (0, -1)):
        canvas[py + offset_y, px + offset_x] = colors
    return canvas


PLANE_SEMANTICS = {
    "rgb": "Exact RGBA image used for inference; alpha is 255.",
    "position_camera": "RGB=FlexGPU camera XYZ metres; A=binary validity.",
    "depth": "Positive optical-axis depth metres; invalid pixels are zero.",
    "normal_camera": "RGB=FlexGPU camera normal; A=binary validity.",
    "mask": "Binary valid-geometry mask with values 0 or 1.",
    "confidence": "Binary validity proxy as float32; not learned confidence.",
}


def _plane_descriptor(path: Path, array: Any, semantics: str) -> dict[str, Any]:
    return {
        "filename": path.name,
        "dtype": str(array.dtype),
        "shape": list(array.shape),
        "byte_length": path.stat().st_size,
        "sha256": _sha256_file(path),
        "semantics": semantics,
    }


def _safe_cleanup(path: Path) -> None:
    resolved = path.resolve()
    try:
        resolved.relative_to(DEFAULT_RUNS_PATH.resolve())
    except ValueError:
        return
    if resolved.is_dir():
        shutil.rmtree(resolved)


def _run_output_path(run_id: str | None, input_digest: str) -> Path:
    DEFAULT_RUNS_PATH.mkdir(parents=True, exist_ok=True)
    if run_id is None:
        run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + input_digest[:10]
    if not run_id or len(run_id) > 96 or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-" for ch in run_id):
        raise ProbeError("run ID contains unsupported characters or is too long")
    output = (DEFAULT_RUNS_PATH / run_id).resolve()
    if output.parent != DEFAULT_RUNS_PATH.resolve():
        raise ProbeError("run output escaped the MoGe runs directory")
    if output.exists():
        raise ProbeError("run output already exists; choose a unique --run-id")
    return output


def run_infer(args: argparse.Namespace) -> dict[str, Any]:
    np, _, _ = _import_arrays()
    profile = _profile(args.profile)
    max_edge = args.max_edge if args.max_edge is not None else profile.max_edge
    num_tokens = args.num_tokens if args.num_tokens is not None else profile.num_tokens
    if not 64 <= max_edge <= 2048:
        raise ProbeError("max edge must be between 64 and 2048")
    if not 1200 <= num_tokens <= 3600:
        raise ProbeError("num_tokens must be between 1200 and 3600")
    if not 0 <= args.warmup <= 20 or not 1 <= args.repeat <= 100:
        raise ProbeError("warmup/repeat are outside their bounded ranges")

    input_path = Path(args.input).expanduser().resolve()
    source_timestamp_ns = time.time_ns()
    rgb, input_digest = _load_rgb(input_path, max_edge)
    output_path = _run_output_path(args.run_id, input_digest)
    temporary = Path(tempfile.mkdtemp(prefix=".moge2-", dir=DEFAULT_RUNS_PATH)).resolve()
    try:
        if args.backend == "mock":
            raw, performance = _mock_infer(rgb)
            precision = "mock"
            model_id = "flexgpu:mock-plane"
        else:
            checkpoint = _model_path(args.model_path)
            verify_model(checkpoint)
            _configure_huggingface(_cache_path(args.cache_dir), offline=True)
            raw, performance = _real_infer(
                rgb,
                model_path=checkpoint,
                device=args.device,
                num_tokens=num_tokens,
                warmup=args.warmup,
                repeat=args.repeat,
            )
            precision = "fp16"
            model_id = MODEL_REPOSITORY.replace("/", ":")
        geometry = _sanitize_geometry(rgb, raw)
        mask_bool = geometry["mask"].astype(bool)

        plane_descriptors: dict[str, Any] = {}
        for name in ("rgb", "position_camera", "depth", "normal_camera", "mask", "confidence"):
            path = temporary / (name + ".npy")
            np.save(path, geometry[name], allow_pickle=False)
            plane_descriptors[name] = _plane_descriptor(path, geometry[name], PLANE_SEMANTICS[name])

        _save_png(temporary / "rgb.png", geometry["rgb"])
        _save_png(temporary / "mask.png", geometry["mask"] * 255)
        _save_png(temporary / "depth_preview.png", _depth_preview(geometry["depth"], mask_bool))
        normal_preview = np.clip(
            (geometry["normal_camera"][..., :3] * 0.5 + 0.5) * 255.0,
            0,
            255,
        ).astype(np.uint8)
        normal_preview[~mask_bool] = 0
        _save_png(temporary / "normal_preview.png", normal_preview)
        _write_ply(
            temporary / "points.ply",
            geometry["position_camera"],
            geometry["rgb"],
            mask_bool,
        )
        _save_png(
            temporary / "point_preview.png",
            _point_preview(
                geometry["position_camera"], geometry["rgb"], mask_bool
            ),
        )

        intrinsics = geometry["intrinsics"]
        height, width = rgb.shape[:2]
        normalized = [
            float(intrinsics[0, 0]),
            float(intrinsics[1, 1]),
            float(intrinsics[0, 2]),
            float(intrinsics[1, 2]),
        ]
        pixels = [
            normalized[0] * width,
            normalized[1] * height,
            normalized[2] * width,
            normalized[3] * height,
        ]
        samples = [float(value) for value in performance["inference_samples_ms"]]
        completed_timestamp_ns = time.time_ns()
        manifest = {
            "contract": CONTRACT,
            "producer_session_id": "probe-" + uuid.uuid4().hex,
            "frame_id": 0,
            "source_session_id": "saved-frame",
            "source_frame_id": 0,
            "source_timestamp_ns": str(source_timestamp_ns),
            "completed_timestamp_ns": str(completed_timestamp_ns),
            "generation_id": "generated-" + input_digest[:16],
            "width": width,
            "height": height,
            "model": {
                "id": model_id,
                "source_revision": MOGE_SOURCE_REVISION,
                "model_revision": MODEL_REVISION if args.backend == "moge2" else "0" * 40,
                "precision": precision,
                "num_tokens": num_tokens,
                "inference_ms": sum(samples) / len(samples),
            },
            "intrinsics_normalized": normalized,
            "intrinsics_pixels": pixels,
            "camera_to_world": [
                1.0, 0.0, 0.0, 0.0,
                0.0, 1.0, 0.0, 0.0,
                0.0, 0.0, 1.0, 0.0,
                0.0, 0.0, 0.0, 1.0,
            ],
            "coordinate_system": COORDINATE_SYSTEM,
            "source_coordinate_system": SOURCE_COORDINATE_SYSTEM,
            "valid_fraction": float(mask_bool.mean()),
            "confidence_mean": float(geometry["confidence"].mean()),
            "confidence_semantics": "binary_validity_proxy",
            "planes": plane_descriptors,
        }
        validate_geometry_manifest(manifest)
        manifest_path = temporary / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True, allow_nan=False) + "\n",
            encoding="utf-8",
        )

        valid_depth = geometry["depth"][mask_bool]
        report = {
            "status": "pass",
            "backend": args.backend,
            "profile": asdict(profile),
            "input": {
                "filename": input_path.name,
                "sha256": input_digest,
                "decoded_width": width,
                "decoded_height": height,
            },
            "geometry": {
                "valid_fraction": manifest["valid_fraction"],
                "depth_min_m": float(valid_depth.min()),
                "depth_median_m": float(np.median(valid_depth)),
                "depth_max_m": float(valid_depth.max()),
                "fov_x_deg": float(math.degrees(2.0 * math.atan(0.5 / normalized[0]))),
                "fov_y_deg": float(math.degrees(2.0 * math.atan(0.5 / normalized[1]))),
            },
            "performance": {
                **performance,
                "inference_mean_ms": sum(samples) / len(samples),
                "inference_min_ms": min(samples),
                "inference_max_ms": max(samples),
            },
            "pins": {
                "moge_source_revision": MOGE_SOURCE_REVISION,
                "model_revision": MODEL_REVISION,
                "model_sha256": MODEL_SHA256,
            },
        }
        (temporary / "report.json").write_text(
            json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        verify_geometry_bundle(manifest_path)
        os.replace(temporary, output_path)
        return {
            "status": "pass",
            "backend": args.backend,
            "output": str(output_path),
            "manifest": str(output_path / "manifest.json"),
            "report": str(output_path / "report.json"),
            "valid_fraction": report["geometry"]["valid_fraction"],
            "inference_mean_ms": report["performance"]["inference_mean_ms"],
            "peak_cuda_memory_mib": performance.get("peak_cuda_memory_mib", 0.0),
        }
    except Exception:
        _safe_cleanup(temporary)
        raise


def run_doctor(args: argparse.Namespace) -> dict[str, Any]:
    profile = _profile(args.profile)
    result: dict[str, Any] = {
        "status": "pass",
        "profile": asdict(profile),
        "pins": {
            "moge_source_repository": MOGE_SOURCE_REPOSITORY,
            "moge_source_revision": MOGE_SOURCE_REVISION,
            "model_repository": MODEL_REPOSITORY,
            "model_revision": MODEL_REVISION,
            "model_bytes": MODEL_BYTES,
            "model_sha256": MODEL_SHA256,
        },
        "dependencies": {},
        "model": {"status": "not_checked"},
    }
    try:
        import numpy
        import PIL
        import torch
        import moge

        result["dependencies"] = {
            "numpy": numpy.__version__,
            "pillow": PIL.__version__,
            "torch": torch.__version__,
            "moge_imported": bool(moge),
            "cuda_available": torch.cuda.is_available(),
            "cuda_runtime": torch.version.cuda,
            "devices": [torch.cuda.get_device_name(index) for index in range(torch.cuda.device_count())],
        }
        if not torch.cuda.is_available():
            result["status"] = "fail"
    except ImportError as exc:
        result["status"] = "fail"
        result["dependencies"] = {"error": "missing dependency: " + str(exc.name)}
    try:
        result["model"] = verify_model(_model_path(args.model_path))
    except ModelError as exc:
        result["model"] = {"status": "missing_or_invalid", "error": str(exc)}
        result["status"] = "fail"
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Pinned, local-only MoGe-2 offline probe for FlexGPU"
    )
    subparsers = parser.add_subparsers(dest="action", required=True)

    subparsers.add_parser("profiles", help="print dependency-free GPU starting profiles")

    doctor = subparsers.add_parser("doctor", help="check the isolated runtime and checkpoint")
    doctor.add_argument("--profile", choices=sorted(PROFILES), default="3080ti_16gb")
    doctor.add_argument("--model-path")

    install = subparsers.add_parser(
        "model-install", help="explicitly download and verify the pinned official checkpoint"
    )
    install.add_argument("--model-path")
    install.add_argument("--cache-dir")
    install.add_argument("--replace", action="store_true")

    infer = subparsers.add_parser("infer", help="run one saved image through MoGe-2")
    infer.add_argument("--input", required=True)
    infer.add_argument("--profile", choices=sorted(PROFILES), default="3080ti_16gb")
    infer.add_argument("--backend", choices=("moge2", "mock"), default="moge2")
    infer.add_argument("--model-path")
    infer.add_argument("--cache-dir")
    infer.add_argument("--device", default="cuda:0")
    infer.add_argument("--num-tokens", type=int)
    infer.add_argument("--max-edge", type=int)
    infer.add_argument("--warmup", type=int, default=1)
    infer.add_argument("--repeat", type=int, default=3)
    infer.add_argument("--run-id")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.action == "profiles":
            payload = {
                "status": "ok",
                "profiles": {name: asdict(profile) for name, profile in PROFILES.items()},
                "pins": {
                    "moge_source_revision": MOGE_SOURCE_REVISION,
                    "model_revision": MODEL_REVISION,
                    "model_sha256": MODEL_SHA256,
                },
            }
        elif args.action == "doctor":
            payload = run_doctor(args)
        elif args.action == "model-install":
            payload = install_model(args)
        else:
            payload = run_infer(args)
        _print(payload)
        return 0 if payload.get("status") not in {"fail", "error"} else 3
    except (ProbeError, OSError, ValueError) as exc:
        _print({"status": "error", "type": type(exc).__name__, "error": str(exc)})
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
