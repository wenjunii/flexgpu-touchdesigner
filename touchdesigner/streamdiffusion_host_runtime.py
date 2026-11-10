"""Standalone runtime embedded in the public StreamDiffusion host DAT.

No private component code is inspected. Only explicitly selected public TOPs
and top-level custom parameters are used. Importing this module does nothing.
"""
from __future__ import print_function

import math
import os
import re
import time
import uuid


CONTROL_MAP = {
    "Sdsource": "Source", "Sdsmoothenabled": "Smoothenabled",
    "Sdsmoothms": "Smoothms", "Sdtoxfile": "Toxfile",
    "Sdpayload": "Payload", "Sdrgboutput": "Rgboutput",
    "Sdfollowpodcast": "Followpodcast", "Sdpromptparameter": "Promptparameter",
    "Sdseedparameter": "Seedparameter", "Sdstartparameter": "Startparameter",
    "Sdstopparameter": "Stopparameter",
    "Sdstreamenabled": "Streamenabled",
    "Sdnoiseenabled": "Noiseenabled", "Sdnoisewidth": "Noisewidth",
    "Sdnoiseheight": "Noiseheight", "Sdnoiseseed": "Noiseseed",
}
PULSE_MAP = {"Sdreset": "Reset", "Sdloadtox": "Loadtox",
             "Sdstart": "Start", "Sdstop": "Stop", "Sdopencontrols": "Opencontrols",
             "Sdopenpreview": "Openpreview", "Sdopennoisecontrols": "Opennoisecontrols",
             "Sdopennoisepreview": "Opennoisepreview", "Sdoffload": "Offload"}
NOISE_QUICK_MAP = {'Noisewidth': 'resolutionw', 'Noiseheight': 'resolutionh', 'Noiseseed': 'seed'}


def history_weight(elapsed, response_ms, enabled=True):
    """Frame-rate independent exponential history retention (not interpolation)."""
    if not enabled:
        return 0.0
    dt, ms = float(elapsed), float(response_ms)
    if not math.isfinite(dt) or not math.isfinite(ms) or dt <= 0 or ms <= 0:
        return 0.0
    return min(0.98, math.exp(-min(dt, 1.0) / (min(ms, 250.0) / 1000.0)))


def filter_elapsed(state, cook_frame, now):
    """Measure actual filter cook cadence, not the UI callback frequency."""
    if state.get('cook_frame') != cook_frame:
        state['filter_time'] = state.get('last_time')
        state['cook_frame'] = cook_frame
    previous = state.get('filter_time')
    state['last_time'] = now
    return 0.0 if previous is None else max(0.0, now - previous)


def _get(owner, name, default=None):
    try:
        value = getattr(owner.par, name).eval()
        return default if value is None else value
    except Exception:
        return default


def _set(owner, name, value):
    p = getattr(owner.par, name, None)
    if p is not None and p.eval() != value:
        p.val = value


def _state(host):
    state = host.fetch('_sd_host_state', None)
    if state is None:
        state = {'last_time': None, 'key': None, 'resets': 0, 'last_status': 0,
                 'message': '', 'syncing': False, 'error': ''}
        host.store('_sd_host_state', state)
    return state


def selected_payload(host):
    """Select only a direct child of the drop slot, never arbitrary network code."""
    slot = host.op('PAYLOAD_SLOT')
    if slot is None:
        return None
    name = str(_get(host, 'Payload', '')).strip()
    candidates = [x for x in slot.children if x.family == 'COMP']
    if not name and len(candidates) == 1:
        return candidates[0]
    if name and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name):
        node = slot.op(name)
        if node is not None and node.family == 'COMP' and node.parent() == slot:
            return node
    return None


def public_rgb(payload, relative_path):
    """Resolve one explicitly named public RGB TOP, without enumerating internals."""
    path = str(relative_path).strip()
    if not path or path.startswith('/') or '..' in path.split('/'):
        return None
    node = payload.op(path)
    if node is not None and node.family == 'TOP' and (
            node.path.startswith(payload.path + '/')):
        return node
    return None


def wire_noise(payload, noise_top, enabled):
    """Touch only public input zero; never replace a user's different input."""
    inputs = list(payload.inputs)
    own_input = bool(inputs) and inputs[0] == noise_top
    connectors = list(payload.inputConnectors)
    if not enabled:
        if own_input:
            connectors[0].disconnect()
        return
    if not connectors:
        raise ValueError('Generator has no public TOP input 0; disable Feed Noise for this version')
    if inputs and not own_input:
        raise ValueError('Generator input 0 is occupied; disconnect it or disable Feed Noise')
    if not own_input:
        noise_top.outputConnectors[0].connect(connectors[0])
        if not payload.inputs or payload.inputs[0] != noise_top:
            raise ValueError('Public generator input connection was not established')


