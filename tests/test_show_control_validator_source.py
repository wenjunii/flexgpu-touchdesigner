from __future__ import annotations

import ast
import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VALIDATOR_PATH = ROOT / "touchdesigner" / "validate_show_controls.py"
REPAIR_PATH = ROOT / "scripts" / "Repair-FlexShowEnvoyRegistry.ps1"
BRIDGE_CHECKER_PATH = ROOT / "scripts" / "Test-TDKnowledgeBridge.ps1"


def load_validator():
    spec = importlib.util.spec_from_file_location(
        "validate_show_controls", VALIDATOR_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load validate_show_controls.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ShowControlValidatorSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_validator()
        cls.source = VALIDATOR_PATH.read_text(encoding="utf-8")

    def test_validator_is_import_safe_and_covers_every_public_control(self) -> None:
        ast.parse(self.source)
        self.assertEqual(len(self.module.VALUE_CONTROLS), 45)
        self.assertEqual(len(self.module.PULSE_CONTROLS), 6)
        self.assertEqual(len(self.module.STATUS_CONTROLS), 4)
        controls = (
            self.module.VALUE_CONTROLS
            + self.module.PULSE_CONTROLS
            + self.module.STATUS_CONTROLS
        )
        self.assertEqual(len(controls), 55)
        self.assertEqual(len(set(controls)), 55)

    def test_validator_restores_state_even_when_a_check_fails(self) -> None:
        self.assertIn("finally:", self.source)
        self.assertIn("callbacks.apply_all()", self.source)
        self.assertIn('"restoration_failures"', self.source)
        self.assertIn('"external_validation_required"', self.source)
        self.assertIn("Qualityprofile_points", self.source)
        self.assertIn("147456", self.source)
        self.assertIn("operator_lookup=None", self.source)
        self.assertIn("FLEXGPU_COLOR_BRIGHTNESS", self.source)
        self.assertIn("FLEXGPU_COLOR_CONTRAST", self.source)
        self.assertIn("FLEXGPU_COLOR_SATURATION", self.source)
        self.assertIn("callbacks._reset_color_grade()", self.source)
        self.assertIn('"Audioenabled_adapter"', self.source)
        self.assertIn('"Audiosource_exclusive_switch_index"', self.source)
        self.assertIn(
            '_restore_parameter_state(\n'
            '                audio_switch, "index"',
            self.source,
        )
        self.assertIn(
            '_restore_parameter_state(\n'
            '                audio_out, "active"',
            self.source,
        )

    def test_envoy_repair_is_dry_run_first_and_never_kills_touchdesigner(self) -> None:
        source = REPAIR_PATH.read_text(encoding="utf-8-sig")
        checker = BRIDGE_CHECKER_PATH.read_text(encoding="utf-8-sig")
        for marker in (
            "[switch]$Repair",
            "Get-NetTCPConnection",
            "OwningProcess -eq $processId",
            "Copy-Item",
            "Move-Item",
            "status = $status",
        ):
            self.assertIn(marker, source)
        self.assertNotIn("Stop-Process", source)
        self.assertNotIn("taskkill", source.lower())
        self.assertIn("Repair-FlexShowEnvoyRegistry.ps1", checker)


if __name__ == "__main__":
    unittest.main()
