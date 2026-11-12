from pathlib import Path
from enum import Enum
from types import SimpleNamespace
from unittest.mock import Mock
import ast
import unittest

from touchdesigner import sync_combined_podcast_5090 as migration
from touchdesigner import streamdiffusion_host as host
from touchdesigner import retire_legacy_streamdiffusion as old_retirement
from touchdesigner import validate_podcast_5090 as validation
from touchdesigner import audit_streamdiffusion_controls as host_audit
from touchdesigner import bootstrap_project as bootstrap


class Podcast5090SyncTests(unittest.TestCase):
    def test_host_value_audit_has_no_model_or_file_pulses(self):
        source = Path(host_audit.__file__).read_text(encoding='utf-8')
        ast.parse(source)
        self.assertIn('finally:', source)
        self.assertIn('runtime.server_flags(payload) != (False, False)', source)
        self.assertNotIn('.pulse(', source)
        self.assertNotIn('Sdoffloadremove', host_audit.TRIALS)
        self.assertNotIn('Sdfollowpodcast', host_audit.TRIALS)
        self.assertNotIn('.save(', source)
    def test_expression_uses_parameter_enum_without_global_td_symbol(self):
        class Mode(Enum):
            CONSTANT = 0
            EXPRESSION = 1
        parameter = SimpleNamespace(mode=Mode.CONSTANT, expr='')
        migration._expr(parameter, 'parent().par.Audiofile')
        self.assertEqual(parameter.mode, Mode.EXPRESSION)
        self.assertEqual(parameter.expr, 'parent().par.Audiofile')

    def test_preservation_excludes_only_named_derived_runtime_counters(self):
        self.assertEqual(migration.DERIVED_STATUS, {
            'ROLE_BRIDGE': ('Frameid', 'Framevalid'),
            'TEMPORAL_WORLD': ('Deltaseconds', 'Newframe', 'Sourcevalid'),
            'SENSOR_INTERACTION': ('Sensoragems', 'Sensorframeid'),
        })
        self.assertNotIn('POINT_RENDER', migration.DERIVED_STATUS)

    def test_startup_restore_preserves_5090_override_and_rejects_custom_code(self):
        patched = migration.patch_startup_resolution(migration.LEGACY_STARTUP)
        self.assertIn("module_dat.module.apply(root_comp, {'tier':'5090'}, preserve_show_selections=True)", patched)
        self.assertIn('_restore_saved_venue_output', patched)
        self.assertIn('_venue_restore_pending', patched)
        self.assertEqual(migration.patch_startup_resolution(patched), patched)
        with self.assertRaisesRegex(RuntimeError, 'Unrecognized public startup'):
            migration.patch_startup_resolution('def onStart(): user_custom_callback()')
        previous = bootstrap.STARTUP_CALLBACKS_PRE_SELECTION.replace(
            'module_dat.module.apply(root_comp)', "module_dat.module.apply(root_comp, {'tier':'5090'})")
        self.assertEqual(migration.patch_startup_resolution(previous), patched)

    def test_runtime_preamble_patch_is_scoped_idempotent_and_fail_closed(self):
        new = bootstrap.RUNTIME_HELPERS
        node = next(n for n in ast.parse(new).body if isinstance(n, ast.FunctionDef)
                    and n.name == '_preserve_show_selections')
        lines = new.splitlines(keepends=True)
        addition = ''.join(lines[node.lineno - 1:node.end_lineno])
        old = new.replace(addition + '\n', '', 1).replace(
            ', preserve_show_selections=False):', '):', 1).replace(
            '    if preserve_show_selections:\n        _preserve_show_selections(root_comp, state)\n', '', 1).replace(
            'apply(root_comp, preserve_show_selections=True)', 'apply(root_comp)', 1)
        old += '\n# local receiver helper must survive\n'
        patched = migration.patch_runtime_show_selections(old)
        self.assertEqual(patched, new + '\n# local receiver helper must survive\n')
        self.assertEqual(migration.patch_runtime_show_selections(patched), patched)
        with self.assertRaisesRegex(RuntimeError, 'Unrecognized public runtime'):
            migration.patch_runtime_show_selections(old.replace('state.update(overrides)', 'custom_merge(overrides)'))
        with self.assertRaisesRegex(RuntimeError, 'tick initialization'):
            migration.patch_runtime_show_selections(old.replace('        apply(root_comp)', '        custom_start(root_comp)'))

    def test_startup_selections_merge_only_saved_provider_and_mode(self):
        namespace = {}
        exec(bootstrap.RUNTIME_HELPERS, namespace)
        root = SimpleNamespace(op=lambda _: SimpleNamespace(par=SimpleNamespace(
            Geometryprovider=SimpleNamespace(eval=lambda: 'depth_anything'),
            Displaymode=SimpleNamespace(eval=lambda: 'artistic_multi_angle'))))
        state = {'tier': '5090', 'source': {'port': 9250},
                 'render': {'camera': 'local'}, 'calibration': {'x': 1}}
        namespace['_preserve_show_selections'](root, state)
        self.assertEqual(state, {'tier': '5090', 'source': {'port': 9250, 'geometry_provider': 'depth_anything'},
            'render': {'camera': 'local', 'display_mode': 'artistic_multi_angle'}, 'calibration': {'x': 1}})
        signature = next(n for n in ast.parse(bootstrap.RUNTIME_HELPERS).body
                         if isinstance(n, ast.FunctionDef) and n.name == 'apply')
        self.assertEqual(signature.args.args[-1].arg, 'preserve_show_selections')
        self.assertFalse(signature.args.defaults[-1].value)
        manual = {}
        namespace['_preserve_show_selections'](root, manual)
        self.assertNotIn('source', manual)
        self.assertEqual(manual['source_geometry_provider'], 'depth_anything')

    def test_import_is_inert_and_3080_destination_rejected_before_lookup(self):
        root = SimpleNamespace(path='/project1/flexgpu', op=Mock())
        project = SimpleNamespace(name='FlexShow-podcast-local-3080.46.toe')
        with self.assertRaisesRegex(RuntimeError, '5090 destination'):
            migration.preflight(root, '.', '.', project)
        root.op.assert_not_called()

    def test_audio_callback_patch_preserves_unrelated_source_exactly(self):
        source = ("def camera_motion():\n    return 'keep camera expression'\n\n"
                  "def _apply_audio_controls():\n"
                  "    controls = _controls()\n"
                  "    source = 'Audiosource'\n"
                  "    target = 'audiosource_switch'\n\n"
                  "def geometry_settings():\n    return 5090\n")
        patched = migration.patch_audio_callback(source)
        ast.parse(patched)
        self.assertTrue(patched.startswith(source.split('def _apply_audio_controls')[0]))
        self.assertTrue(patched.endswith(source[source.index('def geometry_settings'):]))
        self.assertIn("adapter.op('combined_audio')", patched)
        self.assertEqual(migration.patch_audio_callback(patched), patched)
        with self.assertRaisesRegex(RuntimeError, 'Unrecognized'):
            migration.patch_audio_callback('def something_else(): pass')
        with self.assertRaisesRegex(RuntimeError, 'contract changed'):
            migration.patch_audio_callback('def _apply_audio_controls(): pass')

    def test_hardware_neutral_controller_guard_matches_reviewed_guard(self):
        source = ("_CONTROLLER = None\n"
                  "def get_controller():\n    global _CONTROLLER\n"
                  "    if _CONTROLLER:\n        return _CONTROLLER\n"
                  "    _CONTROLLER = module.PodcastVisualController(owner)\n"
                  "    return _CONTROLLER\n")
        expected = old_retirement.controller_guard(source)
        self.assertEqual(host.single_generator_controller_guard(source), expected)
        self.assertEqual(host.single_generator_controller_guard(expected), expected)
        ast.parse(expected)

    def test_only_reviewed_roots_are_deleted_and_no_sender_installer_runs(self):
        source = Path(migration.__file__).read_text(encoding='utf-8')
        self.assertEqual(migration.LEGACY, ('StreamDiffusionTD', 'StreamDiffusionTD4'))
        self.assertEqual(migration.DOCKS, ('StreamDiffusionTD_callbacks', 'StreamDiffusionTD_callbacks4'))
        self.assertIn('for name in LEGACY + DOCKS:', source)
        self.assertIn('confirm_legacy is not True', source)
        self.assertIn('server_flags(g) != (False, False)', source)
        self.assertIn('protected_state(root)', source)
        self.assertNotIn('retire_legacy_streamdiffusion', source)
        self.assertNotIn('runtime_pipeline.build', source)
        self.assertNotIn('saveExternalTox', source)
        self.assertNotIn('.save(', source)
        self.assertNotIn('Startstream', source)
        self.assertIn("patch_audio_callback(outer.op('show_control_callbacks').text)", source)

    def test_muted_receiver_audit_restores_and_never_starts_models(self):
        source = Path(validation.__file__).read_text(encoding='utf-8')
        ast.parse(source)
        self.assertIn('finally:', source)
        self.assertIn('protected == migration.protected_state(root)', source)
        self.assertIn('main.par.Audiosource.eval() != control.par.Audiosource.eval()', source)
        self.assertIn("(len(seq.stories), len(seq.scenes), len(seq.captions)) == (136, 619, 2862)", source)
        self.assertNotIn('.save(', source)
        self.assertNotIn('Startstream', source)
        self.assertNotIn('Startmogeworker', source)


if __name__ == '__main__':
    unittest.main()