def slot_occupied(slot):
    """Ignore only the exact owned transport TOP, never arbitrary user nodes."""
    return any(not (x.name == 'INPUT_FROM_HOST' and x.family == 'TOP'
                    and getattr(x, 'type', '') == 'select'
                    and x.fetch('managed_owner', '') == 'flexgpu-streamdiffusion-input/v1')
               for x in slot.children)


def attach_podcast_controller(host, controller):
    """Keep prompt/caption/audio sequencing without the retired two renderers.

    These are public controller hooks, not private component implementation.
    The external home-podcast module/class is not changed.
    """
    if not host.fetch('legacy_retired', False) or getattr(controller, '_flexgpu_single_source', False):
        return controller
    controller._write_streamdiffusion = lambda *args, **kwargs: 'single_generator_host'
    controller._write_color_controls = lambda *args, **kwargs: 'point_cloud_color_controls'
    controller._flexgpu_single_source = True
    return controller


def construct_podcast_controller(host, controller_type, owner):
    """Retain approved sequencing without constructing the retired dual rig.

    The reviewed public five-second shim's inherited __init__ checks only for
    the two deleted semantic TOPs, then delegates to its base. Skip that one
    obsolete constructor, not its scene-approval loader or other MRO behavior.
    No external class/module or private component implementation is modified.
    """
    if not host.fetch('legacy_retired', False):
        return controller_type(owner)
    initializer = controller_type.__init__
    code = getattr(initializer, '__code__', None)
    obsolete_guard = None
    if code is not None and os.path.basename(code.co_filename) == 'five_second_td_controller.py':
        def literals(item):
            if isinstance(item, str):
                return {item}
            items = getattr(item, 'co_consts', item if isinstance(item, tuple) else ())
            return set().union(*(literals(x) for x in items)) if items else set()
        expected = {'Semanticneutralready', 'Semanticneutralmix', 'semantic_neutral_output_',
                    'Native semantic-neutral output rig is not installed/verified; do not use the staged controller in .34'}
        if (literals(code) != expected or set(code.co_names) !=
                {'getattr', 'par', 'eval', 'any', 'RuntimeError', 'super', '__init__'}):
            raise RuntimeError('The public legacy-rig constructor changed; review before adapting it')
        obsolete_guard = next((c for c in controller_type.__mro__
                               if c.__dict__.get('__init__') is initializer), None)

    class SingleGeneratorPodcastController(controller_type):
        def _write_streamdiffusion(self, *args, **kwargs):
            return 'single_generator_host'

        def _write_color_controls(self, *args, **kwargs):
            return 'point_cloud_color_controls'

    if obsolete_guard is not None:
        def initialize_single(instance, owner_comp):
            super(obsolete_guard, instance).__init__(owner_comp)
        SingleGeneratorPodcastController.__init__ = initialize_single
    result = SingleGeneratorPodcastController(owner)
    result._flexgpu_single_source = True
    return result


def reset(host, reason='manual'):
    host.op('HISTORY').par.resetpulse.pulse()
    host.op('SMOOTH').par.vec0valuex = 0.0
    state = _state(host)
    state['resets'] += 1
    state['message'] = 'History reset: ' + str(reason)
    state['last_time'] = None
    state['filter_time'] = None
    state['cook_frame'] = None


def disarm(host, reason='disconnected'):
    """Close delivery, not the model server; invalidate prior geometry epochs."""
    _set(host, 'Streamenabled', False)
    gate = host.op('STREAM_GATE')
    if gate is not None:
        _set(gate, 'index', 0)
    host.store('stream_epoch', uuid.uuid4().hex[:16])
    reset(host, reason)


def route_state(mode, payload_present, rgb_present, requested):
    """Public routing state; never infer that a private model is generating."""
    if mode not in ('slot', 'existing'):
        return 'invalid_source', False, False
    ready = mode == 'existing' or (payload_present and rgb_present)
    if not ready:
        return ('select_generator' if not payload_present else 'configure_rgb'), False, False
    return ('connected' if requested else 'preview'), True, bool(requested)


