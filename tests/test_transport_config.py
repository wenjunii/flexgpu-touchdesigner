from __future__ import annotations

import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from flexgpu.config import validate_config  # noqa: E402
from flexgpu.models import ConfigError  # noqa: E402


def process(role: str) -> dict[str, object]:
    return {
        "command": [sys.executable, "-c", "print(%r)" % role],
        "touchdesigner": False,
    }


def configuration(topology: str, transport: object | None = None) -> dict[str, object]:
    if topology == "single":
        result: dict[str, object] = {
            "topology": topology,
            "processes": {"world": process("world")},
        }
    elif topology == "dual_local":
        result = {
            "topology": topology,
            "processes": {"ai": process("ai"), "world": process("world")},
        }
    else:
        result = {
            "topology": topology,
            "node_role": "ai",
            "processes": {"ai": process("ai")},
        }
    if transport is not None:
        result["transport"] = transport
    return result


def shared_transport(**updates: object) -> dict[str, object]:
    result: dict[str, object] = {
        "type": "shared_memory",
        "segment_name": "FlexShowWorldBus",
        "atlas_width": 1024,
        "atlas_height": 512,
        "atlas_fps": 10,
    }
    result.update(updates)
    return result


def touch_transport(peer_host: str = "192.0.2.20", **updates: object) -> dict[str, object]:
    result: dict[str, object] = {
        "type": "touch_tcp",
        "peer_host": peer_host,
        "atlas_width": 1024,
        "atlas_height": 512,
        "atlas_fps": 5,
        "atlas_port": 12000,
        "control_port": 12001,
        "heartbeat_port": 12002,
        "heartbeat_timeout_ms": 2000,
    }
    result.update(updates)
    return result


class TransportConfigTests(unittest.TestCase):
    def test_single_defaults_to_and_normalizes_local_transport(self) -> None:
        implicit = validate_config(configuration("single"))
        alias = validate_config(configuration("single", "inprocess"))

        self.assertEqual(implicit.transport, {"type": "local"})
        self.assertEqual(alias.transport, {"type": "local"})

    def test_dual_local_normalizes_shared_memory_alias_and_segment(self) -> None:
        transport = shared_transport(type="sharedmem", segment_name="  FlexShowWorldBus  ")
        profile = configuration("dual_local", transport)
        profile["source"] = {"frame_state_operator": "PRODUCER_FRAME_STATE"}
        config = validate_config(profile)

        self.assertEqual(config.transport["type"], "shared_memory")
        self.assertEqual(config.transport["segment_name"], "FlexShowWorldBus")

    def test_dual_local_shared_memory_requires_producer_frame_state_sidecar(self) -> None:
        for source in (None, {}, {"frame_state_operator": "   "}):
            with self.subTest(source=source), self.assertRaisesRegex(
                ConfigError, "producer-backed metadata sidecar"
            ):
                profile = configuration("dual_local", shared_transport())
                if source is not None:
                    profile["source"] = source
                validate_config(profile)

    def test_dual_local_allows_touch_aliases_only_with_loopback_peer(self) -> None:
        cases = (
            ("touch", "127.0.0.1"),
            ("touch_in_out", "localhost"),
            ("tcp", "::1"),
        )
        for transport_type, peer in cases:
            with self.subTest(transport_type=transport_type, peer=peer):
                transport = touch_transport(peer, type=transport_type)
                config = validate_config(configuration("dual_local", transport))
                self.assertEqual(config.transport["type"], "touch_tcp")
                self.assertEqual(config.transport["peer_host"], peer)

        with self.assertRaisesRegex(ConfigError, "peer_host must be 127.0.0.1"):
            validate_config(
                configuration("dual_local", touch_transport("192.0.2.20"))
            )

    def test_split_topologies_require_an_explicit_transport(self) -> None:
        for topology in ("dual_local", "dual_network"):
            with self.subTest(topology=topology), self.assertRaisesRegex(
                ConfigError, "transport is required"
            ):
                validate_config(configuration(topology))

    def test_transport_type_must_match_topology(self) -> None:
        cases = (
            ("single", shared_transport(), "shared_memory"),
            ("dual_local", {"type": "local", "atlas_width": 1024,
                            "atlas_height": 512, "atlas_fps": 10}, "local"),
            (
                "dual_network",
                shared_transport(
                    peer_host="192.0.2.20",
                    atlas_port=12000,
                    control_port=12001,
                    heartbeat_port=12002,
                    heartbeat_timeout_ms=2000,
                ),
                "shared_memory",
            ),
        )
        for topology, transport, name in cases:
            with self.subTest(topology=topology), self.assertRaisesRegex(
                ConfigError, "incompatible with topology"
            ):
                validate_config(configuration(topology, transport))

    def test_required_bridge_fields_are_not_silently_defaulted(self) -> None:
        with self.assertRaises(ConfigError) as raised:
            validate_config(
                configuration(
                    "dual_network",
                    {
                        "type": "touch_tcp",
                        "peer_host": "192.0.2.20",
                    },
                )
            )
        message = str(raised.exception)
        for field in (
            "atlas_width",
            "atlas_height",
            "atlas_fps",
            "atlas_port",
            "control_port",
            "heartbeat_port",
            "heartbeat_timeout_ms",
        ):
            self.assertIn("transport.%s is required" % field, message)

    def test_atlas_dimensions_cadence_and_ports_are_bounded(self) -> None:
        invalid = touch_transport(
            atlas_width=1023,
            atlas_height=0,
            atlas_fps=True,
            atlas_port=0,
            control_port=65536,
            heartbeat_timeout_ms=0,
        )
        with self.assertRaises(ConfigError) as raised:
            validate_config(configuration("dual_network", invalid))
        message = str(raised.exception)
        self.assertIn("atlas_width must be a multiple of 2", message)
        self.assertIn("atlas_height must be between 1 and 16384", message)
        self.assertIn("atlas_fps must be an integer", message)
        self.assertIn("atlas_port must be between 1 and 65535", message)
        self.assertIn("control_port must be between 1 and 65535", message)
        self.assertIn("heartbeat_timeout_ms must be between 1 and 600000", message)

    def test_ports_must_be_distinct_and_unknown_fields_are_rejected(self) -> None:
        invalid = touch_transport(control_port=12000, misspelled_port=12003)
        with self.assertRaises(ConfigError) as raised:
            validate_config(configuration("dual_network", invalid))
        message = str(raised.exception)
        self.assertIn("unsupported field 'misspelled_port'", message)
        self.assertIn("must not reuse transport.atlas_port port 12000", message)

    def test_peer_and_flag_types_are_validated(self) -> None:
        invalid = touch_transport(
            peer_host="   ",
            drop_stale_frames="yes",
            hold_last_complete_frame=1,
        )
        with self.assertRaises(ConfigError) as raised:
            validate_config(configuration("dual_network", invalid))
        message = str(raised.exception)
        self.assertIn("peer_host must not be empty", message)
        self.assertIn("drop_stale_frames must be true or false", message)
        self.assertIn("hold_last_complete_frame must be true or false", message)


if __name__ == "__main__":
    unittest.main()
