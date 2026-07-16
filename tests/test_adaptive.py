from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "src"))

from flexgpu.adaptive import (  # noqa: E402
    AdaptiveQualityGovernor,
    TelemetryJsonlWriter,
    TelemetrySample,
    quality_bounds_for_tier,
    read_telemetry_jsonl,
    summarize_telemetry,
    write_telemetry_summary,
)
from benchmark_flexshow import main as benchmark_main  # noqa: E402


class AdaptiveQualityTests(unittest.TestCase):
    def test_tier_bounds_are_exact_and_keep_display_refresh_fixed(self) -> None:
        expected_points = {
            "3080ti_16gb": (60_000, 120_000),
            "4090": (100_000, 250_000),
            "5090": (150_000, 262_144),
        }
        for tier, point_bounds in expected_points.items():
            with self.subTest(tier=tier):
                bounds = quality_bounds_for_tier(tier)
                minimum = bounds.settings_for_level(0)
                maximum = bounds.settings_for_level(bounds.levels - 1)
                self.assertEqual((minimum["max_points"], maximum["max_points"]), point_bounds)
                self.assertEqual(minimum["vr_refresh_hz"], maximum["vr_refresh_hz"])
                levels = [
                    bounds.settings_for_level(level)["max_points"]
                    for level in range(bounds.levels)
                ]
                self.assertEqual(levels, sorted(levels))

    def test_sustained_overload_degrades_only_after_down_window(self) -> None:
        governor = AdaptiveQualityGovernor(
            "3080ti_16gb",
            frame_budget_ms=10,
            queue_budget_ms=100,
            down_window=3,
            up_window=5,
            cooldown_samples=0,
        )
        decisions = [
            governor.observe(
                timestamp=index,
                frame_time_ms=11,
                vram_used_mib=8_000,
                vram_total_mib=16_000,
                queue_age_ms=20,
            )
            for index in range(3)
        ]
        self.assertEqual([decision.changed for decision in decisions], [False, False, True])
        self.assertEqual(decisions[-1].direction, "down")
        self.assertEqual(decisions[-1].reason, "sustained_overload")
        self.assertEqual(decisions[-1].state.level, 3)

    def test_sustained_headroom_recovers_slowly(self) -> None:
        governor = AdaptiveQualityGovernor(
            "4090",
            frame_budget_ms=10,
            queue_budget_ms=100,
            initial_level=1,
            down_window=2,
            up_window=3,
            cooldown_samples=0,
        )
        decisions = [
            governor.observe(
                timestamp=index,
                frame_time_ms=5,
                vram_used_mib=10_000,
                vram_total_mib=24_000,
                queue_age_ms=20,
            )
            for index in range(3)
        ]
        self.assertEqual([decision.changed for decision in decisions], [False, False, True])
        self.assertEqual(decisions[-1].direction, "up")
        self.assertEqual(decisions[-1].state.level, 2)

    def test_dead_band_prevents_quality_oscillation(self) -> None:
        governor = AdaptiveQualityGovernor(
            "5090",
            frame_budget_ms=10,
            queue_budget_ms=100,
            initial_level=2,
            down_window=2,
            up_window=2,
            cooldown_samples=0,
        )
        for index in range(20):
            decision = governor.observe(
                timestamp=index,
                frame_time_ms=9,
                vram_used_mib=26_000,
                vram_total_mib=32_000,
                queue_age_ms=80,
            )
            self.assertFalse(decision.changed)
            self.assertEqual(decision.reason, "hysteresis")
        self.assertEqual(governor.state.level, 2)

    def test_critical_vram_bypasses_cooldown(self) -> None:
        governor = AdaptiveQualityGovernor(
            "3080ti_16gb",
            frame_budget_ms=10,
            queue_budget_ms=100,
            down_window=1,
            up_window=10,
            cooldown_samples=50,
        )
        first = governor.observe(
            timestamp=0,
            frame_time_ms=11,
            vram_used_mib=10_000,
            vram_total_mib=16_000,
            queue_age_ms=20,
        )
        second = governor.observe(
            timestamp=1,
            frame_time_ms=8,
            vram_used_mib=15_900,
            vram_total_mib=16_000,
            queue_age_ms=20,
        )
        self.assertEqual(first.state.level, 3)
        self.assertGreater(first.cooldown_remaining, 0)
        self.assertTrue(second.changed)
        self.assertEqual(second.reason, "critical_vram")
        self.assertEqual(second.state.level, 2)

    def test_cooldown_holds_the_configured_number_of_observations(self) -> None:
        governor = AdaptiveQualityGovernor(
            "3080ti_16gb",
            frame_budget_ms=10,
            queue_budget_ms=100,
            down_window=1,
            up_window=10,
            cooldown_samples=1,
        )
        decisions = [
            governor.observe(
                timestamp=index,
                frame_time_ms=11,
                vram_used_mib=8_000,
                vram_total_mib=16_000,
                queue_age_ms=20,
            )
            for index in range(3)
        ]
        self.assertEqual([decision.changed for decision in decisions], [True, False, True])
        self.assertEqual(decisions[1].reason, "cooldown")
        self.assertEqual(decisions[1].cooldown_remaining, 0)

    def test_invalid_metrics_are_rejected(self) -> None:
        governor = AdaptiveQualityGovernor("3080ti_16gb")
        with self.assertRaises(ValueError):
            governor.observe(
                frame_time_ms=float("nan"),
                vram_used_mib=1,
                vram_total_mib=16_000,
                queue_age_ms=1,
            )
        with self.assertRaises(ValueError):
            governor.observe(
                frame_time_ms=10,
                vram_used_mib=1,
                vram_total_mib=0,
                queue_age_ms=1,
            )


