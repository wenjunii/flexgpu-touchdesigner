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
import re


BUILD_VERSION = "1.2.0"
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
import math
import os
import re
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

def _value(comp, name, fallback=None):
    parameter = _par(comp, name)
    if parameter is None:
        return fallback
    try:
        return parameter.eval()
    except Exception:
        try:
            return parameter.val
        except Exception:
            return fallback

def _pulse(comp, name):
    parameter = _par(comp, name)
    if parameter is None:
        return False
    try:
        parameter.pulse()
        return True
    except Exception:
        try:
            parameter.val = True
            parameter.val = False
            return True
        except Exception:
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

def _config_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate runtime config key')
        result[key] = value
    return result

def _config_constant(value):
    raise ValueError('non-finite runtime config number')

def _json(path):
    if not path:
        return {}
    try:
        expanded = os.path.expandvars(os.path.expanduser(path))
        if str(expanded).lower().endswith('.toml'):
            import tomllib
            with open(expanded, 'rb') as handle:
                value = tomllib.load(handle)
        else:
            with open(expanded, 'r', encoding='utf-8-sig') as handle:
                value = json.load(handle, object_pairs_hook=_config_pairs,
                                  parse_constant=_config_constant)
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

def _set_shader_int_constant(root_comp, path, variable, marker, value):
    dat = root_comp.op(path)
    if dat is None:
        return False
    try:
        original = str(dat.text)
        lines = original.splitlines()
        replacement = 'const int %s = %d; // %s' % (
            variable, int(value), marker)
        for index, line in enumerate(lines):
            if marker in line:
                indent = line[:len(line) - len(line.lstrip())]
                updated = indent + replacement
                if updated != line:
                    lines[index] = updated
                    dat.text = '\n'.join(lines) + ('\n' if original.endswith('\n') else '')
                return True
        return False
    except Exception as exc:
        print('[FlexGPU] shader integer control warning: %s' % exc)
        return False

def _vec4(value, fallback):
    try:
        if isinstance(value, str):
            values = value.replace(',', ' ').split()
        else:
            values = list(value)
        parsed = [float(item) for item in values]
        if len(parsed) != 4 or not all(math.isfinite(item) for item in parsed):
            raise ValueError('expected four finite numbers')
        return parsed
    except Exception:
        return list(fallback)

def _set_shader_vec4_constant(root_comp, path, variable, marker, value, fallback):
    dat = root_comp.op(path)
    if dat is None:
        return False
    try:
        row = _vec4(value, fallback)
        original = str(dat.text)
        lines = original.splitlines()
        replacement = ('const vec4 %s = vec4(%.9g, %.9g, %.9g, %.9g); // %s' %
                       (variable, row[0], row[1], row[2], row[3], marker))
        for index, line in enumerate(lines):
            if marker in line:
                indent = line[:len(line) - len(line.lstrip())]
                updated = indent + replacement
                if updated != line:
                    lines[index] = updated
                    dat.text = '\n'.join(lines) + ('\n' if original.endswith('\n') else '')
                return True
        return False
    except Exception as exc:
        print('[FlexGPU] shader matrix control warning: %s' % exc)
        return False

