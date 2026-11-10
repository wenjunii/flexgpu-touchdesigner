from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from touchdesigner import streamdiffusion_host_runtime as runtime
from touchdesigner import upgrade_streamdiffusion_host as upgrade


class Par:
    def __init__(self, name, value):
        self.name, self.val = name, value
        self.mode, self.expr, self.bindExpr = 'CONSTANT', '', ''
        self.isPulse, self.isCustom, self.isToggle, self.readOnly = False, True, False, False

    def eval(self):
        return self.val


class Node:
    def __init__(self, path, **values):
        self.path, self.name = path, path.rsplit('/', 1)[-1]
        self.family, self.children, self.nodes, self.storage = 'COMP', [], {}, {}
        self.customPars = [Par(key, value) for key, value in values.items()]
        self.par = SimpleNamespace(**{par.name: par for par in self.customPars})

    def op(self, path):
        return self.nodes.get(path)

    def fetch(self, key, default=None):
        return self.storage.get(key, default)


class HostUpgradeTests(unittest.TestCase):
    def fixture(self, folder, machine='3080'):
        root = Node('/project1/flexgpu')
        pipeline = Node(root.path + '/WORKING_PIPELINE')
        show = Node(pipeline.path + '/SHOW_CONTROL', Qualityprofile=upgrade.PROFILES[machine],
                    Wallwidth=1920, Viewscale=1.2, Sdtoxfile='local-private.tox', Sdstreamenabled=False)
        adapter = Node(root.path + '/' + upgrade.ADAPTER, Geometrysource='depth_anything')
        root.nodes.update({'WORKING_PIPELINE': pipeline, upgrade.ADAPTER: adapter,
                           'WORKING_PIPELINE/SHOW_CONTROL': show})
        pipeline.nodes['SHOW_CONTROL'] = show
        pipeline.children = [show, Node(pipeline.path + '/SENSOR_INTERACTION', Enabled=False)]
        for name in upgrade.BRIDGES:
            bridge = Node(adapter.path + '/' + name, Profile=upgrade.PROFILES[machine], Capturefps=5)
            bridge.nodes['bridge_runtime'] = Node(bridge.path + '/bridge_runtime')
            adapter.nodes[name] = bridge
        source = Path(folder) / ('Show-%s.current.toe' % machine)
        backup = Path(folder) / ('Show-%s.before-upgrade.toe' % machine)
        for path in (source, backup):
            path.write_bytes(b'unit-test TOE fixture\n' * 100)
        current = SimpleNamespace(name=source.name, folder=str(folder))
        return root, show, adapter, current, backup

    def test_all_three_profiles_default_to_read_only(self):
        with tempfile.TemporaryDirectory() as folder:
            for machine in upgrade.PROFILES:
                with self.subTest(machine=machine):
                    root, show, adapter, current, backup = self.fixture(folder, machine)
                    before = backup.read_bytes()
                    with mock.patch.object(upgrade.builder, 'install') as install:
                        result = upgrade.upgrade(root, machine, backup, project_object=current)
                    self.assertEqual(result['status'], 'preflight_pass')
                    self.assertFalse(result['applied'])
                    self.assertFalse(result['live_acceptance'])
                    self.assertEqual(backup.read_bytes(), before)
                    install.assert_not_called()

    def test_cross_machine_and_ambiguous_identity_rejected_before_install(self):
        with tempfile.TemporaryDirectory() as folder:
            for source in upgrade.PROFILES:
                for expected in upgrade.PROFILES:
                    if source == expected:
                        continue
                    root, _, _, current, backup = self.fixture(folder, source)
                    with mock.patch.object(upgrade.builder, 'install') as install, self.assertRaises(ValueError):
                        upgrade.upgrade(root, expected, backup, apply=True, project_object=current)
                    install.assert_not_called()
            for name in ('Show.toe', 'Show-3080-5090.toe', 'Show-13080.toe', 'Show-4090x.toe'):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    runtime.machine_tag(name)
            self.assertEqual(runtime.machine_tag('Show-3080TI.1.toe'), '3080')

    def test_profile_backup_root_and_activity_fail_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            for case in ('profile', 'bridge_profile', 'same_backup', 'missing_backup', 'root', 'running', 'unknown'):
                root, show, adapter, current, backup = self.fixture(folder)
                if case == 'profile':
                    show.par.Qualityprofile.val = '5090'
                elif case == 'bridge_profile':
                    adapter.op(upgrade.BRIDGES[0]).par.Profile.val = '5090'
                elif case == 'same_backup':
                    backup = Path(folder) / current.name
                elif case == 'missing_backup':
                    backup.unlink()
                elif case == 'root':
                    root.path = '/project1/other'
                else:
                    generator = Node(adapter.path + '/StreamDiffusionTD')
                    adapter.children.append(generator)
                    if case == 'running':
                        generator.par.Serveractive = Par('Serveractive', True)
                        generator.par.Streamactive = Par('Streamactive', False)
                        generator.par.Serveractive.isToggle = generator.par.Streamactive.isToggle = True
                with self.subTest(case=case), mock.patch.object(upgrade.builder, 'install') as install:
                    with self.assertRaises((ValueError, RuntimeError)):
                        upgrade.upgrade(root, '3080', backup, apply=True, project_object=current)
                    install.assert_not_called()

    def test_apply_only_installs_host_disarms_and_preserves_public_controls(self):
        with tempfile.TemporaryDirectory() as folder:
            root, show, _, current, backup = self.fixture(folder, '4090')
            host = Node('/managed-host')
            with mock.patch.object(upgrade.builder, 'install', return_value=host) as install, \
                    mock.patch.object(upgrade.runtime, 'disarm') as disarm:
                report = upgrade.upgrade(root, '4090', backup, apply=True, project_object=current)
            install.assert_called_once_with(root)
            disarm.assert_called_once()
            self.assertEqual(report['status'], 'upgraded')
            self.assertFalse(report['saved'])
            self.assertEqual(show.par.Viewscale.eval(), 1.2)
            self.assertNotIn('local-private.tox', str(report))

    def test_unexpected_setting_change_raises_without_claiming_success(self):
        with tempfile.TemporaryDirectory() as folder:
            root, show, _, current, backup = self.fixture(folder)
            def bad_install(_):
                show.par.Viewscale.val = 2
                return Node('/managed-host')
            with mock.patch.object(upgrade.builder, 'install', side_effect=bad_install), \
                    mock.patch.object(upgrade.runtime, 'disarm'), self.assertRaisesRegex(RuntimeError, 'do not save'):
                upgrade.upgrade(root, '3080', backup, apply=True, project_object=current)

    def test_live_project_identity_overrides_stale_offload_storage(self):
        with tempfile.TemporaryDirectory() as folder:
            backup = Path(folder) / 'Show-3080.rollback.toe'
            backup.write_bytes(b'x' * 2048)
            host = Node('/host', Offloadbackup=str(backup))
            host.storage.update(offload_project_name='Show-3080.toe', offload_backup_folder=folder)
            current = SimpleNamespace(name='Show-4090.toe', folder=folder)
            with mock.patch.object(runtime, 'project', current, create=True), self.assertRaises(ValueError):
                runtime._removal_backup(host)

    def test_offload_rollback_machine_matrix(self):
        with tempfile.TemporaryDirectory() as folder:
            for machine in upgrade.PROFILES:
                host = Node('/host')
                host.storage.update(offload_project_name='Show-%s.toe' % machine, offload_backup_folder=folder)
                for backup_machine in upgrade.PROFILES:
                    backup = Path(folder) / ('Show-%s.rollback.toe' % backup_machine)
                    backup.write_bytes(b'x' * 2048)
                    host.par.Offloadbackup = Par('Offloadbackup', str(backup))
                    if machine == backup_machine:
                        self.assertEqual(Path(runtime._removal_backup(host)), backup.resolve())
                    else:
                        with self.assertRaises(ValueError):
                            runtime._removal_backup(host)


if __name__ == '__main__':
    unittest.main()
