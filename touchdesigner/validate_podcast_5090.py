"""Muted, reversible public playback audit for the 5090 podcast receiver.

No generator/worker starts, private component traversal, model changes or saves.
Real image generation, listening and venue acceptance remain separate checks.
"""
import json
import math
from pathlib import Path

try:
    import sync_combined_podcast_5090 as migration
except ImportError:
    from touchdesigner import sync_combined_podcast_5090 as migration


def validate(root, podcast_root, *, lookup, report_path=None):
    project = migration.rp._symbol('project')
    owner = root.op(migration.ADAPTER)
    control = owner.op('show_control')
    host = owner.op('STREAMDIFFUSION_HOST')
    main = root.op('WORKING_PIPELINE/SHOW_CONTROL')
    if migration.host_runtime.machine_tag(project.name) != '5090' or main.par.Qualityprofile.eval() != '5090':
        raise RuntimeError('Verified 5090 destination required')
    if not owner.fetch('receiver_playlist46_installed', False):
        raise RuntimeError('Complete the rollback-gated receiver migration first')
    if (migration.host_runtime.server_flags(migration.host_runtime.selected_payload(host)) != (False, False)
            or host.par.Followpodcast.eval() or host.par.Streamenabled.eval()
            or control.par.Play.eval() or control.par.Audioenabled.eval()
            or main.par.Audioenabled.eval()
            or main.par.Audiosource.eval() != control.par.Audiosource.eval()):
        raise RuntimeError('Stop, pause, mute and disconnect before this data-only audit')
    _, manifest, _, _ = migration.read_edition(podcast_root)
    checks = []
    def check(name, value):
        checks.append({'check': name, 'passed': bool(value)})
    names = ('Audiosource', 'Visualpath', 'Randomseeds', 'Crossfadesec',
             'Storyfadesec', 'Loopplaylist', 'Storychoice')
    before = {n: getattr(control.par, n).eval() for n in names}
    before_frame = lookup('/project1').time.frame
    protected = migration.protected_state(root)
    public_callbacks = control.op('control_callbacks').module
    def controller():
        return owner.op('execute_callbacks').module.get_controller()
    try:
        check('canonical cached playback', control.par.Runtimeprofile.eval() == '3080_safe'
              and not control.par.Livevariations.eval())
        check('gap colour disabled', control.par.Abstractfadesec.eval() == 0
              and not control.par.Abstractfadesec.enable)
        for mode in migration.MODES:
            control.par.Visualpath = mode
            public_callbacks.onValueChange(control.par.Visualpath, before['Visualpath'])
            current = controller()
            seq = current.sequencer
            check(mode + ':counts', (len(seq.stories), len(seq.scenes), len(seq.captions)) == (136, 619, 2862))
            check(mode + ':duration', seq.duration_ms == manifest['duration_ms'])
            check(mode + ':all story openings', all(seq.story_at(s['start_ms'])['number'] == s['number'] for s in seq.stories))
            for index, scene in enumerate(seq.scenes[1:], 1):
                fade = seq._effective_crossfade_ms(index, scene['duration_ms'], 3000)
                layers = seq.at(scene['start_ms'] + fade // 2, crossfade_ms=3000).prompt_layers
                check(f'{mode}:join{index}', fade > 0 and len(layers) == 2
                      and all(0 < layer.weight < 1 for layer in layers))
            for number in (1, 68, 136):
                control.par.Storychoice = 'story' + str(number)
                migration.playlist_event(owner, control.par.Storychoice, lookup)
                check(f'{mode}:native story{number}',
                      seq.story_at(owner.par.Playheadsec.eval() * 1000)['number'] == number
                      and owner.par.Semanticneutralmix.eval() == 0)
            current.update((seq.duration_ms - 20) / 1000)
            current.update(0)
            check(mode + ':loop carry', len(seq.at(1000, crossfade_ms=3000).prompt_layers) == 2)
        for source, index in (('voices', 0), ('soundscape', 1), ('combined', 2)):
            control.par.Audiosource = source
            public_callbacks.onValueChange(control.par.Audiosource, before['Audiosource'])
            check('podcast audio route:' + source, owner.op('audiosource_switch').par.index.eval() == index)
            main.par.Audiosource = source
            main.op('show_control_callbacks').module._apply_audio_controls()
            check('main audio route:' + source, owner.op('audiosource_switch').par.index.eval() == index
                  and control.par.Audiosource.eval() == source and not owner.op('audio_out').par.active.eval())
        for name in ('voices_only_audio', 'soundscape_audio', 'combined_audio'):
            node = owner.op(name)
            for seconds in (0, 500, 11937):
                lookup('/project1').time.frame = math.ceil(seconds * project.cookRate) + 1
                node.cook(force=True)
                check(f'{name}:decode{seconds}', not node.errors() and node.numChans == 2 and node.rate == 48000)
        control.par.Storychoice = 'story136'
        migration.playlist_event(owner, control.par.Storychoice, lookup)
        migration.playlist_event(owner, control.par.Nextstory, lookup, pulse=True)
        check('next wraps last to first', controller().sequencer.story_at(owner.par.Playheadsec.eval() * 1000)['number'] == 1)
        migration.playlist_event(owner, control.par.Previousstory, lookup, pulse=True)
        check('previous wraps first to last', controller().sequencer.story_at(owner.par.Playheadsec.eval() * 1000)['number'] == 136)
        migration.playlist_event(owner, control.par.Restartplaylist, lookup, pulse=True)
        check('restart pauses at first', not control.par.Play.eval() and owner.par.Playheadsec.eval() < .04)
    finally:
        for name, value in before.items():
            getattr(control.par, name).val = value
        main.par.Audiosource = before['Audiosource']
        public_callbacks.onValueChange(control.par.Visualpath, before['Visualpath'])
        lookup('/project1').time.frame = before_frame
        controller().update(float(owner.par.Playheadsec.eval()))
        lookup('/local/time').par.rangelimit = 'loop' if before['Loopplaylist'] else 'hold'
        owner.op('execute_callbacks').module._synchronize_show_control()
        main.op('show_control_callbacks').module._apply_audio_controls()
        lookup('/local/time').par.play = False
    check('protected settings restored', protected == migration.protected_state(root))
    check('no model server started', migration.host_runtime.server_flags(
        migration.host_runtime.selected_payload(host)) == (False, False))
    failures = [item for item in checks if not item['passed']]
    report = {'status': 'fail' if failures else 'pass', 'checks': len(checks),
              'passed': len(checks) - len(failures), 'failures': failures,
              'scope': 'muted public data/control mapping; not listening or generated visual acceptance',
              'saved': False, 'model_servers_started': False}
    if report_path:
        Path(report_path).write_text(json.dumps(report, indent=2), encoding='utf-8')
    return report
