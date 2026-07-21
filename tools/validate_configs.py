#!/usr/bin/env python3
"""Validate FlexShow JSON profiles with both schema and launcher semantics."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Iterable, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCHEMA = REPOSITORY_ROOT / "config" / "flexshow.schema.json"
DEFAULT_TARGETS = (
    REPOSITORY_ROOT / "config" / "flexshow.json",
    REPOSITORY_ROOT / "config" / "presets",
)
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if os.fspath(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, os.fspath(SOURCE_ROOT))

from flexgpu.config import validate_config  # noqa: E402
from flexgpu.models import ConfigError  # noqa: E402


def _reject_constant(value: str) -> None:
    raise ValueError("non-finite JSON number: " + value)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key: " + key)
        result[key] = value
    return result


def load_strict_json(path: Path) -> Any:
    """Load JSON while rejecting duplicate keys and non-standard NaN values."""

    with path.open("r", encoding="utf-8-sig") as handle:
        return json.load(
            handle,
            parse_constant=_reject_constant,
            object_pairs_hook=_reject_duplicate_keys,
        )


def configuration_paths(targets: Iterable[Path], schema_path: Path) -> list[Path]:
    """Expand files/directories into a stable, duplicate-free JSON file list."""

    selected: dict[str, Path] = {}
    schema_resolved = schema_path.resolve()
    for target in targets:
        candidate = target.resolve()
        if not candidate.exists():
            raise FileNotFoundError(os.fspath(candidate))
        files = sorted(candidate.rglob("*.json")) if candidate.is_dir() else [candidate]
        for path in files:
            resolved = path.resolve()
            if resolved == schema_resolved:
                continue
            selected[os.path.normcase(os.fspath(resolved))] = resolved
    return [selected[key] for key in sorted(selected)]


def _json_pointer(parts: Iterable[Any]) -> str:
    encoded = [str(part).replace("~", "~0").replace("/", "~1") for part in parts]
    return "/" + "/".join(encoded) if encoded else "/"


def validate(
    schema_path: Path,
    targets: Iterable[Path],
) -> tuple[list[Path], list[str]]:
    """Return the selected files and human-readable validation failures."""

    try:
        import jsonschema
    except ImportError as exc:  # pragma: no cover - exercised by operator environments
        raise RuntimeError(
            "jsonschema is required; install the pinned CI dependency before validation"
        ) from exc

    schema_path = schema_path.resolve()
    schema = load_strict_json(schema_path)
    jsonschema.Draft202012Validator.check_schema(schema)
    validator = jsonschema.Draft202012Validator(schema)
    paths = configuration_paths(targets, schema_path)
    if not paths:
        raise ValueError("no JSON configuration files were selected")

    failures: list[str] = []
    for path in paths:
        try:
            instance = load_strict_json(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            failures.append("%s: invalid JSON: %s" % (path, exc))
            continue

        if isinstance(instance, dict) and "$schema" in instance:
            declaration = instance["$schema"]
            if isinstance(declaration, str) and "://" not in declaration:
                declared_path = (path.parent / declaration).resolve()
                if declared_path != schema_path:
                    failures.append(
                        "%s: /$schema resolves to %s, expected %s"
                        % (path, declared_path, schema_path)
                    )

        for error in sorted(validator.iter_errors(instance), key=lambda item: list(item.path)):
            failures.append(
                "%s: %s: %s" % (path, _json_pointer(error.absolute_path), error.message)
            )
        if isinstance(instance, dict):
            try:
                validate_config(instance, os.fspath(path))
            except ConfigError as exc:
                for message in exc.errors:
                    failures.append("%s: launcher: %s" % (path, message))
    return paths, failures


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate FlexShow JSON profiles with JSON Schema Draft 2020-12 "
            "and the dependency-free launcher validator."
        )
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="configuration file or directory (defaults to shipped profiles)",
    )
    parser.add_argument(
        "--schema",
        type=Path,
        default=DEFAULT_SCHEMA,
        help="schema path (default: config/flexshow.schema.json)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    targets = args.paths or list(DEFAULT_TARGETS)
    try:
        paths, failures = validate(args.schema, targets)
    except Exception as exc:
        print("configuration validation failed: %s" % exc, file=sys.stderr)
        return 2
    if failures:
        for failure in failures:
            print(failure, file=sys.stderr)
        print("%d configuration validation error(s)" % len(failures), file=sys.stderr)
        return 1
    print(
        "validated %d configuration(s) against %s and launcher semantics"
        % (len(paths), Path(args.schema).resolve())
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