def _scene_token(host):
    # Weights interpolate continuously; only scene/seed identity resets history.
    dat = host.parent().op('prompt_out') if host.parent() is not None else None
    if dat is None or dat.numRows < 2:
        return ()
    header = [str(dat[0, col]) for col in range(dat.numCols)]
    fields = [header.index(x) for x in ('scene_id', 'seed') if x in header]
    return tuple(str(dat[row, col]) for row in range(1, dat.numRows) for col in fields)


def _mapped_parameter(payload, name):
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', str(name)):
        raise ValueError('Configure a top-level public parameter name first')
    p = getattr(payload.par, str(name), None)
    if p is None or not getattr(p, 'isCustom', False):
        raise ValueError('The selected version does not expose that custom parameter')
    return p


def server_flags(payload):
    if payload is None:
        return None
    flags = []
    for name in ('Serveractive', 'Streamactive'):
        p = getattr(payload.par, name, None)
        if p is None or not getattr(p, 'isCustom', False) or not getattr(p, 'isToggle', False):
            return None
        flags.append(bool(p.eval()))
    return tuple(flags)


def bind_public_controls(host, payload):
    """Recognize the documented SDTD public contract, not guessed internals."""
    state = _state(host)
    previous = state.get('auto_controls')
    path = payload.path if payload is not None else ''
    if previous and previous['path'] != path:
        for field, old in previous['fields'].items():
            if _get(host, field, '') == old:
                _set(host, field, '')
        state.pop('auto_controls', None)
    if payload is None or server_flags(payload) is None:
        return False
    for name, label in (('Startstream', 'Start Stream'), ('Stopstream', 'Stop Server')):
        p = getattr(payload.par, name, None)
        if (p is None or not getattr(p, 'isCustom', False) or not getattr(p, 'isPulse', False)
                or str(getattr(p, 'label', '')).strip().casefold() != label.casefold()):
            return False
    fields = dict(state.get('auto_controls', {}).get('fields', {}))
    for field, name in (('Startparameter', 'Startstream'), ('Stopparameter', 'Stopstream')):
        if not str(_get(host, field, '')).strip():
            _set(host, field, name)
            fields[field] = name
    state['auto_controls'] = {'path': path, 'fields': fields}
    return True


def _wake_shutdown_callbacks(host, request):
    time_comp = host.op('/local/time')
    if time_comp is None or bool(_get(time_comp, 'play', True)):
        return
    request['restore_paused'] = True
    root = host.op('/project1')
    if root is not None:
        request['paused_frame'] = float(root.time.frame)
    host.store('offload_wake_timeline', True)
    _set(time_comp, 'play', True)


def _restore_shutdown_callbacks(host, request):
    if request.pop('restore_paused', False):
        time_comp = host.op('/local/time')
        if time_comp is not None:
            _set(time_comp, 'play', False)
        root = host.op('/project1')
        if root is not None and 'paused_frame' in request:
            root.time.frame = request['paused_frame']
    if host.fetch('offload_wake_timeline', False):
        host.store('offload_wake_timeline', False)


def offload_state(host, payload, now):
    request = _state(host).get('offload')
    if request is None:
        return None
    if payload is None or payload.path != request['path']:
        _restore_shutdown_callbacks(host, request)
        _state(host).pop('offload', None)
        return None
    flags = server_flags(payload)
    if flags == (False, False):
        request['confirmed'] = True
        _restore_shutdown_callbacks(host, request)
        return 'offloaded'
    if request.get('confirmed') and flags is not None and flags[0]:
        # The user explicitly restarted in native controls. Keep delivery off,
        # but allow preview/reconnection again without a stale offload lock.
        _state(host).pop('offload', None)
        return None
    if now - request['requested_at'] >= 20.0:
        _restore_shutdown_callbacks(host, request)
        return 'offload_unconfirmed'
    return 'offloading'


def request_offload(host, payload):
    disarm(host, 'offload requested')
    if payload is None:
        raise ValueError('No selected generator is loaded')
    bind_public_controls(host, payload)
    p = _mapped_parameter(payload, _get(host, 'Stopparameter', ''))
    if not getattr(p, 'isPulse', False):
        raise ValueError('Configure a public Stop Server Pulse parameter for this version')
    prior = _state(host).get('offload')
    if prior is not None:
        _restore_shutdown_callbacks(host, prior)
    request = {'path': payload.path, 'requested_at': time.perf_counter(), 'confirmed': False}
    _state(host)['offload'] = request
    if server_flags(payload) != (False, False):
        _wake_shutdown_callbacks(host, request)
        try:
            p.pulse()
        except Exception:
            _restore_shutdown_callbacks(host, request)
            _state(host).pop('offload', None)
            raise
    _state(host)['command'] = 'Offload requested through public Stop Server; component retained'


