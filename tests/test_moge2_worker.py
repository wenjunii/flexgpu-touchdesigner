from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import time
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
WORKER_PATH = ROOT / "tools" / "moge2_worker.py"
PROBE_PATH = ROOT / "tools" / "moge2_probe.py"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from flexgpu.moge2_transport import decode_moge2_atlas  # noqa: E402
from flexgpu.worldbus import (  # noqa: E402
    TCPFrameSender,
    WorldBusReceiver,
    make_frame,
)


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


worker_module = _load_module("flexgpu_test_moge2_worker", WORKER_PATH)
probe_module = _load_module("flexgpu_test_moge2_probe", PROBE_PATH)
HAS_RUNTIME_ARRAYS = (
    importlib.util.find_spec("numpy") is not None
    and importlib.util.find_spec("PIL") is not None
)


IDENTITY = [
    1.0, 0.0, 0.0, 0.0,
    0.0, 1.0, 0.0, 0.0,
    0.0, 0.0, 1.0, 0.0,
    0.0, 0.0, 0.0, 1.0,
]


def source_frame(
    frame_id: int,
    *,
    width: int = 4,
    height: int = 2,
    generation_id: str = "generation-a",
    producer_session_id: str = "stream-session-a",
    private_extensions: bool = False,
    geometry_provider: str = "moge2",
):
    payload = bytes(
        component
        for index in range(width * height)
        for component in (
            (index * 11 + frame_id) % 256,
            (index * 17 + 2) % 256,
            (index * 23 + 3) % 256,
            (index * 29 + 4) % 256,
        )
    )
    metadata = {
        "worldbus_version": 1,
        "frame_id": frame_id,
        "timestamp_ns": str(1_000_000_000 + frame_id),
        "width": width,
        "height": height,
        "pixel_format": "rgba8",
        "payload_bytes": len(payload),
        "intrinsics": [width * 0.8, height * 0.8, width * 0.5, height * 0.5],
        "depth_scale_bias": [1.0, 0.0],
        "camera_to_world": IDENTITY,
        "generation_id": generation_id,
        "producer_session_id": producer_session_id,
        "geometry_provider": geometry_provider,
    }
    if private_extensions:
        metadata.update(
            {
                "private_path": "C:/never/forward/this/path",
                "access_token": "<provided-at-runtime>",
                "prompt": "never-forward-this-private-prompt",
            }
        )
    return make_frame(metadata, payload)


class RecordingMockBackend(worker_module.MockBackend):
    def __init__(self) -> None:
        super().__init__()
        self.last_rgb_bytes = b""
        self.last_rgb_shape = ()

    def infer(self, rgb, *, num_tokens, fov_x_deg):
        self.last_rgb_bytes = rgb.tobytes(order="C")
        self.last_rgb_shape = tuple(rgb.shape)
        return super().infer(
            rgb,
            num_tokens=num_tokens,
            fov_x_deg=fov_x_deg,
        )


class DirtyBackend:
    model_id = "flexgpu/dirty-test"
    model_revision = "0" * 40
    precision = "mock"
    load_count = 1

    def __init__(self) -> None:
        self.inference_count = 0

    def infer(self, rgb, *, num_tokens, fov_x_deg):
        del rgb, num_tokens, fov_x_deg
        import numpy as np

        self.inference_count += 1
        return worker_module.BackendOutput(
            depth=np.array([[float("nan"), 1.5], [2.0, -3.0]], dtype=np.float32),
            mask=np.array([[1.0, 1.0], [0.0, 1.0]], dtype=np.float32),
            intrinsics=np.array(
                [[0.8, 0.0, 0.5], [0.0, 0.8, 0.5], [0.0, 0.0, 1.0]],
                dtype=np.float32,
            ),
        )


