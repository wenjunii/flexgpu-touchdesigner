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
            "'--target-pixels'",
            "$TargetPixels = 147456",
            "$MaxEdge = 512",
            "'1024x567 -> 512x284'",
            "execution = 'foreground; press Ctrl+C to stop'",
            "FlexGPU MoGe-2 Worker",
        ):
            self.assertIn(marker, source)
        self.assertNotIn("Start-Process", source)
        self.assertNotIn("DownloadModel", source.split("if (-not $Start)", 1)[0])

    def test_worker_waits_for_touchdesigner_result_listener(self) -> None:
        worker = (ROOT / "tools" / "moge2_worker.py").read_text(
            encoding="utf-8")
        self.assertIn('"--output-connect-timeout-s"', worker)
        self.assertIn("did not become", worker)
        self.assertIn('"ready within %.1f seconds; select the matching "', worker)
        self.assertIn('"geometry provider and enable its bridge"', worker)

        launcher = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("$ListenerWaitSeconds = 120.0", launcher)
        self.assertIn("'--output-connect-timeout-s'", launcher)
        self.assertIn("listener_wait_seconds = $ListenerWaitSeconds", launcher)

    def test_network_warning_and_private_runtime_boundary_are_explicit(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("not authenticated or encrypted", source)
        self.assertIn("trusted private show network", source)
        self.assertIn("runtime\\moge2-model\\model.pt", source)
        self.assertNotIn("access_token", source.lower())
        self.assertNotIn("password", source.lower())


if __name__ == "__main__":
    unittest.main()
