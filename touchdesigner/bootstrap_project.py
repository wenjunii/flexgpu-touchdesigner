"""Build the non-destructive FlexGPU TouchDesigner 2025 show project.

Run this module *inside TouchDesigner*.  It creates or updates only
``/project1/flexgpu`` and never removes unknown nodes.  Alongside stable adapter
contracts it installs the stock-operator ``WORKING_PIPELINE`` demo: animated
RGB/depth, reconstruction, persistence, sensor interaction, view completion,
GPU-native point rendering, installation output, and stereo preview.
"""

from __future__ import print_function

import json
import os


BUILD_VERSION = "1.1.0"
ROOT_PATH = "/project1/flexgpu"
LAST_REPORT = None


DEFAULTS = {
    "role": "standalone",          # standalone | world | ai
    "topology": "single",          # single | dual_local | dual_network
    "experience": "installation", # installation | vr | combined
    "completion": "hybrid",       # fog | procedural | hybrid
    "tier": "3080ti_16gb",
    "safe_mode": True,
    "ai_enabled": True,
    "sensor_enabled": True,
    "installation_fps": 60,
    "vr_fps": 72,
    "diffusion_fps": 10,
    "diffusion_resolution": 512,
    "geometry_resolution": 384,
    "geometry_fps": 5,
    "point_budget": 120000,
    "worldbus_version": "1.0",
}