def forward_value(host, field, value):
    payload = selected_payload(host)
    if payload is None:
        raise ValueError('Select a component in PAYLOAD_SLOT first')
    parameter = _mapped_parameter(payload, _get(host, field + 'parameter', ''))
    if parameter.eval() != value:
        parameter.val = value


def _follow_podcast(host, payload):
    dat = host.parent().op('prompt_out')
    if payload is None or dat is None or dat.numRows < 2:
        return
    header = [str(dat[0, col]) for col in range(dat.numCols)]
    if not all(x in header for x in ('concept', 'seed', 'weight')):
        raise ValueError('Podcast prompt_out has an unsupported public schema')
    row = max(range(1, dat.numRows), key=lambda i: float(dat[i, header.index('weight')]))
    forward_value(host, 'Prompt', str(dat[row, header.index('concept')]))
    forward_value(host, 'Seed', int(float(str(dat[row, header.index('seed')]))))


def _sync_show(host):
    owner = host
    for _ in range(3):
        owner = owner.parent() if owner is not None else None
    show = owner.op('SHOW_CONTROL') if owner is not None else None
    if show is None:
        return
    state = _state(host)
    state['syncing'] = True
    try:
        for target, source in CONTROL_MAP.items():
            _set(show, target, _get(host, source))
        _set(show, 'Sdstatus', str(_get(host, 'Status', '')))
        _set(show, 'Sdstage', str(_get(host, 'Stage', '')))
        _set(show, 'Sdoffloadstatus', str(_get(host, 'Offloadstatus', 'not_requested')))
    finally:
        state['syncing'] = False


def _sync_noise(host):
    """The native TOP owns settings; mirror only integer convenience controls."""
    noise = host.op('INPUT_NOISE')
    if noise is None:
        return
    state = _state(host)
    state['noise_syncing'] = True
    try:
        mirrors = state.setdefault('noise_mirrors', {})
        seen = state.setdefault('noise_native_values', {})
        for quick, native in NOISE_QUICK_MAP.items():
            value = _get(noise, native, _get(host, quick))
            if (quick in seen and value == seen[quick] and quick in mirrors
                    and _get(host, quick) != mirrors[quick]):
                # Honor a direct host edit even if a frame tick beats the
                # deferred Parameter Execute callback to it.
                _apply_noise_quick(host, quick, _get(host, quick))
                value = _get(noise, native, value)
            _set(host, quick, int(float(value)))
            # Use the actual (possibly bounded) quick value. A delayed callback
            # must not clamp a native fractional/large value back to this mirror.
            mirrors[quick] = _get(host, quick)
            seen[quick] = value
    finally:
        state['noise_syncing'] = False


def _apply_noise_quick(host, name, value):
    native = NOISE_QUICK_MAP.get(name)
    noise = host.op('INPUT_NOISE')
    if native is None or noise is None:
        return
    p = getattr(noise.par, native)
    if hasattr(p, 'mode'):
        p.mode = type(p.mode).CONSTANT
    p.val = value
    if name in ('Noisewidth', 'Noiseheight'):
        _set(noise, 'outputresolution', 'custom')
    _state(host).setdefault('noise_mirrors', {})[name] = value


def host_changed(host, parameter):
    state = _state(host)
    if state.get('noise_syncing'):
        return
    name, value = str(parameter.name), parameter.eval()
    if name in NOISE_QUICK_MAP and state.get('noise_mirrors', {}).get(name) != value:
        _apply_noise_quick(host, name, value)
    tick(host)


