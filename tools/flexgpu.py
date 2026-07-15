#!/usr/bin/env python3
"""Repository-local FlexGPU command-line entry point."""

from __future__ import annotations

import os
import sys


REPOSITORY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE_ROOT = os.path.join(REPOSITORY_ROOT, "src")
if SOURCE_ROOT not in sys.path:
    sys.path.insert(0, SOURCE_ROOT)

from flexgpu.cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