RUNTIME_HELPERS = r'''# FlexGPU runtime helpers (embedded by bootstrap_project.py)
import json
import os
import time

ENV_KEYS = {
    'FLEXGPU_ROLE': 'role',
    'FLEXGPU_TOPOLOGY': 'topology',
    'FLEXGPU_EXPERIENCE': 'experience',
    'FLEXGPU_COMPLETION': 'completion',
    'FLEXGPU_TIER': 'tier',
    'FLEXGPU_DIFFUSION_RESOLUTION': 'diffusion_resolution',
    'FLEXGPU_DIFFUSION_HZ': 'diffusion_fps',
    'FLEXGPU_GEOMETRY_RESOLUTION': 'geometry_resolution',
    'FLEXGPU_GEOMETRY_HZ': 'geometry_fps',
    'FLEXGPU_MAX_POINTS': 'point_budget',
    'FLEXGPU_VR_REFRESH_HZ': 'vr_fps',
    'FLEXGPU_TRANSPORT': 'transport_type',
    'FLEXGPU_TRANSPORT_SEGMENT': 'transport_segment',
    'FLEXGPU_PEER_HOST': 'transport_peer_host',
    'FLEXGPU_ATLAS_WIDTH': 'transport_atlas_width',
    'FLEXGPU_ATLAS_HEIGHT': 'transport_atlas_height',
    'FLEXGPU_ATLAS_PORT': 'transport_atlas_port',
    'FLEXGPU_TRANSPORT_FPS': 'transport_fps',
}

QUALITY_PRESETS = {
    '3080ti_16gb': {'diffusion_resolution':512, 'diffusion_fps':10,
                    'geometry_resolution':384, 'geometry_fps':5,
                    'point_budget':120000, 'vr_fps':72},
    '4090': {'diffusion_resolution':512, 'diffusion_fps':15,
             'geometry_resolution':512, 'geometry_fps':10,
             'point_budget':250000, 'vr_fps':90},
    '5090': {'diffusion_resolution':512, 'diffusion_fps':20,
             'geometry_resolution':512, 'geometry_fps':15,
             'point_budget':400000, 'vr_fps':90},
    'custom': {'diffusion_resolution':384, 'diffusion_fps':5,
               'geometry_resolution':256, 'geometry_fps':3,
               'point_budget':50000, 'vr_fps':60},
}

QUALITY_MINIMUMS = {
    '3080ti_16gb': {'diffusion_resolution':384, 'diffusion_fps':5,
                    'geometry_resolution':256, 'geometry_fps':3,
                    'point_budget':60000, 'vr_fps':72},
    '4090': {'diffusion_resolution':384, 'diffusion_fps':8,
             'geometry_resolution':256, 'geometry_fps':5,
             'point_budget':100000, 'vr_fps':90},
    '5090': {'diffusion_resolution':384, 'diffusion_fps':10,
             'geometry_resolution':256, 'geometry_fps':6,
             'point_budget':150000, 'vr_fps':90},
    'custom': {'diffusion_resolution':256, 'diffusion_fps':4,
               'geometry_resolution':256, 'geometry_fps':3,
               'point_budget':50000, 'vr_fps':60},
}

QUALITY_KEYS = ('diffusion_resolution', 'diffusion_fps',
                'geometry_resolution', 'geometry_fps',
                'point_budget', 'vr_fps')

def _par(comp, name):
    if comp is None:
        return None
    try:
        return getattr(comp.par, name)
    except Exception:
        pass
    try:
        wanted = str(name).lower()
        for parameter in comp.pars():
            if str(parameter.name).lower() == wanted:
                return parameter
    except Exception:
        pass
    return None

def _set(comp, name, value):
    p = _par(comp, name)
    if p is not None:
        try:
            p.val = value
            return True
        except Exception:
            pass
    return False

def _set_resolution(comp, width, height):
    if comp is None:
        return
    _set(comp, 'outputresolution', 'custom')
    _set(comp, 'resmult', False)
    if not _set(comp, 'resolutionw', int(width)):
        _set(comp, 'resw', int(width))
    if not _set(comp, 'resolutionh', int(height)):
        _set(comp, 'resh', int(height))

def _json(path):
    if not path:
        return {}
    try:
        with open(os.path.expandvars(os.path.expanduser(path)), 'r') as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except Exception as exc:
        print('[FlexGPU] runtime config warning: %s' % exc)
        return {}

def _lookup(data, key, default=None):
    if key in data:
        return data[key]
    for section in ('flexgpu', 'runtime', 'show', 'profile'):
        value = data.get(section)
        if isinstance(value, dict) and key in value:
            return value[key]
    return default

def _mapping(value):
    return dict(value) if isinstance(value, dict) else {}

def environment():
    values = {}
    config_path = os.environ.get('FLEXGPU_CONFIG', '')
    config = _json(config_path)
    for key in ('role', 'topology', 'experience', 'completion', 'tier'):
        value = _lookup(config, key)
        if value not in (None, ''):
            values[key] = value
    for key in QUALITY_KEYS:
        value = _lookup(config, key)
        if value not in (None, ''):
            values[key] = value
    for section in ('adaptive', 'telemetry', 'source', 'sensor', 'render', 'transport'):
        value = _lookup(config, section)
        if isinstance(value, dict):
            values[section] = dict(value)
    for env_name, key in ENV_KEYS.items():
        value = os.environ.get(env_name)
        if value:
            values[key] = value
    values['config_path'] = config_path
    return values

def _integer(value, fallback):
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(fallback)

def _number(value, fallback):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(fallback)

def _materialize(state):
    defaults = {'role':'standalone', 'topology':'single',
                'experience':'installation', 'completion':'hybrid',
                'tier':'3080ti_16gb'}
    for key, fallback in defaults.items():
        state[key] = str(state.get(key, fallback)).lower()
    if state['tier'] not in QUALITY_PRESETS:
        state['tier'] = 'custom'

    supplied = dict((key, state[key]) for key in QUALITY_KEYS if key in state)
    for key, value in QUALITY_PRESETS[state['tier']].items():
        state[key] = value
    render = _mapping(state.get('render'))
    render_quality = {'point_budget':'point_budget', 'vr_fps':'vr_fps',
                      'installation_fps':'installation_fps'}
    for source_key, state_key in render_quality.items():
        if source_key in render:
            state[state_key] = render[source_key]
    state.update(supplied)  # explicit FLEXGPU_* values have highest precedence

    for key in QUALITY_KEYS:
        state[key] = _integer(state[key], QUALITY_PRESETS[state['tier']][key])
    state['installation_fps'] = _integer(
        state.get('installation_fps', 60), 60)
    state['installation_width'] = _integer(
        render.get('installation_width', 1280), 1280)
    state['installation_height'] = _integer(
        render.get('installation_height', 720), 720)
    state['stereo_width'] = _integer(render.get('stereo_width', 2560), 2560)
    state['stereo_height'] = _integer(render.get('stereo_height', 720), 720)
    state['point_size_px'] = _number(render.get('point_size_px', 3.0), 3.0)
    state['fog_density'] = _number(render.get('fog_density', 0.35), 0.35)
    state['procedural_mix'] = _number(render.get('procedural_mix', 0.72), 0.72)

    transport = _mapping(state.get('transport'))
    transport_type = str(state.get(
        'transport_type', transport.get('type', 'local'))).strip().lower()
    segment = state.get('transport_segment',
                        transport.get('segment_name', 'FlexShowWorldBus'))
    peer_host = state.get('transport_peer_host',
                          transport.get('peer_host', '127.0.0.1'))
    atlas_width = _integer(state.get(
        'transport_atlas_width', transport.get('atlas_width', 1024)), 1024)
    atlas_height = _integer(state.get(
        'transport_atlas_height', transport.get('atlas_height', 512)), 512)
    atlas_port = _integer(state.get(
        'transport_atlas_port', transport.get('atlas_port', 12000)), 12000)
    transport_fps = _integer(state.get(
        'transport_fps', transport.get('atlas_fps', state['geometry_fps'])),
        state['geometry_fps'])
    state.update({'transport_type':transport_type,
                  'transport_segment':str(segment),
                  'transport_peer_host':str(peer_host),
                  'transport_atlas_width':atlas_width,
                  'transport_atlas_height':atlas_height,
                  'transport_atlas_port':atlas_port,
                  'transport_fps':transport_fps})

    # Config files are validated by the launcher, but process.env can override
    # these values after validation. Canonicalize aliases here and fail closed
    # instead of silently selecting a different transport on a split role.
    aliases = {'local':'local', 'in_process':'local', 'inprocess':'local',
               'shared_memory':'shared_memory', 'shared_mem':'shared_memory',
               'sharedmem':'shared_memory', 'touch_tcp':'touch_tcp',
               'touch':'touch_tcp', 'touch_in_out':'touch_tcp', 'tcp':'touch_tcp'}
    key = transport_type.replace('-', '_')
    canonical = aliases.get(key)
    transport_errors = []
    permitted = {'single':('local',),
                 'dual_local':('shared_memory', 'touch_tcp'),
                 'dual_network':('touch_tcp',)}.get(state['topology'], ())
    if canonical is None:
        transport_errors.append('unsupported transport.type %r' % transport_type)
    elif canonical not in permitted:
        transport_errors.append('transport.type %s is incompatible with %s' %
                                (canonical, state['topology']))
    if atlas_width < 2 or atlas_width > 16384 or atlas_width % 2:
        transport_errors.append('atlas_width must be even and between 2 and 16384')
    if atlas_height < 1 or atlas_height > 16384:
        transport_errors.append('atlas_height must be between 1 and 16384')
    if transport_fps < 1 or transport_fps > 240:
        transport_errors.append('atlas_fps must be between 1 and 240')
    if canonical == 'shared_memory' and not str(segment).strip():
        transport_errors.append('segment_name must not be empty')
    if canonical == 'touch_tcp':
        if not str(peer_host).strip():
            transport_errors.append('peer_host must not be empty')
        if atlas_port < 1 or atlas_port > 65535:
            transport_errors.append('atlas_port must be between 1 and 65535')
        if (state['topology'] == 'dual_local' and
                str(peer_host).strip().lower() not in ('127.0.0.1', 'localhost', '::1')):
            transport_errors.append('dual_local touch_tcp peer_host must be loopback')
    state['transport_type'] = canonical or 'invalid'
    if transport_errors:
        state['transport_error'] = '; '.join(transport_errors)
        # Keep the inactive diagnostic graph inside legal parameter ranges.
        safe_width = min(16384, max(2, atlas_width))
        state['transport_atlas_width'] = safe_width - (safe_width % 2)
        state['transport_atlas_height'] = min(16384, max(1, atlas_height))
        state['transport_atlas_port'] = min(65535, max(1, atlas_port))
        state['transport_fps'] = min(240, max(1, transport_fps))
    return state

def _role_policy(state):
    role = state['role']
    if role == 'render':
        role = 'world'
        state['role'] = role
    topology = state['topology']
    invalid_transport = bool(state.get('transport_error'))
    ai_on = (not invalid_transport and
             (role in ('standalone', 'ai') or
              (role == 'world' and topology == 'single')))
    world_on = not invalid_transport and role in ('standalone', 'world')
    split_role = (not invalid_transport and
                  topology in ('dual_local', 'dual_network') and
                  role in ('ai', 'world'))
    sender_on = split_role and role == 'ai'
    receiver_on = split_role and role == 'world'

    touch_types = ('touch_tcp', 'touch', 'touch_in_out', 'tcp')
    if topology == 'dual_network':
        bridge_transport = 'tcp'
    elif topology == 'dual_local' and state['transport_type'] in touch_types:
        bridge_transport = 'tcp'
    else:
        bridge_transport = 'shared'
    if sender_on:
        bridge_mode = 'send_' + bridge_transport
        route_index = 0
    elif receiver_on:
        bridge_mode = 'receive_' + bridge_transport
        route_index = 1
    else:
        bridge_mode = 'local'
        route_index = 0
    install_on = world_on and state['experience'] in ('installation', 'combined')
    vr_on = world_on and state['experience'] in ('vr', 'combined')
    state.update({'ai_active':ai_on, 'world_active':world_on,
                  'installation_active':install_on, 'vr_active':vr_on,
                  'transport_sender_active':sender_on,
                  'transport_receiver_active':receiver_on,
                  'transport_active':bridge_transport if split_role else 'local',
                  'bridge_mode':bridge_mode, 'bridge_route_index':route_index,
                  'atlas_route_index':1 if bridge_transport == 'tcp' else 0})
    return state

def _display(value):
    if isinstance(value, (dict, list, tuple)):
        try:
            return json.dumps(value, sort_keys=True, separators=(',', ':'))
        except Exception:
            pass
    return value

def _write_state(root_comp, state):
    table = root_comp.op('CONFIG/runtime_state')
    if table is None:
        return
    try:
        table.clear()
        table.appendRow(['key', 'value'])
        for key in sorted(state):
            table.appendRow([key, _display(state[key])])
    except Exception:
        pass

def _allow(comp, enabled):
    if comp is None:
        return
    try:
        comp.allowCooking = bool(enabled)
    except Exception:
        pass

def _set_shader_constant(root_comp, path, variable, marker, value):
    """Update one marked GLSL constant without relying on version-specific uniforms."""
    dat = root_comp.op(path)
    if dat is None:
        return False
    try:
        original = str(dat.text)
        lines = original.splitlines()
        replacement = 'const float %s = %.9g; // %s' % (
            variable, float(value), marker)
        changed = False
        for index, line in enumerate(lines):
            if marker in line:
                indent = line[:len(line) - len(line.lstrip())]
                updated = indent + replacement
                changed = updated != line
                lines[index] = updated
                break
        else:
            return False
        if changed:
            dat.text = '\n'.join(lines) + ('\n' if original.endswith('\n') else '')
        return True
    except Exception as exc:
        print('[FlexGPU] shader control warning: %s' % exc)
        return False

def _apply_working_pipeline(root_comp, state):
    pipeline = root_comp.op('WORKING_PIPELINE')
    if pipeline is None:
        return

    sources = root_comp.op('WORKING_PIPELINE/SOURCES')
    # An absent source section means "respect the saved/manual adapter state".
    # This is important when the artist drops StreamDiffusionTD.tox into the
    # adapter and operates the project directly instead of through a preset.
    if 'source' in state:
        source = _mapping(state.get('source'))
        requested_source = str(source.get('mode', 'demo')).lower()
        active_source = (requested_source if requested_source in
                         ('demo', 'streamdiffusion') else 'demo')
        use_stream = active_source == 'streamdiffusion'
        _set(sources, 'UseStreamDiffusion', use_stream)
        if 'depth_operator' in source:
            _set(sources, 'UseExternalDepth',
                 use_stream and bool(source.get('depth_operator')))
        _set(root_comp.op('WORKING_PIPELINE/SOURCES/STREAMDIFFUSION_ADAPTER'),
             'Enabled', use_stream)
        state['source_mode_requested'] = requested_source
        state['source_mode_active'] = active_source
        if active_source != requested_source:
            state['source_fallback'] = 'demo (adapter for %s is not installed)' % requested_source

    for path in ('WORKING_PIPELINE/SOURCES/DEMO_RGB_GENERATOR',
                 'WORKING_PIPELINE/SOURCES/STREAMDIFFUSION_ADAPTER/REPLACE_WITH_STREAMDIFFUSION_RGB'):
        _set_resolution(root_comp.op(path), state['diffusion_resolution'],
                        state['diffusion_resolution'])
    for path in ('WORKING_PIPELINE/SOURCES/DEMO_DEPTH_GENERATOR',
                 'WORKING_PIPELINE/SOURCES/STREAMDIFFUSION_ADAPTER/REPLACE_WITH_DEPTH_ESTIMATE'):
        _set_resolution(root_comp.op(path), state['geometry_resolution'],
                        state['geometry_resolution'])

    reconstruction = root_comp.op('WORKING_PIPELINE/RECONSTRUCTION')
    _set(reconstruction, 'Geometryresolution', state['geometry_resolution'])
    render = root_comp.op('WORKING_PIPELINE/POINT_RENDER')
    _set(render, 'Maxpoints', state['point_budget'])
    _set(render, 'Pointsize', state['point_size_px'])
    completion = root_comp.op('WORKING_PIPELINE/COMPLETION')
    _set(completion, 'Mode', state['completion'])
    render_config = _mapping(state.get('render'))
    if 'fog_density' in render_config:
        _set(completion, 'Fogdensity', state['fog_density'])
        _set_shader_constant(
            root_comp,
            'WORKING_PIPELINE/COMPLETION/fog_completion_PIXEL',
            'fogDensity', 'FLEXGPU_FOG_DENSITY', state['fog_density'])
    if 'procedural_mix' in render_config:
        _set(completion, 'Proceduralmix', state['procedural_mix'])
        _set_shader_constant(
            root_comp,
            'WORKING_PIPELINE/COMPLETION/hybrid_completion_PIXEL',
            'proceduralMix', 'FLEXGPU_PROCEDURAL_MIX', state['procedural_mix'])

    width = state['installation_width']
    height = state['installation_height']
    _set_resolution(root_comp.op('WORKING_PIPELINE/POINT_RENDER/RENDER_CENTER'), width, height)
    _set_resolution(root_comp.op('WORKING_PIPELINE/INSTALLATION_OUTPUT/installation_grade'),
                    width, height)
    stereo_width = state['stereo_width']
    stereo_height = state['stereo_height']
    eye_width = max(64, stereo_width // 2)
    for path in ('WORKING_PIPELINE/POINT_RENDER/RENDER_LEFT_EYE',
                 'WORKING_PIPELINE/POINT_RENDER/RENDER_RIGHT_EYE'):
        _set_resolution(root_comp.op(path), eye_width, stereo_height)
    _set_resolution(root_comp.op('WORKING_PIPELINE/STEREO_PREVIEW/STEREO_SIDE_BY_SIDE'),
                    stereo_width, stereo_height)
    _set_resolution(root_comp.op('WORKING_PIPELINE/STEREO_PREVIEW/STEREO_SIDE_BY_SIDE_FALLBACK'),
                    stereo_width, stereo_height)

    if 'sensor' in state:
        sensor = _mapping(state.get('sensor'))
        sensor_mode = str(sensor.get('mode', 'simulated')).lower()
        active_sensor = (sensor_mode if sensor_mode in ('simulated', 'replay')
                         else 'simulated')
        _set(root_comp.op('WORKING_PIPELINE/SENSOR_INTERACTION'), 'Mode', active_sensor)
        mask = root_comp.op('WORKING_PIPELINE/SENSOR_INTERACTION/SIMULATED_SENSOR_MASK')
        _set(mask, 'radius', 0.0 if sensor_mode == 'disabled' else 0.16)
        state['sensor_mode_active'] = ('disabled' if sensor_mode == 'disabled'
                                       else active_sensor)
        if sensor_mode not in ('simulated', 'replay', 'disabled'):
            state['sensor_fallback'] = 'simulated (depth-sensor adapter is not installed)'

    bridge = root_comp.op('WORKING_PIPELINE/ROLE_BRIDGE')
    _set(bridge, 'Mode', state['bridge_mode'])
    _set(bridge, 'Senderactive', state['transport_sender_active'])
    _set(bridge, 'Receiveractive', state['transport_receiver_active'])
    _set(bridge, 'Segmentname', state['transport_segment'])
    _set(bridge, 'Peeraddress', state['transport_peer_host'])
    _set(bridge, 'Atlaswidth', state['transport_atlas_width'])
    _set(bridge, 'Atlasheight', state['transport_atlas_height'])
    _set(bridge, 'Atlasport', state['transport_atlas_port'])
    _set(bridge, 'Sendfps', state['transport_fps'])
    try:
        timeline_fps = max(1.0, float(project.cookRate))
    except Exception:
        try:
            timeline_fps = max(1.0, float(root_comp.time.rate))
        except Exception:
            timeline_fps = 60.0
    send_step = max(1, int(round(timeline_fps / float(state['transport_fps']))))
    state['transport_send_step'] = send_step
    state['transport_effective_fps'] = timeline_fps / float(send_step)
    _set(bridge, 'Sendstep', send_step)
    _set(root_comp.op('WORKING_PIPELINE/ROLE_BRIDGE/RGB_ROUTE'),
         'index', state['bridge_route_index'])
    _set(root_comp.op('WORKING_PIPELINE/ROLE_BRIDGE/DEPTH_ROUTE'),
         'index', state['bridge_route_index'])
    _set(root_comp.op('WORKING_PIPELINE/ROLE_BRIDGE/ATLAS_ROUTE'),
         'index', state['atlas_route_index'])

    _allow(bridge, True)
    # allowCooking is writable only on COMPs in TouchDesigner 2025. Sender and
    # Touch endpoint TOPs use Active expressions; Shared Mem In cooks on demand
    # only when ATLAS_ROUTE selects it. The shared sender is additionally
    # force-cooked only in send_shared.
    if state['bridge_mode'] == 'send_shared':
        endpoint_name = 'TX_SHARED_ATLAS'
    elif state['bridge_mode'] == 'receive_shared':
        endpoint_name = 'RX_SHARED_ATLAS'
    elif state['bridge_mode'] == 'send_tcp':
        endpoint_name = 'TX_TCP_ATLAS'
    elif state['bridge_mode'] == 'receive_tcp':
        endpoint_name = 'RX_TCP_ATLAS'
    else:
        endpoint_name = ''
    state['transport_endpoint_active'] = endpoint_name

    _allow(sources, state['ai_active'])
    for path in ('RECONSTRUCTION', 'SENSOR_INTERACTION', 'TEMPORAL_WORLD',
                 'COMPLETION', 'RENDER_CONTRACT', 'POINT_RENDER'):
        _allow(root_comp.op('WORKING_PIPELINE/' + path), state['world_active'])
    _allow(root_comp.op('WORKING_PIPELINE/POINT_RENDER/RENDER_CENTER'),
           state['installation_active'])
    for path in ('RENDER_LEFT_EYE', 'RENDER_RIGHT_EYE'):
        _allow(root_comp.op('WORKING_PIPELINE/POINT_RENDER/' + path),
               state['vr_active'])

    telemetry_enabled = bool(_mapping(state.get('telemetry')).get('enabled', False))
    _allow(root_comp.op('WORKING_PIPELINE/TELEMETRY'), telemetry_enabled)
    _allow(root_comp.op('WORKING_PIPELINE/INSTALLATION_OUTPUT'),
           state.get('installation_active', True))
    _allow(root_comp.op('WORKING_PIPELINE/STEREO_PREVIEW'), state.get('vr_active', False))

def _interpolate_quality(low, high, level, levels, key):
    if levels <= 1:
        return int(high)
    ratio = float(level) / float(levels - 1)
    value = float(low) + (float(high) - float(low)) * ratio
    if key in ('diffusion_resolution', 'geometry_resolution'):
        return max(64, int(round(value / 64.0)) * 64)
    if key == 'point_budget':
        return max(1000, int(round(value / 1000.0)) * 1000)
    return int(round(value))

def _configure_adaptive(state):
    config = _mapping(state.get('adaptive'))
    enabled = bool(config.get('enabled', False))
    levels = max(2, _integer(config.get('levels', 5), 5))
    level = _integer(config.get('initial_level', levels - 1), levels - 1)
    level = max(0, min(levels - 1, level))
    tier = state['tier'] if state['tier'] in QUALITY_MINIMUMS else 'custom'
    maximum = dict((key, int(state[key])) for key in QUALITY_KEYS)
    minimum = dict(QUALITY_MINIMUMS[tier])
    for key in QUALITY_KEYS:
        minimum[key] = min(minimum[key], maximum[key])
    runtime = {
        'state': state, 'adaptive': config, 'adaptive_enabled': enabled,
        'levels': levels, 'level': level, 'minimum': minimum, 'maximum': maximum,
        'overload_streak': 0, 'healthy_streak': 0, 'cooldown': 0,
        'last_tick': None, 'frame': 0, 'telemetry_buffer': [],
        'transport_frame': 0,
        'telemetry_count': 0, 'telemetry_frame_sum': 0.0,
        'telemetry_frame_max': 0.0,
    }
    if enabled:
        _apply_level(runtime)
    state['adaptive_enabled'] = enabled
    state['adaptive_level'] = level
    state['adaptive_levels'] = levels
    return runtime

def _apply_level(runtime):
    state = runtime['state']
    level = runtime['level']
    levels = runtime['levels']
    for key in QUALITY_KEYS:
        state[key] = _interpolate_quality(runtime['minimum'][key],
                                          runtime['maximum'][key],
                                          level, levels, key)
    state['adaptive_level'] = level

def apply(root_comp=None, overrides=None):
    root_comp = root_comp or op('/project1/flexgpu')
    if root_comp is None:
        return {}
    dashboard = root_comp.op('OPERATOR_DASHBOARD')
    state = environment()
    if overrides:
        state.update(overrides)
    _materialize(state)
    _role_policy(state)
    ai_on = state['ai_active']
    world_on = state['world_active']
    install_on = state['installation_active']
    vr_on = state['vr_active']

    runtime = _configure_adaptive(state)
    try:
        root_comp.store('_flexgpu_runtime', runtime)
    except Exception:
        pass

    if dashboard is not None:
        _set(dashboard, 'Role', state['role'])
        _set(dashboard, 'Topology', state['topology'])
        _set(dashboard, 'Experience', state['experience'])
        _set(dashboard, 'Completion', state['completion'])
        _set(dashboard, 'Tier', state['tier'])

    ai = root_comp.op('AI_PIPELINE')
    world = root_comp.op('WORLD_CORE')
    install = root_comp.op('INSTALLATION_OUT')
    vr = root_comp.op('VR_OUT')
    for comp, name, key in (
        (ai, 'Diffusionresolution', 'diffusion_resolution'),
        (ai, 'Diffusionfps', 'diffusion_fps'),
        (ai, 'Geometryresolution', 'geometry_resolution'),
        (ai, 'Geometryfps', 'geometry_fps'),
        (world, 'Pointbudget', 'point_budget'),
        (install, 'Targetfps', 'installation_fps'),
        (vr, 'Targetfps', 'vr_fps'),
    ):
        _set(comp, name, state[key])

    completion_index = {'fog':0, 'procedural':1, 'hybrid':2}.get(state['completion'], 2)
    _set(root_comp.op('COMPLETION/switch_completion'), 'index', completion_index)
    _set(ai, 'Enabled', ai_on)
    _set(world, 'Enabled', world_on)
    _set(install, 'Enabled', install_on)
    _set(vr, 'Enabled', vr_on)
    _apply_working_pipeline(root_comp, state)
    _transport_tick(root_comp, runtime, True)
    _write_state(root_comp, state)
    if dashboard is not None:
        if state.get('transport_error'):
            status = 'ERROR / transport / %s' % state['transport_error']
        else:
            status = '%s / %s / %s / %s / %s / Q%s' % (
                state['role'], state['topology'], state['experience'],
                state['completion'], state['tier'], state['adaptive_level'])
        _set(dashboard, 'Status', status)
    try:
        root_comp.store('runtime_state', dict(state))
    except Exception:
        pass
    if state.get('transport_error'):
        print('[FlexGPU] runtime transport ERROR (all heavy stages disabled): %s' %
              state['transport_error'])
    print('[FlexGPU] runtime: %s' % state)
    return state

def _runtime(root_comp):
    try:
        value = root_comp.fetch('_flexgpu_runtime', None)
        return value if isinstance(value, dict) else None
    except Exception:
        return None

def _telemetry_path(runtime, key):
    value = _mapping(runtime['state'].get('telemetry')).get(key, '')
    if not value:
        return ''
    value = os.path.expandvars(os.path.expanduser(str(value)))
    if not os.path.isabs(value):
        config_path = runtime['state'].get('config_path', '')
        base = os.path.dirname(config_path) if config_path else os.getcwd()
        value = os.path.join(base, value)
    return os.path.normpath(value)

def flush_telemetry(root_comp=None, final=False):
    root_comp = root_comp or op('/project1/flexgpu')
    runtime = _runtime(root_comp) if root_comp is not None else None
    if not runtime:
        return 0
    records = runtime.get('telemetry_buffer', [])
    path = _telemetry_path(runtime, 'jsonl_path')
    if records and path:
        try:
            directory = os.path.dirname(path)
            if directory and not os.path.isdir(directory):
                os.makedirs(directory)
            with open(path, 'a') as handle:
                for record in records:
                    handle.write(json.dumps(record, sort_keys=True, separators=(',', ':')) + '\n')
            del records[:]
        except Exception as exc:
            print('[FlexGPU] telemetry warning: %s' % exc)
    if final:
        summary_path = _telemetry_path(runtime, 'summary_path')
        count = runtime.get('telemetry_count', 0)
        if summary_path and count:
            summary = {'samples':count,
                       'mean_frame_time_ms':runtime['telemetry_frame_sum'] / float(count),
                       'max_frame_time_ms':runtime['telemetry_frame_max'],
                       'tier':runtime['state']['tier'],
                       'final_level':runtime['level']}
            try:
                directory = os.path.dirname(summary_path)
                if directory and not os.path.isdir(directory):
                    os.makedirs(directory)
                temporary = summary_path + '.tmp'
                with open(temporary, 'w') as handle:
                    json.dump(summary, handle, indent=2, sort_keys=True)
                    handle.write('\n')
                os.replace(temporary, summary_path)
            except Exception as exc:
                print('[FlexGPU] telemetry summary warning: %s' % exc)
    return len(records)

def _operator_cook_ms(root_comp):
    total = 0.0
    for path in ('WORKING_PIPELINE/SOURCES', 'WORKING_PIPELINE/RECONSTRUCTION',
                 'WORKING_PIPELINE/TEMPORAL_WORLD', 'WORKING_PIPELINE/COMPLETION',
                 'WORKING_PIPELINE/POINT_RENDER'):
        node = root_comp.op(path)
        try:
            total += max(0.0, float(node.cookTime))
        except Exception:
            pass
    return total

def _transport_tick(root_comp, runtime, force=False):
    state = runtime['state']
    frame = runtime.get('transport_frame', 0)
    runtime['transport_frame'] = frame + 1
    if state.get('bridge_mode') != 'send_shared':
        return False
    step = max(1, _integer(state.get('transport_send_step', 1), 1))
    if not force and frame % step != 0:
        return False
    cooked = False
    for name in ('TX_SHARED_ATLAS',):
        node = root_comp.op('WORKING_PIPELINE/ROLE_BRIDGE/' + name)
        if node is None:
            continue
        try:
            _set(node, 'active', True)
            node.cook(force=True)
            cooked = True
        except TypeError:
            try:
                _set(node, 'active', True)
                node.cook()
                cooked = True
            except Exception:
                pass
        except Exception as exc:
            print('[FlexGPU] shared-memory sender warning: %s' % exc)
        finally:
            _set(node, 'active', False)
    if cooked:
        state['transport_last_send_frame'] = frame
    return cooked

def tick(root_comp=None):
    root_comp = root_comp or op('/project1/flexgpu')
    if root_comp is None:
        return None
    runtime = _runtime(root_comp)
    if runtime is None:
        apply(root_comp)
        runtime = _runtime(root_comp)
    if runtime is None:
        return None
    _transport_tick(root_comp, runtime)
    now = time.perf_counter()
    previous = runtime.get('last_tick')
    runtime['last_tick'] = now
    if previous is None:
        return None
    frame_ms = max(0.0, (now - previous) * 1000.0)
    runtime['frame'] += 1
    changed = False

    if runtime['adaptive_enabled']:
        config = runtime['adaptive']
        thresholds = _mapping(config.get('thresholds'))
        budget = _number(config.get('frame_budget_ms', 1000.0 / 60.0), 1000.0 / 60.0)
        high = _number(thresholds.get('frame_high', 1.08), 1.08)
        low = _number(thresholds.get('frame_low', 0.82), 0.82)
        critical = _number(thresholds.get('critical_frame', 2.0), 2.0)
        down_window = max(1, _integer(config.get('down_window', 3), 3))
        up_window = max(1, _integer(config.get('up_window', 120), 120))
        cooldown_samples = max(0, _integer(config.get('cooldown_samples', 30), 30))
        if frame_ms >= budget * high:
            runtime['overload_streak'] += 1
            runtime['healthy_streak'] = 0
        elif frame_ms <= budget * low:
            runtime['healthy_streak'] += 1
            runtime['overload_streak'] = 0
        else:
            runtime['overload_streak'] = 0
            runtime['healthy_streak'] = 0
        critical_now = frame_ms >= budget * critical
        if ((critical_now or (runtime['cooldown'] == 0 and
                              runtime['overload_streak'] >= down_window)) and
                runtime['level'] > 0):
            runtime['level'] -= 1
            changed = True
        elif (runtime['cooldown'] == 0 and
              runtime['healthy_streak'] >= up_window and
              runtime['level'] < runtime['levels'] - 1):
            runtime['level'] += 1
            changed = True
        if changed:
            runtime['overload_streak'] = 0
            runtime['healthy_streak'] = 0
            runtime['cooldown'] = cooldown_samples
            _apply_level(runtime)
            _apply_working_pipeline(root_comp, runtime['state'])
            _write_state(root_comp, runtime['state'])
            try:
                root_comp.store('runtime_state', dict(runtime['state']))
            except Exception:
                pass
        elif runtime['cooldown'] > 0:
            runtime['cooldown'] -= 1

    telemetry = _mapping(runtime['state'].get('telemetry'))
    if bool(telemetry.get('enabled', False)):
        interval = max(1, _integer(telemetry.get('sample_interval_frames', 1), 1))
        if runtime['frame'] % interval == 0:
            record = {'timestamp':time.time(), 'frame_time_ms':frame_ms,
                      'tier':runtime['state']['tier'], 'role':runtime['state']['role'],
                      'adaptive_level':runtime['level'],
                      'settings':dict((key, runtime['state'][key]) for key in QUALITY_KEYS)}
            if bool(telemetry.get('include_operator_metrics', True)):
                record['operator_cook_time_ms'] = _operator_cook_ms(root_comp)
            runtime['telemetry_buffer'].append(record)
            runtime['telemetry_count'] += 1
            runtime['telemetry_frame_sum'] += frame_ms
            runtime['telemetry_frame_max'] = max(runtime['telemetry_frame_max'], frame_ms)
            flush_every = max(1, _integer(telemetry.get('flush_every', 60), 60))
            if len(runtime['telemetry_buffer']) >= flush_every:
                flush_telemetry(root_comp)
    return {'frame_time_ms':frame_ms, 'changed':changed,
            'level':runtime['level']}

def safe_reset(root_comp=None):
    root_comp = root_comp or op('/project1/flexgpu')
    return apply(root_comp, {'role':'world', 'experience':'installation',
                             'completion':'fog',
                             'adaptive':{'enabled':False}})

class FlexGpuRuntimeExt(object):
    def __init__(self, ownerComp):
        self.ownerComp = ownerComp
    def Apply(self):
        return apply(self.ownerComp)
    def Tick(self):
        return tick(self.ownerComp)
    def FlushTelemetry(self):
        return flush_telemetry(self.ownerComp, True)
    def SafeReset(self):
        return safe_reset(self.ownerComp)
    @property
    def State(self):
        try:
            return self.ownerComp.fetch('runtime_state', {})
        except Exception:
            return {}
'''


