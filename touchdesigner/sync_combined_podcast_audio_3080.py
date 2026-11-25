"""Link the approved Home Podcast .41 audio edition into an existing FlexGPU 3080 playlist.

The CLI verifies media outside the TD cook thread. The live helper only changes
known file parameters after a dry run, an explicit apply, and a saved rollback.
It never copies media, starts models, rebuilds operators, or saves a TOE.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import wave

try:
    from streamdiffusion_host_runtime import machine_tag, server_flags
    from update_combined_storytelling_3080 import ADAPTER_PATH, EDITION as BASE_EDITION, MODES, PROJECT_PATTERN, _symbol
except ImportError:
    from touchdesigner.streamdiffusion_host_runtime import machine_tag, server_flags
    from touchdesigner.update_combined_storytelling_3080 import (
        ADAPTER_PATH, EDITION as BASE_EDITION, MODES, PROJECT_PATTERN, _symbol,
    )


EDITION = 'work/storytelling-real-december/approved-ambient-136-3080-2026-11-24'
FORMAT = 'flexgpu-podcast41-audio-preflight/v1'
STORY_COUNT, SCENE_COUNT, CAPTION_COUNT = 136, 619, 2862
DURATION_MS, RATE, GAP_MS = 11938720, 48000, 1000
TRACKS = ('voices_only', 'soundscape_only', 'combined')
FILES = ('Audiofile', 'Soundscapeaudiofile', 'Combinedaudiofile')
CHOPS = ('voices_only_audio', 'soundscape_audio', 'combined_audio')
MENU = ('voices', 'soundscape', 'combined')
PLAN_PARAMS = ('Scenejson', 'Humanfigurejson', 'Balancedjson')
AUDIO_METADATA = {'soundscape_audio_file', 'combined_audio_file', 'audio_sha256'}
BRIDGES = ('MOGE2_BRIDGE', 'DEPTH_ANYTHING_GEOMETRY_BRIDGE')


def _require(ok, message):
    if not ok:
        raise ValueError(message)


def _sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _asset(root, name):
    relative = Path(name)
    _require(not relative.anchor and '..' not in relative.parts, 'Expected a local relative Home Podcast asset')
    path = (root / relative).resolve(strict=True)
    _require(path.is_relative_to(root) and path.is_file(), 'Asset must stay in the Home Podcast checkout')
    return path


def _document(path):
    raw = path.read_bytes()
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def _edition(root):
    root = Path(root).resolve(strict=True)
    before_file, after_file = (_asset(root, f'{folder}/playlist.json') for folder in (BASE_EDITION, EDITION))
    before, source_sha = _document(before_file)
    after, update_sha = _document(after_file)
    proof, _ = _document(_asset(root, f'{EDITION}/assembly-verification.json'))
    approval, _ = _document(_asset(root, after['ambient_approval']))
    _require(after['source_manifest_sha256'] == source_sha
             and _asset(root, after['source_manifest']) == before_file,
             'The audio release must derive from the installed .40 playlist')
    _require(approval.get('target') == 'podcast.41.toe'
             and approval.get('human_ambient_approval') == 'approved_by_user'
             and approval.get('story_count') == STORY_COUNT
             and after.get('human_ambient_approval') == 'approved_by_user',
             'The .41 ambient release is not approved')
    _require(proof.get('passed') is True and proof.get('manifest_sha256') == update_sha
             and proof.get('audio') == after['audio_sha256'] and proof.get('new_tracks') == 2,
             'Assembly verification does not match the approved release')
    frames = DURATION_MS * RATE // 1000
    stories = after['stories']
    _require(before['stories'] == stories and len(stories) == STORY_COUNT
             and all(doc['profile'] == '3080_safe' and doc['executable'] is True
                     and doc['kind'] == 'approved_continuous_image_hold_playlist'
                     and doc['story_count'] == STORY_COUNT and doc['duration_ms'] == DURATION_MS
                     and set(doc['plans']) == set(MODES) and set(doc['audio']) == set(TRACKS)
                     and set(doc['audio_sha256']) == set(TRACKS) for doc in (before, after)),
             'Playlist identity, clock or story order changed')
    ends = [s['content_end_ms'] for s in stories]
    starts = [s['start_ms'] for s in stories]
    _require(starts[0] == 0 and all(type(s) is int and type(e) is int and s < e
                                   and e + GAP_MS == limit
                                   for s, e, limit in zip(starts, ends, starts[1:] + [DURATION_MS])),
             'Playlist story durations or exact one-second separators changed')
    old = {'Playlistmanifest': before_file}
    new = {'Playlistmanifest': after_file}
    for i, (kind, parameter) in enumerate(zip(TRACKS, FILES)):
        old[parameter], new[parameter] = (_asset(root, doc['audio'][kind]) for doc in (before, after))
        same_path = old[parameter] == new[parameter]
        same_hash = before['audio_sha256'][kind] == after['audio_sha256'][kind]
        _require(same_path == same_hash == (i == 0), 'Expected only new ambient and combined tracks')
    for mode, parameter in zip(MODES, PLAN_PARAMS):
        plans = []
        for doc, paths in ((before, old), (after, new)):
            entry = doc['plans'][mode]
            path = _asset(root, entry['path'])
            plan, digest = _document(path)
            _require(digest == entry['sha256'], f'Reviewed {mode} visual plan hash differs')
            _require(plan['executable'] is True and plan['hold_visual_gaps_edition'] is True
                     and plan['duration_ms'] == DURATION_MS and plan['playlist_stories'] == stories
                     and len(plan['scenes']) == SCENE_COUNT and len(plan['captions']) == CAPTION_COUNT
                     and plan['audio_sha256'] == doc['audio_sha256']
                     and all(_asset(root, plan[field]) == paths[param] for field, param in zip(
                         ('audio_file', 'soundscape_audio_file', 'combined_audio_file'), FILES)),
                     f'Reviewed {mode} timeline or audio metadata differs')
            _require(all(_asset(root, plan[field]) == _asset(root, f'{BASE_EDITION}/{file}')
                         for field, file in (('controller_module','td_controller.py'),
                                             ('sequencer_module','boundary_sequencer.py'))),
                     'The audio update must keep the existing playback modules')
            paths[parameter] = path
            plans.append({k: v for k, v in plan.items() if k not in AUDIO_METADATA})
        _require(plans[0] == plans[1], f'{mode} non-audio plan content changed')
    for paths in (old, new):
        paths.update(Canonicalvisuals=paths['Scenejson'], Canonicalhumanvisuals=paths['Humanfigurejson'])
    return root, old, new, after, ends, {
        'version': FORMAT, 'status': 'pass', 'source_manifest_sha256': source_sha,
        'manifest_sha256': update_sha, 'stories': STORY_COUNT, 'scenes_per_path': SCENE_COUNT,
        'captions': CAPTION_COUNT, 'duration_ms': DURATION_MS, 'rate': RATE, 'audio_frames': frames,
    }


def _stamps(bindings, manifest):
    tracks = {}
    for kind, parameter in zip(TRACKS, FILES):
        stat = bindings[parameter].stat()
        tracks[kind] = {'bytes': stat.st_size, 'mtime_ns': stat.st_mtime_ns, 'sha256': manifest['audio_sha256'][kind]}
    return tracks


def verify_assets(podcast_root):
    """Read and hash all three local PCM WAVs; verify every exact silent separator."""
    _, _, new, manifest, ends, receipt = _edition(podcast_root)
    stamps = _stamps(new, manifest)
    gap_frames = RATE * GAP_MS // 1000
    silence = bytes(gap_frames * 4)  # signed 16-bit stereo PCM
    for kind, parameter in zip(TRACKS, FILES):
        track = new[parameter]
        _require(_sha(track) == stamps[kind]['sha256'], f'{kind} SHA-256 differs from the approved WAV')
        with wave.open(str(track), 'rb') as wav:
            _require((wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getnframes(),
                      wav.getcomptype()) == (2, 2, RATE, receipt['audio_frames'], 'NONE'),
                     f'{kind} does not match the approved stereo PCM clock')
            for end in ends:
                wav.setpos(end * RATE // 1000)
                _require(wav.readframes(gap_frames) == silence, f'{kind} has a non-silent separator')
    _require(stamps == _stamps(new, manifest), 'An audio file changed while being verified')
    return dict(receipt, tracks=stamps, silent_separators_per_track=len(ends))


def _rollback(project, build, backup_path):
    name = str(project.name)
    _require(str(build) == '2025.32820' and PROJECT_PATTERN.fullmatch(name)
             and machine_tag(name) == '3080', 'Open a combined 3080 TOE in TouchDesigner 2025.32820')
    folder = Path(project.folder).resolve(strict=True)
    source = (folder / name).resolve(strict=True)
    backup = Path(backup_path).resolve(strict=True)
    _require(all(path.parent == folder and path.is_file() and path.stat().st_size >= 1024
                 and path.suffix.lower() == '.toe' and machine_tag(path.name) == '3080'
                 for path in (source, backup)) and not source.samefile(backup),
             'Keep a distinct, saved 3080 rollback TOE in the same project folder')
    return backup


def _value(node, parameter):
    return getattr(node.par, parameter).eval()


def _target(lookup):
    owner = lookup(ADAPTER_PATH)
    show = lookup('/project1/flexgpu/WORKING_PIPELINE/SHOW_CONTROL')
    clock = lookup('/local/time')
    _require(owner is not None and owner.path == ADAPTER_PATH and show is not None,
             'The existing combined FlexGPU adapter and Show Control are required')
    panel, host = owner.op('show_control'), owner.op('STREAMDIFFUSION_HOST')
    switch, out = owner.op('audiosource_switch'), owner.op('audio_out')
    bridges = [owner.op(name) for name in BRIDGES]
    _require(all(node is not None for node in (panel, host, clock, switch, out, *bridges)),
             'Expected the installed playlist, audio switch, and single-generator host')
    _require(_value(show, 'Qualityprofile') == '3080ti_16gb'
             and _value(panel, 'Runtimeprofile') == '3080_safe'
             and all(_value(node, 'Profile') == '3080ti_16gb' for node in bridges),
             'The live profiles do not match this 3080 audio update')
    _require(not any(_value(node, name) for node, name in (
        (panel,'Livevariations'), (panel,'Play'), (panel,'Audioenabled'), (show,'Audioenabled'),
        (clock,'play'), (host,'Streamenabled'), (out,'active'),
    )), 'Pause and mute the show, disable live variations and disconnect RGB delivery first')
    slot = host.op('PAYLOAD_SLOT')
    _require(slot is not None, 'The managed generator slot is missing')
    generators = [node for node in slot.children if node.family == 'COMP']
    generators += [node for node in owner.children
                   if node.family == 'COMP' and node.name.startswith('StreamDiffusionTD')]
    _require(all(server_flags(node) == (False, False) for node in generators),
             'Stop every installed generator and confirm both public activity flags are off')
    nodes = [owner.op(name) for name in CHOPS]
    _require(all(node is not None and _value(node, 'playmode') == 'locked' for node in nodes)
             and list(switch.inputs) == nodes and _value(panel, 'Audiosource') in MENU
             and int(_value(switch, 'index')) == MENU.index(_value(panel, 'Audiosource')),
             'The existing locked three-track audio routing is not intact')
    return owner, nodes


def _check_native(owner, nodes, manifest, *, reload):
    callbacks = owner.op('execute_callbacks').module
    if reload:
        callbacks._CONTROLLER = None
    seq = callbacks.get_controller().sequencer
    _require((len(seq.stories), len(seq.scenes), len(seq.captions), seq.duration_ms)
             == (STORY_COUNT, SCENE_COUNT, CAPTION_COUNT, DURATION_MS)
             and seq.stories == manifest['stories'] and seq.plan['audio_sha256'] == manifest['audio_sha256'],
             'Native sequencer did not load the approved audio metadata')
    for node in nodes:
        node.cook(force=True)
        _require(not node.errors() and node.numChans == 2 and node.rate == RATE and node.numSamples > 0,
                 'A verified track could not decode in TouchDesigner')


def sync(podcast_root, receipt, backup_path, *, lookup, project, build, apply=False, check_native=False):
    """Preview by default. Apply only file-parameter changes; no project save."""
    _rollback(project, build, backup_path)
    owner, nodes = _target(lookup)
    root, old, new, manifest, ends, expected = _edition(podcast_root)
    expected.update(tracks=_stamps(new, manifest), silent_separators_per_track=len(ends))
    _require(receipt == expected, 'Missing or stale offline WAV preflight; run Test-HomePodcastAudioSync.ps1 again')
    for name, filename in (('Controllermodule','td_controller.py'), ('Sequencermodule','boundary_sequencer.py')):
        _require(Path(str(_value(owner, name))).resolve() == _asset(root, f'{BASE_EDITION}/{filename}'),
                 'The installed playback modules differ; review before syncing audio')
    parameters = {name: getattr(owner.par, name, None) for name in new}
    _require(all(p is not None and str(p.mode).endswith('CONSTANT') for p in parameters.values()),
             'Existing media file parameters must be present and constant')
    actual = {name: Path(str(p.eval())).resolve() for name, p in parameters.items()}
    _require(actual in (old, new), 'Expected the complete .40 or .41 binding set, not a mixed/custom edition')
    changed = [name for name in new if old[name] != new[name]]

    def check_paths(paths):
        _require(all(Path(str(p.eval())).resolve() == paths[name] for name, p in parameters.items())
                 and all(Path(str(_value(node, 'file'))).resolve() == paths[name]
                         for name, node in zip(FILES, nodes)),
                 'An audio CHOP does not follow its public file parameter')

    check_paths(actual)
    report = {'status': 'already_current' if actual == new else 'preflight_pass',
              'project': str(project.name), 'stories': STORY_COUNT, 'duration_ms': DURATION_MS,
              'tracks_to_update': ['Ambient Only', 'Narration + Ambient'] if actual == old else [],
              'bindings_to_update': changed if actual == old else [],
              'saved': False, 'models_started': False, 'native_decode_checked': False}
    if actual == new:
        if check_native:
            _check_native(owner, nodes, manifest, reload=False)
            report['native_decode_checked'] = True
        return report
    if not apply:
        return report
    callbacks = owner.op('execute_callbacks').module
    prior_controller = callbacks._CONTROLLER
    previous = {name: parameters[name].val for name in changed}
    try:
        for name in changed:
            parameters[name].val = str(new[name])
        check_paths(new)
        _check_native(owner, nodes, manifest, reload=True)
    except Exception:
        for name, value in previous.items():
            parameters[name].val = value
        callbacks._CONTROLLER = prior_controller
        raise
    report.update(status='updated', tracks_updated=report.pop('tracks_to_update'),
                  bindings_updated=report.pop('bindings_to_update'), native_decode_checked=True)
    return report


def sync_live(podcast_root, receipt_path, backup_path, *, apply=False, check_native=False):
    """Entry point for the existing 3080 TouchDesigner project."""
    runtime = (Path(__file__).resolve().parents[1] / 'runtime').resolve()
    receipt_path = Path(receipt_path).resolve(strict=True)
    _require(receipt_path.is_relative_to(runtime) and receipt_path.suffix == '.json',
             'Use the offline verification report from this checkout’s ignored runtime folder')
    return sync(podcast_root, _document(receipt_path)[0], backup_path, lookup=_symbol('op'),
                project=_symbol('project'), build=_symbol('app').build,
                apply=apply, check_native=check_native)


def main():
    parser = argparse.ArgumentParser(description='Verify the private, approved 3080 Home Podcast .41 audio assets')
    parser.add_argument('podcast_root', type=Path)
    args = parser.parse_args()
    try:
        report = verify_assets(args.podcast_root)
    except (ValueError, OSError, EOFError, KeyError, TypeError, wave.Error) as error:
        parser.exit(1, f'Audio preflight failed: {error}\n')
    print(json.dumps(report, sort_keys=True))


if __name__ == '__main__':
    main()
