#!/usr/bin/env python3
"""Generate and validate local FlexShow commissioning data."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence


REPOSITORY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE_ROOT = os.path.join(REPOSITORY_ROOT, "src")
if SOURCE_ROOT not in sys.path:
    sys.path.insert(0, SOURCE_ROOT)

from flexgpu.commissioning import (  # noqa: E402
    CalibrationProfile,
    CommissioningError,
    generate_demo_bundle,
    load_strict_json,
    validate_bundle,
)


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0 or not parsed < float("inf"):
        raise argparse.ArgumentTypeError("must be a positive finite number")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Generate deterministic RGB/depth commissioning data or validate "
            "a local adapter calibration/replay bundle."
        )
    )
    commands = parser.add_subparsers(dest="action", required=True)

    generate = commands.add_parser(
        "demo", help="create a deterministic synchronized commissioning bundle"
    )
    generate.add_argument("--output", required=True)
    generate.add_argument("--frames", type=_positive_int, default=8)
    generate.add_argument("--width", type=_positive_int, default=64)
    generate.add_argument("--height", type=_positive_int, default=36)
    generate.add_argument("--interval-ms", type=_positive_float, default=100.0)

    calibration = commands.add_parser(
        "calibration", help="strictly validate one calibration profile"
    )
    calibration.add_argument("path")

    inspect = commands.add_parser(
        "inspect", help="verify and summarize a synchronized replay bundle"
    )
    inspect.add_argument("manifest")
    inspect.add_argument(
        "--skip-hashes",
        action="store_true",
        help="validate structure and file presence without reading every payload",
    )
    return parser


def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.action == "demo":
        return generate_demo_bundle(
            args.output,
            frames=args.frames,
            width=args.width,
            height=args.height,
            interval_ms=args.interval_ms,
        )
    if args.action == "calibration":
        profile = CalibrationProfile.from_mapping(load_strict_json(args.path))
        return {
            "status": "valid",
            "version": profile.version,
            "calibration_id": profile.calibration_id,
            "calibration_digest": profile.calibration_digest,
            "dimensions": {"width": profile.width, "height": profile.height},
            "depth_encoding": profile.depth_encoding,
            "coordinate_system": profile.coordinate_system,
        }
    return validate_bundle(args.manifest, verify_hashes=not args.skip_hashes)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = run(args)
    except (CommissioningError, OSError, ValueError) as exc:
        payload = {
            "status": "error",
            "error": type(exc).__name__,
            "message": str(exc),
        }
        print(json.dumps(payload, indent=2, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