def _apply_calibrated_contracts(root_comp, state):
    reconstruction = root_comp.op('WORKING_PIPELINE/RECONSTRUCTION')
    depth_shader = 'WORKING_PIPELINE/RECONSTRUCTION/depth_to_position_PIXEL'
    mode_value = _value(reconstruction, 'Depthmode', 'normalized')
    mode_name = str(mode_value).strip().lower()
    mode = {'normalized':0, 'metric':1, 'inverse':2}.get(mode_name)
    if mode is None:
        try:
            mode = max(0, min(2, int(mode_value)))
        except Exception:
            mode = 0
    _set_shader_int_constant(root_comp, depth_shader, 'depthMode',
                             'FLEXGPU_DEPTH_MODE', mode)
    for parameter, variable, marker, fallback in (
        ('Depthscale', 'depthScale', 'FLEXGPU_DEPTH_SCALE', 1.0),
        ('Depthbias', 'depthBias', 'FLEXGPU_DEPTH_BIAS', 0.0),
        ('Nearmetres', 'nearMetres', 'FLEXGPU_NEAR_METRES', 0.35),
        ('Farmetres', 'farMetres', 'FLEXGPU_FAR_METRES', 4.5),
        ('Fxnormalized', 'fxNormalized', 'FLEXGPU_INTRINSICS_FX', 0.0),
        ('Fynormalized', 'fyNormalized', 'FLEXGPU_INTRINSICS_FY', 0.0),
        ('Cxnormalized', 'cxNormalized', 'FLEXGPU_INTRINSICS_CX', 0.5),
        ('Cynormalized', 'cyNormalized', 'FLEXGPU_INTRINSICS_CY', 0.5),
    ):
        _set_shader_constant(root_comp, depth_shader, variable, marker,
                             _number(_value(reconstruction, parameter, fallback), fallback))
    identity = ((1, 0, 0, 0), (0, 1, 0, 0),
                (0, 0, 1, 0), (0, 0, 0, 1))
    for index in range(4):
        _set_shader_vec4_constant(
            root_comp, depth_shader, 'cameraToWorld%d' % index,
            'FLEXGPU_CAMERA_TO_WORLD_%d' % index,
            _value(reconstruction, 'Cameratoworld%d' % index,
                   ' '.join(str(x) for x in identity[index])), identity[index])

    sensor = root_comp.op('WORKING_PIPELINE/SENSOR_INTERACTION')
    interaction_shader = 'WORKING_PIPELINE/SENSOR_INTERACTION/interaction_field_PIXEL'
    _set_shader_constant(
        root_comp, interaction_shader, 'interactionRadiusMetres',
        'FLEXGPU_INTERACTION_RADIUS',
        max(0.001, _number(_value(sensor, 'Interactionradius', 0.55), 0.55)))
    _set_shader_constant(
        root_comp, interaction_shader, 'forceGain', 'FLEXGPU_FORCE_GAIN',
        max(0.0, _number(_value(sensor, 'Forcegain', 1.0), 1.0)))
    sensor_shader = 'WORKING_PIPELINE/SENSOR_INTERACTION/CALIBRATE_SENSOR_POSITION_PIXEL'
    for index in range(4):
        _set_shader_vec4_constant(
            root_comp, sensor_shader, 'sensorToWorld%d' % index,
            'FLEXGPU_SENSOR_TO_WORLD_%d' % index,
            _value(sensor, 'Sensortoworld%d' % index,
                   ' '.join(str(x) for x in identity[index])), identity[index])

    temporal = root_comp.op('WORKING_PIPELINE/TEMPORAL_WORLD')
    temporal_shader = 'WORKING_PIPELINE/TEMPORAL_WORLD/temporal_state_PIXEL'
    confidence_decay = min(1.0, max(
        0.0, _number(_value(temporal, 'Confidencedecay', 0.985), 0.985)))
    age_seconds = max(0.05, _number(_value(temporal, 'Ageseconds', 2.0), 2.0))
    try:
        cook_rate = max(1.0, float(project.cookRate))
    except Exception:
        cook_rate = 60.0
    _set_shader_constant(root_comp, temporal_shader, 'confidenceDecay',
                         'FLEXGPU_CONFIDENCE_DECAY', confidence_decay)
    _set_shader_constant(root_comp, temporal_shader, 'ageStep',
                         'FLEXGPU_AGE_STEP', 1.0 / (cook_rate * age_seconds))

    completion = root_comp.op('WORKING_PIPELINE/COMPLETION')
    radius = max(1.0, _number(_value(completion, 'Disocclusionradius', 2.0), 2.0))
    noise = max(0.0, _number(_value(completion, 'Fognoise', 0.5), 0.5))
    _set_shader_constant(root_comp,
        'WORKING_PIPELINE/COMPLETION/fog_completion_PIXEL',
        'disocclusionRadius', 'FLEXGPU_DISOCCLUSION_RADIUS', radius)
    _set_shader_constant(root_comp,
        'WORKING_PIPELINE/COMPLETION/fog_completion_PIXEL',
        'fogNoiseAmount', 'FLEXGPU_FOG_NOISE', noise)

    fog_density = max(0.0, _number(
        _value(completion, 'Fogdensity', state.get('fog_density', 0.35)), 0.35))
    installation = root_comp.op('WORKING_PIPELINE/INSTALLATION_OUTPUT')
    stereo = root_comp.op('WORKING_PIPELINE/STEREO_PREVIEW')
    for path, view_comp in (
        ('WORKING_PIPELINE/INSTALLATION_OUTPUT/installation_grade_PIXEL', installation),
        ('WORKING_PIPELINE/STEREO_PREVIEW/GRADE_LEFT_EYE_PIXEL', stereo),
        ('WORKING_PIPELINE/STEREO_PREVIEW/GRADE_RIGHT_EYE_PIXEL', stereo),
    ):
        view_density = max(0.0, _number(
            _value(view_comp, 'Fogdensity', fog_density), fog_density))
        view_radius = max(1.0, _number(
            _value(view_comp, 'Fogradius', radius), radius))
        _set_shader_constant(root_comp, path, 'viewFogDensity',
                             'FLEXGPU_VIEW_FOG_DENSITY', view_density)
        _set_shader_constant(root_comp, path, 'viewFogRadius',
                             'FLEXGPU_VIEW_FOG_RADIUS', view_radius)

