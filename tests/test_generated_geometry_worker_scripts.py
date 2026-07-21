from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMMON = ROOT / "scripts" / "_GeneratedGeometry.Common.ps1"
STOP = ROOT / "scripts" / "Stop-GeneratedGeometryWorker.ps1"
README = ROOT / "README.md"
MIGRATION = ROOT / "docs" / "5090_MIGRATION.md"


class GeneratedGeometryWorkerScriptTests(unittest.TestCase):
    def test_profile_guard_distinguishes_supported_physical_gpus(self) -> None:
        source = COMMON.read_text(encoding="utf-8")
        for marker in (
            "Assert-FlexGpuGeneratedGeometryProfile",
            "--query-gpu=index,name,memory.total",
            "'3080ti_16gb'",
            "'4090'",
            "'5090'",
            "RTX\\s+3080\\s+Ti",
            "RTX\\s+4090",
            "RTX\\s+5090",
            "Keep 3080 and 5090 launch profiles separate",
            "AllowProfileMismatch",
        ):
            self.assertIn(marker, source)

    def test_single_worker_guard_is_checkout_scoped(self) -> None:
        source = COMMON.read_text(encoding="utf-8")
        for marker in (
            "tools\\moge2_worker.py",
            "[regex]::Escape($workerPath)",
            "--backend\\s+(moge2|depth_anything)",
            "Assert-FlexGpuNoGeneratedGeometryWorker",
            "Stop-GeneratedGeometryWorker.ps1 -Stop",
        ):
            self.assertIn(marker, source)
        self.assertNotIn("depth_anything_worker.py", source)

    def test_stop_wrapper_is_preview_first_and_exactly_scoped(self) -> None:
        source = STOP.read_text(encoding="utf-8")
        for marker in (
            "[ValidateSet('all', 'moge2', 'depth_anything')]",
            "[switch]$Stop",
            "if (-not $Stop)",
            "Get-FlexGpuGeneratedGeometryWorkers",
            "Stop-Process -Id $worker.ProcessId",
            "No matching worker is running",
        ):
            self.assertIn(marker, source)
        self.assertNotIn("Get-Process python", source)
        self.assertNotIn("taskkill", source.casefold())

    def test_docs_define_separate_machine_local_identities(self) -> None:
        source = (
            README.read_text(encoding="utf-8")
            + MIGRATION.read_text(encoding="utf-8")
        )
        for marker in (
            "config/local-3080ti.json",
            "config/local-5090.json",
            "projects/*-3080ti-*.toe",
            "projects/*-5090-*.toe",
            "Stop-GeneratedGeometryWorker.ps1 -Stop",
            "-Profile 3080ti_16gb",
            "-Profile 5090",
            "hardware-neutral",
        ):
            self.assertIn(marker, source)


if __name__ == "__main__":
    unittest.main()
