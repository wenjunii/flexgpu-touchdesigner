from __future__ import annotations

import importlib.util
import json
import os
import socket
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

import touchdesigner.depth_anything_sensor_runtime as sensor_module  # noqa: E402
from flexgpu.depth_anything_transport import (  # noqa: E402
    make_sensor_worldbus_metadata,
    pack_sensor_frame,
)
from flexgpu.worldbus import (  # noqa: E402
    NewestFrameQueue,
    TCPFrameSender,
    WorldBusReceiver,
    make_frame,
)
from touchdesigner.depth_anything_sensor_runtime import (  # noqa: E402
    SensorBridgeConfig,
    SensorBridgeRuntime,
    SensorBridgeError,
    derive_frame_state,
    sensor_result_to_touchdesigner_numpy,
    validate_sensor_result_frame,
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


def load_runtime_helpers() -> dict[str, object]:
    path = ROOT / "touchdesigner" / "bootstrap_project.py"
    spec = importlib.util.spec_from_file_location("sensor_bridge_bootstrap", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not import bootstrap_project.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    namespace: dict[str, object] = {}
    exec(module.RUNTIME_HELPERS, namespace)
    return namespace


def sensor_frame(
    frame_id: int = 1,
    *,
    capture_timestamp_ns: int | None = None,
    producer_session_id: str = "webcam-session-a",
    calibration_id: str = "webcam-pseudo-v1",
    calibration_digest: str = "0" * 64,
):
    packed = pack_sensor_frame(
        [1.0, 2.0, 0.0, 3.0],
        [1.0, 1.0, 0.0, 1.0],
        [1.0, 0.5, 0.0, 0.25],
        width=2,
        height=2,
    )
    timestamp = capture_timestamp_ns or time.time_ns()
    metadata = make_sensor_worldbus_metadata(
        packed,
        frame_id=frame_id,
        capture_timestamp_ns=timestamp,
        intrinsics=[2.0, 2.0, 1.0, 1.0],
        camera_to_world=IDENTITY,
        generation_id="audience-sensor",
        producer_session_id=producer_session_id,
        sensor_calibration_id=calibration_id,
        sensor_calibration_digest=calibration_digest,
        model_id="depth-anything-v2-small",
        model_revision="1" * 40,
        calibration_mode="session_frozen",
        raw_order="near_is_larger",
        raw_percentiles=[2.0, 98.0],
        raw_bounds=[0.1, 0.9],
        pseudo_metre_slab=[0.5, 4.0],
        foreground_far_m=3.0,
        capture_source="webcam:0",
        inference_ms=30.0,
    )
    return make_frame(metadata, packed.payload)


class FakeReceiver:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.frames = NewestFrameQueue(kwargs.get("limits"))
        self.start_calls = 0
        self.close_calls = 0
        self._errors = []

    @property
    def errors(self):
        return list(self._errors)

    def start(self):
        self.start_calls += 1
        return self

    def close(self):
        self.close_calls += 1
        self.frames.close()


class ReceiverFactory:
    def __init__(self) -> None:
        self.instances = []

    def __call__(self, **kwargs):
        receiver = FakeReceiver(**kwargs)
        self.instances.append(receiver)
        return receiver


class StepClock:
    def __init__(self) -> None:
        self.wall = 2_000_000_000
        self.monotonic = 1_000_000_000

    def wall_ns(self):
        return self.wall

    def monotonic_ns(self):
        return self.monotonic

    def advance(self, milliseconds: int):
        delta = milliseconds * 1_000_000
        self.wall += delta
        self.monotonic += delta


class FakeParameter:
    def __init__(self, name, value) -> None:
        self.name = name
        self.val = value

    def eval(self):
        return self.val


class FakeParameters:
    def __init__(self, values) -> None:
        for name, value in values.items():
            setattr(self, name, FakeParameter(name, value))


class FakeTextDat:
    def __init__(self) -> None:
        self.text = ""


class FakeTableDat:
    def __init__(self) -> None:
        self.rows = []

    def clear(self):
        self.rows.clear()

    def appendRow(self, row):
        self.rows.append(tuple(row))


class FakeConstant:
    def __init__(self) -> None:
        self.par = FakeParameters(
            {"colorr": 0.0, "colorg": 0.0, "colorb": 0.0, "colora": 0.0}
        )


class FakeScriptTop:
    def __init__(self, on_cook=None) -> None:
        self.on_cook = on_cook
        self.arrays = []
        self.call_threads = []
        self.cook_forces = []

    def copyNumpyArray(self, value):
        self.call_threads.append(threading.get_ident())
        self.arrays.append(value.copy())

    def cook(self, force=False):
        self.cook_forces.append(force)
        if self.on_cook is not None:
            return self.on_cook(self)
        return None


class FakeBridge:
    def __init__(self) -> None:
        self.par = FakeParameters(
            {
                "Enabled": True,
                "Resultbindhost": "127.0.0.1",
                "Resulttcp": 9241,
                "Resultudp": 9240,
                "Allowtrustednetwork": False,
                "Stalems": 800.0,
                "Flipvertical": True,
                "Resultvalid": False,
            }
        )
        self.nodes = {
            "RESULT_PACKED": FakeScriptTop(sensor_module.on_script_top_cook),
            "DEPTH_CALIBRATION": FakeConstant(),
            "INTRINSICS_NORMALIZED": FakeConstant(),
            "FRAME_STATE": FakeTextDat(),
            "STATUS": FakeTableDat(),
        }

    def op(self, name):
        return self.nodes.get(name)


class ImportAndContractTests(unittest.TestCase):
    def test_embedded_dat_resolves_src_from_launcher_config_on_cold_reopen(self) -> None:
        bridge_path = ROOT / "touchdesigner" / "depth_anything_sensor_runtime.py"
        config_path = (
            ROOT / "config" / "presets" / "single-3080ti-16gb.json"
        )
        command = f"""
import os, pathlib, sys, types
repo = pathlib.Path({str(ROOT)!r})
src = repo / 'src'
td = repo / 'touchdesigner'
def normalized(value):
    return os.path.normcase(os.path.abspath(os.fspath(value)))
for key in ('FLEXGPU_ROOT', 'FLEXGPU_SRC'):
    os.environ.pop(key, None)
os.environ['FLEXGPU_CONFIG'] = os.fspath(pathlib.Path({str(config_path)!r}))
for name in tuple(sys.modules):
    if name == 'flexgpu' or name.startswith('flexgpu.'):
        sys.modules.pop(name, None)
forbidden = {{normalized(repo), normalized(src), normalized(td)}}
sys.path[:] = [entry for entry in sys.path
               if not isinstance(entry, str) or normalized(entry) not in forbidden]
module = types.ModuleType('embedded_depth_anything_sensor_runtime_cold')
module.__file__ = '/project1/flexgpu/WORKING_PIPELINE/SENSOR_INTERACTION/DEPTH_SENSOR_ADAPTER/DEPTH_ANYTHING_BRIDGE/sensor_runtime'
sys.modules[module.__name__] = module
source = pathlib.Path({str(bridge_path)!r}).read_text(encoding='utf-8')
exec(compile(source, module.__file__, 'exec'), module.__dict__)
assert 'flexgpu.worldbus' in sys.modules
assert normalized(src) in {{normalized(entry) for entry in sys.path if isinstance(entry, str)}}
"""
        with tempfile.TemporaryDirectory() as directory:
            completed = subprocess.run(
                [sys.executable, "-S", "-c", command],
                cwd=directory,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_embedded_dat_resolves_src_from_touchdesigner_sys_path(self) -> None:
        bridge_path = ROOT / "touchdesigner" / "depth_anything_sensor_runtime.py"
        command = f"""
import os, pathlib, sys, types
repo = pathlib.Path({str(ROOT)!r})
src = repo / 'src'
td = repo / 'touchdesigner'
def normalized(value):
    return os.path.normcase(os.path.abspath(os.fspath(value)))
forbidden = {{normalized(repo), normalized(src)}}
sys.path[:] = [entry for entry in sys.path
               if not isinstance(entry, str) or normalized(entry) not in forbidden]
sys.path.insert(0, os.fspath(td))
module = types.ModuleType('embedded_depth_anything_sensor_runtime')
module.__file__ = '/project1/flexgpu/WORKING_PIPELINE/SENSOR_INTERACTION/DEPTH_SENSOR_ADAPTER/DEPTH_ANYTHING_BRIDGE/sensor_runtime'
sys.modules[module.__name__] = module
source = pathlib.Path({str(bridge_path)!r}).read_text(encoding='utf-8')
exec(compile(source, module.__file__, 'exec'), module.__dict__)
assert 'flexgpu.worldbus' in sys.modules
assert normalized(src) in {{normalized(entry) for entry in sys.path if isinstance(entry, str)}}
"""
        with tempfile.TemporaryDirectory() as directory:
            completed = subprocess.run(
                [sys.executable, "-S", "-c", command],
                cwd=directory,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_import_does_not_load_numpy_td_or_camera_model_packages(self) -> None:
        command = (
            "import sys; sys.path[:0]=[r'%s',r'%s']; "
            "import touchdesigner.depth_anything_sensor_runtime; "
            "assert 'numpy' not in sys.modules; assert 'td' not in sys.modules; "
            "assert 'cv2' not in sys.modules; assert 'torch' not in sys.modules"
        ) % (ROOT, SRC)
        completed = subprocess.run(
            [sys.executable, "-S", "-c", command],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_contract_validates_no_rgb_payload_and_exact_frame_identity(self) -> None:
        result = validate_sensor_result_frame(sensor_frame())
        self.assertEqual(result.valid_fraction, 0.75)
        self.assertAlmostEqual(result.confidence_mean, (255 + 128 + 64) / (3 * 255))
        state = derive_frame_state(result)
        self.assertEqual(state["timestamp_ns"], result.capture_timestamp_ns)
        self.assertEqual(state["calibration_id"], result.calibration_id)
        self.assertEqual(state["calibration_digest"], result.calibration_digest)
        self.assertNotIn("rgb", state)
        validated = load_runtime_helpers()["_validate_frame_state"](
            state, {"sensor": {"mode": "depth_sensor"}}, "sensor"
        )
        self.assertEqual(validated["calibration_id"], result.calibration_id)

        contains_rgb = sensor_frame().metadata.to_dict()
        contains_rgb["depth_anything_contains_rgb"] = True
        with self.assertRaisesRegex(SensorBridgeError, "must not contain camera RGB"):
            validate_sensor_result_frame(
                make_frame(contains_rgb, sensor_frame().payload)
            )

        identity = sensor_frame().metadata.to_dict()
        identity["sensor_frame_id"] = identity["frame_id"] + 1
        with self.assertRaisesRegex(SensorBridgeError, "frame id"):
            validate_sensor_result_frame(make_frame(identity, sensor_frame().payload))

    def test_payload_invariants_and_main_thread_upload(self) -> None:
        valid = sensor_frame()
        payload = bytearray(valid.payload)
        payload[2] = 1
        with self.assertRaisesRegex(SensorBridgeError, "mask must be binary"):
            validate_sensor_result_frame(make_frame(valid.metadata.to_dict(), payload))

        result = validate_sensor_result_frame(valid)
        runtime = SensorBridgeRuntime()
        top = FakeScriptTop()
        owner = threading.get_ident()
        self.assertTrue(runtime.upload_result_to_top(top, result))
        self.assertEqual(top.call_threads, [owner])
        self.assertEqual(top.arrays[0].dtype.name, "float32")
        expected = sensor_result_to_touchdesigner_numpy(result)
        self.assertEqual(expected.shape, (2, 2, 4))
        errors = []

        def cross_thread():
            try:
                runtime.upload_result_to_top(top, result)
            except Exception as exc:  # assertion inspects the exact boundary error
                errors.append(str(exc))

        thread = threading.Thread(target=cross_thread)
        thread.start()
        thread.join()
        self.assertIn("owner thread", errors[0])


class ReceiverLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.factory = ReceiverFactory()
        self.clock = StepClock()
        self.runtime = SensorBridgeRuntime(
            SensorBridgeConfig(stale_ms=800.0),
            receiver_factory=self.factory,
            monotonic_ns=self.clock.monotonic_ns,
            timestamp_ns=self.clock.wall_ns,
        )

    def tearDown(self) -> None:
        self.runtime.stop()

    def test_lifecycle_is_idempotent_and_receiver_is_result_only(self) -> None:
        self.runtime.start()
        self.runtime.start()
        self.assertEqual(len(self.factory.instances), 1)
        receiver = self.factory.instances[0]
        self.assertEqual(receiver.start_calls, 1)
        self.assertEqual(receiver.kwargs["host"], "127.0.0.1")
        self.assertEqual(receiver.kwargs["tcp_port"], 9241)
        self.assertIsNone(receiver.kwargs["udp_port"])
        self.assertFalse(hasattr(self.runtime, "capture_top"))
        self.runtime.stop()
        self.runtime.stop()
        self.assertEqual(receiver.close_calls, 1)
        self.runtime.start()
        self.assertEqual(len(self.factory.instances), 2)

    def test_non_loopback_bind_requires_explicit_trusted_network_opt_in(self) -> None:
        with self.assertRaisesRegex(SensorBridgeError, "trusted-network opt-in"):
            SensorBridgeConfig(bind_host="0.0.0.0")
        selected = SensorBridgeConfig(
            bind_host="192.0.2.10", allow_trusted_network=True
        )
        self.assertEqual(selected.bind_host, "192.0.2.10")
        self.assertTrue(selected.allow_trusted_network)
        with self.assertRaisesRegex(SensorBridgeError, "must be boolean"):
            SensorBridgeConfig(allow_trusted_network=1)  # type: ignore[arg-type]
        bridge = FakeBridge()
        bridge.par.Allowtrustednetwork.val = "false"
        with self.assertRaisesRegex(SensorBridgeError, "explicit boolean toggle"):
            sensor_module._component_config(bridge)

    def test_reserved_udp_occupancy_cannot_block_real_tcp_result_receiver(self) -> None:
        blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        blocker.bind(("127.0.0.1", 0))
        occupied_udp = int(blocker.getsockname()[1])
        receivers = []

        def tcp_only_factory(**kwargs):
            self.assertIsNone(kwargs["udp_port"])
            receiver = WorldBusReceiver(
                host=kwargs["host"],
                tcp_port=0,
                udp_port=kwargs["udp_port"],
                stale_after_s=kwargs["stale_after_s"],
                limits=kwargs["limits"],
            )
            receivers.append(receiver)
            return receiver

        runtime = SensorBridgeRuntime(
            SensorBridgeConfig(result_udp=occupied_udp),
            receiver_factory=tcp_only_factory,
        )
        try:
            runtime.start()
            self.assertEqual(len(receivers), 1)
            with TCPFrameSender(*receivers[0].tcp_address) as sender:
                sender.send(sensor_frame())
            wait_until(lambda: runtime.latest_result() is not None)
            self.assertEqual(runtime.status()["reserved_result_udp"], occupied_udp)
            self.assertEqual(runtime.status()["accepted_results"], 1)
        finally:
            runtime.stop()
            blocker.close()

    def test_capture_timestamp_drives_freshness_and_stale_fails_closed(self) -> None:
        self.runtime.start()
        receiver = self.factory.instances[0]
        receiver.frames.put(sensor_frame(capture_timestamp_ns=self.clock.wall))
        wait_until(lambda: self.runtime.latest_result() is not None)
        self.assertTrue(self.runtime.status()["result_fresh"])
        self.clock.advance(801)
        self.assertIsNone(self.runtime.latest_result())
        self.assertEqual(self.runtime.status()["state"], "stale")

        receiver.frames.put(
            sensor_frame(frame_id=2, capture_timestamp_ns=self.clock.wall - 900_000_000)
        )
        wait_until(lambda: self.runtime.status()["rejected_results"] == 1)
        self.assertEqual(self.runtime.status()["last_error"], "stale_capture_timestamp")
        self.assertIsNone(self.runtime.latest_result())

    def test_session_frame_and_calibration_are_correlated_exactly(self) -> None:
        self.runtime.start()
        receiver = self.factory.instances[0]
        receiver.frames.put(sensor_frame(capture_timestamp_ns=self.clock.wall))
        wait_until(lambda: self.runtime.latest_result() is not None)

        receiver.frames.put(
            sensor_frame(
                2,
                capture_timestamp_ns=self.clock.wall,
                calibration_id="changed",
                calibration_digest="1" * 64,
            )
        )
        wait_until(lambda: self.runtime.status()["rejected_results"] == 1)
        self.assertEqual(
            self.runtime.status()["last_error"], "session_calibration_changed"
        )
        self.assertIsNone(self.runtime.latest_result())

        receiver.frames.put(
            sensor_frame(
                1,
                capture_timestamp_ns=self.clock.wall,
                producer_session_id="webcam-session-b",
                calibration_id="changed",
                calibration_digest="1" * 64,
            )
        )
        wait_until(
            lambda: self.runtime.latest_result() is not None
            and self.runtime.latest_result().producer_session_id == "webcam-session-b"
        )
        accepted = receiver.frames.put(
            sensor_frame(
                3,
                capture_timestamp_ns=self.clock.wall,
                producer_session_id="webcam-session-a",
            )
        )
        self.assertFalse(accepted)
        self.assertEqual(receiver.frames.stats["rejected_retired_session"], 1)
        self.assertEqual(self.runtime.latest_result().producer_session_id,
                         "webcam-session-b")

    def test_newest_only_queue_and_receiver_error_invalidate_current(self) -> None:
        self.runtime.start()
        receiver = self.factory.instances[0]
        receiver.frames.put(sensor_frame(1, capture_timestamp_ns=self.clock.wall))
        receiver.frames.put(sensor_frame(2, capture_timestamp_ns=self.clock.wall))
        wait_until(
            lambda: self.runtime.latest_result() is not None
            and self.runtime.latest_result().frame_id == 2
        )
        self.assertGreaterEqual(receiver.frames.stats["superseded"], 1)
        receiver._errors.append("simulated disconnect")
        wait_until(lambda: self.runtime.status()["receiver_errors"] == 1)
        self.assertIsNone(self.runtime.latest_result())
        self.assertEqual(self.runtime.status()["last_error"], "receiver_error")


class ModuleCallbackTests(unittest.TestCase):
    def tearDown(self) -> None:
        sensor_module.stop()

    def test_exact_script_top_handshake_precedes_frame_state_publish(self) -> None:
        accepted = validate_sensor_result_frame(sensor_frame())
        component = FakeBridge()
        main_thread = threading.get_ident()

        class FakeRuntime:
            def __init__(self, _config) -> None:
                self.config = _config
                self.current = accepted
                self.upload_threads = []
                self.errors = []
                self.stopped = 0

            def start(self):
                return self

            def stop(self):
                self.stopped += 1

            def latest_result(self):
                return self.current

            def upload_result_to_top(self, top, result):
                self.upload_threads.append(threading.get_ident())
                top.copyNumpyArray(sensor_result_to_touchdesigner_numpy(result))
                return True

            def _record_error(self, code, invalidate=False):
                self.errors.append(code)
                if invalidate:
                    self.current = None

            def status(self):
                return {"state": "running", "last_error": "none"}

        created = []

        def factory(config):
            runtime = FakeRuntime(config)
            created.append(runtime)
            return runtime

        with mock.patch.object(sensor_module, "SensorBridgeRuntime", side_effect=factory):
            sensor_module.tick(component)
            self.assertTrue(component.par.Resultvalid.val)
            self.assertEqual(component.nodes["RESULT_PACKED"].cook_forces, [True])
            self.assertEqual(created[0].upload_threads, [main_thread])
            state = json.loads(component.nodes["FRAME_STATE"].text)
            self.assertEqual(state["frame_id"], accepted.frame_id)
            self.assertEqual(state["calibration_id"], accepted.calibration_id)
            self.assertEqual(
                component.nodes["DEPTH_CALIBRATION"].par.colorr.val, 0.001
            )
            self.assertEqual(
                component.nodes["INTRINSICS_NORMALIZED"].par.colorb.val, 0.5
            )

            # A normal cook return is not confirmation: only the callback may
            # mark the exact staged key as uploaded.
            component.nodes["RESULT_PACKED"] = FakeScriptTop()
            created[0].current = validate_sensor_result_frame(sensor_frame(2))
            sensor_module.tick(component)
            self.assertFalse(component.par.Resultvalid.val)
            self.assertIn("packed_upload_unconfirmed", created[0].errors)


class InstallerSourceTests(unittest.TestCase):
    def test_installer_is_bounded_default_off_and_backend_replaceable(self) -> None:
        path = ROOT / "touchdesigner" / "runtime_pipeline.py"
        spec = importlib.util.spec_from_file_location("sensor_pipeline", path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        source = path.read_text(encoding="utf-8")
        installer = source.split("def install_depth_anything_sensor_bridge", 1)[1]
        installer = installer.split("def install_moge2_bridge", 1)[0]
        self.assertIn("fallbacks.append(source)", installer)
        self.assertIn("_wire_depth_anything_sensor_routes", installer)
        self.assertNotIn("build(", installer)
        self.assertNotIn("destroy", installer)
        self.assertIn("installed disabled", installer)
        for name in (
            "DEPTH_ANYTHING_BRIDGE",
            "RESULT_PACKED",
            "OUT_POSITION",
            "OUT_MASK",
            "OUT_CONFIDENCE",
            "FRAME_STATE",
            "DEPTH_ANYTHING_FAIL_CLOSED_ZERO",
        ):
            self.assertIn(name, source)
        self.assertIn("Spout, NDI, TOP, or API", source)
        self.assertIn("No RGB enters this COMP", source)
        self.assertIn("Allowtrustednetwork", source)
        self.assertIn("Reserved UDP (unused)", source)
        self.assertNotIn('_set(packed, "alwayscook"', source)


if __name__ == "__main__":
    unittest.main()