def _temporal_signature(root_comp, state):
    sources = root_comp.op('WORKING_PIPELINE/SOURCES')
    reconstruction = root_comp.op('WORKING_PIPELINE/RECONSTRUCTION')
    source_config = _mapping(state.get('source'))
    sensor_config = _mapping(state.get('sensor'))
    values = [
        _integer(_value(reconstruction, 'Geometryresolution',
                        state.get('geometry_resolution', 384)), 384),
        str(_value(sources, 'UseStreamDiffusion', False)),
        str(_value(sources, 'UseExternalDepth', False)),
        _integer(_value(sources, 'Sessionepoch', 0), 0),
        str(_value(reconstruction, 'Depthmode', 'normalized')),
        _number(_value(reconstruction, 'Depthscale', 1.0), 1.0),
        _number(_value(reconstruction, 'Depthbias', 0.0), 0.0),
        _number(_value(reconstruction, 'Nearmetres', 0.35), 0.35),
        _number(_value(reconstruction, 'Farmetres', 4.5), 4.5),
        _number(_value(reconstruction, 'Fxnormalized', 0.0), 0.0),
        _number(_value(reconstruction, 'Fynormalized', 0.0), 0.0),
        _number(_value(reconstruction, 'Cxnormalized', 0.5), 0.5),
        _number(_value(reconstruction, 'Cynormalized', 0.5), 0.5),
        _integer(_value(reconstruction, 'Calibrationepoch', 0), 0),
    ]
    for index in range(4):
        values.append(tuple(_vec4(
            _value(reconstruction, 'Cameratoworld%d' % index, ''),
            ((1, 0, 0, 0), (0, 1, 0, 0),
             (0, 0, 1, 0), (0, 0, 0, 1))[index])))
    # Configured local adapter/calibration identities are safe fingerprints;
    # no file is read and no private component is imported here.
    for key in ('mode', 'streamdiffusion_tox', 'replay_path', 'rgb_operator',
                'depth_operator', 'mask_operator', 'confidence_operator',
                'frame_state_operator', 'camera_metadata_operator',
                'calibration_path'):
        values.append(str(source_config.get(key, '')))
    for key in ('mode', 'adapter_tox', 'replay_path', 'calibration_path',
                'position_operator', 'mask_operator', 'confidence_operator',
                'frame_state_operator'):
        values.append(str(sensor_config.get(key, '')))
    return tuple(values)

def _reset_temporal_history(root_comp, runtime, reason):
    reset = False
    for name in ('POSITION_HISTORY', 'COLOR_HISTORY', 'STATE_HISTORY'):
        reset = (_pulse(root_comp.op(
            'WORKING_PIPELINE/TEMPORAL_WORLD/' + name), 'reset') or reset)
    count = int(runtime.get('temporal_reset_count', 0)) + 1
    runtime['temporal_reset_count'] = count
    runtime['temporal_last_reset_reason'] = str(reason)
    state = runtime.get('state', {})
    state['temporal_reset_count'] = count
    state['temporal_last_reset_reason'] = str(reason)
    temporal = root_comp.op('WORKING_PIPELINE/TEMPORAL_WORLD')
    _set(temporal, 'Resetcount', count)
    _set(temporal, 'Sourceepoch', _integer(_value(
        root_comp.op('WORKING_PIPELINE/SOURCES'), 'Sessionepoch', 0), 0))
    return reset

def _check_temporal_signature(root_comp, state, reason='contract change'):
    try:
        runtime = root_comp.fetch('_flexgpu_runtime', None)
    except Exception:
        runtime = None
    if not isinstance(runtime, dict):
        return False
    signature = _temporal_signature(root_comp, state)
    previous = runtime.get('temporal_signature')
    if previous == signature:
        return False
    runtime['temporal_signature'] = signature
    _reset_temporal_history(root_comp, runtime,
                            'initial contract' if previous is None else reason)
    return True

