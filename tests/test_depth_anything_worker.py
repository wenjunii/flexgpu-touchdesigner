from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
WORKER_PATH = ROOT / "tools" / "depth_anything_worker.py"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from flexgpu.depth_anything_transport import decode_sensor_frame  # noqa: E402
from flexgpu.worldbus import WorldBusReceiver  # noqa: E402


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "flexgpu_test_depth_anything_worker", WORKER_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


worker_module = _load_module()


class DepthAnythingWorkerTests(unittest.TestCase):
    def test_profiles_and_preview_need_no_optional_runtime_or_camera(self) -> None:
        profiles = subprocess.run(
            [sys.executable, "-S", str(WORKER_PATH), "profiles"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(profiles.returncode, 0, profiles.stderr)
        payload = json.loads(profiles.stdout)
        self.assertEqual(payload["pins"]["model_revision"], worker_module.MODEL_REVISION)
        self.assertEqual(payload["pins"]["model_sha256"], worker_module.MODEL_SHA256)
        self.assertEqual(payload["profiles"]["3080ti_16gb"]["input_size"], 384)
        self.assertEqual(payload["profiles"]["3080ti_16gb"]["output_width"], 256)
        self.assertEqual(payload["profiles"]["3080ti_16gb"]["output_height"], 144)
        self.assertEqual(payload["profiles"]["3080ti_16gb"]["inference_hz"], 3.0)
        self.assertEqual(
            payload["output_limits"],
            {"max_width": 640, "max_height": 480, "max_pixels": 307200},
        )
        self.assertIs(payload["contains_rgb"], False)

        preview = subprocess.run(
            [sys.executable, "-S", str(WORKER_PATH), "serve", "--backend", "mock"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(preview.returncode, 0, preview.stderr)
        plan = json.loads(preview.stdout)
        self.assertEqual(plan["status"], "preview")
        self.assertEqual(plan["capture"], "mock")
        self.assertIs(plan["webcam_will_open"], False)
        self.assertEqual(plan["output_limits"]["max_pixels"], 307200)

    def test_worker_rejects_output_dimensions_beyond_receiver_limits(self) -> None:
        mapper = worker_module.FrozenPercentileMapper(
            mode="fixed", raw_low=0.0, raw_high=1.0
        )
        for width, height, message in (
            (641, 64, "output_width"),
            (64, 481, "output_height"),
        ):
            with self.subTest(size=(width, height)), self.assertRaisesRegex(
                worker_module.WorkerError, message
            ):
                worker_module.DepthAnythingSensorWorker(
                    worker_module.MockDepthBackend(),
                    mapper,
                    profile=worker_module.PROFILES["3080ti_16gb"],
                    output_width=width,
                    output_height=height,
                )
        with mock.patch.object(worker_module, "MAX_PIXELS", 10_000):
            with self.assertRaisesRegex(worker_module.WorkerError, "pixel receiver limit"):
                worker_module.DepthAnythingSensorWorker(
                    worker_module.MockDepthBackend(),
                    mapper,
                    profile=worker_module.PROFILES["3080ti_16gb"],
                    output_width=128,
                    output_height=128,
                )

    def test_session_percentiles_freeze_once_and_fixed_mapping_is_stable(self) -> None:
        import numpy as np

        mapper = worker_module.FrozenPercentileMapper(
            calibration_frames=3,
            percentile_low=10,
            percentile_high=90,
        )
        base = np.linspace(0.0, 1.0, 100, dtype=np.float32).reshape(10, 10)
        self.assertIsNone(mapper.observe_and_map(base))
        self.assertIsNone(mapper.observe_and_map(base + 0.1))
        mapped = mapper.observe_and_map(base + 0.2)
        self.assertIsNotNone(mapped)
        self.assertTrue(mapper.locked)
        frozen = (mapper.raw_low, mapper.raw_high, mapper.calibration_digest)
        mapper.observe_and_map(base * 100.0)
        self.assertEqual((mapper.raw_low, mapper.raw_high, mapper.calibration_digest), frozen)

        fixed = worker_module.FrozenPercentileMapper(
            mode="fixed",
            raw_low=0.0,
            raw_high=1.0,
            pseudo_near_m=0.5,
            pseudo_far_m=4.0,
            foreground_far_m=4.0,
        )
        output = fixed.observe_and_map(np.array([[0.0, 1.0] * 32], dtype=np.float32))
        self.assertIsNotNone(output)
        assert output is not None
        self.assertAlmostEqual(float(output.depth_metres[0, 0]), 4.0)
        self.assertAlmostEqual(float(output.depth_metres[0, 1]), 0.5)

    def test_newest_slot_supersedes_and_disconnect_discards_pending(self) -> None:
        slot = worker_module.LatestCaptureSlot()
        slot.put(worker_module.CapturedFrame(1, object()))
        slot.put(worker_module.CapturedFrame(2, object()))
        self.assertEqual(slot.get(0.0).timestamp_ns, 2)
        self.assertEqual(slot.stats["superseded"], 1)
        slot.put(worker_module.CapturedFrame(3, object()))
        slot.close("camera disconnected")
        with self.assertRaisesRegex(worker_module.CaptureError, "disconnected"):
            slot.get(0.0)

    def test_worker_output_contains_no_rgb_and_preserves_capture_timestamp(self) -> None:
        import numpy as np

        mapper = worker_module.FrozenPercentileMapper(
            mode="fixed", raw_low=0.0, raw_high=1.0
        )
        worker = worker_module.DepthAnythingSensorWorker(
            worker_module.MockDepthBackend(),
            mapper,
            profile=worker_module.PROFILES["3080ti_16gb"],
            output_width=64,
            output_height=64,
            producer_session_id="test-session",
            capture_source="synthetic",
        )
        bgr = np.zeros((64, 64, 3), dtype=np.uint8)
        timestamp = 1_234_567_890
        output = worker.process_capture(worker_module.CapturedFrame(timestamp, bgr))
        self.assertIsNotNone(output)
        assert output is not None
        self.assertEqual(output.metadata.timestamp_ns, timestamp)
        self.assertEqual(
            output.metadata.extensions["sensor_capture_timestamp_ns"], str(timestamp)
        )
        self.assertEqual(
            output.metadata.extensions["sensor_frame_id"], output.metadata.frame_id
        )
        self.assertIs(
            output.metadata.extensions["depth_anything_contains_rgb"], False
        )
        self.assertEqual(len(output.payload), 64 * 64 * 4)
        decoded = decode_sensor_frame(output.payload, width=64, height=64)
        self.assertGreater(sum(decoded.foreground_mask), 0)
        serialized = json.dumps(output.metadata.to_dict())
        self.assertNotIn("camera_rgb", serialized)
        self.assertNotIn("thumbnail", serialized)

    def test_loopback_service_uses_mock_capture_and_persistent_backend(self) -> None:
        mapper = worker_module.FrozenPercentileMapper(
            calibration_frames=1,
            percentile_low=2,
            percentile_high=98,
        )
        backend = worker_module.MockDepthBackend()
        worker = worker_module.DepthAnythingSensorWorker(
            backend,
            mapper,
            profile=worker_module.PROFILES["3080ti_16gb"],
            output_width=64,
            output_height=64,
            inference_hz=20,
            producer_session_id="loopback-session",
            capture_source="synthetic",
        )
        sink = WorldBusReceiver(host="127.0.0.1", tcp_port=0, udp_port=0).start()
        service = worker_module.SensorWorkerService(
            worker,
            lambda: worker_module.SyntheticCapture(width=64, height=64, fps=60),
            output_host=sink.tcp_address[0],
            output_tcp_port=sink.tcp_address[1],
            stale_after_ms=800,
        )
        try:
            service.start()
            report = service.serve(max_frames=3, duration_s=3.0)
            frame = sink.frames.get(timeout=2.0)
            self.assertEqual(report["inferred_captures"], 3)
            self.assertEqual(report["sent_frames"], 3)
            self.assertEqual(report["backend_load_count"], 1)
            self.assertEqual(report["backend_inference_count"], 3)
            self.assertIs(report["contains_rgb"], False)
            self.assertEqual(frame.metadata.pixel_format, "rgba8")
        finally:
            service.close()
            sink.close()

    def test_unexpected_capture_thread_exceptions_close_service_handoff(self) -> None:
        class FailedCapture:
            def __init__(self, error_type) -> None:
                self.error_type = error_type
                self.released = False

            def read(self):
                raise self.error_type("synthetic device failure")

            def release(self) -> None:
                self.released = True

        mapper = worker_module.FrozenPercentileMapper(
            mode="fixed", raw_low=0.0, raw_high=1.0
        )
        worker = worker_module.DepthAnythingSensorWorker(
            worker_module.MockDepthBackend(),
            mapper,
            profile=worker_module.PROFILES["3080ti_16gb"],
            output_width=64,
            output_height=64,
            producer_session_id="capture-failure",
            capture_source="synthetic",
        )
        for error_type in (OSError, LookupError):
            with self.subTest(error_type=error_type.__name__):
                source = FailedCapture(error_type)
                sink = WorldBusReceiver(
                    host="127.0.0.1", tcp_port=0, udp_port=None
                ).start()
                service = worker_module.SensorWorkerService(
                    worker,
                    lambda: source,
                    output_host=sink.tcp_address[0],
                    output_tcp_port=sink.tcp_address[1],
                )
                began = time.monotonic()
                try:
                    service.start()
                    with self.assertRaisesRegex(
                        worker_module.CaptureError, error_type.__name__
                    ):
                        service.serve(duration_s=3.0)
                    self.assertLess(time.monotonic() - began, 1.0)
                    self.assertTrue(source.released)
                finally:
                    service.close()
                    sink.close()

    def test_non_loopback_is_denied_before_backend_or_capture_start(self) -> None:
        code = worker_module.main(
            [
                "serve",
                "--backend",
                "mock",
                "--capture",
                "mock",
                "--output-host",
                "192.0.2.1",
                "--start",
            ]
        )
        self.assertEqual(code, 3)


if __name__ == "__main__":
    unittest.main()
