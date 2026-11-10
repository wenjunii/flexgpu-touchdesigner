from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.test_streamdiffusion_host import Node, Parameter
from touchdesigner import streamdiffusion_host as builder
from touchdesigner import streamdiffusion_host_runtime as runtime
from touchdesigner import validate_streamdiffusion_host as validation


class PublicNode(Node):
    def __init__(self, name, parent=None):
        super().__init__(name, parent=parent)
        self.inputs, self.type = [], 'null'

    def op(self, path):
        current = self
        for name in path.split('/'):
            current = next((x for x in current.children if x.name == name), None)
            if current is None:
                break
        return current

    def errors(self):
        return ''


class StreamDiffusionHostValidationTests(unittest.TestCase):
    def fixture(self):
        root = PublicNode('root')
        pipeline = PublicNode('WORKING_PIPELINE', root)
        sources = PublicNode('SOURCES', pipeline)
        adapter = PublicNode('STREAMDIFFUSION_ADAPTER', sources)
        host = PublicNode(builder.HOST_NAME, adapter)
        host.store('managed_owner', builder.OWNER)
        host.store('legacy_retired', True)
        host.store('workflow_version', 2)
        show = PublicNode('SHOW_CONTROL', pipeline)
        values = dict(Source='slot', Smoothenabled=True, Smoothms=50.0,
                      Streamenabled=False, Ready=True, Stage='preview',
                      Followpodcast=False, Promptformat='auto', Promptstatus='manual')
        for source in runtime.CONTROL_MAP.values():
            values.setdefault(source, '')
        for name, value in values.items():
            setattr(host.par, name, Parameter(value))
        host.par.Source.menuNames = ['slot']
        for mirror, source in runtime.CONTROL_MAP.items():
            setattr(show.par, mirror, Parameter(values[source]))
        for name in ('RAW_RGB','HISTORY','SMOOTH','OUTPUT_ROUTE','STREAM_GATE','OUT_RGB',
                     'OUT_RAW','DISCONNECTED','PAYLOAD_SLOT','INPUT_NOISE','GENERATOR_INPUT','OUT_INPUT'):
            PublicNode(name, host)
        for name, inputs in {
            'HISTORY':['RAW_RGB'], 'SMOOTH':['RAW_RGB','HISTORY'],
            'OUTPUT_ROUTE':['RAW_RGB','SMOOTH'], 'STREAM_GATE':['DISCONNECTED','OUTPUT_ROUTE'],
            'OUT_RGB':['STREAM_GATE'], 'OUT_RAW':['RAW_RGB'],
            'GENERATOR_INPUT':['INPUT_NOISE'], 'OUT_INPUT':['GENERATOR_INPUT'],
        }.items():
            host.op(name).inputs = [host.op(n) for n in inputs]
        host.op('SMOOTH').par.outputresolution = Parameter('useinput')
        host.op('SMOOTH').par.vec0valuex = Parameter(0.6)
        host.op('HISTORY').par.top = Parameter(host.op('SMOOTH'))
        host.op('OUTPUT_ROUTE').par.index = Parameter(1)
        host.op('STREAM_GATE').par.index = Parameter(0)
        for name in ('MOGE2_BRIDGE','DEPTH_ANYTHING_GEOMETRY_BRIDGE'):
            bridge = PublicNode(name, adapter)
            bridge.inputs = [host]
            bridge.par.Sourceactive, bridge.par.Sourceepoch = Parameter(False), Parameter('test')
        for output, route in (('OUT_RGB','GENERATED_GEOMETRY_RGB_ROUTE'),
                              ('OUT_DEPTH','GENERATED_GEOMETRY_DEPTH_ROUTE')):
            PublicNode(output, adapter).inputs = [PublicNode(route, adapter)]
        return root, host, show

    def test_read_only_report_passes_without_pulses_or_live_acceptance(self):
        root, host, _ = self.fixture()
        with mock.patch.object(runtime, '_set', side_effect=AssertionError('must not mutate')):
            report = validation.validate(root)
        self.assertEqual(report['status'], 'pass', report['checks'])
        self.assertFalse(report['complete_live_acceptance'])
        self.assertEqual(report['generator_buttons'], 'external_validation_required')
        self.assertEqual(report['response_ms'], 50)
        self.assertTrue(host.par.Smoothenabled.eval())

    def test_missing_roots_and_partial_host_fail_without_raising(self):
        self.assertEqual(validation.validate(None)['status'], 'fail')
        root, host, _ = self.fixture()
        host.children = []
        report = validation.validate(root)
        self.assertEqual(report['status'], 'fail')
        self.assertFalse(report['checks']['smoothing_inputs'])

    def test_wrong_feedback_target_and_preview_route_are_rejected(self):
        root, host, _ = self.fixture()
        host.op('HISTORY').par.top.val = host.op('RAW_RGB')
        host.op('OUT_RAW').inputs = [host.op('SMOOTH')]
        report = validation.validate(root)
        self.assertFalse(report['checks']['feedback_target'])
        self.assertFalse(report['checks']['preview_before_filter'])

    def test_stale_control_mirror_and_wrong_gate_are_rejected(self):
        root, host, show = self.fixture()
        show.par.Sdsmoothms.val = 100
        host.op('STREAM_GATE').par.index.val = 1
        report = validation.validate(root)
        self.assertFalse(report['checks']['host_control_mirrors'])
        self.assertFalse(report['checks']['delivery_gate_selection'])

    def test_nonfinite_response_or_weight_fails_closed(self):
        for name, value in (('Smoothms', math.nan), ('Smoothms', math.inf),
                            ('Smoothms', 251), ('vec0valuex', math.nan), ('vec0valuex', 1)):
            root, host, _ = self.fixture()
            target = host.op('SMOOTH') if name == 'vec0valuex' else host
            getattr(target.par, name).val = value
            self.assertEqual(validation.validate(root)['status'], 'fail')

    def test_optional_report_contains_no_prompt_text_or_private_payload_details(self):
        root, host, _ = self.fixture()
        payload = PublicNode('private_generator', host.op('PAYLOAD_SLOT'))
        payload.par.Prompt = Parameter('PRIVATE_USER_PROMPT_NOT_A_REPORT_FIELD')
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'report.json'
            validation.validate(root, report_path=path)
            text = path.read_text(encoding='utf-8')
            self.assertNotIn('PRIVATE_USER_PROMPT', text)
            self.assertNotIn('private_generator', text)
            self.assertEqual(json.loads(text)['version'], validation.REPORT_VERSION)


if __name__ == '__main__':
    unittest.main()