def _health_snapshot(root_comp, runtime, frame_ms):
    state = runtime['state']
    sources = root_comp.op('WORKING_PIPELINE/SOURCES')
    sensor = root_comp.op('WORKING_PIPELINE/SENSOR_INTERACTION')
    source_age = _number(_value(sources, 'Sourceagems', -1.0), -1.0)
    sensor_age = _number(_value(sensor, 'Sensoragems', -1.0), -1.0)
    source_config = _mapping(state.get('source'))
    sensor_config = _mapping(state.get('sensor'))
    source_timeout = _number(source_config.get('stale_timeout_ms', 1000), 1000)
    sensor_timeout = _number(sensor_config.get('stale_timeout_ms', 1000), 1000)
    warnings = []
    if source_age >= 0 and source_age > source_timeout:
        warnings.append('source_stale')
    if sensor_age >= 0 and sensor_age > sensor_timeout:
        warnings.append('sensor_stale')
    if state.get('transport_error'):
        warnings.append('transport_error')
    for field, warning in (
        ('source_contract_error', 'source_contract_error'),
        ('calibration_error', 'calibration_error'),
        ('source_fallback', 'source_fallback'),
        ('sensor_fallback', 'sensor_fallback'),
    ):
        if state.get(field):
            warnings.append(warning)
    return {
        'status':'warning' if warnings else 'healthy',
        'warnings':warnings,
        'frame_time_ms':float(frame_ms),
        'operator_cook_time_ms':_operator_cook_ms(root_comp),
        'source_age_ms':source_age,
        'sensor_age_ms':sensor_age,
        'source_frame_id':_integer(_value(sources, 'Frameid', -1), -1),
        'sensor_frame_id':_integer(_value(sensor, 'Sensorframeid', -1), -1),
        'source_session_epoch':_integer(_value(sources, 'Sessionepoch', 0), 0),
        'temporal_resets':int(runtime.get('temporal_reset_count', 0)),
        'temporal_last_reset_reason':runtime.get('temporal_last_reset_reason', ''),
        'bridge_mode':state.get('bridge_mode', 'local'),
        'adaptive_level':runtime.get('level', 0),
    }

def _write_health(root_comp, health):
    table = root_comp.op('WORKING_PIPELINE/TELEMETRY/LIVE_HEALTH')
    if table is None:
        return
    try:
        table.clear()
        table.appendRow(['metric', 'value'])
        for key in sorted(health):
            table.appendRow([key, _display(health[key])])
    except Exception:
        pass

def _local_path(state, value, suffix=None):
    if value in (None, ''):
        return ''
    try:
        path = os.path.expandvars(os.path.expanduser(str(value)))
        if not os.path.isabs(path):
            config_path = str(state.get('config_path', ''))
            base = os.path.dirname(config_path) if config_path else os.getcwd()
            path = os.path.join(base, path)
        path = os.path.abspath(os.path.normpath(path))
        if suffix and not path.lower().endswith(str(suffix).lower()):
            return ''
        if not os.path.isfile(path):
            return ''
        return path
    except Exception:
        return ''

def _child_op(root_comp, container, path, require_top=True):
    if not path:
        return None
    text = str(path).strip()
    candidates = []
    try:
        candidates.append(container.op(text))
    except Exception:
        pass
    try:
        candidates.append(root_comp.op(text))
    except Exception:
        pass
    if text.startswith('/'):
        try:
            candidates.append(op(text))
        except Exception:
            pass
    for candidate in candidates:
        if candidate is not None:
            try:
                if require_top and hasattr(candidate, 'isTOP') and not candidate.isTOP:
                    continue
            except Exception:
                pass
            return candidate
    return None

def _wire_adapter_output(root_comp, adapter, output_name, source):
    if source is None:
        return False
    output = None
    try:
        output = adapter.op(output_name)
    except Exception:
        pass
    if output is None:
        output = root_comp.op(adapter.path.replace(root_comp.path + '/', '') +
                              '/' + output_name)
    if output is None:
        return False
    try:
        connector = output.inputConnectors[0]
        for connection in list(connector.connections):
            try:
                connection.disconnect()
            except Exception:
                pass
        connector.connect(source)
        return True
    except Exception:
        return False

def _auto_load_tox(root_comp, adapter, state, config, label):
    if not bool(config.get('auto_load_tox', False)):
        return adapter
    path = _local_path(state, config.get(
        'streamdiffusion_tox' if label == 'source' else 'adapter_tox'), '.tox')
    if not path:
        state[label + '_adapter_error'] = 'configured local .tox is missing or invalid'
        return None
    try:
        holder = adapter.op('AUTO_LOADED_TOX')
    except Exception:
        holder = None
    if holder is None:
        try:
            type_symbol = globals().get('baseCOMP')
            if type_symbol is None:
                raise RuntimeError('baseCOMP is unavailable')
            holder = adapter.create(type_symbol, 'AUTO_LOADED_TOX')
        except Exception:
            state[label + '_adapter_error'] = 'could not create local .tox holder'
            return None
    try:
        previous = holder.fetch('_flexgpu_loaded_tox', '')
    except Exception:
        previous = ''
    if previous and os.path.normcase(str(previous)) != os.path.normcase(path):
        state[label + '_adapter_error'] = 'local .tox changed; restart before loading another component'
        return None
    if not previous:
        try:
            holder.loadTox(path)
            holder.store('_flexgpu_loaded_tox', path)
        except Exception:
            state[label + '_adapter_error'] = 'local .tox load failed'
            return None
    state[label + '_adapter_loaded'] = True
    return holder

def _finite(value, label):
    if isinstance(value, bool):
        raise ValueError(label + ' must be numeric')
    try:
        result = float(value)
    except Exception:
        raise ValueError(label + ' must be numeric')
    if not math.isfinite(result):
        raise ValueError(label + ' must be finite')
    return result

