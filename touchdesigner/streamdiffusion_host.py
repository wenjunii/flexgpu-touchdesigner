"""Bounded, non-destructive StreamDiffusion RGB host + temporal smoothing builder."""
from __future__ import print_function

from pathlib import Path

try:
    import runtime_pipeline as rp
except ImportError:
    from touchdesigner import runtime_pipeline as rp


HOST_NAME = 'STREAMDIFFUSION_HOST'
OWNER = 'flexgpu-streamdiffusion-host/v1'
VALUE_CONTROLS = ('Sdsource', 'Sdsmoothenabled', 'Sdsmoothms', 'Sdtoxfile',
                  'Sdpayload', 'Sdrgboutput', 'Sdfollowpodcast', 'Sdpromptparameter',
                  'Sdseedparameter', 'Sdstartparameter', 'Sdstopparameter',
                  'Sdstreamenabled')
PULSE_CONTROLS = ('Sdreset', 'Sdloadtox', 'Sdstart', 'Sdstop', 'Sdopencontrols')
STATUS_CONTROLS = ('Sdstatus', 'Sdstage')
PIXEL = '''uniform float uHistory;
out vec4 fragColor;
void main() {
    vec4 current = texture(sTD2DInputs[0], vUV.st);
    vec4 previous = texture(sTD2DInputs[1], vUV.st);
    if (any(isnan(previous)) || any(isinf(previous))) previous = current;
    fragColor = TDOutputSwizzle(mix(current, previous, clamp(uHistory, 0.0, 0.98)));
}
'''
HOST_CALLBACKS = '''def onFrameStart(frame):
    parent().op('host_runtime').module.tick(parent())
    return

def onStart():
    parent().op('host_runtime').module.disarm(parent(), 'project reopened')
    return
'''
PARAM_CALLBACKS = '''def onValueChange(par, prev):
    parent().op('host_runtime').module.tick(parent())
    return
def onPulse(par):
    parent().op('host_runtime').module.pulse(parent(), par.name)
    return
'''
SHOW_CALLBACKS = '''def _host():
    return parent().parent().op('SOURCES/STREAMDIFFUSION_ADAPTER/STREAMDIFFUSION_HOST')
def onValueChange(par, prev):
    h = _host()
    h.op('host_runtime').module.control_changed(h, par)
    return
def onPulse(par):
    h = _host()
    h.op('host_runtime').module.control_changed(h, par, True)
    return
'''


def podcast_timeline_guard(source):
    """Keep the old podcast Play switch from pausing a standalone generator."""
    marker = '# FLEXGPU_SINGLE_GENERATOR_TIMELINE'
    if marker in source:
        return source
    needle = '    if play_parameter is not None:\n'
    if source.count(needle) != 1:
        raise RuntimeError('Unrecognized public podcast timeline callback; review before installing')
    return source.replace(needle,
        '    ' + marker + '\n'
        '    host = owner.op("STREAMDIFFUSION_HOST")\n'
        '    podcast_owns_timeline = (host is None or host.par.Source.eval() == "existing"\n'
        '                             or bool(host.par.Followpodcast.eval()))\n'
        '    if play_parameter is not None and podcast_owns_timeline:\n', 1)


