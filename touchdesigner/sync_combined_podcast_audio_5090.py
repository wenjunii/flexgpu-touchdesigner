"""Verify and link the approved audio edition on an already-installed 5090 receiver.

The CLI is offline and writes only a new ignored receipt. The live entry point
previews by default. It never runs either receiver installer, stages private
assets, starts models, saves a TOE, or changes a launcher.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import wave

if __package__:
    from . import sync_combined_podcast_5090 as receiver
    from . import sync_combined_podcast_audio_3080 as assets  # data checks only
    from .streamdiffusion_host_runtime import machine_tag, server_flags
else:
    import sync_combined_podcast_5090 as receiver
    import sync_combined_podcast_audio_3080 as assets  # data checks only
    from streamdiffusion_host_runtime import machine_tag, server_flags


TIER = '5090'
BUILD = '2025.32820'
ROOT = '/project1/flexgpu'
TRACK_UPDATES = ('Ambient Only', 'Narration + Ambient')


def _require(ok, message):
    if not ok:
        raise ValueError(message)


def _review_files(project, build, backup_path):
    name = str(project.name)
    _require(str(build) == BUILD and re.fullmatch(r'FlexShow[^/\\]*\.toe', name, re.I)
             and machine_tag(name) == TIER, 'Open the 5090 working TOE in TouchDesigner 2025.32820')
    folder = Path(project.folder).resolve(strict=True)
    working = (folder / name).resolve(strict=True)
    backup = Path(backup_path).resolve(strict=True)
    _require(all(p.parent == folder and p.is_file() and p.stat().st_size >= 1024
                 and p.suffix.lower() == '.toe' and machine_tag(p.name) == TIER for p in (working, backup))
             and not working.samefile(backup),
             'Keep a distinct, saved 5090 rollback TOE in the same project folder')
    return working, backup


def _targets(lookup):
    value = assets._value
    root = lookup(ROOT)
    _require(root is not None and root.path == ROOT, 'Expected the live FlexGPU root')
    owner = root.op(receiver.ADAPTER)
    show = root.op('WORKING_PIPELINE/SHOW_CONTROL')
    clock, project_root = lookup('/local/time'), lookup('/project1')
    _require(all(n is not None for n in (owner, show, clock, project_root))
             and owner.fetch('receiver_playlist46_installed', False, search=False),
             'The reviewed 5090 receiver must already be installed')
    panel, host = owner.op('show_control'), owner.op('STREAMDIFFUSION_HOST')
    switch, output = owner.op('audiosource_switch'), owner.op('audio_out')
    bridges = [owner.op(n) for n in assets.BRIDGES]
    _require(all(n is not None for n in (panel, host, switch, output, *bridges))
             and host.fetch('managed_owner', '', search=False) == receiver.host_builder.OWNER
             and value(host, 'Source') == 'slot',
             'Expected the existing managed single generator and audio routing')
    _require(value(show, 'Qualityprofile') == TIER
             and all(value(n, 'Profile') == TIER for n in bridges),
             'Show Control and both geometry bridge profiles must be 5090')
    quiet = ((panel, 'Play'), (panel, 'Audioenabled'), (panel, 'Livevariations'),
             (show, 'Audioenabled'), (clock, 'play'), (output, 'active'),
             (host, 'Followpodcast'), (host, 'Streamenabled'))
    _require(value(panel, 'Runtimeprofile') == '3080_safe'
             and not any(value(n, field) for n, field in quiet),
             'Pause/mute cached playback and disable live variations, prompt following and RGB delivery')
    slot = host.op('PAYLOAD_SLOT')
    _require(slot is not None, 'The managed generator slot is missing')
    generators = [n for n in slot.children if n.family == 'COMP']
    generators.extend(n for n in owner.children if n.family == 'COMP'
                      and n.name.startswith('StreamDiffusionTD'))
    _require(all(server_flags(n) == (False, False) for n in generators),
             'Stop every generator and confirm both public activity flags are off')
    inputs = [owner.op(name) for name in assets.CHOPS]
    mode = value(panel, 'Audiosource')
    _require(all(n is not None and value(n, 'playmode') == 'locked' for n in inputs)
             and list(switch.inputs) == inputs and mode in assets.MENU
             and value(show, 'Audiosource') == mode and int(value(switch, 'index')) == assets.MENU.index(mode),
             'The locked three-track audio routing or source selections do not match')

    def snapshot():
        return (receiver.protected_state(root), value(show, 'Audiosource'),
                value(panel, 'Audiosource'), value(panel, 'Visualpath'), value(switch, 'index'),
                project_root.time.frame, tuple(value(n, field) for n, field in quiet),
                tuple(value(clock, field)
                      for field in ('start', 'end', 'rangestart', 'rangeend', 'rangelimit')),
                list(switch.inputs) == inputs, tuple(value(n, 'playmode') for n in inputs),
                tuple(server_flags(n) for n in generators))

    return root, owner, inputs, snapshot


def _clean(root):
    _require(not (root.errors(recurse=True) or root.scriptErrors(recurse=True)
                  or root.warnings(recurse=True)), 'Resolve recursive operator errors and warnings first')


def sync(podcast_root, receipt, backup_path, *, lookup, project, build, apply=False,
         review_digest=None, check_native=False):
    """Preview, explicitly apply a reviewed plan, or verify existing .41 bindings."""
    working, backup = _review_files(project, build, backup_path)
    root, owner, inputs, snapshot = _targets(lookup)
    podcast, old, new, manifest, ends, expected = assets._edition(podcast_root)
    expected.update(tracks=assets._stamps(new, manifest), silent_separators_per_track=len(ends))
    _require(receipt == expected, 'Missing or stale offline audio receipt; generate a fresh one')
    for field, filename in (('Controllermodule', 'td_controller.py'),
                            ('Sequencermodule', 'boundary_sequencer.py')):
        _require(Path(str(assets._value(owner, field))).resolve()
                 == assets._asset(podcast, f'{assets.BASE_EDITION}/{filename}'),
                 'Installed playback modules do not match the reviewed edition')
    params = {name: getattr(owner.par, name, None) for name in new}
    _require(all(p is not None and str(p.mode).rsplit('.', 1)[-1] == 'CONSTANT' for p in params.values()),
             'Expected existing constant file parameters; do not replace expressions or binds')

    def bindings():
        return {name: Path(str(p.eval())).resolve() for name, p in params.items()}

    def check_routes(expected_paths):
        _require(bindings() == expected_paths
                 and all(Path(str(assets._value(node, 'file'))).resolve() == expected_paths[name]
                         for name, node in zip(assets.FILES, inputs)),
                 'A native audio input does not follow the approved file binding')

    actual = bindings()
    _require(actual in (old, new), 'Mixed or custom audio bindings; stop and review')
    check_routes(actual)
    _clean(root)
    before = snapshot()
    proposed = [] if actual == new else [name for name in new if new[name] != old[name]]
    backup_sha = assets._sha(backup)
    report = dict(status='already_current' if actual == new else 'preflight_pass',
                  machine=TIER, project=working.name, backup_sha256=backup_sha,
                  protected_digest=before[0], proposed_bindings=proposed, updated_tracks=[],
                  native_decode_checked=False, saved=False, models_started=False)

    def verify(expected_paths, *, reload):
        assets._check_native(owner, inputs, manifest, reload=reload)
        check_routes(expected_paths)
        _clean(root)
        _require(snapshot() == before, 'Protected 5090 settings or playback changed; do not save')

    if actual == new:
        if check_native is True:
            verify(new, reload=False)
            report['native_decode_checked'] = True
        return report

    # The review covers the exact files and protected state checked by this dry run,
    # not merely the operator's name. Re-preview after any intervening change.
    reviewed = (str(working), assets._sha(working), str(backup), backup_sha,
                receipt, before, [(name, str(new[name])) for name in proposed])
    digest = hashlib.sha256(json.dumps(reviewed, sort_keys=True).encode()).hexdigest()
    report['review_digest'] = digest
    if apply is not True:
        return report
    _require(review_digest == digest, 'Apply requires the matching dry-run review_digest')
    callbacks = owner.op('execute_callbacks').module
    prior_controller = callbacks._CONTROLLER
    original = {name: params[name].val for name in proposed}
    try:
        for name in proposed:
            params[name].val = str(new[name])
        verify(new, reload=True)
    except Exception:
        try:
            for name, previous in original.items():
                params[name].val = previous
        finally:
            callbacks._CONTROLLER = prior_controller
        raise
    report.update(status='updated', updated_tracks=list(TRACK_UPDATES),
                  native_decode_checked=True, settings_preserved=True)
    return report


def _receipt_path(repository, path):
    repository = Path(repository).resolve(strict=True)
    runtime = repository / 'runtime'
    target = (repository / path).resolve()
    _require(runtime.resolve() == runtime and target.is_relative_to(runtime)
             and target.suffix.lower() == '.json', 'Use a JSON receipt under this checkout\'s ignored runtime/')
    return target


def verify_to_receipt(podcast_root, report_path, *, repository, source_sha256=None, edition_sha256=None):
    """Verify private assets offline and exclusively create an ignored JSON receipt."""
    repository = Path(repository).resolve(strict=True)
    target = _receipt_path(repository, report_path)
    _require(not target.exists(), 'Choose a fresh receipt filename')
    ignored = subprocess.run(['git', '-C', str(repository), 'check-ignore', '--quiet',
                              '--', target.relative_to(repository).as_posix()],
                             check=False, timeout=10)
    _require(ignored.returncode == 0, 'The receipt path must be Git-ignored')
    pins = (source_sha256, edition_sha256)
    _require(pins == (None, None) or all(isinstance(p, str) and re.fullmatch(r'[0-9a-fA-F]{64}', p)
                                         for p in pins), 'Supply both SHA-256 pins or neither')
    result = assets.verify_assets(podcast_root)
    if source_sha256 is not None:
        _require((result['source_manifest_sha256'], result['manifest_sha256']) == tuple(p.lower() for p in pins),
                 'Manifest SHA-256 differs from the reviewed handoff')
    target.parent.mkdir(parents=True, exist_ok=True)
    _require(_receipt_path(repository, target) == target, 'Receipt destination changed during verification')
    with target.open('x', encoding='utf-8') as out:
        json.dump(result, out, indent=2, sort_keys=True)
        out.write('\n')
    return result


def sync_live(podcast_root, receipt_path, backup_path, *, apply=False, review_digest=None, check_native=False):
    """Live entry point for a saved 5090 copy in this checkout's projects/ folder."""
    project = receiver.rp._symbol('project')
    repository = Path(__file__).resolve().parents[1]
    _require(Path(project.folder).resolve() == repository / 'projects',
             'The live 5090 copy must be in this checkout\'s projects/ folder')
    receipt_path = _receipt_path(repository, receipt_path)
    return sync(podcast_root, assets._document(receipt_path)[0], backup_path,
                lookup=receiver.rp._symbol('op'), project=project, build=receiver.rp._symbol('app').build,
                apply=apply, review_digest=review_digest, check_native=check_native)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Verify local Home Podcast audio assets for the 5090 receiver')
    parser.add_argument('podcast_root', type=Path)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--source-sha256')
    parser.add_argument('--edition-sha256')
    args = parser.parse_args(argv)
    try:
        result = verify_to_receipt(args.podcast_root, args.report,
                                   repository=Path(__file__).resolve().parents[1],
                                   source_sha256=args.source_sha256, edition_sha256=args.edition_sha256)
    except (ValueError, OSError, EOFError, KeyError, TypeError, wave.Error, subprocess.TimeoutExpired) as error:
        parser.exit(1, f'Audio preflight failed: {error}\n')
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
