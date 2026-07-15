#!/usr/bin/env python3
"""Run deterministic FlexShow quality simulations or replay telemetry JSONL."""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence


REPOSITORY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE_ROOT = os.path.join(REPOSITORY_ROOT, "src")
if SOURCE_ROOT not in sys.path:
    sys.path.insert(0, SOURCE_ROOT)

from flexgpu.adaptive import (  # noqa: E402
    AdaptiveQualityGovernor,
    TelemetryJsonlWriter,
    TelemetrySample,
    read_telemetry_jsonl,
    summarize_telemetry,
    write_telemetry_summary,
)


TIER_VRAM_MIB = {
    "3080ti_16gb": 16_384,
    "4090": 24_564,
    "5090": 32_607,
    "custom": 16_384,
}


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _add_governor_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--tier",
        choices=("3080ti_16gb", "4090", "5090", "custom"),
        default="3080ti_16gb",
    )
    parser.add_argument("--frame-budget-ms", type=_positive_float, default=1000.0 / 60.0)
    parser.add_argument("--queue-budget-ms", type=_positive_float, default=200.0)
    parser.add_argument("--down-window", type=_positive_int, default=3)
    parser.add_argument("--up-window", type=_positive_int, default=120)
    parser.add_argument("--cooldown", type=int, default=30, help="samples held after a change")
    parser.add_argument("--initial-level", type=int, help="zero-based quality level")
    parser.add_argument("--output-jsonl", help="write every evaluated sample")
    parser.add_argument("--summary-json", help="atomically write the final JSON summary")
    parser.add_argument("--compact", action="store_true", help="print compact JSON")


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="benchmark_flexshow",
        description="Exercise adaptive quality without TouchDesigner or third-party packages.",
    )
    commands = parser.add_subparsers(dest="mode", required=True)

    synthetic = commands.add_parser("synthetic", help="generate deterministic metrics")
    _add_governor_options(synthetic)
    synthetic.add_argument("--samples", type=_positive_int, default=600)
    synthetic.add_argument(
        "--pattern",
        choices=("steady", "ramp", "spike", "cycle"),
        default="cycle",
    )
    synthetic.add_argument("--seed", type=int, default=7)
    synthetic.add_argument("--vram-total-mib", type=_positive_float)

    replay = commands.add_parser("replay", help="re-evaluate captured telemetry JSONL")
    _add_governor_options(replay)
    replay.add_argument("input_jsonl", help="telemetry captured by FlexShow or this tool")
    return parser


def synthetic_samples(
    count: int,
    *,
    pattern: str,
    seed: int,
    frame_budget_ms: float,
    queue_budget_ms: float,
    vram_total_mib: float,
) -> Iterator[dict[str, float]]:
    """Yield a deterministic workload without sleeping or touching the GPU."""

    if count <= 0:
        raise ValueError("count must be greater than zero")
    if pattern not in {"steady", "ramp", "spike", "cycle"}:
        raise ValueError("unsupported synthetic pattern %r" % pattern)
    randomizer = random.Random(seed)
    for index in range(count):
        progress = index / float(max(1, count - 1))
        if pattern == "steady":
            frame_ratio, vram_ratio, queue_ratio = 0.72, 0.68, 0.35
        elif pattern == "ramp":
            frame_ratio = 0.65 + (0.85 * progress)
            vram_ratio = 0.65 + (0.32 * progress)
            queue_ratio = 0.30 + (1.40 * progress)
        elif pattern == "spike":
            spike = (index % 120) in range(72, 84)
            frame_ratio = 1.65 if spike else 0.72
            vram_ratio = 0.94 if spike else 0.69
            queue_ratio = 2.0 if spike else 0.35
        else:
            phase = (index % 240) / 240.0
            if phase < 0.25:
                frame_ratio, vram_ratio, queue_ratio = 0.70, 0.67, 0.32
            elif phase < 0.50:
                local = (phase - 0.25) / 0.25
                frame_ratio = 0.75 + (0.65 * local)
                vram_ratio = 0.70 + (0.24 * local)
                queue_ratio = 0.40 + (1.30 * local)
            elif phase < 0.75:
                frame_ratio, vram_ratio, queue_ratio = 1.30, 0.92, 1.55
            else:
                frame_ratio, vram_ratio, queue_ratio = 0.68, 0.65, 0.28

        frame_ratio *= 1.0 + randomizer.uniform(-0.025, 0.025)
        vram_ratio = max(0.0, min(1.02, vram_ratio + randomizer.uniform(-0.006, 0.006)))
        queue_ratio *= 1.0 + randomizer.uniform(-0.04, 0.04)
        yield {
            "timestamp": index * (frame_budget_ms / 1000.0),
            "frame_time_ms": frame_budget_ms * frame_ratio,
            "vram_used_mib": vram_total_mib * vram_ratio,
            "vram_total_mib": vram_total_mib,
            "queue_age_ms": queue_budget_ms * queue_ratio,
        }


def _required_number(record: Mapping[str, Any], name: str, index: int) -> float:
    if name not in record:
        raise ValueError("replay record %d is missing %s" % (index, name))
    try:
        return float(record[name])
    except (TypeError, ValueError) as exc:
        raise ValueError("replay record %d field %s is not numeric" % (index, name)) from exc


