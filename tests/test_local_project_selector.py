from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE_SCRIPT = ROOT / "scripts" / "Set-FlexShowLocalProject.ps1"


@unittest.skipUnless(os.name == "nt", "PowerShell helper is Windows-only")
class LocalProjectSelectorTests(unittest.TestCase):
    def setUp(self) -> None:
        powershell = shutil.which("powershell.exe") or shutil.which("powershell")
        if powershell is None:
            self.skipTest("Windows PowerShell is unavailable")
        self.powershell = powershell
        self.temporary = tempfile.TemporaryDirectory()
        self.repository = Path(self.temporary.name)
        (self.repository / "scripts").mkdir()
        (self.repository / "config").mkdir()
        (self.repository / "projects").mkdir()
        shutil.copy2(
            SOURCE_SCRIPT,
            self.repository / "scripts" / SOURCE_SCRIPT.name,
        )
        (self.repository / ".gitignore").write_text(
            "config/local-*.json\nprojects/*.toe\n!projects/FlexShow.toe\n",
            encoding="utf-8",
        )
        subprocess.run(
            ["git", "init", "--quiet"],
            cwd=self.repository,
            check=True,
        )
        self.old_project = self.repository / "projects" / "Show-3080.26.toe"
        self.new_project = self.repository / "projects" / "Show-3080.27.toe"
        self.old_project.write_bytes(b"old")
        self.new_project.write_bytes(b"new")
        self.config = self.repository / "config" / "local-3080.json"
        self._write_config("3080ti_16gb", self.old_project)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_config(self, tier: str, project: Path) -> None:
        self.config.write_text(
            json.dumps(
                {
                    "tier": tier,
                    "processes": {"world": {"project": str(project)}},
                    "render": {"installation_width": 1920},
                }
            ),
            encoding="utf-8",
        )

    def _run(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                self.powershell,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(self.repository / "scripts" / SOURCE_SCRIPT.name),
                *arguments,
            ],
            cwd=self.repository,
            check=check,
            capture_output=True,
            text=True,
        )

    def test_retargets_only_world_project_and_preserves_other_values(self) -> None:
        result = self._run(
            "-Config",
            str(self.config),
            "-Project",
            str(self.new_project),
            "-ExpectedTier",
            "3080ti_16gb",
        )
        self.assertIn('"status":  "updated"', result.stdout)
        written = json.loads(self.config.read_text(encoding="utf-8-sig"))
        self.assertTrue(
            Path(written["processes"]["world"]["project"]).samefile(
                self.new_project
            )
        )
        self.assertEqual(written["render"]["installation_width"], 1920)
        self.assertEqual(written["tier"], "3080ti_16gb")

    def test_whatif_does_not_change_config(self) -> None:
        before = self.config.read_bytes()
        result = self._run(
            "-Config",
            str(self.config),
            "-Project",
            str(self.new_project),
            "-ExpectedTier",
            "3080ti_16gb",
            "-WhatIf",
        )
        self.assertIn('"status":  "preview"', result.stdout)
        self.assertEqual(self.config.read_bytes(), before)

    def test_rejects_tier_and_filename_mixing(self) -> None:
        wrong_tier = self._run(
            "-Config",
            str(self.config),
            "-Project",
            str(self.new_project),
            "-ExpectedTier",
            "5090",
            check=False,
        )
        self.assertNotEqual(wrong_tier.returncode, 0)
        self.assertIn("does not match -ExpectedTier", wrong_tier.stderr)

        conflicting = self.repository / "projects" / "Show-5090.27.toe"
        conflicting.write_bytes(b"wrong machine")
        wrong_file = self._run(
            "-Config",
            str(self.config),
            "-Project",
            str(conflicting),
            "-ExpectedTier",
            "3080ti_16gb",
            check=False,
        )
        self.assertNotEqual(wrong_file.returncode, 0)
        self.assertIn("conflicts with local filename", wrong_file.stderr)

    def test_rejects_ambiguous_machine_local_filenames(self) -> None:
        ambiguous_project = self.repository / "projects" / "Show-latest.toe"
        ambiguous_project.write_bytes(b"ambiguous machine")
        wrong_project = self._run(
            "-Config",
            str(self.config),
            "-Project",
            str(ambiguous_project),
            "-ExpectedTier",
            "3080ti_16gb",
            check=False,
        )
        self.assertNotEqual(wrong_project.returncode, 0)
        self.assertIn("requires its own machine tag", wrong_project.stderr)

        ambiguous_config = self.repository / "config" / "local-show.json"
        original_config = self.config
        self.config = ambiguous_config
        try:
            self._write_config("3080ti_16gb", self.new_project)
        finally:
            self.config = original_config
        wrong_config = self._run(
            "-Config",
            str(ambiguous_config),
            "-Project",
            str(self.new_project),
            "-ExpectedTier",
            "3080ti_16gb",
            check=False,
        )
        self.assertNotEqual(wrong_config.returncode, 0)
        self.assertIn("requires its own machine tag", wrong_config.stderr)

    def test_accepts_distinct_5090_identity(self) -> None:
        project_5090 = self.repository / "projects" / "Show-5090.30.toe"
        project_5090.write_bytes(b"5090")
        config_5090 = self.repository / "config" / "local-5090.json"
        original_config = self.config
        self.config = config_5090
        try:
            self._write_config("5090", project_5090)
        finally:
            self.config = original_config
        result = self._run(
            "-Config",
            str(config_5090),
            "-Project",
            str(project_5090),
            "-ExpectedTier",
            "5090",
        )
        self.assertIn('"status":  "unchanged"', result.stdout)

    def test_rejects_tracked_canonical_project(self) -> None:
        canonical = self.repository / "projects" / "FlexShow.toe"
        canonical.write_bytes(b"tracked")
        result = self._run(
            "-Config",
            str(self.config),
            "-Project",
            str(canonical),
            "-ExpectedTier",
            "3080ti_16gb",
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("is not ignored by Git", result.stderr)


if __name__ == "__main__":
    unittest.main()