def _matrix16(value, label):
    if not isinstance(value, (list, tuple)) or len(value) != 16:
        raise ValueError(label + ' must contain 16 numbers')
    return [_finite(item, label) for item in value]

def _strict_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate calibration key')
        result[key] = value
    return result

def _reject_json_constant(value):
    raise ValueError('non-finite calibration number')

def _load_calibration(root_comp, state, configured_path):
    path = _local_path(state, configured_path, '.json')
    if not path:
        state['calibration_error'] = 'configured calibration is missing or invalid'
        return False
    try:
        if os.path.getsize(path) > 1024 * 1024:
            raise ValueError('calibration is too large')
        with open(path, 'r', encoding='utf-8-sig') as handle:
            data = json.load(handle, object_pairs_hook=_strict_pairs,
                             parse_constant=_reject_json_constant)
        if not isinstance(data, dict) or data.get('version') != 'flexgpu-calibration/v1':
            raise ValueError('unsupported calibration version')
        allowed = {'version', 'calibration_id', 'image', 'intrinsics', 'depth',
                   'camera_to_world', 'sensor_to_world', 'coordinate_system'}
        if set(data).difference(allowed):
            raise ValueError('unsupported calibration field')
        if data.get('coordinate_system', 'right_handed_y_up_metres') != \
                'right_handed_y_up_metres':
            raise ValueError('unsupported coordinate system')
        calibration_id = data.get('calibration_id')
        if (not isinstance(calibration_id, str) or
                re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}',
                             calibration_id) is None):
            raise ValueError('invalid calibration id')
        existing_id = state.get('calibration_id')
        if existing_id and existing_id != calibration_id:
            raise ValueError('source and sensor calibration ids differ')
        image = data.get('image')
        if not isinstance(image, dict) or set(image).difference({'width', 'height'}):
            raise ValueError('invalid calibration image')
        if type(image.get('width')) is not int or type(image.get('height')) is not int:
            raise ValueError('calibration dimensions must be integers')
        width = image['width']
        height = image['height']
        if width < 1 or height < 1 or width > 16384 or height > 16384:
            raise ValueError('invalid calibration dimensions')
        intrinsics = data.get('intrinsics')
        if (not isinstance(intrinsics, dict) or
                set(intrinsics).difference({'fx', 'fy', 'cx', 'cy'})):
            raise ValueError('invalid calibration intrinsics')
        fx = _finite(intrinsics.get('fx'), 'fx')
        fy = _finite(intrinsics.get('fy'), 'fy')
        cx = _finite(intrinsics.get('cx'), 'cx')
        cy = _finite(intrinsics.get('cy'), 'cy')
        if fx <= 0 or fy <= 0:
            raise ValueError('invalid focal length')
        if fx > width * 100 or fy > height * 100:
            raise ValueError('implausible focal length')
        if not (-width <= cx <= width * 2 and -height <= cy <= height * 2):
            raise ValueError('invalid principal point')
        depth = data.get('depth')
        if (not isinstance(depth, dict) or
                set(depth).difference({'encoding', 'scale', 'bias',
                                       'near_m', 'far_m'})):
            raise ValueError('invalid calibration depth')
        encoding = str(depth.get('encoding', ''))
        mode = {'normalized':'normalized', 'metres':'metric',
                'millimetres':'metric', 'disparity':'inverse',
                'inverse_depth':'inverse'}.get(encoding)
        if mode is None:
            raise ValueError('unsupported depth encoding')
        scale = _finite(depth.get('scale', 1.0), 'depth scale')
        bias = _finite(depth.get('bias', 0.0), 'depth bias')
        near_m = _finite(depth.get('near_m'), 'near')
        far_m = _finite(depth.get('far_m'), 'far')
        if scale <= 0 or near_m <= 0 or far_m <= near_m or far_m > 1000:
            raise ValueError('invalid depth range')
        camera = _matrix16(data.get('camera_to_world'), 'camera_to_world')
        sensor = _matrix16(data.get('sensor_to_world'), 'sensor_to_world')
        for matrix in (camera, sensor):
            if (abs(matrix[12]) > 1e-6 or abs(matrix[13]) > 1e-6 or
                    abs(matrix[14]) > 1e-6 or abs(matrix[15] - 1.0) > 1e-6):
                raise ValueError('transform must be homogeneous row-major')
            determinant = (
                matrix[0] * (matrix[5] * matrix[10] - matrix[6] * matrix[9]) -
                matrix[1] * (matrix[4] * matrix[10] - matrix[6] * matrix[8]) +
                matrix[2] * (matrix[4] * matrix[9] - matrix[5] * matrix[8]))
            if determinant <= 1e-8:
                raise ValueError(
                    'transform must have a non-singular right-handed spatial basis')
        reconstruction = root_comp.op('WORKING_PIPELINE/RECONSTRUCTION')
        _set(reconstruction, 'Depthmode', mode)
        _set(reconstruction, 'Depthscale', scale)
        _set(reconstruction, 'Depthbias', bias)
        _set(reconstruction, 'Nearmetres', near_m)
        _set(reconstruction, 'Farmetres', far_m)
        _set(reconstruction, 'Fxnormalized', fx / float(width))
        _set(reconstruction, 'Fynormalized', fy / float(height))
        _set(reconstruction, 'Cxnormalized', cx / float(width))
        _set(reconstruction, 'Cynormalized', cy / float(height))
        epoch = sum((index + 1) * ord(char)
                    for index, char in enumerate(calibration_id)) % 2147483647
        _set(reconstruction, 'Calibrationepoch', epoch)
        sensor_comp = root_comp.op('WORKING_PIPELINE/SENSOR_INTERACTION')
        for index in range(4):
            _set(reconstruction, 'Cameratoworld%d' % index,
                 ' '.join('%.12g' % item for item in camera[index * 4:index * 4 + 4]))
            _set(sensor_comp, 'Sensortoworld%d' % index,
                 ' '.join('%.12g' % item for item in sensor[index * 4:index * 4 + 4]))
        state['calibration_id'] = calibration_id
        state['calibration_status'] = 'ready'
        return True
    except Exception:
        state['calibration_error'] = 'calibration validation failed'
        return False

