from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from touchdesigner import streamdiffusion_host as builder
from touchdesigner import streamdiffusion_host_runtime as runtime
from touchdesigner import validate_show_controls
from touchdesigner import retire_legacy_streamdiffusion as retirement


class Parameter:
    def __init__(self, value=None, custom=True, pulse=False, toggle=False, label=''):
        self.val = value
        self.isCustom = custom
        self.isPulse = pulse
        self.isToggle = toggle
        self.label = label
        self.pulses = 0

    def eval(self):
        return self.val

    def pulse(self):
        self.pulses += 1


class Node:
    def __init__(self, name, family='COMP', parent=None):
        self.name, self.family, self._parent = name, family, parent
        self.path = (parent.path if parent else '') + '/' + name
        self.children = []
        self.par = SimpleNamespace()
        self.storage = {}
        if parent:
            parent.children.append(self)

    def parent(self):
        return self._parent

    def op(self, path):
        return next((x for x in self.children if x.name == path), None)

    def fetch(self, key, default):
        return self.storage.get(key, default)

    def store(self, key, value):
        self.storage[key] = value


class StreamDiffusionHostTests(unittest.TestCase):
    def fixture(self):
        host = Node('host')
        slot = Node('PAYLOAD_SLOT', parent=host)
        host.par.Payload = Parameter('')
        return host, slot

    def test_history_is_frame_rate_independent_and_bounded(self):
        full = runtime.history_weight(1 / 30, 50)
        half = runtime.history_weight(1 / 60, 50)
        self.assertAlmostEqual(full, half * half)
        for dt, ms in [(0, 50), (-1, 50), (1, 0), (math.nan, 50), (1, math.inf)]:
            self.assertEqual(runtime.history_weight(dt, ms), 0)
        self.assertEqual(runtime.history_weight(1 / 60, 50, False), 0)
        self.assertLessEqual(runtime.history_weight(0.00001, 999999), 0.98)

    def test_filter_timing_follows_demand_cooks_not_callback_ticks(self):
        state = {}
        self.assertEqual(runtime.filter_elapsed(state, 10, 1.0), 0)
        self.assertAlmostEqual(runtime.filter_elapsed(state, 11, 1.1), 0.1)
        self.assertAlmostEqual(runtime.filter_elapsed(state, 11, 1.15), 0.15)
        self.assertAlmostEqual(runtime.filter_elapsed(state, 11, 1.2), 0.2)
        self.assertAlmostEqual(runtime.filter_elapsed(state, 12, 1.3), 0.1)

    def test_only_direct_drop_slot_components_can_be_selected(self):
        host, slot = self.fixture()
        payload = Node('version_a', parent=slot)
        self.assertIs(runtime.selected_payload(host), payload)
        Node('version_b', parent=slot)
        self.assertIsNone(runtime.selected_payload(host))
        host.par.Payload.val = 'version_a'
        self.assertIs(runtime.selected_payload(host), payload)
        for name in ('../outside', '/project1/other', 'a/b'):
            host.par.Payload.val = name
            self.assertIsNone(runtime.selected_payload(host))

    def test_public_rgb_requires_explicit_bounded_top(self):
        payload = Node('payload')
        top = Node('out1', family='TOP', parent=payload)
        self.assertIs(runtime.public_rgb(payload, 'out1'), top)
        for name in ('', '../other', '/project1/other', 'missing'):
            self.assertIsNone(runtime.public_rgb(payload, name))

    def test_forwarding_uses_only_custom_public_parameters(self):
        host, slot = self.fixture()
        payload = Node('version_a', parent=slot)
        payload.par.Prompt = Parameter('old')
        host.par.Promptparameter = Parameter('Prompt')
        runtime.forward_value(host, 'Prompt', 'new')
        self.assertEqual(payload.par.Prompt.val, 'new')
        payload.par.Prompt.isCustom = False
        with self.assertRaisesRegex(ValueError, 'custom parameter'):
            runtime.forward_value(host, 'Prompt', 'unsafe')
        host.par.Promptparameter.val = '__dict__.value'
        with self.assertRaises(ValueError):
            runtime.forward_value(host, 'Prompt', 'unsafe')

    def test_start_stop_dispatch_only_explicit_mapped_custom_pulses(self):
        host, slot = self.fixture()
        payload = Node('version_a', parent=slot)
        for name in ('Start', 'Stop'):
            setattr(payload.par, name, Parameter(pulse=True))
            setattr(host.par, name + 'parameter', Parameter(name))
        with mock.patch.object(runtime, 'reset'), mock.patch.object(runtime, 'tick'), \
                mock.patch.object(runtime, 'disarm') as disarm:
            runtime.pulse(host, 'Start')
            runtime.pulse(host, 'Stop')
            payload.par.Start.isPulse = False
            runtime.pulse(host, 'Start')
        self.assertEqual(payload.par.Start.pulses, 1)
        self.assertEqual(payload.par.Stop.pulses, 1)
        self.assertIn('custom Pulse', host.fetch('_sd_host_state', {})['error'])
        self.assertEqual(disarm.call_count, 3)

    def controlled_fixture(self):
        host, slot = self.fixture()
        payload = Node('generator', parent=slot)
        host.par.Startparameter = Parameter('')
        host.par.Stopparameter = Parameter('')
        payload.par.Startstream = Parameter(pulse=True, label='Start Stream')
        payload.par.Stopstream = Parameter(pulse=True, label='Stop Server')
        payload.par.Serveractive = Parameter(True, toggle=True)
        payload.par.Streamactive = Parameter(True, toggle=True)
        return host, payload

    def test_known_public_controls_bind_without_overriding_manual_mappings(self):
        host, payload = self.controlled_fixture()
        self.assertTrue(runtime.bind_public_controls(host, payload))
        self.assertEqual(host.par.Startparameter.eval(), 'Startstream')
        self.assertEqual(host.par.Stopparameter.eval(), 'Stopstream')
        host.par.Stopparameter.val = 'Manualstop'
        runtime.bind_public_controls(host, payload)
        self.assertEqual(host.par.Stopparameter.eval(), 'Manualstop')
        other = Node('another_version', parent=payload.parent())
        runtime.bind_public_controls(host, other)
        self.assertEqual(host.par.Startparameter.eval(), '')
        self.assertEqual(host.par.Stopparameter.eval(), 'Manualstop')

    def test_stop_stream_label_is_not_assumed_to_be_stop_server(self):
        host, payload = self.controlled_fixture()
        payload.par.Stopstream.label = 'Stop Stream'
        self.assertFalse(runtime.bind_public_controls(host, payload))
        self.assertEqual(host.par.Stopparameter.eval(), '')

    def test_offload_waits_for_public_flags_and_keeps_component_loaded(self):
        host, payload = self.controlled_fixture()
        with mock.patch.object(runtime, 'disarm'), \
                mock.patch.object(runtime, '_wake_shutdown_callbacks'), \
                mock.patch.object(runtime.time, 'perf_counter', return_value=100):
            runtime.request_offload(host, payload)
        self.assertEqual(payload.par.Stopstream.pulses, 1)
        self.assertEqual(runtime.offload_state(host, payload, 101), 'offloading')
        payload.par.Serveractive.val = False
        payload.par.Streamactive.val = False
        self.assertEqual(runtime.offload_state(host, payload, 102), 'offloaded')
        self.assertIn(payload, payload.parent().children)
        with mock.patch.object(runtime, 'disarm'):
            runtime.request_offload(host, payload)
        self.assertEqual(payload.par.Stopstream.pulses, 1)  # Already inactive is idempotent.
        runtime.offload_state(host, payload, 103)
        payload.par.Serveractive.val = True
        payload.par.Streamactive.val = True
        self.assertIsNone(runtime.offload_state(host, payload, 104))

    def test_offload_timeout_does_not_claim_a_server_stopped(self):
        host, payload = self.controlled_fixture()
        with mock.patch.object(runtime, 'disarm'), \
                mock.patch.object(runtime, '_wake_shutdown_callbacks'), \
                mock.patch.object(runtime.time, 'perf_counter', return_value=100):
            runtime.request_offload(host, payload)
        self.assertEqual(runtime.offload_state(host, payload, 121), 'offload_unconfirmed')
        self.assertTrue(payload.par.Serveractive.eval())

    def test_offload_temporarily_runs_callbacks_and_restores_paused_frame(self):
        host, _ = self.fixture()
        clock = Node('/local/time', parent=host)
        clock.par.play = Parameter(False)
        project = Node('/project1', parent=host)
        project.time = SimpleNamespace(frame=1719)
        request = {}
        runtime._wake_shutdown_callbacks(host, request)
        self.assertTrue(clock.par.play.eval())
        self.assertTrue(host.fetch('offload_wake_timeline', False))
        project.time.frame = 1740
        runtime._restore_shutdown_callbacks(host, request)
        self.assertFalse(clock.par.play.eval())
        self.assertEqual(project.time.frame, 1719)
        self.assertFalse(host.fetch('offload_wake_timeline', False))

    def test_loading_never_replaces_an_occupied_slot(self):
        host, slot = self.fixture()
        Node('accepted_version', parent=slot)
        host.par.Status = Parameter('')
        slot.loadTox = mock.Mock()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'candidate.tox'
            path.write_bytes(b'fixture-not-a-real-tox')
            host.par.Toxfile = Parameter(str(path))
            with mock.patch.object(runtime, '_sync_show'), mock.patch.object(runtime, 'disarm'):
                runtime.pulse(host, 'Loadtox')
        slot.loadTox.assert_not_called()
        self.assertIn('occupied', host.par.Status.eval())

    def test_route_stages_require_an_explicit_delivery_request(self):
        self.assertEqual(runtime.route_state('slot', False, False, True),
                         ('select_generator', False, False))
        self.assertEqual(runtime.route_state('slot', True, False, True),
                         ('configure_rgb', False, False))
        self.assertEqual(runtime.route_state('slot', True, True, False),
                         ('preview', True, False))
        self.assertEqual(runtime.route_state('slot', True, True, True),
                         ('connected', True, True))
        self.assertEqual(runtime.route_state('existing', False, False, False),
                         ('preview', True, False))
        self.assertEqual(runtime.route_state('unexpected', True, True, True),
                         ('invalid_source', False, False))

    def test_disarm_closes_gate_and_invalidates_previous_epoch(self):
        host, _ = self.fixture()
        host.par.Streamenabled = Parameter(True)
        gate = Node('STREAM_GATE', family='TOP', parent=host)
        gate.par.index = Parameter(1)
        host.store('stream_epoch', 'previous')
        with mock.patch.object(runtime, 'reset'):
            runtime.disarm(host)
        self.assertFalse(host.par.Streamenabled.eval())
        self.assertEqual(gate.par.index.eval(), 0)
        self.assertNotEqual(host.fetch('stream_epoch', ''), 'previous')

    def test_open_generator_controls_never_starts_or_sends_stream(self):
        host, slot = self.fixture()
        payload = Node('version_a', parent=slot)
        payload.openParameters = mock.Mock()
        host.par.Streamenabled = Parameter(False)
        with mock.patch.object(runtime, 'tick'):
            runtime.pulse(host, 'Opencontrols')
        payload.openParameters.assert_called_once_with()
        self.assertFalse(host.par.Streamenabled.eval())

    def test_preview_and_noise_windows_do_not_start_or_connect_generation(self):
        host, _ = self.fixture()
        host.par.Streamenabled = Parameter(False)
        raw = Node('OUT_RAW', 'TOP', parent=host)
        raw.openViewer = mock.Mock()
        preview = Node('OUT_INPUT', 'TOP', parent=host)
        preview.openViewer = mock.Mock()
        noise = Node('INPUT_NOISE', 'TOP', parent=host)
        noise.openParameters = mock.Mock()
        with mock.patch.object(runtime, 'tick'), mock.patch.object(runtime, 'disarm') as disarm:
            for name in ('Openpreview', 'Opennoisecontrols', 'Opennoisepreview'):
                runtime.pulse(host, name)
        raw.openViewer.assert_called_once_with(unique=False, borders=True)
        preview.openViewer.assert_called_once_with(unique=False, borders=True)
        noise.openParameters.assert_called_once_with()
        disarm.assert_not_called()
        self.assertFalse(host.par.Streamenabled.eval())

    def test_native_noise_and_quick_controls_sync_without_clobbering_native_seed(self):
        host, _ = self.fixture()
        noise = Node('INPUT_NOISE', 'TOP', parent=host)
        for quick, native in runtime.NOISE_QUICK_MAP.items():
            value = 1 if native == 'seed' else 512
            setattr(host.par, quick, Parameter(value))
            getattr(host.par, quick).name = quick
            setattr(noise.par, native, Parameter(value))
        noise.par.outputresolution = Parameter('custom')
        runtime._sync_noise(host)
        noise.par.seed.val = 2.75
        runtime._sync_noise(host)
        self.assertEqual(host.par.Noiseseed.eval(), 2)
        with mock.patch.object(runtime, 'tick'):
            runtime.host_changed(host, host.par.Noiseseed)
        self.assertEqual(noise.par.seed.eval(), 2.75)
        # A host edit remains authoritative if the frame tick arrives first.
        host.par.Noisewidth.val = 384
        runtime._sync_noise(host)
        self.assertEqual(noise.par.resolutionw.eval(), 384)
        show_parameter = Parameter(640)
        show_parameter.name = 'Sdnoisewidth'
        with mock.patch.object(runtime, 'tick'):
            runtime.control_changed(host, show_parameter)
        self.assertEqual(noise.par.resolutionw.eval(), 640)
        self.assertEqual(noise.par.seed.eval(), 2.75)

    def test_public_podcast_guard_is_narrow_and_idempotent(self):
        original = 'def sync():\n    if play_parameter is not None:\n        timeline_play.val = desired_play\n'
        guarded = builder.podcast_timeline_guard(original)
        self.assertIn('podcast_owns_timeline', guarded)
        self.assertIn('host.par.Followpodcast.eval()', guarded)
        self.assertEqual(builder.podcast_timeline_guard(guarded), guarded)
        compile(guarded, '<public podcast guard>', 'exec')
        with self.assertRaisesRegex(RuntimeError, 'Unrecognized public'):
            builder.podcast_timeline_guard('some unrelated public callback')

    def test_noise_wiring_uses_only_public_input_zero_and_preserves_other_input(self):
        payload = SimpleNamespace(inputs=[])
        noise = SimpleNamespace(outputConnectors=[])
        incoming = mock.Mock()
        incoming.disconnect.side_effect = lambda: setattr(payload, 'inputs', [])
        payload.inputConnectors = [incoming]
        outgoing = mock.Mock()
        outgoing.connect.side_effect = lambda target: setattr(payload, 'inputs', [noise])
        noise.outputConnectors = [outgoing]
        runtime.wire_noise(payload, noise, True)
        outgoing.connect.assert_called_once_with(incoming)
        runtime.wire_noise(payload, noise, True)
        outgoing.connect.assert_called_once()
        runtime.wire_noise(payload, noise, False)
        self.assertEqual(payload.inputs, [])
        payload.inputs = [object()]
        with self.assertRaisesRegex(ValueError, 'occupied'):
            runtime.wire_noise(payload, noise, True)
        runtime.wire_noise(payload, noise, False)
        self.assertEqual(len(payload.inputs), 1)
        payload.inputs = []
        payload.inputConnectors = []
        with self.assertRaisesRegex(ValueError, 'no public TOP input'):
            runtime.wire_noise(payload, noise, True)

    def test_loader_ignores_only_its_owned_noise_transport(self):
        host, slot = self.fixture()
        transport = Node('INPUT_FROM_HOST', family='TOP', parent=slot)
        transport.type = 'select'
        transport.store('managed_owner', 'flexgpu-streamdiffusion-input/v1')
        self.assertFalse(runtime.slot_occupied(slot))
        Node('user_node', family='TOP', parent=slot)
        self.assertTrue(runtime.slot_occupied(slot))

    def test_retired_controller_keeps_sequencing_but_cannot_write_old_generators(self):
        host, _ = self.fixture()
        host.store('legacy_retired', True)
        controller = SimpleNamespace(_write_streamdiffusion=mock.Mock(),
                                     _write_color_controls=mock.Mock(), update=mock.Mock())
        self.assertIs(runtime.attach_podcast_controller(host, controller), controller)
        self.assertEqual(controller._write_streamdiffusion(None), 'single_generator_host')
        self.assertEqual(controller._write_color_controls(), 'point_cloud_color_controls')
        controller.update.assert_not_called()
        source = ('def get_controller():\n    if cached:\n        return _CONTROLLER\n'
                  '    _CONTROLLER = module.PodcastVisualController(owner)\n    return _CONTROLLER\n')
        patched = retirement.controller_guard(source)
        self.assertEqual(retirement.controller_guard(patched), patched)
        compile(patched, '<retired public controller>', 'exec')

    def test_single_controller_skips_only_retired_rig_check_and_keeps_approval(self):
        host, _ = self.fixture()
        host.store('legacy_retired', True)
        class Base:
            def __init__(self, owner):
                self.owner_comp = owner
                self._load_scene()
                self.render_state = self._write_streamdiffusion()
            def _load_scene(self):
                return True
            def _write_streamdiffusion(self):
                raise AssertionError('Retired renderer must not be targeted')
        class OldRigGuard(Base):
            def __init__(self, owner):
                ready = getattr(owner.par, 'Semanticneutralready', None)
                mix = getattr(owner.par, 'Semanticneutralmix', None)
                if (ready is None or not ready.eval() or mix is None or
                        any(owner.op('semantic_neutral_output_' + str(i)) is None for i in (1, 2))):
                    raise RuntimeError('Native semantic-neutral output rig is not installed/verified; do not use the staged controller in .34')
                super().__init__(owner)
            def _load_scene(self):
                if not self.owner_comp.approved:
                    raise RuntimeError('User approval is required')
                return super()._load_scene()
        OldRigGuard.__init__.__code__ = OldRigGuard.__init__.__code__.replace(
            co_filename='five_second_td_controller.py')
        class Latest(OldRigGuard):
            pass
        result = runtime.construct_podcast_controller(host, Latest, SimpleNamespace(approved=True))
        self.assertEqual(result.render_state, 'single_generator_host')
        with self.assertRaisesRegex(RuntimeError, 'User approval'):
            runtime.construct_podcast_controller(host, Latest, SimpleNamespace(approved=False))

    def test_retirement_requires_explicit_confirmation_backup_and_3080_scope(self):
        arguments = dict(backup_path='missing.toe', project_folder='.', project_name='3080.44.toe')
        with self.assertRaisesRegex(RuntimeError, 'Explicit confirmation'):
            retirement.retire(None, **arguments)
        arguments['project_name'] = '5090.71.toe'
        with self.assertRaisesRegex(RuntimeError, '3080 working copy'):
            retirement.retire(None, confirm=True, **arguments)
        arguments['project_name'] = '3080.44.toe'
        with self.assertRaisesRegex(RuntimeError, 'backup is required'):
            retirement.retire(None, confirm=True, **arguments)

    def test_retirement_tolerates_automatically_deleted_docked_callbacks(self):
        nodes = {}
        callback = SimpleNamespace(destroy=mock.Mock())
        def remove_generator():
            nodes.pop('StreamDiffusionTD')
            nodes.pop('StreamDiffusionTD_callbacks')
        generator = SimpleNamespace(destroy=mock.Mock(side_effect=remove_generator))
        nodes.update(StreamDiffusionTD=generator, StreamDiffusionTD_callbacks=callback)
        adapter = SimpleNamespace(op=nodes.get)
        retirement._destroy_reviewed_roots(adapter, ['StreamDiffusionTD', 'StreamDiffusionTD_callbacks'])
        generator.destroy.assert_called_once()
        callback.destroy.assert_not_called()
        with self.assertRaisesRegex(RuntimeError, 'Unreviewed'):
            retirement._destroy_reviewed_roots(adapter, ['audio_out'])

    def test_optional_show_control_inventory_matches_builder(self):
        self.assertEqual(builder.VALUE_CONTROLS, validate_show_controls.SD_HOST_VALUE_CONTROLS)
        self.assertEqual(builder.PULSE_CONTROLS, validate_show_controls.SD_HOST_PULSE_CONTROLS)
        self.assertEqual(builder.STATUS_CONTROLS, validate_show_controls.SD_HOST_STATUS_CONTROLS)
        self.assertEqual(retirement.RETIRED_COLOR_CONTROLS,
                         validate_show_controls.RETIRED_ADAPTER_COLOR_CONTROLS)

    def test_builder_has_no_automatic_private_load_or_engine(self):
        source = Path(builder.__file__).read_text(encoding='utf-8')
        self.assertNotIn('.loadTox(', source)
        self.assertNotIn('.destroy(', source)
        self.assertNotIn("'engineCOMP'", source)
        self.assertIn("'Smoothenabled', False", source)
        self.assertIn("'Streamenabled', False", source)
        self.assertIn("('slot',) if retired else ('slot', 'existing')", source)
        self.assertIn("def onStart()", builder.HOST_CALLBACKS)
        self.assertIn("sources[0] != sources[1]", source)
        self.assertIn("'rgba16float'", source)
        compile(builder.HOST_CALLBACKS, '<host callbacks>', 'exec')
        compile(builder.PARAM_CALLBACKS, '<parameter callbacks>', 'exec')
        compile(builder.SHOW_CALLBACKS, '<show callbacks>', 'exec')
        compile(builder.NOISE_CALLBACKS, '<noise callbacks>', 'exec')


if __name__ == '__main__':
    unittest.main()