STARTUP_CALLBACKS = r'''# Execute DAT callbacks
def _apply():
    root_comp = me.parent().parent()
    module_dat = root_comp.op('STARTUP/runtime_helpers')
    if module_dat is not None:
        return module_dat.module.apply(root_comp)
    return {}

def onStart():
    _apply()
    return

def onCreate():
    _apply()
    return

def onFrameStart(frame):
    root_comp = me.parent().parent()
    module_dat = root_comp.op('STARTUP/runtime_helpers')
    if module_dat is not None:
        module_dat.module.tick(root_comp)
    return

def onExit():
    root_comp = me.parent().parent()
    module_dat = root_comp.op('STARTUP/runtime_helpers')
    if module_dat is not None:
        module_dat.module.flush_telemetry(root_comp, True)
    return
'''


class BuildReport(object):
    def __init__(self):
        self.created = []
        self.reused = []
        self.warnings = []
        self.output_path = None

    def warn(self, message):
        self.warnings.append(str(message))
        print("[FlexGPU bootstrap] WARNING: %s" % message)

    def as_dict(self):
        return {"build_version": BUILD_VERSION, "created": self.created,
                "reused": self.reused, "warnings": self.warnings,
                "output_path": self.output_path}


def _symbol(name):
    value = globals().get(name)
    if value is not None:
        return value
    try:
        import builtins
        value = getattr(builtins, name, None)
        if value is not None:
            return value
    except Exception:
        pass
    try:
        import td
        return getattr(td, name, None)
    except Exception:
        return None


