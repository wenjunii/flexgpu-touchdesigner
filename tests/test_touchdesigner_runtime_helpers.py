from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import tempfile
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
    def __init__(self, path: str, **parameters: object) -> None:
        self.path = path
        self.par = FakeParameters(parameters)
        self.allowCooking = True

    def pars(self):
        return list(self.par._parameters.values())


class FakeTextNode(FakeNode):
    def __init__(self, path: str, text: str) -> None:
        super().__init__(path)
        self.text = text


class FakeRoot(FakeNode):
    def __init__(self, nodes: dict[str, FakeNode]) -> None:
        super().__init__("/project1/flexgpu")
        self.nodes = nodes
        self.storage: dict[str, object] = {}

    def op(self, path: str):
        return self.nodes.get(path)

    def store(self, key: str, value: object) -> None:
        self.storage[key] = value

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


def quiet_apply(helpers, root, overrides):
    with contextlib.redirect_stdout(io.StringIO()):
        return helpers["apply"](root, overrides)


def complete_runtime_root() -> FakeRoot:
    nodes: dict[str, FakeNode] = {
        "WORKING_PIPELINE": FakeNode("WORKING_PIPELINE"),
        "WORKING_PIPELINE/SOURCES": FakeNode(
            "SOURCES", UseStreamDiffusion=False, UseExternalDepth=False
        ),
        "WORKING_PIPELINE/SOURCES/STREAMDIFFUSION_ADAPTER": FakeNode(
            "STREAMDIFFUSION_ADAPTER", Enabled=False
        ),
        "WORKING_PIPELINE/RECONSTRUCTION": FakeNode(
            "RECONSTRUCTION", Geometryresolution=384
        ),
        "WORKING_PIPELINE/POINT_RENDER": FakeNode(
            "POINT_RENDER", Maxpoints=120000, Pointsize=3.0
        ),
        "WORKING_PIPELINE/COMPLETION": FakeNode(
            "COMPLETION", Mode="hybrid", Fogdensity=0.35, Proceduralmix=0.72
        ),
        "WORKING_PIPELINE/SENSOR_INTERACTION": FakeNode(
            "SENSOR_INTERACTION", Mode="simulated"
        ),
        "WORKING_PIPELINE/SENSOR_INTERACTION/SIMULATED_SENSOR_MASK": FakeNode(
            "SIMULATED_SENSOR_MASK", radius=0.16
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
        "WORKING_PIPELINE/SOURCES/DEMO_RGB_GENERATOR",
        "WORKING_PIPELINE/SOURCES/STREAMDIFFUSION_ADAPTER/REPLACE_WITH_STREAMDIFFUSION_RGB",
        "WORKING_PIPELINE/SOURCES/DEMO_DEPTH_GENERATOR",
        "WORKING_PIPELINE/SOURCES/STREAMDIFFUSION_ADAPTER/REPLACE_WITH_DEPTH_ESTIMATE",
        "WORKING_PIPELINE/POINT_RENDER/RENDER_CENTER",
        "WORKING_PIPELINE/INSTALLATION_OUTPUT/installation_grade",
        "WORKING_PIPELINE/POINT_RENDER/RENDER_LEFT_EYE",
        "WORKING_PIPELINE/POINT_RENDER/RENDER_RIGHT_EYE",
        "WORKING_PIPELINE/STEREO_PREVIEW/STEREO_SIDE_BY_SIDE",
    ):
        nodes[path] = resolution_node(path)
    nodes["WORKING_PIPELINE/COMPLETION/fog_completion_PIXEL"] = FakeTextNode(
        "fog_completion_PIXEL",
        "const float fogDensity = 0.35; // FLEXGPU_FOG_DENSITY\n",
    )
    nodes["WORKING_PIPELINE/COMPLETION/hybrid_completion_PIXEL"] = FakeTextNode(
        "hybrid_completion_PIXEL",
        "const float proceduralMix = 0.72; // FLEXGPU_PROCEDURAL_MIX\n",
    )
    return FakeRoot(nodes)


class TouchDesignerRuntimeHelperTests(unittest.TestCase):
    def test_apply_binds_adaptive_quality_to_real_pipeline_nodes(self) -> None:
        helpers = load_helpers()
        root = complete_runtime_root()
        state = quiet_apply(
            helpers,
            root,
            {
                "role": "world",
                "topology": "single",
                "experience": "combined",
                "tier": "4090",
                "source": {"mode": "streamdiffusion", "depth_operator": "out_depth"},
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
        self.assertEqual(state["point_budget"], 100000)
        self.assertEqual(
            root.op("WORKING_PIPELINE/RECONSTRUCTION").par.Geometryresolution.val,
            256,
        )
        point_render = root.op("WORKING_PIPELINE/POINT_RENDER")
        self.assertEqual(point_render.par.Maxpoints.val, 100000)
        self.assertEqual(point_render.par.Pointsize.val, 5.5)
        self.assertEqual(
            root.op("WORKING_PIPELINE/SOURCES").par.UseStreamDiffusion.val, True
        )
        self.assertEqual(
            root.op("WORKING_PIPELINE/SOURCES").par.UseExternalDepth.val, True
        )
        self.assertEqual(
            root.op("WORKING_PIPELINE/SENSOR_INTERACTION/SIMULATED_SENSOR_MASK").par.radius.val,
            0.0,
        )
        center = root.op("WORKING_PIPELINE/POINT_RENDER/RENDER_CENTER")
        left = root.op("WORKING_PIPELINE/POINT_RENDER/RENDER_LEFT_EYE")
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
                root.op("WORKING_PIPELINE/POINT_RENDER").par.Maxpoints.val, 175000
            )
            self.assertTrue(jsonl.is_file())
            record = json.loads(jsonl.read_text(encoding="utf-8").strip())
            self.assertEqual(record["adaptive_level"], 1)
            self.assertEqual(record["settings"]["point_budget"], 175000)

            helpers["flush_telemetry"](root, True)
            payload = json.loads(summary.read_text(encoding="utf-8"))
            self.assertEqual(payload["samples"], 1)
            self.assertEqual(payload["final_level"], 1)

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


if __name__ == "__main__":
    unittest.main()
