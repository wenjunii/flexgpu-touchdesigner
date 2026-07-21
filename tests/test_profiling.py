from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from flexgpu.models import DiscoveryError  # noqa: E402
from flexgpu.profiling import (  # noqa: E402
    PROFILE_VERSION,
    build_hardware_profile,
    parse_runtime_profile_csv,
    recommend_role_placement,
    write_hardware_profile,
)


PROFILE_CSV = """
0, GPU-low, 00000000:01:00.0, NVIDIA GeForce RTX 3080 Ti Laptop GPU, 16384, 1024, 5, 60, 65.0, 150.0, 1450, 7000, Enabled, P2, 610.62
1, GPU-high, 00000000:02:00.0, NVIDIA GeForce RTX 4090, 24564, 1000, 2, 55, 120.0, 450.0, 2500, 10500, Disabled, P0, 610.62
"""


class HardwareProfilingTests(unittest.TestCase):
    def test_runtime_profile_parser_preserves_live_metrics(self) -> None:
        profiles = parse_runtime_profile_csv(PROFILE_CSV)
        self.assertEqual([profile.index for profile in profiles], [0, 1])
        self.assertEqual(profiles[0].memory_headroom_mib, 15360)
        self.assertTrue(profiles[0].display_active)
        self.assertFalse(profiles[1].display_active)
        self.assertGreater(profiles[1].score("ai"), profiles[0].score("ai"))

    def test_na_power_fields_are_allowed_for_laptop_gpus(self) -> None:
        csv_text = PROFILE_CSV.splitlines()[1].replace("65.0, 150.0", "[N/A], [N/A]")
        profile = parse_runtime_profile_csv(csv_text)[0]
        self.assertIsNone(profile.power_draw_w)
        self.assertIsNone(profile.power_limit_w)

    def test_dual_role_recommendation_prefers_capacity_for_ai_and_display_for_render(self) -> None:
        recommendation = recommend_role_placement(
            parse_runtime_profile_csv(PROFILE_CSV), "dual_local"
        )
        self.assertEqual(recommendation["assignment"]["ai_uuid"], "GPU-high")
        self.assertEqual(recommendation["assignment"]["render_uuid"], "GPU-low")
        self.assertIn("never reassign", recommendation["caveat"])

    def test_single_role_recommendation_uses_one_stable_uuid(self) -> None:
        recommendation = recommend_role_placement(
            parse_runtime_profile_csv(PROFILE_CSV), "single"
        )
        self.assertEqual(
            recommendation["assignment"]["ai_uuid"],
            recommendation["assignment"]["render_uuid"],
        )

    def test_profile_rejects_bad_ranges_duplicates_and_field_counts(self) -> None:
        duplicate = PROFILE_CSV + PROFILE_CSV.splitlines()[1] + "\n"
        bad_utilization = PROFILE_CSV.replace(", 5, 60,", ", 101, 60,")
        short = "0, GPU-only\n"
        for payload in (duplicate, bad_utilization, short):
            with self.subTest(payload=payload):
                with self.assertRaises(DiscoveryError):
                    parse_runtime_profile_csv(payload)

    def test_profile_write_is_atomic_and_requires_explicit_overwrite(self) -> None:
        payload = build_hardware_profile(
            parse_runtime_profile_csv(PROFILE_CSV), "dual_local"
        )
        self.assertEqual(payload["version"], PROFILE_VERSION)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hardware.json"
            write_hardware_profile(path, payload)
            written = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(written["recommendation"]["topology"], "dual_local")
            with self.assertRaises(FileExistsError):
                write_hardware_profile(path, payload)
            write_hardware_profile(path, payload, overwrite=True)
            self.assertFalse(path.with_name(path.name + ".tmp").exists())


if __name__ == "__main__":
    unittest.main()