def _op(path):
    fn = _symbol("op")
    if fn is None:
        raise RuntimeError("TouchDesigner op() is unavailable; run inside TouchDesigner 2025.")
    return fn(path)


def _child(parent, name):
    try:
        return parent.op(name)
    except Exception:
        return None


def _ensure(parent, type_name, name, report, optional=False):
    found = _child(parent, name)
    if found is not None:
        report.reused.append(found.path)
        return found
    errors = []
    for type_value in (type_name, _symbol(type_name)):
        if type_value is None:
            continue
        try:
            node = parent.create(type_value, name)
            report.created.append(node.path)
            return node
        except Exception as exc:
            errors.append(str(exc))
    message = "%s %s unavailable under %s" % (type_name, name, parent.path)
    if errors:
        message += " (%s)" % errors[-1]
    if optional:
        report.warn(message)
        return None
    raise RuntimeError(message)


def _style(node, x, y, color=None, comment=None, width=None, height=None):
    for attr, value in (("nodeX", x), ("nodeY", y), ("comment", comment),
                        ("nodeWidth", width), ("nodeHeight", height)):
        if value is not None:
            try:
                setattr(node, attr, value)
            except Exception:
                pass
    if color is not None:
        try:
            node.color = color
        except Exception:
            pass


def _par(node, *names):
    for name in names:
        try:
            value = getattr(node.par, name)
            if value is not None:
                return value
        except Exception:
            pass
    wanted = {str(name).lower() for name in names}
    try:
        for parameter in node.pars():
            if str(parameter.name).lower() in wanted:
                return parameter
    except Exception:
        pass
    return None


