#!/usr/bin/env python3
"""Opt-in, offline CUDA smoke test; no webcam, listeners, TOE, or saved RGB.

Run this with the candidate worker's Python, not TouchDesigner's interpreter.
Synthetic frames exercise resizing, persistent inference, and atlas packing.
This is dependency compatibility evidence, not visual or venue acceptance.
"""

from __future__ import annotations

import argparse
import importlib.metadata as metadata
import json
from pathlib import Path
import sys
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SIZES = ((512, 512), (1024, 567), (1024, 576))
OUTPUT_SIZES = ((384, 384), (512, 284), (512, 288))


def check_source_revision(name: str, expected: str) -> None:
    direct = json.loads(metadata.distribution(name).read_text("direct_url.json") or "{}")
    if direct.get("vcs_info", {}).get("commit_id") != expected:
        raise RuntimeError(f"{name}: installed source revision differs from pin")


def check_custom_generation_consent() -> None:
    """Regression for GHSA-x9r9-c232-4q39 without any Hub request."""
    from transformers.generation import utils as generation

    with patch.object(generation, "has_file", return_value=True), patch.object(
        generation, "get_cached_module_file"
    ) as download:
        try:
            generation.GenerationMixin.load_custom_generate(
                object(), "flexgpu-synthetic/no-download", trust_remote_code=False
            )
        except ValueError:
            pass
        else:
            raise RuntimeError("Custom generation did not reject absent trust consent")
        if download.called:
            raise RuntimeError("Custom generation downloaded code before trust consent")


def check_versions(provider: str) -> dict[str, str]:
    if sys.version_info < (3, 11):
        raise RuntimeError("The patched worker runtime requires Python 3.11+")
    requirements = ROOT / "integrations" / provider / "requirements-runtime.txt"
    versions = {}
    for line in requirements.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "==" in line:
            name, expected = line.split("==", 1)
            actual = metadata.version(name)
            if actual != expected:
                raise RuntimeError(f"{name}: installed {actual}, expected {expected}")
            versions[name] = actual
        elif " @ git+" in line:
            name, url = line.split(" @ git+", 1)
            check_source_revision(name, url.rsplit("@", 1)[-1])
            versions[name] = metadata.version(name)
        else:
            raise RuntimeError("Unsupported runtime requirement; update this audit explicitly")
    for name, expected in (("torch", "2.11.0"), ("torchvision", "0.26.0")):
        actual = metadata.version(name)
        if actual.split("+", 1)[0] != expected:
            raise RuntimeError(f"{name}: installed {actual}, expected {expected}")
        versions[name] = actual
    return versions


def synthetic_frame(provider: str, frame_id: int, width: int, height: int):
    import numpy as np
    from flexgpu.worldbus import make_frame

    y, x = np.mgrid[0:height, 0:width].astype(np.float32)
    x, y = x / width, y / height
    rgba = np.empty((height, width, 4), dtype=np.uint8)
    rgba[..., 0] = (40 + 160 * y).astype(np.uint8)
    rgba[..., 1] = (50 + 140 * x).astype(np.uint8)
    rgba[..., 2] = (210 - 140 * y).astype(np.uint8)
    rgba[..., 3] = 255
    sphere = ((x - 0.35) ** 2 + (y - 0.55) ** 2) < 0.045
    rgba[sphere, :3] = (220, 70, 35)
    box = (x > 0.6) & (x < 0.8) & (y > 0.35) & (y < 0.85)
    rgba[box, :3] = (45, 190, 90)
    payload = rgba.tobytes()
    return make_frame({
        "worldbus_version": 1, "frame_id": frame_id,
        "timestamp_ns": str(1_000_000_000 + frame_id),
        "width": width, "height": height, "pixel_format": "rgba8",
        "payload_bytes": len(payload),
        "intrinsics": [width * 0.8, height * 0.8, width * 0.5, height * 0.5],
        "depth_scale_bias": [1.0, 0.0],
        "camera_to_world": [1., 0., 0., 0., 0., 1., 0., 0., 0., 0., 1., 0., 0., 0., 0., 1.],
        "generation_id": "synthetic-security-smoke",
        "producer_session_id": "synthetic-security-smoke",
        "geometry_provider": provider,
    }, payload)


