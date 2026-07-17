from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from flexgpu.worldbus import (  # noqa: E402
    FRAME_MAGIC,
    FRAME_PREFIX,
    FrameStreamDecoder,
    FramingError,
    HeartbeatMonitor,
    NewestFrameQueue,
    SizeLimitError,
    TCPFrameSender,
    UDPJsonEndpoint,
    ValidationError,
    WorldBusError,
    WorldBusLimits,
    WorldBusReceiver,
    decode_frame,
    decode_udp_message,
    encode_frame,
    encode_udp_message,
    generate_replay_frames,
    iter_replay,
    make_control,
    make_frame,
    make_heartbeat,
    make_metadata_message,
    replay_summary,
    validate_metadata,
    write_replay,
)


IDENTITY = [
    1.0,
    0.0,
    0.0,
    0.0,
    0.0,
    1.0,
    0.0,
    0.0,
    0.0,
    0.0,
    1.0,
    0.0,
    0.0,
    0.0,
    0.0,
    1.0,
]


def metadata(frame_id: int = 1, **overrides):
    value = {
        "worldbus_version": 1,
        "frame_id": frame_id,
        "timestamp_ns": str(1_000_000_000 + frame_id),
        "width": 2,
        "height": 1,
        "pixel_format": "rgba8_atlas",
        "payload_bytes": 8,
        "intrinsics": [2.0, 2.0, 1.0, 0.5],
        "depth_scale_bias": [1.0, 0.0],
        "camera_to_world": IDENTITY,
        "generation_id": "test-generation",
    }
    value.update(overrides)
    return value


def frame(frame_id: int = 1, *, producer_session_id: str | None = None):
    values = metadata(frame_id)
    if producer_session_id is not None:
        values["producer_session_id"] = producer_session_id
    return make_frame(values, bytes([frame_id & 255]) * 8)


