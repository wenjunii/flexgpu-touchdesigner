from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INITIALIZE = ROOT / "scripts" / "Initialize-DepthAnything.ps1"
START = ROOT / "scripts" / "Start-DepthAnythingWorker.ps1"
GEOMETRY_START = ROOT / "scripts" / "Start-DepthAnythingGeometryWorker.ps1"
REQUIREMENTS = ROOT / "integrations" / "depth_anything" / "requirements-runtime.txt"
README = ROOT / "integrations" / "depth_anything" / "README.md"
DOC = ROOT / "docs" / "DEPTH_ANYTHING_SENSOR.md"


class DepthAnythingIntegrationSourceTests(unittest.TestCase):
    def test_installer_is_isolated_preview_first_and_download_separate(self) -> None:
        source = INITIALIZE.read_text(encoding="utf-8")
        for marker in (
            ".venv\\depth-anything",
            "[switch]$Install",
            "[switch]$DownloadModel",
            "if (-not $Install -and -not $DownloadModel)",
            "torch==2.11.0",
            "torchvision==0.26.0",
            "model-install",
            "HF_HUB_DISABLE_IMPLICIT_TOKEN",
            "HF_HUB_DISABLE_TELEMETRY",
        ):
            self.assertIn(marker, source)
        self.assertNotIn("access_token", source.casefold())

    def test_runtime_dependencies_and_model_are_pinned(self) -> None:
        requirements = REQUIREMENTS.read_text(encoding="utf-8")
        for marker in (
            "numpy==2.2.6",
            "opencv-python==4.10.0.84",
            "transformers==4.52.4",
            "safetensors==0.5.3",
        ):
            self.assertIn(marker, requirements)
        installer = INITIALIZE.read_text(encoding="utf-8")
        self.assertIn("870a35c76c2bc1d82fbde922d95015496cb7dd6c", installer)
        self.assertIn("3152477ce0d8d6978d76b995120de97cb5b928701fd0f817769f59e249a16b70", installer)
        self.assertNotIn("@main", installer + requirements)

    def test_start_wrapper_requires_explicit_start_and_defaults_loopback(self) -> None:
        source = START.read_text(encoding="utf-8")
        for marker in (
            "[switch]$Start",
            "if (-not $Start)",
            "webcam_will_open",
            "[ValidateSet('auto', 'msmf', 'dshow', 'any')]",
            "'--camera-backend'",
            "camera_backend = $CameraBackend",
            "camera_resolution = @($CameraWidth, $CameraHeight)",
            "profile = $Profile",
            "[int]$CameraWidth = 640",
            "[int]$CameraHeight = 480",
            "[double]$InferenceHz = 5.0",
            "'127.0.0.1'",
            "[int]$OutputTcpPort = 9241",
            "$env:CUDA_VISIBLE_DEVICES = [string]$GpuIndex",
            "'--stale-after-ms'",
            "'--allow-trusted-network'",
            "[ValidateRange(64, 640)]",
            "[ValidateRange(64, 480)]",
            "307200",
            "reserved_udp_metadata",
            "contains_rgb = $false",
        ):
            self.assertIn(marker, source)
        self.assertNotIn("Start-Process", source)

    def test_mock_start_can_use_path_python_without_optional_runtime(self) -> None:
        source = START.read_text(encoding="utf-8")
        self.assertIn("if ($Backend -ne 'mock')", source)
        self.assertIn("Get-Command python -CommandType Application", source)
        self.assertIn("Mock mode is using PATH Python", source)
        self.assertIn("Mock mode needs Python with NumPy on PATH", source)

    def test_docs_keep_worker_optional_private_and_replaceable(self) -> None:
        documentation = README.read_text(encoding="utf-8") + DOC.read_text(encoding="utf-8")
        for marker in (
            "default-off",
            "does not serialize or transport RGB",
            "paid Depth Anything app",
            "replace this worker",
            "does not redistribute model",
            "Base/Large/Giant",
            "session_frozen",
            "newest",
        ):
            self.assertIn(marker, documentation)

    def test_generated_geometry_wrapper_is_separate_preview_first_and_no_camera(self) -> None:
        source = GEOMETRY_START.read_text(encoding="utf-8")
        for marker in (
            "[switch]$Start",
            "if (-not $Start)",
            "generated_image_geometry",
            "geometry_provider = 'depth_anything'",
            "'--provider', 'depth_anything'",
            "[int]$InputTcpPort = 9251",
            "[int]$InputUdpPort = 9250",
            "[int]$OutputTcpPort = 9261",
            "$ListenerWaitSeconds = 120.0",
            "'--output-connect-timeout-s'",
            "listener_wait_seconds = $ListenerWaitSeconds",
            "'--target-pixels'",
            "$TargetPixels = 147456",
            "$MaxEdge = 512",
            "'1024x567 -> 512x284'",
            ".venv\\depth-anything",
            "contains_generated_rgb = $true",
            "opens_webcam = $false",
            "FlexGPU Depth Anything Geometry Worker",
        ):
            self.assertIn(marker, source)
        self.assertNotIn("camera-index", source)
        self.assertNotIn("Start-Process", source)


if __name__ == "__main__":
    unittest.main()
