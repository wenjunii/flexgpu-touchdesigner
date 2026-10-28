#!/usr/bin/env python3
"""Conservative CI workload selection. Publication safety is never skipped."""
from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def documentation_only(paths: list[str]) -> bool:
    if not paths:
        return False
    for value in paths:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or "\\" in value:
            return False
        if value == "LICENSE" or path.name == "README.md":
            continue
        if len(path.parts) > 1 and path.parts[0] == "docs" and path.suffix == ".md":
            continue
        return False  # New/unknown files always get the full PR test suite.
    return True


def changed_paths(base: str, head: str, root: Path = ROOT) -> list[str]:
    if any(not re.fullmatch(r"[0-9a-fA-F]{40}", ref) or set(ref) == {"0"}
           for ref in (base, head)):
        return []  # Manual/release/first-push events are conservative.
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "--no-renames", "-z", base, head, "--"],
            cwd=root, check=True, capture_output=True, timeout=30,
        )
        return [p.decode("utf-8", errors="strict") for p in result.stdout.split(b"\0") if p]
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return []


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="")
    parser.add_argument("--head", default="")
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args(argv)
    paths = changed_paths(args.base, args.head)
    docs_only = documentation_only(paths)
    # Do not echo attacker-controlled filenames into runner commands or logs.
    print(json.dumps({"docs_only": docs_only, "changed_paths": len(paths)}))
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as stream:
            stream.write(f"docs_only={str(docs_only).lower()}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