def run_smoke(provider: str, model: Path, device: str) -> dict:
    versions = check_versions(provider)
    sys.path.insert(0, str(ROOT / "src"))
    import numpy as np
    import torch
    import moge2_worker as worker_module
    from flexgpu.moge2_transport import decode_moge2_atlas

    cache = ROOT / "runtime" / "security-smoke-cache" / provider
    if provider == "moge2":
        check_source_revision("moge", worker_module.MOGE_SOURCE_REVISION)
        versions["moge"] = metadata.version("moge")
        backend = worker_module.MoGe2Backend(model_path=model, cache_path=cache, device=device)
    else:
        check_custom_generation_consent()
        backend = worker_module.DepthAnythingGeometryBackend(
            model_dir=model, cache_dir=cache, device=device, calibration_frames=1,
        )
    worker = worker_module.MoGe2Worker(
        backend, profile=worker_module.PROFILES["3080ti_16gb"],
        max_edge=512, target_pixels=147456, provider=provider,
    )
    frames = []
    for (width, height), expected in zip(SIZES, OUTPUT_SIZES):
        for repeat in range(2):
            source = synthetic_frame(provider, len(frames), width, height)
            result = worker.process_frame(source)
            decoded = decode_moge2_atlas(
                result.payload, atlas_width=result.metadata.width,
                height=result.metadata.height,
                depth_scale=result.metadata.depth_scale_bias[0],
                depth_bias=result.metadata.depth_scale_bias[1],
            )
            if (decoded.source_width, decoded.height) != expected:
                raise RuntimeError("Adaptive geometry dimensions changed")
            _, resized_rgba = worker_module._resize_rgb(source, 512, 147456)
            if decoded.source_rgba != resized_rgba:
                raise RuntimeError("Atlas RGB is not synchronized with inference input")
            depths, mask = np.asarray(decoded.depth_metres), np.asarray(decoded.mask)
            if not mask.any() or not np.isfinite(depths).all() or not (depths[mask] > 0).all():
                raise RuntimeError("Geometry has no positive finite valid depth")
            frames.append({
                "source_size": [width, height], "geometry_size": list(expected),
                "repeat": repeat, "valid_fraction": float(mask.mean()),
            })
    if backend.load_count != 1 or backend.inference_count != 6:
        raise RuntimeError("Persistent backend lifecycle changed")
    return {
        "status": "ok", "provider": provider, "profile": "3080ti_16gb",
        "gpu": torch.cuda.get_device_name(backend._backend.device if provider == "depth_anything" else backend.device),
        "versions": versions, "model_loads": backend.load_count,
        "inferences": backend.inference_count, "frames": frames,
        "visual_acceptance": "not_tested", "webcam_opened": False,
        "listeners_opened": False,
        "custom_generation_consent": "pass" if provider == "depth_anything" else "not_applicable",
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", required=True, choices=("moge2", "depth_anything"))
    parser.add_argument("--model", type=Path, help="Existing pinned checkpoint file or Depth Anything directory")
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--run", action="store_true", help="Explicitly load the model and run six synthetic GPU inferences")
    args = parser.parse_args(argv)
    if not args.run:
        print(json.dumps({"status": "preview", "provider": args.provider, "inferences": 6}))
        return 0
    model = args.model or (
        ROOT / "runtime" / "moge2-model" / "model.pt" if args.provider == "moge2"
        else ROOT / "runtime" / "depth-anything-v2-small"
    )
    try:
        result = run_smoke(args.provider, model, args.device)
    except Exception as exc:
        print(json.dumps({"status": "error", "type": type(exc).__name__, "error": str(exc)}))
        return 1
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
