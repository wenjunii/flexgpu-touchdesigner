"""Explicit, rollback-gated 5090 podcast-data/single-host migration.

No full world build, model start, environment/config change or TOE save. Private
components are inspected only at their public roots; deletion is restricted to
the two separately reviewed stopped legacy generators and their docked DATs.
"""
from pathlib import Path
import ast
import hashlib
import json
import math
import re

try:
    import runtime_pipeline as rp
    import streamdiffusion_host as host_builder
    import streamdiffusion_host_runtime as host_runtime
    import bootstrap_project as bootstrap
    from update_combined_storytelling_3080 import read_edition  # read-only data verifier
    from upgrade_streamdiffusion_host import _parameters
except ImportError:
    from touchdesigner import runtime_pipeline as rp
    from touchdesigner import streamdiffusion_host as host_builder
    from touchdesigner import streamdiffusion_host_runtime as host_runtime
    from touchdesigner import bootstrap_project as bootstrap
    from touchdesigner.update_combined_storytelling_3080 import read_edition
    from touchdesigner.upgrade_streamdiffusion_host import _parameters

ADAPTER = 'WORKING_PIPELINE/SOURCES/STREAMDIFFUSION_ADAPTER'
LEGACY = ('StreamDiffusionTD', 'StreamDiffusionTD4')
DOCKS = ('StreamDiffusionTD_callbacks', 'StreamDiffusionTD_callbacks4')
LEGACY_PREVIEW = 'SINGLE_GENERATOR_PREVIEW'
MODES = ('original', 'human_figures', 'balanced')
RETIRED_COLOR = ('Colorenabled', 'Brightness', 'Contrast', 'Gamma', 'Blacklevel',
                 'Opacity', 'Hue', 'Saturation', 'Value', 'Resetcolor')
# Public runtime counters, not user settings. These legacy controls are not
# consistently marked read-only, and naturally change when the timeline cooks.
DERIVED_STATUS = {'ROLE_BRIDGE': ('Frameid', 'Framevalid'),
                  'TEMPORAL_WORLD': ('Deltaseconds', 'Newframe', 'Sourcevalid'),
                  'SENSOR_INTERACTION': ('Sensoragems', 'Sensorframeid')}
