from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('ci_scope', ROOT / 'tools/ci_scope.py')
scope = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scope)


class CIScopeTests(unittest.TestCase):
    def test_docs_allowlist_and_unknown_files_fail_closed(self):
        self.assertTrue(scope.documentation_only(['README.md', 'docs/MOGE2_LIVE.md', 'LICENSE']))
        for paths in ([], ['AGENTS.md'], ['docs/example.py'], ['.github/workflows/ci.yml'],
                      ['requirements-test.txt'], ['README.md', 'src/flexgpu/worker.py'],
                      ['README.MD'], ['new-file.md'], ['../README.md'], ['/README.md'],
                      ['docs\\README.md'], ['README.md\n::set-output name=docs_only::true']):
            self.assertFalse(scope.documentation_only(paths), paths)

    def test_invalid_refs_and_git_errors_do_not_downgrade_tests(self):
        with patch.object(scope.subprocess, 'run') as run:
            self.assertEqual(scope.changed_paths('--malicious-ref', 'a' * 40), [])
            self.assertEqual(scope.changed_paths('0' * 40, 'a' * 40), [])
            run.assert_not_called()
        with patch.object(scope.subprocess, 'run', side_effect=subprocess.TimeoutExpired('git', 30)):
            self.assertEqual(scope.changed_paths('a' * 40, 'b' * 40), [])

    def test_diff_does_not_hide_renamed_code_or_split_spaced_filenames(self):
        with patch.object(scope.subprocess, 'run', return_value=subprocess.CompletedProcess(
            [], 0, stdout=b'docs/old.md\0src/new file.py\0')) as run:
            paths = scope.changed_paths('a' * 40, 'b' * 40)
        self.assertIn('--no-renames', run.call_args.args[0])
        self.assertIn('-z', run.call_args.args[0])
        self.assertEqual(paths, ['docs/old.md', 'src/new file.py'])
        self.assertFalse(scope.documentation_only(paths))

    def test_github_output_is_a_fixed_boolean_only(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'github-output'
            with patch.object(scope, 'changed_paths', return_value=['README.md']):
                self.assertEqual(scope.main(['--github-output', str(output)]), 0)
            self.assertEqual(output.read_text(), 'docs_only=true\n')

    def test_ci_keeps_publication_gate_and_full_manual_release_matrix(self):
        source = (ROOT / '.github/workflows/ci.yml').read_text()
        self.assertNotIn('paths-ignore:', source)
        self.assertIn('cancel-in-progress: ${{ github.event_name == \'pull_request\' }}', source)
        self.assertIn('types: [published]', source)
        self.assertIn('needs: publication-safety', source)
        self.assertIn('linux-source-tests:', source)
        self.assertIn('"3.11", "3.12"', source)
        self.assertIn("outputs.docs_only != 'true'", source)
        self.assertIn('-Scope All -SelfTest -ExitWithCode', source)
        self.assertIn('BASE_SHA:', source)
        self.assertNotIn('git diff ${{', source)

    def test_dependabot_batches_routine_updates_without_suppressing_security(self):
        source = (ROOT / '.github/dependabot.yml').read_text()
        self.assertEqual(source.count('interval: monthly'), 2)
        self.assertEqual(source.count('applies-to: security-updates'), 2)
        self.assertEqual(source.count('applies-to: version-updates'), 2)
        self.assertEqual(source.count('open-pull-requests-limit: 1'), 2)
        self.assertNotIn('ignore:', source)
        for directory in ('/integrations/moge2', '/integrations/depth_anything'):
            self.assertIn(directory, source)


if __name__ == '__main__':
    unittest.main()
