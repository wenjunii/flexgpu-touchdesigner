"""Build the non-destructive FlexGPU TouchDesigner 2025 show project.

Run this module *inside TouchDesigner*.  It creates or updates only
``/project1/flexgpu`` and never removes unknown nodes.  Alongside stable adapter
contracts it installs the stock-operator ``WORKING_PIPELINE`` demo: animated
RGB/depth, reconstruction, persistence, sensor interaction, view completion,
GPU-native point rendering, installation output, and stereo preview.
"""

from __future__ import print_function

import json
import math
import os
import re


BUILD_VERSION = "1.2.1"
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
import datetime
import hashlib
import json
import math
import os
import re
import time

RUNTIME_BUILD_VERSION = '1.2.1'
FRAME_STATE_VERSION = 'flexgpu-frame-state/v1'
CAMERA_METADATA_VERSION = 'flexgpu-camera-metadata/v1'
HEARTBEAT_VERSION = 1
IDENTIFIER_PATTERN = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')
SHA256_PATTERN = re.compile(r'^[0-9a-f]{64}$')
TRANSFORM_TOLERANCE = 1e-4

# Readiness inspection runs on the heartbeat path, so keep both its cadence and
# result size bounded. External TOX roots are inspected for propagated errors,
# but their private internals are opaque to this scan; a paid or user-supplied
# component must not consume the entire managed-health budget.
READINESS_HEALTH_INTERVAL_SECONDS = 0.5
READINESS_MANAGED_OPERATOR_LIMIT = 512
READINESS_ISSUE_LIMIT = 8
READINESS_MESSAGE_LIMIT = 240
READINESS_MAX_OUTPUT_DIMENSION = 32768

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
             'point_budget':262144, 'vr_fps':90},
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
        result = float(value)
        return result if math.isfinite(result) else float(fallback)
    except (TypeError, ValueError):
        return float(fallback)

def _bounded_integer(value, fallback, low, high, label, errors):
    if isinstance(value, bool):
        errors.append(label + ' must be an integer')
        return int(fallback)
    try:
        parsed = int(value)
        if isinstance(value, float) and value != parsed:
            raise ValueError()
    except (TypeError, ValueError, OverflowError):
        errors.append(label + ' must be an integer')
        return int(fallback)
    if parsed < low or parsed > high:
        errors.append('%s must be between %d and %d' % (label, low, high))
        return min(high, max(low, parsed))
    return parsed

def _bounded_number(value, fallback, low, high, label, errors,
                    exclusive_low=False):
    if isinstance(value, bool):
        errors.append(label + ' must be numeric')
        return float(fallback)
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        errors.append(label + ' must be numeric')
        return float(fallback)
    invalid = (not math.isfinite(parsed) or parsed > high or
               (parsed <= low if exclusive_low else parsed < low))
    if invalid:
        comparator = 'greater than' if exclusive_low else 'at least'
        errors.append('%s must be %s %s and at most %s' %
                      (label, comparator, low, high))
        if not math.isfinite(parsed):
            return float(fallback)
        floor = max(float(fallback), float(low)) if exclusive_low else float(low)
        return min(float(high), max(floor, parsed))
    return parsed

def _materialize(state):
    runtime_errors = []
    defaults = {'role':'standalone', 'topology':'single',
                'experience':'installation', 'completion':'hybrid',
                'tier':'3080ti_16gb'}
    for key, fallback in defaults.items():
        state[key] = str(state.get(key, fallback)).lower()
    if state['tier'] not in QUALITY_PRESETS:
        runtime_errors.append('tier has unsupported value %r' % state['tier'])
        state['tier'] = 'custom'
    for key, permitted in (
        ('role', ('standalone', 'world', 'render', 'ai')),
        ('topology', ('single', 'dual_local', 'dual_network')),
        ('experience', ('installation', 'vr', 'combined')),
        ('completion', ('fog', 'procedural', 'hybrid')),
    ):
        if state[key] not in permitted:
            runtime_errors.append('%s has unsupported value %r' % (key, state[key]))
            state[key] = defaults[key]

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

    integer_bounds = {
        'diffusion_resolution':(64, 2048),
        'diffusion_fps':(1, 240),
        'geometry_resolution':(64, 2048),
        'geometry_fps':(1, 240),
        'point_budget':(1000, 10000000),
        'vr_fps':(1, 240),
    }
    for key in QUALITY_KEYS:
        low, high = integer_bounds[key]
        state[key] = _bounded_integer(
            state[key], QUALITY_PRESETS[state['tier']][key], low, high,
            key, runtime_errors)
    state['installation_fps'] = _bounded_integer(
        state.get('installation_fps', 60), 60, 1, 240,
        'installation_fps', runtime_errors)
    state['installation_width'] = _bounded_integer(
        render.get('installation_width', 1280), 1280, 64, 16384,
        'render.installation_width', runtime_errors)
    state['installation_height'] = _bounded_integer(
        render.get('installation_height', 720), 720, 64, 16384,
        'render.installation_height', runtime_errors)
    state['stereo_width'] = _bounded_integer(
        render.get('stereo_width', 2560), 2560, 64, 16384,
        'render.stereo_width', runtime_errors)
    state['stereo_height'] = _bounded_integer(
        render.get('stereo_height', 720), 720, 64, 16384,
        'render.stereo_height', runtime_errors)
    triple_defaults = {
        '3080ti_16gb':(640, 360),
        '4090':(960, 540),
        '5090':(1280, 720),
        'custom':(640, 360),
    }
    triple_default_width, triple_default_height = triple_defaults.get(
        state['tier'], triple_defaults['custom'])
    state['triple_surface_width'] = _bounded_integer(
        render.get('triple_surface_width', triple_default_width),
        triple_default_width, 64, 8192,
        'render.triple_surface_width', runtime_errors)
    state['triple_surface_height'] = _bounded_integer(
        render.get('triple_surface_height', triple_default_height),
        triple_default_height, 64, 8192,
        'render.triple_surface_height', runtime_errors)
    display_mode = str(render.get('display_mode', 'single')).strip().lower()
    permitted_display_modes = (
        'single', 'panoramic_wrap', 'artistic_multi_angle')
    if display_mode not in permitted_display_modes:
        runtime_errors.append(
            'render.display_mode has unsupported value %r' % display_mode)
        display_mode = 'single'
    state['display_mode'] = display_mode
    state['surface_fov_degrees'] = _bounded_number(
        render.get('surface_fov_degrees', 60.0), 60.0, 10.0, 140.0,
        'render.surface_fov_degrees', runtime_errors)
    state['triple_wrap_yaw_degrees'] = _bounded_number(
        render.get('triple_wrap_yaw_degrees', 45.0), 45.0, 0.0, 120.0,
        'render.triple_wrap_yaw_degrees', runtime_errors)
    state['triple_artistic_yaw_degrees'] = _bounded_number(
        render.get('triple_artistic_yaw_degrees', 18.0), 18.0, 0.0, 90.0,
        'render.triple_artistic_yaw_degrees', runtime_errors)
    state['triple_artistic_offset_metres'] = _bounded_number(
        render.get('triple_artistic_offset_metres', 0.45), 0.45, 0.0, 10.0,
        'render.triple_artistic_offset_metres', runtime_errors)
    state['point_size_px'] = _bounded_number(
        render.get('point_size_px', 3.0), 3.0, 0.0, 128.0,
        'render.point_size_px', runtime_errors, True)
    state['point_keep_fraction'] = _bounded_number(
        render.get('point_keep_fraction', 0.68), 0.68, 0.0, 1.0,
        'render.point_keep_fraction', runtime_errors)
    state['fog_density'] = _bounded_number(
        render.get('fog_density', 0.35), 0.35, 0.0, 10.0,
        'render.fog_density', runtime_errors)
    state['procedural_mix'] = _bounded_number(
        render.get('procedural_mix', 0.72), 0.72, 0.0, 1.0,
        'render.procedural_mix', runtime_errors)
    available_points = state['geometry_resolution'] ** 2
    if state['point_budget'] > available_points:
        state['point_budget_requested'] = state['point_budget']
        state['point_budget'] = available_points
        state['point_budget_adjustment'] = (
            'capped to geometry_resolution^2 (%d)' % available_points)

    transport = _mapping(state.get('transport'))
    transport_type = str(state.get(
        'transport_type', transport.get('type', 'local'))).strip().lower()
    segment = state.get('transport_segment',
                        transport.get('segment_name', 'FlexShowWorldBus'))
    peer_host = state.get('transport_peer_host',
                          transport.get('peer_host', '127.0.0.1'))
    atlas_width = _bounded_integer(state.get(
        'transport_atlas_width', transport.get('atlas_width', 1024)),
        1024, 2, 16384, 'transport.atlas_width', runtime_errors)
    atlas_height = _bounded_integer(state.get(
        'transport_atlas_height', transport.get('atlas_height', 512)),
        512, 1, 16384, 'transport.atlas_height', runtime_errors)
    atlas_port = _bounded_integer(state.get(
        'transport_atlas_port', transport.get('atlas_port', 12000)),
        12000, 1, 65535, 'transport.atlas_port', runtime_errors)
    transport_fps = _bounded_integer(state.get(
        'transport_fps', transport.get('atlas_fps', state['geometry_fps'])),
        state['geometry_fps'], 1, 240, 'transport.atlas_fps', runtime_errors)
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
    if runtime_errors:
        state['runtime_error'] = '; '.join(runtime_errors)
    return state