def _paths_refer_to_same_file(first: str | os.PathLike[str], second: str | os.PathLike[str]) -> bool:
    """Compare paths before opening either one, including existing hard links."""

    first_path = Path(first).resolve()
    second_path = Path(second).resolve()
    if first_path == second_path:
        return True
    try:
        return os.path.samefile(first_path, second_path)
    except (FileNotFoundError, OSError):
        return False


def _validate_output_paths(args: argparse.Namespace, input_path: Path | None) -> None:
    labelled: list[tuple[str, str | os.PathLike[str]]] = []
    if input_path is not None:
        labelled.append(("replay input", input_path))
    if args.output_jsonl:
        labelled.append(("JSONL output", args.output_jsonl))
    if args.summary_json:
        labelled.append(("summary output", args.summary_json))
    for index, (first_label, first_path) in enumerate(labelled):
        for second_label, second_path in labelled[index + 1 :]:
            if _paths_refer_to_same_file(first_path, second_path):
                raise ValueError(
                    "%s and %s must use different files" % (first_label, second_label)
                )


def evaluate_samples(
    samples: Iterable[Mapping[str, Any]],
    governor: AdaptiveQualityGovernor,
    *,
    output_jsonl: str | os.PathLike[str] | None = None,
) -> tuple[list[dict[str, Any]], float]:
    """Evaluate raw samples and optionally capture the decisions as JSONL."""

    evaluated: list[dict[str, Any]] = []
    writer = TelemetryJsonlWriter(output_jsonl) if output_jsonl else None
    started = time.perf_counter()
    try:
        if writer:
            writer.open()
        for index, sample in enumerate(samples, 1):
            timestamp = _required_number(sample, "timestamp", index)
            frame = _required_number(sample, "frame_time_ms", index)
            used = _required_number(sample, "vram_used_mib", index)
            total = _required_number(sample, "vram_total_mib", index)
            queue = _required_number(sample, "queue_age_ms", index)
            decision = governor.observe(
                timestamp=timestamp,
                frame_time_ms=frame,
                vram_used_mib=used,
                vram_total_mib=total,
                queue_age_ms=queue,
            )
            record = TelemetrySample(
                timestamp=timestamp,
                frame_time_ms=frame,
                vram_used_mib=used,
                vram_total_mib=total,
                queue_age_ms=queue,
                quality_level=decision.state.level,
                tier=decision.state.tier,
                role=str(sample.get("role", "benchmark")),
                quality_direction=decision.direction,
                quality_reason=decision.reason,
                quality_settings=decision.state.settings,
                metadata={"pressures": dict(decision.pressures)},
            ).to_dict()
            evaluated.append(record)
            if writer:
                writer.write(record)
    finally:
        if writer:
            writer.close()
    return evaluated, time.perf_counter() - started


def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.cooldown < 0:
        raise ValueError("cooldown must be non-negative")
    governor = AdaptiveQualityGovernor(
        args.tier,
        frame_budget_ms=args.frame_budget_ms,
        queue_budget_ms=args.queue_budget_ms,
        down_window=args.down_window,
        up_window=args.up_window,
        cooldown_samples=args.cooldown,
        initial_level=args.initial_level,
    )
    input_path: Path | None = None
    if args.mode == "replay":
        input_path = Path(args.input_jsonl).resolve()
    _validate_output_paths(args, input_path)

    if args.mode == "synthetic":
        total_vram = args.vram_total_mib or TIER_VRAM_MIB[args.tier]
        samples: Iterable[Mapping[str, Any]] = synthetic_samples(
            args.samples,
            pattern=args.pattern,
            seed=args.seed,
            frame_budget_ms=args.frame_budget_ms,
            queue_budget_ms=args.queue_budget_ms,
            vram_total_mib=total_vram,
        )
        source: dict[str, Any] = {
            "type": "synthetic",
            "pattern": args.pattern,
            "seed": args.seed,
        }
    else:
        assert input_path is not None
        samples = read_telemetry_jsonl(input_path)
        source = {"type": "replay", "path": str(input_path)}

    evaluated, elapsed = evaluate_samples(samples, governor, output_jsonl=args.output_jsonl)
    telemetry = summarize_telemetry(evaluated)
    result = {
        "status": "ok",
        "mode": args.mode,
        "source": source,
        "tier": args.tier,
        "processed_samples": len(evaluated),
        "processing_seconds": elapsed,
        "samples_per_second": (len(evaluated) / elapsed) if elapsed > 0 else None,
        "quality_bounds": governor.bounds.to_dict(),
        "final_quality": governor.state.to_dict(),
        "telemetry": telemetry,
        "output_jsonl": str(Path(args.output_jsonl).resolve()) if args.output_jsonl else None,
    }
    if args.summary_json:
        write_telemetry_summary(args.summary_json, result)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    try:
        result = run(args)
    except (OSError, ValueError) as exc:
        print(
            json.dumps({"status": "error", "error": str(exc)}, separators=(",", ":")),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            result,
            indent=None if args.compact else 2,
            separators=(",", ":") if args.compact else None,
            sort_keys=not args.compact,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
