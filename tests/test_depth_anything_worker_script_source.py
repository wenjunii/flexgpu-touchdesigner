from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "tools" / "depth_anything_worker.py"


class DepthAnythingWorkerScriptSourceTests(unittest.TestCase):
    def test_worker_has_no_rgb_persistence_or_transport_path(self) -> None:
        source = WORKER.read_text(encoding="utf-8")
        for forbidden in (
            "cv2.imwrite",
            "VideoWriter",
            "Image.save",
            "write_replay",
            "source_rgba",
            "rgba8_atlas",
        ):
            self.assertNotIn(forbidden, source)
        transport = (ROOT / "src" / "flexgpu" / "depth_anything_transport.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('"depth_anything_contains_rgb": False', transport)
        self.assertIn('"contains_rgb": False', source)

    def test_camera_open_is_below_explicit_start_gate(self) -> None:
        source = WORKER.read_text(encoding="utf-8")
        gate = source.index("if not args.start:")
        backend = source.index("backend = _make_backend(args)", gate)
        service = source.index("service.start()", backend)
        camera = source.index("cv2.VideoCapture")
        self.assertLess(gate, backend)
        self.assertLess(backend, service)
        # VideoCapture exists only in the lazily invoked source constructor.
        self.assertGreater(camera, 0)

    def test_contract_pins_and_offline_inference_are_literal(self) -> None:
        source = WORKER.read_text(encoding="utf-8")
        for marker in (
            'MODEL_REPOSITORY = "depth-anything/Depth-Anything-V2-Small-hf"',
            'MODEL_REVISION = "870a35c76c2bc1d82fbde922d95015496cb7dd6c"',
            'MODEL_SHA256 = "3152477ce0d8d6978d76b995120de97cb5b928701fd0f817769f59e249a16b70"',
            'os.environ["HF_HUB_OFFLINE"] = "1"',
            'os.environ["TRANSFORMERS_OFFLINE"] = "1"',
            "local_files_only=True",
            "trust_remote_code=False",
        ):
            self.assertIn(marker, source)

    def test_capture_failures_and_output_allocations_are_bounded(self) -> None:
        source = WORKER.read_text(encoding="utf-8")
        for marker in (
            "except Exception as exc:",
            "self.slot.close(type(exc).__name__",
            "MAX_WIDTH",
            "MAX_HEIGHT",
            "MAX_PIXELS",
            "_validate_output_dimensions",
            '"opened": False',
            "CAP_MSMF",
            "--camera-backend",
            "camera_backend_selected",
            "camera_open_ms",
            "result_connection_refreshed_after_camera_open",
        ):
            self.assertIn(marker, source)


if __name__ == "__main__":
    unittest.main()
