"""Install the reviewed podcast.40 playback edition at the FlexGPU boundary.

Runs inside TD 2025.32820. The source home-podcast checkout owns its reviewed
media and sequencer modules; this adapter owns relocation and public wiring.
The legacy panel is retained disabled for rollback. No model is started and
no project is saved. The geometry bridges and private components are retained.
"""
from pathlib import Path
import hashlib
import json
import math
import re
import sys
import wave

ADAPTER_PATH = '/project1/flexgpu/WORKING_PIPELINE/SOURCES/STREAMDIFFUSION_ADAPTER'
EDITION = 'work/storytelling-real-december/continuous-image-hold-136-3080-2026-10-30'
MODES = ('original', 'human_figures', 'balanced')
PROJECT_PATTERN = re.compile(r'FlexShow(?!.*(?:4090|5090)).*podcast.*3080(?:\.\d+)?\.toe', re.I)
MARKER = 'FlexGPU reviewed podcast40 playback'


def read_edition(podcast_root):
    """Check the selected edition before any live parameter changes."""
    root = Path(podcast_root).resolve()
    folder = root / EDITION
    manifest = json.loads((folder / 'playlist.json').read_text(encoding='utf-8'))
    if (manifest.get('kind') != 'approved_continuous_image_hold_playlist'
            or manifest.get('story_count') != 136
            or manifest.get('duration_ms') != 11938720
            or not manifest.get('executable')):
        raise ValueError('Expected the reviewed 136-story continuous-image edition')
    bindings = {'Playlistmanifest': folder / 'playlist.json',
                'Sequencermodule': folder / 'boundary_sequencer.py',
                'Controllermodule': folder / 'td_controller.py'}
    plans = {}
    for mode, parameter in zip(MODES, ('Scenejson', 'Humanfigurejson', 'Balancedjson')):
        entry = manifest['plans'][mode]
        path = (root / entry['path']).resolve()
        if not path.is_relative_to(root):
            raise ValueError('Reviewed plan must stay inside the podcast checkout')
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != entry['sha256']:
            raise ValueError('Reviewed plan hash differs: ' + mode)
        plan = json.loads(raw)
        if not plan.get('hold_visual_gaps_edition') or not plan.get('executable'):
            raise ValueError('Continuous-image executable plan required')
        if (len(plan.get('scenes', [])) != 619
                or len(plan.get('captions', [])) != 2862
                or len(plan.get('playlist_stories', [])) != 136):
            raise ValueError('Reviewed scene, caption, or story counts differ')
        bindings[parameter] = path
        plans[mode] = plan
    for kind, parameter in (('voices_only', 'Audiofile'),
                            ('soundscape_only', 'Soundscapeaudiofile'),
                            ('combined', 'Combinedaudiofile')):
        path = (root / manifest['audio'][kind]).resolve()
        if not path.is_relative_to(root):
            raise ValueError('Reviewed audio must stay inside the podcast checkout')
        with wave.open(str(path), 'rb') as audio:
            if (audio.getnchannels() != 2 or audio.getframerate() != 48000
                    or audio.getnframes() != manifest['duration_ms'] * 48):
                raise ValueError('Audio does not match the reviewed stereo clock')
        bindings[parameter] = path
    for path in list(bindings.values()) + [
            root / 'touchdesigner/show_control_component.py',
            root / 'touchdesigner/show_control_callbacks.py',
            root / 'touchdesigner/execute_callbacks.py',
            root / 'touchdesigner/parameter_callbacks.py']:
        if not path.is_file():
            raise FileNotFoundError(path)
    return root, manifest, bindings, plans


def _page(node, name):
    return next((p for p in node.customPages if p.name == name), None) or node.appendCustomPage(name)


def _parameter(node, page, kind, name, label):
    current = getattr(node.par, name, None)
    return current if current is not None else getattr(page, 'append' + kind)(name, label=label)[0]


def _set(node, name, value):
    getattr(node.par, name).val = value


def _expression(parameter, value):
    parameter.expr = value
    parameter.mode = _symbol('ParMode').EXPRESSION


def _controller(owner):
    return owner.op('execute_callbacks').module.get_controller()