def install(root=None):
    """Preserve private/podcast nodes, ports, workers, and RGB/depth outputs.

    Only the two geometry input wires are redirected through this host. No
    private component is loaded, moved, started, deleted, externalized or saved.
    """
    root = rp._op(rp.ROOT_PATH) if root is None else (rp._op(root) if isinstance(root, str) else root)
    if root is None:
        raise RuntimeError('FlexGPU root is missing')
    pipeline = root.op('WORKING_PIPELINE')
    if pipeline is None:
        raise RuntimeError('WORKING_PIPELINE is missing')
    adapter = pipeline.op('SOURCES/STREAMDIFFUSION_ADAPTER')
    if adapter is None:
        raise RuntimeError('STREAMDIFFUSION_ADAPTER is missing')
    podcast_callbacks = adapter.op('execute_callbacks')
    guarded_podcast = None
    if podcast_callbacks is not None:
        if bool(rp._value(podcast_callbacks, 'syncfile', False)):
            raise RuntimeError('Public podcast callback is file-synced; review its source before installing')
        guarded_podcast = podcast_timeline_guard(podcast_callbacks.text)
    bridges = [adapter.op(x) for x in ('MOGE2_BRIDGE', 'DEPTH_ANYTHING_GEOMETRY_BRIDGE')]
    if any(x is None for x in bridges):
        raise RuntimeError('Both generated-geometry bridges must be installed first')
    host = adapter.op(HOST_NAME)
    if host is not None and host.fetch('managed_owner', None) != OWNER:
        raise RuntimeError('Refusing to overwrite an unrecognized host')
    if host is not None:
        source = rp._first_input(host)
    else:
        sources = [rp._first_input(x) for x in bridges]
        if any(x is None for x in sources) or sources[0] != sources[1]:
            raise RuntimeError('Geometry bridges do not share one accepted RGB source')
        source = sources[0]
    if source is None or source == host:
        raise RuntimeError('Accepted upstream RGB source is unavailable')
    report = rp.BuildReport()
    host = rp._ensure(adapter, 'baseCOMP', HOST_NAME, report)
    host.store('managed_owner', OWNER)
    right_edge = max((float(x.nodeX) + float(x.nodeWidth) for x in adapter.children if x != host), default=0)
    rp._style(host, right_edge + 300, 600, (0.15, 0.45, 0.55),
              'Stable RGB host; drop private tox in PAYLOAD_SLOT; smoothing defaults OFF', 300, 125)
    page = rp._page(host, 'StreamDiffusion Host')
    specs = [
        ('Menu', 'Source', 'slot', ('slot', 'existing'), 'Pipeline Source'),
        ('File', 'Toxfile', '', None, '1. Trusted Local StreamDiffusion .tox'),
        ('Pulse', 'Loadtox', False, None, '1. Load Selected Generator'),
        ('Pulse', 'Opencontrols', False, None, '2. Open Generator Controls'),
        ('Str', 'Payload', '', None, 'Selected Drop-Slot Component Name'),
        ('Str', 'Rgboutput', 'out1', None, 'Public RGB TOP Relative to Component'),
        ('Str', 'Startparameter', '', None, 'Public Start Pulse Parameter'),
        ('Str', 'Stopparameter', '', None, 'Public Stop Pulse Parameter'),
        ('Pulse', 'Start', False, None, '2. Start Generator (Mapped Pulse)'),
        ('Pulse', 'Stop', False, None, 'Stop Generator / Disconnect RGB'),
        ('Toggle', 'Streamenabled', False, None, '3. Send RGB to 3D'),
        ('Toggle', 'Smoothenabled', False, None, 'Temporal Smoothing'),
        ('Float', 'Smoothms', 50.0, None, 'Smoothing Response (ms)'),
        ('Pulse', 'Reset', False, None, 'Reset History'),
        ('Toggle', 'Followpodcast', False, None, 'Follow Podcast Dominant Prompt / Seed'),
        ('Str', 'Promptparameter', 'Prompt', None, 'Public Prompt Parameter'),
        ('Str', 'Seedparameter', 'Seed', None, 'Public Seed Parameter'),
        ('Toggle', 'Ready', False, None, 'Public RGB Route Configured'),
        ('Str', 'Stage', 'select_generator', None, 'Pipeline Stage'),
        ('Str', 'Status', 'Select and load one generator', None, 'Host Status'),
    ]
    for order, (kind, name, default, menu, label) in enumerate(specs):
        rp._custom(host, page, kind, name, default, menu, label,
                   minimum=0 if name == 'Smoothms' else None,
                   maximum=250 if name == 'Smoothms' else None)
        rp._par(host, name).order = order
        rp._par(host, name).label = label
    rp._par(host, 'Source').menuLabels = ['Selected Generator', 'Legacy Podcast (Rollback)']
    for name in ('Ready', 'Status', 'Stage'):
        rp._par(host, name).readOnly = True
    existing = rp._in_top(host, 'EXISTING_RGB', 0, report)
    slot = rp._ensure(host, 'baseCOMP', 'PAYLOAD_SLOT', report)
    rp._style(slot, -400, -200, (0.5, 0.2, 0.4), 'Drop a trusted private .tox HERE. Nothing is auto-started by the host.', 320, 125)
    selected = rp._ensure(host, 'selectTOP', 'PAYLOAD_RGB', report)
    rp._set(selected, 'top', existing.path)
    raw = rp._ensure(host, 'switchTOP', 'RAW_RGB', report)
    rp._connect(existing, raw, 0, report=report, replace=True)
    rp._connect(selected, raw, 1, report=report, replace=True)
    history = rp._ensure(host, 'feedbackTOP', 'HISTORY', report)
    rp._connect(raw, history, report=report, replace=True)
    shader = rp._text(host, 'SMOOTH_PIXEL', PIXEL, report)
    smooth = rp._ensure(host, 'glslTOP', 'SMOOTH', report)
    rp._set(smooth, ('pixeldat', 'pixelshader'), shader.path)
    rp._set(smooth, 'format', 'rgba16float')
    rp._set(smooth, 'outputresolution', 'useinput')
    rp._set(smooth, 'vec0name', 'uHistory')
    rp._set(smooth, 'vec0valuex', 0.0)
    rp._connect(raw, smooth, 0, report=report, replace=True)
    rp._connect(history, smooth, 1, report=report, replace=True)
    rp._set(history, ('targettop', 'target', 'top'), smooth.path)
    route = rp._ensure(host, 'switchTOP', 'OUTPUT_ROUTE', report)
    rp._connect(raw, route, 0, report=report, replace=True)
    rp._connect(smooth, route, 1, report=report, replace=True)
    blocked = rp._ensure(host, 'constantTOP', 'DISCONNECTED', report)
    for channel in ('colorr', 'colorg', 'colorb'):
        rp._set(blocked, channel, 0.0)
    rp._set(blocked, 'colora', 0.0)
    rp._set_resolution(blocked, 1, 1)
    gate = rp._ensure(host, 'switchTOP', 'STREAM_GATE', report)
    rp._connect(blocked, gate, 0, report=report, replace=True)
    rp._connect(route, gate, 1, report=report, replace=True)
    rp._out_top(host, 'OUT_RGB', gate, 0, report)
    rp._out_top(host, 'OUT_RAW', raw, 1, report)
    rp._table(host, 'STATUS', [('metric', 'value')], report)
    runtime = rp._text(host, 'host_runtime', Path(__file__).with_name('streamdiffusion_host_runtime.py').read_text(encoding='utf-8'), report)
    execute = rp._ensure(host, 'executeDAT', 'host_callbacks', report)
    execute.text = HOST_CALLBACKS
    rp._set(execute, 'framestart', True)
    rp._set(execute, 'start', True)
    rp._set(execute, 'active', True)
    params = rp._ensure(host, 'parameterexecuteDAT', 'host_parameter_callbacks', report)
    params.text = PARAM_CALLBACKS
    rp._set(params, 'op', host.path)
    rp._set(params, 'pars', '*')
    rp._set(params, 'valuechange', True)
    rp._set(params, 'onpulse', True)
    # Value changes only update routing. Loading/starting requires explicit pulses.
    rp._set(params, 'active', True)
    rp._text(host, 'README_FIRST',
             'STREAMDIFFUSION HOST\n\n'
             'ONE selected generator. 1: choose trusted .tox and Load. '
             '2: Open Generator Controls, configure and run it (or map Start). '
             'Preview OUT_RAW. 3: enable Send RGB to 3D.\n'
             'Drop a trusted private .tox inside PAYLOAD_SLOT. Select its direct child '
             'name and public RGB TOP (relative to that component).\n'
             'Load Selected Generator refuses an occupied slot and never deletes an existing version. '
             'Loading a trusted tox executes its own code; stop/unload other model '
             'servers first on a 16 GB GPU. The host never sends Start automatically.\n'
             'Prompt/Seed and Start/Stop mappings target only top-level custom '
             'parameters. Podcast dominant-prompt forwarding is optional and is '
             'not the original dual-stream transition compositor.\n'
             'Stop always disconnects RGB first, even if Stop mapping is missing. '
             'Disconnection does not itself unload an external model server. '
             'Cold reopen and generator/output changes require explicit reconnection. '
             'Legacy is rollback only; there is no A/B generator requirement.\n'
             'MoGe-2 and Depth Anything consume the same gated OUT_RGB. Do not change '
             'their downstream RGB/depth routes. Engine is not enabled by this installer.', report)
    positions = {
        'EXISTING_RGB': (-550, 300), 'PAYLOAD_SLOT': (-700, -50),
        'PAYLOAD_RGB': (-300, 0), 'RAW_RGB': (-50, 300),
        'HISTORY': (-50, 0), 'SMOOTH': (220, 150), 'SMOOTH_PIXEL': (220, -80),
        'OUTPUT_ROUTE': (490, 300), 'STREAM_GATE': (760, 300),
        'DISCONNECTED': (490, 40), 'OUT_RGB': (1030, 300), 'OUT_RAW': (220, 440),
        'STATUS': (760, 0), 'host_runtime': (-550, -350),
        'host_callbacks': (-220, -350), 'host_parameter_callbacks': (110, -350),
        'README_FIRST': (-550, -600),
    }
    for name, (x, y) in positions.items():
        node = host.op(name)
        node.nodeX, node.nodeY = x, y
    show = pipeline.op('SHOW_CONTROL')
    if show is not None:
        show_page = rp._page(show, 'StreamDiffusion')
        for order, (kind, name, default, menu, label) in enumerate(specs):
            key = 'Sd' + name.lower()
            if key in VALUE_CONTROLS + PULSE_CONTROLS + STATUS_CONTROLS:
                rp._custom(show, show_page, kind, key, rp._value(host, name, default), menu, label,
                           minimum=0 if name == 'Smoothms' else None,
                           maximum=250 if name == 'Smoothms' else None)
                rp._par(show, key).order = order
                rp._par(show, key).label = label
        rp._par(show, 'Sdsource').menuLabels = ['Selected Generator', 'Legacy Podcast (Rollback)']
        for name in STATUS_CONTROLS:
            rp._par(show, name).readOnly = True
        show_dat = rp._ensure(show, 'parameterexecuteDAT', 'streamdiffusion_callbacks', report)
        show_dat.text = SHOW_CALLBACKS
        rp._set(show_dat, 'op', show.path)
        rp._set(show_dat, 'pars', ' '.join(VALUE_CONTROLS + PULSE_CONTROLS))
        rp._set(show_dat, 'valuechange', True)
        rp._set(show_dat, 'onpulse', True)
        rp._set(show_dat, 'active', True)
        show_dat.nodeX = max((x.nodeX + x.nodeWidth for x in show.children if x != show_dat), default=0) + 180
        show_dat.nodeY = 130
    # Complete the managed host before touching the two accepted consumer wires.
    rp._connect(source, host, report=report, replace=True)
    for bridge in bridges:
        if not rp._connect(host, bridge, report=report, replace=True):
            raise RuntimeError('Could not connect the shared hosted RGB to geometry')
        bridge_page = rp._page(bridge, 'Source Gate')
        rp._custom(bridge, bridge_page, 'Toggle', 'Sourceactive', True,
                   label='Source Stream Connected')
        rp._custom(bridge, bridge_page, 'Str', 'Sourceepoch', '',
                   label='Source Connection Epoch')
        rp._expr(bridge, 'Sourceactive', "bool(op(%r).par.Streamenabled and op(%r).par.Ready)" %
                 (host.path, host.path))
        rp._expr(bridge, 'Sourceepoch', "op(%r).fetch('stream_epoch', '')" % host.path)
        rp._par(bridge, 'Sourceactive').readOnly = True
        rp._par(bridge, 'Sourceepoch').readOnly = True
        # Refresh only the public generated-geometry runtime, not provider settings.
        bridge.op('bridge_runtime').module.stop(bridge)
        bridge.op('bridge_runtime').text = rp._moge2_runtime_source(report)
    if host.fetch('workflow_version', 0) < 2:
        rp._set(host, 'Source', 'slot')
        rp._set(host, 'Streamenabled', False)
        host.store('workflow_version', 2)
    if guarded_podcast is not None and guarded_podcast != podcast_callbacks.text:
        host.store('legacy_podcast_callback_before_guard', podcast_callbacks.text)
        podcast_callbacks.text = guarded_podcast
    runtime.module.tick(host)
    host.store('install_report', report.as_dict())
    if report.warnings:
        raise RuntimeError('; '.join(report.warnings))
    print('[FlexGPU] Single-generator pipeline ready: select/load -> generate/preview -> send RGB to 3D; TOE unsaved')
    return host
