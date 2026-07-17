from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "Start-MoGe2Worker.ps1"


class MoGe2WorkerScriptSourceTests(unittest.TestCase):
    def test_worker_start_is_preview_first_and_gpu_selectable(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        for marker in (
            "[switch]$Start",
            "if (-not $Start)",
            "$env:CUDA_VISIBLE_DEVICES = [string]$GpuIndex",
            "'--device', 'cuda:0'",
            "'3080ti_16gb', '4090', '5090'",
            "'--input-tcp-port'",
            "'--output-tcp-port'",
            "execution = 'foreground; press Ctrl+C to stop'",
        ):
            self.assertIn(marker, source)
        self.assertNotIn("Start-Process", source)
        self.assertNotIn("DownloadModel", source.split("if (-not $Start)", 1)[0])

    def test_network_warning_and_private_runtime_boundary_are_explicit(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("not authenticated or encrypted", source)
        self.assertIn("trusted private show network", source)
        self.assertIn("runtime\\moge2-model\\model.pt", source)
        self.assertNotIn("access_token", source.lower())
        self.assertNotIn("password", source.lower())


if __name__ == "__main__":
    unittest.main()
