from __future__ import annotations

import ast
import importlib.util
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


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
    def test_parameter_restoration_preserves_motion_expressions_and_bindings(self):
        module = load_validator()
        par = SimpleNamespace(val=7, expr='absTime.seconds', mode='ParMode.EXPRESSION',
                              bindExpr="op('driver').par.X", eval=lambda: 27)
        node = SimpleNamespace(par=SimpleNamespace(tx=par))
        before = module._parameter_state(node, 'tx')
        par.val, par.expr, par.mode, par.bindExpr = 99, '', 'ParMode.CONSTANT', ''
        module._restore_parameter_state(node, 'tx', before)
        self.assertEqual((par.val, par.expr, par.mode, par.bindExpr),
                         (7, 'absTime.seconds', 'ParMode.EXPRESSION', "op('driver').par.X"))
        after = dict(before, value=99, raw_value=99)
        self.assertTrue(module._same_camera_states({'CAMERA_LEFT': {'tx': before}},
                                                  {'CAMERA_LEFT': {'tx': after}}))
        after['expr'] = 'a different motion'
        self.assertFalse(module._same_camera_states({'CAMERA_LEFT': {'tx': before}},
                                                   {'CAMERA_LEFT': {'tx': after}}))

    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_validator()
        cls.source = VALIDATOR_PATH.read_text(encoding="utf-8")

    def test_validator_is_import_safe_and_covers_every_public_control(self) -> None:
        ast.parse(self.source)
        self.assertEqual(len(self.module.VALUE_CONTROLS), 101)
        self.assertEqual(len(self.module.PULSE_CONTROLS), 12)
        self.assertEqual(len(self.module.STATUS_CONTROLS), 8)
        self.assertEqual(len(self.module.ADAPTER_VALUE_CONTROLS), 15)
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
        self.assertEqual(len(adapter_controls), 19)
        self.assertEqual(len(set(adapter_controls)), 19)

    def test_validator_restores_state_even_when_a_check_fails(self) -> None:
        self.assertIn("finally:", self.source)
        self.assertIn("callbacks.apply_all()", self.source)
        self.assertIn('"restoration_failures"', self.source)
        self.assertIn('"external_validation_required"', self.source)
        self.assertIn('"complete_live_acceptance": False', self.source)
        self.assertIn('"acceptance_scope": "public_control_mapping_and_output_dimensions"', self.source)
        self.assertIn("Source tests verify wrapper contracts, not live button execution.", self.source)
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
        self.assertIn('"adapter_Visualpath_connector_"', self.source)
        self.assertIn('"adapter_Visualpath_scene_"', self.source)
        self.assertIn('"Humanfigurejson"', self.source)
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
            "$valueControls.Count -ne $expectedValues",
            "$pulseControls.Count -ne $expectedPulses",
            "$statusControls.Count -ne $expectedStatuses",
            "Experience_installation_vr_disabled",
            "'desktop_mock_pass' } else { 'not_installed'",
            "headset_validation = 'not_performed'",
            "complete_live_acceptance = $false",
            "acceptance_scope = 'public_control_mapping_and_output_dimensions'",
        ):
            self.assertIn(marker, source)
        for forbidden in ("Stop-Process", "Start-Process", "git "):
            self.assertNotIn(forbidden, source)

    def test_host_audit_keeps_status_inventory_and_untested_buttons_explicit(self) -> None:
        source = REPORT_CHECKER_PATH.read_text(encoding="utf-8-sig")
        for marker in ('streamdiffusion_host_status', 'Sdpromptstatus', 'Sdoffloadstatus',
                       'streamdiffusion_host_structure', 'structure_pass_buttons_not_tested',
                       'external_validation_required'):
            self.assertIn(marker, source)
        self.assertIn('"streamdiffusion_host_status": list(SD_HOST_STATUS_CONTROLS)', self.source)
        self.assertIn('Turn off Follow Audio / Podcast Prompts', self.source)
        self.assertLess(self.source.index('Turn off Follow Audio / Podcast Prompts'),
                        self.source.index('snapshot = _snapshot(control, value_controls)'))
        self.assertLess(self.source.index('host_runtime.module.tick(host)'),
                        self.source.index('host_report = host_validation.validate('))

    def test_older_destination_requires_explicit_complete_feature_groups(self):
        all_names = set(self.module.VALUE_CONTROLS + self.module.PULSE_CONTROLS +
                        self.module.STATUS_CONTROLS)
        absent = set().union(*map(set, self.module.OPTIONAL_CONTROL_GROUPS.values()))
        control = SimpleNamespace(customPars=[SimpleNamespace(name=n) for n in all_names - absent])
        pipeline = SimpleNamespace(op=lambda path: None)
        with self.assertRaisesRegex(RuntimeError, 'allow_absent_optional'):
            self.module.control_inventory(control, pipeline)
        values, pulses, statuses, features = self.module.control_inventory(control, pipeline, True)
        self.assertEqual((len(values), len(pulses), len(statuses)), (70, 9, 7))
        self.assertEqual(set(features.values()), {'not_installed'})
        control.customPars.append(SimpleNamespace(name='Vrinputsource'))
        with self.assertRaisesRegex(RuntimeError, 'Partially installed'):
            self.module.control_inventory(control, pipeline, True)

    def test_vr_controls_require_matching_component_and_profiles_are_canonical(self):
        names = self.module.VALUE_CONTROLS + self.module.PULSE_CONTROLS + self.module.STATUS_CONTROLS
        control = SimpleNamespace(customPars=[SimpleNamespace(name=n) for n in names])
        with self.assertRaisesRegex(RuntimeError, 'installed together'):
            self.module.control_inventory(control, SimpleNamespace(op=lambda path: None), True)
        for profile in ('5090_32gb', '4090_24gb', 'unknown'):
            with self.assertRaisesRegex(ValueError, 'canonical'):
                self.module.validate(expected_profile=profile)
        self.assertIn('Show Control profile does not match the requested machine', self.source)
        self.assertIn('Both geometry bridge profiles must match', self.source)

    def test_calibration_reset_supports_old_and_new_public_callbacks(self):
        calls = []
        old = SimpleNamespace(_reset_sensor_calibration_trim=lambda: calls.append('old'))
        new = SimpleNamespace(_reset_sensor_calibration_trim=lambda prefix: calls.append(prefix))
        self.module._reset_calibration_trim(old, 'Sensor')
        self.module._reset_calibration_trim(new, 'Sensor')
        self.module._reset_calibration_trim(new, 'Femto')
        self.assertEqual(calls, ['old', 'Sensor', 'Femto'])
        with self.assertRaisesRegex(RuntimeError, 'prefixed reset'):
            self.module._reset_calibration_trim(old, 'Femto')
        def broken(prefix):
            raise TypeError('callback body failed')
        with self.assertRaisesRegex(TypeError, 'callback body failed'):
            self.module._reset_calibration_trim(
                SimpleNamespace(_reset_sensor_calibration_trim=broken), 'Sensor')

    def test_adapter_scene_selector_only_allows_complete_old_or_new_contract(self):
        old = SimpleNamespace(par=SimpleNamespace())
        new = SimpleNamespace(par=SimpleNamespace(Visualpath=object()))
        with self.assertRaisesRegex(RuntimeError, 'Visualpath'):
            self.module.adapter_control_inventory(old, old)
        values, feature = self.module.adapter_control_inventory(old, old, True)
        self.assertEqual((len(values), feature), (14, 'not_installed'))
        self.assertNotIn('Visualpath', values)
        self.assertEqual(self.module.adapter_control_inventory(new, new)[1], 'installed')
        for adapter, control in ((old, new), (new, old),
                                 (SimpleNamespace(par=SimpleNamespace(Playlistmanifest='')), old)):
            with self.assertRaisesRegex(RuntimeError, 'Visualpath'):
                self.module.adapter_control_inventory(adapter, control, True)

    def report_fixture(self, host=True):
        vr_checks = ('Experience_vr_enabled','Experience_render_vr_enabled','Vrinputsource',
                     'Vrhandenabled','Vrhandgain_shader','Vrleft_eye_dimensions',
                     'Vrright_eye_dimensions','Resetvrheadpose_Vrheadxmetres',
                     'Resetvrhands_Vrlefthandxmetres','Experience_installation_vr_disabled')
        controls = {'value':list(self.module.VALUE_CONTROLS),
                    'pulse':list(self.module.PULSE_CONTROLS),
                    'status':list(self.module.STATUS_CONTROLS),
                    'adapter_value':list(self.module.ADAPTER_VALUE_CONTROLS),
                    'adapter_pulse':list(self.module.ADAPTER_PULSE_CONTROLS)}
        for key, values in (('value',self.module.SD_HOST_VALUE_CONTROLS),
                            ('pulse',self.module.SD_HOST_PULSE_CONTROLS),
                            ('status',self.module.SD_HOST_STATUS_CONTROLS)):
            controls['streamdiffusion_host_' + key] = list(values) if host else []
        return {'version':'flexgpu-show-controls-validation/v2','profile':'3080ti_16gb',
                'acceptance_scope':'public_control_mapping_and_output_dimensions',
                'complete_live_acceptance':False,
                'status':'pass','summary':{'pass':311,'fail':0,'restoration_failures':0},
                'controls':controls,'checks':[{'name':n,'status':'pass'} for n in
                    vr_checks + (('streamdiffusion_host_structure',) if host else ())],
                'output_dimensions':{'status':'pass','wall_width':1920,'wall_height':1080},
                'worker_buttons':{'status':'external_validation_required'},
                'adapter_ui_buttons':{'status':'external_validation_required'},
                'streamdiffusion_host':{'version':'flexgpu-streamdiffusion-host-validation/v1',
                    'status':'pass','complete_live_acceptance':False,
                    'generator_buttons':'external_validation_required'}}

    def run_report_checker(self, report, expected_profile='3080ti_16gb'):
        shell = shutil.which('pwsh') or shutil.which('powershell')
        if shell is None:
            self.skipTest('PowerShell is unavailable')
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'report.json'
            path.write_text(json.dumps(report), encoding='utf-8')
            return subprocess.run([shell, '-NoLogo', '-NoProfile', '-NonInteractive',
                                   '-File', str(REPORT_CHECKER_PATH), '-Report', str(path),
                                   '-ExpectedProfile', expected_profile],
                                  capture_output=True, text=True, timeout=30, check=False)

    def test_report_checker_accepts_optional_host_and_legacy_without_host(self) -> None:
        for installed in (True, False):
            with self.subTest(host=installed):
                result = self.run_report_checker(self.report_fixture(installed))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(json.loads(result.stdout)['complete_live_acceptance'])

    def test_report_checker_uses_canonical_destination_profiles(self):
        for profile in ('4090', '5090'):
            report = self.report_fixture()
            report['profile'] = profile
            result = self.run_report_checker(report, profile)
            self.assertEqual(result.returncode, 0, result.stderr)
            mismatch = self.run_report_checker(report)
            self.assertNotEqual(mismatch.returncode, 0)
            self.assertIn('does not match', mismatch.stderr)
        for alias in ('4090_24gb', '5090_32gb'):
            result = self.run_report_checker(self.report_fixture(), alias)
            self.assertNotEqual(result.returncode, 0)

    def older_report_fixture(self):
        report = self.report_fixture()
        excluded = set().union(*map(set, self.module.OPTIONAL_CONTROL_GROUPS.values()))
        for key in ('value', 'pulse', 'status'):
            report['controls'][key] = [n for n in report['controls'][key] if n not in excluded]
        report['features'] = dict.fromkeys(self.module.OPTIONAL_CONTROL_GROUPS, 'not_installed')
        report['features']['podcast_visual_path'] = 'not_installed'
        report['controls']['adapter_value'].remove('Visualpath')
        report['allow_absent_optional'] = True
        return report

    def test_report_checker_accepts_explicit_older_inventory_without_claiming_vr(self):
        result = self.run_report_checker(self.older_report_fixture())
        self.assertEqual(result.returncode, 0, result.stderr)
        parsed = json.loads(result.stdout)
        self.assertEqual(parsed['public_controls'], 86)
        self.assertEqual(parsed['vr_foundation'], 'not_installed')
        self.assertEqual(parsed['femto_calibration_trim'], 'not_installed')
        self.assertEqual(parsed['podcast_visual_path'], 'not_installed')
        self.assertFalse(parsed['complete_live_acceptance'])

    def test_report_checker_rejects_partial_absent_or_unapproved_features(self):
        for mutation in ('partial', 'no_opt_in', 'missing_base', 'unknown_feature'):
            report = self.older_report_fixture()
            if mutation == 'partial':
                report['controls']['value'].append('Vrinputsource')
            elif mutation == 'no_opt_in':
                report['allow_absent_optional'] = False
            elif mutation == 'missing_base':
                report['controls']['value'].remove('Pointsize')
            else:
                report['features']['vr_foundation'] = 'pass'
            result = self.run_report_checker(report)
            self.assertNotEqual(result.returncode, 0, mutation)

    def test_report_checker_rejects_stale_host_inventory(self) -> None:
        report = self.report_fixture()
        report['controls'].pop('streamdiffusion_host_status')
        result = self.run_report_checker(report)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('inventory is missing or stale', result.stderr)

    def test_report_checker_rejects_overclaimed_top_level_acceptance(self):
        report = self.report_fixture()
        report['complete_live_acceptance'] = True
        result = self.run_report_checker(report)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('overclaimed acceptance scope', result.stderr)

    def test_report_checker_rejects_failed_host_or_overclaimed_acceptance(self) -> None:
        for field, value in (('status','fail'), ('complete_live_acceptance',True),
                             ('generator_buttons','pass')):
            with self.subTest(field=field):
                report = self.report_fixture()
                report['streamdiffusion_host'][field] = value
                result = self.run_report_checker(report)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('structural gate failed', result.stderr)


if __name__ == "__main__":
    unittest.main()