LEGACY_STARTUP = '''# Execute DAT callbacks

def _apply():
    root_comp = me.parent().parent()
    module_dat = root_comp.op('STARTUP/runtime_helpers')
    if module_dat is not None:
        return module_dat.module.apply(root_comp, {'tier':'5090'})
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


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def protected_state(root, *, details=False):
    pipeline = root.op('WORKING_PIPELINE')
    adapter = root.op(ADAPTER)
    host = adapter.op(host_builder.HOST_NAME)
    state = {'show': _parameters(pipeline.op('SHOW_CONTROL'),
        host_builder.VALUE_CONTROLS + host_builder.PULSE_CONTROLS +
        host_builder.STATUS_CONTROLS + ('Audioenabled', 'Audiosource'))}
    for node in pipeline.children:
        if node.family == 'COMP' and node.name not in ('SOURCES', 'SHOW_CONTROL'):
            state[node.path] = _parameters(node, DERIVED_STATUS.get(node.name, ()))
    for name in ('MOGE2_BRIDGE', 'DEPTH_ANYTHING_GEOMETRY_BRIDGE'):
        state[name] = _parameters(adapter.op(name),
            ('Sourceactive', 'Sourceepoch', 'Resultvalid', 'Sourceframeid'))
    state['host'] = _parameters(host, ('Source',))  # slot menu becomes read-only after retirement
    payload = host_runtime.selected_payload(host)
    state['payload'] = None if payload is None else (payload.path, _parameters(payload))
    cameras = [c for c in pipeline.op('POINT_RENDER').children if c.name.startswith('CAMERA_')]
    state['cameras'] = {c.path: {p.name: (str(p.mode),
        str(p.val) if str(p.mode).endswith('CONSTANT') else '', str(p.expr),
        str(getattr(p, 'bindExpr', ''))) for p in c.pars()} for c in cameras}
    state['geometry_wires'] = {n: [x.path for x in adapter.op(n).inputs] for n in
        ('MOGE2_BRIDGE', 'DEPTH_ANYTHING_GEOMETRY_BRIDGE', 'OUT_RGB', 'OUT_DEPTH', 'OUT_MASK')}
    return state if details else _digest(state)


def patch_audio_callback(source):
    """Replace only the public audio mapper, never camera/geometry callbacks."""
    def extract(text):
        nodes = [n for n in ast.parse(text).body if isinstance(n, ast.FunctionDef)
                 and n.name == '_apply_audio_controls']
        if len(nodes) != 1:
            raise RuntimeError('Unrecognized public audio callback')
        n = nodes[0]
        return n, text.splitlines(keepends=True)
    old, lines = extract(source)
    old_body = ''.join(lines[old.lineno - 1:old.end_lineno])
    if not all(marker in old_body for marker in ('Audiosource', 'audiosource_switch', '_controls')):
        raise RuntimeError('Public audio callback contract changed')
    new, canonical = extract(rp.SHOW_CONTROL_CALLBACKS)
    replacement = ''.join(canonical[new.lineno - 1:new.end_lineno])
    result = ''.join(lines[:old.lineno - 1]) + replacement + ''.join(lines[old.end_lineno:])
    compile(result, 'show_control_callbacks', 'exec')
    return result


def patch_startup_resolution(source):
    """Restore venue output and saved selections, retaining the 5090 override."""
    target = bootstrap.STARTUP_CALLBACKS.replace(
        'module_dat.module.apply(root_comp, preserve_show_selections=True)',
        "module_dat.module.apply(root_comp, {'tier':'5090'}, preserve_show_selections=True)")
    previous = bootstrap.STARTUP_CALLBACKS_PRE_SELECTION.replace(
        'module_dat.module.apply(root_comp)',
        "module_dat.module.apply(root_comp, {'tier':'5090'})")
    parsed = ast.dump(ast.parse(source))
    if parsed == ast.dump(ast.parse(target)):
        return source
    if parsed not in (ast.dump(ast.parse(LEGACY_STARTUP)), ast.dump(ast.parse(previous))):
        raise RuntimeError('Unrecognized public startup callback; preserve it for review')
    return target


def patch_runtime_show_selections(source):
    """Patch only the public apply preamble; preserve unrelated local helpers."""
    helper = next(n for n in ast.parse(bootstrap.RUNTIME_HELPERS).body
                  if isinstance(n, ast.FunctionDef) and n.name == '_preserve_show_selections')
    lines = bootstrap.RUNTIME_HELPERS.splitlines(keepends=True)
    addition = ''.join(lines[helper.lineno - 1:helper.end_lineno])
    old_signature = 'def apply(root_comp=None, overrides=None, inherit_environment=True):'
    new_signature = old_signature.replace('True):', 'True, preserve_show_selections=False):')
    old_preamble = '    if overrides:\n        state.update(overrides)\n    _materialize(state)'
    new_preamble = old_preamble.replace('    _materialize(state)',
        '    if preserve_show_selections:\n        _preserve_show_selections(root_comp, state)\n    _materialize(state)')
    old_tick = '    if runtime is None:\n        apply(root_comp)\n        runtime = _runtime(root_comp)'
    new_tick = old_tick.replace('apply(root_comp)', 'apply(root_comp, preserve_show_selections=True)')
    ast.parse(source)
    if addition in source and new_signature in source and new_preamble in source:
        result = source
    elif ('_preserve_show_selections' in source or source.count(old_signature) != 1
            or source.count(old_preamble) != 1):
        raise RuntimeError('Unrecognized public runtime apply preamble; preserve it for review')
    else:
        result = source.replace(old_signature, addition + '\n' + new_signature, 1)
        result = result.replace(old_preamble, new_preamble, 1)
    if result.count(old_tick) == 1:
        result = result.replace(old_tick, new_tick, 1)
    elif result.count(new_tick) != 1:
        raise RuntimeError('Unrecognized public runtime tick initialization; preserve it for review')
    compile(result, 'runtime_helpers', 'exec')
    return result


def preflight(root, podcast_root, backup_path, project):
    if root is None or root.path != '/project1/flexgpu':
        raise RuntimeError('Verified FlexGPU root required')
    if host_runtime.machine_tag(str(project.name)) != '5090':
        raise RuntimeError('Only a 5090 destination may use this migration')
    folder = Path(project.folder).resolve()
    current = folder / project.name
    backup = Path(backup_path).resolve()
    if (backup.parent != folder or backup == current or backup.suffix.lower() != '.toe'
            or host_runtime.machine_tag(backup.name) != '5090'
            or not backup.is_file() or backup.stat().st_size < 1024 or not current.is_file()):
        raise RuntimeError('A distinct saved same-folder 5090 rollback is required')
    pipeline = root.op('WORKING_PIPELINE')
    adapter = root.op(ADAPTER)
    host = adapter.op(host_builder.HOST_NAME)
    if (pipeline.op('SHOW_CONTROL').par.Qualityprofile.eval() != '5090' or
            any(adapter.op(n).par.Profile.eval() != '5090' for n in
                ('MOGE2_BRIDGE', 'DEPTH_ANYTHING_GEOMETRY_BRIDGE'))):
        raise RuntimeError('Preserve canonical 5090 Show Control and both bridge profiles')
    if host.fetch('managed_owner', '') != host_builder.OWNER:
        raise RuntimeError('Managed host required')
    if host.par.Source.eval() != 'slot':
        raise RuntimeError('Select the intended host generator before migration')
    if adapter.fetch('receiver_playlist46_installed', False):
        raise RuntimeError('Receiver edition is already installed; validate it instead')
    selected = host_runtime.selected_payload(host)
    generators = [adapter.op(n) for n in LEGACY if adapter.op(n) is not None]
    if selected is not None:
        generators.append(selected)
    if any(host_runtime.server_flags(g) != (False, False) for g in generators):
        raise RuntimeError('All native server/stream flags must be confirmed OFF')
    for i, name in enumerate(LEGACY, 1):
        node = adapter.op(name)
        if node is not None and (node.type != 'container' or node.parent() != adapter or
                any(x.name not in ('color_switch_' + str(i), 'color_level_' + str(i))
                    or x.parent() != adapter for x in node.outputs)):
            raise RuntimeError('Unexpected legacy type or consumer: ' + name)
    for name, owner in zip(DOCKS, LEGACY):
        node = adapter.op(name)
        if node is not None and (node.type != 'text' or node.dock != adapter.op(owner)):
            raise RuntimeError('Unexpected legacy docked DAT: ' + name)
    for name in ('color_level_1', 'color_level_2'):
        node = adapter.op(name)
        if node is not None and node.type != 'level':
            raise RuntimeError('Unexpected retained legacy TOP: ' + name)
    preview = adapter.op(LEGACY_PREVIEW)
    if preview is not None and (preview.type != 'select' or
            preview.fetch('managed_owner', '') != 'flexgpu-podcast46-5090'):
        raise RuntimeError('Unrecognized single-generator preview')
    data_root, manifest, bindings, plans = read_edition(podcast_root)
    source = (data_root / 'touchdesigner/execute_callbacks.py').read_text(encoding='utf-8')
    host_builder.single_generator_controller_guard(host_builder.podcast_timeline_guard(source))
    patch_audio_callback(pipeline.op('SHOW_CONTROL/show_control_callbacks').text)
    patch_startup_resolution(root.op('STARTUP/startup_callbacks').text)
    patch_runtime_show_selections(root.op('STARTUP/runtime_helpers').text)
    if bool(host.par.Streamenabled.eval()) or bool(host.par.Followpodcast.eval()):
        raise RuntimeError('Disconnect RGB and turn audio prompt following off before migration')
    return {'status': 'preflight_pass', 'machine': '5090',
        'stories': manifest['story_count'], 'scenes_per_path': 619, 'captions_per_path': 2862,
        'legacy_roots': [n for n in LEGACY if adapter.op(n) is not None],
        'settings_digest': protected_state(root),
        'backup_sha256': hashlib.sha256(backup.read_bytes()).hexdigest()}


def _parameter(owner, page, kind, name, label=None):
    p = getattr(owner.par, name, None)
    return p if p is not None else getattr(page, 'append' + kind)(name, label=label or name)[0]


def _page(owner, name):
    return next((p for p in owner.customPages if p.name == name), None) or owner.appendCustomPage(name)


def _expr(p, expression):
    p.expr = expression
    p.mode = type(p.mode).EXPRESSION


def playlist_event(owner, parameter, lookup, *, pulse=False):
    control = owner.op('show_control')
    controller = owner.op('execute_callbacks').module.get_controller()
    seq = controller.sequencer
    name = parameter.name
    if name == 'Storychoice':
        number = int(str(parameter.eval()).removeprefix('story'))
    elif name in ('Previousstory', 'Nextstory'):
        milliseconds = seq.navigate(owner.par.Playheadsec.eval() * 1000,
            -1 if name == 'Previousstory' else 1, loop=bool(control.par.Loopplaylist.eval()))
        number = seq.story_at(milliseconds)['number']
    elif name == 'Restartplaylist':
        control.par.Play = False
        controller.advance_seed_loop()
        number = 1
    elif name == 'Loopplaylist':
        lookup('/local/time').par.rangelimit = 'loop' if control.par.Loopplaylist.eval() else 'hold'
        return
    else:
        controller.update(float(owner.par.Playheadsec.eval()))
        return
    if not 1 <= number <= len(seq.stories):
        raise ValueError('Story selection outside the approved playlist')
    story = seq.stories[number - 1]
    project = rp._symbol('project')
    lookup('/project1').time.frame = math.ceil(story['start_ms'] / 1000 * project.cookRate) + 1
    lookup('/local/time').par.play = bool(control.par.Play.eval())
    controller.update(float(owner.par.Playheadsec.eval()))


def upgrade(root, podcast_root, backup_path, *, apply=False, confirm_legacy=False):
    project = rp._symbol('project')
    if str(rp._symbol('app').build) != '2025.32820':
        raise RuntimeError('Use the reviewed TD 2025.32820 build')
    report = preflight(root, podcast_root, backup_path, project)
    if not apply:
        return report
    if report['legacy_roots'] and confirm_legacy is not True:
        raise RuntimeError('Explicit reviewed legacy deletion confirmation required')
    data_root, manifest, bindings, _ = read_edition(podcast_root)
    pipeline = root.op('WORKING_PIPELINE')
    owner = root.op(ADAPTER)
    control = owner.op('show_control')
    host = owner.op(host_builder.HOST_NAME)
    owner.par.Enabled = False
    control.par.Play = False
    control.par.Audioenabled = False
    for dat in (owner.op('execute_callbacks'), owner.op('parameter_callbacks'), control.op('control_callbacks')):
        dat.par.active = False
    owner.op('audio_out').par.active = False
    host.op('host_runtime').module.disarm(host, 'approved 5090 podcast migration')
    for name in LEGACY + DOCKS:
        node = owner.op(name)  # TD can remove docked DATs together with their owner.
        if node is not None:
            node.destroy()
    # Retain the public legacy test chains as previews of the single host.
    # A Select TOP crosses the network boundary; the host is a Base COMP and
    # cannot itself be wired to a TOP. No chain feeds back into the host.
    preview = owner.op(LEGACY_PREVIEW) or owner.create(rp._symbol('selectTOP'), LEGACY_PREVIEW)
    preview.store('managed_owner', 'flexgpu-podcast46-5090')
    preview.par.top = host.op('OUT_RAW').path
    for name in ('color_level_1', 'color_level_2'):
        node = owner.op(name)
        if node is not None and not node.inputs:
            node.setInputs([preview])
    host.store('legacy_retired', True)
    host.store('legacy_retirement_backup', str(Path(backup_path).resolve()))
    for connector in host.inputConnectors:
        connector.disconnect()
    owner.par.Streamdiffusionpath.val = ''
    owner.par.Streamdiffusionpath.readOnly = True
    page = _page(owner, 'Playlist')
    for name, path in bindings.items():
        _parameter(owner, page, 'File', name).val = str(path)
    for name, key in (('Canonicalvisuals', 'Scenejson'), ('Canonicalhumanvisuals', 'Humanfigurejson')):
        _parameter(owner, page, 'File', name).val = str(bindings[key])
    local_state = Path(project.folder).parent / '.flexgpu' / 'podcast46-5090'
    for name, filename in (('Livecontroljson', 'control.json'), ('Livestatusjson', 'status.json'),
                           ('Livecurrentjson', 'current.json')):
        _parameter(owner, page, 'File', name).val = str(local_state / filename)
    _parameter(owner, page, 'Str', 'Currentnarrationid')
    _parameter(owner, page, 'Toggle', 'Semanticneutralready').val = False
    _parameter(owner, page, 'Float', 'Semanticneutralmix').val = 0.0
    variations = _page(control, 'Local Variations')
    for node, node_page in ((owner, page), (control, variations)):
        for name, choices, labels, value in (
            ('Visualpath', MODES, ('Original Story Visuals', 'Human Figures', 'Balanced Memories'), 'original'),
            ('Audiosource', ('voices', 'soundscape', 'combined'), ('Narration Only', 'Ambient Only', 'Narration + Ambient'), 'combined'),
            ('Runtimeprofile', ('3080_safe', '5090_live'), ('Canonical Cached Playback', 'Live Local Variations'), '3080_safe')):
            p = _parameter(node, node_page, 'Menu', name)
            p.menuNames, p.menuLabels = list(choices), list(labels)
            p.val = value
    control.par.Runtimeprofile.readOnly = True
    for name, value, label in (
        ('Livevariations', False, 'Live Local Variations'),
        ('Narrationvariation', True, 'Narration Text Variation'),
        ('Speechvariation', True, 'Local Speech Variation'),
        ('Visualvariation', True, 'Visual Prompt Variation'),
        ('Interpretivecasting', True, 'Interpretive Casting'),
        ('Randomvariationseeds', True, 'Random Variation Seeds')):
        _parameter(control, variations, 'Toggle', name, label).val = value
    control.par.Livevariations.readOnly = True
    _parameter(control, variations, 'Int', 'Variationseed', 'Fixed Variation Seed').val = 201312
    _parameter(control, variations, 'Pulse', 'Regeneratenext', 'Regenerate Next Loop').enable = False
    control.par.Randomseeds = False
    control.par.Crossfadesec = 3
    control.par.Crossfadesec.label = 'Scene Interpolation Seconds'
    for name in RETIRED_COLOR:
        p = getattr(control.par, name, None)
        if p is not None:
            p.enable, p.readOnly = False, True
    owner.op('execute_callbacks').text = host_builder.single_generator_controller_guard(
        host_builder.podcast_timeline_guard((data_root / 'touchdesigner/execute_callbacks.py').read_text(encoding='utf-8')))
    owner.op('parameter_callbacks').text = (data_root / 'touchdesigner/parameter_callbacks.py').read_text(encoding='utf-8')
    owner.op('parameter_callbacks').par.pars = 'Playheadsec Currentnarrationid Canonicalvisuals Canonicalhumanvisuals'
    control.op('control_callbacks').text = (data_root / 'touchdesigner/show_control_callbacks.py').read_text(encoding='utf-8') + '''

