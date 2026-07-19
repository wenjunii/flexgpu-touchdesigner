import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTEXT_PATH = (
    ROOT / "integrations" / "embody" / "flexgpu-project-context.json"
)
RUNTIME_SOURCE = ROOT / "touchdesigner" / "runtime_pipeline.py"
BRIDGE_CHECKER_PATH = ROOT / "scripts" / "Test-TDKnowledgeBridge.ps1"
CODEX_CONFIG_EXAMPLE_PATH = ROOT / ".codex" / "config.toml.example"
GITIGNORE_PATH = ROOT / ".gitignore"
README_PATH = ROOT / "README.md"
EMBODY_DOC_PATH = ROOT / "docs" / "EMBODY_MCP.md"
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
        self.assertEqual(
            self.context["network"]["identity_operators"],
            ["/project1/flexgpu"],
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

    def test_project_scoped_mcp_wiring_is_public_and_documented(self):
        checker = BRIDGE_CHECKER_PATH.read_text(encoding="utf-8")
        example = CODEX_CONFIG_EXAMPLE_PATH.read_text(encoding="utf-8")
        gitignore = GITIGNORE_PATH.read_text(encoding="utf-8")
        readme = README_PATH.read_text(encoding="utf-8")
        embody_doc = EMBODY_DOC_PATH.read_text(encoding="utf-8")

        for marker in (
            "--envoy-config",
            "--project-context",
            "identity_operators",
            "Test-LoopbackPort",
            "RequireEnvoy",
        ):
            self.assertIn(marker, checker)
        for marker in (
            "--envoy-config",
            "--faiss-db",
            "--project-context",
            "<ABSOLUTE_PATH_TO_",
        ):
            self.assertIn(marker, example)

        self.assertIsNone(WINDOWS_ABSOLUTE_PATH.search(example))
        self.assertIn(".codex/config.toml", gitignore)
        for documentation in (readme, embody_doc):
            self.assertIn("Test-TDKnowledgeBridge.ps1", documentation)
            self.assertIn(".codex/config.toml", documentation)


if __name__ == "__main__":
    unittest.main()
