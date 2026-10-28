from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
SMOKE = ROOT / "tools" / "smoke_worker_runtime.py"
spec = importlib.util.spec_from_file_location("security_smoke_test", SMOKE)
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


class DependencyMaintenanceTests(unittest.TestCase):
    def test_all_image_runtimes_use_same_patched_pillow(self):
        for path in (ROOT / "requirements-test.txt", *sorted((ROOT / "integrations").glob("*/requirements-runtime.txt"))):
            pins = [line for line in path.read_text().splitlines() if line.lower().startswith("pillow==")]
            self.assertEqual(pins, ["Pillow==13.0.0"], str(path))

    def test_source_ci_and_release_require_python_311(self):
        workflow = (ROOT / ".github/workflows/ci.yml").read_text()
        self.assertIn("'[\"3.11\", \"3.12\"]'", workflow)
        self.assertIn("|| '[\"3.11\"]'", workflow)
        release = (ROOT / "scripts/Test-FlexShowRelease.ps1").read_text()
        self.assertIn("sys.version_info >= (3, 11)", release)
        self.assertIn("m.version('Pillow') == '13.0.0'", release)

    def test_installers_check_new_and_existing_interpreters_before_pip(self):
        helper = (ROOT / "scripts/_WorkerPython.Common.ps1").read_text()
        self.assertIn("sys.version_info >= (3, 11)", helper)
        self.assertIn("| Out-Null", helper)
        for name in ("Initialize-DepthAnything.ps1", "Initialize-MoGe2.ps1"):
            source = (ROOT / "scripts" / name).read_text()
            self.assertIn("'_WorkerPython.Common.ps1'", source)
            self.assertLess(source.index("Assert-FlexGpuWorkerPython -Python $basePython"), source.index("'-m', 'venv'"))
            self.assertLess(source.index("Assert-FlexGpuWorkerPython -Python $venvPython"), source.index("'-m', 'pip'"))

    def test_smoke_preview_has_no_optional_imports_or_gpu_work(self):
        for provider in ("moge2", "depth_anything"):
            result = subprocess.run([sys.executable, "-S", str(SMOKE), "--provider", provider], capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["status"], "preview")

    def test_runtime_version_drift_fails_before_model_load(self):
        with patch.object(smoke.metadata, "version", return_value="0.0.0"):
            with self.assertRaisesRegex(RuntimeError, "installed.*expected"):
                smoke.check_versions("depth_anything")

    def test_old_python_is_rejected(self):
        with patch.object(smoke.sys, "version_info", (3, 10, 0)):
            with self.assertRaisesRegex(RuntimeError, "3.11"):
                smoke.check_versions("depth_anything")

    def test_changed_git_source_revision_is_rejected(self):
        distribution = Mock()
        distribution.read_text.return_value = '{"vcs_info":{"commit_id":"wrong"}}'
        with patch.object(smoke.metadata, "distribution", return_value=distribution):
            with self.assertRaisesRegex(RuntimeError, "source revision differs"):
                smoke.check_source_revision("moge", "expected")

    def test_consent_regression_detects_download_before_rejection(self):
        for download_first in (False, True):
            generation = ModuleType("transformers.generation.utils")
            generation.has_file = Mock(return_value=True)
            generation.get_cached_module_file = Mock()

            def load_custom_generate(self, model, *, trust_remote_code):
                self_test.assertFalse(trust_remote_code)
                if download_first:
                    generation.get_cached_module_file(model)
                raise ValueError("Trust consent denied")

            self_test = self
            generation.GenerationMixin = SimpleNamespace(load_custom_generate=load_custom_generate)
            parent = ModuleType("transformers.generation")
            parent.utils = generation
            with patch.dict(sys.modules, {"transformers.generation": parent}):
                if download_first:
                    with self.assertRaisesRegex(RuntimeError, "before trust consent"):
                        smoke.check_custom_generation_consent()
                else:
                    smoke.check_custom_generation_consent()


if __name__ == "__main__":
    unittest.main()