def _configure_source_adapter(root_comp, state, source):
    adapter = root_comp.op('WORKING_PIPELINE/SOURCES/STREAMDIFFUSION_ADAPTER')
    if adapter is None:
        state['source_adapter_error'] = 'source adapter component is missing'
        return False
    search_root = _auto_load_tox(root_comp, adapter, state, source, 'source')
    if search_root is None:
        return False
    if source.get('auto_load_tox') and not source.get('rgb_operator'):
        state['source_adapter_error'] = 'auto-loaded source requires rgb_operator'
        return False
    outputs = (
        ('rgb_operator', 'OUT_RGB'),
        ('depth_operator', 'OUT_DEPTH'),
        ('mask_operator', 'OUT_MASK'),
        ('confidence_operator', 'OUT_CONFIDENCE'),
    )
    for field, output in outputs:
        configured = source.get(field)
        if not configured:
            continue
        node = _child_op(root_comp, search_root, configured)
        if node is None or not _wire_adapter_output(root_comp, adapter, output, node):
            state['source_adapter_error'] = field + ' could not be resolved/wired'
            return False
    for field in ('frame_state_operator', 'camera_metadata_operator'):
        configured = source.get(field)
        if (configured and
                _child_op(root_comp, search_root, configured,
                          require_top=False) is None):
            state['source_adapter_error'] = field + ' could not be resolved'
            return False
    calibration_path = source.get('calibration_path')
    if calibration_path and not _load_calibration(root_comp, state, calibration_path):
        return False
    state['source_adapter_status'] = 'ready'
    return True

