"""Build the non-destructive FlexGPU TouchDesigner 2025 starter shell.

Run this module *inside TouchDesigner*.  It creates or updates only
``/project1/flexgpu`` and never removes unknown nodes.  The generated project is
an integration scaffold: render, AI, sensor, transport and VR implementations
are deliberately represented by clearly labelled placeholders.
"""

from __future__ import print_function

import json
import os


BUILD_VERSION = "0.2.0"
ROOT_PATH = "/project1/flexgpu"
LAST_REPORT = None


DEFAULTS = {
    "role": "standalone",          # standalone | world | ai
    "topology": "single",          # single | dual
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
}

def _par(comp, name):
    try:
        return getattr(comp.par, name)
    except Exception:
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

def environment():
    values = {}
    config_path = os.environ.get('FLEXGPU_CONFIG', '')
    config = _json(config_path)
    for key in ('role', 'topology', 'experience', 'completion', 'tier'):
        value = _lookup(config, key)
        if value not in (None, ''):
            values[key] = value
    for env_name, key in ENV_KEYS.items():
        value = os.environ.get(env_name)
        if value:
            values[key] = value
    values['config_path'] = config_path
    return values

def _write_state(root_comp, state):
    table = root_comp.op('CONFIG/runtime_state')
    if table is None:
        return
    try:
        table.clear()
        table.appendRow(['key', 'value'])
        for key in sorted(state):
            table.appendRow([key, state[key]])
    except Exception:
        pass

def apply(root_comp=None, overrides=None):
    root_comp = root_comp or op('/project1/flexgpu')
    if root_comp is None:
        return {}
    dashboard = root_comp.op('OPERATOR_DASHBOARD')
    state = environment()
    if overrides:
        state.update(overrides)
    defaults = {'role':'standalone', 'topology':'single',
                'experience':'installation', 'completion':'hybrid',
                'tier':'3080ti_16gb'}
    for key, fallback in defaults.items():
        state[key] = str(state.get(key, fallback)).lower()

    if dashboard is not None:
        _set(dashboard, 'Role', state['role'])
        _set(dashboard, 'Topology', state['topology'])
        _set(dashboard, 'Experience', state['experience'])
        _set(dashboard, 'Completion', state['completion'])
        _set(dashboard, 'Tier', state['tier'])

    ai = root_comp.op('AI_PIPELINE')
    world = root_comp.op('WORLD_CORE')
    vr = root_comp.op('VR_OUT')
    for comp, name, key in (
        (ai, 'Diffusionresolution', 'diffusion_resolution'),
        (ai, 'Diffusionfps', 'diffusion_fps'),
        (ai, 'Geometryresolution', 'geometry_resolution'),
        (ai, 'Geometryfps', 'geometry_fps'),
        (world, 'Pointbudget', 'point_budget'),
        (vr, 'Targetfps', 'vr_fps'),
    ):
        if key in state:
            try:
                _set(comp, name, int(state[key]))
            except (TypeError, ValueError):
                pass

    completion = root_comp.op('COMPLETION/switch_completion')
    if completion is not None:
        index = {'fog':0, 'procedural':1, 'hybrid':2}.get(state['completion'], 2)
        _set(completion, 'index', index)

    # Roles are declarative. Integrators should use these Enabled parameters to
    # gate cooking after their real networks have been installed.
    # The planner deliberately emits ROLE=world + TOPOLOGY=single for the
    # unified one-process show. In that combination the world process owns AI.
    ai_on = (state['role'] in ('standalone', 'ai') or
             (state['role'] == 'world' and state['topology'] == 'single'))
    world_on = state['role'] in ('standalone', 'world')
    install_on = world_on and state['experience'] in ('installation', 'combined')
    vr_on = world_on and state['experience'] in ('vr', 'combined')
    _set(root_comp.op('AI_PIPELINE'), 'Enabled', ai_on)
    _set(root_comp.op('WORLD_CORE'), 'Enabled', world_on)
    _set(root_comp.op('INSTALLATION_OUT'), 'Enabled', install_on)
    _set(root_comp.op('VR_OUT'), 'Enabled', vr_on)
    state.update({'ai_active':ai_on, 'world_active':world_on,
                  'installation_active':install_on, 'vr_active':vr_on})
    _write_state(root_comp, state)
    if dashboard is not None:
        _set(dashboard, 'Status', '%s / %s / %s / %s / %s' % (
            state['role'], state['topology'], state['experience'],
            state['completion'], state['tier']))
    try:
        root_comp.store('runtime_state', dict(state))
    except Exception:
        pass
    print('[FlexGPU] runtime: %s' % state)
    return state

