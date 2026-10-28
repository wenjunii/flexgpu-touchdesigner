from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
PS = shutil.which('pwsh') or shutil.which('powershell')
spec = importlib.util.spec_from_file_location('candidate_smoke', ROOT / 'tools/smoke_worker_runtime.py')
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


class MaintenanceContractTests(unittest.TestCase):
    def test_audit_is_not_gpu_or_live_acceptance(self):
        with patch.object(smoke, 'check_versions', return_value={'Pillow': '13.0.0'}), \
             patch.object(smoke, 'run_smoke') as gpu:
            result = smoke.audit_runtime('depth_anything')
        gpu.assert_not_called()
        self.assertEqual(result['mode'], 'audit_only')
        self.assertEqual(result['gpu_inference'], 'not_run')
        self.assertEqual(result['visual_acceptance'], 'not_tested')
        self.assertFalse(result['activated'])
        self.assertEqual(len(result['requirements_sha256']), 64)

    def test_audit_cli_cannot_also_load_gpu(self):
        result = subprocess.run([sys.executable, '-S', str(ROOT / 'tools/smoke_worker_runtime.py'),
                                 '--provider', 'depth_anything', '--run', '--audit-only'],
                                capture_output=True, text=True, timeout=20)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('not allowed', result.stderr)

    def test_release_defaults_full_and_reports_unchecked_work(self):
        source = (ROOT / 'scripts/Test-FlexShowRelease.ps1').read_text()
        self.assertIn("[string]$Mode = 'Full'", source)
        self.assertIn('source_release_accepted', source)
        self.assertIn("$Mode -eq 'Full' -and -not $SkipPublicSync", source)
        self.assertIn("'full_unit_suite', 'synthetic_benchmark', 'initializer_smoke'", source)
        self.assertIn("'local_runtime_readiness'", source)
        self.assertIn('Write-FlexGpuLocalReport', source)
        self.assertIn('-Config $Config -Json', source)

    def test_candidate_does_not_activate_or_install(self):
        source = (ROOT / 'scripts/Test-WorkerCandidate.ps1').read_text()
        for forbidden in ('pip install', 'Start-Process', 'Stop-Process', 'Set-Content', 'Copy-Item'):
            self.assertNotIn(forbidden, source)
        self.assertIn("$Profile -ne '3080ti_16gb'", source)
        self.assertIn('Assert-FlexGpuGeneratedGeometryProfile', source)
        self.assertIn('Assert-FlexGpuNoGeneratedGeometryWorker', source)
        self.assertIn('activated=$false', source)


@unittest.skipUnless(sys.platform == 'win32' and PS, 'Windows PowerShell wrapper tests')
class MaintenancePowerShellTests(unittest.TestCase):
    def run_ps(self, command, *, success=True):
        result = subprocess.run([PS, '-NoProfile', '-NonInteractive', '-Command', command],
                                cwd=ROOT, capture_output=True, text=True, timeout=30)
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def test_candidate_install_preview_has_separate_path_and_no_write(self):
        for name, provider in [('Initialize-MoGe2.ps1', 'moge2'),
                               ('Initialize-DepthAnything.ps1', 'depth-anything')]:
            result = self.run_ps(f"$p = & ./scripts/{name} -CandidateName 3080-unittest-preview; @($p)[-1]")
            plan = json.loads(result.stdout[result.stdout.index('{'):])
            self.assertEqual(plan['status'], 'preview')
            self.assertTrue(plan['environment'].endswith(f'{provider}-candidate-3080-unittest-preview'))
            self.assertFalse(Path(plan['environment']).exists())
            self.run_ps(f'& ./scripts/{name} -CandidateName ../unsafe', success=False)

    def test_existing_candidate_cannot_be_modified_by_installer(self):
        base = ROOT / '.venv'
        base.mkdir(exist_ok=True)
        candidate = '3080-test-' + uuid.uuid4().hex[:12]
        folder = base / ('moge2-candidate-' + candidate)
        folder.mkdir()
        try:
            result = self.run_ps(f'& ./scripts/Initialize-MoGe2.ps1 -CandidateName {candidate} -Install', success=False)
            self.assertIn('already exists', result.stderr)
            self.assertEqual(list(folder.iterdir()), [])
        finally:
            folder.rmdir()  # This test created only this empty directory.

    def test_all_worker_overrides_are_preview_only_and_missing_paths_fail(self):
        exe = str(Path(sys.executable)).replace("'", "''")
        for name in ('Start-MoGe2Worker.ps1', 'Start-DepthAnythingGeometryWorker.ps1', 'Start-DepthAnythingWorker.ps1'):
            args = ' -Capture mock' if name == 'Start-DepthAnythingWorker.ps1' else ''
            result = self.run_ps(f"$p = & ./scripts/{name} -Profile 3080ti_16gb -Backend mock -WorkerPython '{exe}'{args}; @($p)[-1]")
            plan = json.loads(result.stdout[result.stdout.index('{'):])
            self.assertEqual(plan['status'], 'preview')
            self.assertEqual(Path(plan['python']), Path(sys.executable))
            self.run_ps(f"& ./scripts/{name} -Profile 3080ti_16gb -Backend mock -WorkerPython './.venv/does-not-exist/python.exe'{args}", success=False)

    def test_report_guard_and_atomic_failure_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            root = folder.replace("'", "''")
            result = self.run_ps(
                ". ./scripts/_LocalVerification.Common.ps1; "
                f"Write-FlexGpuLocalReport -Root '{root}' -Path 'runtime/failure.json' -Value @{{status='fail'}}; "
                f"Get-Content -Raw -LiteralPath '{root}/runtime/failure.json'")
            self.assertEqual(json.loads(result.stdout)['status'], 'fail')
            for path in ('README.md', 'runtime/../outside.json', 'runtime-not-private/report.json'):
                self.run_ps(". ./scripts/_LocalVerification.Common.ps1; "
                            f"Resolve-FlexGpuLocalReportPath -Root '{root}' -Path '{path}'", success=False)
            self.assertEqual(list((Path(folder) / 'runtime').glob('*.tmp')), [])

    def test_candidate_preview_and_5090_gpu_rejection(self):
        exe = str(Path(sys.executable)).replace("'", "''")
        command = f"& ./scripts/Test-WorkerCandidate.ps1 -Provider depth_anything -Profile 3080ti_16gb -WorkerPython '{exe}'"
        result = self.run_ps(command)
        self.assertEqual(json.loads(result.stdout)['status'], 'preview')
        result = self.run_ps(command.replace('3080ti_16gb', '5090') + ' -RunGpuSmoke', success=False)
        self.assertIn('cannot certify', result.stderr)


if __name__ == '__main__':
    unittest.main()
