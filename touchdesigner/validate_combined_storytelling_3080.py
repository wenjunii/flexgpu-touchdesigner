"""Muted native data/control audit for the combined podcast.40 3080 edition.

Does not start renderers, geometry workers, or save the project. Generated
visuals remain a separate acceptance step. All tested controls are restored.
"""
from pathlib import Path
import json
import math

import update_combined_storytelling_3080 as migration


def validate(podcast_root, *, lookup, report_path=None):
    owner = lookup(migration.ADAPTER_PATH)
    control = owner.op('show_control')
    root, manifest, _, _ = migration.read_edition(podcast_root)
    project = migration._symbol('project')
    if not migration.PROJECT_PATTERN.fullmatch(str(project.name)):
        raise RuntimeError('3080 combined project required')
    if (lookup('/project1/flexgpu/WORKING_PIPELINE/SHOW_CONTROL').par.Qualityprofile.eval() != '3080ti_16gb'
            or control.par.Runtimeprofile.eval() != '3080_safe'
            or bool(control.par.Livevariations.eval())):
        raise RuntimeError('Use the canonical 3080 playback profile for this audit')
    for renderer_name in str(owner.par.Streamdiffusionpath.eval()).split(';'):
        renderer = owner.op(renderer_name.strip())
        if renderer.par.Serveractive.eval() or renderer.par.Streamactive.eval():
            raise RuntimeError('Stop the selected StreamDiffusion server before the muted data audit')
    checks = []
    def check(name, value):
        checks.append({'check': name, 'passed': bool(value)})
    names = ('Play','Audioenabled','Audiosource','Visualpath','Randomseeds',
             'Crossfadesec','Storyfadesec','Loopplaylist','Storychoice')
    before = {n: getattr(control.par, n).eval() for n in names}
    before_frame = lookup('/project1').time.frame
    current = migration._controller(owner)
    control.par.Play = False
    control.par.Audioenabled = False
    control.par.Randomseeds = False
    # Parameter Execute callbacks may be deferred until the next application
    # frame. Apply the pause/mute contract before inspecting the native clock.
    owner.op('execute_callbacks').module._synchronize_show_control()
    try:
        check('paused', not lookup('/local/time').par.play.eval())
        check('3080 safe', control.par.Runtimeprofile.eval() == '3080_safe' and not control.par.Livevariations.eval())
        check('gap colour disabled', control.par.Abstractfadesec.eval() == 0 and not control.par.Abstractfadesec.enable)
        for mode in migration.MODES:
            control.par.Visualpath = mode
            control.op('control_callbacks').module.onValueChange(control.par.Visualpath, before['Visualpath'])
            current = migration._controller(owner)
            seq = current.sequencer
            check(mode + ':counts', len(seq.stories) == 136 and len(seq.scenes) == 619 and len(seq.captions) == 2862)
            check(mode + ':duration', seq.duration_ms == manifest['duration_ms'])
            check(mode + ':all story openings', all(seq.story_at(s['start_ms'])['number'] == s['number'] for s in seq.stories))
            control.par.Crossfadesec = control.par.Storyfadesec = 3
            current.update(0)
            for index, scene in enumerate(seq.scenes[1:], 1):
                fade = seq._effective_crossfade_ms(index, scene['duration_ms'], 3000)
                frame = seq.at(scene['start_ms'] + fade // 2, crossfade_ms=3000)
                check(f'{mode}:join{index}', fade > 0 and len(frame.prompt_layers) == 2
                      and all(0 < layer.weight < 1 for layer in frame.prompt_layers))
            for index, scene in enumerate(seq.scenes):
                next_start = seq.scenes[index+1]['start_ms'] if index+1 < len(seq.scenes) else seq.duration_ms
                if next_start > scene['end_ms']:
                    at = (scene['end_ms'] + next_start) // 2
                    layers = seq.at(at, crossfade_ms=3000).prompt_layers
                    check(f'{mode}:hold{index}', len(layers) == 1 and layers[0].scene_id == scene['scene_id']
                          and seq.neutral_mix_at(at) == 0)
            # Exercise native prompt/seed writes only at representative clocks.
            for story_number in (1, 68, 136):
                migration.jump_story(owner, story_number, lookup)
                check(f'{mode}:native story{story_number}', seq.story_at(owner.par.Playheadsec.eval()*1000)['number'] == story_number
                      and owner.par.Semanticneutralmix.eval() == 0)
                layers = current._controlled_prompt_layers(seq.at(round(owner.par.Playheadsec.eval()*1000), crossfade_ms=3000))
                for renderer_name in str(owner.par.Streamdiffusionpath.eval()).split(';'):
                    renderer = owner.op(renderer_name.strip())
                    prompts, seeds = renderer.par.Promptdict.sequence, renderer.par.Seeddict.sequence
                    check(f'{mode}:native{story_number}:{renderer_name}', prompts.numBlocks == seeds.numBlocks == len(layers)
                          and all(0 <= seeds[i].par.Seedval.eval() <= 2147483647
                                  and abs(prompts[i].par.Weight.eval()-layer.weight) < 1e-5 for i,layer in enumerate(layers)))
            current.update((seq.duration_ms - 20) / 1000)
            current.update(0)
            check(mode + ':loop carry', len(seq.at(1000, crossfade_ms=3000).prompt_layers) == 2)
        for mode, index in (('voices',0),('soundscape',1),('combined',2)):
            control.par.Audiosource = mode
            control.op('control_callbacks').module.onValueChange(control.par.Audiosource, before['Audiosource'])
            check('audio route:' + mode, owner.op('audiosource_switch').par.index.eval() == index)
        for name in ('voices_only_audio','soundscape_audio','combined_audio'):
            node = owner.op(name)
            for seconds in (0, 500, 11937):
                lookup('/project1').time.frame = math.ceil(seconds * project.cookRate) + 1
                node.cook(force=True)
                check(f'{name}:decode{seconds}', not node.errors() and node.numChans == 2 and node.rate == 48000)
        callback = control.op('playlist_callbacks').module
        control.par.Storychoice = 'story136'
        callback.onValueChange(control.par.Storychoice, 'story1')
        callback.onPulse(control.par.Nextstory)
        check('next wraps last to first', migration._controller(owner).sequencer.story_at(owner.par.Playheadsec.eval()*1000)['number'] == 1)
        callback.onPulse(control.par.Previousstory)
        check('previous wraps first to last', migration._controller(owner).sequencer.story_at(owner.par.Playheadsec.eval()*1000)['number'] == 136)
        callback.onPulse(control.par.Restartplaylist)
        check('restart pauses at first', not control.par.Play.eval() and owner.par.Playheadsec.eval() < .04)
    finally:
        for name, value in before.items():
            getattr(control.par, name).val = value
        lookup('/project1').time.frame = before_frame
        owner.op('execute_callbacks').module._CONTROLLER = None
        current = migration._controller(owner)
        current.update(float(owner.par.Playheadsec.eval()))
        owner.op('execute_callbacks').module._synchronize_show_control()
    failures = [c for c in checks if not c['passed']]
    report = {'project':str(project.name), 'status':'fail' if failures else 'pass',
              'checks':len(checks), 'passed':len(checks)-len(failures), 'failures':failures,
              'generated_visual_acceptance':'deferred', 'saved':False, 'model_servers_started':False}
    if report_path:
        Path(report_path).write_text(json.dumps(report, indent=2), encoding='utf-8')
    return report
