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
        self.assertEqual(len(self.module.VALUE_CONTROLS), 80)
        self.assertEqual(len(self.module.PULSE_CONTROLS), 10)
        self.assertEqual(len(self.module.STATUS_CONTROLS), 7)
        self.assertEqual(len(self.module.ADAPTER_VALUE_CONTROLS), 14)
        self.assertEqual(len(self.module.ADAPTER_PULSE_CONTROLS), 4)
        controls = (
            self.module.VALUE_CONTROLS
            + self.module.PULSE_CONTROLS
            + self.module.STATUS_CONTROLS
        )
        self.assertEqual(len(controls), 97)
        self.assertEqual(len(set(controls)), 97)
        adapter_controls = (
            self.module.ADAPTER_VALUE_CONTROLS
            + self.module.ADAPTER_PULSE_CONTROLS
        )
        self.assertEqual(len(adapter_controls), 18)
        self.assertEqual(len(set(adapter_controls)), 18)

    def test_validator_restores_state_even_when_a_check_fails(self) -> None:
        self.assertIn("finally:", self.source)
        self.assertIn("callbacks.apply_all()", self.source)
        self.assertIn('"restoration_failures"', self.source)
        self.assertIn('"external_validation_required"', self.source)
        self.assertIn('"adapter_control_inventory"', self.source)
        self.assertIn('"adapter_ui_buttons"', self.source)
        self.assertIn('"adapter_restored"', self.source)
        self.assertIn('"Crossfadesec"', self.source)
        self.assertIn('"Colorenabled"', self.source)
        self.assertIn('"Resetcolor"', self.source)
        self.assertIn("Qualityprofile_points", self.source)
        self.assertIn("147456", self.source)
        self.assertIn("operator_lookup=None", self.source)
        self.assertIn("FLEXGPU_COLOR_BRIGHTNESS", self.source)
        self.assertIn("FLEXGPU_COLOR_CONTRAST", self.source)
        self.assertIn("FLEXGPU_COLOR_SATURATION", self.source)
        self.assertIn("callbacks._reset_color_grade()", self.source)
        self.assertIn('"Audioenabled_adapter"', self.source)
        self.assertIn('"Audiosource_exclusive_switch_index"', self.source)
        self.assertIn('"Camerainteractionenabled_mode_depth_sensor"', self.source)
        self.assertIn('"Camerasensorsource_femto_mega"', self.source)
        self.assertIn('"Camerasensorsource_depth_bridge_disabled"', self.source)
        self.assertIn('"Camerasensorsource_femto_enabled"', self.source)
        self.assertIn('"Cameramirrorhorizontal"', self.source)
        self.assertIn('"Sensorworkerstatus"', self.source)
        self.assertIn('"Femtostatus"', self.source)
        self.assertIn('"Sensorpositionscale"', self.source)
        self.assertIn('"Sensortrimxmetres"', self.source)
        self.assertIn('"Sensortrimymetres"', self.source)
        self.assertIn('"Sensortrimzmetres"', self.source)
        self.assertIn('"Sensortrimyawdegrees"', self.source)
        self.assertIn('"Sensortrimpitchdegrees"', self.source)
        self.assertIn('"Sensortrimrolldegrees"', self.source)
        self.assertIn('"Resetsensorcalibrationtrim"', self.source)
        self.assertIn('"Sensorcalibration_baseline_preserved"', self.source)
        self.assertIn('"Femtomirrorhorizontal"', self.source)
        self.assertIn('"Femtomirrorhorizontal_shader"', self.source)
        self.assertIn('"Femtopositionscale"', self.source)
        self.assertIn('"Femtotrimxmetres"', self.source)
        self.assertIn('"Femtotrimymetres"', self.source)
        self.assertIn('"Femtotrimzmetres"', self.source)
        self.assertIn('"Femtotrimyawdegrees"', self.source)
        self.assertIn('"Femtotrimpitchdegrees"', self.source)
        self.assertIn('"Femtotrimrolldegrees"', self.source)
        self.assertIn('"Femtoaudiencenearmetres"', self.source)
        self.assertIn('"Femtoaudiencefarmetres"', self.source)
        self.assertIn('"Resetfemtocalibrationtrim"', self.source)
        self.assertIn('"Femtocalibration_baseline_preserved"', self.source)
        self.assertIn('"Installationinteractionenabled"', self.source)
        self.assertIn('"Leftwallinteractionenabled"', self.source)
        self.assertIn('"Centerwallinteractionenabled"', self.source)
        self.assertIn('"Rightwallinteractionenabled"', self.source)
        self.assertIn('"FLEXGPU_VIEW_INTERACTION_GAIN"', self.source)
        self.assertIn('"FLEXGPU_INTERACTION_RADIUS"', self.source)
        self.assertIn('"FLEXGPU_INTERACTION_FALLOFF"', self.source)
        self.assertIn('"FLEXGPU_INTERACTION_RESPONSE"', self.source)
        self.assertIn('"FLEXGPU_INTERACTION_DECAY"', self.source)
        self.assertIn('"INSTALLATION", True, 6.3', self.source)
        self.assertIn('"CENTER", True, 8.5', self.source)
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