def _role_policy(state):
    role = state['role']
    if role == 'render':
        role = 'world'
        state['role'] = role
    topology = state['topology']
    invalid_runtime = bool(state.get('transport_error') or state.get('runtime_error'))
    ai_on = (not invalid_runtime and
             (role in ('standalone', 'ai') or
              (role == 'world' and topology == 'single')))
    world_on = not invalid_runtime and role in ('standalone', 'world')
    split_role = (not invalid_runtime and
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

def _public_state(state):
    public_keys = (
        'role', 'topology', 'experience', 'completion', 'tier',
        'diffusion_resolution', 'diffusion_fps', 'geometry_resolution',
        'geometry_fps', 'point_budget', 'vr_fps', 'installation_fps',
        'installation_width', 'installation_height', 'stereo_width',
        'stereo_height', 'triple_surface_width', 'triple_surface_height',
        'display_mode', 'surface_fov_degrees',
        'triple_wrap_yaw_degrees', 'triple_artistic_yaw_degrees',
        'triple_artistic_offset_metres',
        'point_size_px', 'fog_density', 'procedural_mix',
        'ai_active', 'world_active', 'installation_active', 'vr_active',
        'transport_type', 'transport_fps', 'transport_atlas_width',
        'transport_atlas_height', 'bridge_mode', 'adaptive_enabled',
        'adaptive_level', 'sensor_mode_active', 'source_mode_active',
        'sensor_route_active',
        'source_frame_decision', 'source_metadata_mode',
        'source_camera_metadata_status', 'source_camera_metadata_error',
        'source_camera_session_id', 'source_generation_id',
        'source_calibration_status', 'source_calibration_error',
        'sensor_frame_decision', 'sensor_metadata_mode',
        'sensor_calibration_status', 'sensor_calibration_error',
    )
    return dict((key, state[key]) for key in public_keys if key in state)

def _write_state(root_comp, state):
    table = root_comp.op('CONFIG/runtime_state')
    if table is None:
        return
    public = _public_state(state)
    try:
        table.clear()
        table.appendRow(['key', 'value'])
        for key in sorted(public):
            table.appendRow([key, _display(public[key])])
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
        max(0.0, _number(_value(sensor, 'Forcegain', 0.35), 0.35)))
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
    _set_shader_constant(root_comp, temporal_shader, 'confidenceDecay',
                         'FLEXGPU_CONFIDENCE_DECAY', confidence_decay)

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
    triple = root_comp.op('WORKING_PIPELINE/TRIPLE_DISPLAY')
    stereo = root_comp.op('WORKING_PIPELINE/STEREO_PREVIEW')
    for path, view_comp in (
        ('WORKING_PIPELINE/INSTALLATION_OUTPUT/installation_grade_PIXEL', installation),
        ('WORKING_PIPELINE/TRIPLE_DISPLAY/GRADE_WRAP_LEFT_PIXEL', triple),
        ('WORKING_PIPELINE/TRIPLE_DISPLAY/GRADE_WRAP_CENTER_PIXEL', triple),
        ('WORKING_PIPELINE/TRIPLE_DISPLAY/GRADE_WRAP_RIGHT_PIXEL', triple),
        ('WORKING_PIPELINE/TRIPLE_DISPLAY/GRADE_ARTISTIC_LEFT_PIXEL', triple),
        ('WORKING_PIPELINE/TRIPLE_DISPLAY/GRADE_ARTISTIC_CENTER_PIXEL', triple),
        ('WORKING_PIPELINE/TRIPLE_DISPLAY/GRADE_ARTISTIC_RIGHT_PIXEL', triple),
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
    reconstruction_rgb = root_comp.op(
        'WORKING_PIPELINE/RECONSTRUCTION/RGB_IN')
    source_config = _mapping(state.get('source'))
    sensor_config = _mapping(state.get('sensor'))
    source_calibration = _calibration_identity(state, 'source')
    sensor_calibration = _calibration_identity(state, 'sensor')
    values = [
        _integer(_value(reconstruction, 'Geometryresolution',
                        state.get('geometry_resolution', 384)), 384),
        str(_value(reconstruction, 'Preservegeometryaspect', True)),
        _integer(getattr(reconstruction_rgb, 'width',
                         state.get('geometry_resolution', 384)), 384),
        _integer(getattr(reconstruction_rgb, 'height',
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
        str(source_calibration[0] or ''),
        str(source_calibration[1] or ''),
        str(sensor_calibration[0] or ''),
        str(sensor_calibration[1] or ''),
        str(state.get('sensor_frame_calibration_id', '')),
        str(state.get('sensor_frame_calibration_digest', '')),
        str(state.get('source_camera_session_id', '')),
        str(state.get('source_generation_id', '')),
    ]
    for index in range(4):
        values.append(tuple(_vec4(
            _value(reconstruction, 'Cameratoworld%d' % index, ''),
            ((1, 0, 0, 0), (0, 1, 0, 0),
             (0, 0, 1, 0), (0, 0, 0, 1))[index])))
    sensor_comp = root_comp.op('WORKING_PIPELINE/SENSOR_INTERACTION')
    for index in range(4):
        values.append(tuple(_vec4(
            _value(sensor_comp, 'Sensortoworld%d' % index, ''),
            ((1, 0, 0, 0), (0, 1, 0, 0),
             (0, 0, 1, 0), (0, 0, 0, 1))[index])))
    # Configured local adapter/calibration identities are safe fingerprints;
    # no file is read and no private component is imported here.
    for key in ('mode', 'geometry_provider', 'streamdiffusion_tox',
                'replay_path', 'rgb_operator',
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
    lifecycle = runtime.get('frame_lifecycle', {})
    source_frame = lifecycle.get('source', {})
    sensor_frame = lifecycle.get('sensor', {})
    source_parameter_age = _number(_value(sources, 'Sourceagems', -1.0), -1.0)
    sensor_parameter_age = _number(_value(sensor, 'Sensoragems', -1.0), -1.0)
    source_age = _number(source_frame.get('age_ms', source_parameter_age), -1.0)
    sensor_age = _number(sensor_frame.get('age_ms', sensor_parameter_age), -1.0)
    # Metadata-less legacy adapters may publish age directly on the component.
    if (source_frame.get('metadata_mode') == 'legacy_each_cook' and
            source_parameter_age >= 0):
        source_age = source_parameter_age
    if (sensor_frame.get('metadata_mode') == 'legacy_each_cook' and
            sensor_parameter_age >= 0):
        sensor_age = sensor_parameter_age
    source_config = _mapping(state.get('source'))
    sensor_config = _mapping(state.get('sensor'))
    source_timeout = _number(source_config.get('stale_timeout_ms', 1000), 1000)
    sensor_timeout = _number(sensor_config.get('stale_timeout_ms', 1000), 1000)
    warnings = []
    if source_age >= 0 and source_age > source_timeout:
        warnings.append('source_stale')
    if sensor_age >= 0 and sensor_age > sensor_timeout:
        warnings.append('sensor_stale')
    if source_frame.get('decision') in (
            'future_rejected', 'retired_session_rejected',
            'out_of_order_rejected', 'cook_frame_regression_rejected',
            'metadata_rejected', 'remote_metadata_required',
            'transport_disconnected', 'stale_remote_unverified'):
        warnings.append('source_frame_rejected')
    if sensor_frame.get('decision') in (
            'future_rejected', 'retired_session_rejected',
            'out_of_order_rejected', 'cook_frame_regression_rejected',
            'metadata_rejected'):
        warnings.append('sensor_frame_rejected')
    if state.get('transport_error'):
        warnings.append('transport_error')
    for field, warning in (
        ('source_contract_error', 'source_contract_error'),
        ('sensor_contract_error', 'sensor_contract_error'),
        ('calibration_error', 'calibration_error'),
        ('source_calibration_error', 'source_calibration_error'),
        ('sensor_calibration_error', 'sensor_calibration_error'),
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
        'source_frame_id':source_frame.get(
            'frame_id', _integer(_value(sources, 'Frameid', -1), -1)),
        'sensor_frame_id':sensor_frame.get(
            'frame_id', _integer(_value(sensor, 'Sensorframeid', -1), -1)),
        'source_new_frame':bool(source_frame.get('new_frame', False)),
        'source_valid':bool(source_frame.get('valid', True)),
        'source_frame_decision':source_frame.get('decision', 'unknown'),
        'source_metadata_mode':source_frame.get('metadata_mode', 'unknown'),
        'sensor_frame_decision':sensor_frame.get('decision', 'unknown'),
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

def _calibration_targets(state, label):
    """Return the streams a calibration file owns.

    Explicit source and sensor calibration paths are independent. A single
    legacy path still owns both transforms, unless a dynamic source-camera
    metadata producer makes the source identity authoritative at runtime.
    """
    if label == 'shared':
        return ('source', 'sensor')
    if label not in ('source', 'sensor'):
        raise ValueError('unsupported calibration stream')
    source = _mapping(state.get('source'))
    sensor = _mapping(state.get('sensor'))
    if label == 'source':
        return (('source',) if sensor.get('calibration_path') else
                ('source', 'sensor'))
    owns_dynamic_source = bool(source.get('camera_metadata_operator')) and not bool(
        source.get('calibration_path'))
    if source.get('calibration_path') or owns_dynamic_source:
        return ('sensor',)
    return ('source', 'sensor')

def _calibration_identity(state, label):
    """Return one stream's identity, accepting the pre-split state contract."""
    identity_key = label + '_calibration_id'
    digest_key = label + '_calibration_digest'
    if identity_key in state or digest_key in state:
        return state.get(identity_key), state.get(digest_key)
    split_keys = (
        'source_calibration_id', 'source_calibration_digest',
        'sensor_calibration_id', 'sensor_calibration_digest',
    )
    if not any(key in state for key in split_keys):
        return state.get('calibration_id'), state.get('calibration_digest')
    return None, None

def _record_calibration_error(state, label, message):
    safe = str(message).strip()[:160] or 'calibration validation failed'
    try:
        targets = _calibration_targets(state, label)
    except Exception:
        targets = (label,) if label in ('source', 'sensor') else ('source', 'sensor')
    for target in targets:
        state[target + '_calibration_error'] = safe
    # Preserve the legacy aggregate error used by readiness and launchers.
    state['calibration_error'] = safe

def _load_calibration(root_comp, state, configured_path, label='shared'):
    path = _local_path(state, configured_path, '.json')
    if not path:
        _record_calibration_error(
            state, label, 'configured calibration is missing or invalid')
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
                   'camera_to_world', 'sensor_to_world', 'coordinate_system',
                   'calibration_digest'}
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
            basis = (matrix[0:3], matrix[4:7], matrix[8:11])
            for index, axis in enumerate(basis):
                length_squared = sum(component * component for component in axis)
                if abs(length_squared - 1.0) > TRANSFORM_TOLERANCE:
                    raise ValueError(
                        'transform spatial basis axis %d must have unit length' % index)
            for first, second in ((0, 1), (0, 2), (1, 2)):
                dot = sum(
                    basis[first][component] * basis[second][component]
                    for component in range(3))
                if abs(dot) > TRANSFORM_TOLERANCE:
                    raise ValueError('transform spatial basis must be orthonormal')
            determinant = (
                matrix[0] * (matrix[5] * matrix[10] - matrix[6] * matrix[9]) -
                matrix[1] * (matrix[4] * matrix[10] - matrix[6] * matrix[8]) +
                matrix[2] * (matrix[4] * matrix[9] - matrix[5] * matrix[8]))
            if (determinant <= 0.0 or
                    abs(determinant - 1.0) > TRANSFORM_TOLERANCE * 4):
                raise ValueError('transform must have a rigid right-handed spatial basis')
        # Bind the user-facing calibration_id to the validated semantic content.
        # File whitespace/key order and a supplied digest field are deliberately
        # excluded; normalized numeric values match commissioning.py.
        semantic = {
            'version':'flexgpu-calibration/v1',
            'calibration_id':calibration_id,
            'image':{'width':width, 'height':height},
            'intrinsics':{'fx':float(fx), 'fy':float(fy),
                          'cx':float(cx), 'cy':float(cy)},
            'depth':{'encoding':encoding, 'scale':float(scale),
                     'bias':float(bias), 'near_m':float(near_m),
                     'far_m':float(far_m)},
            'camera_to_world':[float(item) for item in camera],
            'sensor_to_world':[float(item) for item in sensor],
            'coordinate_system':'right_handed_y_up_metres',
        }
        canonical = json.dumps(
            semantic, sort_keys=True, separators=(',', ':'),
            ensure_ascii=False, allow_nan=False).encode('utf-8')
        calibration_digest = hashlib.sha256(canonical).hexdigest()
        supplied_digest = data.get('calibration_digest')
        if supplied_digest is not None:
            if (not isinstance(supplied_digest, str) or
                    SHA256_PATTERN.fullmatch(supplied_digest) is None or
                    supplied_digest != calibration_digest):
                raise ValueError('calibration digest does not match content')
        targets = _calibration_targets(state, label)
        for target in targets:
            existing_id, existing_digest = _calibration_identity(state, target)
            if existing_id and existing_id != calibration_id:
                raise ValueError(target + ' calibration id changed during apply')
            if existing_digest and existing_digest != calibration_digest:
                raise ValueError(target + ' calibration digest changed during apply')
        reconstruction = root_comp.op('WORKING_PIPELINE/RECONSTRUCTION')
        epoch = int(calibration_digest[:8], 16) % 2147483647
        sensor_comp = root_comp.op('WORKING_PIPELINE/SENSOR_INTERACTION')
        if 'source' in targets:
            _set(reconstruction, 'Depthmode', mode)
            _set(reconstruction, 'Depthscale', scale)
            _set(reconstruction, 'Depthbias', bias)
            _set(reconstruction, 'Nearmetres', near_m)
            _set(reconstruction, 'Farmetres', far_m)
            _set(reconstruction, 'Fxnormalized', fx / float(width))
            _set(reconstruction, 'Fynormalized', fy / float(height))
            _set(reconstruction, 'Cxnormalized', cx / float(width))
            _set(reconstruction, 'Cynormalized', cy / float(height))
            _set(reconstruction, 'Calibrationepoch', epoch)
            for index in range(4):
                _set(reconstruction, 'Cameratoworld%d' % index, ' '.join(
                    '%.12g' % item for item in camera[index * 4:index * 4 + 4]))
        if 'sensor' in targets:
            for index in range(4):
                _set(sensor_comp, 'Sensortoworld%d' % index, ' '.join(
                    '%.12g' % item for item in sensor[index * 4:index * 4 + 4]))
        for target in targets:
            state[target + '_calibration_id'] = calibration_id
            state[target + '_calibration_digest'] = calibration_digest
            state[target + '_calibration_status'] = 'ready'
            state.pop(target + '_calibration_error', None)
        if 'source' in targets:
            # Compatibility alias: the historic aggregate identity is the
            # image-derived reconstruction/source-camera identity.
            state['calibration_id'] = calibration_id
            state['calibration_digest'] = calibration_digest
            state['calibration_status'] = 'ready'
        return True
    except Exception:
        _record_calibration_error(state, label, 'calibration validation failed')
        return False

def _configure_source_adapter(root_comp, state, source):
    adapter = root_comp.op('WORKING_PIPELINE/SOURCES/STREAMDIFFUSION_ADAPTER')
    if adapter is None:
        state['source_adapter_error'] = 'source adapter component is missing'
        return False
    configured_geometry_provider = source.get('geometry_provider')
    geometry_provider = str(
        configured_geometry_provider or 'moge2').lower()
    if geometry_provider not in ('moge2', 'depth_anything'):
        state['source_adapter_error'] = 'geometry_provider is unsupported'
        return False
    if (not _set(adapter, 'Geometrysource', geometry_provider) and
            configured_geometry_provider is not None):
        state['source_adapter_error'] = (
            'generated geometry selector is missing; install the geometry bridge')
        return False
    state['source_geometry_provider'] = geometry_provider
    selected_bridge_name = (
        'DEPTH_ANYTHING_GEOMETRY_BRIDGE'
        if geometry_provider == 'depth_anything' else 'MOGE2_BRIDGE')
    selected_bridge = root_comp.op(
        'WORKING_PIPELINE/SOURCES/STREAMDIFFUSION_ADAPTER/' +
        selected_bridge_name)
    if selected_bridge is not None:
        _set(selected_bridge, 'Enabled', True)
        runtime_dat = selected_bridge.op('bridge_runtime')
        if runtime_dat is not None:
            try:
                runtime_dat.module.tick(selected_bridge)
                state['source_bridge_listener_status'] = 'started'
            except Exception:
                # The bridge frame callback retries once embedded modules have
                # finished compiling. External workers also wait for this
                # listener, closing the cold-start race without blocking TD.
                state['source_bridge_listener_status'] = 'pending_compile'
    else:
        state['source_bridge_listener_status'] = 'missing'
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
        if configured:
            resolved = _child_op(root_comp, search_root, configured,
                                 require_top=False)
            if resolved is None:
                state['source_adapter_error'] = field + ' could not be resolved'
                return False
            state['source_' + field + '_path'] = str(resolved.path)
    calibration_path = source.get('calibration_path')
    if calibration_path and not _load_calibration(
            root_comp, state, calibration_path, 'source'):
        return False
    state['source_adapter_status'] = 'ready'
    return True

def select_geometry_provider(root_comp=None, provider='moge2'):
    """Switch strict source lifecycle metadata with the visual provider.

    Generated RGB/depth TOP routing is controlled by SHOW_CONTROL, while
    strict frame freshness and camera metadata live in the runtime state.
    Update both halves atomically so a stopped previous provider cannot gate a
    fresh selected provider to zero inside TEMPORAL_WORLD.
    """
    root_comp = root_comp or op('/project1/flexgpu')
    if root_comp is None:
        return False
    selected = str(provider or 'moge2').strip().lower()
    if selected not in ('moge2', 'depth_anything'):
        return False
    runtime = _runtime(root_comp)
    if runtime is None:
        return False
    bridge_name = (
        'DEPTH_ANYTHING_GEOMETRY_BRIDGE'
        if selected == 'depth_anything' else 'MOGE2_BRIDGE')
    bridge = root_comp.op(
        'WORKING_PIPELINE/SOURCES/STREAMDIFFUSION_ADAPTER/' + bridge_name)
    if bridge is None:
        return False
    frame_state = bridge.op('FRAME_STATE')
    camera_metadata = bridge.op('CAMERA_METADATA')
    if frame_state is None or camera_metadata is None:
        return False

    state = runtime['state']
    source = _mapping(state.get('source'))
    source['geometry_provider'] = selected
    source['frame_state_operator'] = str(frame_state.path)
    source['camera_metadata_operator'] = str(camera_metadata.path)
    state['source'] = source
    state['source_geometry_provider'] = selected
    state['source_frame_state_operator_path'] = str(frame_state.path)
    state['source_camera_metadata_operator_path'] = str(camera_metadata.path)
    state['source_provider_switch_status'] = 'waiting_for_fresh_' + selected
    for key in (
        'source_camera_session_id', 'source_generation_id',
        'source_camera_metadata_status', 'source_camera_metadata_error',
    ):
        state.pop(key, None)
    runtime.setdefault('frame_lifecycle', {}).pop('source', None)
    runtime['source_session_id'] = None
    runtime.pop('source_camera_contract', None)
    runtime.pop('source_camera_rejected_identity', None)
    runtime.pop('source_camera_rejected_error', None)

    sources = root_comp.op('WORKING_PIPELINE/SOURCES')
    temporal = root_comp.op('WORKING_PIPELINE/TEMPORAL_WORLD')
    _set(sources, 'Newframe', False)
    _set(sources, 'Sourcevalid', False)
    _set(temporal, 'Newframe', False)
    _set(temporal, 'Sourcevalid', False)
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
        if configured:
            resolved = _child_op(root_comp, search_root, configured,
                                 require_top=False)
            if resolved is None:
                state['sensor_adapter_error'] = field + ' could not be resolved'
                return False
            state['sensor_' + field + '_path'] = str(resolved.path)
    calibration_path = sensor.get('calibration_path')
    if calibration_path and not _load_calibration(
            root_comp, state, calibration_path, 'sensor'):
        return False
    state['sensor_adapter_status'] = 'ready'
    return True

def _operator_mapping(node):
    """Read a small JSON/table/CHOP metadata operator without sampling imagery."""
    if node is None:
        raise ValueError('frame-state operator is unavailable')
    try:
        stored = node.fetch('_flexgpu_frame_state', None)
        if isinstance(stored, dict):
            return dict(stored)
    except Exception:
        pass
    try:
        text = str(node.text).strip()
        if text.startswith('{'):
            value = json.loads(text, object_pairs_hook=_strict_pairs,
                               parse_constant=_reject_json_constant)
            if isinstance(value, dict):
                return value
            raise ValueError('frame-state JSON must be an object')
        if text.startswith('['):
            raise ValueError('frame-state JSON must be an object')
    except AttributeError:
        pass
    except Exception:
        raise ValueError('frame-state JSON is invalid')
    try:
        rows = int(node.numRows)
        result = {}
        start = 1 if rows and str(node[0, 0]).strip().lower() in (
            'key', 'field', 'metric') else 0
        for row in range(start, rows):
            key = str(node[row, 0]).strip()
            if not key:
                continue
            if key in result:
                raise ValueError('duplicate frame-state field')
            result[key] = str(node[row, 1]).strip()
        if result:
            return result
    except ValueError:
        raise
    except Exception:
        pass
    try:
        result = {}
        for channel in node.chans():
            name = str(channel.name)
            if name in result:
                raise ValueError('duplicate frame-state channel')
            result[name] = channel.eval()
        if result:
            return result
    except ValueError:
        raise
    except Exception:
        pass
    raise ValueError('frame-state operator has no supported mapping')

def _strict_frame_integer(value, field, low, high):
    if isinstance(value, bool):
        raise ValueError(field + ' must be an integer')
    if isinstance(value, str):
        if re.fullmatch(r'0|[1-9][0-9]*', value.strip()) is None:
            raise ValueError(field + ' must be a decimal integer')
        parsed = int(value.strip())
    elif isinstance(value, int):
        parsed = value
    else:
        raise ValueError(field + ' must be an integer')
    if parsed < low or parsed > high:
        raise ValueError(field + ' is outside the supported range')
    return parsed

def _validate_frame_state(value, state, label='source'):
    if not isinstance(value, dict):
        raise ValueError('frame state must be an object')
    allowed = {'version', 'session_id', 'frame_id', 'timestamp_ns',
               'width', 'height', 'calibration_id', 'calibration_digest',
               'valid_fraction', 'confidence_mean'}
    if set(value).difference(allowed):
        raise ValueError('frame state contains an unsupported field')
    if value.get('version') != FRAME_STATE_VERSION:
        raise ValueError('unsupported frame-state version')
    required = allowed
    missing = sorted(required.difference(value))
    if missing:
        raise ValueError('frame state is missing ' + missing[0])
    session_id = value.get('session_id')
    calibration_id = value.get('calibration_id')
    for field, identifier in (('session_id', session_id),
                              ('calibration_id', calibration_id)):
        if (not isinstance(identifier, str) or len(identifier) > 64 or
                IDENTIFIER_PATTERN.fullmatch(identifier) is None):
            raise ValueError(field + ' must be a conservative identifier')
    digest = value.get('calibration_digest')
    if not isinstance(digest, str) or SHA256_PATTERN.fullmatch(digest) is None:
        raise ValueError('calibration_digest must be lowercase SHA-256')
    if label not in ('source', 'sensor'):
        raise ValueError('frame state has an unsupported stream label')
    expected_id, expected_digest = _calibration_identity(state, label)
    source_config = _mapping(state.get('source'))
    dynamic_camera_identity = (
        label == 'source' and bool(source_config.get('camera_metadata_operator')) and
        not bool(source_config.get('calibration_path')))
    if expected_id and not dynamic_camera_identity and calibration_id != expected_id:
        raise ValueError('frame calibration_id does not match loaded calibration')
    if (expected_digest and not dynamic_camera_identity and
            digest != expected_digest):
        raise ValueError('frame calibration_digest does not match loaded calibration')
    valid_fraction = _finite(value.get('valid_fraction'), 'valid_fraction')
    confidence_mean = _finite(value.get('confidence_mean'), 'confidence_mean')
    if not 0.0 <= valid_fraction <= 1.0:
        raise ValueError('valid_fraction must be between zero and one')
    if not 0.0 <= confidence_mean <= 1.0:
        raise ValueError('confidence_mean must be between zero and one')
    return {
        'version':FRAME_STATE_VERSION,
        'session_id':session_id,
        'frame_id':_strict_frame_integer(
            value.get('frame_id'), 'frame_id', 0, 2 ** 63 - 1),
        'timestamp_ns':_strict_frame_integer(
            value.get('timestamp_ns'), 'timestamp_ns', 1, 2 ** 63 - 1),
        'width':_strict_frame_integer(value.get('width'), 'width', 1, 16384),
        'height':_strict_frame_integer(value.get('height'), 'height', 1, 16384),
        'calibration_id':calibration_id,
        'calibration_digest':digest,
        'valid_fraction':valid_fraction,
        'confidence_mean':confidence_mean,
    }

def _camera_metadata_sequence(value, field, length):
    if isinstance(value, str):
        try:
            value = json.loads(value, parse_constant=_reject_json_constant)
        except Exception:
            raise ValueError(field + ' must be a JSON array')
    if not isinstance(value, (list, tuple)) or len(value) != length:
        raise ValueError('%s must contain exactly %d numbers' % (field, length))
    return tuple(_finite(item, field) for item in value)

def _validate_rigid_camera_matrix(value):
    matrix = _camera_metadata_sequence(value, 'camera_to_world', 16)
    if (abs(matrix[12]) > 1e-6 or abs(matrix[13]) > 1e-6 or
            abs(matrix[14]) > 1e-6 or abs(matrix[15] - 1.0) > 1e-6):
        raise ValueError('camera_to_world must be homogeneous row-major')
    basis = (matrix[0:3], matrix[4:7], matrix[8:11])
    for index, axis in enumerate(basis):
        length_squared = sum(component * component for component in axis)
        if abs(length_squared - 1.0) > TRANSFORM_TOLERANCE:
            raise ValueError(
                'camera_to_world spatial basis axis %d must have unit length' % index)
    for first, second in ((0, 1), (0, 2), (1, 2)):
        dot = sum(basis[first][component] * basis[second][component]
                  for component in range(3))
        if abs(dot) > TRANSFORM_TOLERANCE:
            raise ValueError('camera_to_world spatial basis must be orthonormal')
    determinant = (
        matrix[0] * (matrix[5] * matrix[10] - matrix[6] * matrix[9]) -
        matrix[1] * (matrix[4] * matrix[10] - matrix[6] * matrix[8]) +
        matrix[2] * (matrix[4] * matrix[9] - matrix[5] * matrix[8]))
    if (determinant <= 0.0 or
            abs(determinant - 1.0) > TRANSFORM_TOLERANCE * 4):
        raise ValueError(
            'camera_to_world must have a rigid right-handed spatial basis')
    return matrix

def _validate_camera_metadata(value, source_frame):
    if not isinstance(value, dict):
        raise ValueError('camera metadata must be an object')
    required = {
        'version', 'session_id', 'frame_id', 'timestamp_ns', 'width', 'height',
        'generation_id', 'intrinsics_pixels', 'depth_scale_bias',
        'camera_to_world', 'near_metres', 'far_metres', 'calibration_id',
        'calibration_digest',
    }
    actual = set(value)
    missing = sorted(required.difference(actual))
    if missing:
        raise ValueError('camera metadata is missing ' + missing[0])
    if actual.difference(required):
        raise ValueError('camera metadata contains an unsupported field')
    if value.get('version') != CAMERA_METADATA_VERSION:
        raise ValueError('unsupported camera-metadata version')

    session_id = value.get('session_id')
    calibration_id = value.get('calibration_id')
    for field, identifier in (('session_id', session_id),
                              ('calibration_id', calibration_id)):
        if (not isinstance(identifier, str) or len(identifier) > 64 or
                IDENTIFIER_PATTERN.fullmatch(identifier) is None):
            raise ValueError(field + ' must be a conservative identifier')
    generation_id = value.get('generation_id')
    if (not isinstance(generation_id, str) or not generation_id or
            len(generation_id.encode('utf-8')) > 256 or
            any(ord(character) < 32 for character in generation_id)):
        raise ValueError('generation_id must be a bounded non-empty string')
    digest = value.get('calibration_digest')
    if not isinstance(digest, str) or SHA256_PATTERN.fullmatch(digest) is None:
        raise ValueError('calibration_digest must be lowercase SHA-256')

    metadata = {
        'version':CAMERA_METADATA_VERSION,
        'session_id':session_id,
        'frame_id':_strict_frame_integer(
            value.get('frame_id'), 'frame_id', 0, 2 ** 63 - 1),
        'timestamp_ns':_strict_frame_integer(
            value.get('timestamp_ns'), 'timestamp_ns', 1, 2 ** 63 - 1),
        'width':_strict_frame_integer(value.get('width'), 'width', 1, 16384),
        'height':_strict_frame_integer(value.get('height'), 'height', 1, 16384),
        'generation_id':generation_id,
        'calibration_id':calibration_id,
        'calibration_digest':digest,
    }
    intrinsics = _camera_metadata_sequence(
        value.get('intrinsics_pixels'), 'intrinsics_pixels', 4)
    fx, fy, cx, cy = intrinsics
    width = metadata['width']
    height = metadata['height']
    if fx <= 0.0 or fy <= 0.0:
        raise ValueError('intrinsics focal lengths must be greater than zero')
    if fx > width * 100.0 or fy > height * 100.0:
        raise ValueError('intrinsics focal lengths are outside supported bounds')
    if not (-width <= cx <= width * 2 and -height <= cy <= height * 2):
        raise ValueError('intrinsics principal point is outside supported bounds')
    depth = _camera_metadata_sequence(
        value.get('depth_scale_bias'), 'depth_scale_bias', 2)
    if depth[0] <= 0.0 or depth[0] > 1000.0:
        raise ValueError('depth scale must be greater than zero and at most 1000')
    if depth[1] < 0.0 or depth[1] > 1000.0:
        raise ValueError('depth bias must be between zero and 1000')
    near_metres = _finite(value.get('near_metres'), 'near_metres')
    far_metres = _finite(value.get('far_metres'), 'far_metres')
    if (near_metres <= 0.0 or far_metres <= near_metres or
            far_metres > 1000.0):
        raise ValueError('camera metadata contains an invalid depth range')
    camera = _validate_rigid_camera_matrix(value.get('camera_to_world'))
    metadata.update({
        'intrinsics_pixels':intrinsics,
        'depth_scale_bias':depth,
        'camera_to_world':camera,
        'near_metres':near_metres,
        'far_metres':far_metres,
    })

    if not isinstance(source_frame, dict):
        raise ValueError('new source frame is unavailable')
    for field in ('session_id', 'frame_id', 'timestamp_ns', 'width', 'height'):
        if source_frame.get(field) != metadata[field]:
            raise ValueError('camera metadata %s does not match accepted frame' % field)
    for field in ('calibration_id', 'calibration_digest'):
        if source_frame.get(field) != metadata[field]:
            raise ValueError('camera metadata %s does not match frame state' % field)
    return metadata

def _camera_calibration_signature(metadata):
    return (
        metadata['width'], metadata['height'],
        tuple(metadata['intrinsics_pixels']), tuple(metadata['depth_scale_bias']),
        tuple(metadata['camera_to_world']), metadata['near_metres'],
        metadata['far_metres'], metadata['calibration_id'],
        metadata['calibration_digest'],
    )

def _camera_metadata_node(root_comp, state):
    path = state.get('source_camera_metadata_operator_path')
    if not path:
        path = _mapping(state.get('source')).get('camera_metadata_operator')
    if not path:
        return None
    try:
        return root_comp.op(str(path))
    except Exception:
        return None

def _camera_frame_identity(source_frame):
    return tuple(source_frame.get(field) for field in (
        'session_id', 'frame_id', 'timestamp_ns', 'width', 'height'))

def _reject_source_camera_metadata(root_comp, runtime, source_frame, error):
    message = str(error).strip()[:160] or 'camera metadata was rejected'
    state = runtime['state']
    state['source_camera_metadata_status'] = 'rejected'
    state['source_camera_metadata_error'] = message
    runtime['source_camera_rejected_identity'] = _camera_frame_identity(source_frame)
    runtime['source_camera_rejected_error'] = message
    source_frame.update({
        'new_frame':False, 'valid':False,
        'decision':'camera_metadata_rejected', 'error':message,
    })
    sources = root_comp.op('WORKING_PIPELINE/SOURCES')
    temporal = root_comp.op('WORKING_PIPELINE/TEMPORAL_WORLD')
    bridge = root_comp.op('WORKING_PIPELINE/ROLE_BRIDGE')
    _set(sources, 'Newframe', False)
    _set(sources, 'Sourcevalid', False)
    _set(temporal, 'Newframe', False)
    _set(temporal, 'Sourcevalid', False)
    _set(bridge, 'Framevalid', False)
    state['source_frame_decision'] = 'camera_metadata_rejected'
    return False

def _apply_camera_metadata_contract(root_comp, runtime, metadata):
    state = runtime['state']
    reconstruction = root_comp.op('WORKING_PIPELINE/RECONSTRUCTION')
    width = float(metadata['width'])
    height = float(metadata['height'])
    fx, fy, cx, cy = metadata['intrinsics_pixels']
    _set(reconstruction, 'Depthmode', 'metric')
    # The bridge unpacker has already converted packed uint16 depth to metres.
    # Generated scenes can nevertheless infer tens of metres of depth while
    # the audience sensor occupies a room-scale volume. Keep raw metadata as
    # the default, but allow an explicit installation calibration to map both
    # sources into the same metre-scale interaction world.
    provider = str(_mapping(state.get('source')).get(
        'geometry_provider', state.get(
            'source_geometry_provider', 'moge2'))).strip().lower()
    if provider == 'depth_anything':
        installation_override = bool(_value(
            reconstruction, 'Depthanythingdepthoverride', False))
        scale_parameter = 'Depthanythingdepthscale'
        bias_parameter = 'Depthanythingdepthbias'
        near_parameter = 'Depthanythingnear'
        far_parameter = 'Depthanythingfar'
    else:
        installation_override = bool(
            _value(reconstruction, 'Installationdepthoverride', False))
        scale_parameter = 'Installationdepthscale'
        bias_parameter = 'Installationdepthbias'
        near_parameter = 'Installationnear'
        far_parameter = 'Installationfar'
    if installation_override:
        depth_scale = max(1.0e-6, _number(
            _value(reconstruction, scale_parameter, 1.0), 1.0))
        depth_bias = _number(
            _value(reconstruction, bias_parameter, 0.0), 0.0)
        near_metres = max(1.0e-4, _number(
            _value(reconstruction, near_parameter, metadata['near_metres']),
            metadata['near_metres']))
        far_metres = max(near_metres + 1.0e-4, _number(
            _value(reconstruction, far_parameter, metadata['far_metres']),
            metadata['far_metres']))
    else:
        depth_scale = 1.0
        depth_bias = 0.0
        near_metres = metadata['near_metres']
        far_metres = metadata['far_metres']
    _set(reconstruction, 'Depthscale', depth_scale)
    _set(reconstruction, 'Depthbias', depth_bias)
    _set(reconstruction, 'Nearmetres', near_metres)
    _set(reconstruction, 'Farmetres', far_metres)
    _set(reconstruction, 'Fxnormalized', fx / width)
    _set(reconstruction, 'Fynormalized', fy / height)
    _set(reconstruction, 'Cxnormalized', cx / width)
    _set(reconstruction, 'Cynormalized', cy / height)
    camera = metadata['camera_to_world']
    for index in range(4):
        _set(reconstruction, 'Cameratoworld%d' % index, ' '.join(
            '%.12g' % item for item in camera[index * 4:index * 4 + 4]))
    epoch = int(metadata['calibration_digest'][:8], 16) % 2147483647
    _set(reconstruction, 'Calibrationepoch', epoch)
    state['source_calibration_id'] = metadata['calibration_id']
    state['source_calibration_digest'] = metadata['calibration_digest']
    state['source_calibration_status'] = 'ready'
    state.pop('source_calibration_error', None)
    # Compatibility alias for callers and saved projects that predate the
    # source/sensor identity split. It always describes reconstruction input.
    state['calibration_id'] = metadata['calibration_id']
    state['calibration_digest'] = metadata['calibration_digest']
    state['calibration_status'] = 'ready'
    state['source_camera_session_id'] = metadata['session_id']
    state['source_generation_id'] = metadata['generation_id']
    state['source_depth_scale_bias'] = list(metadata['depth_scale_bias'])
    state['source_installation_depth_override'] = installation_override
    state['source_depth_override_provider'] = provider
    state['source_camera_metadata_status'] = 'accepted'
    state.pop('source_camera_metadata_error', None)
    runtime['source_camera_contract'] = dict(metadata)
    runtime.pop('source_camera_rejected_identity', None)
    runtime.pop('source_camera_rejected_error', None)

def _sample_source_camera_metadata(root_comp, runtime):
    state = runtime['state']
    source_config = _mapping(state.get('source'))
    if not source_config.get('camera_metadata_operator'):
        return False
    source_frame = _lifecycle_slot(runtime, 'source')
    identity = _camera_frame_identity(source_frame)
    if not source_frame.get('new_frame'):
        if runtime.get('source_camera_rejected_identity') == identity:
            return _reject_source_camera_metadata(
                root_comp, runtime, source_frame,
                runtime.get('source_camera_rejected_error',
                            'camera metadata was rejected'))
        previous = runtime.get('source_camera_contract')
        if (isinstance(previous, dict) and
                _camera_frame_identity(previous) == identity):
            state['source_camera_metadata_status'] = 'held'
            state.pop('source_camera_metadata_error', None)
        else:
            state['source_camera_metadata_status'] = 'waiting_for_new_frame'
        return False
    if not source_frame.get('valid'):
        return _reject_source_camera_metadata(
            root_comp, runtime, source_frame,
            'camera metadata requires a valid newly accepted source frame')
    if source_frame.get('metadata_mode') != 'explicit':
        return _reject_source_camera_metadata(
            root_comp, runtime, source_frame,
            'camera metadata requires explicit source frame state')
    try:
        metadata = _validate_camera_metadata(
            _operator_mapping(_camera_metadata_node(root_comp, state)), source_frame)
        previous = runtime.get('source_camera_contract')
        if (isinstance(previous, dict) and
                previous.get('session_id') == metadata['session_id'] and
                _camera_calibration_signature(previous) !=
                _camera_calibration_signature(metadata)):
            raise ValueError(
                'camera calibration drift is forbidden within a source session')
        static_identity = bool(source_config.get('calibration_path'))
        if static_identity:
            expected_id, expected_digest = _calibration_identity(state, 'source')
            if expected_id and metadata['calibration_id'] != expected_id:
                raise ValueError(
                    'camera metadata does not match file calibration_id')
            if (expected_digest and
                    metadata['calibration_digest'] != expected_digest):
                raise ValueError(
                    'camera metadata does not match file calibration_digest')
        _apply_camera_metadata_contract(root_comp, runtime, metadata)
        source_frame['generation_id'] = metadata['generation_id']
        source_frame.pop('error', None)
        return True
    except Exception as exc:
        return _reject_source_camera_metadata(
            root_comp, runtime, source_frame, exc)

def _lifecycle_slot(runtime, label):
    lifecycle = runtime.setdefault('frame_lifecycle', {})
    return lifecycle.setdefault(label, {
        'session_id':None, 'frame_id':None, 'timestamp_ns':None,
        'arrival_monotonic':None, 'retired_sessions':[],
        'new_frame':False, 'valid':True, 'age_ms':-1.0,
        'accepted_count':0, 'decision':'initializing',
        'metadata_mode':'legacy_each_cook',
    })

def _accept_explicit_frame(runtime, label, metadata, now_ns, now_monotonic,
                           stale_timeout_ms):
    slot = _lifecycle_slot(runtime, label)
    session_id = metadata['session_id']
    frame_id = metadata['frame_id']
    timestamp_ns = metadata['timestamp_ns']
    age_ms = (now_ns - timestamp_ns) / 1000000.0
    if age_ms < -100.0:
        slot.update({'new_frame':False, 'valid':False, 'age_ms':age_ms,
                     'decision':'future_rejected', 'metadata_mode':'explicit'})
        return slot
    if session_id in slot['retired_sessions']:
        slot.update({'new_frame':False, 'valid':False, 'age_ms':age_ms,
                     'decision':'retired_session_rejected',
                     'metadata_mode':'explicit'})
        return slot
    previous_session = slot.get('session_id')
    session_changed = previous_session not in (None, session_id)
    if label == 'sensor' and previous_session == session_id:
        locked_identity = (
            slot.get('calibration_id'), slot.get('calibration_digest'))
        incoming_identity = (
            metadata.get('calibration_id'), metadata.get('calibration_digest'))
        if (all(locked_identity) and incoming_identity != locked_identity):
            slot.update({
                'new_frame':False, 'valid':False, 'age_ms':age_ms,
                'decision':'calibration_drift_rejected',
                'metadata_mode':'explicit',
                'error':('sensor calibration identity changed within the '
                         'producer session'),
            })
            return slot
    if session_changed:
        retired = list(slot.get('retired_sessions', []))
        retired.append(previous_session)
        slot['retired_sessions'] = retired[-8:]
    if not session_changed and previous_session == session_id:
        previous_id = slot.get('frame_id')
        previous_timestamp = slot.get('timestamp_ns')
        if (previous_id is not None and
                (frame_id < previous_id or timestamp_ns < previous_timestamp or
                 ((frame_id == previous_id) !=
                  (timestamp_ns == previous_timestamp)))):
            slot.update({'new_frame':False, 'valid':False, 'age_ms':age_ms,
                         'decision':'out_of_order_rejected',
                         'metadata_mode':'explicit'})
            return slot
        if frame_id == previous_id and timestamp_ns == previous_timestamp:
            slot.update({'new_frame':False,
                         'valid':age_ms <= stale_timeout_ms,
                         'age_ms':age_ms,
                         'decision':('held' if age_ms <= stale_timeout_ms
                                     else 'stale'),
                         'metadata_mode':'explicit'})
            slot.pop('error', None)
            return slot
    slot.update(metadata)
    slot['accepted_count'] = int(slot.get('accepted_count', 0)) + 1
    slot.update({'new_frame':True, 'valid':age_ms <= stale_timeout_ms,
                  'age_ms':age_ms, 'arrival_monotonic':now_monotonic,
                  'decision':('new_session' if session_changed else 'accepted'),
                  'metadata_mode':'explicit'})
    slot.pop('error', None)
    return slot

def _operator_cook_token(node):
    if node is None:
        return None
    for name in ('cookAbsFrame', 'cookFrame', 'numCooks'):
        try:
            value = getattr(node, name)
            if value is not None:
                return int(value)
        except Exception:
            pass
    return None

def _accept_fallback_frame(runtime, label, token, now_monotonic,
                           stale_timeout_ms, metadata_mode=None,
                           session_id='fallback-local'):
    slot = _lifecycle_slot(runtime, label)
    if token is None:
        token = int(slot.get('fallback_counter', -1)) + 1
        slot['fallback_counter'] = token
        mode = metadata_mode or 'legacy_each_cook'
    else:
        mode = metadata_mode or 'operator_cook_frame'
    previous = slot.get('fallback_token')
    if previous is not None and token < previous:
        slot.update({'new_frame':False, 'valid':False,
                     'decision':'cook_frame_regression_rejected',
                     'metadata_mode':mode})
        return slot
    new_frame = previous != token
    if new_frame:
        slot['fallback_token'] = token
        slot['arrival_monotonic'] = now_monotonic
        slot['accepted_count'] = int(slot.get('accepted_count', 0)) + 1
    arrival = slot.get('arrival_monotonic')
    age_ms = -1.0 if arrival is None else (now_monotonic - arrival) * 1000.0
    valid = age_ms < 0 or age_ms <= stale_timeout_ms
    slot.update({'session_id':session_id, 'frame_id':token,
                 'new_frame':new_frame, 'valid':valid, 'age_ms':age_ms,
                 'decision':('accepted_fallback' if new_frame else
                             'held_fallback' if valid else 'stale_fallback'),
                 'metadata_mode':mode})
    return slot

def _channel_integer(node, name):
    if node is None:
        return None
    try:
        channel = node[name]
    except Exception:
        return None
    for candidate in (
            lambda: channel.eval(),
            lambda: channel[0],
            lambda: channel):
        try:
            value = candidate()
            if isinstance(value, bool):
                return None
            parsed = int(value)
            if float(value) != parsed:
                return None
            return parsed
        except (TypeError, ValueError, OverflowError, IndexError):
            pass
        except Exception:
            pass
    return None

def _receiver_frame_token(root_comp, state):
    """Return only a producer-observable transport counter.

    Touch In exposes ``num_received_frames`` through its Info CHOP. Shared Mem
    In exposes no producer frame counter, so its metadata-less direct path must
    fail closed instead of treating the receiver's local cook frame as new.
    """
    if state.get('transport_endpoint_active') != 'RX_TCP_ATLAS':
        return None, 'explicit_metadata_required'
    info = root_comp.op('WORKING_PIPELINE/ROLE_BRIDGE/RX_TCP_ATLAS_INFO')
    connected = _channel_integer(info, 'connected')
    if connected is not None and connected <= 0:
        return None, 'transport_disconnected'
    received_count = _channel_integer(info, 'num_received_frames')
    if received_count is not None and received_count > 0:
        return received_count, 'transport_receive_counter'
    return None, 'transport_counter_unavailable'

def _accept_unverified_remote_frame(runtime, label, now_monotonic,
                                    stale_timeout_ms, reason):
    """Hold/fail closed when remote producer freshness is not observable."""
    slot = _lifecycle_slot(runtime, label)
    observed = slot.get('remote_observed_monotonic')
    if observed is None:
        observed = now_monotonic
        slot['remote_observed_monotonic'] = observed
    accepted = slot.get('arrival_monotonic')
    reference = accepted if accepted is not None else observed
    age_ms = max(0.0, (now_monotonic - reference) * 1000.0)
    previously_accepted = int(slot.get('accepted_count', 0)) > 0
    still_fresh = previously_accepted and age_ms <= stale_timeout_ms
    if reason == 'transport_disconnected':
        slot['transport_disconnected'] = True
        slot['transport_connected'] = False
    if age_ms > stale_timeout_ms:
        decision = 'stale_remote_unverified'
    elif previously_accepted:
        decision = reason
    else:
        decision = (reason if reason == 'transport_disconnected' else
                    'remote_metadata_required')
    slot.update({
        'session_id':'transport-receiver',
        'frame_id':slot.get('frame_id', -1),
        'new_frame':False,
        'valid':still_fresh,
        'age_ms':age_ms,
        'decision':decision,
        'metadata_mode':'remote_requires_explicit',
        'error':('configure source.frame_state_operator; Shared Mem In and '
                 'receiver cook frames do not prove producer freshness'),
    })
    return slot

def _accept_receiver_counter_frame(runtime, label, token, now_monotonic,
                                   stale_timeout_ms):
    slot = _lifecycle_slot(runtime, label)
    previous_token = slot.get('fallback_token')
    counter_restarted = (previous_token is not None and token < previous_token)
    reconnected = (bool(slot.pop('transport_disconnected', False)) or
                   counter_restarted)
    serial = int(slot.get('transport_session_serial', 0))
    if reconnected:
        serial += 1
        slot.pop('fallback_token', None)
        slot.pop('arrival_monotonic', None)
    slot['transport_session_serial'] = serial
    result = _accept_fallback_frame(
        runtime, label, token, now_monotonic, stale_timeout_ms,
        metadata_mode='transport_receive_counter',
        session_id='transport-receiver-%d' % serial)
    result['transport_connected'] = True
    if reconnected and result.get('new_frame'):
        result['decision'] = 'new_transport_session'
    return result

def _accept_inactive_frame(runtime, label, mode):
    slot = _lifecycle_slot(runtime, label)
    slot.clear()
    slot.update({
        'session_id':None,
        'frame_id':-1,
        'timestamp_ns':None,
        'new_frame':False,
        'valid':False,
        'age_ms':-1.0,
        'accepted_count':0,
        'decision':mode,
        'metadata_mode':mode,
    })
    return slot

def _frame_state_node(root_comp, state, label):
    path = state.get(label + '_frame_state_operator_path')
    if not path:
        config = _mapping(state.get(label))
        path = config.get('frame_state_operator')
    if not path:
        return None
    try:
        return root_comp.op(str(path))
    except Exception:
        return None

def _sensor_route_mode(state, frame_valid=None):
    """Gate strict sensor routes until their explicit frame state is valid."""
    if 'sensor' not in state:
        return None
    active = str(state.get('sensor_mode_active', 'simulated')).lower()
    configured = bool(_mapping(state.get('sensor')).get('frame_state_operator'))
    strict = configured and active in ('depth_sensor', 'replay')
    if strict and frame_valid is not True:
        return 'disabled'
    return active if active != 'inactive' else 'disabled'

def _publish_sensor_frame_identity(state, sensor_frame):
    """Expose only an accepted session lock, never an untrusted drift value."""
    keys = ('sensor_frame_session_id', 'sensor_frame_calibration_id',
            'sensor_frame_calibration_digest')
    has_lock = (
        sensor_frame.get('metadata_mode') == 'explicit' and
        int(sensor_frame.get('accepted_count', 0)) > 0 and
        bool(sensor_frame.get('session_id')) and
        bool(sensor_frame.get('calibration_id')) and
        bool(sensor_frame.get('calibration_digest')))
    if not has_lock:
        for key in keys:
            state.pop(key, None)
        return
    state['sensor_frame_session_id'] = sensor_frame['session_id']
    state['sensor_frame_calibration_id'] = sensor_frame['calibration_id']
    state['sensor_frame_calibration_digest'] = sensor_frame['calibration_digest']

def _fallback_frame_node(root_comp, state, label):
    if label == 'source':
        if state.get('transport_receiver_active'):
            endpoint = state.get('transport_endpoint_active')
            if endpoint:
                return root_comp.op('WORKING_PIPELINE/ROLE_BRIDGE/' + endpoint)
        return root_comp.op('WORKING_PIPELINE/SOURCES/RGB_SOURCE')
    mode = state.get('sensor_mode_active', 'simulated')
    names = {'depth_sensor':'DEPTH_SENSOR_ADAPTER',
             'replay':'REPLAY_SENSOR_ADAPTER'}
    name = names.get(mode, 'SIMULATED_SENSOR_MASK')
    return root_comp.op('WORKING_PIPELINE/SENSOR_INTERACTION/' + name)

def _sample_frame_lifecycle(root_comp, runtime, now_ns, now_monotonic):
    state = runtime['state']
    results = {}
    for label in ('source', 'sensor'):
        config = _mapping(state.get(label))
        timeout = _number(config.get('stale_timeout_ms', 1000), 1000)
        timeout = min(600000.0, max(1.0, timeout))
        configured = bool(config.get('frame_state_operator'))
        inactive_mode = (state.get('sensor_mode_active')
                         if label == 'sensor' else None)
        if inactive_mode in ('disabled', 'inactive'):
            result = _accept_inactive_frame(runtime, label, inactive_mode)
        elif configured:
            try:
                metadata = _validate_frame_state(
                    _operator_mapping(_frame_state_node(root_comp, state, label)),
                    state, label)
                result = _accept_explicit_frame(
                    runtime, label, metadata, now_ns, now_monotonic, timeout)
            except Exception as exc:
                result = _lifecycle_slot(runtime, label)
                result.update({'new_frame':False, 'valid':False,
                               'decision':'metadata_rejected',
                               'metadata_mode':'explicit',
                               'error':str(exc)[:160]})
        elif label == 'source' and state.get('transport_receiver_active'):
            token, receiver_status = _receiver_frame_token(root_comp, state)
            if token is None:
                result = _accept_unverified_remote_frame(
                    runtime, label, now_monotonic, timeout, receiver_status)
            else:
                result = _accept_receiver_counter_frame(
                    runtime, label, token, now_monotonic, timeout)
        else:
            node = _fallback_frame_node(root_comp, state, label)
            result = _accept_fallback_frame(
                runtime, label, _operator_cook_token(node),
                now_monotonic, timeout)
        results[label] = result
    source = results['source']
    sensor = results['sensor']
    _publish_sensor_frame_identity(state, sensor)
    sources_comp = root_comp.op('WORKING_PIPELINE/SOURCES')
    sensor_comp = root_comp.op('WORKING_PIPELINE/SENSOR_INTERACTION')
    sensor_route = _sensor_route_mode(state, bool(sensor.get('valid', False)))
    if sensor_route is not None:
        _set(sensor_comp, 'Mode', sensor_route)
        state['sensor_route_active'] = sensor_route
    temporal = root_comp.op('WORKING_PIPELINE/TEMPORAL_WORLD')
    _set(sources_comp, 'Newframe', source['new_frame'])
    _set(sources_comp, 'Sourcevalid', source['valid'])
    _set(sources_comp, 'Frameid', source.get('frame_id', -1))
    _set(sources_comp, 'Sourceagems', source.get('age_ms', -1.0))
    timestamp_ns = source.get('timestamp_ns')
    _set(sources_comp, 'Frametimestampseconds',
         -1.0 if timestamp_ns is None else timestamp_ns / 1000000000.0)
    previous_session = runtime.get('source_session_id')
    session_id = source.get('session_id')
    tracked_session = source.get('metadata_mode') in (
        'explicit', 'transport_receive_counter')
    if tracked_session and session_id and session_id != previous_session:
        runtime['source_session_id'] = session_id
        runtime['source_session_serial'] = int(
            runtime.get('source_session_serial', -1)) + 1
    if tracked_session:
        _set(sources_comp, 'Sessionepoch', runtime.get('source_session_serial', 0))
    _set(sensor_comp, 'Sensorframeid', sensor.get('frame_id', -1))
    _set(sensor_comp, 'Sensoragems', sensor.get('age_ms', -1.0))
    _set(temporal, 'Newframe', source['new_frame'])
    _set(temporal, 'Sourcevalid', source['valid'])
    bridge = root_comp.op('WORKING_PIPELINE/ROLE_BRIDGE')
    _set(bridge, 'Framesessionid', source.get('session_id') or '')
    _set(bridge, 'Frameid', source.get('frame_id', -1))
    _set(bridge, 'Frametimestampns', str(source.get('timestamp_ns', -1)))
    _set(bridge, 'Calibrationid', source.get(
        'calibration_id', state.get(
            'source_calibration_id', state.get('calibration_id', ''))))
    _set(bridge, 'Calibrationdigest', source.get(
        'calibration_digest', state.get(
            'source_calibration_digest', state.get('calibration_digest', ''))))
    _set(bridge, 'Framevalid', source['valid'])
    state['source_frame_decision'] = source['decision']
    state['source_metadata_mode'] = source['metadata_mode']
    state['sensor_frame_decision'] = sensor['decision']
    state['sensor_metadata_mode'] = sensor['metadata_mode']
    return results

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
                    not _load_calibration(
                        root_comp, state, calibration_path, 'source')):
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
                'source_calibration_error') or state.get(
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
                 'WORKING_PIPELINE/SENSOR_INTERACTION/SIMULATED_SENSOR_CONFIDENCE',
                 'WORKING_PIPELINE/SENSOR_INTERACTION/REPLAY_SENSOR_CONFIDENCE',
                 'WORKING_PIPELINE/TEMPORAL_WORLD/STATE_SEED'):
        _set_resolution(root_comp.op(path), state['geometry_resolution'],
                        state['geometry_resolution'])

    reconstruction = root_comp.op('WORKING_PIPELINE/RECONSTRUCTION')
    _set(reconstruction, 'Geometryresolution', state['geometry_resolution'])
    render = root_comp.op('WORKING_PIPELINE/POINT_RENDER')
    _set(render, 'Maxpoints', state['point_budget'])
    _set(render, 'Pointsize', state['point_size_px'])
    _set(render, 'Pointkeep', state['point_keep_fraction'])
    _set(render, 'Surfacefovdegrees', state['surface_fov_degrees'])
    _set(render, 'Wrapyawdegrees', state['triple_wrap_yaw_degrees'])
    _set(render, 'Artisticyawdegrees', state['triple_artistic_yaw_degrees'])
    _set(render, 'Artisticoffsetmetres',
         state['triple_artistic_offset_metres'])
    _set(pipeline, 'Displaymode', state['display_mode'])
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
        _set(root_comp.op('WORKING_PIPELINE/TRIPLE_DISPLAY'),
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
    _set_resolution(root_comp.op('WORKING_PIPELINE/POINT_RENDER/METRIC_RENDER_CENTER'), width, height)
    _set_resolution(root_comp.op('WORKING_PIPELINE/POINT_RENDER/METRIC_MONO_FALLBACK'), width, height)
    _set_resolution(root_comp.op('WORKING_PIPELINE/INSTALLATION_OUTPUT/installation_grade'),
                    width, height)
    triple_width = state['triple_surface_width']
    triple_height = state['triple_surface_height']
    for mode in ('WRAP', 'ARTISTIC'):
        for side in ('LEFT', 'CENTER', 'RIGHT'):
            _set_resolution(root_comp.op(
                'WORKING_PIPELINE/POINT_RENDER/METRIC_RENDER_%s_%s' %
                (mode, side)), triple_width, triple_height)
            if mode == 'WRAP':
                _set_resolution(root_comp.op(
                    'WORKING_PIPELINE/TRIPLE_DISPLAY/COVERAGE_WRAP_%s' % side),
                    triple_width, triple_height)
            _set_resolution(root_comp.op(
                'WORKING_PIPELINE/TRIPLE_DISPLAY/GRADE_%s_%s' %
                (mode, side)), triple_width, triple_height)
        _set_resolution(root_comp.op(
            'WORKING_PIPELINE/TRIPLE_DISPLAY/%s_MOSAIC' % mode),
            triple_width * 3, triple_height)
        _set_resolution(root_comp.op(
            'WORKING_PIPELINE/TRIPLE_DISPLAY/%s_MOSAIC_FALLBACK' % mode),
            triple_width * 3, triple_height)
    stereo_width = state['stereo_width']
    stereo_height = state['stereo_height']
    eye_width = max(64, stereo_width // 2)
    for path in ('WORKING_PIPELINE/POINT_RENDER/METRIC_RENDER_LEFT_EYE',
                 'WORKING_PIPELINE/POINT_RENDER/METRIC_RENDER_RIGHT_EYE'):
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
                             ('simulated', 'replay', 'depth_sensor', 'disabled')
                             else 'simulated')
            if (active_sensor == 'depth_sensor' and
                    not _configure_sensor_adapter(root_comp, state, sensor)):
                active_sensor = 'simulated'
        else:
            # AI-only and contract-failed processes must not import a local
            # sensor SDK/.tox or resolve its operators.
            active_sensor = 'inactive'
        state['sensor_mode_active'] = (
            'inactive' if not owns_sensor else
            'disabled' if sensor_mode == 'disabled' else active_sensor)
        sensor_route = _sensor_route_mode(state)
        _set(root_comp.op('WORKING_PIPELINE/SENSOR_INTERACTION'), 'Mode', sensor_route)
        state['sensor_route_active'] = sensor_route
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
                'sensor_calibration_error') or state.get(
                'calibration_error') or 'depth-sensor adapter is not installed'
            state['sensor_fallback'] = 'simulated (%s)' % reason

    # Mirror applied runtime values into the optional public show-control
    # surface. Parameter callbacks may reapply the same public values, but
    # never reach into private StreamDiffusionTD or sensor components.
    show_control = root_comp.op('WORKING_PIPELINE/SHOW_CONTROL')
    if show_control is not None:
        source_state = _mapping(state.get('source'))
        geometry_provider = str(source_state.get(
            'geometry_provider', state.get(
                'source_geometry_provider', 'moge2'))).lower()
        _set(show_control, 'Qualityprofile', state['tier'])
        _set(show_control, 'Geometryprovider', geometry_provider)
        _set(show_control, 'Displaymode', state['display_mode'])
        _set(show_control, 'Completionmode', state['completion'])
        _set(show_control, 'Geometryresolution', state['geometry_resolution'])
        _set(show_control, 'Pointbudget', state['point_budget'])
        _set(show_control, 'Pointsize', state['point_size_px'])
        _set(show_control, 'Geometryfps', state['geometry_fps'])
        if 'fog_density' in render_config:
            _set(show_control, 'Fogdensity', state['fog_density'])
        sensor_state = _mapping(state.get('sensor'))
        if 'force_gain' in sensor_state:
            _set(show_control, 'Interactionstrength',
                 sensor_state.get('force_gain'))

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
    _set(root_comp.op('WORKING_PIPELINE/ROLE_BRIDGE/MASK_ROUTE'),
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
                 'COMPLETION', 'RENDER_CONTRACT', 'POINT_RENDER',
                 'TRIPLE_DISPLAY'):
        _allow(root_comp.op('WORKING_PIPELINE/' + path), state['world_active'])
    _allow(root_comp.op('WORKING_PIPELINE/POINT_RENDER/METRIC_RENDER_CENTER'),
           state['installation_active'])
    for path in ('METRIC_RENDER_LEFT_EYE', 'METRIC_RENDER_RIGHT_EYE'):
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
        'frame_lifecycle': {}, 'source_session_id': None,
        'source_session_serial': -1, 'heartbeat_last_write': None,
        'heartbeat_application_state': None, 'readiness_progress': {},
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
    state['point_budget'] = min(
        state['point_budget'], state['geometry_resolution'] ** 2)
    state['adaptive_level'] = level

def apply(root_comp=None, overrides=None, inherit_environment=True):
    root_comp = root_comp or op('/project1/flexgpu')
    if root_comp is None:
        return {}
    dashboard = root_comp.op('OPERATOR_DASHBOARD')
    # Project construction must be deterministic and must never absorb the
    # builder process's ambient FLEXGPU_CONFIG. Normal startup keeps the
    # environment-aware behavior; bootstrap_project.build opts out explicitly.
    state = environment() if inherit_environment else {}
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
                    'temporal_last_reset_reason', 'frame_lifecycle',
                    'source_session_id', 'source_session_serial',
                    'heartbeat_last_write'):
            if key in previous_runtime:
                runtime[key] = previous_runtime[key]
    try:
        root_comp.store('_flexgpu_runtime', runtime)
        root_comp.storeStartupValue('_flexgpu_runtime', None)
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
    try:
        initial_dt = 1.0 / max(1.0, float(project.cookRate))
    except Exception:
        initial_dt = 1.0 / 60.0
    _set(root_comp.op('WORKING_PIPELINE/TEMPORAL_WORLD'),
         'Deltaseconds', initial_dt)
    _write_state(root_comp, state)
    if dashboard is not None:
        if state.get('runtime_error') or state.get('transport_error'):
            status = 'ERROR / %s' % (state.get('runtime_error') or
                                     state.get('transport_error'))
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
        root_comp.storeStartupValue('runtime_state', {})
    except Exception:
        pass
    if state.get('transport_error'):
        print('[FlexGPU] runtime transport ERROR (all heavy stages disabled): %s' %
              state['transport_error'])
    if state.get('runtime_error'):
        print('[FlexGPU] runtime override ERROR (all heavy stages disabled): %s' %
              state['runtime_error'])
    if state.get('source_fallback'):
        print('[FlexGPU] source WARNING: configured adapter was rejected; demo remains active')
    if state.get('sensor_fallback'):
        print('[FlexGPU] sensor WARNING: configured adapter was rejected; simulation remains active')
    _write_heartbeat(root_comp, runtime, force=True)
    print('[FlexGPU] runtime: %s' % json.dumps(
        _public_state(state), sort_keys=True, separators=(',', ':')))
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

def _heartbeat_identity(state):
    safe = dict((key, state.get(key)) for key in (
        'role', 'topology', 'experience', 'completion', 'tier',
        'diffusion_resolution', 'geometry_resolution', 'point_budget',
        'calibration_id', 'calibration_digest',
        'source_calibration_id', 'source_calibration_digest',
        'sensor_calibration_id', 'sensor_calibration_digest',
        'worldbus_version'))
    encoded = json.dumps(safe, sort_keys=True, separators=(',', ':'),
                         ensure_ascii=True, allow_nan=False).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()

def _heartbeat_config_identity(runtime):
    cached = runtime.get('heartbeat_config_identity')
    if isinstance(cached, str) and SHA256_PATTERN.fullmatch(cached):
        return cached, runtime.get('heartbeat_config_identity_kind', 'runtime_state')
    path = str(os.environ.get('FLEXGPU_CONFIG', '')).strip()
    identity = None
    kind = 'runtime_state'
    if path:
        expanded = os.path.abspath(os.path.normpath(
            os.path.expandvars(os.path.expanduser(path))))
        if os.path.isfile(expanded):
            try:
                if expanded.lower().endswith('.toml'):
                    # Keep the heartbeat digest on the same parsed raw mapping
                    # as the supervisor. stdlib tomllib is present in the
                    # supported TouchDesigner Python; fail closed to the manual
                    # runtime-state identity only when that module is absent.
                    try:
                        import tomllib
                    except ImportError:
                        runtime['heartbeat_config_error'] = 'tomllib_unavailable'
                        raw = None
                    else:
                        with open(expanded, 'rb') as handle:
                            raw = tomllib.load(handle)
                else:
                    with open(expanded, 'r', encoding='utf-8-sig') as handle:
                        raw = json.load(handle, object_pairs_hook=_config_pairs,
                                        parse_constant=_config_constant)
                if not isinstance(raw, dict):
                    raise ValueError('runtime config root must be an object')
                encoded = json.dumps(
                    raw, sort_keys=True, separators=(',', ':'),
                    ensure_ascii=False, allow_nan=False).encode('utf-8')
                identity = hashlib.sha256(encoded).hexdigest()
                kind = 'canonical_config_raw'
            except Exception:
                identity = None
                kind = 'runtime_state_config_unavailable'
        else:
            kind = 'runtime_state_config_unavailable'
    if identity is None:
        identity = _heartbeat_identity(runtime['state'])
    runtime['heartbeat_config_identity'] = identity
    runtime['heartbeat_config_identity_kind'] = kind
    return identity, kind

def _expected_heartbeat_identity(runtime):
    file_config, file_config_kind = _heartbeat_config_identity(runtime)
    expected_build = str(os.environ.get(
        'FLEXGPU_EXPECTED_BUILD_VERSION', '')).strip()
    expected_config = str(os.environ.get('FLEXGPU_CONFIG_ID', '')).strip()
    build_matches = not expected_build or expected_build == RUNTIME_BUILD_VERSION
    config_expected = bool(expected_config)
    expected_config_valid = (
        SHA256_PATTERN.fullmatch(expected_config) is not None)
    # The supervisor hashes the validated effective config after CLI overrides.
    # A TouchDesigner process cannot reproduce that identity from the unchanged
    # source file, so a valid launcher-owned injection is the authoritative
    # supervised identity. Keep the local file digest only as diagnostics.
    if config_expected and expected_config_valid:
        config_identity = expected_config
        config_identity_kind = 'supervisor_effective_config'
        config_matches = True
    else:
        config_identity = file_config
        config_identity_kind = file_config_kind
        config_matches = not config_expected
    return {
        'build_version':RUNTIME_BUILD_VERSION,
        'build_expected':bool(expected_build),
        'build_matches':build_matches,
        'config_identity':config_identity,
        'config_identity_kind':config_identity_kind,
        'config_expected':config_expected,
        'config_matches':config_matches,
        'config_file_identity':file_config,
        'config_file_identity_kind':file_config_kind,
        'config_file_matches_effective':bool(
            expected_config_valid and expected_config == file_config),
    }

def _readiness_text(value, limit=READINESS_MESSAGE_LIMIT):
    text = ' '.join(str(value).replace('\x00', '').split())
    if len(text) > limit:
        text = text[:max(0, limit - 3)] + '...'
    return text

def _readiness_node_path(root_comp, node):
    path = _readiness_text(getattr(node, 'path', ''), 192)
    root_path = _readiness_text(getattr(root_comp, 'path', ''), 192).rstrip('/')
    if root_path and path.startswith(root_path + '/'):
        return path[len(root_path) + 1:]
    return path or '<unknown>'

def _readiness_messages(node, method_name):
    method = getattr(node, method_name, None)
    if not callable(method):
        return [], None
    try:
        result = method()
    except Exception as exc:
        return [], _readiness_text('%s: %s' % (method_name, exc))
    if not result:
        return [], None
    if isinstance(result, str):
        values = result.splitlines()
    else:
        try:
            values = list(result)
        except Exception:
            values = [result]
    messages = []
    for value in values:
        message = _readiness_text(value)
        if message:
            messages.append(message)
        if len(messages) >= READINESS_ISSUE_LIMIT:
            break
    return messages, None

def _readiness_external_tox_path(node):
    try:
        external = getattr(getattr(node, 'par', None), 'externaltox', None)
    except Exception:
        return ''
    if external is None:
        return ''
    try:
        return str(external.eval()).strip()
    except Exception:
        try:
            return str(external).strip()
        except Exception:
            return ''

def _bounded_managed_nodes(root_comp, limit=READINESS_MANAGED_OPERATOR_LIMIT):
    pending = [root_comp]
    seen = set()
    nodes = []
    truncated = False
    while pending and len(nodes) < limit:
        node = pending.pop()
        path = str(getattr(node, 'path', ''))
        key = path or 'object:%d' % id(node)
        if key in seen:
            continue
        seen.add(key)
        nodes.append(node)
        # The root COMP still exposes propagated errors from an external TOX.
        # Descending into its implementation would make readiness depend on the
        # size and licensing details of private third-party components.
        if node is not root_comp and _readiness_external_tox_path(node):
            continue
        try:
            iterator = iter(node.children)
        except Exception:
            continue
        children = []
        try:
            for child in iterator:
                if len(nodes) + len(pending) + len(children) >= limit:
                    truncated = True
                    break
                children.append(child)
        except Exception:
            # A node can disappear during an interactive network edit. The
            # next bounded scan retries; this observation fails closed.
            truncated = True
        pending.extend(reversed(children))
    if pending:
        truncated = True
    return nodes, truncated

def _required_readiness_outputs(root_comp, state):
    required = []
    if state.get('world_active'):
        required.extend((
            ('position', 'WORKING_PIPELINE/OUT_POSITION'),
            ('color', 'WORKING_PIPELINE/OUT_COLOR'),
            ('interaction', 'WORKING_PIPELINE/OUT_INTERACTION'),
        ))
        if state.get('installation_active'):
            required.append(
                ('display_active', 'WORKING_PIPELINE/OUT_DISPLAY_ACTIVE'))
        if state.get('vr_active'):
            required.extend((
                ('left_eye', 'WORKING_PIPELINE/OUT_LEFT_EYE'),
                ('right_eye', 'WORKING_PIPELINE/OUT_RIGHT_EYE'),
                ('stereo_preview', 'WORKING_PIPELINE/OUT_STEREO_PREVIEW'),
            ))
    elif state.get('ai_active'):
        if state.get('transport_sender_active'):
            required.append((
                'transport_atlas',
                'WORKING_PIPELINE/ROLE_BRIDGE/PACK_ATOMIC_ATLAS'))
        else:
            required.append(
                ('source_rgb', 'WORKING_PIPELINE/SOURCES/RGB_SOURCE'))
    resolved = []
    for name, path in required:
        try:
            node = root_comp.op(path)
        except Exception:
            node = None
        resolved.append((name, path, node))
    return resolved

def _readiness_dimensions(name, path, node):
    width = None
    height = None
    if node is not None:
        try:
            width = int(node.width)
        except Exception:
            pass
        try:
            height = int(node.height)
        except Exception:
            pass
    valid = (
        width is not None and height is not None and
        0 < width <= READINESS_MAX_OUTPUT_DIMENSION and
        0 < height <= READINESS_MAX_OUTPUT_DIMENSION)
    result = {'name':name, 'path':path, 'width':width, 'height':height,
              'valid':valid}
    if node is None:
        result['problem'] = 'missing_operator'
    elif width is None or height is None:
        result['problem'] = 'dimensions_unavailable'
    elif not valid:
        result['problem'] = 'dimensions_out_of_range'
    return result

def _inspect_readiness_health(root_comp, runtime, now, force=False):
    cached = runtime.get('readiness_managed_health')
    checked_at = runtime.get('readiness_managed_health_checked_at')
    if (not force and isinstance(cached, dict) and checked_at is not None and
            now >= checked_at and
            now - checked_at < READINESS_HEALTH_INTERVAL_SECONDS):
        return cached

    nodes, truncated = _bounded_managed_nodes(root_comp)
    operator_errors = []
    shader_compile_errors = []
    inspection_failures = []
    operator_error_count = 0
    shader_compile_error_count = 0
    for node in nodes:
        path = _readiness_node_path(root_comp, node)
        errors, error_failure = _readiness_messages(node, 'errors')
        warnings, warning_failure = _readiness_messages(node, 'warnings')
        if error_failure or warning_failure:
            if len(inspection_failures) < READINESS_ISSUE_LIMIT:
                inspection_failures.append({
                    'path':path,
                    'messages':[message for message in (
                        error_failure, warning_failure) if message],
                })
        if errors:
            operator_error_count += 1
            if len(operator_errors) < READINESS_ISSUE_LIMIT:
                operator_errors.append({'path':path, 'messages':errors})
        compile_warnings = [
            message for message in warnings
            if 'compile error' in message.lower().replace('-', ' ')]
        if compile_warnings:
            shader_compile_error_count += 1
            if len(shader_compile_errors) < READINESS_ISSUE_LIMIT:
                shader_compile_errors.append({
                    'path':path, 'messages':compile_warnings})

    required_outputs = [
        _readiness_dimensions(name, path, node)
        for name, path, node in _required_readiness_outputs(
            root_comp, runtime['state'])]
    invalid_outputs = [
        item for item in required_outputs if not item['valid']]
    result = {
        'interval_ms':int(READINESS_HEALTH_INTERVAL_SECONDS * 1000.0),
        'operators_scanned':len(nodes),
        'operator_limit':READINESS_MANAGED_OPERATOR_LIMIT,
        'scan_complete':not truncated,
        'operator_error_count':operator_error_count,
        'operator_errors':operator_errors,
        'shader_compile_error_count':shader_compile_error_count,
        'shader_compile_errors':shader_compile_errors,
        'inspection_failures':inspection_failures,
        'required_outputs':required_outputs,
        'invalid_outputs':invalid_outputs,
    }
    runtime['readiness_managed_health'] = result
    runtime['readiness_managed_health_checked_at'] = now
    return result

def _readiness_output_node(root_comp, state):
    if state.get('world_active'):
        if state.get('installation_active'):
            return root_comp.op(
                'WORKING_PIPELINE/OUT_DISPLAY_ACTIVE'), 'display_active'
        if state.get('vr_active'):
            return root_comp.op('WORKING_PIPELINE/OUT_LEFT_EYE'), 'left_eye'
        return None, 'world_output_inactive'
    if state.get('ai_active'):
        if state.get('transport_sender_active'):
            return root_comp.op(
                'WORKING_PIPELINE/ROLE_BRIDGE/PACK_ATOMIC_ATLAS'), 'transport_atlas'
        return root_comp.op('WORKING_PIPELINE/SOURCES/RGB_SOURCE'), 'source_rgb'
    return None, 'no_active_role'

def _update_readiness_progress(root_comp, runtime, now, previous_tick):
    progress = runtime.setdefault('readiness_progress', {})
    progress['tick_count'] = int(progress.get('tick_count', 0)) + 1
    if previous_tick is not None and now > previous_tick:
        progress['cook_advances'] = int(progress.get('cook_advances', 0)) + 1
    primary_node, output_name = _readiness_output_node(
        root_comp, runtime['state'])
    progress['output_name'] = output_name
    prior_slots = progress.get('required_output_slots', {})
    slots = {}
    required = _required_readiness_outputs(root_comp, runtime['state'])
    for name, path, node in required:
        slot = dict(prior_slots.get(name, {}))
        slot.update({'name':name, 'path':path, 'configured':node is not None})
        slot.pop('probe_error', None)
        cook_frame_before = _operator_cook_token(node)
        cooked = False
        probe_count = int(slot.get('probe_count', 0))
        if (node is not None and int(slot.get('advances', 0)) < 1 and
                probe_count < 2):
            try:
                node.cook(force=True)
                cooked = True
            except TypeError:
                try:
                    node.cook()
                    cooked = True
                except Exception as exc:
                    slot['probe_error'] = _readiness_text(exc)
            except Exception as exc:
                slot['probe_error'] = _readiness_text(exc)
            slot['probe_count'] = probe_count + 1
        cook_frame_after = _operator_cook_token(node)
        cook_frame = (
            cook_frame_after
            if cook_frame_after is not None else cook_frame_before)
        prior_cook_frame = slot.get('token')
        if cook_frame is not None:
            slot['observation'] = 'cook_frame'
            if prior_cook_frame is not None and cook_frame > prior_cook_frame:
                slot['advances'] = int(slot.get('advances', 0)) + 1
            elif prior_cook_frame is not None and cook_frame < prior_cook_frame:
                slot['regressed'] = True
            slot['token'] = cook_frame
        else:
            # Minimal unit-test shells do not expose TouchDesigner cook tokens.
            # They remain compatible only after two callback observations.
            slot['observation'] = 'synthetic_unobservable'
            slot['probe_called'] = cooked
        slots[name] = slot
    progress['required_output_slots'] = slots
    output_progress = []
    for name, path, node in required:
        slot = slots[name]
        item = {
            'name':name,
            'path':path,
            'configured':bool(slot.get('configured', False)),
            'observation':slot.get('observation', 'none'),
            'advances':int(slot.get('advances', 0)),
            'regressed':bool(slot.get('regressed', False)),
        }
        if slot.get('probe_error'):
            item['probe_error'] = slot['probe_error']
        output_progress.append(item)
    progress['required_outputs'] = output_progress
    progress['output_configured'] = bool(output_progress) and all(
        item['configured'] for item in output_progress)
    progress['output_regressed'] = any(
        item['regressed'] for item in output_progress)
    progress['outputs_not_advancing'] = [
        item['name'] for item in output_progress
        if (item['configured'] and item['observation'] == 'cook_frame' and
            item['advances'] < 1)]
    progress['outputs_not_observable'] = [
        item['name'] for item in output_progress
        if (item['configured'] and
            item['observation'] == 'synthetic_unobservable' and
            int(progress.get('tick_count', 0)) < 2)]
    progress['output_probe_failures'] = [
        item['name'] for item in output_progress if item.get('probe_error')]
    primary = next((
        item for item in output_progress if item['name'] == output_name), None)
    if primary is None and primary_node is not None:
        primary = {'observation':'none', 'advances':0}
    progress['output_observation'] = (
        primary.get('observation', 'none') if primary else 'none')
    progress['output_advances'] = (
        min(item['advances'] for item in output_progress)
        if output_progress else 0)
    return progress

def _application_readiness(root_comp, runtime, managed_health=None):
    state = runtime['state']
    progress = runtime.get('readiness_progress', {})
    source = runtime.get('frame_lifecycle', {}).get('source', {})
    identity = _expected_heartbeat_identity(runtime)
    if managed_health is None:
        managed_health = runtime.get('readiness_managed_health')
    if not isinstance(managed_health, dict):
        managed_health = _inspect_readiness_health(
            root_comp, runtime, runtime.get('last_tick') or 0.0, force=True)
    reasons = []
    hard_failure = False
    if state.get('runtime_error'):
        reasons.append('runtime_contract_error')
        hard_failure = True
    if state.get('transport_error'):
        reasons.append('transport_contract_error')
        hard_failure = True
    if (state.get('source_contract_error') or
            state.get('sensor_contract_error') or
            state.get('calibration_error') or
            state.get('source_calibration_error') or
            state.get('sensor_calibration_error')):
        reasons.append('source_contract_error')
        hard_failure = True
    if not identity['build_matches']:
        reasons.append('build_identity_mismatch')
        hard_failure = True
    if not identity['config_matches']:
        reasons.append('config_identity_mismatch')
        hard_failure = True
    if not managed_health.get('scan_complete', False):
        reasons.append('managed_health_scan_incomplete')
        hard_failure = True
    if managed_health.get('inspection_failures'):
        reasons.append('managed_health_inspection_failed')
        hard_failure = True
    if int(managed_health.get('operator_error_count', 0)) > 0:
        reasons.append('managed_operator_errors')
        hard_failure = True
    if int(managed_health.get('shader_compile_error_count', 0)) > 0:
        reasons.append('managed_shader_compile_errors')
        hard_failure = True
    if managed_health.get('invalid_outputs'):
        reasons.append('required_output_dimensions_invalid')
        hard_failure = True
    if int(progress.get('tick_count', 0)) < 2 or int(
            progress.get('cook_advances', 0)) < 1:
        reasons.append('cook_not_advancing')
    if int(source.get('accepted_count', 0)) < 1:
        reasons.append('source_not_accepted')
    elif not bool(source.get('valid', False)):
        reasons.append('source_unhealthy')
    if not progress.get('output_configured', False):
        reasons.append('output_not_configured')
    elif progress.get('output_probe_failures'):
        reasons.append('output_probe_failed')
        hard_failure = True
    elif progress.get('output_regressed'):
        reasons.append('output_cook_regressed')
        hard_failure = True
    elif progress.get('outputs_not_advancing'):
        reasons.append('output_not_advancing')
    elif progress.get('outputs_not_observable'):
        reasons.append('output_not_observable')
    ready = not reasons
    return {
        'ready':ready,
        'state':'ready' if ready else 'degraded' if hard_failure else 'starting',
        'reasons':reasons,
        'identity':identity,
        'tick_count':int(progress.get('tick_count', 0)),
        'cook_advances':int(progress.get('cook_advances', 0)),
        'source_accepted':int(source.get('accepted_count', 0)),
        'output_name':progress.get('output_name', 'unknown'),
        'output_observation':progress.get('output_observation', 'none'),
        'output_advances':int(progress.get('output_advances', 0)),
        'required_output_progress':list(progress.get('required_outputs', [])),
        'managed_health':managed_health,
    }

def _heartbeat_settings():
    session_id = str(os.environ.get('FLEXGPU_SESSION_ID', '')).strip()
    path = str(os.environ.get('FLEXGPU_HEARTBEAT_PATH', '')).strip()
    if (not session_id or len(session_id) > 128 or
            IDENTIFIER_PATTERN.fullmatch(session_id) is None or not path):
        return None
    try:
        timeout_ms = float(os.environ.get('FLEXGPU_HEARTBEAT_TIMEOUT_MS', '3000'))
        if not math.isfinite(timeout_ms):
            raise ValueError()
    except Exception:
        timeout_ms = 3000.0
    timeout_ms = min(600000.0, max(250.0, timeout_ms))
    expanded = os.path.abspath(os.path.normpath(
        os.path.expandvars(os.path.expanduser(path))))
    return session_id, expanded, timeout_ms

def _write_heartbeat(root_comp, runtime, health=None, force=False):
    settings = _heartbeat_settings()
    if settings is None:
        return False
    session_id, path, timeout_ms = settings
    now = time.perf_counter()
    interval = min(1.0, max(0.10, timeout_ms / 3000.0))
    state = runtime['state']
    health = health or _health_snapshot(root_comp, runtime, 0.0)
    source = runtime.get('frame_lifecycle', {}).get('source', {})
    sensor = runtime.get('frame_lifecycle', {}).get('sensor', {})
    managed_health = _inspect_readiness_health(
        root_comp, runtime, now, force=force)
    readiness = _application_readiness(
        root_comp, runtime, managed_health=managed_health)
    application_state = readiness['state']
    previous = runtime.get('heartbeat_last_write')
    if (not force and previous is not None and now - previous < interval and
            runtime.get('heartbeat_application_state') == application_state):
        return False
    identity = readiness['identity']
    payload = {
        'version':HEARTBEAT_VERSION,
        'session_id':session_id,
        'role':state.get('role', 'standalone'),
        'pid':os.getpid(),
        'state':application_state,
        'updated_at':datetime.datetime.now(
            datetime.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z'),
        'build':{'version':RUNTIME_BUILD_VERSION,
                 'expected':identity['build_expected'],
                 'matches_expected':identity['build_matches']},
        'config':{'identity':identity['config_identity'],
                  'identity_kind':identity['config_identity_kind'],
                  'expected':identity['config_expected'],
                  'matches_expected':identity['config_matches'],
                  'file_identity':identity['config_file_identity'],
                  'file_identity_kind':identity['config_file_identity_kind'],
                  'file_matches_effective':identity[
                      'config_file_matches_effective']},
        'cook':{'frame':int(runtime.get('frame', 0)),
                'count':readiness['tick_count'],
                'frame_time_ms':float(health.get('frame_time_ms', 0.0)),
                'operator_cook_time_ms':float(
                    health.get('operator_cook_time_ms', 0.0))},
        'source':{'frame_id':source.get('frame_id', -1),
                  'session_epoch':int(runtime.get('source_session_serial', 0)),
                  'age_ms':source.get('age_ms', -1.0),
                  'new_frame':bool(source.get('new_frame', False)),
                  'valid':bool(source.get('valid', True)),
                  'decision':source.get('decision', 'initializing')},
        'sensor':{'frame_id':sensor.get('frame_id', -1),
                  'age_ms':sensor.get('age_ms', -1.0),
                  'valid':bool(sensor.get('valid', True)),
                  'decision':sensor.get('decision', 'initializing')},
        'transport':{'mode':state.get('bridge_mode', 'local'),
                     'endpoint':state.get('transport_endpoint_active', ''),
                     'last_send_frame':state.get('transport_last_send_frame', -1)},
        'output':{'installation_active':bool(
                      state.get('installation_active', False)),
                  'vr_active':bool(state.get('vr_active', False)),
                  'name':readiness['output_name'],
                  'observation':readiness['output_observation'],
                  'advances':readiness['output_advances'],
                  'required':readiness['required_output_progress']},
        'readiness':{'ready':readiness['ready'],
                     'reasons':list(readiness['reasons']),
                     'cook_advances':readiness['cook_advances'],
                     'source_accepted':readiness['source_accepted'],
                     'managed_health':readiness['managed_health']},
    }
    temporary = path + '.tmp.%d' % os.getpid()
    try:
        directory = os.path.dirname(path)
        if directory and not os.path.isdir(directory):
            os.makedirs(directory)
        with open(temporary, 'w', encoding='utf-8') as handle:
            json.dump(payload, handle, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True, allow_nan=False)
            handle.write('\n')
            handle.flush()
        os.replace(temporary, path)
        runtime['heartbeat_last_write'] = now
        runtime['heartbeat_application_state'] = application_state
        return True
    except Exception as exc:
        try:
            if os.path.isfile(temporary):
                os.remove(temporary)
        except Exception:
            pass
        print('[FlexGPU] heartbeat warning: %s' % exc)
        return False

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
    now_ns = time.time_ns()
    previous = runtime.get('last_tick')
    runtime['last_tick'] = now
    if previous is None:
        try:
            delta_seconds = 1.0 / max(1.0, float(project.cookRate))
        except Exception:
            delta_seconds = 1.0 / 60.0
        frame_ms = 0.0
    else:
        delta_seconds = min(0.25, max(0.0, now - previous))
        frame_ms = delta_seconds * 1000.0
    _set(root_comp.op('WORKING_PIPELINE/TEMPORAL_WORLD'),
         'Deltaseconds', delta_seconds)
    _sample_frame_lifecycle(root_comp, runtime, now_ns, now)
    camera_contract_accepted = _sample_source_camera_metadata(root_comp, runtime)
    if camera_contract_accepted:
        _apply_calibrated_contracts(root_comp, runtime['state'])
    signature_changed = _check_temporal_signature(
        root_comp, runtime['state'], 'manual source/session/calibration contract changed')
    _update_readiness_progress(root_comp, runtime, now, previous)
    if previous is None:
        health = _health_snapshot(root_comp, runtime, 0.0)
        _write_health(root_comp, health)
        _write_heartbeat(root_comp, runtime, health)
        return None
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
                root_comp.storeStartupValue('runtime_state', {})
            except Exception:
                pass
        elif runtime['cooldown'] > 0:
            runtime['cooldown'] -= 1

    telemetry = _mapping(runtime['state'].get('telemetry'))
    health = _health_snapshot(root_comp, runtime, frame_ms)
    _write_heartbeat(root_comp, runtime, health)
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


def _operator_type_name(node):
    """Return TouchDesigner's canonical Python operator type when available."""

    for attribute in ("opType", "OPType"):
        try:
            value = getattr(node, attribute)
        except Exception:
            continue
        if value:
            return str(value)
    try:
        value = node.__class__.__name__
    except Exception:
        value = ""
    if any(str(value).lower().endswith(suffix) for suffix in
           ("comp", "top", "chop", "sop", "dat", "mat", "pop")):
        return str(value)
    try:
        operator_type = str(node.type)
        family = str(node.family)
    except Exception:
        return ""
    return operator_type + family


def _operator_type_token(value):
    return "".join(character for character in str(value).lower()
                   if character.isalnum())


def _operator_type_matches(node, expected):
    actual = _operator_type_name(node)
    return bool(actual and
                _operator_type_token(actual) == _operator_type_token(expected))


def _ensure(parent, type_name, name, report, optional=False):
    found = _child(parent, name)
    if found is not None:
        if not _operator_type_matches(found, type_name):
            actual = _operator_type_name(found) or "unverifiable operator type"
            message = "%s already exists as %s; expected %s" % (
                found.path, actual, type_name)
            if optional:
                report.warn(message)
                return None
            raise RuntimeError(message)
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
        return False


def _set_par_expression(node, names, expression):
    if node is None:
        return False
    if isinstance(names, str):
        names = (names,)
    p = _par(node, *names)
    if p is None:
        return False
    try:
        p.expr = expression
        return True
    except Exception:
        return False


def _configure_simulated_sensor_circle(pipeline, report):
    """Apply the documented TouchDesigner Circle TOP parameter contract."""
    try:
        circle = pipeline.op('SENSOR_INTERACTION/SIMULATED_SENSOR_MASK')
    except Exception:
        circle = None
    if circle is None:
        return
    required = (
        _set_par(circle, 'radiusx', 0.16),
        _set_par(circle, 'radiusy', 0.16),
        _set_par_expression(
            circle, 'centerx',
            '0.24 * math.sin(absTime.seconds * 0.73)'),
        _set_par_expression(
            circle, 'centery',
            '0.18 * math.cos(absTime.seconds * 0.91)'),
    )
    # Circle TOP defines (0, 0) as image center. Explicit fraction units make
    # the intended radius and animated offsets independent of output size.
    _set_par(circle, 'radiusunit', 'fraction')
    _set_par(circle, 'centerunit', 'fraction')
    if not all(required):
        report.warn(
            'SIMULATED_SENSOR_MASK is missing the Circle TOP '
            'radiusx/radiusy/centerx/centery contract')


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


def _safe_build_profile(config):
    """Return only non-private values that are safe to persist in a .toe.

    Adapter/operator paths, process commands, environment values, credentials,
    telemetry paths, network peers and shared-memory names are runtime-only.
    Unknown values are omitted rather than copied into CONFIG/profile_flat or
    TouchDesigner storage.
    """
    if not isinstance(config, dict):
        return {}
    safe = {}
    enums = {
        "role": ("standalone", "world", "render", "ai"),
        "topology": ("single", "dual_local", "dual_network"),
        "experience": ("installation", "vr", "combined"),
        "completion": ("fog", "procedural", "hybrid"),
        "tier": ("3080ti_16gb", "4090", "5090", "custom"),
        "node_role": ("ai", "render"),
    }
    for key, permitted in enums.items():
        value = _lookup(config, key, None)
        if isinstance(value, str) and value.lower() in permitted:
            safe[key] = value.lower()

    numeric_keys = (
        "installation_fps", "vr_fps", "diffusion_fps",
        "diffusion_resolution", "geometry_fps", "geometry_resolution",
        "point_budget",
    )
    for key in numeric_keys:
        value = _lookup(config, key, None)
        if (not isinstance(value, bool) and isinstance(value, (int, float)) and
                math.isfinite(float(value))):
            safe[key] = value
    for key in ("safe_mode", "ai_enabled", "sensor_enabled"):
        value = _lookup(config, key, None)
        if isinstance(value, bool):
            safe[key] = value
    worldbus = _lookup(config, "worldbus_version", None)
    if (isinstance(worldbus, str) and
            re.fullmatch(r"[0-9]+(?:\.[0-9]+){0,2}", worldbus)):
        safe["worldbus_version"] = worldbus

    def section(name, numeric=(), boolean=(), enums=()):
        raw = _lookup(config, name, None)
        if not isinstance(raw, dict):
            return
        result = {}
        for key in numeric:
            value = raw.get(key)
            if (not isinstance(value, bool) and
                    isinstance(value, (int, float)) and
                    math.isfinite(float(value))):
                result[key] = value
        for key in boolean:
            value = raw.get(key)
            if isinstance(value, bool):
                result[key] = value
        for key, permitted in enums:
            value = raw.get(key)
            if isinstance(value, str) and value.lower() in permitted:
                result[key] = value.lower()
        if result:
            safe[name] = result

    section(
        "adaptive",
        numeric=("levels", "initial_level", "frame_budget_ms",
                 "queue_budget_ms", "down_window", "up_window",
                 "cooldown_samples"),
        boolean=("enabled",),
    )
    raw_adaptive = _lookup(config, "adaptive", None)
    if isinstance(raw_adaptive, dict) and isinstance(
            raw_adaptive.get("thresholds"), dict):
        thresholds = {}
        for key in (
                "frame_low", "frame_high", "vram_low", "vram_high",
                "queue_low", "queue_high", "critical_frame",
                "critical_vram", "critical_queue"):
            value = raw_adaptive["thresholds"].get(key)
            if (not isinstance(value, bool) and
                    isinstance(value, (int, float)) and
                    math.isfinite(float(value))):
                thresholds[key] = value
        if thresholds:
            safe.setdefault("adaptive", {})["thresholds"] = thresholds
    section(
        "render",
        numeric=("point_size_px", "point_budget", "point_keep_fraction",
                 "installation_width",
                 "installation_height", "installation_fps", "stereo_width",
                 "stereo_height", "vr_fps", "triple_surface_width",
                 "triple_surface_height", "surface_fov_degrees",
                 "triple_wrap_yaw_degrees",
                 "triple_artistic_yaw_degrees",
                 "triple_artistic_offset_metres", "fog_density",
                 "procedural_mix"),
        enums=(("display_mode", (
            "single", "panoramic_wrap", "artistic_multi_angle")),),
    )
    section(
        "transport", numeric=("atlas_width", "atlas_height", "atlas_fps"),
        boolean=("drop_stale_frames", "hold_last_complete_frame"),
        enums=(("type", ("local", "shared_memory", "touch_tcp")),),
    )
    return safe


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
    embedded_profile = _safe_build_profile(config)
    defaults = dict(DEFAULTS)
    for key in defaults:
        defaults[key] = _lookup(embedded_profile, key, defaults[key])

    config_comp = _ensure(flexgpu, "baseCOMP", "CONFIG", report)
    _style(config_comp, -1150, 420, colors["config"], "JSON profile and runtime state")
    rows = [["key", "value", "source"]]
    rows += [[key, defaults[key], "profile/default"] for key in sorted(defaults)]
    _table(config_comp, "settings", rows, report)
    _table(config_comp, "runtime_state", [["key", "value"], ["status", "not started"]], report)
    profile_rows = [["json_path", "value"]] + [
        [key, value] for key, value in _flatten(embedded_profile)]
    if config:
        profile_rows.append(["<runtime-only fields>", "not embedded"])
    _table(config_comp, "profile_flat", profile_rows, report)
    _text(config_comp, "README", "Only safe role/quality/render defaults are embedded. Private adapter, process, path, network and credential values are runtime-only; environment variables win at startup.", report)

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
        # build() enables this only after its environment-isolated defaults
        # have replaced any previously stored private runtime state.
        _set_par(execute, "active", False)
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

    _text(flexgpu, "README_FIRST", "FLEXGPU REALTIME POINT WORLD\nOpen WORKING_PIPELINE/OUT_INSTALLATION for the built-in point-world demo and OUT_STEREO_PREVIEW for desktop stereo.\nLater replace only WORKING_PIPELINE/SOURCES/STREAMDIFFUSION_ADAPTER with your StreamDiffusionTD.tox outputs.\nWORKING_PIPELINE/ROLE_BRIDGE now packs RGB/depth into one atomic RGBA32F preview atlas for Shared Mem or Touch TCP split roles; unassigned stages are cook-gated. This direct image path is not the full WorldBus v1 metadata/control protocol.", report)
    _table(flexgpu, "bootstrap_manifest", [["field", "value"],
        ["build_version", BUILD_VERSION], ["root", ROOT_PATH],
        ["config_path", ("<explicit runtime profile; path omitted>"
                         if config_path else "<defaults>")],
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
    _configure_simulated_sensor_circle(pipeline, report)
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
    # An existing project's Execute DAT may be active while rebuilding. Disable
    # it before creating/updating managed nodes so an onCreate callback cannot
    # import the builder process's ambient FLEXGPU_CONFIG into persistent state.
    existing_startup = _child(_child(flexgpu, "STARTUP"), "startup_callbacks")
    _set_par(existing_startup, "active", False)
    try:
        _style(flexgpu, flexgpu.nodeX, flexgpu.nodeY, (0.22, 0.36, 0.33),
               "FlexGPU modular realtime 2D-to-3D show shell", 260, 150)
    except Exception:
        pass
    config = _load_config(config_path, report)
    embedded_profile = _safe_build_profile(config)
    _build_shell(flexgpu, config, config_path, report)
    _build_working_pipeline(flexgpu, report)

    # Apply build-time defaults now; the Execute DAT reapplies runtime env on start.
    runtime_dat = _child(_child(flexgpu, "STARTUP"), "runtime_helpers")
    if runtime_dat is not None:
        try:
            role_default = _lookup(embedded_profile, "role", None)
            if role_default is None:
                node_role = str(embedded_profile.get("node_role", "")).lower()
                role_default = {"ai":"ai", "render":"world"}.get(
                    node_role, DEFAULTS["role"])
            runtime_overrides = {
                "role": role_default,
                "topology": _lookup(
                    embedded_profile, "topology", DEFAULTS["topology"]),
                "experience": _lookup(
                    embedded_profile, "experience", DEFAULTS["experience"]),
                "completion": _lookup(
                    embedded_profile, "completion", DEFAULTS["completion"]),
                "tier": _lookup(
                    embedded_profile, "tier", DEFAULTS["tier"]),
                # The build path is deliberately not persisted. Runtime startup
                # may load private configuration through FLEXGPU_CONFIG.
                "config_path": "",
            }
            for section in ("adaptive", "telemetry", "source", "sensor",
                            "render", "transport"):
                value = embedded_profile.get(section)
                if isinstance(value, dict):
                    runtime_overrides[section] = dict(value)
            for key in ("diffusion_resolution", "diffusion_fps",
                        "geometry_resolution", "geometry_fps",
                        "point_budget", "vr_fps"):
                value = _lookup(embedded_profile, key, None)
                if value not in (None, ""):
                    runtime_overrides[key] = value
            runtime_dat.module.apply(
                flexgpu, runtime_overrides, inherit_environment=False)
        except Exception as exc:
            report.warn("Runtime defaults were not applied during build: %s" % exc)
    startup_callbacks = _child(_child(flexgpu, "STARTUP"), "startup_callbacks")
    _set_par(startup_callbacks, "active", True)
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