def safe_reset(root_comp=None):
    root_comp = root_comp or op('/project1/flexgpu')
    return apply(root_comp, {'role':'world', 'experience':'installation',
                             'completion':'fog'})

class FlexGpuRuntimeExt(object):
    def __init__(self, ownerComp):
        self.ownerComp = ownerComp
    def Apply(self):
        return apply(self.ownerComp)
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
        return existing
    if page is None:
        return None
    method = getattr(page, "append%s" % kind, None)
    if method is None:
        return None
    try:
        result = method(name, label=name)
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
    _text(ai, "README", "Placeholder contract: publish generated_rgb and generated_position to WORLD_BUS_IN.\nNo model is loaded by this scaffold.", report)
    _table(ai, "output_contract", [["output", "family", "format"],
        ["generated_rgb", "TOP", "RGBA16F or RGBA8"],
        ["generated_position", "TOP", "XYZ + valid alpha, RGBA32F"]], report)

    world = _ensure(flexgpu, "baseCOMP", "WORLD_CORE", report)
    _style(world, -780, 160, colors["world"], "World owner: standalone/world; sensor, interaction and particles", 230, 110)
    _add_enabled(world, True)
    _custom(world, _page(world, "Quality"), "Int", "Pointbudget", int(defaults["point_budget"]))
    _text(world, "README", "Placeholder for depth-sensor ingest, calibration, forces and the persistent GPU point simulation.", report)

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
    _custom(dashboard, control, "Menu", "Topology", str(defaults["topology"]), ("single", "dual"))
    _custom(dashboard, control, "Menu", "Experience", str(defaults["experience"]), ("installation", "vr", "combined"))
    _custom(dashboard, control, "Menu", "Completion", str(defaults["completion"]), ("fog", "procedural", "hybrid"))
    _custom(dashboard, control, "Menu", "Tier", str(defaults["tier"]), ("3080ti_16gb", "4090", "5090"))
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
        _set_par(execute, "active", True)
    else:
        _text(startup, "startup_callbacks_SOURCE", STARTUP_CALLBACKS, report)
    _table(startup, "environment_contract", [["variable", "values", "meaning"],
        ["FLEXGPU_ROLE", "standalone|world|ai", "one-process show or split role"],
        ["FLEXGPU_TOPOLOGY", "single|dual_local|dual_network", "single: world role also owns AI"],
        ["FLEXGPU_CONFIG", "JSON path", "runtime profile; explicit env values win"],
        ["FLEXGPU_EXPERIENCE", "installation|vr|combined", "active output module(s)"],
        ["FLEXGPU_COMPLETION", "fog|procedural|hybrid", "view-completion policy"],
        ["FLEXGPU_TIER", "3080ti_16gb|4090|5090", "performance preset label"]], report)
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

    _text(flexgpu, "README_FIRST", "FLEXGPU STARTER SHELL\nRole=standalone runs AI + world/show in one process.\nRole=ai and role=world split those jobs while preserving the same WorldBus contract.\nRun STARTUP/runtime_helpers to apply FLEXGPU_* environment variables.", report)
    _table(flexgpu, "bootstrap_manifest", [["field", "value"],
        ["build_version", BUILD_VERSION], ["root", ROOT_PATH],
        ["config_path", config_path or "<defaults>"],
        ["managed_scope", ROOT_PATH + " only"],
        ["unknown_nodes", "preserved"]], report)
    try:
        flexgpu.store("bootstrap_report", report.as_dict())
    except Exception:
        pass


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

    # Apply build-time defaults now; the Execute DAT reapplies runtime env on start.
    runtime_dat = _child(_child(flexgpu, "STARTUP"), "runtime_helpers")
    if runtime_dat is not None:
        try:
            runtime_dat.module.apply(flexgpu, {
                "role": _lookup(config, "role", DEFAULTS["role"]),
                "topology": _lookup(config, "topology", DEFAULTS["topology"]),
                "experience": _lookup(config, "experience", DEFAULTS["experience"]),
                "completion": _lookup(config, "completion", DEFAULTS["completion"]),
                "tier": _lookup(config, "tier", DEFAULTS["tier"]),
            })
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
