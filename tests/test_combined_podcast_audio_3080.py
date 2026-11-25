import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import wave

from touchdesigner import sync_combined_podcast_audio_3080 as audio


class Par:
    mode = 'ParMode.CONSTANT'

    def __init__(self, value):
        self.val = value

    def eval(self):
        return self.val


def comp(**parameters):
    return SimpleNamespace(par=SimpleNamespace(**{k: Par(v) for k, v in parameters.items()}))


class AudioSyncTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.podcast = self.root / 'podcast'
        constants = patch.multiple(audio, STORY_COUNT=2, SCENE_COUNT=2, CAPTION_COUNT=1, DURATION_MS=2040)
        constants.start()
        self.addCleanup(constants.stop)
        self.stories = [{'start_ms': 0, 'content_end_ms': 20}, {'start_ms': 1020, 'content_end_ms': 1040}]
        self.original = {}
        self.current = {}
        for folder, paths, amplitude in ((audio.BASE_EDITION, self.original, 1), (audio.EDITION, self.current, 2)):
            for kind in audio.TRACKS:
                path = self.podcast / folder / (kind + '.wav')
                if amplitude == 2 and kind == 'voices_only':
                    path = self.original[kind]
                else:
                    self.write_wav(path, amplitude)
                paths[kind] = path
        self.edition = self.podcast / audio.EDITION
        self.old_edition = self.podcast / audio.BASE_EDITION
        for name in ('td_controller.py', 'boundary_sequencer.py'):
            (self.old_edition / name).touch()
        self.write_package()
        self.receipt = audio.verify_assets(self.podcast)
        _, self.old, self.new, _, _, _ = audio._edition(self.podcast)
        self.project = SimpleNamespace(name='FlexShow-podcast-local-3080.47.toe', folder=str(self.root))
        self.active_file = self.root / self.project.name
        self.backup = self.root / 'FlexShow-podcast-local-3080.46.toe'
        self.active_file.write_bytes(b'a' * 1024)
        self.backup.write_bytes(b'b' * 1024)
        self.setup_native()

    def write_wav(self, path, amplitude, corrupt_gap=False, rate=None):
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), 'wb') as file:
            file.setparams((2, 2, rate or audio.RATE, 0, 'NONE', 'not compressed'))
            packet = bytes((amplitude, 0, amplitude, 0))
            story = packet * (audio.RATE * 20 // 1000)
            gap = (packet if corrupt_gap else bytes(4)) * audio.RATE
            file.writeframes((story + gap) * len(self.stories))

    def manifest(self, folder, paths, *, changed_caption=False):
        relative = lambda p: p.relative_to(self.podcast).as_posix()
        hashes = {kind: audio._sha(path) for kind, path in paths.items()}
        manifest = dict(kind='approved_continuous_image_hold_playlist', profile='3080_safe',
                        executable=True, story_count=audio.STORY_COUNT, duration_ms=audio.DURATION_MS,
                        stories=self.stories, audio={kind: relative(path) for kind, path in paths.items()},
                        audio_sha256=hashes, plans={})
        for mode in audio.MODES:
            plan = dict(executable=True, hold_visual_gaps_edition=True, duration_ms=audio.DURATION_MS,
                        playlist_stories=self.stories, scenes=['a','b'],
                        captions=['drift' if changed_caption else 'original'],
                        audio_file=relative(paths['voices_only']),
                        soundscape_audio_file=relative(paths['soundscape_only']),
                        combined_audio_file=relative(paths['combined']), audio_sha256=hashes,
                        controller_module=f'{audio.BASE_EDITION}/td_controller.py',
                        sequencer_module=f'{audio.BASE_EDITION}/boundary_sequencer.py')
            target = self.podcast / folder / f'{mode}.json'
            target.write_text(json.dumps(plan), encoding='utf-8')
            manifest['plans'][mode] = dict(path=relative(target), sha256=audio._sha(target))
        return manifest

    def write_package(self, *, changed_caption=False):
        before = self.manifest(audio.BASE_EDITION, self.original)
        previous = self.old_edition / 'playlist.json'
        previous.write_text(json.dumps(before), encoding='utf-8')
        approval = dict(target='podcast.41.toe', human_ambient_approval='approved_by_user',
                        story_count=audio.STORY_COUNT)
        (self.edition / 'approval.json').write_text(json.dumps(approval), encoding='utf-8')
        current = self.manifest(audio.EDITION, self.current, changed_caption=changed_caption)
        current.update(ambient_approval=f'{audio.EDITION}/approval.json',
                       source_manifest=f'{audio.BASE_EDITION}/playlist.json',
                       source_manifest_sha256=audio._sha(previous),
                       human_ambient_approval='approved_by_user')
        target = self.edition / 'playlist.json'
        target.write_text(json.dumps(current), encoding='utf-8')
        (self.edition / 'assembly-verification.json').write_text(json.dumps({
            'passed': True, 'manifest_sha256': audio._sha(target),
            'audio': current['audio_sha256'], 'new_tracks': 2,
        }), encoding='utf-8')

    def setup_native(self):
        owner = comp(**{name: str(value) for name, value in self.old.items()},
                     Controllermodule=str(self.old_edition / 'td_controller.py'),
                     Sequencermodule=str(self.old_edition / 'boundary_sequencer.py'))
        self.owner = owner
        owner.path = audio.ADAPTER_PATH
        owner.children = []
        panel = comp(Play=False, Audioenabled=False, Audiosource='combined',
                     Runtimeprofile='3080_safe', Livevariations=False)
        host = comp(Streamenabled=False)
        self.slot = SimpleNamespace(children=[])
        host.op = {'PAYLOAD_SLOT': self.slot}.get
        self.show = comp(Qualityprofile='3080ti_16gb', Audioenabled=False)
        clock = comp(play=False)
        self.original_controller = object()
        self.callbacks = SimpleNamespace(_CONTROLLER=self.original_controller,
                                         get_controller=Mock(side_effect=self.controller))
        switch = comp(index=2)
        entries = {'show_control': panel, 'STREAMDIFFUSION_HOST': host,
                   'audiosource_switch': switch, 'audio_out': comp(active=False),
                   'execute_callbacks': SimpleNamespace(module=self.callbacks)}
        switch.inputs = []
        for chop, field in zip(audio.CHOPS, audio.FILES):
            node = comp(playmode='locked')
            node.par.file = getattr(owner.par, field)
            node.cook = Mock()
            node.errors = Mock(return_value='')
            node.numChans, node.rate, node.numSamples = 2, audio.RATE, 800
            switch.inputs.append(node)
            entries[chop] = node
        for name in audio.BRIDGES:
            entries[name] = comp(Profile='3080ti_16gb')
        owner.op = entries.get
        self.lookup = Mock(side_effect={audio.ADAPTER_PATH: owner,
                                       '/project1/flexgpu/WORKING_PIPELINE/SHOW_CONTROL': self.show,
                                       '/local/time': clock}.get)

    def controller(self):
        plan = json.loads(Path(self.owner.par.Scenejson.eval()).read_text(encoding='utf-8'))
        seq = SimpleNamespace(stories=plan['playlist_stories'], scenes=plan['scenes'],
                              captions=plan['captions'], duration_ms=plan['duration_ms'], plan=plan)
        return SimpleNamespace(sequencer=seq)

    def sync(self, **overrides):
        config = dict(receipt=self.receipt, backup_path=self.backup, lookup=self.lookup,
                      project=self.project, build='2025.32820')
        config.update(overrides)
        return audio.sync(self.podcast, **config)

    def live_bindings(self):
        return {name: Path(getattr(self.owner.par, name).eval()) for name in self.new}

    def test_verified_media_and_dry_run(self):
        self.assertEqual(self.receipt['silent_separators_per_track'], 2)
        self.assertEqual(self.sync()['tracks_to_update'], ['Ambient Only', 'Narration + Ambient'])
        self.assertEqual(self.live_bindings(), self.old)
        self.assertEqual(self.owner.op('audiosource_switch').par.index.eval(), 2)
        self.callbacks.get_controller.assert_not_called()

    def test_apply_only_swaps_approved_paths_and_is_idempotent(self):
        report = self.sync(apply=True)
        self.assertEqual(report['status'], 'updated')
        self.assertEqual(set(report['bindings_updated']), self.new.keys() - {'Audiofile'})
        self.assertEqual(self.live_bindings(), self.new)
        self.assertEqual(self.owner.op('show_control').par.Audiosource.eval(), 'combined')
        self.assertEqual(self.owner.op('audiosource_switch').par.index.eval(), 2)
        self.assertFalse(report['saved'])
        self.assertFalse(report['models_started'])
        self.assertEqual(self.active_file.read_bytes(), b'a' * 1024)
        self.assertEqual(self.backup.read_bytes(), b'b' * 1024)
        self.callbacks.get_controller.reset_mock()
        self.assertEqual(self.sync(apply=True)['status'], 'already_current')
        self.callbacks.get_controller.assert_not_called()
        self.assertTrue(self.sync(check_native=True)['native_decode_checked'])

    def test_failed_native_decode_restores_binding_set_and_controller_cache(self):
        self.owner.op('combined_audio').errors.return_value = 'decode failed'
        with self.assertRaisesRegex(ValueError, 'decode'):
            self.sync(apply=True)
        self.assertEqual(self.live_bindings(), self.old)
        self.assertIs(self.callbacks._CONTROLLER, self.original_controller)

    def test_rejects_other_machine_build_or_rollback_before_network_access(self):
        for name in ('FlexShow-podcast-5090.47.toe', 'FlexShow-podcast-4090-3080.47.toe'):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, '3080'):
                self.sync(project=SimpleNamespace(name=name, folder=str(self.root)), apply=True)
        with self.assertRaisesRegex(ValueError, '2025.32820'):
            self.sync(build='2025.33230', apply=True)
        with self.assertRaisesRegex(ValueError, 'distinct'):
            self.sync(backup_path=self.active_file, apply=True)
        self.lookup.assert_not_called()

    def test_rejects_active_generator_and_wrong_live_profile(self):
        payload = comp(Serveractive=True, Streamactive=False)
        payload.family = 'COMP'
        for parameter in (payload.par.Serveractive, payload.par.Streamactive):
            parameter.isCustom = parameter.isToggle = True
        self.slot.children.append(payload)
        with self.assertRaisesRegex(ValueError, 'Stop every'):
            self.sync(apply=True)
        self.slot.children.clear()
        self.show.par.Qualityprofile.val = '5090'
        with self.assertRaisesRegex(ValueError, 'live profiles'):
            self.sync(apply=True)
        self.assertEqual(self.live_bindings(), self.old)

    def test_rejects_stale_receipt_and_partial_live_sync(self):
        bogus = dict(self.receipt, manifest_sha256='0'*64)
        with self.assertRaisesRegex(ValueError, 'stale offline'):
            self.sync(receipt=bogus, apply=True)
        self.owner.par.Soundscapeaudiofile.val = str(self.new['Soundscapeaudiofile'])
        with self.assertRaisesRegex(ValueError, 'mixed/custom'):
            self.sync(apply=True)
        self.assertEqual(Path(self.owner.par.Combinedaudiofile.val), self.old['Combinedaudiofile'])

    def test_changed_caption_rejected_even_with_consistent_hashes(self):
        self.write_package(changed_caption=True)
        with self.assertRaisesRegex(ValueError, 'non-audio plan content'):
            audio.verify_assets(self.podcast)

    def test_rejects_bad_hash_bad_clock_and_non_silent_gap(self):
        track = self.current['combined']
        self.write_wav(track, 3)
        with self.assertRaisesRegex(ValueError, 'SHA-256'):
            audio.verify_assets(self.podcast)
        self.write_wav(track, 3, rate=44100)
        self.write_package()
        with self.assertRaisesRegex(ValueError, 'PCM clock'):
            audio.verify_assets(self.podcast)
        self.write_wav(track, 3, corrupt_gap=True)
        self.write_package()
        with self.assertRaisesRegex(ValueError, 'non-silent separator'):
            audio.verify_assets(self.podcast)


if __name__ == '__main__':
    unittest.main()