def tick(host):
    state = _state(host)
    if state.get('ticking'):
        return
    state['ticking'] = True
    try:
        _sync_noise(host)
        now = time.perf_counter()
        mode = str(_get(host, 'Source', 'slot'))
        retired = bool(host.fetch('legacy_retired', False))
        if retired and mode != 'slot':
            _set(host, 'Source', 'slot')
            mode = 'slot'
        payload = selected_payload(host)
        compatible = bind_public_controls(host, payload)
        watcher = host.op('generator_state_callbacks')
        if watcher is not None:
            if payload is not None and server_flags(payload) is not None:
                _set(watcher, 'op', payload.path)
                _set(watcher, 'active', True)
            else:
                _set(watcher, 'active', False)
        noise_boundary = host.op('PAYLOAD_SLOT/INPUT_FROM_HOST')
        if payload is not None and noise_boundary is not None:
            wire_noise(payload, noise_boundary, bool(_get(host, 'Noiseenabled', True)))
        rgb = public_rgb(payload, _get(host, 'Rgboutput', 'out1')) if payload else None
        identity = (mode, payload.path if payload else '', str(_get(host, 'Rgboutput', 'out1')),
                    str(_get(host, 'Toxfile', '')), bool(_get(host, 'Noiseenabled', True)))
        if state.get('source_identity') != identity:
            disarm(host, 'generator / output selection changed')
            state['source_identity'] = identity
            state['command'] = ''
        stage, ready, connected = route_state(mode, payload is not None, rgb is not None,
                                             bool(_get(host, 'Streamenabled', False)))
        shutdown = offload_state(host, payload, now)
        if shutdown is not None:
            stage, ready, connected = shutdown, False, False
        _set(host, 'Offloadstatus', shutdown or 'not_requested')
        if state.get('connected') != connected:
            host.store('stream_epoch', uuid.uuid4().hex[:16])
            reset(host, 'RGB delivery connection changed')
            state['connected'] = connected
        if not ready:
            _set(host, 'Streamenabled', False)
        # No automatic legacy fallback: a missing generator must not masquerade
        # as its still-cached predecessor. Legacy is an explicit rollback source.
        target = rgb if mode == 'slot' and rgb is not None and shutdown is None else (
            host.op('EXISTING_RGB') if mode == 'existing' else host.op('DISCONNECTED'))
        _set(host.op('PAYLOAD_RGB'), 'top', target.path)
        _set(host.op('RAW_RGB'), 'index', 0 if retired or mode == 'existing' else 1)
        raw = host.op('RAW_RGB')
        key = (mode, target.path, int(raw.width), int(raw.height), _scene_token(host),
               bool(_get(host, 'Smoothenabled', False)), float(_get(host, 'Smoothms', 50)),
               _get(host, 'Noisewidth', 512), _get(host, 'Noiseheight', 512), _get(host, 'Noiseseed', 1))
        if state['key'] != key:
            reset(host, 'source / scene / resolution / smoothing change')
            state['key'] = key
        dt = filter_elapsed(state, int(host.op('SMOOTH').cookAbsFrame), now)
        weight = history_weight(dt, _get(host, 'Smoothms', 50), _get(host, 'Smoothenabled', False))
        _set(host.op('SMOOTH'), 'vec0valuex', weight)
        _set(host.op('OUTPUT_ROUTE'), 'index', 1 if _get(host, 'Smoothenabled', False) else 0)
        _set(host.op('STREAM_GATE'), 'index', 1 if connected else 0)
        message = {
            'select_generator': '1. Select trusted .tox and Load Selected Generator',
            'configure_rgb': '2. Configure the documented public RGB TOP; delivery is disconnected',
            'preview': '2. Run generation in Generator Controls, inspect OUT_RAW, then 3. Send RGB to 3D',
            'connected': 'RGB delivery connected; geometry bridge status confirms fresh synchronized results',
            'invalid_source': 'Invalid source; delivery is disconnected',
            'offloading': 'Stopping selected generator server; RGB delivery disconnected',
            'offloaded': 'Public server/stream flags are inactive; generator .tox retained for restart',
            'offload_unconfirmed': 'Stop requested but shutdown is not confirmed; check native Stop Server controls',
        }[stage]
        if mode == 'existing':
            message = 'Legacy rollback: ' + message
        if mode == 'slot' and ready and _get(host, 'Followpodcast', False):
            _follow_podcast(host, payload)
        if state.get('command'):
            message += ' | ' + state['command']
        if state.get('error'):
            message += ' | ' + state['error']
        _set(host, 'Ready', ready)
        _set(host, 'Stage', stage)
        _set(host, 'Status', message)
        _sync_show(host)
        if now - state['last_status'] >= 0.5:
            dat = host.op('STATUS')
            dat.clear()
            for key, value in [('metric', 'value'), ('source', mode), ('stage', stage),
                               ('ready', ready), ('stream_connected', connected),
                               ('offload_state', shutdown or 'not_requested'),
                               ('public_control_contract', compatible),
                               ('smoothing_enabled', _get(host, 'Smoothenabled', False)),
                               ('response_ms', _get(host, 'Smoothms', 50)),
                               ('history_weight', round(weight, 6)), ('resets', state['resets']),
                               ('width', raw.width), ('height', raw.height), ('detail', message),
                               ('note', 'One selected generator; routing only, not model/server readiness')]:
                dat.appendRow([str(key), str(value)])
            state['last_status'] = now
    except Exception as exc:
        disarm(host, 'routing error')
        _set(host, 'Ready', False)
        _set(host, 'Stage', 'error')
        _set(host, 'Status', type(exc).__name__ + ': ' + str(exc))
        _sync_show(host)
    finally:
        state['ticking'] = False


