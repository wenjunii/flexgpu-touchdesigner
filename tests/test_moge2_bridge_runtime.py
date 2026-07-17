from __future__ import annotations

import json
import importlib.util
from dataclasses import replace
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import touchdesigner.moge2_bridge_runtime as bridge_module  # noqa: E402
from flexgpu.moge2_transport import (  # noqa: E402
    make_moge2_worldbus_metadata,
    pack_moge2_atlas,
)
from flexgpu.worldbus import (  # noqa: E402
    NewestFrameQueue,
    TCPFrameSender,
    WorldBusReceiver,
    make_frame,
)
from touchdesigner.moge2_bridge_runtime import (  # noqa: E402
    BridgeConfig,
    BridgeLimits,
    BridgeRuntime,
    BridgeRuntimeError,
    REQUEST_CONTRACT,
    atlas_result_to_touchdesigner_numpy,
    derive_result_mappings,
    rgba_numpy_to_top_left_bytes,
    validate_moge2_result_frame,
)


IDENTITY = [
    1.0, 0.0, 0.0, 0.0,
    0.0, 1.0, 0.0, 0.0,
    0.0, 0.0, 1.0, 0.0,
    0.0, 0.0, 0.0, 1.0,
]


def wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition was not met before timeout")
        time.sleep(0.005)