class MoGe2WorkerTests(unittest.TestCase):
    def make_worker(self, backend=None, *, max_edge=None, target_pixels=None):
        selected = worker_module.MockBackend() if backend is None else backend
        return worker_module.MoGe2Worker(
            selected,
            profile=worker_module.PROFILES["3080ti_16gb"],
            max_edge=max_edge,
            target_pixels=target_pixels,
            producer_session_id="worker-test-session",
        )

    def test_profiles_are_dependency_free_and_pins_match_probe(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-S", str(WORKER_PATH), "profiles"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(set(payload["profiles"]), {"3080ti_16gb", "4090", "5090"})
        self.assertEqual(worker_module.MOGE_SOURCE_REVISION, probe_module.MOGE_SOURCE_REVISION)
        self.assertEqual(worker_module.MODEL_REVISION, probe_module.MODEL_REVISION)
        self.assertEqual(worker_module.MODEL_SHA256, probe_module.MODEL_SHA256)
        self.assertEqual(worker_module.MODEL_BYTES, probe_module.MODEL_BYTES)
        self.assertEqual(
            {name: asdict(profile) for name, profile in worker_module.PROFILES.items()},
            {name: asdict(profile) for name, profile in probe_module.PROFILES.items()},
        )
        source = WORKER_PATH.read_text(encoding="utf-8")
        self.assertNotIn("hf_hub_download", source)
        self.assertIn('os.environ["HF_HUB_OFFLINE"] = "1"', source)

    @unittest.skipUnless(HAS_RUNTIME_ARRAYS, "optional NumPy/Pillow runtime is absent")
    def test_resize_sync_fov_lock_and_privacy_boundary(self) -> None:
        backend = RecordingMockBackend()
        worker = self.make_worker(backend, max_edge=64)
        first = worker.process_frame(
            source_frame(1, width=100, height=50, private_extensions=True)
        )
        self.assertEqual((first.metadata.width, first.metadata.height), (128, 32))
        decoded = decode_moge2_atlas(
            first.payload,
            atlas_width=first.metadata.width,
            height=first.metadata.height,
            depth_scale=first.metadata.depth_scale_bias[0],
            depth_bias=first.metadata.depth_scale_bias[1],
        )
        self.assertEqual(backend.last_rgb_shape, (32, 64, 3))
        decoded_rgb = bytes(
            channel
            for index, channel in enumerate(decoded.source_rgba)
            if index % 4 != 3
        )
        self.assertEqual(decoded_rgb, backend.last_rgb_bytes)
        self.assertTrue(
            all(
                decoded.source_rgba[index] == 255
                for index in range(3, len(decoded.source_rgba), 4)
            )
        )

        extensions = first.metadata.extensions
        self.assertEqual(extensions["moge2_source_frame_id"], 1)
        self.assertEqual(extensions["moge2_source_timestamp_ns"], "1000000001")
        self.assertEqual(
            extensions["moge2_source_producer_session_id"], "stream-session-a"
        )
        self.assertFalse(extensions["fov_locked"])
        self.assertGreater(extensions["fov_x_deg"], 1.0)
        self.assertAlmostEqual(first.metadata.intrinsics[0], 0.85 * 64, places=4)
        self.assertAlmostEqual(first.metadata.intrinsics[1], 0.85 * 64, places=4)
        self.assertAlmostEqual(first.metadata.intrinsics[2], 32.0, places=4)
        self.assertAlmostEqual(first.metadata.intrinsics[3], 16.0, places=4)
        self.assertEqual(first.metadata.camera_to_world, tuple(IDENTITY))
        output_metadata = first.metadata.to_dict()
        for key in ("private_path", "access_token", "prompt"):
            self.assertNotIn(key, output_metadata)
        serialized = json.dumps(output_metadata)
        for secret in (
            "C:/never/forward/this/path",
            "<provided-at-runtime>",
            "never-forward-this-private-prompt",
        ):
            self.assertNotIn(secret, serialized)

        second = worker.process_frame(source_frame(2, width=100, height=50))
        self.assertTrue(second.metadata.extensions["fov_locked"])
        self.assertAlmostEqual(
            second.metadata.extensions["fov_x_deg"],
            first.metadata.extensions["fov_x_deg"],
        )
        third = worker.process_frame(
            source_frame(3, width=100, height=50, generation_id="generation-b")
        )
        fourth = worker.process_frame(
            source_frame(4, width=80, height=50, generation_id="generation-b")
        )
        fifth = worker.process_frame(
            source_frame(
                5,
                width=80,
                height=50,
                generation_id="generation-b",
                producer_session_id="stream-session-b",
            )
        )
        self.assertFalse(third.metadata.extensions["fov_locked"])
        self.assertFalse(fourth.metadata.extensions["fov_locked"])
        self.assertFalse(fifth.metadata.extensions["fov_locked"])
        self.assertEqual(backend.requested_fov_x[0], None)
        self.assertAlmostEqual(
            backend.requested_fov_x[1], first.metadata.extensions["fov_x_deg"]
        )
        self.assertEqual(backend.requested_fov_x[2:], [None, None, None])
        self.assertEqual(backend.load_count, 1)
        self.assertEqual(worker.fov_resets, 4)
        self.assertEqual(worker.session_rollovers, 3)
        self.assertEqual(first.metadata.frame_id, 0)
        self.assertEqual(second.metadata.frame_id, 1)
        self.assertEqual(third.metadata.frame_id, 0)
        self.assertEqual(fourth.metadata.frame_id, 0)
        self.assertEqual(fifth.metadata.frame_id, 0)
        first_session = first.metadata.extensions["producer_session_id"]
        self.assertEqual(second.metadata.extensions["producer_session_id"], first_session)
        self.assertEqual(
            third.metadata.extensions["producer_session_id"], first_session + "-r1"
        )
        self.assertEqual(
            fourth.metadata.extensions["producer_session_id"], first_session + "-r2"
        )
        self.assertEqual(
            fifth.metadata.extensions["producer_session_id"], first_session + "-r3"
        )
        self.assertEqual(
            second.metadata.intrinsics,
            first.metadata.intrinsics,
        )

    @unittest.skipUnless(HAS_RUNTIME_ARRAYS, "optional NumPy/Pillow runtime is absent")
    def test_3080_pixel_budget_preserves_square_and_widescreen_aspect(self) -> None:
        backend = RecordingMockBackend()
        worker = self.make_worker(
            backend, max_edge=512, target_pixels=147456
        )
        square = worker.process_frame(source_frame(1, width=512, height=512))
        self.assertEqual(backend.last_rgb_shape, (384, 384, 3))
        self.assertEqual((square.metadata.width, square.metadata.height), (768, 384))

        widescreen = worker.process_frame(
            source_frame(2, width=1024, height=576)
        )
        self.assertEqual(backend.last_rgb_shape, (288, 512, 3))
        self.assertEqual(
            (widescreen.metadata.width, widescreen.metadata.height),
            (1024, 288),
        )
        self.assertEqual(worker.session_rollovers, 1)
        self.assertEqual(widescreen.metadata.frame_id, 0)
        self.assertNotEqual(
            square.metadata.extensions["producer_session_id"],
            widescreen.metadata.extensions["producer_session_id"],
        )

        near_widescreen = worker.process_frame(
            source_frame(3, width=1024, height=567)
        )
        self.assertEqual(backend.last_rgb_shape, (284, 512, 3))
        self.assertEqual(
            (near_widescreen.metadata.width, near_widescreen.metadata.height),
            (1024, 284),
        )
        self.assertEqual(worker.session_rollovers, 2)
        self.assertEqual(near_widescreen.metadata.frame_id, 0)

    def test_bridge_ports_and_session_ids_are_bounded(self) -> None:
        parser = worker_module.build_parser()
        parsed = parser.parse_args(["serve", "--output-tcp-port", "9321"])
        self.assertEqual(parsed.input_tcp_port, 9211)
        self.assertEqual(parsed.input_udp_port, 9210)

        with self.assertRaisesRegex(worker_module.WorkerError, "producer_session_id"):
            worker_module.MoGe2Worker(
                worker_module.MockBackend(),
                profile=worker_module.PROFILES["3080ti_16gb"],
                producer_session_id="unsafe/session",
            )
        bounded = worker_module.MoGe2Worker(
            worker_module.MockBackend(),
            profile=worker_module.PROFILES["3080ti_16gb"],
            producer_session_id="s" * 64,
        )
        bounded._roll_output_session()
        self.assertEqual(len(bounded.producer_session_id), 64)
        self.assertTrue(bounded.producer_session_id.endswith("-r1"))
        with self.assertRaisesRegex(worker_module.WorkerError, "producer_session_id"):
            worker_module.MoGe2Worker(
                worker_module.MockBackend(),
                profile=worker_module.PROFILES["3080ti_16gb"],
                producer_session_id="s" * 65,
            )
        with self.assertRaisesRegex(worker_module.WorkerError, "output_tcp_port"):
            worker_module.MoGe2WorkerService(
                self.make_worker(),
                input_host="127.0.0.1",
                input_tcp_port=0,
                input_udp_port=0,
                output_host="127.0.0.1",
                output_tcp_port=0,
            )

    def test_result_receiver_connection_retries_during_touchdesigner_startup(self) -> None:
        sender = mock.Mock()
        service = worker_module.MoGe2WorkerService(
            self.make_worker(),
            input_host="127.0.0.1",
            input_tcp_port=0,
            input_udp_port=0,
            output_host="127.0.0.1",
            output_tcp_port=9221,
            output_connect_timeout_s=2.0,
            output_connect_retry_s=0.01,
        )
        try:
            with (
                mock.patch.object(
                    worker_module, "TCPFrameSender",
                    side_effect=[ConnectionRefusedError(), sender],
                ) as connect,
                mock.patch.object(worker_module.time, "sleep") as sleep,
            ):
                self.assertIs(service.start(), service)
            self.assertIs(service.sender, sender)
            self.assertEqual(connect.call_count, 2)
            sleep.assert_called_once()
        finally:
            service.close()

    def test_result_receiver_timeout_has_actionable_error(self) -> None:
        service = worker_module.MoGe2WorkerService(
            self.make_worker(),
            input_host="127.0.0.1",
            input_tcp_port=0,
            input_udp_port=0,
            output_host="127.0.0.1",
            output_tcp_port=9221,
            output_connect_timeout_s=0.0,
        )
        with (
            mock.patch.object(
                worker_module, "TCPFrameSender",
                side_effect=ConnectionRefusedError(),
            ),
            self.assertRaisesRegex(
                worker_module.WorkerError,
                "select the matching geometry provider",
            ),
        ):
            service.start()

    @unittest.skipUnless(HAS_RUNTIME_ARRAYS, "optional NumPy/Pillow runtime is absent")
    def test_invalid_depth_and_mask_are_sanitized_before_packing(self) -> None:
        worker = self.make_worker(DirtyBackend())
        output = worker.process_frame(source_frame(1, width=2, height=2))
        decoded = decode_moge2_atlas(
            output.payload,
            atlas_width=output.metadata.width,
            height=output.metadata.height,
            depth_scale=output.metadata.depth_scale_bias[0],
            depth_bias=output.metadata.depth_scale_bias[1],
        )
        self.assertEqual(decoded.mask, (False, True, False, False))
        self.assertEqual(decoded.packed_depth[0], 0)
        self.assertEqual(decoded.packed_depth[1], 1500)
        self.assertEqual(decoded.packed_depth[2:], (0, 0))

    def test_non_rgba8_input_is_rejected(self) -> None:
        source = source_frame(1)
        metadata = source.metadata.to_dict()
        metadata["width"] *= 2
        metadata["pixel_format"] = "rgba8_atlas"
        metadata["payload_bytes"] = len(source.payload) * 2
        atlas = make_frame(metadata, source.payload * 2)
        with self.assertRaisesRegex(worker_module.WorkerError, "rgba8"):
            self.make_worker().process_frame(atlas)

    @unittest.skipUnless(HAS_RUNTIME_ARRAYS, "optional NumPy/Pillow runtime is absent")
    def test_depth_anything_provider_identity_uses_the_same_atomic_atlas(self) -> None:
        worker = worker_module.MoGe2Worker(
            worker_module.MockBackend(),
            profile=worker_module.PROFILES["3080ti_16gb"],
            provider="depth_anything",
            producer_session_id="depth-anything-worker-test",
        )
        output = worker.process_frame(
            source_frame(1, geometry_provider="depth_anything")
        )
        self.assertEqual(
            output.metadata.extensions["geometry_provider"], "depth_anything"
        )
        self.assertEqual(output.metadata.pixel_format, "rgba8_atlas")
        with self.assertRaisesRegex(worker_module.WorkerError, "provider"):
            worker.process_frame(source_frame(2, geometry_provider="moge2"))

    @unittest.skipUnless(HAS_RUNTIME_ARRAYS, "optional NumPy/Pillow runtime is absent")
    def test_depth_anything_geometry_backend_freezes_relative_depth(self) -> None:
        import numpy as np

        module = worker_module._load_depth_anything_module()

        class RelativeBackend:
            load_count = 1
            inference_count = 0

            def infer(self, rgb, *, input_size, output_width, output_height):
                del rgb, input_size
                self.inference_count += 1
                return np.linspace(
                    0.1, 1.0, output_width * output_height, dtype=np.float32
                ).reshape(output_height, output_width)

        backend = object.__new__(worker_module.DepthAnythingGeometryBackend)
        backend._module = module
        backend._backend = RelativeBackend()
        backend.input_size = 384
        backend.default_fov_x_deg = 60.0
        backend._mapper_arguments = {
            "mode": "session_frozen",
            "percentile_low": 2.0,
            "percentile_high": 98.0,
            "calibration_frames": 1,
            "raw_order": "near_is_larger",
            "pseudo_near_m": 0.5,
            "pseudo_far_m": 4.0,
            "foreground_far_m": 4.0,
        }
        backend._mapper = module.FrozenPercentileMapper(
            **backend._mapper_arguments
        )
        output = backend.infer(
            np.zeros((8, 8, 3), dtype=np.uint8),
            num_tokens=1200,
            fov_x_deg=None,
        )
        self.assertEqual(output.depth.shape, (8, 8))
        self.assertEqual(output.mask.shape, (8, 8))
        self.assertTrue(backend.calibration_locked)
        self.assertEqual(backend.calibration_observed_frames, 1)
        self.assertAlmostEqual(float(output.intrinsics[0, 0]), 0.8660254, places=5)
        backend.begin_source_session()
        self.assertFalse(backend.calibration_locked)
        self.assertEqual(backend.calibration_observed_frames, 0)

    @unittest.skipUnless(HAS_RUNTIME_ARRAYS, "optional NumPy/Pillow runtime is absent")
    def test_tcp_loopback_uses_newest_input_and_persistent_backend(self) -> None:
        sink = WorldBusReceiver(host="127.0.0.1", tcp_port=0, udp_port=0).start()
        service = worker_module.MoGe2WorkerService(
            self.make_worker(),
            input_host="127.0.0.1",
            input_tcp_port=0,
            input_udp_port=0,
            output_host=sink.tcp_address[0],
            output_tcp_port=sink.tcp_address[1],
        )
        try:
            service.start()
            with TCPFrameSender(*service.input_tcp_address) as sender:
                sender.send(source_frame(1))
                sender.send(source_frame(2))
            deadline = time.monotonic() + 2.0
            while service.receiver.frames.stats["accepted"] < 2:
                if time.monotonic() >= deadline:
                    self.fail("worker input receiver did not accept both loopback frames")
                time.sleep(0.01)
            report = service.serve(max_frames=1, duration_s=2.0)
            output = sink.frames.get(timeout=2.0)
            self.assertEqual(report["received_frames"], 1)
            self.assertEqual(report["sent_frames"], 1)
            self.assertEqual(report["backend_load_count"], 1)
            self.assertEqual(report["backend_inference_count"], 1)
            self.assertGreaterEqual(report["input_queue"]["superseded"], 1)
            self.assertEqual(output.metadata.extensions["moge2_source_frame_id"], 2)
            self.assertEqual(output.metadata.pixel_format, "rgba8_atlas")
        finally:
            service.close()
            sink.close()


if __name__ == "__main__":
    unittest.main()
