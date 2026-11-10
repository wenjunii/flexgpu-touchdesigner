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
    checks['accepted_upstream_preserved'] = len(host.inputs) == 1 and host.inputs[0] != host
    checks['rgb_output_boundary'] = bool(adapter.op('OUT_RGB').inputs) and adapter.op('OUT_RGB').inputs[0].name == 'GENERATED_GEOMETRY_RGB_ROUTE'
    checks['depth_output_boundary'] = bool(adapter.op('OUT_DEPTH').inputs) and adapter.op('OUT_DEPTH').inputs[0].name == 'GENERATED_GEOMETRY_DEPTH_ROUTE'
    checks['smoothing_resolution'] = host.op('SMOOTH').par.outputresolution.eval() == 'useinput'
    checks['smoothing_bounds'] = 0 <= host.par.Smoothms.eval() <= 250
    checks['no_shader_errors'] = not host.op('SMOOTH').errors()
    checks['drop_slot_present'] = host.op('PAYLOAD_SLOT') is not None
    checks['no_engine_started'] = not any(x.type == 'engine' for x in host.children)
    return {'status': 'pass' if all(checks.values()) else 'fail', 'checks': checks,
            'source': host.par.Source.eval(), 'smoothing_enabled': host.par.Smoothenabled.eval(),
            'ready': host.par.Ready.eval(), 'detail': host.par.Status.eval(),
            'live_visual_acceptance': 'separate; requires fresh generated geometry'}
