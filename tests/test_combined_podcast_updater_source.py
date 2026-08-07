from __future__ import annotations

import ast
import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "touchdesigner" / "update_combined_podcast_3080.py"


def load_module():
    spec = importlib.util.spec_from_file_location(
        "update_combined_podcast_3080", MODULE_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load combined podcast updater")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CombinedPodcastUpdaterSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_module()
        cls.source = MODULE_PATH.read_text(encoding="utf-8")

    def test_module_is_import_safe_and_3080_combined_only(self) -> None:
        ast.parse(self.source)
        self.assertEqual(
            self.module.ADAPTER_PATH,
            "/project1/flexgpu/WORKING_PIPELINE/SOURCES/"
            "STREAMDIFFUSION_ADAPTER",
        )
        self.assertIsNotNone(
            self.module.PROJECT_NAME_PATTERN.fullmatch(
                "FlexShow-moge2-embody-podcast-local-3080.41.toe"
            )
        )
        self.assertIsNone(
            self.module.PROJECT_NAME_PATTERN.fullmatch(
                "FlexShow-moge2-embody-podcast-local-5090.71.toe"
            )
        )

    def test_updater_is_save_free_server_free_and_preserves_private_boundary(self) -> None:
        for marker in (
            "Streamdiffusionpath",
            "streamdiffusion_paths",
            "Visualpath",
            "Humanfigurejson",
            "2013-12.01-visual-scenes-human-figures.json",
            "show_control_component.py",
            "show_control_callbacks.py",
            "podcast_td_controller.py",
            '"saved": False',
            '"model_servers_started": False',
        ):
            self.assertIn(marker, self.source)
        for forbidden in (
            "project.save",
            ".save(",
            "Start-Process",
            "subprocess",
            "destroy()",
            "5090.71",
            "C:\\Users\\",
        ):
            self.assertNotIn(forbidden, self.source)

    def test_preflight_precedes_the_managed_show_control_update(self) -> None:
        self.assertLess(
            self.source.index("missing_children"),
            self.source.index("module.install_show_control"),
        )
        self.assertLess(
            self.source.index("streamdiffusion_paths ="),
            self.source.index("module.install_show_control"),
        )
        self.assertGreater(
            self.source.index(
                'str(stream_parameter.eval()) != streamdiffusion_paths'
            ),
            self.source.index("module.install_show_control"),
        )
        self.assertIn("control_operator_types", self.source)
        self.assertIn('"parameterexecuteDAT"', self.source)
        self.assertIn("_ensure_file_parameter", self.source)
        self.assertIn('"Soundscapeaudiofile"', self.source)
        self.assertIn("_ensure_menu_parameter", self.source)
        self.assertIn('"Audiosource"', self.source)
        for unrelated_operator_type in (
            '"levelTOP"',
            '"hsvadjustTOP"',
            '"switchTOP"',
            '"nullTOP"',
            '"audiofileinCHOP"',
            '"switchCHOP"',
            '"audiodeviceoutCHOP"',
            '"syphonspoutoutTOP"',
        ):
            self.assertNotIn(unrelated_operator_type, self.source)
        self.assertNotIn(
            "operator_types=operator_types,\n    )",
            self.source,
        )


if __name__ == "__main__":
    unittest.main()