def _set_par(node, names, value):
    if node is None:
        return False
    if isinstance(names, str):
        names = (names,)
    p = _par(node, *names)
    if p is None:
        return False
    try:
        p.val = value
        return True
    except Exception:
        try:
            p = value
            return True
        except Exception:
            return False


def _connect(src, dst, dst_index=0, src_index=0, report=None):
    if src is None or dst is None:
        return False
    # Preserve hand-made wiring when the bootstrap is rerun. Fresh managed
    # inputs are empty, so they still receive the starter connection.
    try:
        inputs = dst.inputs
        if len(inputs) > dst_index and inputs[dst_index] is not None:
            return True
    except Exception:
        pass
    try:
        dst.setInput(dst_index, src, src_index)
        return True
    except Exception:
        pass
    try:
        dst.inputConnectors[dst_index].connect(src.outputConnectors[src_index])
        return True
    except Exception as exc:
        if report is not None:
            report.warn("Could not connect %s[%s] -> %s[%s]: %s" %
                        (src.path, src_index, dst.path, dst_index, exc))
        return False


def _text(parent, name, body, report):
    dat = _ensure(parent, "textDAT", name, report)
    try:
        dat.text = body
    except Exception as exc:
        report.warn("Could not update %s: %s" % (dat.path, exc))
    return dat


