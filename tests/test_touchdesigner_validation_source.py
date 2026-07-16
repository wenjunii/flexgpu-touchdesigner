from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "touchdesigner" / "validate_project.py"


def load_module():
    spec = importlib.util.spec_from_file_location("flexgpu_td_validate", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load TouchDesigner validation module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class TouchDesignerValidationSourceTests(unittest.TestCase):
    def test_validation_module_is_import_safe_and_targets_v121(self) -> None:
        module = load_module()
        self.assertEqual(module.VALIDATION_VERSION, "flexgpu-td-validation/v1")
        self.assertEqual(module.validate.__defaults__[0], "1.2.1")
        self.assertIn(
            "/project1/flexgpu/WORKING_PIPELINE/OUT_INSTALLATION",
            module.REQUIRED_OPERATORS,
        )
        self.assertIn(
            "/project1/flexgpu/WORKING_PIPELINE/TELEMETRY/LIVE_HEALTH",
            module.REQUIRED_OPERATORS,
        )
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertIn('"managed_shader_compilation"', source)
        self.assertIn('"managed_shader_cook"', source)
        self.assertIn('"active_output_dimensions"', source)
        self.assertIn('"active_visual_signal"', source)
        self.assertIn('"sensor_disabled_contract"', source)
        self.assertIn('mode.val = "disabled"', source)
        self.assertIn('mode.val = previous_mode', source)
        self.assertIn('"radiusx", "radiusy", "centerx", "centery"', source)
        self.assertIn('("OUT_SENSOR_MASK", "OUT_SENSOR_POSITION", "OUT_INTERACTION")', source)
        self.assertIn('method(delayed=False)', source)
        self.assertIn('"has_signal": maximum > 1e-5 and span > 1e-6', source)
        self.assertIn("os.unlink(output)", source)
        self.assertIn("os.path.getsize(output)", source)
        self.assertIn("METRIC_RENDER_LEFT_EYE", source)

    def test_combined_mode_has_exact_core_installation_and_stereo_contracts(self) -> None:
        module = load_module()
        state = {
            "world_active": True,
            "installation_active": True,
            "vr_active": True,
            "geometry_resolution": 384,
            "installation_width": 1280,
            "installation_height": 720,
            "stereo_width": 2560,
            "stereo_height": 720,
        }
        self.assertEqual(
            module._active_output_dimensions(state),
            {
                "OUT_POSITION": (384, 384),
                "OUT_COLOR": (384, 384),
                "OUT_INTERACTION": (384, 384),
                "OUT_INSTALLATION": (1280, 720),
                "OUT_LEFT_EYE": (1280, 720),
                "OUT_RIGHT_EYE": (1280, 720),
                "OUT_STEREO_PREVIEW": (2560, 720),
            },
        )
        self.assertEqual(
            module._active_signal_outputs(state),
            (
                "OUT_INSTALLATION",
                "OUT_LEFT_EYE",
                "OUT_RIGHT_EYE",
                "OUT_STEREO_PREVIEW",
            ),
        )
        self.assertEqual(
            module._active_capture_outputs(state),
            ("OUT_INSTALLATION", "OUT_STEREO_PREVIEW"),
        )

    def test_active_dimension_contract_rejects_boolean_or_nonpositive_values(self) -> None:
        module = load_module()
        base = {
            "world_active": True,
            "installation_active": False,
            "vr_active": False,
        }
        for value in (True, 0, -1, "invalid"):
            with self.subTest(value=value):
                with self.assertRaises((TypeError, ValueError)):
                    module._active_output_dimensions(
                        dict(base, geometry_resolution=value)
                    )

    def test_experience_activation_contract_requires_the_complete_world_mode(self) -> None:
        module = load_module()
        self.assertEqual(
            module._experience_activation_contract("installation"),
            {
                "world_active": True,
                "installation_active": True,
                "vr_active": False,
            },
        )
        self.assertEqual(
            module._experience_activation_contract("vr"),
            {
                "world_active": True,
                "installation_active": False,
                "vr_active": True,
            },
        )
        self.assertEqual(
            module._experience_activation_contract("combined"),
            {
                "world_active": True,
                "installation_active": True,
                "vr_active": True,
            },
        )
        with self.assertRaisesRegex(ValueError, "unsupported expected experience"):
            module._experience_activation_contract("invalid")

    def test_required_operator_types_fail_closed_on_wrong_named_nodes(self) -> None:
        module = load_module()
        self.assertEqual(
            module.EXPECTED_OPERATOR_TYPES[
                "/project1/flexgpu/WORKING_PIPELINE/OUT_INSTALLATION"
            ],
            ("null",),
        )
        self.assertEqual(
            module.EXPECTED_OPERATOR_TYPES[
                "/project1/flexgpu/STARTUP/runtime_helpers"
            ],
            ("text",),
        )

    def test_shader_error_recognition_requires_both_compile_and_error(self) -> None:
        module = load_module()
        self.assertTrue(module._is_shader_compile_error("GLSL Compile Error"))
        self.assertTrue(module._is_shader_compile_error("error while compiling shader"))
        self.assertFalse(module._is_shader_compile_error("compiled successfully"))

    def test_vr_validation_requires_metric_stereo_and_distinct_eye_contracts(self) -> None:
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertIn('"missing_active_metric_renders"', source)
        self.assertIn('"mono_fallback_is_not_stereo"', source)
        self.assertIn('"invalid_parallel_eye_translation"', source)
        self.assertIn('"stereo_eye_difference"', source)
        self.assertIn("left/right eye images are identical", source)

    def test_atomic_report_writer_replaces_complete_json(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "report.json"
            result = module._atomic_json(path, {"status": "pass", "count": 1})
            # GitHub's Windows runners may expose the same temporary directory
            # through both its 8.3 short name and long name.  Compare filesystem
            # identity instead of requiring those equivalent spellings to match.
            self.assertTrue(os.path.samefile(result, path))
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")),
                {"count": 1, "status": "pass"},
            )
            leftovers = list(path.parent.glob("*.tmp-*"))
            self.assertEqual(leftovers, [])

    def test_validate_refuses_to_run_without_touchdesigner_network(self) -> None:
        module = load_module()
        module._op = lambda _path: None
        with self.assertRaisesRegex(RuntimeError, "not built"):
            module.validate()


if __name__ == "__main__":
    unittest.main()
