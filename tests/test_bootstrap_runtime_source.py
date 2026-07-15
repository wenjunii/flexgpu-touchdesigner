from __future__ import annotations

import ast
import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "touchdesigner" / "bootstrap_project.py"


def load_bootstrap():
    spec = importlib.util.spec_from_file_location("bootstrap_project", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load bootstrap_project.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BootstrapRuntimeSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_bootstrap()
        cls.source = MODULE_PATH.read_text(encoding="utf-8")
        cls.runtime = cls.module.RUNTIME_HELPERS
        cls.callbacks = cls.module.STARTUP_CALLBACKS

    def test_source_and_embedded_runtime_parse_without_touchdesigner(self) -> None:
        ast.parse(self.source)
        ast.parse(self.runtime)
        ast.parse(self.callbacks)
        self.assertEqual(self.module.BUILD_VERSION, "1.1.0")

    def test_transport_config_and_environment_keys_are_embedded(self) -> None:
        for name in (
            "FLEXGPU_TRANSPORT", "FLEXGPU_TRANSPORT_SEGMENT",
            "FLEXGPU_PEER_HOST", "FLEXGPU_ATLAS_PORT",
            "FLEXGPU_ATLAS_WIDTH", "FLEXGPU_ATLAS_HEIGHT",
            "FLEXGPU_TRANSPORT_FPS",
        ):
            self.assertIn(name, self.runtime)
        for key in (
            "segment_name", "peer_host", "atlas_width", "atlas_height",
            "atlas_port", "atlas_fps",
        ):
            self.assertIn(key, self.runtime)

    def test_split_roles_gate_every_heavy_stage_and_endpoint_pair(self) -> None:
        for stage in (
            "SOURCES", "RECONSTRUCTION", "SENSOR_INTERACTION",
            "TEMPORAL_WORLD", "COMPLETION", "RENDER_CONTRACT",
            "POINT_RENDER", "INSTALLATION_OUTPUT", "STEREO_PREVIEW",
        ):
            self.assertIn(stage, self.runtime)
        for endpoint in (
            "RX_SHARED_ATLAS", "TX_SHARED_ATLAS",
            "RX_TCP_ATLAS", "TX_TCP_ATLAS",
        ):
            self.assertIn(endpoint, self.runtime)
        self.assertIn("transport_sender_active", self.runtime)
        self.assertIn("transport_receiver_active", self.runtime)
        self.assertIn("bridge_route_index", self.runtime)
        self.assertIn("atlas_route_index", self.runtime)
        self.assertIn("transport_endpoint_active", self.runtime)
        self.assertIn("allowCooking is writable only on COMPs", self.runtime)
        self.assertIn("Active expressions", self.runtime)

    def test_shared_sender_is_force_cooked_from_frame_start(self) -> None:
        self.assertIn("def _transport_tick", self.runtime)
        self.assertIn("node.cook(force=True)", self.runtime)
        self.assertIn("_set(node, 'active', True)", self.runtime)
        self.assertIn("_set(node, 'active', False)", self.runtime)
        self.assertIn("_transport_tick(root_comp, runtime, True)", self.runtime)
        self.assertIn("_transport_tick(root_comp, runtime)", self.runtime)
        self.assertIn("module_dat.module.tick(root_comp)", self.callbacks)
        self.assertIn("def onFrameStart(frame)", self.callbacks)

    def test_transport_cadence_uses_global_cook_rate_and_send_step(self) -> None:
        self.assertIn("project.cookRate", self.runtime)
        self.assertIn("transport_send_step", self.runtime)
        self.assertIn("transport_effective_fps", self.runtime)
        self.assertIn("_set(bridge, 'Sendstep', send_step)", self.runtime)

    def test_manual_streamdiffusion_and_sensor_state_is_preserved_without_config(self) -> None:
        self.assertIn("if 'source' in state:", self.runtime)
        self.assertIn("if 'depth_operator' in source:", self.runtime)
        self.assertIn("if 'sensor' in state:", self.runtime)

    def test_custom_tier_is_available_in_dashboard_and_contract(self) -> None:
        self.assertIn('("3080ti_16gb", "4090", "5090", "custom")', self.source)
        self.assertIn('3080ti_16gb|4090|5090|custom', self.source)


if __name__ == "__main__":
    unittest.main()