def jump_story(owner, number, lookup):
    story = _controller(owner).sequencer.stories[int(number) - 1]
    lookup('/project1').time.frame = math.ceil(story['start_ms'] / 1000 * _symbol('project').cookRate) + 1
    lookup('/local/time').par.play = bool(owner.op('show_control').par.Play.eval())
    _controller(owner).update(float(owner.par.Playheadsec.eval()))
    return story['number']


def navigate(owner, delta, lookup):
    seq = _controller(owner).sequencer
    milliseconds = seq.navigate(owner.par.Playheadsec.eval() * 1000, delta,
                                loop=bool(owner.op('show_control').par.Loopplaylist.eval()))
    return jump_story(owner, seq.story_at(milliseconds)['number'], lookup)


def playlist_event(owner, parameter, lookup, *, pulse=False):
    control = owner.op('show_control')
    name = parameter.name
    if name == 'Storychoice':
        jump_story(owner, int(str(parameter.eval()).removeprefix('story')), lookup)
    elif name == 'Nextstory':
        navigate(owner, 1, lookup)
    elif name == 'Previousstory':
        navigate(owner, -1, lookup)
    elif name == 'Restartplaylist':
        control.par.Play = False
        _controller(owner).advance_seed_loop()
        jump_story(owner, 1, lookup)
    elif name == 'Loopplaylist':
        lookup('/local/time').par.rangelimit = 'loop' if control.par.Loopplaylist.eval() else 'hold'
    else:
        _controller(owner).update(float(owner.par.Playheadsec.eval()))


