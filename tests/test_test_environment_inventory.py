from __future__ import annotations

import json
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
                       'Assert-NoReparseAncestor', 'KeepEnvironmentName'):
            self.assertIn(marker, source)


@unittest.skipUnless(sys.platform == 'win32' and PS, 'Windows PowerShell inventory checks')
class InventoryPowerShellTests(unittest.TestCase):
    def call(self, *args, success=True):
        result = subprocess.run([PS, '-NoProfile', '-NonInteractive', '-File', str(SCRIPT), *args],
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
            self.assertEqual(item['bytes'], cfg.stat().st_size)
            self.assertEqual(len(item['pyvenv_cfg_sha256']), 64)
            self.assertEqual(cfg.read_text(), 'version = 3.11.9\n')

    def test_non_environment_directory_is_rejected(self):
        base = ROOT / '.venv'
        base.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='inventory-test-', dir=base) as folder:
            self.call('-Worktree', str(ROOT), '-EnvironmentName', Path(folder).name, success=False)


if __name__ == '__main__':
    unittest.main()