def _table(parent, name, rows, report):
    dat = _ensure(parent, "tableDAT", name, report)
    try:
        dat.clear()
        for row in rows:
            dat.appendRow([str(value) for value in row])
    except Exception as exc:
        report.warn("Could not update %s: %s" % (dat.path, exc))
    return dat


def _page(comp, name):
    try:
        for page in comp.customPages:
            if page.name == name:
                return page
    except Exception:
        pass
    try:
        return comp.appendCustomPage(name)
    except Exception:
        return None


def _custom(comp, page, kind, name, default, menu=None):
    existing = _par(comp, name)
    if existing is not None:
        if menu and kind == "Menu":
            try:
                existing.menuNames = list(menu)
                existing.menuLabels = [str(x).replace("_", " ").title() for x in menu]
            except Exception:
                pass
        return existing
    if page is None:
        return None
    method = getattr(page, "append%s" % kind, None)
    if method is None:
        return None
    try:
        canonical_name = str(name)[:1].upper() + str(name)[1:].lower()
        result = method(canonical_name, label=name)
        p = result[0] if isinstance(result, (list, tuple)) else result
        if menu and kind == "Menu":
            p.menuNames = list(menu)
            p.menuLabels = [str(x).replace("_", " ").title() for x in menu]
        p.default = default
        p.val = default
        return p
    except Exception:
        return None


def _flatten(data, prefix=""):
    rows = []
    if isinstance(data, dict):
        for key in sorted(data):
            path = "%s.%s" % (prefix, key) if prefix else str(key)
            value = data[key]
            if isinstance(value, dict):
                rows.extend(_flatten(value, path))
            else:
                rows.append((path, value))
    return rows


def _lookup(data, key, default):
    if not isinstance(data, dict):
        return default
    if key in data:
        return data[key]
    for section in ("flexgpu", "runtime", "show", "profile", "3080ti_16gb"):
        nested = data.get(section)
        if isinstance(nested, dict) and key in nested:
            return nested[key]
    return default


def _load_config(config_path, report):
    if not config_path:
        return {}
    path = os.path.abspath(os.path.expandvars(os.path.expanduser(str(config_path))))
    try:
        with open(path, "r") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise ValueError("top level must be a JSON object")
        return data
    except Exception as exc:
        report.warn("Config %s was not loaded: %s; using safe defaults" % (path, exc))
        return {}


def _add_enabled(comp, default=True):
    page = _page(comp, "FlexGPU")
    _custom(comp, page, "Toggle", "Enabled", bool(default))
    _custom(comp, page, "Str", "Contract", "placeholder")


def _make_branch(parent, name, note, color, report):
    branch = _ensure(parent, "baseCOMP", name, report)
    _style(branch, 0, 0, color=color, comment=note, width=170, height=90)
    _text(branch, "README", note, report)
    source = _ensure(branch, "constantTOP", "preview", report, optional=True)
    out = _ensure(branch, "outTOP", "out1", report, optional=True)
    if source is not None:
        _set_par(source, ("resolutionw", "resw"), 64)
        _set_par(source, ("resolutionh", "resh"), 64)
    _connect(source, out, report=report)
    return branch


