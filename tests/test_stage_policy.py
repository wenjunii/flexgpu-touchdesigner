from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from flexgpu.stage_policy import resolve_stage_policy  # noqa: E402


class StagePolicyTests(unittest.TestCase):
    def test_single_world_owns_source_and_world_without_transport(self) -> None:
        policy = resolve_stage_policy("world", "single", "installation", "local")
        self.assertTrue(policy.source_active)
        self.assertTrue(policy.world_active)
        self.assertTrue(policy.installation_active)
        self.assertFalse(policy.vr_active)
        self.assertFalse(policy.sender_active)
        self.assertFalse(policy.receiver_active)
        self.assertEqual(policy.bridge_mode, "local")
        self.assertEqual(policy.route_index, 0)

    def test_dual_local_ai_only_generates_and_sends(self) -> None:
        policy = resolve_stage_policy("ai", "dual_local", "combined", "shared_memory")
        self.assertTrue(policy.source_active)
        self.assertFalse(policy.world_active)
        self.assertFalse(policy.installation_active)
        self.assertFalse(policy.vr_active)
        self.assertTrue(policy.sender_active)
        self.assertFalse(policy.receiver_active)
        self.assertEqual(policy.bridge_mode, "send_shared")
        self.assertEqual(policy.route_index, 0)

    def test_dual_local_world_receives_and_never_generates(self) -> None:
        policy = resolve_stage_policy("world", "dual_local", "combined", "shared_memory")
        self.assertFalse(policy.source_active)
        self.assertTrue(policy.world_active)
        self.assertTrue(policy.installation_active)
        self.assertTrue(policy.vr_active)
        self.assertFalse(policy.sender_active)
        self.assertTrue(policy.receiver_active)
        self.assertEqual(policy.bridge_mode, "receive_shared")
        self.assertEqual(policy.route_index, 1)

    def test_dual_network_render_alias_receives_touch_streams(self) -> None:
        policy = resolve_stage_policy("render", "dual_network", "vr", "touch_tcp")
        self.assertEqual(policy.role, "world")
        self.assertFalse(policy.source_active)
        self.assertTrue(policy.world_active)
        self.assertFalse(policy.installation_active)
        self.assertTrue(policy.vr_active)
        self.assertTrue(policy.receiver_active)
        self.assertEqual(policy.bridge_mode, "receive_tcp")
        self.assertEqual(policy.route_index, 1)
        self.assertEqual(policy.atlas_route_index, 1)

    def test_standalone_dual_is_safe_local_debug_mode(self) -> None:
        policy = resolve_stage_policy("standalone", "dual_local", "combined", "")
        self.assertTrue(policy.source_active)
        self.assertTrue(policy.world_active)
        self.assertFalse(policy.sender_active)
        self.assertFalse(policy.receiver_active)
        self.assertEqual(policy.bridge_mode, "local")

    def test_incompatible_transport_is_rejected(self) -> None:
        for transport in ("spout", "local"):
            with self.subTest(transport=transport), self.assertRaisesRegex(
                ValueError, "shared_memory or touch_tcp"
            ):
                resolve_stage_policy("ai", "dual_local", "installation", transport)
        with self.assertRaisesRegex(ValueError, "touch_tcp"):
            resolve_stage_policy("world", "dual_network", "installation", "shared_memory")

    def test_dual_local_can_use_loopback_touch_tcp(self) -> None:
        ai = resolve_stage_policy("ai", "dual_local", "installation", "touch_tcp")
        world = resolve_stage_policy("world", "dual_local", "installation", "touch_tcp")
        self.assertEqual(ai.bridge_mode, "send_tcp")
        self.assertEqual(world.bridge_mode, "receive_tcp")
        self.assertEqual(world.route_index, 1)
        self.assertEqual(world.atlas_route_index, 1)

    def test_dual_local_stage_policy_default_is_loopback_touch_tcp(self) -> None:
        ai = resolve_stage_policy("ai", "dual_local", "installation")
        world = resolve_stage_policy("world", "dual_local", "installation")
        self.assertEqual(ai.bridge_mode, "send_tcp")
        self.assertEqual(world.bridge_mode, "receive_tcp")
        self.assertEqual(world.atlas_route_index, 1)


if __name__ == "__main__":
    unittest.main()
