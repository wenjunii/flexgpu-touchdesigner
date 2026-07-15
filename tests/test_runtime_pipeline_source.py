from __future__ import annotations

import ast
import importlib.util
import inspect
import unittest
from pathlib import Path


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

    def test_shader_names_are_stable_and_complete(self) -> None:
        expected = {
            "depth_to_position",
            "sensor_position",
            "interaction_field",
            "temporal_persistence",
            "fog_completion",
            "procedural_backfill",
            "procedural_color",
            "hybrid_completion",
            "installation_grade",
            "transport_pack_atlas",
            "transport_unpack_rgb",
            "transport_unpack_depth",
        }
        self.assertEqual(set(self.module.SHADERS), expected)

    def test_every_shader_declares_output_contract_and_touchdesigner_swizzle(self) -> None:
        for name, shader in self.module.SHADERS.items():
            with self.subTest(shader=name):
                self.assertIn("// CONTRACT:", shader)
                self.assertIn("out vec4 fragColor;", shader)
                self.assertIn("void main()", shader)
                self.assertIn("TDOutputSwizzle", shader)
                self.assertIn("sTD2DInputs", shader)

    def test_position_and_persistence_contracts_keep_active_alpha(self) -> None:
        depth = self.module.SHADERS["depth_to_position"]
        temporal = self.module.SHADERS["temporal_persistence"]
        self.assertIn("vec4(position, valid)", depth)
        self.assertIn("XYZ metres + active alpha", depth)
        self.assertIn("POSITION + HISTORY + INTERACTION", temporal)
        self.assertIn("persistenceDecay", temporal)
        self.assertIn("interaction.rgb", temporal)
        self.assertIn("vec4(position, activity)", temporal)
        self.assertNotIn("float active =", temporal)
        self.assertIn('"resolutionTOP", "COLOR_ALIGNED_RESIZE"', self.source)
        self.assertIn('"Geometryresolution", 384', self.source)

    def test_color_alignment_migrates_legacy_null_without_destroying_it(self) -> None:
        # 1.0.0 used a nullTOP named COLOR_ALIGNED. Generic _ensure reuses by
        # name, so requesting resolutionTOP under that name cannot change type.
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
        self.assertNotIn("sTD2DInputs[3]", procedural_color)
        self.assertIn("FOG_COLOR + PROCEDURAL_COLOR", hybrid)

    def test_public_top_contracts_cover_render_sensor_installation_and_stereo(self) -> None:
        expected = {
            "RGB", "DEPTH", "POSITION", "COLOR", "SENSOR_POSITION",
            "INTERACTION", "INSTALLATION", "STEREO",
        }
        self.assertEqual(set(self.module.TOP_CONTRACTS), expected)
        self.assertIn("XYZ metres", self.module.TOP_CONTRACTS["POSITION"])
        self.assertIn("side-by-side", self.module.TOP_CONTRACTS["STEREO"])

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

    def test_role_bridge_has_real_local_shared_memory_and_network_paths(self) -> None:
        for operator_type in (
            "sharedmeminTOP", "sharedmemoutTOP", "touchinTOP", "touchoutTOP"
        ):
            self.assertIn('"%s"' % operator_type, self.source)
        for operator_name in (
            "ROLE_BRIDGE", "PACK_ATOMIC_ATLAS", "RX_SHARED_ATLAS",
            "TX_SHARED_ATLAS", "RX_TCP_ATLAS", "TX_TCP_ATLAS",
            "ATLAS_ROUTE", "UNPACK_ATLAS_RGB", "UNPACK_ATLAS_DEPTH",
            "RGB_ROUTE", "DEPTH_ROUTE",
        ):
            self.assertIn(operator_name, self.source)
        self.assertIn('"videocodec", "uncompressed"', self.source)
        self.assertIn('"format", "mono16float"', self.source)
        self.assertIn('"memtype", "global"', self.source)
        self.assertIn('"downloadtype", "immediate"', self.source)
        self.assertIn("Atlasport", self.source)
        self.assertIn("RX_TCP_ATLAS_INFO", self.source)
        self.assertIn("mintarget", self.source)
        self.assertIn("maxtarget", self.source)
        self.assertNotIn('"targetdelay"', self.source)
        self.assertIn("not WorldBus v1", self.source)
        self.assertIn("left RGB, right depth", self.source)

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

    def test_feedback_history_has_a_deterministic_seed_input(self) -> None:
        self.assertIn('_connect(position, feedback, 0, 0, report)', self.source)
        self.assertIn('_set(feedback, ("targettop", "target"), persistent.path)', self.source)

    def test_actual_point_render_and_visible_outputs_are_built(self) -> None:
        for operator_name in ("toptoPOP", "rendersimpleTOP", "pointspriteMAT"):
            self.assertIn('"%s"' % operator_name, self.source)
        for output in ("OUT_INSTALLATION", "OUT_LEFT_EYE", "OUT_RIGHT_EYE",
                       "OUT_STEREO_PREVIEW"):
            self.assertIn(output, self.source)
        self.assertIn('"rgba", "pactive"', self.source)
        self.assertIn("POSITION_TO_POINTS", self.source)
        self.assertIn("POINT_SPRITE_MATERIAL", self.source)
        self.assertIn('"Pointsize", 3.0', self.source)
        self.assertIn('"materialsource", "matnode"', self.source)

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
                     "OUT_PERFORMANCE", "infoDAT"):
            self.assertIn(name, self.source)


if __name__ == "__main__":
    unittest.main()
