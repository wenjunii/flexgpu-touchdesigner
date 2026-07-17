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
        self.assertEqual(self.module.BUILD_VERSION, "1.2.1")
        self.assertIn("RUNTIME_BUILD_VERSION = '1.2.1'", self.runtime)

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

    def test_frame_lifecycle_and_heartbeat_are_driven_at_frame_start(self) -> None:
        for marker in (
            "def _validate_frame_state", "def _accept_explicit_frame",
            "out_of_order_rejected", "retired_session_rejected",
            "legacy_each_cook", "operator_cook_frame",
            "def _sample_frame_lifecycle", "Deltaseconds",
        ):
            self.assertIn(marker, self.runtime)
        for marker in (
            "FLEXGPU_SESSION_ID", "FLEXGPU_HEARTBEAT_PATH",
            "FLEXGPU_HEARTBEAT_TIMEOUT_MS", "HEARTBEAT_VERSION = 1",
            "FLEXGPU_EXPECTED_BUILD_VERSION", "FLEXGPU_CONFIG_ID",
            "os.replace(temporary, path)", "def _write_heartbeat",
        ):
            self.assertIn(marker, self.runtime)
        self.assertIn("_write_heartbeat(root_comp, runtime, health)", self.runtime)
        for marker in (
            "def _heartbeat_config_identity", "canonical_config_raw",
            "supervisor_effective_config", "config_file_matches_effective",
            "def _application_readiness", "cook_not_advancing",
            "source_not_accepted", "output_not_advancing",
            "build_identity_mismatch", "config_identity_mismatch",
            "def _update_readiness_progress",
            "def _inspect_readiness_health", "READINESS_MANAGED_OPERATOR_LIMIT",
            "def _readiness_external_tox_path",
            "node is not root_comp and _readiness_external_tox_path(node)",
            "managed_operator_errors", "managed_shader_compile_errors",
            "required_output_dimensions_invalid", "managed_health",
            "def _required_readiness_outputs", "required_output_progress",
            "outputs_not_advancing",
        ):
            self.assertIn(marker, self.runtime)

    def test_camera_metadata_is_frame_bound_before_temporal_signature(self) -> None:
        for marker in (
            "CAMERA_METADATA_VERSION = 'flexgpu-camera-metadata/v1'",
            "def _validate_camera_metadata",
            "def _sample_source_camera_metadata",
            "camera calibration drift is forbidden within a source session",
        ):
            self.assertIn(marker, self.runtime)
        lifecycle = self.runtime.index(
            "    _sample_frame_lifecycle(root_comp, runtime, now_ns, now)"
        )
        camera = self.runtime.index(
            "    camera_contract_accepted = _sample_source_camera_metadata"
        )
        calibration = self.runtime.index(
            "        _apply_calibrated_contracts(root_comp, runtime['state'])",
            camera,
        )
        signature = self.runtime.index(
            "    signature_changed = _check_temporal_signature", camera
        )
        self.assertLess(lifecycle, camera)
        self.assertLess(camera, calibration)
        self.assertLess(calibration, signature)

    def test_source_and_sensor_calibration_identities_are_independent(self) -> None:
        for marker in (
            "def _calibration_targets",
            "def _calibration_identity",
            "state[target + '_calibration_id']",
            "state[target + '_calibration_digest']",
            "state['source_calibration_id'] = metadata['calibration_id']",
            "_calibration_identity(state, label)",
            "_load_calibration(root_comp, state, configured_path, label='shared')",
        ):
            self.assertIn(marker, self.runtime)
        dynamic = self.runtime.split("dynamic_camera_identity = (", 1)[1].split(
            "if expected_id", 1
        )[0]
        self.assertIn("source_config.get('calibration_path')", dynamic)
        self.assertNotIn("sensor_config", dynamic)
        camera_static = self.runtime.split(
            "static_identity = bool(source_config.get('calibration_path'))", 1
        )[1].split("_apply_camera_metadata_contract", 1)[0]
        self.assertNotIn("state.get('sensor')", camera_static)

    def test_explicit_sensor_identity_lock_precedes_temporal_signature(self) -> None:
        for marker in (
            "label == 'sensor' and previous_session == session_id",
            "calibration_drift_rejected",
            "def _publish_sensor_frame_identity",
            "sensor_frame_calibration_id",
            "sensor_frame_calibration_digest",
        ):
            self.assertIn(marker, self.runtime)
        publish_call = self.runtime.index(
            "    _publish_sensor_frame_identity(state, sensor)"
        )
        signature_call = self.runtime.index(
            "    signature_changed = _check_temporal_signature"
        )
        self.assertLess(publish_call, signature_call)
        accept = self.runtime.split("def _accept_explicit_frame", 1)[1].split(
            "def _operator_cook_token", 1
        )[0]
        self.assertIn("label == 'sensor'", accept)
        self.assertNotIn("label == 'source'", accept)

    def test_runtime_overrides_are_bounded_and_point_budget_matches_texture(self) -> None:
        self.assertIn("def _bounded_integer", self.runtime)
        self.assertIn("def _bounded_number", self.runtime)
        self.assertIn("state['geometry_resolution'] ** 2", self.runtime)
        self.assertIn("point_budget_adjustment", self.runtime)

    def test_builder_reuses_only_type_verified_nodes_and_parameter_failures_are_honest(self) -> None:
        class Report:
            def __init__(self) -> None:
                self.reused = []
                self.created = []
                self.warnings = []

            def warn(self, message) -> None:
                self.warnings.append(str(message))

        class Node:
            path = "/project1/flexgpu/CONFIG"

            def __init__(self, *, op_type=None, basic_type=None, family=None) -> None:
                if op_type is not None:
                    self.opType = op_type
                if basic_type is not None:
                    self.type = basic_type
                if family is not None:
                    self.family = family

        class Parent:
            path = "/project1/flexgpu"

            def __init__(self, child) -> None:
                self.child = child

            def op(self, name):
                return self.child

        compatible = Node(basic_type="base", family="COMP")
        report = Report()
        self.assertIs(
            self.module._ensure(Parent(compatible), "baseCOMP", "CONFIG", report),
            compatible,
        )
        self.assertEqual(report.reused, [compatible.path])

        incompatible = Node(op_type="nullTOP")
        with self.assertRaisesRegex(RuntimeError, "nullTOP; expected baseCOMP"):
            self.module._ensure(
                Parent(incompatible), "baseCOMP", "CONFIG", Report()
            )

        class RejectingParameter:
            name = "Value"

            @property
            def val(self):
                return None

            @val.setter
            def val(self, value):
                raise RuntimeError("read only")

        class Parameters:
            Value = RejectingParameter()

        parameter_node = type("ParameterNode", (), {"par": Parameters()})()
        self.assertFalse(self.module._set_par(parameter_node, "Value", 1))

    def test_bootstrap_contract_names_the_direct_transport_rgba32f(self) -> None:
        self.assertIn("atomic RGBA32F preview atlas", self.source)
        self.assertNotIn("atomic RGBA16F preview atlas", self.source)

    def test_safe_build_profile_never_embeds_private_runtime_fields(self) -> None:
        marker = "PRIVATE-MARKER-MUST-NOT-BE-EMBEDDED"
        profile = self.module._safe_build_profile(
            {
                "role": "world",
                "topology": "dual_local",
                "experience": "combined",
                "tier": "4090",
                "render": {"point_size_px": 5.0, "installation_width": 1920},
                "processes": {
                    "world": {
                        "executable": marker + ".exe",
                        "env": {"API_KEY": marker},
                    }
                },
                "source": {
                    "streamdiffusion_tox": marker + ".tox",
                    "rgb_operator": marker,
                },
                "sensor": {"adapter_tox": marker + "-sensor.tox"},
                "telemetry": {"jsonl_path": marker + ".jsonl"},
                "transport": {
                    "type": "touch_tcp",
                    "peer_host": marker,
                    "segment_name": marker,
                    "atlas_width": 1024,
                },
            }
        )
        encoded = __import__("json").dumps(profile, sort_keys=True)
        self.assertNotIn(marker, encoded)
        self.assertNotIn("processes", profile)
        self.assertNotIn("source", profile)
        self.assertNotIn("sensor", profile)
        self.assertNotIn("telemetry", profile)
        self.assertEqual(profile["render"]["point_size_px"], 5.0)
        self.assertEqual(profile["transport"]["type"], "touch_tcp")
        self.assertEqual(profile["transport"]["atlas_width"], 1024)

    def test_circle_repair_uses_real_parameters_and_zero_center_convention(self) -> None:
        class Parameter:
            def __init__(self, name):
                self.name = name
                self.val = None
                self.expr = None

        class Parameters:
            def __init__(self):
                self.values = {
                    name: Parameter(name) for name in (
                        "radiusx", "radiusy", "radiusunit", "centerunit",
                        "centerx", "centery",
                    )
                }

            def __getattr__(self, name):
                try:
                    return self.values[name]
                except KeyError as exc:
                    raise AttributeError(name) from exc

        class Circle:
            def __init__(self):
                self.par = Parameters()

            def pars(self):
                return list(self.par.values.values())

        class Pipeline:
            def __init__(self, circle):
                self.circle = circle

            def op(self, path):
                return self.circle if path.endswith("SIMULATED_SENSOR_MASK") else None

        class Report:
            def __init__(self):
                self.warnings = []

            def warn(self, message):
                self.warnings.append(str(message))

        circle = Circle()
        report = Report()
        self.module._configure_simulated_sensor_circle(Pipeline(circle), report)
        self.assertEqual(circle.par.radiusx.val, 0.16)
        self.assertEqual(circle.par.radiusy.val, 0.16)
        self.assertEqual(circle.par.centerx.expr,
                         "0.24 * math.sin(absTime.seconds * 0.73)")
        self.assertEqual(circle.par.centery.expr,
                         "0.18 * math.cos(absTime.seconds * 0.91)")
        self.assertFalse(report.warnings)

    def test_build_isolates_environment_and_omits_config_path(self) -> None:
        self.assertIn("inherit_environment=False", self.source)
        self.assertIn("_safe_build_profile(config)", self.source)
        self.assertIn("<explicit runtime profile; path omitted>", self.source)
        self.assertNotIn("_flatten(config)", self.source)
        self.assertIn("storeStartupValue('_flexgpu_runtime', None)", self.runtime)
        self.assertIn("storeStartupValue('runtime_state', {})", self.runtime)


if __name__ == "__main__":
    unittest.main()