def _build_shell(flexgpu, config, config_path, report):
    colors = {
        "config": (0.25, 0.35, 0.45), "ai": (0.48, 0.24, 0.55),
        "world": (0.18, 0.45, 0.38), "bus": (0.18, 0.38, 0.56),
        "completion": (0.55, 0.38, 0.18), "output": (0.42, 0.48, 0.18),
        "ui": (0.45, 0.24, 0.22), "startup": (0.30, 0.30, 0.30),
    }
    defaults = dict(DEFAULTS)
    for key in defaults:
        defaults[key] = _lookup(config, key, defaults[key])

    config_comp = _ensure(flexgpu, "baseCOMP", "CONFIG", report)
    _style(config_comp, -1150, 420, colors["config"], "JSON profile and runtime state")
    rows = [["key", "value", "source"]]
    rows += [[key, defaults[key], "profile/default"] for key in sorted(defaults)]
    _table(config_comp, "settings", rows, report)
    _table(config_comp, "runtime_state", [["key", "value"], ["status", "not started"]], report)
    _table(config_comp, "profile_flat", [["json_path", "value"]] +
           [[key, value] for key, value in _flatten(config)], report)
    _text(config_comp, "README", "Safe defaults plus optional JSON profile. Environment variables win at startup.", report)

    ai = _ensure(flexgpu, "baseCOMP", "AI_PIPELINE", report)
    _style(ai, -1120, 160, colors["ai"], "AI owner: standalone/ai, or world+single topology", 230, 110)
    _add_enabled(ai, defaults["ai_enabled"])
    ai_quality = _page(ai, "Quality")
    _custom(ai, ai_quality, "Int", "Diffusionresolution", int(defaults["diffusion_resolution"]))
    _custom(ai, ai_quality, "Int", "Diffusionfps", int(defaults["diffusion_fps"]))
    _custom(ai, ai_quality, "Int", "Geometryresolution", int(defaults["geometry_resolution"]))
    _custom(ai, ai_quality, "Int", "Geometryfps", int(defaults["geometry_fps"]))
    _text(ai, "README", "Adapter contract for a split AI process. The built-in WORKING_PIPELINE demo is immediately usable; later replace only its SOURCES/STREAMDIFFUSION_ADAPTER inputs.", report)
    _table(ai, "output_contract", [["output", "family", "format"],
        ["generated_rgb", "TOP", "RGBA16F or RGBA8"],
        ["generated_depth", "TOP", "normalized 0..1, mono16f/mono32f"],
        ["split_transport", "ROLE_BRIDGE", "shared memory or Touch TCP"]], report)

    world = _ensure(flexgpu, "baseCOMP", "WORLD_CORE", report)
    _style(world, -780, 160, colors["world"], "World owner: standalone/world; sensor, interaction and particles", 230, 110)
    _add_enabled(world, True)
    _custom(world, _page(world, "Quality"), "Int", "Pointbudget", int(defaults["point_budget"]))
    _text(world, "README", "Stable world-role boundary. WORKING_PIPELINE supplies simulated sensor interaction and GPU persistence now; connect the production depth-sensor adapter here later.", report)

    bus_in = _ensure(flexgpu, "baseCOMP", "WORLD_BUS_IN", report)
    _style(bus_in, -440, 210, colors["bus"], "Adapters into stable WorldBus texture contracts", 210, 105)
    bus_rows = [["index", "name", "family", "contract", "producer"]]
    channels = [
        (0, "generated_rgb", "TOP", "RGBA color", "AI role/local"),
        (1, "generated_position", "TOP", "XYZ + valid alpha", "AI role/local"),
        (2, "sensor_position", "TOP", "metric XYZ + valid alpha", "world sensor"),
        (3, "interaction_field", "TOP", "force/occupancy field", "world sensor"),
    ]
    bus_rows += [list(row) for row in channels]
    _table(bus_in, "worldbus_schema", bus_rows, report)
    for index, name, _, contract, _producer in channels:
        source = _ensure(bus_in, "constantTOP", name, report, optional=True)
        out = _ensure(bus_in, "outTOP", "out_%s" % name, report, optional=True)
        _set_par(out, ("outputindex", "index"), index)
        _connect(source, out, report=report)
        if source is not None:
            try:
                source.comment = "PLACEHOLDER: %s" % contract
            except Exception:
                pass

    completion = _ensure(flexgpu, "baseCOMP", "COMPLETION", report)
    _style(completion, -100, 210, colors["completion"], "View completion selector: fog / procedural / hybrid", 230, 110)
    for index, in_name in enumerate(("position_in", "color_in")):
        inp = _ensure(completion, "inTOP", in_name, report, optional=True)
        _set_par(inp, ("inputindex", "index"), index)
    fog = _make_branch(completion, "FOG", "Fog/point-thickness completion placeholder", (0.28, 0.34, 0.40), report)
    procedural = _make_branch(completion, "PROCEDURAL", "Procedural backfill completion placeholder", (0.42, 0.30, 0.16), report)
    hybrid = _make_branch(completion, "HYBRID", "Hybrid fog + procedural completion placeholder", (0.42, 0.38, 0.18), report)
    _style(fog, -330, 60); _style(procedural, -90, 60); _style(hybrid, 160, 60)
    switch = _ensure(completion, "switchTOP", "switch_completion", report, optional=True)
    _style(switch, 50, -110, comment="Runtime selector: 0 fog, 1 procedural, 2 hybrid") if switch else None
    for index, branch in enumerate((fog, procedural, hybrid)):
        _connect(branch, switch, index, 0, report)
    _set_par(switch, "index", {"fog":0, "procedural":1, "hybrid":2}.get(str(defaults["completion"]).lower(), 2))
    completion_out = _ensure(completion, "outTOP", "completed_world", report, optional=True)
    _connect(switch or hybrid, completion_out, report=report)

    bus_out = _ensure(flexgpu, "baseCOMP", "WORLD_BUS_OUT", report)
    _style(bus_out, 230, 210, colors["bus"], "Published world state for both render modules", 210, 105)
    out_channels = [(0, "completed_world"), (1, "world_color"), (2, "sensor_position"), (3, "interaction_field")]
    for index, name in out_channels:
        inp = _ensure(bus_out, "inTOP", "in_%s" % name, report, optional=True)
        out = _ensure(bus_out, "outTOP", "out_%s" % name, report, optional=True)
        _set_par(inp, ("inputindex", "index"), index)
        _set_par(out, ("outputindex", "index"), index)
        _connect(inp, out, report=report)
    _table(bus_out, "publish_contract", [["index", "name"]] + [list(x) for x in out_channels], report)

    install = _ensure(flexgpu, "baseCOMP", "INSTALLATION_OUT", report)
    _style(install, 570, 350, colors["output"], "Projection/LED render shell - 60 Hz target", 250, 115)
    _add_enabled(install, True)
    _custom(install, _page(install, "FlexGPU"), "Int", "Targetfps", int(defaults["installation_fps"]))
    _text(install, "README", "Replace preview with projector/LED camera, render, mapping and Window COMP.\nConsumes the shared WorldBus; it does not own a second simulation.", report)
    iin = _ensure(install, "inTOP", "world_in", report, optional=True)
    ipreview = _ensure(install, "nullTOP", "OUT_INSTALLATION_PREVIEW", report, optional=True)
    _connect(iin, ipreview, report=report)

    vr = _ensure(flexgpu, "baseCOMP", "VR_OUT", report)
    _style(vr, 570, 100, colors["output"], "PCVR stereo render shell - 72 Hz safe starting target", 250, 115)
    _add_enabled(vr, False)
    _custom(vr, _page(vr, "FlexGPU"), "Int", "Targetfps", int(defaults["vr_fps"]))
    _custom(vr, _page(vr, "FlexGPU"), "Float", "Eyescale", 0.75)
    _text(vr, "README", "OpenVR TOP is intentionally NOT created: merely adding it can change project timing.\nAdd stereo cameras and OpenVR only after profiling the shared world render.", report)
    vin = _ensure(vr, "inTOP", "world_in", report, optional=True)
    left = _ensure(vr, "nullTOP", "LEFT_EYE_PLACEHOLDER", report, optional=True)
    right = _ensure(vr, "nullTOP", "RIGHT_EYE_PLACEHOLDER", report, optional=True)
    _connect(vin, left, report=report); _connect(vin, right, report=report)

    dashboard = _ensure(flexgpu, "containerCOMP", "OPERATOR_DASHBOARD", report)
    _style(dashboard, -350, -180, colors["ui"], "Operator controls and health/status shell", 310, 145)
    control = _page(dashboard, "Control")
    _custom(dashboard, control, "Menu", "Role", str(defaults["role"]), ("standalone", "world", "ai"))
    _custom(dashboard, control, "Menu", "Topology", str(defaults["topology"]), ("single", "dual_local", "dual_network"))
    _custom(dashboard, control, "Menu", "Experience", str(defaults["experience"]), ("installation", "vr", "combined"))
    _custom(dashboard, control, "Menu", "Completion", str(defaults["completion"]), ("fog", "procedural", "hybrid"))
    _custom(dashboard, control, "Menu", "Tier", str(defaults["tier"]), ("3080ti_16gb", "4090", "5090", "custom"))
    _custom(dashboard, control, "Toggle", "Aienable", bool(defaults["ai_enabled"]))
    _custom(dashboard, control, "Toggle", "Sensorenable", bool(defaults["sensor_enabled"]))
    _custom(dashboard, control, "Toggle", "Freezeworld", False)
    _custom(dashboard, control, "Toggle", "Safemode", bool(defaults["safe_mode"]))
    _custom(dashboard, control, "Pulse", "Apply", False)
    _custom(dashboard, control, "Pulse", "Emergencyreset", False)
    _custom(dashboard, control, "Str", "Status", "not started")
    _table(dashboard, "operator_checklist", [["check", "expected"],
        ["World render", "60 Hz installation or headset refresh"],
        ["AI queue", "depth <= 1; drop stale frames"],
        ["VRAM", "3080 Ti 16GB target <= 12GB combined"],
        ["Sensor", "30 Hz; interpolate forces"],
        ["Fallback", "fog completion remains available"]], report)
    _text(dashboard, "README", "Use STARTUP/runtime_helpers.module.apply(op('/project1/flexgpu')) after changing controls.\nA production dashboard should bind these parameters and display measured timings.", report)

    startup = _ensure(flexgpu, "baseCOMP", "STARTUP", report)
    _style(startup, 60, -180, colors["startup"], "Environment-aware startup extension and callbacks", 280, 130)
    _text(startup, "runtime_helpers", RUNTIME_HELPERS, report)
    execute = _ensure(startup, "executeDAT", "startup_callbacks", report, optional=True)
    if execute is not None:
        try:
            execute.text = STARTUP_CALLBACKS
        except Exception:
            pass
        _set_par(execute, ("start", "onstart"), True)
        _set_par(execute, ("create", "oncreate"), True)
        _set_par(execute, ("framestart", "onframestart"), True)
        _set_par(execute, ("exit", "onexit"), True)
        _set_par(execute, "active", True)
    else:
        _text(startup, "startup_callbacks_SOURCE", STARTUP_CALLBACKS, report)
    _table(startup, "environment_contract", [["variable", "values", "meaning"],
        ["FLEXGPU_ROLE", "standalone|world|ai", "one-process show or split role"],
        ["FLEXGPU_TOPOLOGY", "single|dual_local|dual_network", "single: world role also owns AI"],
        ["FLEXGPU_CONFIG", "JSON path", "runtime profile; explicit env values win"],
        ["FLEXGPU_EXPERIENCE", "installation|vr|combined", "active output module(s)"],
        ["FLEXGPU_COMPLETION", "fog|procedural|hybrid", "view-completion policy"],
        ["FLEXGPU_TIER", "3080ti_16gb|4090|5090|custom", "performance preset label"],
        ["FLEXGPU_TRANSPORT", "local|shared_memory|touch_tcp", "AI RGB/depth bridge"],
        ["FLEXGPU_TRANSPORT_SEGMENT", "shared-memory name", "base name; _atlas appended"],
        ["FLEXGPU_PEER_HOST", "host/IP", "AI host used by world Touch In TOPs"],
        ["FLEXGPU_ATLAS_WIDTH", "pixels", "atomic atlas width"],
        ["FLEXGPU_ATLAS_HEIGHT", "pixels", "atomic atlas height"],
        ["FLEXGPU_ATLAS_PORT", "TCP port", "atomic RGB/depth Touch stream"],
        ["FLEXGPU_TRANSPORT_FPS", "frames/second", "AI-frame bridge cadence"]], report)
    _table(startup, "quality_environment", [["variable", "setting"],
        ["FLEXGPU_DIFFUSION_RESOLUTION", "AI diffusion input size"],
        ["FLEXGPU_DIFFUSION_HZ", "asynchronous diffusion target"],
        ["FLEXGPU_GEOMETRY_RESOLUTION", "geometry/depth input size"],
        ["FLEXGPU_GEOMETRY_HZ", "asynchronous geometry target"],
        ["FLEXGPU_MAX_POINTS", "authoritative point budget"],
        ["FLEXGPU_VR_REFRESH_HZ", "VR timing target"]], report)

    # Root wiring is best-effort and adds connections only; it never deletes user wiring.
    _connect(bus_in, completion, 0, 1, report)
    _connect(bus_in, completion, 1, 0, report)
    _connect(completion, bus_out, 0, 0, report)
    _connect(bus_in, bus_out, 1, 0, report)
    _connect(bus_in, bus_out, 2, 2, report)
    _connect(bus_in, bus_out, 3, 3, report)
    _connect(bus_out, install, 0, 0, report)
    _connect(bus_out, vr, 0, 0, report)

    _text(flexgpu, "README_FIRST", "FLEXGPU REALTIME POINT WORLD\nOpen WORKING_PIPELINE/OUT_INSTALLATION for the built-in point-world demo and OUT_STEREO_PREVIEW for desktop stereo.\nLater replace only WORKING_PIPELINE/SOURCES/STREAMDIFFUSION_ADAPTER with your StreamDiffusionTD.tox outputs.\nWORKING_PIPELINE/ROLE_BRIDGE now packs RGB/depth into one atomic RGBA16F preview atlas for Shared Mem or Touch TCP split roles; unassigned stages are cook-gated. This direct image path is not the full WorldBus v1 metadata/control protocol.", report)
    _table(flexgpu, "bootstrap_manifest", [["field", "value"],
        ["build_version", BUILD_VERSION], ["root", ROOT_PATH],
        ["config_path", config_path or "<defaults>"],
        ["managed_scope", ROOT_PATH + " only"],
        ["role_bridge", ROOT_PATH + "/WORKING_PIPELINE/ROLE_BRIDGE"],
        ["unknown_nodes", "preserved"]], report)
    try:
        flexgpu.store("bootstrap_report", report.as_dict())
    except Exception:
        pass