def install(podcast_root, *, lookup, types, project_name, build, cook_rate=60):
    if str(build) != '2025.32820' or not PROJECT_PATTERN.fullmatch(str(project_name)):
        raise RuntimeError('Use the combined 3080 podcast TOE in TD 2025.32820')
    owner = lookup(ADAPTER_PATH)
    if owner is None or lookup('/project1/flexgpu/WORKING_PIPELINE/SHOW_CONTROL').par.Qualityprofile.eval() != '3080ti_16gb':
        raise RuntimeError('Verified 3080 FlexGPU root required')
    root, manifest, bindings, _ = read_edition(podcast_root)
    if owner.fetch('podcast40_installed', False):
        raise RuntimeError('Edition already installed; use validate rather than replacing the panel')
    if owner.op('show_control_before_playlist40') is not None:
        raise RuntimeError('A migration backup already exists; inspect the partial migration before retrying')
    stream_paths = str(owner.par.Streamdiffusionpath.eval())
    renderers = [p.strip() for p in re.split(r'[;,\n]+', stream_paths) if p.strip()]
    for name in renderers:
        renderer = owner.op(name)
        for parameter in ('Serveractive', 'Streamactive'):
            if bool(getattr(renderer.par, parameter).eval()):
                raise RuntimeError('Stop the selected StreamDiffusion server before migration')
    required = ('show_control', 'execute_callbacks', 'parameter_callbacks',
                'voices_only_audio', 'soundscape_audio', 'audiosource_switch', 'audio_out')
    required += tuple(f'{stem}_{i}' for stem in ('color_level', 'color_hsv', 'color_switch', 'color_out') for i in (1, 2))
    if any(owner.op(name) is None for name in required):
        raise RuntimeError('Combined podcast public network is incomplete')
    raw_sources = [owner.op(f'color_level_{i}').inputs[0] for i in (1, 2)]
    before_geometry = {n: [i.path for i in owner.op(n).inputs] for n in
                       ('MOGE2_BRIDGE', 'DEPTH_ANYTHING_GEOMETRY_BRIDGE', 'OUT_RGB', 'OUT_DEPTH', 'OUT_MASK')}
    control = owner.op('show_control')
    colors = {n: getattr(control.par, n).eval() for n in
              ('Colorenabled','Brightness','Contrast','Gamma','Blacklevel','Opacity','Hue','Saturation','Value')}
    owner.par.Enabled = False
    control.par.Play = False
    control.par.Audioenabled = False
    lookup('/local/time').par.play = False
    owner.op('audio_out').par.active = False
    for node in (owner.op('execute_callbacks'), owner.op('parameter_callbacks')):
        node.par.active = False
    # Extend the existing panel in place. The generic source installer replaces
    # its panel and restores legacy two-track defaults, so it is unsuitable here.
    legacy = owner.copy(control, name='show_control_before_playlist40')
    legacy.allowCooking = False
    for child in legacy.children:
        if getattr(child.par, 'active', None) is not None:
            child.par.active = False
    control.op('control_callbacks').par.active = False
    control.par.Play = False
    control.par.Audioenabled = False
    for n, value in colors.items():
        _set(control, n, value)
    page = _page(owner, 'Playlist')
    for name, path in bindings.items():
        _parameter(owner, page, 'File', name, name).val = str(path)
    owner.par.Playlistmanifest.readOnly = True
    runtime_page = _page(control, 'Local Variations')
    for node, target_page in ((owner, page), (control, runtime_page)):
        profile = _parameter(node, target_page, 'Menu', 'Runtimeprofile', 'Runtime Profile')
        profile.menuNames = ['3080_safe','5090_live']
        profile.menuLabels = ['RTX 3080 Safe — Canonical','RTX 5090 Live — Local Variations']
    for name in ('Canonicalvisuals','Canonicalhumanvisuals','Livecontroljson','Livestatusjson','Livecurrentjson'):
        _parameter(owner, page, 'File', name, name)
    _parameter(owner, page, 'Str', 'Currentnarrationid', 'Current Narration ID')
    for name, value in (('Livevariations',False),('Narrationvariation',True),('Speechvariation',True),
                        ('Visualvariation',True),('Interpretivecasting',True),('Randomvariationseeds',True)):
        _parameter(control, runtime_page, 'Toggle', name, name).val = value
    _parameter(control, runtime_page, 'Int', 'Variationseed', 'Fixed Variation Seed').val = 201312
    _parameter(control, runtime_page, 'Pulse', 'Regeneratenext', 'Regenerate Next Loop')
    for name in ('Canonicalvisuals', 'Canonicalhumanvisuals'):
        _set(owner, name, str(bindings['Scenejson' if name == 'Canonicalvisuals' else 'Humanfigurejson']))
    for name, filename in (('Livecontroljson','control.json'),('Livestatusjson','status.json'),('Livecurrentjson','current.json')):
        _set(owner, name, str(Path(__file__).resolve().parents[1] / 'runtime/podcast40-playback-3080' / filename))
    for node in (owner, control):
        node.par.Visualpath.menuNames = list(MODES)
        node.par.Visualpath.menuLabels = ['Original Story Visuals','Human Figures','Balanced Memories']
        node.par.Audiosource.menuNames = ['voices','soundscape','combined']
        node.par.Audiosource.menuLabels = ['Narration Only','Ambient Only','Narration + Ambient']
        node.par.Visualpath = 'balanced'
        node.par.Audiosource = 'combined'
        node.par.Runtimeprofile = '3080_safe'
    control.par.Runtimeprofile.readOnly = True
    control.par.Livevariations = False
    control.par.Livevariations.readOnly = True
    control.par.Randomseeds = False
    control.par.Crossfadesec = 3
    control.par.Crossfadesec.label = 'Scene Interpolation Seconds'
    for name, source in (('execute_callbacks','execute_callbacks.py'),('parameter_callbacks','parameter_callbacks.py')):
        owner.op(name).text = (root / 'touchdesigner' / source).read_text(encoding='utf-8')
    owner.op('parameter_callbacks').par.pars = 'Playheadsec Currentnarrationid Canonicalvisuals Canonicalhumanvisuals'
    # Load the public callback directly: the source .40 shim assumes project.folder
    # is the podcast checkout, which is false inside this combined installation.
    control.op('control_callbacks').text = (root / 'touchdesigner/show_control_callbacks.py').read_text(encoding='utf-8')
    for name, parameter in (('voices_only_audio','Audiofile'),('soundscape_audio','Soundscapeaudiofile'),('combined_audio','Combinedaudiofile')):
        node = owner.op(name) or owner.create(types['audiofileinCHOP'], name)
        node.comment = MARKER
        _expression(node.par.file, 'parent().par.' + parameter)
        node.par.playmode = 'locked'
        node.par.play = True
        node.par.repeat = False
        node.outputConnectors[0].connect(owner.op('audiosource_switch').inputConnectors[
            ('voices_only_audio','soundscape_audio','combined_audio').index(name)])
    _parameter(owner, page, 'Toggle', 'Semanticneutralready', 'Verified Semantic Output Rig').val = False
    _parameter(owner, page, 'Float', 'Semanticneutralmix', 'Semantic Neutral Mix').val = 0
    neutral = owner.op('semantic_neutral_colour') or owner.create(types['constantTOP'], 'semantic_neutral_colour')
    neutral.comment = MARKER
    neutral.par.colorr = .24
    neutral.par.colorg = .28
    neutral.par.colorb = .33
    for i, raw in enumerate(raw_sources, 1):
        blend = owner.op(f'semantic_neutral_output_{i}') or owner.create(types['switchTOP'], f'semantic_neutral_output_{i}')
        blend.comment = MARKER
        raw.outputConnectors[0].connect(blend.inputConnectors[0])
        neutral.outputConnectors[0].connect(blend.inputConnectors[1])
        blend.par.blend = True
        _expression(blend.par.index, 'parent().par.Semanticneutralmix')
        blend.outputConnectors[0].connect(owner.op(f'color_level_{i}').inputConnectors[0])
        blend.outputConnectors[0].connect(owner.op(f'color_switch_{i}').inputConnectors[0])
        owner.op(f'color_hsv_{i}').outputConnectors[0].connect(owner.op(f'color_switch_{i}').inputConnectors[1])
        edge = owner.op(f'story_edge_fade_{i}') or owner.create(types['levelTOP'], f'story_edge_fade_{i}')
        edge.comment = MARKER
        edge.par.opacity = edge.par.brightness1 = 1
        owner.op(f'color_out_{i}').outputConnectors[0].connect(edge.inputConnectors[0])
        edge.outputConnectors[0].connect(owner.op(f'null{i}').inputConnectors[0])
    owner.par.Semanticneutralready = True
    playlist_page = _page(control, 'Playlist')
    selector = _parameter(control, playlist_page, 'Menu', 'Storychoice', 'Jump to Story')
    selector.menuNames = [f'story{s["number"]}' for s in manifest['stories']]
    selector.menuLabels = [f'{s["number"]:03d}. {s["title"]}' for s in manifest['stories']]
    for name, label in (('Previousstory','Previous Story'),('Nextstory','Next Story'),('Restartplaylist','Restart Playlist (Pause)')):
        _parameter(control, playlist_page, 'Pulse', name, label)
    _parameter(control, playlist_page, 'Toggle', 'Loopplaylist', 'Loop Full Playlist').val = True
    fade = _parameter(control, playlist_page, 'Float', 'Storyfadesec', 'Story / Loop Interpolation Seconds')
    fade.min, fade.max, fade.normMin, fade.normMax = 0, 30, 0, 8
    fade.clampMin = fade.clampMax = True
    fade.val = 3
    gap = _parameter(control, playlist_page, 'Float', 'Abstractfadesec', 'Gap Colour Off (Hold Image)')
    gap.val, gap.readOnly, gap.enable = 0, True, False
    seq = "parent().op('execute_callbacks').module.get_controller().sequencer"
    current = f'{seq}.story_at(parent().par.Playheadsec.eval()*1000)'
    for kind, name, label, expression in (
            ('Str','Currentstory','Now Playing',f"str({current}['number'])+' / 136 — '+{current}['title']"),
            ('Float','Storyelapsed','Story Elapsed Seconds',f"max(0,parent().par.Playheadsec.eval()-{current}['start_ms']/1000)"),
            ('Float','Storyduration','Story Duration Seconds',f"{current}['duration_ms']/1000"),
            ('Float','Playlistelapsed','Playlist Elapsed Seconds','parent().par.Playheadsec.eval()')):
        parameter = _parameter(control, playlist_page, kind, name, label)
        _expression(parameter, expression)
        parameter.readOnly = True
    _expression(owner.par.Currentnarrationid, "me.op('execute_callbacks').module.get_controller().sequencer.story_at(me.par.Playheadsec.eval()*1000)['narration_id']")
    callback = control.create(types['parameterexecuteDAT'], 'playlist_callbacks')
    callback.par.active = False
    callback.par.op = control.path
    callback.par.pars = 'Storychoice Previousstory Nextstory Restartplaylist Loopplaylist Storyfadesec Abstractfadesec'
    callback.par.valuechange = callback.par.onpulse = True
    callback.par.valueschanged = False
    callback.text = (
        'import importlib.util, sys\n'
        f'_spec=importlib.util.spec_from_file_location("flexgpu_playlist40_callbacks",{str(Path(__file__).resolve())!r})\n'
        '_migration=importlib.util.module_from_spec(_spec)\n'
        'sys.modules[_spec.name]=_migration\n_spec.loader.exec_module(_migration)\n'
        'def onValueChange(par,prev):\n    _migration.playlist_event(parent().parent(),par,op)\n'
        'def onPulse(par):\n    _migration.playlist_event(parent().parent(),par,op,pulse=True)\n')
    callback.nodeX, callback.nodeY = 240, -200
    legacy.nodeX, legacy.nodeY = control.nodeX, control.nodeY - 320
    for name, x, y in (('combined_audio', 0, -600),
                       ('semantic_neutral_colour', 920, -700),
                       ('semantic_neutral_output_1', 1050, -400),
                       ('semantic_neutral_output_2', 1050, -600),
                       ('story_edge_fade_1', 1650, -400),
                       ('story_edge_fade_2', 1650, -600)):
        owner.op(name).nodeX, owner.op(name).nodeY = x, y
    outer = lookup('/project1/flexgpu/WORKING_PIPELINE/SHOW_CONTROL')
    outer.par.Audiosource.menuNames = ['voices','soundscape','combined']
    outer.par.Audiosource.menuLabels = ['Narration Only','Ambient Only','Narration + Ambient']
    outer.par.Audiosource = 'combined'
    outer.par.Audioenabled = False
    # Refresh only this public DAT to add capability-based third-track routing.
    import runtime_pipeline
    outer.op('show_control_callbacks').text = runtime_pipeline.SHOW_CONTROL_CALLBACKS
    clock = lookup('/local/time')
    for n in ('start','rangestart'):
        _set(clock, n, 1)
    for n in ('end','rangeend'):
        _set(clock, n, math.ceil(manifest['duration_ms'] / 1000 * float(cook_rate)) + 1)
    clock.par.rangelimit = 'loop'
    lookup('/project1').time.frame = 1
    owner.op('execute_callbacks').module._CONTROLLER = None
    owner.par.Enabled = True
    _controller(owner).update(0)
    control.par.Storychoice = 'story1'
    for node in (owner.op('execute_callbacks'), owner.op('parameter_callbacks'), control.op('control_callbacks'), callback):
        node.par.active = True
    owner.op('execute_callbacks').module._synchronize_show_control()
    control.op('control_callbacks').module._write_live_control()
    control.currentPage = 'Playlist'
    control.current = True
    after_geometry = {n: [i.path for i in owner.op(n).inputs] for n in before_geometry}
    if before_geometry != after_geometry or str(owner.par.Streamdiffusionpath.eval()) != stream_paths:
        raise RuntimeError('Geometry wiring or renderer selection changed unexpectedly')
    owner.store('podcast40_installed', True)
    return dict(status='updated', project=str(project_name), stories=136,
                duration_ms=manifest['duration_ms'], visual_paths=3, audio_modes=3,
                saved=False, model_servers_started=False, geometry_connections_preserved=True,
                legacy_panel=legacy.path, manifest=str(bindings['Playlistmanifest']))


def _symbol(name):
    import td
    symbols = vars(sys.modules['__main__'])
    import builtins
    return getattr(td, name, None) or symbols.get(name) or getattr(builtins, name)


def install_live(podcast_root):
    names = ('baseCOMP','tableDAT','parameterexecuteDAT','audiofileinCHOP','constantTOP','switchTOP','levelTOP')
    return install(podcast_root, lookup=_symbol('op'), types={n:_symbol(n) for n in names},
                   project_name=_symbol('project').name, build=_symbol('app').build,
                   cook_rate=_symbol('project').cookRate)
