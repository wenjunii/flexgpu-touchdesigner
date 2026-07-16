from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock


import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from flexgpu.config import load_config, load_config_data, validate_config  # noqa: E402
from flexgpu.models import ConfigError  # noqa: E402


def base_profile() -> dict[str, object]:
    return {
        "topology": "single",
        "experience": "installation",
        "completion": "hybrid",
        "tier": "auto",
        "gpu": {"ai": "auto", "render": "auto"},
        "processes": {"world": {"command": ["python", "show.py"]}},
        "transport": {"type": "local"},
    }


class RuntimeConfigSectionTests(unittest.TestCase):
    def test_advanced_runtime_sections_are_dependency_free_validated(self) -> None:
        profile = base_profile()
        profile.update(
            {
                "adaptive": {
                    "enabled": True,
                    "levels": 5,
                    "initial_level": 4,
                    "frame_budget_ms": 16.667,
                    "queue_budget_ms": 200,
                    "down_window": 3,
                    "up_window": 120,
                    "cooldown_samples": 30,
                    "thresholds": {
                        "frame_low": 0.8,
                        "frame_high": 1.1,
                        "critical_frame": 2.0,
                    },
                },
                "telemetry": {
                    "enabled": True,
                    "jsonl_path": "runtime/show.jsonl",
                    "summary_path": "runtime/show-summary.json",
                    "sample_interval_frames": 1,
                    "flush_every": 60,
                },
                "source": {
                    "mode": "streamdiffusion",
                    "streamdiffusion_tox": "local-components/StreamDiffusionTD.tox",
                    "rgb_operator": "out_rgb",
                    "depth_operator": "out_depth",
                    "confidence_operator": "out_confidence",
                    "frame_state_operator": "frame_state",
                    "camera_metadata_operator": "camera_metadata",
                    "calibration_path": "calibration/source.json",
                    "auto_load_tox": True,
                    "stale_timeout_ms": 750,
                },
                "sensor": {
                    "mode": "depth_sensor",
                    "adapter_tox": "local-components/SensorAdapter.tox",
                    "position_operator": "out_position",
                    "confidence_operator": "out_confidence",
                    "frame_state_operator": "frame_state",
                    "calibration_path": "calibration/sensor.json",
                    "auto_load_tox": True,
                    "interaction_radius_m": 0.45,
                    "force_gain": 1.2,
                    "stale_timeout_ms": 1000,
                },
                "render": {
                    "point_size_px": 3.0,
                    "point_budget": 120000,
                    "installation_width": 1920,
                    "installation_height": 1080,
                    "installation_fps": 60,
                    "stereo_width": 2560,
                    "stereo_height": 720,
                    "vr_fps": 72,
                    "fog_density": 0.35,
                    "procedural_mix": 0.7,
                },
                "supervisor": {
                    "heartbeat_timeout_ms": 2500,
                    "readiness_timeout_ms": 10000,
                    "require_ready": True,
                },
            }
        )
        config = validate_config(profile)
        self.assertEqual(config.raw["source"]["frame_state_operator"], "frame_state")
        self.assertEqual(config.raw["sensor"]["auto_load_tox"], True)
        self.assertEqual(config.supervisor["heartbeat_timeout_ms"], 2500)
        self.assertEqual(config.supervisor["readiness_timeout_ms"], 10000)
        self.assertIs(config.supervisor["require_ready"], True)

    def test_supervisor_defaults_are_backwards_compatible(self) -> None:
        config = validate_config(base_profile())
        self.assertEqual(
            config.supervisor,
            {
                "heartbeat_timeout_ms": 5000,
                "readiness_timeout_ms": 0,
                "require_ready": False,
            },
        )

    def test_supervisor_contract_rejects_unknown_types_and_bounds(self) -> None:
        cases = (
            {"heartbeat_timeout_ms": 249},
            {"readiness_timeout_ms": -1},
            {"require_ready": 1},
            {"unknown": True},
        )
        for supervisor in cases:
            with self.subTest(supervisor=supervisor):
                profile = base_profile()
                profile["supervisor"] = supervisor
                with self.assertRaises(ConfigError):
                    validate_config(profile)

    def test_unknown_top_level_and_runtime_fields_fail_closed(self) -> None:
        cases = []
        unknown_top = base_profile()
        unknown_top["mystery"] = True
        cases.append(unknown_top)
        unknown_source = base_profile()
        unknown_source["source"] = {"mode": "demo", "mystery": True}
        cases.append(unknown_source)
        unknown_threshold = base_profile()
        unknown_threshold["adaptive"] = {"thresholds": {"mystery": 1.0}}
        cases.append(unknown_threshold)
        null_section = base_profile()
        null_section["telemetry"] = None
        cases.append(null_section)
        duplicate_alias = base_profile()
        duplicate_alias["profile"] = "4090"
        cases.append(duplicate_alias)
        for profile in cases:
            with self.subTest(profile=profile):
                with self.assertRaises(ConfigError):
                    validate_config(profile)

    def test_replay_and_auto_load_dependencies_are_required(self) -> None:
        source_replay = base_profile()
        source_replay["source"] = {"mode": "replay"}
        source_auto = base_profile()
        source_auto["source"] = {"mode": "streamdiffusion", "auto_load_tox": True}
        sensor_replay = base_profile()
        sensor_replay["sensor"] = {"mode": "replay"}
        sensor_auto = base_profile()
        sensor_auto["sensor"] = {"mode": "depth_sensor", "auto_load_tox": True}
        source_missing_output = base_profile()
        source_missing_output["source"] = {
            "mode": "streamdiffusion",
            "auto_load_tox": True,
            "streamdiffusion_tox": "local-components/source.tox",
        }
        sensor_missing_output = base_profile()
        sensor_missing_output["sensor"] = {
            "mode": "depth_sensor",
            "auto_load_tox": True,
            "adapter_tox": "local-components/sensor.tox",
        }
        for profile in (
            source_replay,
            source_auto,
            source_missing_output,
            sensor_replay,
            sensor_auto,
            sensor_missing_output,
        ):
            with self.subTest(profile=profile):
                with self.assertRaises(ConfigError):
                    validate_config(profile)

    def test_adaptive_relations_and_runtime_types_fail_closed(self) -> None:
        profile = base_profile()
        profile["adaptive"] = {
            "enabled": 1,
            "levels": 3,
            "initial_level": 3,
            "thresholds": {
                "frame_low": 1.2,
                "frame_high": 1.1,
                "critical_frame": 1.0,
            },
        }
        profile["sensor"] = {"interaction_radius_m": 0}
        profile["render"] = {"point_budget": True, "procedural_mix": 1.5}
        with self.assertRaises(ConfigError) as captured:
            validate_config(profile)
        message = str(captured.exception)
        self.assertIn("adaptive.enabled", message)
        self.assertIn("initial_level", message)
        self.assertIn("frame_low", message)
        self.assertIn("interaction_radius_m", message)
        self.assertIn("point_budget", message)

    def test_huge_runtime_numbers_are_reported_as_config_errors(self) -> None:
        profile = base_profile()
        profile["render"] = {"point_size_px": 10**4000}
        with self.assertRaisesRegex(ConfigError, "point_size_px.*finite"):
            validate_config(profile)

    def test_telemetry_outputs_must_be_distinct(self) -> None:
        profile = base_profile()
        profile["telemetry"] = {
            "jsonl_path": "runtime/telemetry.json",
            "summary_path": "runtime/./telemetry.json",
        }
        with self.assertRaisesRegex(ConfigError, "must be different"):
            validate_config(profile)

    def test_telemetry_outputs_compare_resolved_config_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "show.json"
            profile = base_profile()
            profile["telemetry"] = {
                "jsonl_path": str(Path(directory) / "telemetry.jsonl"),
                "summary_path": "telemetry.jsonl",
            }
            with self.assertRaisesRegex(ConfigError, "must be different"):
                validate_config(profile, str(config_path))

    def test_launcher_json_loader_rejects_duplicate_keys_and_nonfinite_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "show.json"
            for payload in ('{"tier":"auto","tier":"custom"}', '{"value":NaN}'):
                with self.subTest(payload=payload):
                    path.write_text(payload, encoding="utf-8")
                    with self.assertRaises(ConfigError):
                        load_config_data(path)

    def test_toml_process_defaults_and_roles_reject_unknown_fields(self) -> None:
        try:
            import tomllib  # noqa: F401
        except ModuleNotFoundError:
            self.skipTest("tomllib is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "show.toml"
            path.write_text(
                'topology = "single"\n'
                '[processes.defaults]\n'
                'gpu_affinty = false\n'
                '[processes.world]\n'
                'command = ["python", "show.py"]\n'
                'workdir = "runtime"\n',
                encoding="utf-8",
            )
            with self.assertRaises(ConfigError) as raised:
                load_config(path)
            message = str(raised.exception)
            self.assertIn("processes.defaults has unsupported field 'gpu_affinty'", message)
            self.assertIn("processes.world has unsupported field 'workdir'", message)

    def test_toml_without_standard_library_support_is_a_config_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "show.toml"
            path.write_text('topology = "single"\n', encoding="utf-8")
            original_import = __import__

            def controlled_import(name, *args, **kwargs):
                if name == "tomllib":
                    raise ModuleNotFoundError("tomllib is unavailable")
                return original_import(name, *args, **kwargs)

            with mock.patch("builtins.__import__", side_effect=controlled_import):
                with self.assertRaisesRegex(ConfigError, "tomllib"):
                    load_config_data(path)


if __name__ == "__main__":
    unittest.main()