def _build_working_pipeline(flexgpu, report):
    """Load the sibling runtime builder and merge its report.

    The generated operators are saved into the .toe, so this source file is
    needed only while building/updating the project.  Importing via the normal
    module path keeps the Textport workflow short; the file-location fallback
    also supports callers that loaded this bootstrap directly by filename.
    """
    module = None
    import_error = None
    try:
        import importlib
        module = importlib.import_module("runtime_pipeline")
        module = importlib.reload(module)
    except Exception as exc:
        import_error = exc
    if module is None:
        try:
            import importlib.util
            source_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "runtime_pipeline.py")
            spec = importlib.util.spec_from_file_location("flexgpu_runtime_pipeline",
                                                          source_path)
            if spec is None or spec.loader is None:
                raise RuntimeError("could not create an import spec")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception as exc:
            raise RuntimeError("runtime_pipeline.py could not be loaded: %s / %s" %
                               (import_error, exc))
    pipeline = module.build(flexgpu)
    pipeline_report = getattr(module, "LAST_REPORT", None)
    if pipeline_report is not None:
        report.created.extend(getattr(pipeline_report, "created", ()))
        report.reused.extend(getattr(pipeline_report, "reused", ()))
        report.warnings.extend(getattr(pipeline_report, "warnings", ()))
    try:
        flexgpu.store("working_pipeline_path", pipeline.path)
    except Exception:
        pass
    return pipeline


def _save(output_path, report):
    if not output_path:
        raise ValueError("output_path is required when save=True")
    path = os.path.abspath(os.path.expandvars(os.path.expanduser(str(output_path))))
    if not path.lower().endswith(".toe"):
        path += ".toe"
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    project_obj = _symbol("project")
    try:
        project_obj.save(path)
    except Exception as first:
        app_obj = _symbol("app")
        try:
            app_obj.saveProject(path)
        except Exception as second:
            raise RuntimeError("Could not save .toe via project.save or app.saveProject: %s / %s" % (first, second))
    report.output_path = path
    return path


def build(output_path, config_path=None, save=True):
    """Create/update ``/project1/flexgpu`` and optionally save the whole project.

    The function does not delete any node. Existing managed nodes are reused;
    unknown children inside ``flexgpu`` are preserved. It returns the flexgpu COMP.
    Inspect ``LAST_REPORT`` or ``flexgpu.fetch('bootstrap_report')`` for warnings.
    """
    global LAST_REPORT
    report = BuildReport()
    LAST_REPORT = report
    project1 = _op("/project1")
    if project1 is None:
        raise RuntimeError("/project1 does not exist")
    flexgpu = _child(project1, "flexgpu")
    if flexgpu is None:
        flexgpu = _ensure(project1, "baseCOMP", "flexgpu", report)
    try:
        _style(flexgpu, flexgpu.nodeX, flexgpu.nodeY, (0.22, 0.36, 0.33),
               "FlexGPU modular realtime 2D-to-3D show shell", 260, 150)
    except Exception:
        pass
    config = _load_config(config_path, report)
    _build_shell(flexgpu, config, config_path, report)
    _build_working_pipeline(flexgpu, report)

    # Apply build-time defaults now; the Execute DAT reapplies runtime env on start.
    runtime_dat = _child(_child(flexgpu, "STARTUP"), "runtime_helpers")
    if runtime_dat is not None:
        try:
            role_default = _lookup(config, "role", None)
            if role_default is None:
                node_role = str(config.get("node_role", "")).lower()
                role_default = {"ai":"ai", "render":"world"}.get(
                    node_role, DEFAULTS["role"])
            runtime_overrides = {
                "role": role_default,
                "topology": _lookup(config, "topology", DEFAULTS["topology"]),
                "experience": _lookup(config, "experience", DEFAULTS["experience"]),
                "completion": _lookup(config, "completion", DEFAULTS["completion"]),
                "tier": _lookup(config, "tier", DEFAULTS["tier"]),
                "config_path": config_path or "",
            }
            for section in ("adaptive", "telemetry", "source", "sensor",
                            "render", "transport"):
                value = config.get(section)
                if isinstance(value, dict):
                    runtime_overrides[section] = dict(value)
            for key in ("diffusion_resolution", "diffusion_fps",
                        "geometry_resolution", "geometry_fps",
                        "point_budget", "vr_fps"):
                value = _lookup(config, key, None)
                if value not in (None, ""):
                    runtime_overrides[key] = value
            runtime_dat.module.apply(flexgpu, runtime_overrides)
        except Exception as exc:
            report.warn("Runtime defaults were not applied during build: %s" % exc)
    if save:
        _save(output_path, report)
    try:
        flexgpu.store("bootstrap_report", report.as_dict())
    except Exception:
        pass
    print("[FlexGPU bootstrap] ready: %s (%d created, %d reused, %d warnings)%s" %
          (flexgpu.path, len(report.created), len(report.reused), len(report.warnings),
           " -> " + report.output_path if report.output_path else ""))
    return flexgpu


# Intentionally no automatic __main__ call. Importing or exec()ing this file in
# a live TouchDesigner session is side-effect free until build(...) is invoked.
