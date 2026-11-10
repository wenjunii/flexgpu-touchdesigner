"""Read-only structural gate for the optional generated-RGB host.

This does not claim image quality, actual generation FPS or Engine compatibility.
"""
from __future__ import print_function


def validate(root):
    pipeline = root.op('WORKING_PIPELINE')
    adapter = pipeline.op('SOURCES/STREAMDIFFUSION_ADAPTER')
    host = adapter.op('STREAMDIFFUSION_HOST')
    checks = {}
    checks['managed_owner'] = host is not None and host.fetch('managed_owner', '') == 'flexgpu-streamdiffusion-host/v1'
    if host is None:
        return {'status': 'fail', 'checks': checks}
    checks['shared_rgb_input'] = all(
        list(adapter.op(name).inputs) in ([host], [host.op('OUT_RGB')])
        for name in ('MOGE2_BRIDGE', 'DEPTH_ANYTHING_GEOMETRY_BRIDGE'))
    retired = bool(host.fetch('legacy_retired', False))
    checks['accepted_upstream_preserved_or_retired'] = (
        len(host.inputs) == 0 if retired else len(host.inputs) == 1 and host.inputs[0] != host)
    checks['rgb_output_boundary'] = bool(adapter.op('OUT_RGB').inputs) and adapter.op('OUT_RGB').inputs[0].name == 'GENERATED_GEOMETRY_RGB_ROUTE'
    checks['depth_output_boundary'] = bool(adapter.op('OUT_DEPTH').inputs) and adapter.op('OUT_DEPTH').inputs[0].name == 'GENERATED_GEOMETRY_DEPTH_ROUTE'
    checks['smoothing_resolution'] = host.op('SMOOTH').par.outputresolution.eval() == 'useinput'
    checks['smoothing_bounds'] = 0 <= host.par.Smoothms.eval() <= 250
    checks['no_shader_errors'] = not host.op('SMOOTH').errors()
    checks['drop_slot_present'] = host.op('PAYLOAD_SLOT') is not None
    checks['noise_input_present'] = (host.op('INPUT_NOISE') is not None and
                                     host.op('GENERATOR_INPUT') is not None and
                                     host.op('OUT_INPUT') is not None)
    if retired:
        checks['legacy_generators_removed'] = all(adapter.op(n) is None for n in
                                                  ('StreamDiffusionTD', 'StreamDiffusionTD4', 'null1', 'null2'))
        checks['legacy_source_removed'] = list(host.par.Source.menuNames) == ['slot']
    checks['single_generator_workflow'] = host.fetch('workflow_version', 0) == 2
    checks['audio_prompt_controls'] = all(hasattr(host.par, n) for n in
                                         ('Followpodcast', 'Promptformat', 'Promptstatus'))
    checks['explicit_delivery_gate'] = list(host.op('OUT_RGB').inputs) == [host.op('STREAM_GATE')]
    checks['bridge_source_gates'] = all(
        hasattr(adapter.op(name).par, 'Sourceactive') and
        hasattr(adapter.op(name).par, 'Sourceepoch')
        for name in ('MOGE2_BRIDGE', 'DEPTH_ANYTHING_GEOMETRY_BRIDGE'))
    checks['no_engine_started'] = not any(x.type == 'engine' for x in host.children)
    return {'status': 'pass' if all(checks.values()) else 'fail', 'checks': checks,
            'source': host.par.Source.eval(), 'smoothing_enabled': host.par.Smoothenabled.eval(),
            'ready': host.par.Ready.eval(), 'detail': host.par.Status.eval(),
            'stage': host.par.Stage.eval(), 'stream_connected': host.par.Streamenabled.eval(),
            'legacy_retired': retired, 'noise_enabled': host.par.Noiseenabled.eval(),
            'audio_prompt_status': host.par.Promptstatus.eval(),
            'live_visual_acceptance': 'separate; requires fresh generated geometry'}
