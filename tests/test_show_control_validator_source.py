from __future__ import annotations

import ast
import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VALIDATOR_PATH = ROOT / "touchdesigner" / "validate_show_controls.py"
REPAIR_PATH = ROOT / "scripts" / "Repair-FlexShowEnvoyRegistry.ps1"
BRIDGE_CHECKER_PATH = ROOT / "scripts" / "Test-TDKnowledgeBridge.ps1"
REPORT_CHECKER_PATH = ROOT / "scripts" / "Test-FlexShowControlReport.ps1"


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
        self.assertEqual(len(self.module.VALUE_CONTROLS), 101)
        self.assertEqual(len(self.module.PULSE_CONTROLS), 12)
        self.assertEqual(len(self.module.STATUS_CONTROLS), 8)
        self.assertEqual(len(self.module.ADAPTER_VALUE_CONTROLS), 14)
        self.assertEqual(len(self.module.ADAPTER_PULSE_CONTROLS), 4)
        controls = (
            self.module.VALUE_CONTROLS
            + self.module.PULSE_CONTROLS
            + self.module.STATUS_CONTROLS
        )
        self.assertEqual(len(controls), 121)
        self.assertEqual(len(set(controls)), 121)
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
        self.assertIn(
            'REPORT_VERSION = "flexgpu-show-controls-validation/v2"',
            self.source,
        )
        self.assertIn("OUTPUT_DIMENSION_TARGETS", self.source)
        self.assertIn("int(node.width)", self.source)
        self.assertIn("int(node.height)", self.source)
        self.assertIn('"output_dimensions": output_dimensions', self.source)
        self.assertIn(
            '"actual_top_dimensions_are_authoritative": True',
            self.source,
        )
        self.assertGreater(
            self.source.index(
                'failures = [item for item in checks '
                'if item["status"] != "pass"]'),
            self.source.index('"restored_output_dimensions_" + name'),
        )
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
        self.assertIn("femto_fail_closed", self.source)
        self.assertIn("femto_state_is_consistent", self.source)
        self.assertIn(
            '"disconnected_hardware_is_accepted_only_when_fail_closed"',
            self.source)
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
        self.assertIn('"Experience_vr_enabled"', self.source)
        self.assertIn('"Vrinputsource"', self.source)
        self.assertIn('"Vrhandenabled"', self.source)
        self.assertIn('"FLEXGPU_VR_HAND_GAIN"', self.source)
        self.assertIn('"Vrleft_eye_dimensions"', self.source)
        self.assertIn('"Vrright_eye_dimensions"', self.source)
        self.assertIn("callbacks._reset_vr_head_pose()", self.source)
        self.assertIn("callbacks._reset_vr_hands()", self.source)
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

    def test_report_checker_rejects_license_clamped_top_dimensions(self) -> None:
        source = REPORT_CHECKER_PATH.read_text(encoding="utf-8-sig")
        for marker in (
            "flexgpu-show-controls-validation/v2",
            "ExpectedProfile = '3080ti_16gb'",
            "$result.output_dimensions.status -ne 'pass'",
            "Cooked wall output dimensions",
            "actual=$($_.Value.actual -join 'x')",
            "$valueControls.Count -ne 101",
            "$pulseControls.Count -ne 12",
            "$statusControls.Count -ne 8",
            "Experience_installation_vr_disabled",
            "vr_foundation = 'desktop_mock_pass'",
            "headset_validation = 'not_performed'",
        ):
            self.assertIn(marker, source)
        for forbidden in ("Stop-Process", "Start-Process", "git "):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
