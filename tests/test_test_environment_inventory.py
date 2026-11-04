from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/Get-TestEnvironmentInventory.ps1'
PS = shutil.which('pwsh') or shutil.which('powershell')


class InventorySourceTests(unittest.TestCase):
    def test_inventory_has_no_mutating_or_age_based_cleanup_path(self):
        source = SCRIPT.read_text()
        for forbidden in ('Remove-Item', 'Move-Item', 'DeleteDirectory', 'Start-Process',
                          'Stop-Process', 'LastWriteTime', 'CreationTime', '-Recurse'):
            self.assertNotIn(forbidden, source)
        for marker in ('deletes_files=$false', 'requires_dependency_review',
                       'Get-CimInstance Win32_Process', 'pyvenv_cfg_sha256',
                       'Assert-NoReparseAncestor', 'KeepEnvironmentName',
                       'ls-files --', '$tracked.Count', 'tracked_files=$tracked'):
            self.assertIn(marker, source)


@unittest.skipUnless(sys.platform == 'win32' and PS, 'Windows PowerShell inventory checks')
class InventoryPowerShellTests(unittest.TestCase):
    def call(self, *args, success=True, script=SCRIPT):
        result = subprocess.run([PS, '-NoProfile', '-NonInteractive', '-File', str(script), *args],
                                cwd=ROOT, text=True, capture_output=True, timeout=30)
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return json.loads(result.stdout)
        self.assertNotEqual(result.returncode, 0)
        return result

    def test_unrelated_repository_and_non_venv_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            subprocess.run(['git', 'init', '-q', folder], check=True)
            self.call('-Worktree', folder, '-EnvironmentName', 'test', success=False)
        self.call('-Worktree', str(ROOT), '-EnvironmentName', '../projects', success=False)

    def test_current_checkout_environment_is_kept_and_content_unchanged(self):
        # Only this empty synthetic fixture is created by the test, never a venv install.
        base = ROOT / '.venv'
        base.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='inventory-test-', dir=base) as folder:
            target = Path(folder)
            cfg = target / 'pyvenv.cfg'
            cfg.write_text('version = 3.11.9\n', encoding='utf-8')
            result = self.call('-Worktree', str(ROOT), '-EnvironmentName', target.name)
            self.assertFalse(result['deletes_files'])
            self.assertTrue(result['dependency_review_required'])
            item = result['environments'][0]
            self.assertEqual(item['classification'], 'keep')
            self.assertEqual(item['tracked_files'], [])
            self.assertEqual(item['bytes'], cfg.stat().st_size)
            self.assertEqual(len(item['pyvenv_cfg_sha256']), 64)
            self.assertEqual(cfg.read_text(), 'version = 3.11.9\n')

    def test_non_environment_directory_is_rejected(self):
        base = ROOT / '.venv'
        base.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='inventory-test-', dir=base) as folder:
            self.call('-Worktree', str(ROOT), '-EnvironmentName', Path(folder).name, success=False)

    def test_tracked_content_blocks_a_staging_environment(self):
        # Synthetic repositories only: never change the actual checkout's Git state.
        with tempfile.TemporaryDirectory() as folder:
            repo = Path(folder) / 'repo'
            staging = Path(folder) / 'staging'
            env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1')

            def git(*args):
                subprocess.run(['git', *args], check=True, env=env,
                               capture_output=True, text=True, timeout=20)

            git('init', '--template=', '-q', str(repo))
            script = repo / 'scripts' / SCRIPT.name
            script.parent.mkdir()
            shutil.copy2(SCRIPT, script)
            target = repo / '.venv' / 'tracked-test'
            target.mkdir(parents=True)
            (target / 'pyvenv.cfg').write_text('version = 3.11.9\n', encoding='utf-8')
            git('-C', str(repo), 'add', '.')
            git('-C', str(repo), '-c', 'user.name=FlexGPU Tests',
                '-c', 'user.email=tests@example.invalid', '-c', 'commit.gpgsign=false',
                'commit', '-qm', 'Synthetic inventory fixture')
            git('-C', str(repo), 'worktree', 'add', '--detach', str(staging), 'HEAD')
            result = self.call('-Worktree', str(staging), '-EnvironmentName', 'tracked-test',
                               script=script)
            item = result['environments'][0]
            self.assertEqual(item['classification'], 'blocked')
            self.assertEqual(item['tracked_files'], ['.venv/tracked-test/pyvenv.cfg'])
            self.assertTrue((staging / '.venv/tracked-test/pyvenv.cfg').exists())


if __name__ == '__main__':
    unittest.main()