def pulse(host, name):
    try:
        _state(host)['error'] = ''
        if name == 'Reset':
            reset(host)
        elif name == 'Loadtox':
            disarm(host, 'load requested')
            path = os.path.abspath(os.path.expanduser(str(_get(host, 'Toxfile', ''))))
            if not path.lower().endswith('.tox') or not os.path.isfile(path):
                raise ValueError('Choose an existing trusted local .tox file')
            slot = host.op('PAYLOAD_SLOT')
            if slot_occupied(slot):
                raise ValueError('Drop slot is occupied; preserve/stop the old version before loading another')
            candidate = slot.loadTox(path, unwired=True)
            _set(host, 'Payload', candidate.name)
            reset(host, 'component loaded; no start command sent')
        elif name == 'Opencontrols':
            payload = selected_payload(host)
            if payload is None:
                raise ValueError('Load the selected generator first')
            payload.openParameters()
        elif name in ('Openpreview', 'Opennoisepreview'):
            target = host.op('OUT_RAW' if name == 'Openpreview' else 'OUT_INPUT')
            if target is None:
                raise ValueError('Preview TOP is unavailable; reinstall the public host')
            target.openViewer(unique=False, borders=True)
        elif name == 'Opennoisecontrols':
            noise = host.op('INPUT_NOISE')
            if noise is None:
                raise ValueError('Noise TOP is unavailable; reinstall the public host')
            noise.openParameters()
        elif name == 'Offload':
            request_offload(host, selected_payload(host))
        elif name in ('Start', 'Stop'):
            # Closing delivery must succeed even if a version has no Stop API.
            disarm(host, name.lower() + ' requested')
            payload = selected_payload(host)
            if payload is None:
                raise ValueError('Select a component in PAYLOAD_SLOT first')
            compatible = bind_public_controls(host, payload)
            if name == 'Stop' and compatible and _get(host, 'Stopparameter', '') == 'Stopstream':
                request_offload(host, payload)
                tick(host)
                return
            if name == 'Start' and compatible and server_flags(payload)[0]:
                raise ValueError('Selected generator server is already active; Offload it before another start')
            noise_boundary = host.op('PAYLOAD_SLOT/INPUT_FROM_HOST')
            if name == 'Start' and noise_boundary is not None:
                wire_noise(payload, noise_boundary, bool(_get(host, 'Noiseenabled', True)))
            parameter = _mapped_parameter(payload, _get(host, name + 'parameter', ''))
            if not getattr(parameter, 'isPulse', False):
                raise ValueError('Start/Stop mappings must name custom Pulse parameters')
            if name == 'Start':
                previous = _state(host).pop('offload', None)
                if previous is not None:
                    _restore_shutdown_callbacks(host, previous)
                # A deliberate standalone Start needs running callbacks/video.
                # Do not start podcast playback/audio on the user's behalf.
                if (str(_get(host, 'Source', 'slot')) == 'slot'
                        and not bool(_get(host, 'Followpodcast', False))):
                    time_comp = host.op('/local/time')
                    if time_comp is not None:
                        _set(time_comp, 'play', True)
            parameter.pulse()
            _state(host)['command'] = name + ' pulse sent; confirm in generator controls'
            reset(host, name.lower())
        tick(host)
    except Exception as exc:
        _state(host)['error'] = type(exc).__name__ + ': ' + str(exc)
        _set(host, 'Status', _state(host)['error'])
        _sync_show(host)


def control_changed(host, parameter, is_pulse=False):
    if _state(host)['syncing']:
        return
    name = str(parameter.name)
    if is_pulse and name in PULSE_MAP:
        pulse(host, PULSE_MAP[name])
    elif name in CONTROL_MAP:
        target, value = CONTROL_MAP[name], parameter.eval()
        if _get(host, target) == value:
            return
        _set(host, target, value)
        if target in NOISE_QUICK_MAP:
            _apply_noise_quick(host, target, value)
        tick(host)