class TelemetryTests(unittest.TestCase):
    def _sample(self, timestamp: float, level: int) -> TelemetrySample:
        return TelemetrySample(
            timestamp=timestamp,
            frame_time_ms=10 + timestamp,
            vram_used_mib=8_000 + timestamp,
            vram_total_mib=16_000,
            queue_age_ms=20 + timestamp,
            quality_level=level,
            tier="3080ti_16gb",
        )

    def test_jsonl_round_trip_summary_and_atomic_summary_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            jsonl_path = Path(directory) / "nested" / "capture.jsonl"
            summary_path = Path(directory) / "summary.json"
            samples = [self._sample(0, 4), self._sample(1, 3), self._sample(2, 3)]
            with TelemetryJsonlWriter(jsonl_path) as writer:
                for sample in samples:
                    writer.write(sample)
            records = list(read_telemetry_jsonl(jsonl_path))
            self.assertEqual(len(records), 3)
            summary = summarize_telemetry(records)
            self.assertEqual(summary["count"], 3)
            self.assertEqual(summary["duration_s"], 2)
            self.assertEqual(summary["quality_changes"], 1)
            self.assertAlmostEqual(summary["metrics"]["frame_time_ms"]["mean"], 11)
            self.assertAlmostEqual(summary["metrics"]["vram_utilization"]["max"], 8002 / 16000)
            write_telemetry_summary(summary_path, summary)
            self.assertEqual(json.loads(summary_path.read_text(encoding="utf-8"))["count"], 3)
            self.assertEqual(list(Path(directory).glob(".*.tmp")), [])

    def test_reader_reports_malformed_line_number(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.jsonl"
            path.write_text('{"ok":1}\nnot-json\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, r":2:"):
                list(read_telemetry_jsonl(path))

    def test_empty_summary_is_well_formed(self) -> None:
        summary = summarize_telemetry([])
        self.assertEqual(summary["count"], 0)
        self.assertEqual(summary["duration_s"], 0)
        self.assertEqual(summary["metrics"], {})

    def test_sparse_vram_fields_are_not_paired_across_different_records(self) -> None:
        summary = summarize_telemetry(
            [{"vram_used_mib": 8_000}, {"vram_total_mib": 4_000}]
        )
        self.assertNotIn("vram_utilization", summary["metrics"])


class BenchmarkCliTests(unittest.TestCase):
    def test_synthetic_capture_can_be_replayed_dependency_free(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            capture = Path(directory) / "capture.jsonl"
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                status = benchmark_main(
                    [
                        "synthetic",
                        "--samples",
                        "24",
                        "--pattern",
                        "spike",
                        "--output-jsonl",
                        str(capture),
                        "--compact",
                    ]
                )
            self.assertEqual(status, 0)
            result = json.loads(output.getvalue())
            self.assertEqual(result["processed_samples"], 24)
            self.assertTrue(capture.is_file())

            replay_output = io.StringIO()
            with contextlib.redirect_stdout(replay_output):
                replay_status = benchmark_main(
                    ["replay", str(capture), "--tier", "3080ti_16gb", "--compact"]
                )
            self.assertEqual(replay_status, 0)
            replay = json.loads(replay_output.getvalue())
            self.assertEqual(replay["processed_samples"], 24)
            self.assertEqual(replay["source"]["type"], "replay")

    def test_replay_output_cannot_truncate_its_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            capture = Path(directory) / "capture.jsonl"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    benchmark_main(
                        ["synthetic", "--samples", "3", "--output-jsonl", str(capture), "--compact"]
                    ),
                    0,
                )
            original = capture.read_bytes()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
                status = benchmark_main(
                    ["replay", str(capture), "--output-jsonl", str(capture), "--compact"]
                )
            self.assertEqual(status, 2)
            self.assertIn("must use different files", stderr.getvalue())
            self.assertEqual(capture.read_bytes(), original)

    def test_jsonl_and_summary_outputs_must_be_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result.json"
            stderr = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
                status = benchmark_main(
                    [
                        "synthetic",
                        "--samples",
                        "3",
                        "--output-jsonl",
                        str(output),
                        "--summary-json",
                        str(output),
                        "--compact",
                    ]
                )
            self.assertEqual(status, 2)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
