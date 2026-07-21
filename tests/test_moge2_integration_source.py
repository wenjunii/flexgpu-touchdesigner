from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "Initialize-MoGe2.ps1"
REQUIREMENTS = ROOT / "integrations" / "moge2" / "requirements-runtime.txt"
README = ROOT / "integrations" / "moge2" / "README.md"


class MoGe2IntegrationSourceTests(unittest.TestCase):
    def test_installer_requires_explicit_install_and_model_download(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        for marker in (
            "[switch]$Install",
            "[switch]$DownloadModel",
            "if (-not $Install -and -not $DownloadModel)",
            "HF_HUB_DISABLE_IMPLICIT_TOKEN",
            "HF_HUB_DISABLE_TELEMETRY",
            "torch==2.11.0",
            "torchvision==0.26.0",
            "--no-deps",
            "model-install",
        ):
            self.assertIn(marker, source)
        self.assertNotIn("Remove-Item", source)

    def test_upstream_git_dependencies_are_immutable(self) -> None:
        requirements = REQUIREMENTS.read_text(encoding="utf-8")
        self.assertIn(
            "utils3d.git@3fab839f0be9931dac7c8488eb0e1600c236e183",
            requirements,
        )
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn(
            "MoGe.git@'$mogeRevision".replace("'", ""),
            source.replace("$mogeRevision\"", "$mogeRevision"),
        )
        self.assertNotIn("@main", requirements + source)

    def test_documentation_keeps_private_artifacts_out_of_git(self) -> None:
        documentation = README.read_text(encoding="utf-8")
        for marker in (".venv/", "runtime/", "does not redistribute the checkpoint"):
            self.assertIn(marker, documentation)


if __name__ == "__main__":
    unittest.main()
