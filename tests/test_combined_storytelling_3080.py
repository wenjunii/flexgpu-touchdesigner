from pathlib import Path
import importlib.util
import json
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('playlist_migration', ROOT / 'touchdesigner/update_combined_storytelling_3080.py')
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


class PlaylistMigrationTests(unittest.TestCase):
    def test_5090_is_rejected_before_network_access(self):
        lookup = MagicMock()
        for project_name in ('FlexShow-podcast-5090.71.toe', 'FlexShow-podcast-5090-3080.43.toe'):
            with self.subTest(project=project_name), self.assertRaisesRegex(RuntimeError, '3080'):
                migration.install('.', lookup=lookup, types={}, project_name=project_name, build='2025.32820')
        lookup.assert_not_called()

    def test_legacy_updater_refuses_a_playlist_before_changing_it(self):
        spec = importlib.util.spec_from_file_location('legacy_migration', ROOT / 'touchdesigner/update_combined_podcast_3080.py')
        legacy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(legacy)
        owner = SimpleNamespace(path=legacy.ADAPTER_PATH,
            par=SimpleNamespace(Playlistmanifest=SimpleNamespace(eval=lambda: 'reviewed/playlist.json')))
        with self.assertRaisesRegex(RuntimeError, 'reviewed storytelling playlist'):
            legacy.update_combined_podcast_3080(owner, '.', operator_types={}, project_name='FlexShow-podcast-3080.42.toe')

    def test_changed_reviewed_prompt_is_rejected_before_audio_or_td_access(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work = root / migration.EDITION
            work.mkdir(parents=True)
            (work / 'plan.json').write_text('{}')
            manifest = {'kind':'approved_continuous_image_hold_playlist','story_count':136,
                        'duration_ms':11938720,'executable':True,
                        'plans':{'original':{'path':(work/'plan.json').relative_to(root).as_posix(),
                                             'sha256':'0'*64}}}
            (work / 'playlist.json').write_text(json.dumps(manifest))
            with patch.object(migration.wave, 'open') as audio:
                with self.assertRaisesRegex(ValueError, 'hash differs'):
                    migration.read_edition(root)
                audio.assert_not_called()

    def test_runtime_audio_supports_combined_only_when_track_exists(self):
        spec = importlib.util.spec_from_file_location('runtime_builder', ROOT / 'touchdesigner/runtime_pipeline.py')
        runtime = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runtime)
        namespace = {}
        exec(runtime.SHOW_CONTROL_CALLBACKS, namespace)
        for present, wanted_source, wanted_index in ((True,'combined',2),(False,'voices',0)):
            with self.subTest(combined_track=present):
                switch = object()
                adapter = SimpleNamespace(op=lambda name: (object() if present else None) if name=='combined_audio'
                                          else switch if name=='audiosource_switch' else None)
                writes = []
                namespace.update(_controls=lambda: None,
                    _pipeline=lambda: SimpleNamespace(op=lambda name:adapter),
                    _value=lambda name, default=None: 'combined' if name=='Audiosource' else False,
                    _set=lambda node,name,value: writes.append((node,name,value)))
                namespace['_apply_audio_controls']()
                self.assertIn((adapter,'Audiosource',wanted_source), writes)
                self.assertIn((switch,'index',wanted_index), writes)


if __name__ == '__main__':
    unittest.main()
