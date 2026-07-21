from __future__ import annotations

import ast
import importlib.util
import inspect
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "touchdesigner" / "runtime_pipeline.py"


def load_runtime_pipeline():
    spec = importlib.util.spec_from_file_location("runtime_pipeline", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load runtime_pipeline.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RuntimePipelineSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_runtime_pipeline()
        cls.source = MODULE_PATH.read_text(encoding="utf-8")

    def test_module_imports_without_touchdesigner_and_has_no_import_side_effect(self) -> None:
        self.assertEqual(self.module.PIPELINE_NAME, "WORKING_PIPELINE")
        self.assertIsNone(self.module.LAST_REPORT)
        ast.parse(self.source)

    def test_embedded_bridge_runtimes_bind_public_src_for_cold_reopen(self) -> None:
        class Report:
            def __init__(self) -> None:
                self.warnings = []

            def warn(self, message) -> None:
                self.warnings.append(str(message))

        src_path = os.path.abspath(ROOT / "src")
        for source_loader, module_name in (
            (self.module._moge2_runtime_source, "embedded_moge2_cold"),
            (
                self.module._depth_anything_runtime_source,
                "embedded_depth_anything_cold",
            ),
        ):
            with self.subTest(module=module_name):
                report = Report()
                embedded_source = source_loader(report)
                self.assertEqual(report.warnings, [])
                self.assertIn(
                    "_EMBEDDED_FLEXGPU_SRC = %r" % src_path,
                    embedded_source,
                )
                with tempfile.TemporaryDirectory() as directory:
                    runtime_path = Path(directory) / (module_name + ".py")
                    runtime_path.write_text(embedded_source, encoding="utf-8")
                    command = f"""
import os, pathlib, sys, types
repo = pathlib.Path({str(ROOT)!r})
runtime_path = pathlib.Path({str(runtime_path)!r})
def normalized(value):
    return os.path.normcase(os.path.abspath(os.fspath(value)))
for key in ('FLEXGPU_CONFIG', 'FLEXGPU_ROOT', 'FLEXGPU_SRC'):
    os.environ.pop(key, None)
for name in tuple(sys.modules):
    if name == 'flexgpu' or name.startswith('flexgpu.'):
        sys.modules.pop(name, None)
blocked = {{
    normalized(repo),
    normalized(repo / 'src'),
    normalized(repo / 'touchdesigner'),
}}
sys.path[:] = [
    entry for entry in sys.path
    if not isinstance(entry, str) or normalized(entry) not in blocked
]
module = types.ModuleType({module_name!r})
module.__file__ = '/project1/flexgpu/WORKING_PIPELINE/runtime'
sys.modules[module.__name__] = module
source = runtime_path.read_text(encoding='utf-8')
exec(compile(source, module.__file__, 'exec'), module.__dict__)
assert 'flexgpu.worldbus' in sys.modules
assert normalized({src_path!r}) in {{
    normalized(entry) for entry in sys.path if isinstance(entry, str)
}}
"""
                    completed = subprocess.run(
                        [sys.executable, "-S", "-c", command],
                        cwd=directory,
                        check=False,
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_shader_names_are_stable_and_complete(self) -> None:
        expected = {
            "point_glyph",
            "validity_combine",
            "depth_to_position",
            "sensor_position",
            "sensor_to_world",
            "sensor_validity",
            "interaction_field",
            "interaction_smoothing",
            "interaction_debug",
            "temporal_observation",
            "temporal_state",
            "temporal_advect",
            "temporal_persistence",
            "temporal_color",
            "fog_completion",
            "procedural_backfill",
            "procedural_color",
            "hybrid_completion",
            "installation_grade",
            "panoramic_coverage",
            "view_completion",
            "transport_pack_geometry",
            "transport_pack_atlas",
            "transport_unpack_rgb",
            "transport_unpack_depth",
            "transport_unpack_confidence",
            "transport_unpack_mask",
            "moge2_unpack_rgb",
            "moge2_unpack_depth",
            "moge2_unpack_confidence",
            "moge2_unpack_mask",
            "depth_anything_sensor_position",
            "depth_anything_sensor_mask",
            "depth_anything_sensor_confidence",
        }
        self.assertEqual(set(self.module.SHADERS), expected)

    def test_stock_glsl_tops_never_exceed_three_wired_inputs(self) -> None:
        tree = ast.parse(self.source)
        glsl_calls = [
            node for node in ast.walk(tree)
            if (isinstance(node, ast.Call) and
                isinstance(node.func, ast.Name) and node.func.id == "_glsl")
        ]
        self.assertTrue(glsl_calls)
        for call in glsl_calls:
            inputs = call.args[3]
            with self.subTest(line=call.lineno):
                self.assertIsInstance(inputs, (ast.List, ast.Tuple))
                self.assertLessEqual(len(inputs.elts), 3)
        for name, shader in self.module.SHADERS.items():
            indices = [
                int(value) for value in re.findall(r"sTD2DInputs\[(\d+)\]", shader)
            ]
            with self.subTest(shader=name):
                if name == "point_glyph":
                    self.assertFalse(indices)
                    continue
                self.assertTrue(indices)
                self.assertLessEqual(max(indices), 2)
        self.assertIn("exceeds the three-input limit", self.source)

    def test_glsl_upgrade_replaces_old_wires_and_clears_surplus_inputs(self) -> None:
        class FakeShaderNode:
            path = "/project1/flexgpu/old_shader"

            def __init__(self) -> None:
                self.inputs = ["old-0", "old-1", "old-2", "old-3", "old-4"]
                self.inputConnectors = [object() for _ in self.inputs]

            def setInput(self, index, source, *unused) -> None:
                self.inputs[index] = source

        node = FakeShaderNode()
        source_dat = type("SourceDat", (), {"path": "/shader/pixel"})()
        with mock.patch.object(self.module, "_text", return_value=source_dat), \
                mock.patch.object(self.module, "_ensure", return_value=node), \
                mock.patch.object(self.module, "_set", return_value=True):
            result = self.module._glsl(
                object(), "MIGRATED_SHADER", "transport_pack_atlas",
                ["new-0", "new-1"], None,
            )
        self.assertIs(result, node)
        self.assertEqual(node.inputs, ["new-0", "new-1", None, None, None])
        with self.assertRaisesRegex(ValueError, "three-input limit"):
            self.module._glsl(
                object(), "INVALID_SHADER", "transport_pack_atlas",
                [1, 2, 3, 4], None,
            )

    def test_every_shader_declares_output_contract_and_touchdesigner_swizzle(self) -> None:
        for name, shader in self.module.SHADERS.items():
            with self.subTest(shader=name):
                self.assertIn("// CONTRACT:", shader)
                self.assertIn("out vec4 fragColor;", shader)
                self.assertIn("void main()", shader)
                self.assertIn("TDOutputSwizzle", shader)
                if name == "point_glyph":
                    self.assertNotIn("sTD2DInputs", shader)
                else:
                    self.assertIn("sTD2DInputs", shader)

    def test_position_and_persistence_contracts_keep_active_alpha(self) -> None:
        depth = self.module.SHADERS["depth_to_position"]
        temporal = self.module.SHADERS["temporal_persistence"]
        advect = self.module.SHADERS["temporal_advect"]
        self.assertIn("vec4(worldPosition, valid * confidence)", depth)
        self.assertIn("world XYZ metres + active alpha", depth)
        self.assertIn("(uv.y - cyNormalized) * z / fy", depth)
        self.assertNotIn("-(uv.y - cyNormalized) * z / fy", depth)
        backfill = self.module.SHADERS["procedural_backfill"]
        self.assertIn("(uv.y - 0.5) * generatedDepth / generatedFocal", backfill)
        self.assertIn("float generatedActive = 1.0", backfill)
        self.assertIn("POSITION + ADVECTED_HISTORY + TEMPORAL_STATE", temporal)
        self.assertIn("state.r", temporal)
        self.assertIn("interaction.rgb", advect)
        self.assertIn("motionDt", advect)
        self.assertIn("float carriedActivity = min(history.a, state.r);", temporal)
        self.assertIn("max(currentActivity, carriedActivity)", temporal)
        self.assertNotIn("history.a * state.r", temporal)
        self.assertIn("vec4(position, activity)", temporal)
        self.assertNotIn("float active =", temporal)
        self.assertIn('"resolutionTOP", "COLOR_ALIGNED_RESIZE"', self.source)
        self.assertIn('"Geometryresolution", 384', self.source)

    def test_held_frame_activity_uses_one_absolute_decay_envelope(self) -> None:
        """The shader contract must survive the 5-10 Hz inter-frame hold."""
        confidence_decay = 0.985
        render_delta = 1.0 / 60.0
        maximum_age = 2.0

        def hold(cooks: int) -> tuple[float, float]:
            state_confidence = 1.0
            activity = 1.0
            age = 0.0
            for _ in range(cooks):
                age = min(1.0, age + render_delta / maximum_age)
                retention = confidence_decay ** (render_delta * 60.0)
                state_confidence *= retention * (1.0 if age < 1.0 else 0.0)
                state_alive = 1.0 if state_confidence >= 0.001 else 0.0
                # Mirrors carriedActivity=min(history.a, state.r), not the old
                # recursive history.a*state.r feedback multiplication.
                activity = state_alive * min(activity, state_confidence)
            return state_confidence, activity

        for source_fps, held_cooks in ((10, 6), (5, 12)):
            with self.subTest(source_fps=source_fps):
                confidence, activity = hold(held_cooks)
                expected = confidence_decay ** held_cooks
                self.assertAlmostEqual(confidence, expected, places=12)
                self.assertAlmostEqual(activity, expected, places=12)
                self.assertGreater(activity, 0.8)

        # With decay disabled, the explicit maximum age remains authoritative.
        confidence_decay = 1.0
        self.assertGreater(hold(119)[1], 0.0)
        # Accumulated shader float error may put the cutoff one render cook
        # after the nominal boundary, but never another source-frame interval.
        self.assertEqual(hold(121)[1], 0.0)

    def test_calibrated_reconstruction_contract_has_safe_depth_modes(self) -> None:
        depth = self.module.SHADERS["depth_to_position"]
        for marker in (
            "FLEXGPU_DEPTH_MODE", "FLEXGPU_DEPTH_SCALE", "FLEXGPU_DEPTH_BIAS",
            "FLEXGPU_INTRINSICS_FX", "FLEXGPU_INTRINSICS_FY",
            "FLEXGPU_CAMERA_TO_WORLD_0", "FLEXGPU_CAMERA_TO_WORLD_3",
        ):
            self.assertIn(marker, depth)
        self.assertIn("depthMode == 1", depth)
        self.assertIn("depthMode == 2", depth)
        self.assertIn("calibrated >= 0.0 && calibrated <= 1.0", depth)
        self.assertIn("confidence > 0.0", depth)
        self.assertNotIn("calibrated > 0.002", depth)
        self.assertNotIn("calibrated < 0.998", depth)
        self.assertNotIn("rawDepth = max(0.0", depth)
        self.assertIn("CONFIDENCE_IN", self.source)
        self.assertIn("CONFIDENCE_ALIGNED_RESIZE", self.source)
        self.assertIn('"Depthmode", "normalized"', self.source)
        self.assertIn('"Cameratoworld0", "1 0 0 0"', self.source)

    def test_color_alignment_migrates_legacy_null_without_destroying_it(self) -> None:
        # 1.0.0 used a nullTOP named COLOR_ALIGNED. The new managed name keeps
        # that unknown legacy node intact while type-safe _ensure fails closed
        # on accidental type collisions for every currently managed name.
        self.assertIn(
            '_ensure(comp, "resolutionTOP", "COLOR_ALIGNED_RESIZE", report)',
            self.source,
        )
        self.assertNotIn(
            '_ensure(comp, "resolutionTOP", "COLOR_ALIGNED", report)',
            self.source,
        )
        self.assertIn(
            '_connect(color, position, 0, 0, report, replace=True)',
            self.source,
        )
        self.assertIn(
            '_connect(color, color_out, 0, 0, report, replace=True)',
            self.source,
        )
        self.assertNotIn('.destroy(', self.source)

    def test_ensure_reuses_only_a_verified_operator_type(self) -> None:
        class Report:
            def __init__(self) -> None:
                self.reused = []
                self.created = []
                self.warnings = []

            def warn(self, message) -> None:
                self.warnings.append(str(message))

        class Existing:
            path = "/project1/flexgpu/WORKING_PIPELINE"

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

            def create(self, *unused):
                raise AssertionError("existing operators must not be recreated")

        for child in (
            Existing(op_type="baseCOMP"),
            Existing(basic_type="base", family="COMP"),
        ):
            with self.subTest(actual=self.module._operator_type_name(child)):
                report = Report()
                self.assertIs(
                    self.module._ensure(
                        Parent(child), "baseCOMP", "WORKING_PIPELINE", report
                    ),
                    child,
                )
                self.assertEqual(report.reused, [child.path])

        wrong = Existing(op_type="nullTOP")
        with self.assertRaisesRegex(RuntimeError, "nullTOP; expected baseCOMP"):
            self.module._ensure(
                Parent(wrong), "baseCOMP", "WORKING_PIPELINE", Report()
            )
        report = Report()
        self.assertIsNone(
            self.module._ensure(
                Parent(wrong), "baseCOMP", "WORKING_PIPELINE", report,
                optional=True,
            )
        )
        self.assertEqual(len(report.warnings), 1)

    def test_completion_shaders_cover_requested_modes(self) -> None:
        fog = self.module.SHADERS["fog_completion"]
        procedural = self.module.SHADERS["procedural_backfill"]
        procedural_color = self.module.SHADERS["procedural_color"]
        hybrid = self.module.SHADERS["hybrid_completion"]
        self.assertIn("disocclusion", fog)
        self.assertIn("nearby expands point silhouettes", fog)
        self.assertIn("PROCEDURAL_POSITION", procedural)
        self.assertIn("generatedActive", procedural)
        self.assertNotIn("float active =", procedural)
        self.assertIn("POSITION + PROCEDURAL_POSITION + COLOR", procedural_color)
        self.assertIn("vec3 palette = source.rgb", procedural_color)
        self.assertNotIn("vec3(0.78, 0.30, 0.16)", procedural_color)
        self.assertNotIn("sTD2DInputs[3]", procedural_color)
        self.assertIn("FOG_COLOR + PROCEDURAL_COLOR", hybrid)

    def test_public_top_contracts_cover_render_sensor_installation_and_stereo(self) -> None:
        expected = {
            "RGB", "DEPTH", "POSITION", "COLOR", "SENSOR_POSITION",
            "CONFIDENCE", "TEMPORAL_STATE", "INTERACTION", "INSTALLATION",
            "TRIPLE_DISPLAY", "STEREO",
        }
        self.assertEqual(set(self.module.TOP_CONTRACTS), expected)
        self.assertIn("XYZ metres", self.module.TOP_CONTRACTS["POSITION"])
        self.assertIn("side-by-side", self.module.TOP_CONTRACTS["STEREO"])
        self.assertIn(
            "three RGBA surface TOPs",
            self.module.TOP_CONTRACTS["TRIPLE_DISPLAY"],
        )

    def test_sensor_interaction_uses_calibrated_world_positions(self) -> None:
        interaction = self.module.SHADERS["interaction_field"]
        self.assertIn("calibrated SENSOR_POSITION", interaction)
        self.assertIn("distanceMetres", interaction)
        self.assertIn("interactionRadiusMetres", interaction)
        self.assertIn('"Float", "Forcegain", 0.35', self.source)
        self.assertNotIn("vec2 radial", interaction)
        self.assertIn("occupancyGridSize = 8", interaction)
        self.assertIn("sensorUV", interaction)
        self.assertNotIn("texture(sTD2DInputs[1], uv)", interaction)
        interaction_debug = self.module.SHADERS["interaction_debug"]
        self.assertIn("display-only", interaction_debug)
        self.assertIn("interaction.a", interaction_debug)
        self.assertIn('"OUT_INTERACTION_DEBUG"', self.source)
        self.assertIn(
            "_install_interaction_debug_output(sensor, pipeline, report)",
            self.source,
        )
        validity = self.module.SHADERS["sensor_validity"]
        self.assertIn("sensor.a * mask * confidence", validity)
        self.assertIn("DEPTH_SENSOR_ADAPTER", self.source)
        self.assertIn("REPLACE_WITH_CALIBRATED_SENSOR_POSITION", self.source)
        self.assertIn("SENSOR_POSITION_SOURCE", self.source)

    def test_interaction_smoothing_is_low_latency_and_feedback_bounded(self) -> None:
        smoothing = self.module.SHADERS["interaction_smoothing"]
        self.assertIn("FLEXGPU_INTERACTION_SMOOTHING", smoothing)
        self.assertIn("attackBlend", smoothing)
        self.assertIn("releaseBlend", smoothing)
        self.assertIn("current.a >= history.a", smoothing)
        self.assertIn('"Interactionsmoothing", 0.35', self.source)
        self.assertIn('"INTERACTION_SMOOTH_HISTORY"', self.source)
        self.assertIn(
            '_set(history, ("targettop", "target", "top"), smoothed.path)',
            self.source,
        )
        self.assertIn(
            '_out_top(comp, "OUT_INTERACTION", interaction, 1, report)',
            self.source,
        )

    def test_depth_anything_data_constants_are_unpremultiplied(self) -> None:
        shader = self.module.SHADERS["depth_anything_sensor_position"]
        self.assertIn("Constant TOPs premultiply RGB by alpha", shader)
        self.assertIn(
            "depthCalibrationPacked.rgb / "
            "max(abs(depthCalibrationPacked.a), 1e-6)",
            shader,
        )
        self.assertIn(
            "intrinsicsPacked.rgb / max(abs(intrinsicsPacked.a), 1e-6)",
            shader,
        )

    def test_sensor_disabled_route_is_zero_and_circle_uses_documented_coordinates(self) -> None:
        self.assertIn(
            '("simulated", "replay", "depth_sensor", "disabled")',
            self.source,
        )
        self.assertIn('"DISABLED_SENSOR_ZERO"', self.source)
        self.assertIn(
            '_connect(disabled_zero, mask_switch, 3, 0, report, replace=True)',
            self.source,
        )
        self.assertIn(
            '_connect(disabled_zero, confidence_switch, 3, 0, report, replace=True)',
            self.source,
        )
        self.assertIn(
            '_connect(disabled_zero, sensor_position, 3, 0, report, replace=True)',
            self.source,
        )
        self.assertIn('_set(circle, "radiusx", 0.16)', self.source)
        self.assertIn('_set(circle, "radiusy", 0.16)', self.source)
        self.assertIn(
            '"0.24 * math.sin(absTime.seconds * 0.73)"', self.source
        )
        self.assertNotIn(
            '"0.5 + 0.24 * math.sin(absTime.seconds * 0.73)"', self.source
        )

    def test_streamdiffusion_boundary_is_unmistakable(self) -> None:
        required_names = (
            "STREAMDIFFUSION_ADAPTER",
            "REPLACE_WITH_STREAMDIFFUSION_RGB",
            "REPLACE_WITH_DEPTH_ESTIMATE",
            "OUT_RGB",
            "OUT_DEPTH",
        )
        for name in required_names:
            self.assertIn(name, self.source)
        self.assertIn("Demo mode works without this branch", self.source)
        self.assertIn("use_stream.name", self.source)
        self.assertIn("use_depth.name", self.source)
        self.assertIn("canonical_name", self.source)
        bootstrap = (ROOT / "touchdesigner" / "bootstrap_project.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("def _auto_load_tox", bootstrap)
        self.assertIn("if not bool(config.get('auto_load_tox', False))", bootstrap)
        self.assertIn("holder.loadTox(path)", bootstrap)
        self.assertIn("configured adapter was rejected; demo remains active", bootstrap)

    def test_role_bridge_has_real_local_shared_memory_and_network_paths(self) -> None:
        for operator_type in (
            "sharedmeminTOP", "sharedmemoutTOP", "touchinTOP", "touchoutTOP"
        ):
            self.assertIn('"%s"' % operator_type, self.source)
        for operator_name in (
            "ROLE_BRIDGE", "PACK_ATOMIC_ATLAS", "RX_SHARED_ATLAS",
            "PACK_DEPTH_PLANES",
            "TX_SHARED_ATLAS", "RX_TCP_ATLAS", "TX_TCP_ATLAS",
            "ATLAS_ROUTE", "UNPACK_ATLAS_RGB", "UNPACK_ATLAS_DEPTH",
            "UNPACK_ATLAS_CONFIDENCE", "UNPACK_ATLAS_MASK",
            "RGB_ROUTE", "DEPTH_ROUTE", "MASK_ROUTE",
            "CONFIDENCE_ROUTE", "FRAME_STATE_CONTRACT",
        ):
            self.assertIn(operator_name, self.source)
        self.assertIn('"videocodec", "uncompressed"', self.source)
        self.assertIn('_set(atlas_pack, "format", "rgba32float")', self.source)
        self.assertIn('_set(unpack_depth, "format", "mono32float")', self.source)
        self.assertIn('required(node, "format", "rgba32float")', self.source)
        self.assertIn('"memtype", "global"', self.source)
        self.assertIn('"downloadtype", "immediate"', self.source)
        self.assertIn("Atlasport", self.source)
        self.assertIn("RX_TCP_ATLAS_INFO", self.source)
        self.assertIn("mintarget", self.source)
        self.assertIn("maxtarget", self.source)
        self.assertNotIn('"targetdelay"', self.source)
        pack = self.module.SHADERS["transport_pack_geometry"]
        atlas = self.module.SHADERS["transport_pack_atlas"]
        unpack = self.module.SHADERS["transport_unpack_depth"]
        self.assertIn("vec4(rawDepth, confidence, mask", pack)
        self.assertIn("rawDepth, confidence, mask", atlas)
        self.assertNotIn("clamp(texture(sTD2DInputs[0]", pack)
        self.assertNotIn("clamp(texture(sTD2DInputs[0]", unpack)
        self.assertIn("calibration_digest", self.source)
        self.assertIn("WorldBus required for producer metadata", self.source)
        self.assertIn(
            "Touch TCP num_received_frames is transport-arrival preview only",
            self.source,
        )
        self.assertIn("metadata-less Shared Mem fails closed", self.source)
        self.assertIn("explicit frame-state sidecar or WorldBus", self.source)
        self.assertNotIn("endpoint cook-frame supplies receive freshness", self.source)

    def test_role_bridge_is_the_only_managed_path_into_reconstruction(self) -> None:
        self.assertIn(
            "_connect(sources, role_bridge, 0, 0, report, replace=True)",
            self.source,
        )
        self.assertIn(
            "_connect(sources, role_bridge, 1, 1, report, replace=True)",
            self.source,
        )
        self.assertIn(
            "_connect(role_bridge, reconstruction, 0, 0, report, replace=True)",
            self.source,
        )
        self.assertIn(
            "_connect(role_bridge, reconstruction, 1, 1, report, replace=True)",
            self.source,
        )
        root_wiring = self.source.split("# These wires are owned by the builder", 1)[1]
        root_wiring = root_wiring.split("# Easy-to-find root outputs", 1)[0]
        self.assertNotIn("_connect(sources, reconstruction", root_wiring)

    def test_moge2_bridge_is_opt_in_synchronized_and_decodes_uint16_depth(self) -> None:
        for name in (
            "MOGE2_BRIDGE",
            "RESULT_ATLAS",
            "DEPTH_SCALE_BIAS",
            "FRAME_STATE",
            "CAMERA_METADATA",
            "MOGE2_RGB_ROUTE",
            "MOGE2_DEPTH_ROUTE",
            "MOGE2_CONFIDENCE_ROUTE",
            "MOGE2_MASK_ROUTE",
        ):
            self.assertIn(name, self.source)
        self.assertIn('_custom(comp, page, "Toggle", "Enabled", False)', self.source)
        self.assertIn("op('MOGE2_BRIDGE').par.Resultvalid", self.source)
        self.assertIn("returned RGB with its metric depth", self.source)
        depth = self.module.SHADERS["moge2_unpack_depth"]
        self.assertIn("highByte * 256.0 + lowByte", depth)
        self.assertIn("uint16Depth * scaleBias.r + scaleBias.g", depth)
        self.assertIn("packed.b >= 0.5 && packed.a >= 0.5", depth)
        self.assertIn("texelFetch(sTD2DInputs[1]", depth)
        self.assertNotEqual(
            self.module.SHADERS["moge2_unpack_depth"],
            self.module.SHADERS["transport_unpack_depth"],
        )
        self.assertIn("module_dat.module.stop(me.parent())", self.module.MOGE2_EXECUTE_CALLBACKS)
        self.assertNotIn('_set(atlas, "alwayscook"', self.source)
        self.assertNotIn(".destroy(", self.source)

    def test_bounded_moge2_installer_preserves_existing_adapter_fallbacks(self) -> None:
        signature = inspect.signature(self.module.install_moge2_bridge)
        self.assertEqual(list(signature.parameters), ["root"])
        self.assertIsNone(signature.parameters["root"].default)
        installer = inspect.getsource(self.module.install_moge2_bridge)
        self.assertIn("fallbacks.append(source)", installer)
        self.assertIn("_first_input(source)", installer)
        self.assertIn("_wire_moge2_routes", installer)
        self.assertNotIn("build(", installer)
        self.assertNotIn("destroy", installer)
        self.assertIn("installed disabled", installer)

    def test_depth_anything_generated_geometry_is_isolated_and_selectable(self) -> None:
        for marker in (
            "DEPTH_ANYTHING_GEOMETRY_BRIDGE",
            "GENERATED_GEOMETRY_RGB_ROUTE",
            "GENERATED_GEOMETRY_DEPTH_ROUTE",
            "GENERATED_GEOMETRY_CONFIDENCE_ROUTE",
            "GENERATED_GEOMETRY_MASK_ROUTE",
            "DEPTH_ANYTHING_GEOMETRY_FAIL_CLOSED_ZERO",
            "Geometrysource",
            "depth_anything",
            "9251",
            "9250",
            "9261",
            "9260",
        ):
            self.assertIn(marker, self.source)
        signature = inspect.signature(
            self.module.install_depth_anything_geometry_bridge)
        self.assertEqual(list(signature.parameters), ["root"])
        installer = inspect.getsource(
            self.module.install_depth_anything_geometry_bridge)
        self.assertNotIn("build(", installer)
        self.assertNotIn("destroy", installer)
        self.assertIn("installed disabled", installer)
        self.assertIn("_wire_generated_geometry_routes", installer)

    def test_feedback_history_has_a_deterministic_seed_input(self) -> None:
        self.assertIn(
            '_connect(position, feedback, 0, 0, report, replace=True)',
            self.source,
        )
        self.assertIn(
            '_set(feedback, ("targettop", "target", "top"), persistent.path)',
            self.source,
        )
        self.assertIn('"feedbackTOP", "COLOR_HISTORY"', self.source)
        self.assertIn('"feedbackTOP", "STATE_HISTORY"', self.source)
        self.assertIn('"temporal_color", "temporal_color"', self.source)
        state = self.module.SHADERS["temporal_state"]
        observation = self.module.SHADERS["temporal_observation"]
        self.assertIn("confidenceDecay", state)
        self.assertIn("ageStep", state)
        self.assertIn("carriedConfidence", state)
        self.assertIn("new-frame one-cook pulse", observation)
        self.assertIn("timeBasedRetention", state)
        self.assertIn('"FRAME_CONTROL"', self.source)
        self.assertIn('"TEMPORAL_OBSERVATION"', self.source)
        self.assertIn('"ADVECT_HISTORY"', self.source)

    def test_completion_is_applied_in_each_output_view(self) -> None:
        installation = self.module.SHADERS["installation_grade"]
        view = self.module.SHADERS["view_completion"]
        for shader in (installation, view):
            self.assertIn("edgeHole", shader)
            self.assertIn("FLEXGPU_VIEW_FOG_DENSITY", shader)
            self.assertIn("FLEXGPU_VIEW_FOG_RADIUS", shader)
        self.assertNotIn("fog.rgb * fog.a * 0.24", installation)
        self.assertIn("vec3 color = points.rgb + edgeColor * viewFog", installation)
        self.assertIn('"GRADE_LEFT_EYE", "view_completion"', self.source)
        self.assertIn('"GRADE_RIGHT_EYE", "view_completion"', self.source)
        self.assertIn('"GRADE_" + key, "installation_grade"', self.source)
        self.assertIn('for mode in ("WRAP", "ARTISTIC")', self.source)
        self.assertIn('for side in ("LEFT", "CENTER", "RIGHT")', self.source)
        self.assertIn('_set(node, "bgcolora", 0.0)', self.source)

    def test_panorama_has_independent_fov_and_procedural_only_coverage(self) -> None:
        coverage = self.module.SHADERS["panoramic_coverage"]
        self.assertIn("FLEXGPU_WRAP_COVERAGE", coverage)
        self.assertIn("FLEXGPU_WRAP_NOISE", coverage)
        self.assertIn("FLEXGPU_WRAP_PANEL_INDEX", coverage)
        self.assertIn("panoramaUV", coverage)
        self.assertIn("proceduralMist", coverage)
        self.assertIn("continuousFill", coverage)
        self.assertIn("no generated RGB pixels are copied", coverage)
        self.assertNotIn("sTD2DInputs[1]", coverage)
        self.assertNotIn("fog_plate", coverage.lower())
        self.assertIn('"Wrapfovdegrees", 78.0', self.source)
        self.assertIn(
            '_expr(camera, "fov", "parent().par.Wrapfovdegrees.eval()")',
            self.source,
        )
        self.assertIn(
            '_expr(camera, "fov", "parent().par.Surfacefovdegrees.eval()")',
            self.source,
        )
        self.assertIn('"COVERAGE_" + key, "panoramic_coverage"', self.source)
        self.assertIn('if mode == "WRAP":', self.source)
        self.assertIn("[grade_input, fog_plate]", self.source)

    def test_public_show_control_and_bounded_upgrade_are_present(self) -> None:
        for marker in (
            "SHOW_CONTROL",
            "Geometryprovider",
            "Displaymode",
            "Completionmode",
            "Fogdensity",
            "Interactionstrength",
            "Interactionsmoothing",
            "Qualityprofile",
            "Wrapcoverage",
        ):
            self.assertIn(marker, self.source)
        self.assertIn('"parameterexecuteDAT"', self.source)
        self.assertIn("SHOW_CONTROL_CALLBACKS", self.source)
        signature = inspect.signature(self.module.install_show_control_upgrade)
        self.assertEqual(list(signature.parameters), ["root"])
        installer = inspect.getsource(self.module.install_show_control_upgrade)
        self.assertNotIn("destroy", installer)
        self.assertNotIn("build(", installer)
        self.assertIn("surface_width", installer)
        self.assertIn("surface_height", installer)
        self.assertIn("_activate_geometry_bridge", self.source)
        self.assertIn("runtime_dat.module.tick(bridge)", self.source)
        self.assertIn("helpers.module.select_geometry_provider", self.source)
        self.assertIn("_switch_runtime_geometry_contract(provider)", self.source)

    def test_venue_1080p_upgrade_is_bounded_and_complete(self) -> None:
        installer = inspect.getsource(self.module.install_venue_1080p_outputs)
        self.assertIn("1920, 1080", installer)
        self.assertIn("wall_width * 3", installer)
        self.assertIn("METRIC_RENDER_CENTER", installer)
        self.assertIn("METRIC_RENDER_%s_%s", installer)
        self.assertIn("GRADE_%s_%s", installer)
        self.assertNotIn("destroy", installer)
        self.assertNotIn("build(", installer)

    def test_actual_point_render_and_visible_outputs_are_built(self) -> None:
        for operator_name in (
            "toptoPOP", "rendersimpleTOP", "pointspriteMAT", "geometryCOMP",
            "selectPOP", "cameraCOMP", "renderTOP", "deletePOP",
        ):
            self.assertIn('"%s"' % operator_name, self.source)
        for output in ("OUT_INSTALLATION", "OUT_LEFT_EYE", "OUT_RIGHT_EYE",
                       "OUT_STEREO_PREVIEW", "OUT_TRIPLE_WRAP",
                       "OUT_TRIPLE_ARTISTIC", "OUT_DISPLAY_ACTIVE"):
            self.assertIn(output, self.source)
        self.assertIn('"rgba", "pactive"', self.source)
        self.assertIn("POSITION_TO_POINTS", self.source)
        self.assertIn("VISIBLE_POINT_THIN", self.source)
        self.assertIn("POINT_GLYPH", self.source)
        self.assertIn("POINT_SPRITE_MATERIAL", self.source)
        self.assertIn('"Pointsize", 3.0', self.source)
        self.assertIn('"Pointkeep", 0.68', self.source)
        self.assertIn('"Pointopacity", 0.92', self.source)
        self.assertIn('_set_sequence_blocks(points, "input", 2)', self.source)
        self.assertIn(
            '"input0attrscope", "P(0) P(1) P(2) active"',
            self.source,
        )
        self.assertIn(
            '"Color(0) Color(1) Color(2) Color(3)"',
            self.source,
        )
        self.assertNotIn('"input0attrscope", "P P P P"', self.source)
        self.assertNotIn('"input1attrscope", "Color Color Color Color"', self.source)
        self.assertIn('"thinrandomseed", 19', self.source)
        self.assertIn(
            "1.0 - pow(max(0.0, 1.0 - parent().par.Pointkeep.eval()),",
            self.source,
        )
        self.assertIn('"overridemat", point_material.path', self.source)
        self.assertIn('"normalizegeo", False', self.source)
        self.assertNotIn('"normalizegeo", True', self.source)
        self.assertIn('_expr(camera, "tx", shift_expression)', self.source)
        self.assertIn('_set(camera, "ipdshift", 0.0)', self.source)
        self.assertIn('"CAMERA_WRAP_" + side', self.source)
        self.assertIn(
            '("LEFT", "parent().par.Wrapyawdegrees.eval()")',
            self.source,
        )
        self.assertIn(
            '("RIGHT", "-parent().par.Wrapyawdegrees.eval()")',
            self.source,
        )
        self.assertIn('"CAMERA_ARTISTIC_" + side', self.source)
        self.assertIn('"tx", 0.0', self.source)
        self.assertIn("Wrapyawdegrees", self.source)
        self.assertIn("Artisticoffsetmetres", self.source)
        self.assertIn("DISPLAY_MODE_ROUTE", self.source)
        self.assertIn('("OUT_SOURCE_COLOR", role_bridge, 0)', self.source)
        self.assertIn('("OUT_COLOR", contract, 1)', self.source)
        self.assertIn(
            'menu=("single", "panoramic_wrap", "artistic_multi_angle")',
            self.source,
        )
        self.assertNotIn("eye_offset * -35.0", self.source)
        self.assertIn("HEADSET_ADAPTER_CONTRACT", self.source)

    def test_point_glyph_is_round_soft_and_separate_from_scene_color(self) -> None:
        glyph = self.module.SHADERS["point_glyph"]
        self.assertIn("length(pointUV)", glyph)
        self.assertIn("smoothstep(0.72, 1.0, radius)", glyph)
        self.assertIn("vec4(1.0, 1.0, 1.0, alpha)", glyph)
        self.assertIn('_set(point_material, "colormap", point_glyph.path)', self.source)
        self.assertNotIn('_set(point_material, "colormap", color.path)', self.source)
        self.assertIn('_set(point_material, "alphatest", True)', self.source)
        self.assertIn('_set(point_material, "blending", True)', self.source)

    def test_explicit_resolutions_ignore_the_host_global_multiplier(self) -> None:
        self.assertIn('_set(node, "resmult", False)', self.source)
        self.assertIn('_set(color, "resmult", False)', self.source)

    def test_component_connect_order_and_point_render_wiring_are_explicit(self) -> None:
        # TouchDesigner In/Out TOP connectors use Connect Order. If all orders
        # remain zero, RENDER_CONTRACT exposes COLOR, INTERACTION, POSITION by
        # name and POINT_RENDER receives the wrong textures.
        self.assertIn('(\"connectorder\", \"inputindex\", \"index\")', self.source)
        self.assertIn('(\"connectorder\", \"outputindex\", \"index\")', self.source)
        self.assertIn(
            '_connect(contract, point_render, 0, 0, report, replace=True)',
            self.source,
        )
        self.assertIn(
            '_connect(contract, point_render, 1, 1, report, replace=True)',
            self.source,
        )
        self.assertNotIn(
            '_connect(contract, point_render, 0, 1, report, replace=True)',
            self.source,
        )
        self.assertNotIn(
            '_connect(contract, point_render, 1, 2, report, replace=True)',
            self.source,
        )
        self.assertIn("Interaction stays", self.source)

    def test_managed_root_wiring_replaces_legacy_alphabetical_connections(self) -> None:
        self.assertIn("def _connect(src, dst, dst_index=0, src_index=0, report=None, replace=False)",
                      self.source)
        self.assertIn("if not replace:", self.source)
        root_wiring = self.source.split("# These wires are owned by the builder", 1)[1]
        root_wiring = root_wiring.split("# Easy-to-find root outputs", 1)[0]
        managed_connections = [line.strip() for line in root_wiring.splitlines()
                               if line.strip().startswith("_connect(")]
        self.assertGreaterEqual(len(managed_connections), 18)
        self.assertTrue(all("replace=True" in line for line in managed_connections))

    def test_managed_internal_wiring_replaces_legacy_stage_connections(self) -> None:
        # Rebuilding an older .toe must move the managed render switches and
        # stereo layout from the retired RENDER_* path to the metric cameras
        # and per-eye completion passes. Unknown artist nodes remain untouched.
        for connection in (
            '_connect(rendered, switch, 1, 0, report, replace=True)',
            '_connect(left_grade, layout, 0, 0, report, replace=True)',
            '_connect(right_grade, layout, 1, 0, report, replace=True)',
            '_connect(source, node, report=report, replace=True)',
        ):
            with self.subTest(connection=connection):
                self.assertIn(connection, self.source)

    def test_experimental_external_adapters_are_off_by_default(self) -> None:
        self.assertEqual(set(self.module.EXPERIMENTAL_ADAPTERS),
                         {"SHARP_EXTERNAL", "GAUSSIAN_EXTERNAL"})
        for name, spec in self.module.EXPERIMENTAL_ADAPTERS.items():
            with self.subTest(adapter=name):
                self.assertFalse(spec["default_enabled"])
        self.assertIn("stub.allowCooking = False", self.source)

    def test_build_is_idempotent_in_shape_and_never_deletes_nodes(self) -> None:
        signature = inspect.signature(self.module.build)
        self.assertEqual(list(signature.parameters), ["root"])
        self.assertIsNone(signature.parameters["root"].default)
        self.assertNotIn(".destroy(", self.source)
        self.assertIn("_ensure(root, \"baseCOMP\", PIPELINE_NAME", self.source)
        self.assertIn('_set(pipeline, "Buildversion", BUILD_VERSION)', self.source)
        self.assertIn("managed_scope", self.source)

    def test_telemetry_dat_and_chop_sources_exist(self) -> None:
        for name in ("TELEMETRY_CONTRACT", "PERFORMANCE_METRICS", "infoCHOP",
                     "OUT_PERFORMANCE", "LIVE_HEALTH", "infoDAT"):
            self.assertIn(name, self.source)


if __name__ == "__main__":
    unittest.main()
