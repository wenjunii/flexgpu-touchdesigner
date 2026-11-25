import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from touchdesigner import sync_combined_podcast_audio_5090 as audio


assets = audio.assets


class Par:
    mode = 'ParMode.CONSTANT'

    def __init__(self, value):
        self.val = value

    def eval(self):
        return self.val


def comp(children=(), **values):
    return SimpleNamespace(children=list(children), family='COMP',
                           par=SimpleNamespace(**{k: Par(v) for k, v in values.items()}))


class ReceiverAudioTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.repo = Path(temp.name).resolve()
        self.podcast = self.repo / 'podcast'
        self.podcast.mkdir()
        baseline = self.podcast / assets.BASE_EDITION
        baseline.mkdir(parents=True)
        for name in ('td_controller.py', 'boundary_sequencer.py'):
            (baseline / name).touch()

        fields = ('Playlistmanifest', *assets.FILES, *assets.PLAN_PARAMS,
                  'Canonicalvisuals', 'Canonicalhumanvisuals')
        self.old = {name: self.podcast / f'old-{name}' for name in fields}
        self.new = {name: self.podcast / f'new-{name}' for name in fields}
        self.new['Audiofile'] = self.old['Audiofile']
        for path in {*self.old.values(), *self.new.values()}:
            path.touch()
        self.manifest = dict(stories=[{}] * assets.STORY_COUNT,
                             audio_sha256={k: 'a' * 64 for k in assets.TRACKS})
        ends = [1000, 2000]
        report = dict(version=assets.FORMAT, status='pass',
                      source_manifest_sha256='1' * 64, manifest_sha256='2' * 64)
        self.receipt = dict(report, tracks=assets._stamps(self.new, self.manifest),
                            silent_separators_per_track=len(ends))
        edition = patch.object(assets, '_edition', side_effect=lambda _: (
            self.podcast, self.old, self.new, self.manifest, ends, report.copy()))
        edition.start()
        self.addCleanup(edition.stop)

        self.working = self.repo / 'FlexShow-receiver-5090-working.2.toe'
        self.backup = self.repo / 'FlexShow-receiver-5090.1.toe'
        self.working.write_bytes(b'w' * 1024)
        self.backup.write_bytes(b'b' * 1024)
        self.project = SimpleNamespace(name=self.working.name, folder=str(self.repo))
        self.owner = comp(**{k: str(v) for k, v in self.old.items()},
                          Controllermodule=str(baseline / 'td_controller.py'),
                          Sequencermodule=str(baseline / 'boundary_sequencer.py'))
        self.owner.fetch = Mock(return_value=True)
        self.panel = comp(Play=False, Audioenabled=False, Livevariations=False,
                          Runtimeprofile='3080_safe', Audiosource='combined', Visualpath='original')
        self.host = comp(Source='slot', Followpodcast=False, Streamenabled=False)
        self.host.fetch = Mock(return_value=audio.receiver.host_builder.OWNER)
        self.slot = comp()
        self.host.op = {'PAYLOAD_SLOT': self.slot}.get
        self.clock = comp(play=False, start=1, end=24, rangestart=1, rangeend=24, rangelimit='loop')
        self.project_root = SimpleNamespace(time=SimpleNamespace(frame=1))
        self.show = comp(Qualityprofile='5090', Audioenabled=False, Audiosource='combined')
        self.bridges = {name: comp(Profile='5090') for name in assets.BRIDGES}
        self.nodes = {}
        for chop, field in zip(assets.CHOPS, assets.FILES):
            node = comp(playmode='locked')
            node.par.file = getattr(self.owner.par, field)
            node.cook = Mock()
            node.errors = Mock(return_value='')
            node.numChans, node.rate, node.numSamples = 2, assets.RATE, 800
            self.nodes[chop] = node
        self.switch = comp(index=2)
        self.switch.inputs = list(self.nodes.values())
        self.old_cache = object()
        seq = SimpleNamespace(stories=self.manifest['stories'], scenes=[None] * assets.SCENE_COUNT,
                              captions=[None] * assets.CAPTION_COUNT, duration_ms=assets.DURATION_MS,
                              plan={'audio_sha256': self.manifest['audio_sha256']})
        self.callbacks = SimpleNamespace(_CONTROLLER=self.old_cache,
                                         get_controller=Mock(return_value=SimpleNamespace(sequencer=seq)))
        self.owner.op = dict(self.bridges, **self.nodes, show_control=self.panel,
                             STREAMDIFFUSION_HOST=self.host, audiosource_switch=self.switch,
                             audio_out=comp(active=False),
                             execute_callbacks=SimpleNamespace(module=self.callbacks)).get
        self.root = SimpleNamespace(path=audio.ROOT, op={
            audio.receiver.ADAPTER: self.owner, 'WORKING_PIPELINE/SHOW_CONTROL': self.show}.get,
            errors=Mock(return_value=''), scriptErrors=Mock(return_value=''), warnings=Mock(return_value=''))
        self.lookup = Mock(side_effect={audio.ROOT: self.root, '/project1': self.project_root,
                                       '/local/time': self.clock}.get)
        fingerprint = patch.object(audio.receiver, 'protected_state', return_value='protected')
        self.fingerprint = fingerprint.start()
        self.addCleanup(fingerprint.stop)

    def sync(self, **changes):
        settings = dict(receipt=self.receipt, backup_path=self.backup, lookup=self.lookup,
                        project=self.project, build=audio.BUILD)
        return audio.sync(self.podcast, **(settings | changes))

    def bindings(self):
        return {k: Path(getattr(self.owner.par, k).eval()) for k in self.old}

    def test_preview_is_read_only_and_apply_needs_the_same_review(self):
        report = self.sync()
        self.assertEqual(report['status'], 'preflight_pass')
        self.assertEqual(set(report['proposed_bindings']), self.new.keys() - {'Audiofile'})
        self.assertEqual(self.bindings(), self.old)
        self.callbacks.get_controller.assert_not_called()
        with self.assertRaisesRegex(ValueError, 'review_digest'):
            self.sync(apply=True)
        self.clock.par.rangeend.val += 1
        with self.assertRaisesRegex(ValueError, 'review_digest'):
            self.sync(apply=True, review_digest=report['review_digest'])
        self.assertEqual(self.bindings(), self.old)

    def test_apply_and_recheck_preserve_narration_toes_and_controls(self):
        reviewed = self.sync()['review_digest']
        report = self.sync(apply=True, review_digest=reviewed)
        self.assertEqual(report['status'], 'updated')
        self.assertEqual(report['updated_tracks'], list(audio.TRACK_UPDATES))
        self.assertTrue(report['native_decode_checked'])
        self.assertEqual(self.bindings(), self.new)
        self.assertEqual(self.old['Audiofile'], self.new['Audiofile'])
        self.assertEqual(self.project_root.time.frame, 1)
        self.assertEqual(self.switch.par.index.val, 2)
        self.assertEqual(self.working.read_bytes(), b'w' * 1024)
        self.assertEqual(self.backup.read_bytes(), b'b' * 1024)
        self.assertFalse(report['models_started'] or report['saved'])
        self.callbacks.get_controller.reset_mock()
        self.assertEqual(self.sync()['status'], 'already_current')
        self.callbacks.get_controller.assert_not_called()
        self.assertTrue(self.sync(check_native=True)['native_decode_checked'])

    def test_rejects_other_machine_build_or_non_distinct_rollback_before_access(self):
        for name in ('FlexShow-3080.1.toe', 'FlexShow-4090.1.toe', 'FlexShow-3080-5090.2.toe'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.sync(project=SimpleNamespace(name=name, folder=self.project.folder))
        with self.assertRaisesRegex(ValueError, audio.BUILD):
            self.sync(build='2025.30000')
        with self.assertRaisesRegex(ValueError, 'distinct'):
            self.sync(backup_path=self.working)
        self.lookup.assert_not_called()

    def test_rejects_a_bridge_profile_active_payload_or_unsafe_playback(self):
        bridge = self.bridges['DEPTH_ANYTHING_GEOMETRY_BRIDGE']
        bridge.par.Profile.val = '3080ti_16gb'
        with self.assertRaisesRegex(ValueError, 'profiles must be 5090'):
            self.sync()
        bridge.par.Profile.val = '5090'
        payload = comp(Serveractive=False, Streamactive=True)
        for flag in (payload.par.Serveractive, payload.par.Streamactive):
            flag.isCustom = flag.isToggle = True
        payload.name = 'StreamDiffusionTD4'
        self.owner.children.append(payload)
        with self.assertRaisesRegex(ValueError, 'both public activity flags'):
            self.sync()
        self.owner.children.clear()
        self.host.par.Followpodcast.val = True
        with self.assertRaisesRegex(ValueError, 'prompt following'):
            self.sync()
        self.assertEqual(self.bindings(), self.old)

    def test_rejects_installer_state_mixed_bindings_and_stale_receipt(self):
        self.owner.fetch.return_value = False
        with self.assertRaisesRegex(ValueError, 'already be installed'):
            self.sync()
        self.owner.fetch.return_value = True
        with self.assertRaisesRegex(ValueError, 'stale offline'):
            self.sync(receipt=dict(self.receipt, manifest_sha256='0' * 64))
        self.owner.par.Soundscapeaudiofile.val = str(self.new['Soundscapeaudiofile'])
        with self.assertRaisesRegex(ValueError, 'Mixed or custom'):
            self.sync()
        self.assertEqual(self.bindings()['Combinedaudiofile'], self.old['Combinedaudiofile'])

    def test_rejects_different_public_routes_or_bindings(self):
        self.show.par.Audiosource.val = 'voices'
        with self.assertRaisesRegex(ValueError, 'source selections'):
            self.sync()
        self.show.par.Audiosource.val = 'combined'
        self.nodes['combined_audio'].par.file = Par(str(self.old['Audiofile']))
        with self.assertRaisesRegex(ValueError, 'native audio input'):
            self.sync()
        self.nodes['combined_audio'].par.file = self.owner.par.Combinedaudiofile
        self.owner.par.Combinedaudiofile.mode = 'ParMode.EXPRESSION'
        with self.assertRaisesRegex(ValueError, 'constant file'):
            self.sync()

    def test_failed_native_decode_restores_bindings_and_cache(self):
        reviewed = self.sync()['review_digest']
        self.nodes['combined_audio'].rate = 44100
        with self.assertRaisesRegex(ValueError, 'decode'):
            self.sync(apply=True, review_digest=reviewed)
        self.assertEqual(self.bindings(), self.old)
        self.assertIs(self.callbacks._CONTROLLER, self.old_cache)

    def test_protected_change_during_reload_fails_and_restores_bindings(self):
        reviewed = self.sync()['review_digest']
        self.nodes['combined_audio'].cook.side_effect = (
            lambda **_: setattr(self.fingerprint, 'return_value', 'changed'))
        with self.assertRaisesRegex(ValueError, 'Protected 5090 settings'):
            self.sync(apply=True, review_digest=reviewed)
        self.assertEqual(self.bindings(), self.old)
        self.assertIs(self.callbacks._CONTROLLER, self.old_cache)

    def test_recursive_warnings_fail_closed_without_mutation(self):
        self.root.warnings.return_value = 'not safe'
        with self.assertRaisesRegex(ValueError, 'recursive operator'):
            self.sync()
        self.assertEqual(self.bindings(), self.old)


class OfflineReceiptTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.repo = Path(temp.name).resolve()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        (self.repo / '.gitignore').write_text('/runtime/validation/\n', encoding='utf-8')
        self.result = dict(version=assets.FORMAT, status='pass',
                           source_manifest_sha256='1' * 64, manifest_sha256='2' * 64)
        verifier = patch.object(assets, 'verify_assets', return_value=self.result)
        self.verify_assets = verifier.start()
        self.addCleanup(verifier.stop)

    def verify(self, path, **pins):
        return audio.verify_to_receipt(self.repo / 'podcast', path, repository=self.repo, **pins)

    def test_writes_only_a_fresh_ignored_receipt(self):
        path = 'runtime/validation/5090-audio.json'
        self.assertEqual(self.verify(path, source_sha256='1'*64, edition_sha256='2'*64), self.result)
        self.assertEqual(json.loads((self.repo / path).read_text(encoding='utf-8')), self.result)
        with self.assertRaisesRegex(ValueError, 'fresh receipt'):
            self.verify(path)
        self.verify_assets.assert_called_once()
        for unexpected in ('runtime/not-ignored.json', 'runtime/../outside.json', 'runtime/validation/a.wav'):
            with self.subTest(path=unexpected), self.assertRaises(ValueError):
                self.verify(unexpected)
            self.assertFalse((self.repo / unexpected).exists())

    def test_wrong_pins_never_write_a_receipt(self):
        path = 'runtime/validation/5090-audio.json'
        with self.assertRaisesRegex(ValueError, 'both SHA-256'):
            self.verify(path, edition_sha256='2' * 64)
        with self.assertRaisesRegex(ValueError, 'reviewed handoff'):
            self.verify(path, source_sha256='0' * 64, edition_sha256='2' * 64)
        self.assertFalse((self.repo / path).exists())


if __name__ == '__main__':
    unittest.main()
