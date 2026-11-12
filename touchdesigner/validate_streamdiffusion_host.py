"""Read-only public host wiring/control gate, not generation or show acceptance.

No parameters are changed, private descendants inspected, or model pulses sent.
An optional JSON report belongs in the ignored runtime/validation directory.
"""
from __future__ import print_function

import json
import math
import time
from pathlib import Path

try:
    import streamdiffusion_host as builder
    import streamdiffusion_host_runtime as runtime
except ImportError:
    from touchdesigner import streamdiffusion_host as builder
    from touchdesigner import streamdiffusion_host_runtime as runtime


REPORT_VERSION = 'flexgpu-streamdiffusion-host-validation/v1'


def _node(parent, path):
    return parent.op(path) if parent is not None else None


def _value(node, name, default=None):
    return runtime._get(node, name, default)


def _inputs(node):
    return list(node.inputs) if node is not None else []


def _from(node, sources):
    return node is not None and all(x is not None for x in sources) and _inputs(node) == sources


def validate(root, report_path=None):
    pipeline = _node(root, 'WORKING_PIPELINE')
    adapter = _node(pipeline, 'SOURCES/STREAMDIFFUSION_ADAPTER')
    host = _node(adapter, builder.HOST_NAME)
    checks = {'managed_owner': host is not None and
              host.fetch('managed_owner', '') == builder.OWNER}
    result = {'version': REPORT_VERSION, 'captured_ns': time.time_ns(),
              'acceptance_scope': 'public_host_structure_and_control_mirrors',
              'complete_live_acceptance': False, 'checks': checks,
              'generator_buttons': 'external_validation_required',
              'live_visual_acceptance': 'separate; requires fresh generated geometry'}
    if host is not None:
        raw, history, smooth, route, gate, out = [
            _node(host, n) for n in ('RAW_RGB', 'HISTORY', 'SMOOTH', 'OUTPUT_ROUTE', 'STREAM_GATE', 'OUT_RGB')]
        bridges = [_node(adapter, n) for n in ('MOGE2_BRIDGE', 'DEPTH_ANYTHING_GEOMETRY_BRIDGE')]
        retired = bool(host.fetch('legacy_retired', False))
        checks['shared_rgb_input'] = all(b is not None and _inputs(b) in ([host], [out]) for b in bridges)
        checks['accepted_upstream_preserved_or_retired'] = (
            len(_inputs(host)) == 0 if retired else len(_inputs(host)) == 1 and host.inputs[0] != host)
        for output, expected in (('OUT_RGB', 'GENERATED_GEOMETRY_RGB_ROUTE'),
                                  ('OUT_DEPTH', 'GENERATED_GEOMETRY_DEPTH_ROUTE')):
            inputs = _inputs(_node(adapter, output))
            checks[output.lower() + '_boundary'] = bool(inputs) and inputs[0].name == expected
        checks['smoothing_inputs'] = _from(smooth, [raw, history]) and _from(history, [raw])
        checks['smoothing_resolution'] = _value(smooth, 'outputresolution') == 'useinput'
        response = _value(host, 'Smoothms', -1)
        checks['smoothing_bounds'] = isinstance(response, (int, float)) and math.isfinite(response) and 0 <= response <= 250
        weight = _value(smooth, 'vec0valuex', -1)
        checks['smoothing_weight_bounds'] = isinstance(weight, (int, float)) and math.isfinite(weight) and 0 <= weight <= 0.98
        checks['feedback_target'] = smooth is not None and _value(history, 'top') == smooth
        checks['no_shader_errors'] = smooth is not None and not smooth.errors()
        checks['drop_slot_present'] = _node(host, 'PAYLOAD_SLOT') is not None
        noise, generator_input, noise_out = [_node(host, n) for n in ('INPUT_NOISE', 'GENERATOR_INPUT', 'OUT_INPUT')]
        checks['noise_input_present'] = noise is not None and _from(generator_input, [noise]) and _from(noise_out, [generator_input])
        if retired:
            checks['legacy_generators_removed'] = all(_node(adapter, n) is None for n in
                                                      ('StreamDiffusionTD', 'StreamDiffusionTD4'))
            # Non-generating old test TOPs may be retained after a separately
            # reviewed destination retirement. They cannot feed the host once
            # its legacy input is disconnected and only slot mode is allowed.
            result['retained_legacy_test_outputs'] = [n for n in ('null1', 'null2')
                                                       if _node(adapter, n) is not None]
            checks['retired_raw_slot_only'] = _from(raw, [_node(host, 'PAYLOAD_RGB')])
            source_parameter = getattr(host.par, 'Source', None)
            checks['legacy_source_removed'] = source_parameter is not None and list(source_parameter.menuNames) == ['slot']
        checks['single_generator_workflow'] = host.fetch('workflow_version', 0) == 2
        checks['audio_prompt_controls'] = all(hasattr(host.par, n) for n in
                                             ('Followpodcast', 'Promptformat', 'Promptstatus'))
        checks['smoothing_switch_inputs'] = _from(route, [raw, smooth])
        checks['smoothing_switch_selection'] = _value(route, 'index') == int(bool(_value(host, 'Smoothenabled', False)))
        checks['preview_before_filter'] = _from(_node(host, 'OUT_RAW'), [raw])
        checks['delivery_gate_inputs'] = _from(gate, [_node(host, 'DISCONNECTED'), route])
        connected = bool(_value(host, 'Streamenabled', False) and _value(host, 'Ready', False))
        checks['delivery_gate_selection'] = _value(gate, 'index') == int(connected)
        checks['explicit_delivery_gate'] = _from(out, [gate])
        checks['bridge_source_gates'] = all(b is not None and
            hasattr(b.par, 'Sourceactive') and hasattr(b.par, 'Sourceepoch') for b in bridges)
        checks['no_engine_started'] = not any(x.type == 'engine' for x in host.children)
        show = _node(pipeline, 'SHOW_CONTROL')
        checks['host_control_mirrors'] = show is not None and all(
            hasattr(host.par, source) and hasattr(show.par, mirror) and
            _value(host, source) == _value(show, mirror)
            for mirror, source in runtime.CONTROL_MAP.items())
        result.update({'source': _value(host, 'Source'),
                       'smoothing_enabled': bool(_value(host, 'Smoothenabled', False)),
                       'response_ms': response, 'history_weight': weight,
                       'ready': bool(_value(host, 'Ready', False)),
                       'stage': _value(host, 'Stage'),
                       'stream_connected': bool(_value(host, 'Streamenabled', False)),
                       'legacy_retired': retired,
                       'noise_enabled': bool(_value(host, 'Noiseenabled', False)),
                       'audio_prompt_status': _value(host, 'Promptstatus', 'not_installed')})
    result['status'] = 'pass' if all(checks.values()) else 'fail'
    if report_path:
        path = Path(report_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    return result
