#!/usr/bin/env python3
"""Capture a read-only GPU snapshot and recommend a starting role placement."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Sequence


REPOSITORY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE_ROOT = os.path.join(REPOSITORY_ROOT, "src")
if SOURCE_ROOT not in sys.path:
    sys.path.insert(0, SOURCE_ROOT)

from flexgpu.models import DiscoveryError  # noqa: E402
from flexgpu.profiling import (  # noqa: E402
    build_hardware_profile,
    query_runtime_gpu_profiles,
    write_hardware_profile,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read current NVIDIA headroom, load, thermals, clocks, and display "
            "ownership. The result is a commissioning hint, not a benchmark."
        )
    )
    parser.add_argument("--topology", choices=("single", "dual_local"), default="single")
    parser.add_argument("--nvidia-smi", help="path to nvidia-smi")
    parser.add_argument("--output", help="atomically write a machine-local JSON snapshot")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--compact", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        profiles = query_runtime_gpu_profiles(args.nvidia_smi)
        payload = build_hardware_profile(profiles, args.topology)
        if args.output:
            write_hardware_profile(args.output, payload, overwrite=args.overwrite)
            payload["output"] = os.path.abspath(args.output)
    except (DiscoveryError, OSError, ValueError) as exc:
        print(
            json.dumps(
                {"status": "error", "error": type(exc).__name__, "message": str(exc)},
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 2
    payload["status"] = "ok"
    print(
        json.dumps(
            payload,
            indent=None if args.compact else 2,
            separators=(",", ":") if args.compact else None,
            sort_keys=not args.compact,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
