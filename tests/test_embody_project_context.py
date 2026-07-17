import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTEXT_PATH = (
    ROOT / "integrations" / "embody" / "flexgpu-project-context.json"
)
RUNTIME_SOURCE = ROOT / "touchdesigner" / "runtime_pipeline.py"
BLOCKED_FIELD_PATTERN = re.compile(
    r"(?:^|_)(?:api_?key|access_?token|auth_?token|password|passwd|secret|"
    r"credential|private_?key)(?:$|_)",
    re.IGNORECASE,
)
WINDOWS_ABSOLUTE_PATH = re.compile(r"^[A-Za-z]:[\\/]")


def _walk(value, location="$"):
    yield location, None, value
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _walk(child, f"{location}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk(child, f"{location}[{index}]")


class EmbodyProjectContextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.context = json.loads(CONTEXT_PATH.read_text(encoding="utf-8"))
        cls.runtime_source = RUNTIME_SOURCE.read_text(encoding="utf-8")

    def test_context_identity_matches_supported_baseline(self):
        self.assertEqual(self.context["schema_version"], 1)
        self.assertEqual(self.context["project_id"], "flexgpu-touchdesigner")
        self.assertEqual(
            self.context["overview"]["touchdesigner_build"],
            "2025.32820",
        )
        self.assertEqual(self.context["overview"]["runtime_build"], "1.2.1")
        self.assertEqual(
            self.context["network"]["managed_scope"],
            "/project1/flexgpu/WORKING_PIPELINE",
        )

    def test_every_named_output_is_declared_by_the_runtime_builder(self):
        outputs = self.context["outputs"]["comparison_order"]
        self.assertGreaterEqual(len(outputs), 16)
        names = set()
        for output in outputs:
            name = output["name"]
            path = output["path"]
            self.assertNotIn(name, names)
            names.add(name)
            self.assertTrue(path.startswith("/project1/flexgpu/WORKING_PIPELINE/"))
            operator_name = path.rsplit("/", 1)[-1]
            self.assertIn(f'"{operator_name}"', self.runtime_source)

    def test_profile_contains_no_secret_keys_or_absolute_machine_paths(self):
        for location, _, value in _walk(self.context):
            if isinstance(value, dict):
                for key in value:
                    self.assertIsNone(
                        BLOCKED_FIELD_PATTERN.search(key),
                        f"secret-shaped key at {location}.{key}",
                    )
            if isinstance(value, str):
                self.assertIsNone(
                    WINDOWS_ABSOLUTE_PATH.match(value),
                    f"absolute Windows path at {location}",
                )
                self.assertNotIn("\\\\", value, f"UNC-like value at {location}")


if __name__ == "__main__":
    unittest.main()