def load_bootstrap_runtime_helpers() -> dict[str, object]:
    path = ROOT / "touchdesigner" / "bootstrap_project.py"
    spec = importlib.util.spec_from_file_location("moge2_bridge_bootstrap", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not import bootstrap_project.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    namespace: dict[str, object] = {}
    exec(module.RUNTIME_HELPERS, namespace)
    return namespace


def result_frame(
    frame_id: int = 1,
    *,
    timestamp_ns: int | None = None,
    source_timestamp_ns: int | None = None,
    source_session_id: str = "td-session-a",
    source_frame_id: int | None = None,
    generation_id: str = "prompt-epoch-1",
    profile: str = "3080ti_16gb",
    original_width: int = 2,
    original_height: int = 2,
):
    source = bytes(
        (
            10, 20, 30, 40, 50, 60, 70, 80,
            90, 100, 110, 120, 130, 140, 150, 160,
        )
    )
    atlas = pack_moge2_atlas(
        source,
        [1.0, 2.0, 3.0, 0.0],
        [1.0, 1.0, 1.0, 1.0],
        source_width=2,
        height=2,
    )
    completed_timestamp = timestamp_ns or time.time_ns()
    source_timestamp = source_timestamp_ns or (completed_timestamp - 100)
    metadata = make_moge2_worldbus_metadata(
        atlas,
        frame_id=frame_id,
        timestamp_ns=completed_timestamp,
        intrinsics=[2.0, 2.0, 1.0, 1.0],
        camera_to_world=IDENTITY,
        generation_id=generation_id,
        producer_session_id="worker-session-a",
        source_frame_id=source_frame_id if source_frame_id is not None else frame_id + 40,
        source_timestamp_ns=source_timestamp,
        source_producer_session_id=source_session_id,
        model_id="Ruicheng/moge-2-vits-normal",
        model_source_revision="0" * 40,
        model_revision="1" * 40,
        extra_extensions={
            "moge2_profile": profile,
            "moge2_source_width": original_width,
            "moge2_source_height": original_height,
            "moge2_source_pixel_format": "rgba8",
        },
    )
    return make_frame(metadata, atlas.payload)


class FakeReceiver:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.frames = NewestFrameQueue(kwargs.get("limits"))
        self.start_calls = 0
        self.close_calls = 0

    def start(self):
        self.start_calls += 1
        return self

    def close(self) -> None:
        self.close_calls += 1
        self.frames.close()


class ReceiverFactory:
    def __init__(self) -> None:
        self.instances: list[FakeReceiver] = []

    def __call__(self, **kwargs):
        receiver = FakeReceiver(**kwargs)
        self.instances.append(receiver)
        return receiver


class BlockingSenderFactory:
    def __init__(self) -> None:
        self.frames = []
        self.thread_ids: list[int] = []
        self.first_started = threading.Event()
        self.release_first = threading.Event()
        self.two_sent = threading.Event()
        self.close_calls = 0

    def __call__(self, *_args, **_kwargs):
        factory = self

        class Sender:
            def send(self, frame) -> int:
                factory.thread_ids.append(threading.get_ident())
                if not factory.frames:
                    factory.first_started.set()
                    if not factory.release_first.wait(2.0):
                        raise RuntimeError("test sender gate timed out")
                factory.frames.append(frame)
                if len(factory.frames) >= 2:
                    factory.two_sent.set()
                return len(frame.payload)

            def close(self) -> None:
                factory.close_calls += 1

        return Sender()


class StepClock:
    def __init__(self) -> None:
        self.value = 1_000_000_000

    def __call__(self) -> int:
        return self.value

    def advance(self, milliseconds: int = 20) -> None:
        self.value += milliseconds * 1_000_000


class FakeInputTop:
    def __init__(self, array) -> None:
        self.array = array
        self.call_threads: list[int] = []
        self.delayed_values: list[object] = []

    def numpyArray(self, *, delayed=False):
        self.call_threads.append(threading.get_ident())
        self.delayed_values.append(delayed)
        return self.array


class FakeScriptTop:
    def __init__(self, on_cook=None) -> None:
        self.arrays = []
        self.call_threads: list[int] = []
        self.cook_forces: list[bool] = []
        self.on_cook = on_cook

    def copyNumpyArray(self, value) -> None:
        self.call_threads.append(threading.get_ident())
        self.arrays.append(value.copy())

    def cook(self, force: bool = False):
        self.cook_forces.append(force)
        if self.on_cook is not None:
            return self.on_cook(self)
        return None


class FakeParameter:
    def __init__(self, name: str, value) -> None:
        self.name = name
        self.val = value

    def eval(self):
        return self.val


class FakeParameters:
    def __init__(self, values: dict[str, object]) -> None:
        for name, value in values.items():
            setattr(self, name, FakeParameter(name, value))


class FakeTextDat:
    def __init__(self) -> None:
        self.text = ""


class FakeTableDat:
    def __init__(self) -> None:
        self.rows = []

    def clear(self) -> None:
        self.rows.clear()

    def appendRow(self, row) -> None:
        self.rows.append(tuple(row))


class FakeNode:
    def __init__(self, parameters: dict[str, object]) -> None:
        self.par = FakeParameters(parameters)


class FakeBridgeComp:
    def __init__(self) -> None:
        self.par = FakeParameters(
            {
                "Enabled": True,
                "Profile": "3080ti_16gb",
                "Workerhost": "127.0.0.1",
                "Workerinputtcp": 9211,
                "Workerinputudp": 9210,
                "Resultbindhost": "127.0.0.1",
                "Resulttcp": 9221,
                "Resultudp": 9220,
                "Capturefps": 5,
                "Flipvertical": True,
                "Resultvalid": False,
                "Generationid": "streamdiffusion",
                "Sourceframeid": 0,
            }
        )
        self.nodes = {
            "IN_RGB": object(),
            "DEPTH_SCALE_BIAS": FakeNode(
                {"colorr": 0.0, "colorg": 0.0, "colorb": 0.0, "colora": 0.0}
            ),
            "FRAME_STATE": FakeTextDat(),
            "CAMERA_METADATA": FakeTextDat(),
            "STATUS": FakeTableDat(),
            "RESULT_ATLAS": FakeScriptTop(bridge_module.on_script_top_cook),
        }

    def op(self, name: str):
        return self.nodes.get(name)


class ConversionAndContractTests(unittest.TestCase):
    def test_embedded_dat_resolves_src_from_touchdesigner_sys_path(self) -> None:
        bridge_path = ROOT / "touchdesigner" / "moge2_bridge_runtime.py"
        command = f"""
import os
import pathlib
import sys
import types

repo = pathlib.Path({str(ROOT)!r})
src = repo / "src"
touchdesigner = repo / "touchdesigner"

def normalized(value):
    return os.path.normcase(os.path.abspath(os.fspath(value)))

forbidden = {{normalized(repo), normalized(src)}}
sys.path[:] = [
    entry for entry in sys.path
    if not isinstance(entry, str) or normalized(entry) not in forbidden
]
sys.path.insert(0, os.fspath(touchdesigner))
assert normalized(src) not in {{
    normalized(entry) for entry in sys.path if isinstance(entry, str)
}}

module = types.ModuleType("embedded_moge2_bridge_runtime")
module.__file__ = "/project1/flexgpu/WORKING_PIPELINE/MOGE2_BRIDGE/bridge_runtime"
sys.modules[module.__name__] = module
source = pathlib.Path({str(bridge_path)!r}).read_text(encoding="utf-8")
exec(compile(source, module.__file__, "exec"), module.__dict__)
assert "flexgpu.worldbus" in sys.modules
assert normalized(src) in {{
    normalized(entry) for entry in sys.path if isinstance(entry, str)
}}
"""
        with tempfile.TemporaryDirectory() as temporary_directory:
            completed = subprocess.run(
                [sys.executable, "-S", "-c", command],
                cwd=temporary_directory,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_module_import_does_not_import_numpy_or_touchdesigner(self) -> None:
        command = (
            "import sys; sys.path[:0]=[r'%s',r'%s']; "
            "import touchdesigner.moge2_bridge_runtime; "
            "assert 'numpy' not in sys.modules; assert 'td' not in sys.modules"
        ) % (ROOT, SRC)
        completed = subprocess.run(
            [sys.executable, "-S", "-c", command],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_float_and_uint8_capture_are_normalized_and_flipped(self) -> None:
        import numpy as np

        floating = np.asarray(
            [
                [[0.0, 0.5, np.nan, np.inf], [1.1, -0.1, 0.25, 1.0]],
                [[1.0, 0.0, 0.75, 0.5], [0.1, 0.2, 0.3, 0.4]],
            ],
            dtype=np.float32,
        )
        encoded, width, height = rgba_numpy_to_top_left_bytes(floating)
        expected = np.floor(
            np.clip(np.nan_to_num(floating[::-1], nan=0.0, posinf=1.0), 0.0, 1.0)
            * 255.0
            + 0.5
        ).astype(np.uint8)
        self.assertEqual((width, height), (2, 2))
        self.assertEqual(encoded, expected.tobytes())

        exact = np.arange(16, dtype=np.uint8).reshape(2, 2, 4)
        exact_bytes, _, _ = rgba_numpy_to_top_left_bytes(exact)
        self.assertEqual(exact_bytes, exact[::-1].tobytes())
        with self.assertRaisesRegex(BridgeRuntimeError, "uint8 or a floating"):
            rgba_numpy_to_top_left_bytes(exact.astype(np.uint16))

    def test_result_validation_mapping_and_upload_flip(self) -> None:
        import numpy as np

        result = validate_moge2_result_frame(result_frame())
        self.assertIsInstance(result.payload, bytes)
        frame_state, camera = derive_result_mappings(result)
        self.assertEqual(frame_state["version"], "flexgpu-frame-state/v1")
        self.assertEqual(frame_state["width"], 2)
        self.assertEqual(frame_state["height"], 2)
        self.assertEqual(frame_state["valid_fraction"], 0.75)
        self.assertEqual(len(frame_state["calibration_digest"]), 64)
        expected_camera_fields = {
            "version", "session_id", "frame_id", "timestamp_ns", "width", "height",
            "generation_id", "intrinsics_pixels", "depth_scale_bias",
            "camera_to_world", "near_metres", "far_metres", "calibration_id",
            "calibration_digest",
        }
        self.assertEqual(set(camera), expected_camera_fields)
        self.assertEqual(camera["intrinsics_pixels"], [2.0, 2.0, 1.0, 1.0])
        for field in (
            "session_id", "frame_id", "timestamp_ns", "width", "height",
            "calibration_id", "calibration_digest",
        ):
            self.assertEqual(camera[field], frame_state[field])
        helpers = load_bootstrap_runtime_helpers()
        validated_state = helpers["_validate_frame_state"](frame_state, {})
        validated_camera = helpers["_validate_camera_metadata"](camera, validated_state)
        self.assertEqual(validated_camera["intrinsics_pixels"], (2.0, 2.0, 1.0, 1.0))
        converted = atlas_result_to_touchdesigner_numpy(result)
        expected = (
            np.frombuffer(result.payload, dtype=np.uint8)
            .reshape(result.height, result.width, 4)[::-1]
            .astype(np.float32)
            / 255.0
        )
        np.testing.assert_allclose(converted, expected, rtol=0.0, atol=1e-7)
        self.assertEqual(converted.dtype, np.float32)

    def test_result_contract_mismatch_fails_closed(self) -> None:
        valid = result_frame()
        metadata = valid.metadata.to_dict()
        metadata["moge2_atlas_contract"] = "wrong/v1"
        invalid = make_frame(metadata, valid.payload)
        with self.assertRaisesRegex(BridgeRuntimeError, "atlas contract"):
            validate_moge2_result_frame(invalid)

    def test_consumer_geometry_bounds_fail_closed(self) -> None:
        valid = result_frame()
        cases = []
        focal = valid.metadata.to_dict()
        focal["intrinsics"] = [1000.0, 2.0, 1.0, 1.0]
        cases.append(("focal lengths", make_frame(focal, valid.payload)))
        depth = valid.metadata.to_dict()
        depth["depth_scale_bias"] = [1.0, 0.0]
        cases.append(("depth calibration", make_frame(depth, valid.payload)))
        handedness = valid.metadata.to_dict()
        matrix = list(handedness["camera_to_world"])
        matrix[0] = -1.0
        handedness["camera_to_world"] = matrix
        cases.append(("right-handed", make_frame(handedness, valid.payload)))
        for message, frame in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(BridgeRuntimeError, message):
                    validate_moge2_result_frame(frame)

    def test_calibration_change_rolls_consumer_session_and_generation_is_sanitized(self) -> None:
        result = validate_moge2_result_frame(
            result_frame(generation_id="a private generated prompt with spaces")
        )
        frame_state, camera = derive_result_mappings(result)
        self.assertTrue(camera["generation_id"].startswith("generation-"))
        self.assertNotIn("private", camera["generation_id"])
        repeated_state, _ = derive_result_mappings(result)
        self.assertEqual(repeated_state["session_id"], frame_state["session_id"])
        changed = replace(result, intrinsics=(2.1, 2.0, 1.0, 1.0))
        changed_state, _ = derive_result_mappings(changed)
        self.assertNotEqual(changed_state["calibration_digest"], frame_state["calibration_digest"])
        self.assertNotEqual(changed_state["session_id"], frame_state["session_id"])


class BridgeThreadingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.receiver_factory = ReceiverFactory()
        self.sender_factory = BlockingSenderFactory()
        self.clock = StepClock()
        self.runtime = BridgeRuntime(
            BridgeConfig(capture_fps=60.0),
            receiver_factory=self.receiver_factory,
            sender_factory=self.sender_factory,
            monotonic_ns=self.clock,
            timestamp_ns=lambda: 2_000_000_000,
        )

    def tearDown(self) -> None:
        self.sender_factory.release_first.set()
        self.runtime.stop()

    def issue_request(self, generation_id: str = "generation-a"):
        import numpy as np

        receipt = self.runtime.capture_numpy(
            np.zeros((2, 2, 4), dtype=np.uint8), generation_id=generation_id
        )
        self.assertIsNotNone(receipt)
        self.clock.advance()
        return receipt

    def test_lifecycle_is_idempotent_and_restart_gets_fresh_receiver(self) -> None:
        self.runtime.start()
        self.runtime.start()
        self.assertEqual(len(self.receiver_factory.instances), 1)
        self.assertEqual(self.receiver_factory.instances[0].start_calls, 1)
        self.runtime.stop()
        self.runtime.stop()
        self.assertEqual(self.receiver_factory.instances[0].close_calls, 1)
        self.runtime.start()
        self.assertEqual(len(self.receiver_factory.instances), 2)

    def test_result_connection_survives_idle_before_first_worker_result(self) -> None:
        import numpy as np

        receivers = []

        def receiver_factory(**kwargs):
            kwargs["tcp_port"] = 0
            kwargs["udp_port"] = 0
            receiver = WorldBusReceiver(**kwargs)
            receivers.append(receiver)
            return receiver

        class ImmediateSender:
            def send(self, frame) -> int:
                return len(frame.payload)

            def close(self) -> None:
                pass

        runtime = BridgeRuntime(
            BridgeConfig(capture_fps=60.0),
            limits=BridgeLimits(socket_timeout_s=0.5),
            receiver_factory=receiver_factory,
            sender_factory=lambda *_args, **_kwargs: ImmediateSender(),
        )
        try:
            runtime.start()
            issued = runtime.capture_numpy(
                np.zeros((2, 2, 4), dtype=np.uint8),
                generation_id="generation-a",
            )
            self.assertIsNotNone(issued)
            receiver = receivers[0]
            self.assertEqual(receiver.limits.socket_timeout_s, 30.0)
            with TCPFrameSender(*receiver.tcp_address, timeout=1.0) as sender:
                wait_until(lambda: receiver._active_connection is not None)
                time.sleep(0.65)
                sender.send(
                    result_frame(
                        source_session_id=runtime.producer_session_id,
                        source_frame_id=issued.frame_id,
                        source_timestamp_ns=issued.timestamp_ns,
                        generation_id="generation-a",
                    )
                )
            wait_until(lambda: runtime.status()["accepted_results"] == 1)
            self.assertEqual(receiver.errors, [])
            self.assertEqual(runtime.status()["rejected_results"], 0)
        finally:
            runtime.stop()

    def test_outgoing_queue_is_newest_only_and_workers_receive_bytes_only(self) -> None:
        import numpy as np

        main_thread = threading.get_ident()
        source_top = FakeInputTop(np.zeros((2, 2, 4), dtype=np.uint8))
        self.runtime.start()
        first = self.runtime.capture_top(
            source_top,
            generation_id="a vivid private prompt that must not cross",
            flip_vertical=True,
        )
        self.assertIsNotNone(first)
        self.assertTrue(self.sender_factory.first_started.wait(1.0))
        self.clock.advance()
        second = self.runtime.capture_top(
            source_top, generation_id="a vivid private prompt that must not cross"
        )
        self.clock.advance()
        third = self.runtime.capture_top(
            source_top, generation_id="a vivid private prompt that must not cross"
        )
        self.assertEqual([first.frame_id, second.frame_id, third.frame_id], [1, 2, 3])
        self.sender_factory.release_first.set()
        self.assertTrue(self.sender_factory.two_sent.wait(1.0))
        self.assertEqual(
            [frame.metadata.frame_id for frame in self.sender_factory.frames], [1, 3]
        )
        sent = self.sender_factory.frames[0]
        self.assertIsInstance(sent.payload, bytes)
        self.assertEqual(sent.metadata.pixel_format, "rgba8")
        self.assertEqual(
            sent.metadata.extensions["moge2_request_contract"], REQUEST_CONTRACT
        )
        self.assertEqual(sent.metadata.extensions["moge2_source_orientation"], "top_left")
        self.assertTrue(sent.metadata.extensions["producer_session_id"].startswith("td-moge2-"))
        self.assertTrue(sent.metadata.generation_id.startswith("generation-"))
        self.assertNotIn("private prompt", sent.metadata.generation_id)
        self.assertEqual(source_top.call_threads, [main_thread, main_thread, main_thread])
        self.assertEqual(source_top.delayed_values, [True, True, True])
        self.assertTrue(
            all(thread_id != main_thread for thread_id in self.sender_factory.thread_ids)
        )
        self.assertGreaterEqual(self.runtime.status()["outgoing_superseded"], 1)

    def test_receiver_exposes_newest_immutable_result_and_upload_stays_on_owner(self) -> None:
        main_thread = threading.get_ident()
        self.runtime.start()
        receiver = self.receiver_factory.instances[0]
        session_id = self.runtime.producer_session_id
        first = self.issue_request()
        second = self.issue_request()
        receiver.frames.put(
            result_frame(
                1,
                source_session_id=session_id,
                source_frame_id=first.frame_id,
                source_timestamp_ns=first.timestamp_ns,
                generation_id="generation-a",
            )
        )
        receiver.frames.put(
            result_frame(
                2,
                source_session_id=session_id,
                source_frame_id=second.frame_id,
                source_timestamp_ns=second.timestamp_ns,
                generation_id="generation-a",
            )
        )
        wait_until(
            lambda: self.runtime.latest_result() is not None
            and self.runtime.latest_result().frame_id == 2
        )
        latest = self.runtime.latest_result()
        self.assertIsInstance(latest.payload, bytes)
        output = FakeScriptTop()
        self.assertTrue(self.runtime.upload_latest_to_top(output))
        self.assertEqual(output.call_threads, [main_thread])
        self.assertEqual(output.arrays[0].dtype.name, "float32")
        self.assertEqual(self.runtime.status()["accepted_results"], 1)

    def test_invalid_result_is_rejected_without_reaching_touchdesigner(self) -> None:
        self.runtime.start()
        receiver = self.receiver_factory.instances[0]
        issued = self.issue_request()
        valid = result_frame(
            source_session_id=self.runtime.producer_session_id,
            source_frame_id=issued.frame_id,
            source_timestamp_ns=issued.timestamp_ns,
            generation_id="generation-a",
        )
        metadata = valid.metadata.to_dict()
        metadata["moge2_confidence_semantics"] = "untrusted"
        receiver.frames.put(make_frame(metadata, valid.payload))
        wait_until(lambda: self.runtime.status()["rejected_results"] == 1)
        self.assertIsNone(self.runtime.latest_result())
        self.assertEqual(self.runtime.status()["last_error"], "result_rejected")

    def test_foreign_session_is_rejected_and_worker_clock_skew_is_normalized(self) -> None:
        self.runtime.start()
        receiver = self.receiver_factory.instances[0]
        issued = self.issue_request()
        receiver.frames.put(
            result_frame(
                1,
                source_session_id="foreign-session",
                source_frame_id=issued.frame_id,
                source_timestamp_ns=issued.timestamp_ns,
                generation_id="generation-a",
            )
        )
        wait_until(lambda: self.runtime.status()["rejected_results"] == 1)
        self.assertEqual(self.runtime.status()["last_error"], "foreign_source_session")
        receiver.frames.put(
            result_frame(
                2,
                timestamp_ns=100,
                source_timestamp_ns=issued.timestamp_ns,
                source_session_id=self.runtime.producer_session_id,
                source_frame_id=issued.frame_id,
                generation_id="generation-a",
            )
        )
        wait_until(lambda: self.runtime.latest_result() is not None)
        self.assertEqual(self.runtime.latest_result().timestamp_ns, 2_000_000_000)

    def test_result_freshness_expires_after_worker_loss(self) -> None:
        self.runtime.start()
        receiver = self.receiver_factory.instances[0]
        issued = self.issue_request()
        receiver.frames.put(
            result_frame(
                1,
                source_session_id=self.runtime.producer_session_id,
                source_frame_id=issued.frame_id,
                source_timestamp_ns=issued.timestamp_ns,
                generation_id="generation-a",
            )
        )
        wait_until(lambda: self.runtime.latest_result() is not None)
        self.assertTrue(self.runtime.status()["result_fresh"])
        self.clock.advance(1100)
        self.assertIsNone(self.runtime.latest_result())
        self.assertFalse(self.runtime.status()["result_fresh"])
        self.assertEqual(self.runtime.status()["state"], "stale")

    def test_issued_request_fields_and_profile_are_correlated_exactly(self) -> None:
        self.runtime.start()
        receiver = self.receiver_factory.instances[0]
        issued = self.issue_request()
        common = {
            "source_session_id": self.runtime.producer_session_id,
            "source_frame_id": issued.frame_id,
            "source_timestamp_ns": issued.timestamp_ns,
            "generation_id": "generation-a",
        }
        mismatches = [
            {"source_frame_id": issued.frame_id + 100},
            {"source_timestamp_ns": issued.timestamp_ns + 1},
            {"generation_id": "generation-b"},
            {"original_width": 3},
            {"profile": "4090"},
        ]
        for output_frame_id, mismatch in enumerate(mismatches, 1):
            values = dict(common)
            values.update(mismatch)
            receiver.frames.put(result_frame(output_frame_id, **values))
            wait_until(
                lambda expected=output_frame_id: self.runtime.status()["rejected_results"]
                == expected
            )
        receiver.frames.put(result_frame(10, **common))
        wait_until(lambda: self.runtime.latest_result() is not None)
        self.assertEqual(self.runtime.latest_result().source_frame_id, issued.frame_id)


class ModuleCallbackTests(unittest.TestCase):
    def tearDown(self) -> None:
        bridge_module.stop()

    def test_tick_and_script_callback_update_td_objects_on_calling_thread(self) -> None:
        accepted = validate_moge2_result_frame(result_frame())
        main_thread = threading.get_ident()

        class FakeRuntime:
            def __init__(self, _config) -> None:
                self.config = _config
                self.stopped = 0
                self.upload_threads = []
                self.current_result = accepted
                self.uploaded_results = []
                self.fail_upload = False
                self.errors = []

            def start(self):
                return self

            def stop(self):
                self.stopped += 1

            def capture_due(self):
                return False

            def latest_result(self):
                return self.current_result

            def status(self):
                return {
                    "state": "running",
                    "last_error": "none",
                    "accepted_results": 1,
                }

            def upload_result_to_top(self, script_top, result):
                self.upload_threads.append(threading.get_ident())
                if self.fail_upload:
                    raise BridgeRuntimeError("simulated upload failure")
                self.uploaded_results.append(result)
                script_top.copyNumpyArray(atlas_result_to_touchdesigner_numpy(result))
                return True

            def _record_error(self, code):
                self.errors.append(code)

        component = FakeBridgeComp()
        created = []

        def factory(config):
            runtime = FakeRuntime(config)
            created.append(runtime)
            return runtime

        with mock.patch.object(bridge_module, "BridgeRuntime", side_effect=factory):
            state = bridge_module.tick(component)
            self.assertEqual(state["state"], "running")
            self.assertTrue(component.par.Resultvalid.val)
            output = component.nodes["RESULT_ATLAS"]
            self.assertEqual(output.cook_forces, [True])
            self.assertEqual(output.call_threads, [main_thread])
            self.assertEqual(created[0].upload_threads, [main_thread])
            self.assertEqual(created[0].uploaded_results, [accepted])
            self.assertEqual(component.nodes["DEPTH_SCALE_BIAS"].par.colorr.val, 0.001)
            self.assertEqual(component.nodes["DEPTH_SCALE_BIAS"].par.colorg.val, 0.0)
            frame_state = json.loads(component.nodes["FRAME_STATE"].text)
            camera = json.loads(component.nodes["CAMERA_METADATA"].text)
            self.assertEqual(frame_state["frame_id"], accepted.frame_id)
            self.assertEqual(camera["frame_id"], accepted.frame_id)
            self.assertEqual(component.nodes["STATUS"].rows[0], ("metric", "value"))
            newer = validate_moge2_result_frame(result_frame(2))
            created[0].current_result = newer

            # A cook method returning normally is insufficient: the callback
            # must confirm that it copied the exact staged result key.
            silent_top = FakeScriptTop()
            component.nodes["RESULT_ATLAS"] = silent_top
            bridge_module.tick(component)
            self.assertEqual(silent_top.cook_forces, [True])
            self.assertFalse(component.par.Resultvalid.val)
            self.assertIn("atlas_upload_unconfirmed", created[0].errors)
            self.assertEqual(
                json.loads(component.nodes["FRAME_STATE"].text)["frame_id"], 1
            )

            component.nodes["RESULT_ATLAS"] = output
            bridge_module.tick(component)
            self.assertTrue(component.par.Resultvalid.val)
            self.assertEqual(output.cook_forces, [True, True])
            self.assertEqual(created[0].uploaded_results, [accepted, newer])
            self.assertEqual(
                json.loads(component.nodes["FRAME_STATE"].text)["frame_id"], 2
            )

            newest = validate_moge2_result_frame(result_frame(3))
            created[0].current_result = newest
            created[0].fail_upload = True
            bridge_module.tick(component)
            self.assertFalse(component.par.Resultvalid.val)
            self.assertIn("upload_failed", created[0].errors)
            self.assertEqual(
                json.loads(component.nodes["FRAME_STATE"].text)["frame_id"], 2
            )

            created[0].fail_upload = False
            component.nodes["RESULT_ATLAS"] = object()
            bridge_module.tick(component)
            self.assertFalse(component.par.Resultvalid.val)
            self.assertIn("atlas_cook_unavailable", created[0].errors)

            created[0].current_result = None
            bridge_module.tick(component)
            self.assertFalse(component.par.Resultvalid.val)
            self.assertFalse(bridge_module.on_script_top_cook(output))
            bridge_module.stop(component)
            self.assertFalse(component.par.Resultvalid.val)
            self.assertEqual(created[0].stopped, 1)


if __name__ == "__main__":
    unittest.main()
