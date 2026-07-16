from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_PATH = ROOT / "touchdesigner" / "bootstrap_project.py"


def load_helpers():
    spec = importlib.util.spec_from_file_location("bootstrap_project", BOOTSTRAP_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not import bootstrap_project.py")
    bootstrap = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bootstrap)
    namespace: dict[str, object] = {}
    exec(bootstrap.RUNTIME_HELPERS, namespace)
    return namespace


class FakeParameter:
    def __init__(self, name: str, value=None) -> None:
        self.name = name
        self.val = value
        self.pulse_count = 0

    def eval(self):
        return self.val

    def pulse(self) -> None:
        self.pulse_count += 1


class FakeParameters:
    def __init__(self, values: dict[str, object]) -> None:
        self._parameters = {
            name: FakeParameter(name, value) for name, value in values.items()
        }

    def __getattr__(self, name: str) -> FakeParameter:
        try:
            return self._parameters[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


class FakeNode:
    def __init__(
        self, path: str, *, operator_errors=None, operator_warnings=None,
        **parameters: object,
    ) -> None:
        self.path = path
        self.par = FakeParameters(parameters)
        self.allowCooking = True
        self.children: list[FakeNode] = []
        self._operator_errors = list(operator_errors or [])
        self._operator_warnings = list(operator_warnings or [])
        self.error_inspections = 0
        self.warning_inspections = 0

    def pars(self):
        return list(self.par._parameters.values())

    def errors(self):
        self.error_inspections += 1
        return list(self._operator_errors)

    def warnings(self):
        self.warning_inspections += 1
        return list(self._operator_warnings)


class FakeTextNode(FakeNode):
    def __init__(self, path: str, text: str) -> None:
        super().__init__(path)
        self.text = text


class FakeChannel:
    def __init__(self, value: float) -> None:
        self.value = value

    def eval(self) -> float:
        return self.value

    def __getitem__(self, index: int) -> float:
        if index != 0:
            raise IndexError(index)
        return self.value


class FakeChopNode(FakeNode):
    def __init__(self, path: str, **channels: float) -> None:
        super().__init__(path)
        self.channels = {
            name: FakeChannel(value) for name, value in channels.items()
        }

    def __getitem__(self, name: str) -> FakeChannel:
        return self.channels[name]


class FakeCookNode(FakeNode):
    def __init__(
        self, path: str, *, width: int = 128, height: int = 128,
        **parameters: object,
    ) -> None:
        super().__init__(path, **parameters)
        self.cookAbsFrame = 0
        self.width = width
        self.height = height

    def cook(self, force: bool = False) -> None:
        self.cookAbsFrame += 1


class FakeStaticCookNode(FakeCookNode):
    def cook(self, force: bool = False) -> None:
        pass


class FakeRoot(FakeNode):
    def __init__(self, nodes: dict[str, FakeNode]) -> None:
        super().__init__("/project1/flexgpu")
        self.nodes = nodes
        self.children = list(nodes.values())
        self.storage: dict[str, object] = {}
        self.startup_storage: dict[str, object] = {}

    def op(self, path: str):
        return self.nodes.get(path)

    def store(self, key: str, value: object) -> None:
        self.storage[key] = value

    def storeStartupValue(self, key: str, value: object) -> None:
        self.startup_storage[key] = value

    def fetch(self, key: str, default=None):
        return self.storage.get(key, default)


def resolution_node(path: str) -> FakeNode:
    return FakeNode(
        path,
        outputresolution="useinput",
        resmult=True,
        resolutionw=1,
        resolutionh=1,
    )


def quiet_apply(helpers, root, overrides, *, inherit_environment=True):
    with contextlib.redirect_stdout(io.StringIO()):
        return helpers["apply"](
            root, overrides, inherit_environment=inherit_environment
        )


def complete_runtime_root() -> FakeRoot:
    nodes: dict[str, FakeNode] = {
        "WORKING_PIPELINE": FakeNode("WORKING_PIPELINE"),
        "WORKING_PIPELINE/SOURCES": FakeNode(
            "SOURCES", UseStreamDiffusion=False, UseExternalDepth=False,
            Frameid=-1, Sessionepoch=0, Sourceagems=-1.0,
            Newframe=True, Sourcevalid=True, Frametimestampseconds=-1.0,
        ),
        "WORKING_PIPELINE/SOURCES/STREAMDIFFUSION_ADAPTER": FakeNode(
            "STREAMDIFFUSION_ADAPTER", Enabled=False
        ),
        "WORKING_PIPELINE/RECONSTRUCTION": FakeNode(
            "RECONSTRUCTION", Geometryresolution=384,
            Depthmode="normalized", Depthscale=1.0, Depthbias=0.0,
            Nearmetres=0.35, Farmetres=4.5,
            Fxnormalized=0.0, Fynormalized=0.0,
            Cxnormalized=0.5, Cynormalized=0.5,
            Cameratoworld0="1 0 0 0", Cameratoworld1="0 1 0 0",
            Cameratoworld2="0 0 1 0", Cameratoworld3="0 0 0 1",
            Calibrationepoch=0,
        ),
        "WORKING_PIPELINE/POINT_RENDER": FakeNode(
            "POINT_RENDER", Maxpoints=120000, Pointsize=3.0
        ),
        "WORKING_PIPELINE/COMPLETION": FakeNode(
            "COMPLETION", Mode="hybrid", Fogdensity=0.35, Proceduralmix=0.72
        ),
        "WORKING_PIPELINE/SENSOR_INTERACTION": FakeNode(
            "SENSOR_INTERACTION", Mode="simulated", Interactionradius=0.55,
            Forcegain=1.0, Sensoragems=-1.0, Sensorframeid=-1,
            Sensortoworld0="1 0 0 0", Sensortoworld1="0 1 0 0",
            Sensortoworld2="0 0 1 0", Sensortoworld3="0 0 0 1",
        ),
        "WORKING_PIPELINE/SOURCES/RGB_SOURCE": FakeCookNode("RGB_SOURCE"),
        "WORKING_PIPELINE/SENSOR_INTERACTION/SIMULATED_SENSOR_MASK": FakeNode(
            "SIMULATED_SENSOR_MASK", radiusx=0.16, radiusy=0.16,
            centerx=0.0, centery=0.0,
        ),
        "WORKING_PIPELINE/SENSOR_INTERACTION/DEPTH_SENSOR_ADAPTER": FakeNode(
            "DEPTH_SENSOR_ADAPTER", Enabled=False
        ),
        "WORKING_PIPELINE/TEMPORAL_WORLD": FakeNode(
            "TEMPORAL_WORLD", Confidencedecay=0.985, Ageseconds=2.0,
            Sourceepoch=0, Resetcount=0, Newframe=True, Sourcevalid=True,
            Deltaseconds=1.0 / 60.0,
        ),
        "WORKING_PIPELINE/ROLE_BRIDGE": FakeNode(
            "ROLE_BRIDGE", Mode="local", Senderactive=False,
            Receiveractive=False, Segmentname="FlexShowWorldBus",
            Peeraddress="127.0.0.1", Atlaswidth=1024, Atlasheight=512,
            Atlasport=12000, Sendfps=5, Sendstep=12,
            Framesessionid="", Frameid=-1, Frametimestampns="-1",
            Calibrationid="", Calibrationdigest="", Framevalid=True,
        ),
        "WORKING_PIPELINE/TEMPORAL_WORLD/POSITION_HISTORY": FakeNode(
            "POSITION_HISTORY", reset=False
        ),
        "WORKING_PIPELINE/TEMPORAL_WORLD/COLOR_HISTORY": FakeNode(
            "COLOR_HISTORY", reset=False
        ),
        "WORKING_PIPELINE/TEMPORAL_WORLD/STATE_HISTORY": FakeNode(
            "STATE_HISTORY", reset=False
        ),
        "WORKING_PIPELINE/ROLE_BRIDGE/RX_SHARED_ATLAS": FakeCookNode(
            "RX_SHARED_ATLAS"
        ),
        "WORKING_PIPELINE/ROLE_BRIDGE/RX_TCP_ATLAS": FakeCookNode(
            "RX_TCP_ATLAS"
        ),
        "WORKING_PIPELINE/ROLE_BRIDGE/RX_TCP_ATLAS_INFO": FakeChopNode(
            "RX_TCP_ATLAS_INFO", connected=1, num_received_frames=0
        ),
        "WORKING_PIPELINE/TELEMETRY": FakeNode("TELEMETRY"),
        "WORKING_PIPELINE/INSTALLATION_OUTPUT": FakeNode("INSTALLATION_OUTPUT"),
        "WORKING_PIPELINE/STEREO_PREVIEW": FakeNode("STEREO_PREVIEW"),
        "AI_PIPELINE": FakeNode(
            "AI_PIPELINE",
            Diffusionresolution=512,
            Diffusionfps=10,
            Geometryresolution=384,
            Geometryfps=5,
            Enabled=True,
        ),
        "WORLD_CORE": FakeNode("WORLD_CORE", Pointbudget=120000, Enabled=True),
        "INSTALLATION_OUT": FakeNode("INSTALLATION_OUT", Targetfps=60, Enabled=True),
        "VR_OUT": FakeNode("VR_OUT", Targetfps=72, Enabled=False),
        "COMPLETION/switch_completion": FakeNode("switch_completion", index=2),
    }
    for path in (
        "WORKING_PIPELINE/OUT_POSITION",
        "WORKING_PIPELINE/OUT_COLOR",
        "WORKING_PIPELINE/OUT_INTERACTION",
        "WORKING_PIPELINE/OUT_INSTALLATION",
        "WORKING_PIPELINE/OUT_LEFT_EYE",
        "WORKING_PIPELINE/OUT_RIGHT_EYE",
        "WORKING_PIPELINE/OUT_STEREO_PREVIEW",
        "WORKING_PIPELINE/ROLE_BRIDGE/PACK_ATOMIC_ATLAS",
    ):
        nodes[path] = FakeCookNode(path)
    for path in (
        "WORKING_PIPELINE/SOURCES/DEMO_RGB_GENERATOR",
        "WORKING_PIPELINE/SOURCES/STREAMDIFFUSION_ADAPTER/REPLACE_WITH_STREAMDIFFUSION_RGB",
        "WORKING_PIPELINE/SOURCES/DEMO_DEPTH_GENERATOR",
        "WORKING_PIPELINE/SOURCES/STREAMDIFFUSION_ADAPTER/REPLACE_WITH_DEPTH_ESTIMATE",
        "WORKING_PIPELINE/POINT_RENDER/METRIC_RENDER_CENTER",
        "WORKING_PIPELINE/POINT_RENDER/METRIC_MONO_FALLBACK",
        "WORKING_PIPELINE/INSTALLATION_OUTPUT/installation_grade",
        "WORKING_PIPELINE/POINT_RENDER/METRIC_RENDER_LEFT_EYE",
        "WORKING_PIPELINE/POINT_RENDER/METRIC_RENDER_RIGHT_EYE",
        "WORKING_PIPELINE/STEREO_PREVIEW/STEREO_SIDE_BY_SIDE",
    ):
        nodes[path] = resolution_node(path)
    for path in (
        "WORKING_PIPELINE/ROLE_BRIDGE/RGB_ROUTE",
        "WORKING_PIPELINE/ROLE_BRIDGE/DEPTH_ROUTE",
        "WORKING_PIPELINE/ROLE_BRIDGE/CONFIDENCE_ROUTE",
        "WORKING_PIPELINE/ROLE_BRIDGE/MASK_ROUTE",
        "WORKING_PIPELINE/ROLE_BRIDGE/ATLAS_ROUTE",
    ):
        nodes[path] = FakeNode(path, index=0)
    nodes["WORKING_PIPELINE/COMPLETION/fog_completion_PIXEL"] = FakeTextNode(
        "fog_completion_PIXEL",
        "const float fogDensity = 0.35; // FLEXGPU_FOG_DENSITY\n",
    )
    nodes["WORKING_PIPELINE/COMPLETION/hybrid_completion_PIXEL"] = FakeTextNode(
        "hybrid_completion_PIXEL",
        "const float proceduralMix = 0.72; // FLEXGPU_PROCEDURAL_MIX\n",
    )
    nodes["WORKING_PIPELINE/RECONSTRUCTION/depth_to_position_PIXEL"] = FakeTextNode(
        "depth_to_position_PIXEL",
        "\n".join((
            "const int depthMode = 0; // FLEXGPU_DEPTH_MODE: defaults",
            "const float depthScale = 1.0; // FLEXGPU_DEPTH_SCALE",
            "const float depthBias = 0.0; // FLEXGPU_DEPTH_BIAS",
            "const float nearMetres = 0.35; // FLEXGPU_NEAR_METRES",
            "const float farMetres = 4.5; // FLEXGPU_FAR_METRES",
            "const float fxNormalized = 0.0; // FLEXGPU_INTRINSICS_FX",
            "const float fyNormalized = 0.0; // FLEXGPU_INTRINSICS_FY",
            "const float cxNormalized = 0.5; // FLEXGPU_INTRINSICS_CX",
            "const float cyNormalized = 0.5; // FLEXGPU_INTRINSICS_CY",
            "const vec4 cameraToWorld0 = vec4(1,0,0,0); // FLEXGPU_CAMERA_TO_WORLD_0",
            "const vec4 cameraToWorld1 = vec4(0,1,0,0); // FLEXGPU_CAMERA_TO_WORLD_1",
            "const vec4 cameraToWorld2 = vec4(0,0,1,0); // FLEXGPU_CAMERA_TO_WORLD_2",
            "const vec4 cameraToWorld3 = vec4(0,0,0,1); // FLEXGPU_CAMERA_TO_WORLD_3",
        )) + "\n",
    )
    nodes["WORKING_PIPELINE/SENSOR_INTERACTION/interaction_field_PIXEL"] = FakeTextNode(
        "interaction_field_PIXEL",
        "const float interactionRadiusMetres = 0.55; // FLEXGPU_INTERACTION_RADIUS\n"
        "const float forceGain = 1.0; // FLEXGPU_FORCE_GAIN\n",
    )
    nodes["WORKING_PIPELINE/SENSOR_INTERACTION/CALIBRATE_SENSOR_POSITION_PIXEL"] = FakeTextNode(
        "CALIBRATE_SENSOR_POSITION_PIXEL",
        "\n".join(
            "const vec4 sensorToWorld%d = vec4(0,0,0,0); // FLEXGPU_SENSOR_TO_WORLD_%d" % (i, i)
            for i in range(4)
        ) + "\n",
    )
    nodes["WORKING_PIPELINE/TEMPORAL_WORLD/temporal_state_PIXEL"] = FakeTextNode(
        "temporal_state_PIXEL",
        "const float confidenceDecay = 0.985; // FLEXGPU_CONFIDENCE_DECAY\n",
    )
    for path in (
        "WORKING_PIPELINE/INSTALLATION_OUTPUT/installation_grade_PIXEL",
        "WORKING_PIPELINE/STEREO_PREVIEW/GRADE_LEFT_EYE_PIXEL",
        "WORKING_PIPELINE/STEREO_PREVIEW/GRADE_RIGHT_EYE_PIXEL",
    ):
        nodes[path] = FakeTextNode(
            path,
            "const float viewFogDensity = 0.35; // FLEXGPU_VIEW_FOG_DENSITY\n"
            "const float viewFogRadius = 2.0; // FLEXGPU_VIEW_FOG_RADIUS\n",
        )
    return FakeRoot(nodes)


class TouchDesignerRuntimeHelperTests(unittest.TestCase):
    @staticmethod
    def frame_state(
        *, session_id: str = "session-a", frame_id: int = 1,
        timestamp_ns: int | None = None, digest: str = "a" * 64,
    ) -> dict[str, object]:
        return {
            "version": "flexgpu-frame-state/v1",
            "session_id": session_id,
            "frame_id": frame_id,
            "timestamp_ns": timestamp_ns or __import__("time").time_ns(),
            "width": 384,
            "height": 384,
            "calibration_id": "camera-v1",
            "calibration_digest": digest,
            "valid_fraction": 0.9,
            "confidence_mean": 0.8,
        }

    def test_apply_binds_adaptive_quality_to_real_pipeline_nodes(self) -> None:
        helpers = load_helpers()
        root = complete_runtime_root()
        root.op("WORKING_PIPELINE/SOURCES").par.UseExternalDepth.val = True
        state = quiet_apply(
            helpers,
            root,
            {
                "role": "world",
                "topology": "single",
                "experience": "combined",
                "tier": "4090",
                "source": {"mode": "streamdiffusion"},
                "sensor": {"mode": "disabled"},
                "render": {
                    "point_budget": 200000,
                    "point_size_px": 5.5,
                    "installation_width": 1920,
                    "installation_height": 1080,
                    "stereo_width": 3000,
                    "stereo_height": 900,
                    "fog_density": 0.6,
                    "procedural_mix": 0.4,
                },
                "adaptive": {"enabled": True, "levels": 3, "initial_level": 0},
                "telemetry": {"enabled": True},
            },
        )

        self.assertEqual(state["geometry_resolution"], 256)
        self.assertEqual(state["point_budget"], 256**2)
        self.assertEqual(
            root.op("WORKING_PIPELINE/RECONSTRUCTION").par.Geometryresolution.val,
            256,
        )
        point_render = root.op("WORKING_PIPELINE/POINT_RENDER")
        self.assertEqual(point_render.par.Maxpoints.val, 256**2)
        self.assertEqual(point_render.par.Pointsize.val, 5.5)
        self.assertEqual(
            root.op("WORKING_PIPELINE/SOURCES").par.UseStreamDiffusion.val, True
        )
        self.assertEqual(
            root.op("WORKING_PIPELINE/SOURCES").par.UseExternalDepth.val, True
        )
        sensor = root.op("WORKING_PIPELINE/SENSOR_INTERACTION")
        self.assertEqual(sensor.par.Mode.val, "disabled")
        mask = root.op(
            "WORKING_PIPELINE/SENSOR_INTERACTION/SIMULATED_SENSOR_MASK"
        )
        self.assertEqual((mask.par.radiusx.val, mask.par.radiusy.val), (0.16, 0.16))
        center = root.op("WORKING_PIPELINE/POINT_RENDER/METRIC_RENDER_CENTER")
        left = root.op("WORKING_PIPELINE/POINT_RENDER/METRIC_RENDER_LEFT_EYE")
        self.assertEqual((center.par.resolutionw.val, center.par.resolutionh.val), (1920, 1080))
        self.assertEqual((left.par.resolutionw.val, left.par.resolutionh.val), (1500, 900))
        self.assertTrue(root.op("WORKING_PIPELINE/TELEMETRY").allowCooking)
        completion = root.op("WORKING_PIPELINE/COMPLETION")
        self.assertEqual(completion.par.Fogdensity.val, 0.6)
        self.assertEqual(completion.par.Proceduralmix.val, 0.4)
        self.assertIn(
            "const float fogDensity = 0.6; // FLEXGPU_FOG_DENSITY",
            root.op("WORKING_PIPELINE/COMPLETION/fog_completion_PIXEL").text,
        )
        self.assertIn(
            "const float proceduralMix = 0.4; // FLEXGPU_PROCEDURAL_MIX",
            root.op("WORKING_PIPELINE/COMPLETION/hybrid_completion_PIXEL").text,
        )

    def test_absent_adapter_sections_preserve_saved_manual_selections(self) -> None:
        helpers = load_helpers()
        root = complete_runtime_root()
        sources = root.op("WORKING_PIPELINE/SOURCES")
        adapter = root.op("WORKING_PIPELINE/SOURCES/STREAMDIFFUSION_ADAPTER")
        sensor = root.op("WORKING_PIPELINE/SENSOR_INTERACTION")
        sources.par.UseStreamDiffusion.val = True
        sources.par.UseExternalDepth.val = True
        adapter.par.Enabled.val = True
        sensor.par.Mode.val = "replay"

        quiet_apply(helpers, root, {"tier": "3080ti_16gb"})

        self.assertTrue(sources.par.UseStreamDiffusion.val)
        self.assertTrue(sources.par.UseExternalDepth.val)
        self.assertTrue(adapter.par.Enabled.val)
        self.assertEqual(sensor.par.Mode.val, "replay")

    def test_build_style_apply_can_ignore_ambient_private_config(self) -> None:
        helpers = load_helpers()
        root = complete_runtime_root()
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "private-show.json"
            private_marker = "PRIVATE-TOX-MUST-NOT-PERSIST"
            config.write_text(
                json.dumps(
                    {
                        "role": "ai",
                        "source": {
                            "mode": "streamdiffusion",
                            "streamdiffusion_tox": private_marker + ".tox",
                            "auto_load_tox": True,
                        },
                        "sensor": {
                            "mode": "depth_sensor",
                            "adapter_tox": private_marker + "-sensor.tox",
                        },
                        "telemetry": {"jsonl_path": private_marker + ".jsonl"},
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.dict(
                helpers["os"].environ,
                {"FLEXGPU_CONFIG": str(config)},
                clear=True,
            ):
                state = quiet_apply(
                    helpers,
                    root,
                    {"role": "world", "topology": "single"},
                    inherit_environment=False,
                )

        self.assertEqual(state["role"], "world")
        self.assertNotIn("source", state)
        self.assertNotIn("sensor", state)
        self.assertNotIn("telemetry", state)
        self.assertNotIn(private_marker, json.dumps(root.storage, default=str))
        self.assertIsNone(root.startup_storage["_flexgpu_runtime"])
        self.assertEqual(root.startup_storage["runtime_state"], {})

    def test_runtime_console_summary_omits_private_state(self) -> None:
        helpers = load_helpers()
        root = complete_runtime_root()
        marker = "PRIVATE-CONSOLE-MARKER"
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            helpers["apply"](
                root,
                {
                    "source": {"mode": "demo", "replay_path": marker},
                    "telemetry": {"jsonl_path": marker},
                },
                inherit_environment=False,
            )
        self.assertNotIn(marker, output.getvalue())
        self.assertIn('"role":"standalone"', output.getvalue())

    def test_missing_depth_operator_does_not_disable_manually_wired_depth(self) -> None:
        helpers = load_helpers()
        root = complete_runtime_root()
        sources = root.op("WORKING_PIPELINE/SOURCES")
        sources.par.UseExternalDepth.val = True

        quiet_apply(
            helpers,
            root,
            {
                "tier": "3080ti_16gb",
                "source": {"mode": "streamdiffusion"},
            },
        )

        self.assertTrue(sources.par.UseStreamDiffusion.val)
        self.assertTrue(sources.par.UseExternalDepth.val)
        self.assertTrue(
            root.op("WORKING_PIPELINE/SOURCES/STREAMDIFFUSION_ADAPTER").par.Enabled.val
        )

    def test_invalid_environment_transport_override_fails_closed(self) -> None:
        helpers = load_helpers()
        root = complete_runtime_root()

        state = quiet_apply(
            helpers,
            root,
            {
                "role": "ai",
                "topology": "dual_local",
                "transport_type": "typo_transport",
            },
        )

        self.assertIn("unsupported transport.type", state["transport_error"])
        self.assertFalse(state["ai_active"])
        self.assertFalse(state["world_active"])
        self.assertEqual(state["transport_endpoint_active"], "")
        self.assertFalse(root.op("WORKING_PIPELINE/SOURCES").allowCooking)
        self.assertFalse(root.op("WORKING_PIPELINE/RECONSTRUCTION").allowCooking)

    def test_runtime_override_bounds_fail_closed_and_cap_point_texture_budget(self) -> None:
        helpers = load_helpers()
        invalid_root = complete_runtime_root()
        invalid = quiet_apply(
            helpers,
            invalid_root,
            {"geometry_resolution": 32, "point_budget": 9_000_000},
        )
        self.assertIn("geometry_resolution", invalid["runtime_error"])
        self.assertEqual(invalid["geometry_resolution"], 64)
        self.assertEqual(invalid["point_budget"], 64**2)
        self.assertFalse(invalid["world_active"])

        valid_root = complete_runtime_root()
        valid = quiet_apply(
            helpers,
            valid_root,
            {"geometry_resolution": 256, "point_budget": 200_000},
        )
        self.assertNotIn("runtime_error", valid)
        self.assertEqual(valid["point_budget"], 256**2)
        self.assertEqual(valid["point_budget_requested"], 200_000)
        self.assertIn("geometry_resolution^2", valid["point_budget_adjustment"])

    def test_valid_calibration_profile_binds_depth_and_world_transforms(self) -> None:
        helpers = load_helpers()
        root = complete_runtime_root()
        with tempfile.TemporaryDirectory() as directory:
            calibration = Path(directory) / "calibration.json"
            calibration.write_text(
                json.dumps(
                    {
                        "version": "flexgpu-calibration/v1",
                        "calibration_id": "test-camera-v1",
                        "image": {"width": 640, "height": 480},
                        "intrinsics": {"fx": 320, "fy": 360, "cx": 300, "cy": 220},
                        "depth": {
                            "encoding": "metres", "scale": 1.25, "bias": -0.1,
                            "near_m": 0.2, "far_m": 8.0,
                        },
                        "camera_to_world": [
                            1, 0, 0, 0.25, 0, 1, 0, 0, 0, 0, 1, -0.5, 0, 0, 0, 1,
                        ],
                        "sensor_to_world": [
                            1, 0, 0, -0.25, 0, 1, 0, 0, 0, 0, 1, 0.5, 0, 0, 0, 1,
                        ],
                    }
                ),
                encoding="utf-8",
            )
            state = quiet_apply(
                helpers,
                root,
                {
                    "source": {
                        "mode": "streamdiffusion",
                        "calibration_path": str(calibration),
                    }
                },
            )

        reconstruction = root.op("WORKING_PIPELINE/RECONSTRUCTION")
        self.assertEqual(state["calibration_status"], "ready")
        self.assertEqual(reconstruction.par.Depthmode.val, "metric")
        self.assertEqual(reconstruction.par.Depthscale.val, 1.25)
        self.assertAlmostEqual(reconstruction.par.Fxnormalized.val, 0.5)
        self.assertAlmostEqual(reconstruction.par.Fynormalized.val, 0.75)
        self.assertIn("0.25", reconstruction.par.Cameratoworld0.val)
        sensor = root.op("WORKING_PIPELINE/SENSOR_INTERACTION")
        self.assertIn("-0.25", sensor.par.Sensortoworld0.val)
        shader = root.op(
            "WORKING_PIPELINE/RECONSTRUCTION/depth_to_position_PIXEL"
        ).text
        self.assertIn("const int depthMode = 1; // FLEXGPU_DEPTH_MODE", shader)
        self.assertIn("const float depthScale = 1.25", shader)

    def test_calibration_depth_encodings_map_to_shader_modes(self) -> None:
        helpers = load_helpers()
        identity = [1, 0, 0, 0, 0, 1, 0, 0,
                    0, 0, 1, 0, 0, 0, 0, 1]
        with tempfile.TemporaryDirectory() as directory:
            for encoding, scale, expected in (
                ("millimetres", 0.001, "metric"),
                ("disparity", 2.0, "inverse"),
                ("inverse_depth", 1.0, "inverse"),
            ):
                with self.subTest(encoding=encoding):
                    root = complete_runtime_root()
                    path = Path(directory) / (encoding + ".json")
                    path.write_text(
                        json.dumps(
                            {
                                "version": "flexgpu-calibration/v1",
                                "calibration_id": "cal-" + encoding.replace("_", "-"),
                                "image": {"width": 320, "height": 240},
                                "intrinsics": {"fx": 200, "fy": 200, "cx": 160, "cy": 120},
                                "depth": {
                                    "encoding": encoding, "scale": scale, "bias": 0,
                                    "near_m": 0.1, "far_m": 10,
                                },
                                "camera_to_world": identity,
                                "sensor_to_world": identity,
                            }
                        ),
                        encoding="utf-8",
                    )
                    state = quiet_apply(
                        helpers,
                        root,
                        {"source": {"mode": "streamdiffusion",
                                    "calibration_path": str(path)}},
                    )
                    self.assertEqual(state["calibration_status"], "ready")
                    reconstruction = root.op("WORKING_PIPELINE/RECONSTRUCTION")
                    self.assertEqual(reconstruction.par.Depthmode.val, expected)
                    self.assertEqual(reconstruction.par.Depthscale.val, scale)

    def test_calibration_loader_rejects_nested_contract_drift(self) -> None:
        helpers = load_helpers()
        identity = [1, 0, 0, 0, 0, 1, 0, 0,
                    0, 0, 1, 0, 0, 0, 0, 1]
        base = {
            "version": "flexgpu-calibration/v1",
            "calibration_id": "strict-camera-v1",
            "image": {"width": 320, "height": 240},
            "intrinsics": {"fx": 200, "fy": 200, "cx": 160, "cy": 120},
            "depth": {
                "encoding": "metres", "scale": 1, "bias": 0,
                "near_m": 0.1, "far_m": 10,
            },
            "camera_to_world": identity,
            "sensor_to_world": identity,
        }
        cases = []
        invalid_identifier = json.loads(json.dumps(base))
        invalid_identifier["calibration_id"] = "../unsafe"
        cases.append(invalid_identifier)
        boolean_width = json.loads(json.dumps(base))
        boolean_width["image"]["width"] = True
        cases.append(boolean_width)
        unknown_intrinsic = json.loads(json.dumps(base))
        unknown_intrinsic["intrinsics"]["skew"] = 0
        cases.append(unknown_intrinsic)
        extreme_range = json.loads(json.dumps(base))
        extreme_range["depth"]["far_m"] = 1001
        cases.append(extreme_range)
        singular_transform = json.loads(json.dumps(base))
        singular_transform["sensor_to_world"][:12] = [0] * 12
        cases.append(singular_transform)
        scaled_transform = json.loads(json.dumps(base))
        scaled_transform["camera_to_world"][0] = 1.01
        cases.append(scaled_transform)
        sheared_transform = json.loads(json.dumps(base))
        sheared_transform["sensor_to_world"][1] = 0.02
        cases.append(sheared_transform)
        left_handed_transform = json.loads(json.dumps(base))
        left_handed_transform["camera_to_world"][0] = -1
        cases.append(left_handed_transform)

        with tempfile.TemporaryDirectory() as directory:
            for index, profile in enumerate(cases):
                with self.subTest(index=index):
                    path = Path(directory) / ("invalid-%d.json" % index)
                    path.write_text(json.dumps(profile), encoding="utf-8")
                    state = quiet_apply(
                        helpers,
                        complete_runtime_root(),
                        {"source": {"mode": "streamdiffusion",
                                    "calibration_path": str(path)}},
                    )
                    self.assertNotEqual(state.get("calibration_status"), "ready")
                    self.assertIn("calibration", state["source_fallback"])

    def test_unresolved_configured_source_and_sensor_outputs_fail_to_safe_modes(self) -> None:
        helpers = load_helpers()
        root = complete_runtime_root()
        source_state = quiet_apply(
            helpers,
            root,
            {
                "source": {
                    "mode": "streamdiffusion",
                    "rgb_operator": "missing/out_rgb",
                }
            },
        )
        self.assertEqual(source_state["source_mode_active"], "demo")
        self.assertIn("could not be resolved", source_state["source_fallback"])
        self.assertFalse(
            root.op("WORKING_PIPELINE/SOURCES").par.UseStreamDiffusion.val
        )

        sensor_state = quiet_apply(
            helpers,
            root,
            {
                "sensor": {
                    "mode": "depth_sensor",
                    "position_operator": "missing/out_position",
                }
            },
        )
        self.assertEqual(sensor_state["sensor_mode_active"], "simulated")
        self.assertIn("could not be resolved", sensor_state["sensor_fallback"])
        self.assertEqual(
            root.op("WORKING_PIPELINE/SENSOR_INTERACTION").par.Mode.val,
            "simulated",
        )

    def test_auto_load_is_opt_in_and_missing_local_tox_never_activates_adapter(self) -> None:
        helpers = load_helpers()
        root = complete_runtime_root()
        state = quiet_apply(
            helpers,
            root,
            {
                "source": {
                    "mode": "streamdiffusion",
                    "auto_load_tox": True,
                    "streamdiffusion_tox": "missing-private-component.tox",
                    "rgb_operator": "out_rgb",
                }
            },
        )
        self.assertEqual(state["source_mode_active"], "demo")
        self.assertEqual(
            state["source_adapter_error"],
            "configured local .tox is missing or invalid",
        )
        self.assertFalse(
            root.op("WORKING_PIPELINE/SOURCES/STREAMDIFFUSION_ADAPTER").par.Enabled.val
        )
        health = helpers["_health_snapshot"](
            root, root.fetch("_flexgpu_runtime"), 16.0
        )
        self.assertIn("source_fallback", health["warnings"])

    def test_split_roles_load_only_their_owned_private_adapters(self) -> None:
        helpers = load_helpers()
        transport = {
            "type": "shared_memory",
            "segment_name": "FlexShowRoleTest",
            "atlas_width": 1024,
            "atlas_height": 512,
            "atlas_fps": 5,
        }

        world_state = quiet_apply(
            helpers,
            complete_runtime_root(),
            {
                "role": "world",
                "topology": "dual_local",
                "transport": transport,
                "source": {
                    "mode": "streamdiffusion",
                    "auto_load_tox": True,
                    "streamdiffusion_tox": "missing-private-source.tox",
                    "rgb_operator": "out_rgb",
                },
            },
        )
        self.assertFalse(world_state["ai_active"])
        self.assertEqual(world_state["source_mode_active"], "remote")
        self.assertNotIn("source_adapter_error", world_state)

        ai_state = quiet_apply(
            helpers,
            complete_runtime_root(),
            {
                "role": "ai",
                "topology": "dual_local",
                "transport": transport,
                "sensor": {
                    "mode": "depth_sensor",
                    "auto_load_tox": True,
                    "adapter_tox": "missing-private-sensor.tox",
                    "position_operator": "out_position",
                },
            },
        )
        self.assertFalse(ai_state["world_active"])
        self.assertEqual(ai_state["sensor_mode_active"], "inactive")
        self.assertNotIn("sensor_adapter_error", ai_state)

    def test_metadata_contract_resolution_accepts_non_top_operators(self) -> None:
        helpers = load_helpers()
        root = complete_runtime_root()
        metadata = FakeNode("metadata")
        metadata.isTOP = False
        root.nodes["metadata"] = metadata
        self.assertIs(
            helpers["_child_op"](
                root, root, "metadata", require_top=False
            ),
            metadata,
        )
        self.assertIsNone(helpers["_child_op"](root, root, "metadata"))

    def test_explicit_frame_lifecycle_pulses_once_and_rejects_old_frames(self) -> None:
        helpers = load_helpers()
        root = complete_runtime_root()
        initial = self.frame_state()
        metadata = FakeTextNode("frame_state", json.dumps(initial))
        metadata.isTOP = False
        root.nodes["frame_state"] = metadata
        quiet_apply(
            helpers,
            root,
            {"source": {"mode": "demo", "frame_state_operator": "frame_state"}},
        )
        sources = root.op("WORKING_PIPELINE/SOURCES")
        with mock.patch.object(helpers["time"], "perf_counter", return_value=10.0):
            helpers["tick"](root)
        self.assertTrue(sources.par.Newframe.val)
        runtime = root.fetch("_flexgpu_runtime")
        self.assertEqual(
            runtime["frame_lifecycle"]["source"]["decision"], "accepted"
        )

        with mock.patch.object(helpers["time"], "perf_counter", return_value=10.01):
            helpers["tick"](root)
        self.assertFalse(sources.par.Newframe.val)
        self.assertTrue(sources.par.Sourcevalid.val)
        self.assertEqual(
            runtime["frame_lifecycle"]["source"]["decision"], "held"
        )

        older = dict(initial, frame_id=0, timestamp_ns=int(initial["timestamp_ns"]) - 1)
        metadata.text = json.dumps(older)
        with mock.patch.object(helpers["time"], "perf_counter", return_value=10.02):
            helpers["tick"](root)
        source_state = runtime["frame_lifecycle"]["source"]
        self.assertEqual(source_state["decision"], "out_of_order_rejected")
        self.assertFalse(source_state["valid"])
        self.assertFalse(sources.par.Newframe.val)

    def test_frame_session_change_retires_previous_session(self) -> None:
        helpers = load_helpers()
        runtime = {"frame_lifecycle": {}}
        now = 2_000_000_000
        first = helpers["_validate_frame_state"](
            self.frame_state(timestamp_ns=now - 1_000_000), {}
        )
        second = helpers["_validate_frame_state"](
            self.frame_state(
                session_id="session-b", frame_id=0,
                timestamp_ns=now - 500_000,
            ),
            {},
        )
        helpers["_accept_explicit_frame"](
            runtime, "source", first, now, 1.0, 1000.0
        )
        switched = dict(helpers["_accept_explicit_frame"](
            runtime, "source", second, now, 1.1, 1000.0
        ))
        self.assertEqual(switched["decision"], "new_session")
        replay = helpers["_accept_explicit_frame"](
            runtime, "source", first, now, 1.2, 1000.0
        )
        self.assertEqual(replay["decision"], "retired_session_rejected")

    def test_metadata_less_cook_frame_fallback_holds_without_reabsorption(self) -> None:
        helpers = load_helpers()
        runtime = {"frame_lifecycle": {}}
        first = dict(helpers["_accept_fallback_frame"](
            runtime, "source", 44, 1.0, 1000.0
        ))
        held = dict(helpers["_accept_fallback_frame"](
            runtime, "source", 44, 1.1, 1000.0
        ))
        stale = dict(helpers["_accept_fallback_frame"](
            runtime, "source", 44, 2.1, 1000.0
        ))
        self.assertTrue(first["new_frame"])
        self.assertFalse(held["new_frame"])
        self.assertEqual(held["decision"], "held_fallback")
        self.assertFalse(stale["valid"])
        self.assertEqual(stale["decision"], "stale_fallback")

    def test_disabled_sensor_lifecycle_is_invalid_and_never_new(self) -> None:
        helpers = load_helpers()
        root = complete_runtime_root()
        quiet_apply(helpers, root, {"sensor": {"mode": "disabled"}})

        with mock.patch.object(helpers["time"], "perf_counter", return_value=1.0):
            helpers["tick"](root)

        sensor = root.fetch("_flexgpu_runtime")["frame_lifecycle"]["sensor"]
        self.assertFalse(sensor["new_frame"])
        self.assertFalse(sensor["valid"])
        self.assertEqual(sensor["accepted_count"], 0)
        self.assertEqual(sensor["decision"], "disabled")
        self.assertEqual(sensor["metadata_mode"], "disabled")

    def test_shared_receiver_never_uses_local_cook_frames_as_producer_frames(self) -> None:
        helpers = load_helpers()
        root = complete_runtime_root()
        quiet_apply(
            helpers,
            root,
            {
                "role": "world",
                "topology": "dual_local",
                "transport": {"type": "shared_memory"},
                "source": {"stale_timeout_ms": 100},
            },
        )
        endpoint = root.op("WORKING_PIPELINE/ROLE_BRIDGE/RX_SHARED_ATLAS")

        with mock.patch.object(helpers["time"], "perf_counter", return_value=1.0):
            helpers["tick"](root)
        endpoint.cook(force=True)
        with mock.patch.object(helpers["time"], "perf_counter", return_value=1.05):
            helpers["tick"](root)
        source = root.fetch("_flexgpu_runtime")["frame_lifecycle"]["source"]
        self.assertFalse(source["new_frame"])
        self.assertFalse(source["valid"])
        self.assertEqual(source["accepted_count"], 0)
        self.assertEqual(source["decision"], "remote_metadata_required")
        self.assertEqual(source["metadata_mode"], "remote_requires_explicit")

        endpoint.cook(force=True)
        with mock.patch.object(helpers["time"], "perf_counter", return_value=1.11):
            helpers["tick"](root)
        source = root.fetch("_flexgpu_runtime")["frame_lifecycle"]["source"]
        self.assertFalse(source["new_frame"])
        self.assertFalse(source["valid"])
        self.assertEqual(source["decision"], "stale_remote_unverified")

    def test_tcp_receiver_uses_received_counter_and_holds_until_stale(self) -> None:
        helpers = load_helpers()
        root = complete_runtime_root()
        quiet_apply(
            helpers,
            root,
            {
                "role": "world",
                "topology": "dual_local",
                "transport": {"type": "touch_tcp"},
                "source": {"stale_timeout_ms": 100},
            },
        )
        info = root.op("WORKING_PIPELINE/ROLE_BRIDGE/RX_TCP_ATLAS_INFO")
        endpoint = root.op("WORKING_PIPELINE/ROLE_BRIDGE/RX_TCP_ATLAS")
        info.channels["num_received_frames"].value = 1

        with mock.patch.object(helpers["time"], "perf_counter", return_value=2.0):
            helpers["tick"](root)
        source = root.fetch("_flexgpu_runtime")["frame_lifecycle"]["source"]
        self.assertTrue(source["new_frame"])
        self.assertTrue(source["valid"])

        self.assertEqual(source["metadata_mode"], "transport_receive_counter")

        endpoint.cook(force=True)
        with mock.patch.object(helpers["time"], "perf_counter", return_value=2.05):
            helpers["tick"](root)
        source = root.fetch("_flexgpu_runtime")["frame_lifecycle"]["source"]
        self.assertFalse(source["new_frame"])
        self.assertEqual(source["decision"], "held_fallback")

        endpoint.cook(force=True)
        with mock.patch.object(helpers["time"], "perf_counter", return_value=2.11):
            helpers["tick"](root)
        source = root.fetch("_flexgpu_runtime")["frame_lifecycle"]["source"]
        self.assertFalse(source["new_frame"])
        self.assertFalse(source["valid"])
        self.assertEqual(source["decision"], "stale_fallback")

        info.channels["num_received_frames"].value = 2
        with mock.patch.object(helpers["time"], "perf_counter", return_value=2.12):
            helpers["tick"](root)
        source = root.fetch("_flexgpu_runtime")["frame_lifecycle"]["source"]
        self.assertTrue(source["new_frame"])
        self.assertTrue(source["valid"])

        info.channels["connected"].value = 0
        with mock.patch.object(helpers["time"], "perf_counter", return_value=2.13):
            helpers["tick"](root)
        source = root.fetch("_flexgpu_runtime")["frame_lifecycle"]["source"]
        self.assertFalse(source["new_frame"])
        self.assertTrue(source["valid"])
        self.assertEqual(source["decision"], "transport_disconnected")

        with mock.patch.object(helpers["time"], "perf_counter", return_value=2.23):
            helpers["tick"](root)
        source = root.fetch("_flexgpu_runtime")["frame_lifecycle"]["source"]
        self.assertFalse(source["valid"])
        self.assertEqual(source["decision"], "stale_remote_unverified")

        info.channels["connected"].value = 1
        info.channels["num_received_frames"].value = 1
        with mock.patch.object(helpers["time"], "perf_counter", return_value=2.24):
            helpers["tick"](root)
        source = root.fetch("_flexgpu_runtime")["frame_lifecycle"]["source"]
        self.assertTrue(source["new_frame"])
        self.assertTrue(source["valid"])
        self.assertEqual(source["decision"], "new_transport_session")
        self.assertEqual(source["session_id"], "transport-receiver-1")
        self.assertEqual(
            root.op("WORKING_PIPELINE/SOURCES").par.Sessionepoch.val, 1
        )

        info.channels["num_received_frames"].value = 5
        with mock.patch.object(helpers["time"], "perf_counter", return_value=2.25):
            helpers["tick"](root)
        info.channels["num_received_frames"].value = 1
        with mock.patch.object(helpers["time"], "perf_counter", return_value=2.26):
            helpers["tick"](root)
        source = root.fetch("_flexgpu_runtime")["frame_lifecycle"]["source"]
        self.assertTrue(source["new_frame"])
        self.assertEqual(source["decision"], "new_transport_session")
        self.assertEqual(source["session_id"], "transport-receiver-2")

    def test_invalid_frame_calibration_digest_is_rejected(self) -> None:
        helpers = load_helpers()
        state = {"calibration_id": "camera-v1", "calibration_digest": "b" * 64}
        with self.assertRaisesRegex(ValueError, "calibration_digest"):
            helpers["_validate_frame_state"](self.frame_state(), state)

    def test_temporal_feedback_resets_only_when_contract_signature_changes(self) -> None:
        helpers = load_helpers()
        root = complete_runtime_root()
        quiet_apply(helpers, root, {"tier": "3080ti_16gb"})
        histories = [
            root.op("WORKING_PIPELINE/TEMPORAL_WORLD/" + name)
            for name in ("POSITION_HISTORY", "COLOR_HISTORY", "STATE_HISTORY")
        ]
        self.assertEqual([node.par.reset.pulse_count for node in histories], [1, 1, 1])

        quiet_apply(helpers, root, {"tier": "3080ti_16gb"})
        self.assertEqual([node.par.reset.pulse_count for node in histories], [1, 1, 1])

        root.op("WORKING_PIPELINE/SOURCES").par.Sessionepoch.val = 1
        with mock.patch.object(helpers["time"], "perf_counter", return_value=10.0):
            self.assertIsNone(helpers["tick"](root))
        self.assertEqual([node.par.reset.pulse_count for node in histories], [2, 2, 2])
        runtime = root.fetch("_flexgpu_runtime")
        self.assertEqual(runtime["temporal_reset_count"], 2)
        self.assertEqual(runtime["state"]["temporal_reset_count"], 2)

    def test_execute_callbacks_drive_adaptation_and_final_telemetry_flush(self) -> None:
        spec = importlib.util.spec_from_file_location("bootstrap_project", BOOTSTRAP_PATH)
        self.assertIsNotNone(spec)
        bootstrap = importlib.util.module_from_spec(spec)
        assert spec is not None and spec.loader is not None
        spec.loader.exec_module(bootstrap)
        self.assertIn("def onFrameStart(frame):", bootstrap.STARTUP_CALLBACKS)
        self.assertIn("module_dat.module.tick(root_comp)", bootstrap.STARTUP_CALLBACKS)
        self.assertIn("def onExit():", bootstrap.STARTUP_CALLBACKS)
        self.assertIn("flush_telemetry(root_comp, True)", bootstrap.STARTUP_CALLBACKS)

    def test_frame_tick_changes_live_quality_and_writes_configured_telemetry(self) -> None:
        helpers = load_helpers()
        root = complete_runtime_root()
        with tempfile.TemporaryDirectory() as directory:
            jsonl = Path(directory) / "live.jsonl"
            summary = Path(directory) / "summary.json"
            quiet_apply(
                helpers,
                root,
                {
                    "tier": "4090",
                    "adaptive": {
                        "enabled": True,
                        "levels": 3,
                        "initial_level": 2,
                        "frame_budget_ms": 10,
                        "down_window": 1,
                        "cooldown_samples": 0,
                    },
                    "telemetry": {
                        "enabled": True,
                        "jsonl_path": str(jsonl),
                        "summary_path": str(summary),
                        "sample_interval_frames": 1,
                        "flush_every": 1,
                    },
                },
            )
            with mock.patch.object(
                helpers["time"], "perf_counter", side_effect=[10.0, 10.1]
            ):
                self.assertIsNone(helpers["tick"](root))
                decision = helpers["tick"](root)

            self.assertTrue(decision["changed"])
            self.assertEqual(decision["level"], 1)
            self.assertEqual(
                root.op("WORKING_PIPELINE/POINT_RENDER").par.Maxpoints.val, 384**2
            )
            self.assertTrue(jsonl.is_file())
            record = json.loads(jsonl.read_text(encoding="utf-8").strip())
            self.assertEqual(record["adaptive_level"], 1)
            self.assertEqual(record["settings"]["point_budget"], 384**2)
            self.assertIn("health", record)
            self.assertIn("temporal_resets", record["health"])

            helpers["flush_telemetry"](root, True)
            payload = json.loads(summary.read_text(encoding="utf-8"))
            self.assertEqual(payload["samples"], 1)
            self.assertEqual(payload["final_level"], 1)

    def test_runtime_never_replaces_jsonl_with_summary_at_the_same_path(self) -> None:
        helpers = load_helpers()
        root = complete_runtime_root()
        with tempfile.TemporaryDirectory() as directory:
            shared = Path(directory) / "telemetry.jsonl"
            quiet_apply(
                helpers,
                root,
                {
                    "telemetry": {
                        "enabled": True,
                        "jsonl_path": str(shared),
                        "summary_path": str(shared),
                        "sample_interval_frames": 1,
                        "flush_every": 1,
                    }
                },
            )
            with mock.patch.object(
                helpers["time"], "perf_counter", side_effect=[10.0, 10.1]
            ):
                helpers["tick"](root)
                helpers["tick"](root)
            before = shared.read_text(encoding="utf-8")
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                helpers["flush_telemetry"](root, True)
            self.assertEqual(shared.read_text(encoding="utf-8"), before)
            self.assertIn("paths are identical", stream.getvalue())

    def test_atomic_heartbeat_contains_readiness_without_local_paths(self) -> None:
        helpers = load_helpers()
        root = complete_runtime_root()
        with tempfile.TemporaryDirectory() as directory:
            heartbeat = Path(directory) / "heartbeat.json"
            env = {
                "FLEXGPU_SESSION_ID": "show-session-1",
                "FLEXGPU_HEARTBEAT_PATH": str(heartbeat),
                "FLEXGPU_HEARTBEAT_TIMEOUT_MS": "3000",
            }
            with mock.patch.dict(helpers["os"].environ, env, clear=False):
                quiet_apply(helpers, root, {"role": "world"})
                starting = json.loads(heartbeat.read_text(encoding="utf-8"))
                self.assertEqual(starting["state"], "starting")
                self.assertIn(
                    "cook_not_advancing", starting["readiness"]["reasons"]
                )
                helpers["tick"](root)
                helpers["tick"](root)
            payload = json.loads(heartbeat.read_text(encoding="utf-8"))
            self.assertEqual(payload["version"], 1)
            self.assertEqual(payload["session_id"], "show-session-1")
            self.assertEqual(payload["role"], "world")
            self.assertEqual(payload["state"], "ready")
            self.assertEqual(payload["build"]["version"], "1.2.1")
            self.assertRegex(payload["config"]["identity"], r"^[0-9a-f]{64}$")
            self.assertIn("source", payload)
            self.assertIn("transport", payload)
            self.assertTrue(payload["readiness"]["ready"])
            self.assertGreaterEqual(payload["cook"]["count"], 2)
            self.assertGreaterEqual(payload["readiness"]["source_accepted"], 1)
            managed = payload["readiness"]["managed_health"]
            self.assertTrue(managed["scan_complete"])
            self.assertEqual(managed["operator_error_count"], 0)
            self.assertFalse(managed["invalid_outputs"])
            self.assertTrue(payload["output"]["required"])
            self.assertTrue(all(
                item["advances"] >= 1
                for item in payload["output"]["required"]
            ))
            self.assertNotIn(str(heartbeat), json.dumps(payload))
            self.assertFalse((Path(str(heartbeat) + ".tmp")).exists())

    def test_readiness_fails_closed_on_managed_operator_or_shader_errors(self) -> None:
        helpers = load_helpers()
        root = complete_runtime_root()
        quiet_apply(helpers, root, {"role": "world"})
        helpers["tick"](root)
        helpers["tick"](root)
        runtime = root.fetch("_flexgpu_runtime")

        operator = root.op("WORKING_PIPELINE/TEMPORAL_WORLD")
        operator._operator_errors = ["Feedback input is not connected"]
        managed = helpers["_inspect_readiness_health"](
            root, runtime, 10.0, force=True
        )
        readiness = helpers["_application_readiness"](
            root, runtime, managed_health=managed
        )
        self.assertEqual(readiness["state"], "degraded")
        self.assertIn("managed_operator_errors", readiness["reasons"])
        self.assertEqual(managed["operator_error_count"], 1)
        self.assertEqual(
            managed["operator_errors"][0]["path"],
            "TEMPORAL_WORLD",
        )

        operator._operator_errors = []
        operator._operator_warnings = [
            "GLSL Compile Error: input index is out of range"
        ]
        managed = helpers["_inspect_readiness_health"](
            root, runtime, 11.0, force=True
        )
        readiness = helpers["_application_readiness"](
            root, runtime, managed_health=managed
        )
        self.assertIn("managed_shader_compile_errors", readiness["reasons"])
        self.assertEqual(managed["shader_compile_error_count"], 1)
        self.assertIn(
            "Compile Error", managed["shader_compile_errors"][0]["messages"][0]
        )

    def test_readiness_reports_invalid_required_output_dimensions(self) -> None:
        helpers = load_helpers()
        root = complete_runtime_root()
        quiet_apply(helpers, root, {"role": "world", "experience": "combined"})
        helpers["tick"](root)
        helpers["tick"](root)
        runtime = root.fetch("_flexgpu_runtime")
        root.op("WORKING_PIPELINE/OUT_RIGHT_EYE").width = 0
        managed = helpers["_inspect_readiness_health"](
            root, runtime, 20.0, force=True
        )
        readiness = helpers["_application_readiness"](
            root, runtime, managed_health=managed
        )
        self.assertEqual(readiness["state"], "degraded")
        self.assertIn(
            "required_output_dimensions_invalid", readiness["reasons"]
        )
        self.assertEqual(
            managed["invalid_outputs"],
            [{
                "name": "right_eye",
                "path": "WORKING_PIPELINE/OUT_RIGHT_EYE",
                "width": 0,
                "height": 128,
                "valid": False,
                "problem": "dimensions_out_of_range",
            }],
        )

    def test_heartbeat_publishes_actionable_managed_health_failure(self) -> None:
        helpers = load_helpers()
        root = complete_runtime_root()
        with tempfile.TemporaryDirectory() as directory:
            heartbeat = Path(directory) / "heartbeat.json"
            env = {
                "FLEXGPU_SESSION_ID": "managed-health-session",
                "FLEXGPU_HEARTBEAT_PATH": str(heartbeat),
            }
            with mock.patch.dict(helpers["os"].environ, env, clear=False):
                quiet_apply(helpers, root, {"role": "world"})
                helpers["tick"](root)
                helpers["tick"](root)
                self.assertEqual(
                    json.loads(heartbeat.read_text(encoding="utf-8"))["state"],
                    "ready",
                )
                temporal = root.op("WORKING_PIPELINE/TEMPORAL_WORLD")
                temporal._operator_warnings = [
                    "GLSL compile error: sampler expects another input"
                ]
                helpers["_write_heartbeat"](
                    root, root.fetch("_flexgpu_runtime"), force=True
                )
            payload = json.loads(heartbeat.read_text(encoding="utf-8"))
        self.assertEqual(payload["state"], "degraded")
        self.assertFalse(payload["readiness"]["ready"])
        self.assertIn(
            "managed_shader_compile_errors", payload["readiness"]["reasons"]
        )
        managed = payload["readiness"]["managed_health"]
        self.assertEqual(managed["shader_compile_error_count"], 1)
        self.assertEqual(
            managed["shader_compile_errors"][0]["path"], "TEMPORAL_WORLD"
        )

    def test_readiness_health_scan_is_cached_and_operator_bounded(self) -> None:
        helpers = load_helpers()
        root = complete_runtime_root()
        quiet_apply(helpers, root, {"role": "world"})
        runtime = root.fetch("_flexgpu_runtime")
        watched = root.op("WORKING_PIPELINE/RECONSTRUCTION")
        first = helpers["_inspect_readiness_health"](
            root, runtime, 30.0, force=True
        )
        error_inspections = watched.error_inspections
        second = helpers["_inspect_readiness_health"](
            root, runtime, 30.1
        )
        self.assertIs(second, first)
        self.assertEqual(watched.error_inspections, error_inspections)
        helpers["_inspect_readiness_health"](root, runtime, 30.6)
        self.assertEqual(watched.error_inspections, error_inspections + 1)

        large_root = FakeRoot({})
        large_root.children = [
            FakeNode("/project1/flexgpu/node%d" % index)
            for index in range(600)
        ]
        nodes, truncated = helpers["_bounded_managed_nodes"](large_root)
        self.assertEqual(len(nodes), 512)
        self.assertTrue(truncated)

    def test_readiness_requires_an_observed_output_cook_advance_when_available(self) -> None:
        helpers = load_helpers()
        root = complete_runtime_root()
        output = FakeCookNode("OUT_INSTALLATION")
        root.nodes["WORKING_PIPELINE/OUT_INSTALLATION"] = output
        quiet_apply(helpers, root, {"role": "world"})
        helpers["tick"](root)
        first = helpers["_application_readiness"](
            root, root.fetch("_flexgpu_runtime")
        )
        self.assertFalse(first["ready"])
        self.assertIn("output_not_advancing", first["reasons"])
        helpers["tick"](root)
        second = helpers["_application_readiness"](
            root, root.fetch("_flexgpu_runtime")
        )
        self.assertTrue(second["ready"])
        self.assertGreaterEqual(second["output_advances"], 1)
        self.assertGreaterEqual(output.cookAbsFrame, 2)

    def test_readiness_requires_every_active_output_to_advance(self) -> None:
        helpers = load_helpers()
        root = complete_runtime_root()
        root.nodes["WORKING_PIPELINE/OUT_RIGHT_EYE"] = FakeStaticCookNode(
            "OUT_RIGHT_EYE"
        )
        quiet_apply(
            helpers, root,
            {"role": "world", "experience": "combined"},
        )
        helpers["tick"](root)
        helpers["tick"](root)
        readiness = helpers["_application_readiness"](
            root, root.fetch("_flexgpu_runtime")
        )
        self.assertFalse(readiness["ready"])
        self.assertIn("output_not_advancing", readiness["reasons"])
        required = {
            item["name"]: item
            for item in readiness["required_output_progress"]
        }
        self.assertGreaterEqual(required["installation"]["advances"], 1)
        self.assertEqual(required["right_eye"]["advances"], 0)

    def test_heartbeat_binds_expected_build_and_effective_config_identity(self) -> None:
        helpers = load_helpers()
        root = complete_runtime_root()
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "show.json"
            raw = {"role": "world", "topology": "single", "tier": "3080ti_16gb"}
            config.write_text(json.dumps(raw, indent=2), encoding="utf-8")
            canonical = json.dumps(
                raw, sort_keys=True, separators=(",", ":"),
                ensure_ascii=False, allow_nan=False,
            ).encode("utf-8")
            expected_id = __import__("hashlib").sha256(canonical).hexdigest()
            heartbeat = Path(directory) / "heartbeat.json"
            env = {
                "FLEXGPU_CONFIG": str(config),
                "FLEXGPU_SESSION_ID": "identity-session",
                "FLEXGPU_HEARTBEAT_PATH": str(heartbeat),
                "FLEXGPU_HEARTBEAT_TIMEOUT_MS": "3000",
                "FLEXGPU_EXPECTED_BUILD_VERSION": "1.2.1",
                "FLEXGPU_CONFIG_ID": expected_id,
            }
            with mock.patch.dict(helpers["os"].environ, env, clear=False):
                quiet_apply(helpers, root, None)
                helpers["tick"](root)
                helpers["tick"](root)
            payload = json.loads(heartbeat.read_text(encoding="utf-8"))
            self.assertEqual(payload["state"], "ready")
            self.assertEqual(payload["config"]["identity"], expected_id)
            self.assertEqual(
                payload["config"]["identity_kind"],
                "supervisor_effective_config",
            )
            self.assertEqual(payload["config"]["file_identity"], expected_id)
            self.assertEqual(
                payload["config"]["file_identity_kind"], "canonical_config_raw"
            )
            self.assertTrue(payload["config"]["file_matches_effective"])
            self.assertTrue(payload["build"]["matches_expected"])
            self.assertTrue(payload["config"]["matches_expected"])
            self.assertNotIn(str(config), json.dumps(payload))

    def test_heartbeat_retains_tomllib_file_identity_diagnostics(self) -> None:
        helpers = load_helpers()
        root = complete_runtime_root()
        raw = {
            "role": "world",
            "topology": "single",
            "tier": "3080ti_16gb",
            "source": {"mode": "demo"},
        }
        canonical = json.dumps(
            raw, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        ).encode("utf-8")
        expected_id = __import__("hashlib").sha256(canonical).hexdigest()
        fake_tomllib = types.ModuleType("tomllib")
        loads: list[str] = []

        def load_toml(handle):
            loads.append(handle.mode)
            return dict(raw)

        fake_tomllib.load = load_toml
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "show.toml"
            config.write_text(
                'role = "world"\ntopology = "single"\ntier = "3080ti_16gb"\n',
                encoding="utf-8",
            )
            heartbeat = Path(directory) / "heartbeat.json"
            env = {
                "FLEXGPU_CONFIG": str(config),
                "FLEXGPU_SESSION_ID": "toml-identity-session",
                "FLEXGPU_HEARTBEAT_PATH": str(heartbeat),
                "FLEXGPU_EXPECTED_BUILD_VERSION": "1.2.1",
                "FLEXGPU_CONFIG_ID": expected_id,
            }
            with mock.patch.dict(sys.modules, {"tomllib": fake_tomllib}), \
                    mock.patch.dict(helpers["os"].environ, env, clear=False):
                quiet_apply(helpers, root, None)
                helpers["tick"](root)
                helpers["tick"](root)
            payload = json.loads(heartbeat.read_text(encoding="utf-8"))
        self.assertEqual(payload["state"], "ready")
        self.assertEqual(payload["config"]["identity"], expected_id)
        self.assertEqual(
            payload["config"]["identity_kind"], "supervisor_effective_config"
        )
        self.assertEqual(payload["config"]["file_identity"], expected_id)
        self.assertEqual(
            payload["config"]["file_identity_kind"], "canonical_config_raw"
        )
        self.assertTrue(payload["config"]["matches_expected"])
        self.assertGreaterEqual(loads.count("rb"), 2)

    def test_heartbeat_uses_supervisor_identity_when_cli_overrides_change_file(self) -> None:
        helpers = load_helpers()
        root = complete_runtime_root()
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "show.json"
            file_raw = {
                "topology": "single",
                "experience": "installation",
                "completion": "fog",
                "tier": "3080ti_16gb",
            }
            effective_raw = dict(file_raw)
            effective_raw.update(
                {
                    "experience": "vr",
                    "completion": "procedural",
                    "tier": "4090",
                }
            )
            config.write_text(json.dumps(file_raw, indent=2), encoding="utf-8")
            canonical_file = json.dumps(
                file_raw,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
            canonical_effective = json.dumps(
                effective_raw,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
            hashlib = __import__("hashlib")
            file_id = hashlib.sha256(canonical_file).hexdigest()
            effective_id = hashlib.sha256(canonical_effective).hexdigest()
            self.assertNotEqual(file_id, effective_id)
            heartbeat = Path(directory) / "heartbeat.json"
            env = {
                "FLEXGPU_CONFIG": str(config),
                "FLEXGPU_SESSION_ID": "override-identity-session",
                "FLEXGPU_HEARTBEAT_PATH": str(heartbeat),
                "FLEXGPU_EXPECTED_BUILD_VERSION": "1.2.1",
                "FLEXGPU_CONFIG_ID": effective_id,
                "FLEXGPU_EXPERIENCE": "vr",
                "FLEXGPU_COMPLETION": "procedural",
                "FLEXGPU_TIER": "4090",
            }
            with mock.patch.dict(helpers["os"].environ, env, clear=False):
                quiet_apply(helpers, root, None)
                helpers["tick"](root)
                helpers["tick"](root)
            payload = json.loads(heartbeat.read_text(encoding="utf-8"))
        self.assertEqual(payload["state"], "ready")
        self.assertEqual(payload["config"]["identity"], effective_id)
        self.assertEqual(
            payload["config"]["identity_kind"], "supervisor_effective_config"
        )
        self.assertTrue(payload["config"]["matches_expected"])
        self.assertEqual(payload["config"]["file_identity"], file_id)
        self.assertFalse(payload["config"]["file_matches_effective"])

    def test_heartbeat_never_becomes_ready_on_expected_identity_mismatch(self) -> None:
        helpers = load_helpers()
        root = complete_runtime_root()
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "show.json"
            config.write_text("{}\n", encoding="utf-8")
            heartbeat = Path(directory) / "heartbeat.json"
            env = {
                "FLEXGPU_CONFIG": str(config),
                "FLEXGPU_SESSION_ID": "mismatch-session",
                "FLEXGPU_HEARTBEAT_PATH": str(heartbeat),
                "FLEXGPU_EXPECTED_BUILD_VERSION": "9.9.9",
                "FLEXGPU_CONFIG_ID": "not-a-sha256",
            }
            with mock.patch.dict(helpers["os"].environ, env, clear=False):
                quiet_apply(helpers, root, None)
                helpers["tick"](root)
                helpers["tick"](root)
                helpers["_write_heartbeat"](
                    root, root.fetch("_flexgpu_runtime"), force=True
                )
            payload = json.loads(heartbeat.read_text(encoding="utf-8"))
            self.assertEqual(payload["state"], "degraded")
            self.assertFalse(payload["readiness"]["ready"])
            self.assertIn(
                "build_identity_mismatch", payload["readiness"]["reasons"]
            )
            self.assertIn(
                "config_identity_mismatch", payload["readiness"]["reasons"]
            )

    def test_health_uses_configured_source_stale_timeout(self) -> None:
        helpers = load_helpers()
        root = complete_runtime_root()
        quiet_apply(
            helpers,
            root,
            {"source": {"mode": "demo", "stale_timeout_ms": 500}},
        )
        root.op("WORKING_PIPELINE/SOURCES").par.Sourceagems.val = 600
        runtime = root.fetch("_flexgpu_runtime")
        health = helpers["_health_snapshot"](root, runtime, 16.0)
        self.assertIn("source_stale", health["warnings"])

    def test_runtime_builder_exposes_marked_completion_controls(self) -> None:
        source = (ROOT / "touchdesigner" / "runtime_pipeline.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("FLEXGPU_FOG_DENSITY", source)
        self.assertIn("FLEXGPU_PROCEDURAL_MIX", source)
        self.assertIn('"Float", "Fogdensity", 0.35', source)
        self.assertIn('"Float", "Proceduralmix", 0.72', source)
        self.assertIn("fogBase * max(0.0, fogDensity)", source)
        self.assertIn("clamp(proceduralMix, 0.0, 1.0)", source)

    def test_runtime_builder_preserves_held_activity_without_recursive_decay(self) -> None:
        source = (ROOT / "touchdesigner" / "runtime_pipeline.py").read_text(
            encoding="utf-8"
        )
        shader = source.split('"temporal_persistence":', 1)[1].split("''',", 1)[0]
        self.assertIn("state.r is the absolute confidence", shader)
        self.assertIn("carriedActivity = min(history.a, state.r)", shader)
        self.assertIn("max(currentActivity, carriedActivity)", shader)
        self.assertNotIn("history.a * state.r", shader)


if __name__ == "__main__":
    unittest.main()
