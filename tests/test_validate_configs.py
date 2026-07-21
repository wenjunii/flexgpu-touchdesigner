from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from flexgpu.config import TIER_ALIASES, TRANSPORT_TYPE_ALIASES  # noqa: E402


def load_validation_tool():
    path = ROOT / "tools" / "validate_configs.py"
    spec = importlib.util.spec_from_file_location("validate_configs", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load tools/validate_configs.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ConfigValidationToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tool = load_validation_tool()

    def test_default_target_discovery_is_stable_and_excludes_schema(self) -> None:
        paths = self.tool.configuration_paths(
            self.tool.DEFAULT_TARGETS, self.tool.DEFAULT_SCHEMA
        )
        self.assertEqual(len(paths), 9)
        self.assertNotIn(self.tool.DEFAULT_SCHEMA.resolve(), paths)
        self.assertEqual(paths, sorted(paths, key=lambda path: str(path).lower()))

    def test_strict_json_rejects_duplicate_keys_and_non_finite_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.json"
            for payload in ('{"tier":"auto","tier":"custom"}', '{"value":NaN}'):
                with self.subTest(payload=payload):
                    path.write_text(payload, encoding="utf-8")
                    with self.assertRaises(ValueError):
                        self.tool.load_strict_json(path)

    def test_schema_and_python_accept_the_same_canonical_tiers(self) -> None:
        schema = json.loads(self.tool.DEFAULT_SCHEMA.read_text(encoding="utf-8"))
        schema_tiers = set(schema["properties"]["tier"]["enum"])
        python_tiers = set(TIER_ALIASES.values())
        self.assertEqual(schema_tiers, python_tiers)

    def test_schema_and_python_accept_the_same_canonical_transport_types(self) -> None:
        schema = json.loads(self.tool.DEFAULT_SCHEMA.read_text(encoding="utf-8"))
        schema_types = set(
            schema["$defs"]["transport"]["properties"]["type"]["enum"]
        )
        python_types = set(TRANSPORT_TYPE_ALIASES.values())
        self.assertEqual(schema_types, python_types)

    def test_shipped_dual_local_presets_use_turnkey_loopback_touch_tcp(self) -> None:
        for name in (
            "dual-local-heterogeneous.json",
            "dual-local-same-4090.json",
        ):
            with self.subTest(name=name):
                profile = self.tool.load_strict_json(
                    ROOT / "config" / "presets" / name
                )
                self.assertEqual(profile["transport"]["type"], "touch_tcp")
                self.assertEqual(profile["transport"]["peer_host"], "127.0.0.1")
                self.assertNotIn("segment_name", profile["transport"])

    def test_initializer_generates_loopback_touch_for_dual_local(self) -> None:
        source = (ROOT / "scripts" / "Initialize-FlexShow.ps1").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn(
            "$transportType = if ($resolvedTopology -eq 'dual_local') { 'touch_tcp' }",
            source,
        )
        self.assertIn("$transport.peer_host = '127.0.0.1'", source)

    @unittest.skipUnless(importlib.util.find_spec("jsonschema"), "jsonschema is optional")
    def test_schema_enforces_topology_specific_transport_contracts(self) -> None:
        import jsonschema

        schema = json.loads(self.tool.DEFAULT_SCHEMA.read_text(encoding="utf-8"))
        validator = jsonschema.Draft202012Validator(schema)

        def profile(topology: str, transport: dict[str, object]) -> dict[str, object]:
            processes: dict[str, object]
            result: dict[str, object] = {
                "topology": topology,
                "experience": "installation",
                "completion": "hybrid",
                "tier": "auto",
                "gpu": {"ai": "auto", "render": "auto"},
                "transport": transport,
            }
            if topology == "single":
                processes = {"world": {"command": ["python", "show.py"]}}
            elif topology == "dual_local":
                processes = {
                    "ai": {"command": ["python", "ai.py"]},
                    "world": {"command": ["python", "show.py"]},
                }
            else:
                result["node_role"] = "ai"
                processes = {"ai": {"command": ["python", "ai.py"]}}
            result["processes"] = processes
            return result

        atlas = {"atlas_width": 1024, "atlas_height": 512, "atlas_fps": 5}
        local = profile("single", {"type": "local"})
        shared = profile(
            "dual_local",
            {"type": "shared_memory", "segment_name": "FlexShowWorldBus", **atlas},
        )
        shared["source"] = {"frame_state_operator": "PRODUCER_FRAME_STATE"}
        loopback = profile(
            "dual_local",
            {
                "type": "touch_tcp",
                "peer_host": "127.0.0.1",
                "atlas_port": 12000,
                **atlas,
            },
        )
        network = profile(
            "dual_network",
            {
                "type": "touch_tcp",
                "peer_host": "192.0.2.20",
                "atlas_port": 12000,
                "control_port": 12001,
                "heartbeat_port": 12002,
                "heartbeat_timeout_ms": 2000,
                **atlas,
            },
        )
        for valid in (local, shared, loopback, network):
            with self.subTest(topology=valid["topology"]):
                self.assertEqual(list(validator.iter_errors(valid)), [])

        remote_loopback_profile = dict(loopback)
        remote_loopback_profile["transport"] = {
            **loopback["transport"],
            "peer_host": "192.0.2.20",
        }
        wrong_network_type = dict(network)
        wrong_network_type["transport"] = {
            **network["transport"],
            "type": "shared_memory",
            "segment_name": "FlexShowWorldBus",
        }
        metadata_less_shared = dict(shared)
        metadata_less_shared.pop("source")
        for invalid in (
            remote_loopback_profile,
            wrong_network_type,
            metadata_less_shared,
        ):
            self.assertTrue(list(validator.iter_errors(invalid)))

    @unittest.skipUnless(importlib.util.find_spec("jsonschema"), "jsonschema is optional")
    def test_tool_also_enforces_launcher_only_adaptive_relationships(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid-relations.json"
            path.write_text(
                json.dumps(
                    {
                        "topology": "single",
                        "experience": "installation",
                        "completion": "hybrid",
                        "tier": "auto",
                        "gpu": {"ai": "auto", "render": "auto"},
                        "processes": {
                            "world": {"command": ["python", "show.py"]}
                        },
                        "transport": {"type": "local"},
                        "adaptive": {
                            "levels": 2,
                            "initial_level": 2,
                            "thresholds": {"frame_low": 2.0, "frame_high": 1.0},
                        },
                    }
                ),
                encoding="utf-8",
            )
            _paths, failures = self.tool.validate(self.tool.DEFAULT_SCHEMA, [path])
            self.assertTrue(any("launcher:" in failure for failure in failures))
            self.assertTrue(any("initial_level" in failure for failure in failures))


if __name__ == "__main__":
    unittest.main()