# FLEXGPU_5090_CANONICAL_ONLY
_canonical_write_live_control = _write_live_control
def _write_live_control(*, regenerate=False):
    parent().par.Runtimeprofile = '3080_safe'
    parent().par.Livevariations = False
    return _canonical_write_live_control(regenerate=False)
'''
    for index, (name, parameter) in enumerate((('voices_only_audio', 'Audiofile'),
            ('soundscape_audio', 'Soundscapeaudiofile'), ('combined_audio', 'Combinedaudiofile'))):
        node = owner.op(name) or owner.create(rp._symbol('audiofileinCHOP'), name)
        _expr(node.par.file, 'parent().par.' + parameter)
        node.par.playmode, node.par.play, node.par.repeat = 'locked', True, False
        node.outputConnectors[0].connect(owner.op('audiosource_switch').inputConnectors[index])
    playlist = _page(control, 'Playlist')
    selector = _parameter(control, playlist, 'Menu', 'Storychoice', 'Jump to Story')
    selector.menuNames = ['story' + str(s['number']) for s in manifest['stories']]
    selector.menuLabels = [str(s['number']).zfill(3) + '. ' + s['title'] for s in manifest['stories']]
    for name, label in (('Previousstory', 'Previous Story'), ('Nextstory', 'Next Story'),
                        ('Restartplaylist', 'Restart Playlist (Pause)')):
        _parameter(control, playlist, 'Pulse', name, label)
    _parameter(control, playlist, 'Toggle', 'Loopplaylist', 'Loop Full Playlist').val = True
    fade = _parameter(control, playlist, 'Float', 'Storyfadesec', 'Story / Loop Interpolation Seconds')
    fade.min, fade.max, fade.normMin, fade.normMax = 0, 30, 0, 8
    fade.clampMin = fade.clampMax = True
    fade.val = 3
    gap = _parameter(control, playlist, 'Float', 'Abstractfadesec', 'Gap Colour Off (Hold Image)')
    gap.val, gap.readOnly, gap.enable = 0, True, False
    seq = "parent().op('execute_callbacks').module.get_controller().sequencer"
    story = seq + '.story_at(parent().par.Playheadsec.eval()*1000)'
    for kind, name, label, expression in (
        ('Str', 'Currentstory', 'Now Playing', "str(" + story + "['number'])+' / 136 - '+" + story + "['title']"),
        ('Float', 'Storyelapsed', 'Story Elapsed Seconds', 'max(0,parent().par.Playheadsec.eval()-' + story + "['start_ms']/1000)"),
        ('Float', 'Storyduration', 'Story Duration Seconds', story + "['duration_ms']/1000"),
        ('Float', 'Playlistelapsed', 'Playlist Elapsed Seconds', 'parent().par.Playheadsec.eval()')):
        p = _parameter(control, playlist, kind, name, label)
        _expr(p, expression)
        p.readOnly = True
    _expr(owner.par.Currentnarrationid, "me.op('execute_callbacks').module.get_controller().sequencer.story_at(me.par.Playheadsec.eval()*1000)['narration_id']")
    callback = control.op('playlist_callbacks') or control.create(rp._symbol('parameterexecuteDAT'), 'playlist_callbacks')
    callback.par.active = False
    callback.par.op = control.path
    callback.par.pars = 'Storychoice Previousstory Nextstory Restartplaylist Loopplaylist Storyfadesec Abstractfadesec'
    callback.par.valuechange = callback.par.onpulse = True
    callback.par.valueschanged = False
    callback.text = (
        'import importlib.util, sys\n'
        f'_spec=importlib.util.spec_from_file_location("flexgpu_playlist46_5090",{str(Path(__file__).resolve())!r})\n'
        '_migration=importlib.util.module_from_spec(_spec)\n'
        'sys.modules[_spec.name]=_migration\n_spec.loader.exec_module(_migration)\n'
        'def onValueChange(par,prev):\n    _migration.playlist_event(parent().parent(),par,op)\n'
        'def onPulse(par):\n    _migration.playlist_event(parent().parent(),par,op,pulse=True)\n')
    outer = pipeline.op('SHOW_CONTROL')
    outer.op('show_control_callbacks').text = patch_audio_callback(outer.op('show_control_callbacks').text)
    outer.par.Audiosource.menuNames = ['voices', 'soundscape', 'combined']
    outer.par.Audiosource.menuLabels = ['Narration Only', 'Ambient Only', 'Narration + Ambient']
    outer.par.Audiosource, outer.par.Audioenabled = 'combined', False
    clock = rp._symbol('op')('/local/time')
    clock.par.play = False
    for name in ('start', 'rangestart'):
        getattr(clock.par, name).val = 1
    for name in ('end', 'rangeend'):
        getattr(clock.par, name).val = math.ceil(manifest['duration_ms'] / 1000 * project.cookRate) + 1
    clock.par.rangelimit = 'loop'
    rp._symbol('op')('/project1').time.frame = 1
    owner.op('execute_callbacks').module._CONTROLLER = None
    # Reinstall only the public host. No 3080 retirement helper is invoked.
    host_builder.install(root)
    root.op('STARTUP/runtime_helpers').text = patch_runtime_show_selections(
        root.op('STARTUP/runtime_helpers').text)
    root.op('STARTUP/startup_callbacks').text = patch_startup_resolution(
        root.op('STARTUP/startup_callbacks').text)
    outer.op('show_control_callbacks').module._apply_wall_resolution()
    owner.par.Enabled = True
    controller = owner.op('execute_callbacks').module.get_controller()
    controller.update(0)
    control.par.Storychoice = 'story1'
    for dat in (owner.op('execute_callbacks'), owner.op('parameter_callbacks'),
                control.op('control_callbacks'), callback):
        dat.par.active = True
    control.op('control_callbacks').par.pars = '*'
    owner.op('execute_callbacks').module._synchronize_show_control()
    control.op('control_callbacks').module._write_live_control()
    control.currentPage = 'Playlist'
    after = protected_state(root)
    if after != report['settings_digest']:
        raise RuntimeError('Protected 5090 settings changed: do not save; reopen the rollback')
    if (len(controller.sequencer.stories), len(controller.sequencer.scenes), len(controller.sequencer.captions)) != (136, 619, 2862):
        raise RuntimeError('Reviewed playlist counts differ; do not save')
    owner.store('receiver_playlist46_installed', True)
    owner.store('podcast40_installed', True)
    report.update(status='updated', saved=False, model_servers_started=False,
        protected_settings_preserved=True, selected_generator_preserved=True,
        audio_modes=3, visual_paths=3, legacy_removed=list(LEGACY))
    return report
