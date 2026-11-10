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


class Parameter:
    def __init__(self, value=None, custom=True, pulse=False):
        self.val = value
        self.isCustom = custom
        self.isPulse = pulse
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
        with mock.patch.object(runtime, 'reset'), mock.patch.object(runtime, 'tick'):
            runtime.pulse(host, 'Start')
            runtime.pulse(host, 'Stop')
            payload.par.Start.isPulse = False
            runtime.pulse(host, 'Start')
        self.assertEqual(payload.par.Start.pulses, 1)
        self.assertEqual(payload.par.Stop.pulses, 1)
        self.assertIn('custom Pulse', host.fetch('_sd_host_state', {})['error'])

    def test_loading_never_replaces_an_occupied_slot(self):
        host, slot = self.fixture()
        Node('accepted_version', parent=slot)
        host.par.Status = Parameter('')
        slot.loadTox = mock.Mock()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'candidate.tox'
            path.write_bytes(b'fixture-not-a-real-tox')
            host.par.Toxfile = Parameter(str(path))
            with mock.patch.object(runtime, '_sync_show'):
                runtime.pulse(host, 'Loadtox')
        slot.loadTox.assert_not_called()
        self.assertIn('occupied', host.par.Status.eval())

    def test_optional_show_control_inventory_matches_builder(self):
        self.assertEqual(builder.VALUE_CONTROLS, validate_show_controls.SD_HOST_VALUE_CONTROLS)
        self.assertEqual(builder.PULSE_CONTROLS, validate_show_controls.SD_HOST_PULSE_CONTROLS)
        self.assertEqual(builder.STATUS_CONTROLS, validate_show_controls.SD_HOST_STATUS_CONTROLS)

    def test_builder_has_no_automatic_private_load_or_engine(self):
        source = Path(builder.__file__).read_text(encoding='utf-8')
        self.assertNotIn('.loadTox(', source)
        self.assertNotIn('.destroy(', source)
        self.assertNotIn("'engineCOMP'", source)
        self.assertIn("'Smoothenabled', False", source)
        self.assertIn("sources[0] != sources[1]", source)
        self.assertIn("'rgba16float'", source)
        compile(builder.HOST_CALLBACKS, '<host callbacks>', 'exec')
        compile(builder.PARAM_CALLBACKS, '<parameter callbacks>', 'exec')
        compile(builder.SHOW_CALLBACKS, '<show callbacks>', 'exec')


if __name__ == '__main__':
    unittest.main()