def _configure_sensor_adapter(root_comp, state, sensor):
    adapter = root_comp.op(
        'WORKING_PIPELINE/SENSOR_INTERACTION/DEPTH_SENSOR_ADAPTER')
    if adapter is None:
        state['sensor_adapter_error'] = 'sensor adapter component is missing'
        return False
    search_root = _auto_load_tox(root_comp, adapter, state, sensor, 'sensor')
    if search_root is None:
        return False
    if sensor.get('auto_load_tox') and not sensor.get('position_operator'):
        state['sensor_adapter_error'] = 'auto-loaded sensor requires position_operator'
        return False
    for field, output in (('position_operator', 'OUT_POSITION'),
                          ('mask_operator', 'OUT_MASK'),
                          ('confidence_operator', 'OUT_CONFIDENCE')):
        configured = sensor.get(field)
        if not configured:
            continue
        node = _child_op(root_comp, search_root, configured)
        if node is None or not _wire_adapter_output(root_comp, adapter, output, node):
            state['sensor_adapter_error'] = field + ' could not be resolved/wired'
            return False
    for field in ('frame_state_operator',):
        configured = sensor.get(field)
        if (configured and
                _child_op(root_comp, search_root, configured,
                          require_top=False) is None):
            state['sensor_adapter_error'] = field + ' could not be resolved'
            return False
    calibration_path = sensor.get('calibration_path')
    if calibration_path and not _load_calibration(root_comp, state, calibration_path):
        return False
    state['sensor_adapter_status'] = 'ready'
    return True

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
        owns_source = bool(state.get('ai_active', True))
        if owns_source:
            active_source = (requested_source if requested_source in
                             ('demo', 'streamdiffusion') else 'demo')
            if (active_source == 'streamdiffusion' and
                    not _configure_source_adapter(root_comp, state, source)):
                active_source = 'demo'
        else:
            # A split world process consumes the bridge. Never load or resolve
            # the private AI adapter in that process, but do apply the shared
            # camera calibration needed to reconstruct received depth.
            active_source = ('remote' if state.get('transport_receiver_active')
                             else 'inactive')
            calibration_path = source.get('calibration_path')
            if (calibration_path and
                    not _load_calibration(root_comp, state, calibration_path)):
                state['source_contract_error'] = (
                    'remote reconstruction disabled: calibration is invalid')
                state['world_active'] = False
                state['installation_active'] = False
                state['vr_active'] = False
        use_stream = owns_source and active_source == 'streamdiffusion'
        _set(sources, 'UseStreamDiffusion', use_stream)
        if 'depth_operator' in source:
            _set(sources, 'UseExternalDepth',
                 use_stream and bool(source.get('depth_operator')))
        _set(root_comp.op('WORKING_PIPELINE/SOURCES/STREAMDIFFUSION_ADAPTER'),
             'Enabled', use_stream)
        state['source_mode_requested'] = requested_source
        state['source_mode_active'] = active_source
        if owns_source and active_source != requested_source:
            reason = state.get('source_adapter_error') or state.get(
                'calibration_error') or 'adapter for %s is not installed' % requested_source
            state['source_fallback'] = 'demo (%s)' % reason

    for path in ('WORKING_PIPELINE/SOURCES/DEMO_RGB_GENERATOR',
                 'WORKING_PIPELINE/SOURCES/STREAMDIFFUSION_ADAPTER/REPLACE_WITH_STREAMDIFFUSION_RGB'):
        _set_resolution(root_comp.op(path), state['diffusion_resolution'],
                        state['diffusion_resolution'])
    for path in ('WORKING_PIPELINE/SOURCES/DEMO_DEPTH_GENERATOR',
                 'WORKING_PIPELINE/SOURCES/DEMO_CONFIDENCE',
                 'WORKING_PIPELINE/SOURCES/DEMO_VALID_MASK',
                 'WORKING_PIPELINE/SOURCES/STREAMDIFFUSION_ADAPTER/REPLACE_WITH_DEPTH_ESTIMATE',
                 'WORKING_PIPELINE/SOURCES/STREAMDIFFUSION_ADAPTER/REPLACE_WITH_CONFIDENCE',
                 'WORKING_PIPELINE/SOURCES/STREAMDIFFUSION_ADAPTER/REPLACE_WITH_VALID_MASK',
                 'WORKING_PIPELINE/RECONSTRUCTION/CONFIDENCE_ALIGNED_RESIZE',
                 'WORKING_PIPELINE/TEMPORAL_WORLD/STATE_SEED'):
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
        _set(root_comp.op('WORKING_PIPELINE/INSTALLATION_OUTPUT'),
             'Fogdensity', state['fog_density'])
        _set(root_comp.op('WORKING_PIPELINE/STEREO_PREVIEW'),
             'Fogdensity', state['fog_density'])
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
        owns_sensor = bool(state.get('world_active', True))
        if owns_sensor:
            active_sensor = (sensor_mode if sensor_mode in
                             ('simulated', 'replay', 'depth_sensor')
                             else 'simulated')
            if (active_sensor == 'depth_sensor' and
                    not _configure_sensor_adapter(root_comp, state, sensor)):
                active_sensor = 'simulated'
        else:
            # AI-only and contract-failed processes must not import a local
            # sensor SDK/.tox or resolve its operators.
            active_sensor = 'inactive'
        _set(root_comp.op('WORKING_PIPELINE/SENSOR_INTERACTION'), 'Mode',
             active_sensor if active_sensor != 'inactive' else 'simulated')
        mask = root_comp.op('WORKING_PIPELINE/SENSOR_INTERACTION/SIMULATED_SENSOR_MASK')
        _set(mask, 'radius', 0.0 if sensor_mode == 'disabled' or not owns_sensor else 0.16)
        state['sensor_mode_active'] = (
            'inactive' if not owns_sensor else
            'disabled' if sensor_mode == 'disabled' else active_sensor)
        _set(root_comp.op(
            'WORKING_PIPELINE/SENSOR_INTERACTION/DEPTH_SENSOR_ADAPTER'),
            'Enabled', active_sensor == 'depth_sensor')
        if 'interaction_radius_m' in sensor:
            _set(root_comp.op('WORKING_PIPELINE/SENSOR_INTERACTION'),
                 'Interactionradius', sensor.get('interaction_radius_m'))
        if 'force_gain' in sensor:
            _set(root_comp.op('WORKING_PIPELINE/SENSOR_INTERACTION'),
                 'Forcegain', sensor.get('force_gain'))
        if owns_sensor and active_sensor != sensor_mode and sensor_mode != 'disabled':
            reason = state.get('sensor_adapter_error') or state.get(
                'calibration_error') or 'depth-sensor adapter is not installed'
            state['sensor_fallback'] = 'simulated (%s)' % reason

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
    _set(root_comp.op('WORKING_PIPELINE/ROLE_BRIDGE/CONFIDENCE_ROUTE'),
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
    _apply_calibrated_contracts(root_comp, state)
    _check_temporal_signature(root_comp, state, 'runtime contract changed')

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
        'temporal_signature': None, 'temporal_reset_count': 0,
        'temporal_last_reset_reason': '',
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

    try:
        previous_runtime = root_comp.fetch('_flexgpu_runtime', None)
    except Exception:
        previous_runtime = None
    runtime = _configure_adaptive(state)
    if isinstance(previous_runtime, dict):
        for key in ('temporal_signature', 'temporal_reset_count',
                    'temporal_last_reset_reason'):
            if key in previous_runtime:
                runtime[key] = previous_runtime[key]
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
        elif state.get('source_fallback') or state.get('sensor_fallback'):
            status = 'WARNING / %s%s' % (
                state.get('source_fallback', ''),
                (' / ' + state.get('sensor_fallback'))
                if state.get('sensor_fallback') else '')
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
    if state.get('source_fallback'):
        print('[FlexGPU] source WARNING: configured adapter was rejected; demo remains active')
    if state.get('sensor_fallback'):
        print('[FlexGPU] sensor WARNING: configured adapter was rejected; simulation remains active')
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
        if (path and summary_path and
                os.path.normcase(os.path.abspath(path)) ==
                os.path.normcase(os.path.abspath(summary_path))):
            print('[FlexGPU] telemetry summary warning: JSONL and summary paths are identical')
            summary_path = ''
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
    signature_changed = _check_temporal_signature(
        root_comp, runtime['state'], 'manual source/calibration contract changed')
    now = time.perf_counter()
    previous = runtime.get('last_tick')
    runtime['last_tick'] = now
    if previous is None:
        _write_health(root_comp, _health_snapshot(root_comp, runtime, 0.0))
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
    health = _health_snapshot(root_comp, runtime, frame_ms)
    if runtime['frame'] % 15 == 0:
        _write_health(root_comp, health)
    if bool(telemetry.get('enabled', False)):
        interval = max(1, _integer(telemetry.get('sample_interval_frames', 1), 1))
        if runtime['frame'] % interval == 0:
            record = {'timestamp':time.time(), 'frame_time_ms':frame_ms,
                      'tier':runtime['state']['tier'], 'role':runtime['state']['role'],
                      'adaptive_level':runtime['level'],
                      'settings':dict((key, runtime['state'][key]) for key in QUALITY_KEYS)}
            if bool(telemetry.get('include_operator_metrics', True)):
                record['operator_cook_time_ms'] = _operator_cook_ms(root_comp)
            record['health'] = health
            runtime['telemetry_buffer'].append(record)
            runtime['telemetry_count'] += 1
            runtime['telemetry_frame_sum'] += frame_ms
            runtime['telemetry_frame_max'] = max(runtime['telemetry_frame_max'], frame_ms)
            flush_every = max(1, _integer(telemetry.get('flush_every', 60), 60))
            if len(runtime['telemetry_buffer']) >= flush_every:
                flush_telemetry(root_comp)
    return {'frame_time_ms':frame_ms, 'changed':changed,
            'temporal_reset':signature_changed,
            'level':runtime['level'], 'health':health}

def safe_reset(root_comp=None):
    root_comp = root_comp or op('/project1/flexgpu')
    state = apply(root_comp, {'role':'world', 'experience':'installation',
                              'completion':'fog',
                              'adaptive':{'enabled':False}})
    runtime = _runtime(root_comp) if root_comp is not None else None
    if runtime is not None:
        _reset_temporal_history(root_comp, runtime, 'operator safe reset')
        _write_state(root_comp, runtime['state'])
    return state

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


def _load_config_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate config key")
        result[key] = value
    return result


def _load_config_constant(value):
    raise ValueError("non-finite config number")


def _load_config(config_path, report):
    if not config_path:
        return {}
    path = os.path.abspath(os.path.expandvars(os.path.expanduser(str(config_path))))
    try:
        if path.lower().endswith(".toml"):
            import tomllib
            with open(path, "rb") as handle:
                data = tomllib.load(handle)
        else:
            with open(path, "r", encoding="utf-8-sig") as handle:
                data = json.load(
                    handle,
                    object_pairs_hook=_load_config_pairs,
                    parse_constant=_load_config_constant,
                )
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
