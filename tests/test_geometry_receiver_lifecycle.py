"""Same-process project reloads must not orphan managed return sockets."""
import builtins
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from touchdesigner import moge2_bridge_runtime as bridge


class ReceiverLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.registry = {'version': 1, 'lock': threading.RLock(), 'owners': {}}
        self.registry_patch = patch.object(builtins, bridge._RECEIVER_REGISTRY,
                                          self.registry, create=True)
        self.registry_patch.start()
        self.addCleanup(self.registry_patch.stop)
        self.src_patch = patch.object(bridge, '_EMBEDDED_FLEXGPU_SRC', '')
        self.src_patch.start()
        self.addCleanup(self.src_patch.stop)
        self.owner = SimpleNamespace(path=sorted(bridge._MANAGED_BRIDGE_PATHS)[0],
                                     fileFolder='/checkout-a/projects')

    def test_same_owner_replacement_closes_predecessor_before_registration(self):
        old, new = Mock(), Mock()
        bridge._register_owned_receiver(self.owner, old)
        reloaded_owner = SimpleNamespace(**vars(self.owner))
        bridge._retire_owned_receiver(reloaded_owner)
        old.stop.assert_called_once_with()
        bridge._register_owned_receiver(reloaded_owner, new)
        self.assertIs(self.registry['owners'][bridge._receiver_owner_key(self.owner)], new)
        bridge._forget_owned_receiver(old)
        self.assertIs(self.registry['owners'][bridge._receiver_owner_key(self.owner)], new)

    def test_other_checkouts_and_other_bridge_paths_are_never_stopped(self):
        other_checkout = SimpleNamespace(path=self.owner.path, fileFolder='/checkout-b/projects')
        other_bridge = SimpleNamespace(path=sorted(bridge._MANAGED_BRIDGE_PATHS)[1],
                                       fileFolder=self.owner.fileFolder)
        old, unrelated_a, unrelated_b = Mock(), Mock(), Mock()
        for owner, runtime in ((self.owner, old), (other_checkout, unrelated_a),
                               (other_bridge, unrelated_b)):
            bridge._register_owned_receiver(owner, runtime)
        bridge._retire_owned_receiver(self.owner)
        old.stop.assert_called_once()
        unrelated_a.stop.assert_not_called()
        unrelated_b.stop.assert_not_called()

    def test_unknown_roots_and_missing_checkout_are_not_registered(self):
        runtime = Mock()
        for owner in (SimpleNamespace(path='/another_project/bridge', fileFolder='/checkout-a'),
                      SimpleNamespace(path=self.owner.path)):
            bridge._register_owned_receiver(owner, runtime)
            bridge._retire_owned_receiver(owner)
        runtime.stop.assert_not_called()
        self.assertEqual(self.registry['owners'], {})

    def test_duplicate_registration_is_fail_closed_and_cleanup_is_identity_scoped(self):
        old, new = Mock(), Mock()
        bridge._register_owned_receiver(self.owner, old)
        bridge._register_owned_receiver(self.owner, old)
        with self.assertRaisesRegex(bridge.BridgeRuntimeError, 'already has an owner'):
            bridge._register_owned_receiver(self.owner, new)
        bridge._forget_owned_receiver(new)
        self.assertEqual(len(self.registry['owners']), 1)
        bridge._forget_owned_receiver(old)
        self.assertEqual(self.registry['owners'], {})

    def test_failed_close_retains_handle_and_unknown_registry_is_not_replaced(self):
        old = Mock()
        old.stop.side_effect = RuntimeError('close failed')
        bridge._register_owned_receiver(self.owner, old)
        with self.assertRaisesRegex(RuntimeError, 'close failed'):
            bridge._retire_owned_receiver(self.owner)
        self.assertIs(self.registry['owners'][bridge._receiver_owner_key(self.owner)], old)
        with patch.object(builtins, bridge._RECEIVER_REGISTRY, {'version': 99}):
            with self.assertRaisesRegex(bridge.BridgeRuntimeError, 'Unrecognized'):
                bridge._register_owned_receiver(self.owner, Mock())


if __name__ == '__main__':
    unittest.main()