class WorldBusValidationTests(unittest.TestCase):
    def test_metadata_normalizes_int64_and_preserves_additive_extensions(self) -> None:
        value = metadata(scene={"name": "demo", "weight": 0.5})
        normalized = validate_metadata(value)
        self.assertEqual(normalized.timestamp_ns, 1_000_000_001)
        self.assertEqual(normalized.to_dict()["timestamp_ns"], "1000000001")
        self.assertEqual(normalized.extensions["scene"]["name"], "demo")

    def test_metadata_rejects_version_shape_and_non_finite_values(self) -> None:
        invalid = [
            metadata(worldbus_version=2),
            metadata(frame_id=True),
            metadata(payload_bytes=7),
            metadata(pixel_format=[]),
            metadata(intrinsics=[float("nan"), 2.0, 1.0, 0.5]),
            metadata(camera_to_world=IDENTITY[:-1]),
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                validate_metadata(value)

    def test_metadata_enforces_allocation_limits_before_frame_creation(self) -> None:
        limits = WorldBusLimits(max_width=4, max_height=4, max_pixels=8)
        with self.assertRaises(SizeLimitError):
            validate_metadata(
                metadata(width=4, height=4, payload_bytes=64), limits
            )

    def test_atlas_requires_even_width_and_session_id_is_bounded(self) -> None:
        with self.assertRaisesRegex(ValidationError, "width must be even"):
            validate_metadata(metadata(width=3, height=1, payload_bytes=12))
        with self.assertRaisesRegex(ValidationError, "producer_session_id"):
            validate_metadata(metadata(producer_session_id=""))


class WorldBusFramingTests(unittest.TestCase):
    def test_frame_round_trip_and_fragmented_stream(self) -> None:
        expected = frame(7)
        wire = encode_frame(expected)
        self.assertEqual(decode_frame(wire), expected)
        decoder = FrameStreamDecoder()
        results = []
        for byte in wire:
            results.extend(decoder.feed(bytes([byte])))
        decoder.finish()
        self.assertEqual(results, [expected])

    def test_decoder_accepts_multiple_frames_and_rejects_trailing_bytes(self) -> None:
        first, second = frame(1), frame(2)
        decoder = FrameStreamDecoder()
        self.assertEqual(decoder.feed(encode_frame(first) + encode_frame(second)), [first, second])
        decoder.finish()
        with self.assertRaises(FramingError):
            decode_frame(encode_frame(first) + b"x")

    def test_decoder_rejects_oversize_prefix_without_waiting_for_payload(self) -> None:
        limits = WorldBusLimits(max_payload_bytes=16)
        malicious = FRAME_PREFIX.pack(FRAME_MAGIC, 2, 17)
        with self.assertRaises(SizeLimitError):
            FrameStreamDecoder(limits).feed(malicious)


class WorldBusFreshnessTests(unittest.TestCase):
    def test_newest_queue_supersedes_pending_and_rejects_old_frames(self) -> None:
        queue = NewestFrameQueue()
        self.assertTrue(queue.put(frame(1)))
        self.assertTrue(queue.put(frame(3)))
        self.assertFalse(queue.put(frame(2)))
        self.assertEqual(queue.get_nowait().metadata.frame_id, 3)
        self.assertFalse(queue.put(frame(3)))
        self.assertEqual(
            queue.stats,
            {
                "accepted": 2,
                "superseded": 1,
                "rejected_stale": 2,
                "session_resets": 0,
                "rejected_retired_session": 0,
                "rejected_missing_session": 0,
                "highest_frame_id": 3,
                "pending": 0,
            },
        )

    def test_new_producer_session_can_restart_ids_without_old_session_rollback(self) -> None:
        queue = NewestFrameQueue()
        self.assertTrue(queue.put(frame(100, producer_session_id="session-a")))
        self.assertEqual(queue.get_nowait().metadata.frame_id, 100)
        self.assertTrue(queue.put(frame(1, producer_session_id="session-b")))
        self.assertEqual(queue.get_nowait().metadata.frame_id, 1)
        self.assertFalse(queue.put(frame(101, producer_session_id="session-a")))
        self.assertFalse(queue.put(frame(2)))
        self.assertEqual(queue.stats["session_resets"], 1)
        self.assertEqual(queue.stats["rejected_retired_session"], 1)
        self.assertEqual(queue.stats["rejected_missing_session"], 1)

    def test_heartbeat_uses_local_receive_time_for_stale_detection(self) -> None:
        now = [10.0]
        monitor = HeartbeatMonitor(0.5, clock=lambda: now[0])
        self.assertEqual(monitor.status("ai")["state"], "missing")
        monitor.record("ai")
        now[0] = 10.5
        self.assertEqual(monitor.status("ai")["state"], "alive")
        now[0] = 10.5001
        self.assertEqual(monitor.status("ai")["state"], "stale")
        self.assertTrue(monitor.is_stale("ai"))

    def test_heartbeat_peer_count_is_bounded(self) -> None:
        monitor = HeartbeatMonitor(limits=WorldBusLimits(max_heartbeat_peers=1))
        monitor.record("ai")
        monitor.record("show")
        self.assertEqual(monitor.status("ai")["state"], "missing")
        self.assertEqual(monitor.status("show")["state"], "alive")
        self.assertEqual(monitor.stats["evicted"], 1)

    def test_heartbeat_peers_expire_after_stale_state_is_observable(self) -> None:
        now = [10.0]
        monitor = HeartbeatMonitor(
            0.5, expire_after_s=1.5, clock=lambda: now[0]
        )
        monitor.record("ai")
        now[0] = 10.6
        self.assertEqual(monitor.status("ai")["state"], "stale")
        now[0] = 11.6
        self.assertEqual(monitor.status("ai")["state"], "missing")
        self.assertEqual(monitor.snapshot(), {})
        self.assertEqual(monitor.stats["expired"], 1)


class WorldBusDatagramAndReplayTests(unittest.TestCase):
    def test_udp_messages_round_trip_and_reject_wrong_namespaces(self) -> None:
        heartbeat = make_heartbeat("ai-worker", timestamp_ns=123)
        self.assertEqual(decode_udp_message(encode_udp_message(heartbeat)), heartbeat)
        control = make_control(
            "/flexgpu/v1/control/freeze_ai", True, timestamp_ns=124, request_id="r1"
        )
        self.assertEqual(decode_udp_message(encode_udp_message(control)), control)
        meta = make_metadata_message(frame(9).metadata, timestamp_ns=125)
        self.assertEqual(
            decode_udp_message(encode_udp_message(meta))["metadata"]["frame_id"], 9
        )
        with self.assertRaises(ValidationError):
            make_control("/not-worldbus/freeze", True)
        with self.assertRaises(ValidationError):
            encode_udp_message(
                {
                    "worldbus_version": 1,
                    "kind": [],
                    "address": "/flexgpu/v1/control/value",
                    "timestamp_ns": "1",
                    "value": True,
                }
            )
        invalid_json = json.dumps(
            {
                "worldbus_version": 1,
                "kind": "control",
                "address": "/flexgpu/v1/control/value",
                "timestamp_ns": "1",
                "value": float("nan"),
            }
        ).encode("utf-8")
        with self.assertRaises(ValidationError):
            decode_udp_message(invalid_json)

    def test_replay_is_atomic_bounded_and_streamed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "demo.wbr")
            result = write_replay(path, generate_replay_frames(3, 4, 2))
            self.assertEqual(result["frames"], 3)
            loaded = list(iter_replay(path))
            self.assertEqual([item.metadata.frame_id for item in loaded], [1, 2, 3])
            summary = replay_summary(path)
            self.assertEqual(summary["duration_ns"], 200_000_000)
            self.assertTrue(summary["producer_session_id"].startswith("worldbus-replay-"))
            with self.assertRaises(FileExistsError):
                write_replay(path, generate_replay_frames(1, 4, 2))

    def test_generated_replay_uses_source_intrinsics_and_packed_depth_plane(self) -> None:
        generated = next(
            generate_replay_frames(
                1, 4, 2, timestamp_start_ns=1, producer_session_id="replay-test"
            )
        )
        self.assertEqual(generated.metadata.intrinsics, (2.0, 2.0, 1.0, 1.0))
        self.assertEqual(generated.metadata.extensions["producer_session_id"], "replay-test")
        pixels = [generated.payload[index : index + 4] for index in range(0, 32, 4)]
        self.assertTrue(all(pixel[3] == 255 for pixel in (pixels[0], pixels[1], pixels[4], pixels[5])))
        # Right plane is uint16 depth in R/G, valid mask B, confidence A.
        self.assertEqual(pixels[2], bytes((0, 0, 255, 224)))
        self.assertEqual(pixels[3], bytes((128, 0, 255, 224)))
        self.assertEqual(pixels[7], bytes((255, 255, 255, 224)))

    def test_tcp_and_udp_local_loopback(self) -> None:
        sent = frame(11, producer_session_id="current-session")
        retired_metadata = frame(11, producer_session_id="retired-session").metadata
        with WorldBusReceiver(stale_after_s=1.0) as receiver:
            with UDPJsonEndpoint() as endpoint:
                endpoint.send(
                    make_heartbeat("ai"), receiver.udp_address[0], receiver.udp_address[1]
                )
                endpoint.send(
                    make_metadata_message(sent.metadata),
                    receiver.udp_address[0],
                    receiver.udp_address[1],
                )
                endpoint.send(
                    make_metadata_message(retired_metadata),
                    receiver.udp_address[0],
                    receiver.udp_address[1],
                )
                with TCPFrameSender(*receiver.tcp_address) as sender:
                    sender.send(sent)
                received = receiver.frames.get(2.0)
                self.assertEqual(received, sent)
                deadline = time.monotonic() + 2.0
                while receiver.heartbeats.status("ai")["state"] == "missing":
                    if time.monotonic() >= deadline:
                        self.fail("heartbeat did not arrive")
                    time.sleep(0.01)
                while (
                    receiver.metadata_for(11, "current-session") is None
                    or receiver.metadata_for(11, "retired-session") is None
                ):
                    if time.monotonic() >= deadline:
                        self.fail("session-qualified metadata datagrams did not arrive")
                    time.sleep(0.01)
                self.assertEqual(
                    receiver.metadata_for(11, "current-session"), sent.metadata
                )
                self.assertEqual(receiver.errors, [])

    def test_receiver_is_explicitly_one_shot_after_close(self) -> None:
        receiver = WorldBusReceiver().start()
        receiver.close()
        with self.assertRaisesRegex(WorldBusError, "cannot be restarted"):
            receiver.start()
        receiver.close()  # idempotent

    def test_tcp_only_receiver_does_not_allocate_udp_endpoint(self) -> None:
        receiver = WorldBusReceiver(udp_port=None).start()
        try:
            self.assertIsNone(receiver._udp_endpoint)
            self.assertIsNone(receiver._udp_thread)
            with self.assertRaisesRegex(WorldBusError, "not enabled"):
                _ = receiver.udp_address
            sent = frame(12, producer_session_id="tcp-only")
            with TCPFrameSender(*receiver.tcp_address) as sender:
                sender.send(sent)
            self.assertEqual(receiver.frames.get(2.0), sent)
        finally:
            receiver.close()

    def test_incomplete_trickle_client_cannot_hold_the_only_tcp_slot(self) -> None:
        limits = WorldBusLimits(socket_timeout_s=0.3)
        receiver = WorldBusReceiver(limits=limits).start()
        attacker = socket.create_connection(receiver.tcp_address, timeout=1.0)
        stopped = threading.Event()

        def trickle() -> None:
            try:
                while not stopped.wait(0.04):
                    attacker.sendall(b"W")
            except OSError:
                pass

        thread = threading.Thread(target=trickle, daemon=True)
        try:
            attacker.sendall(b"W")
            thread.start()
            time.sleep(0.05)
            sent = frame(17, producer_session_id="real-producer")
            with TCPFrameSender(*receiver.tcp_address, timeout=1.0) as sender:
                sender.send(sent)
            received = receiver.frames.get(1.5)
            self.assertEqual(received, sent)
            self.assertTrue(
                any("receive deadline" in error for error in receiver.errors),
                receiver.errors,
            )
        finally:
            stopped.set()
            try:
                attacker.close()
            except OSError:
                pass
            if thread.ident is not None:
                thread.join(timeout=1.0)
            receiver.close()


if __name__ == "__main__":
    unittest.main()
