"""Explicit, bounded upgrade of an existing machine-local RGB host.

Importing is inert. Preflight is read-only. Applying never rebuilds the world,
loads a TOX, starts a model, removes a generator, or saves the project.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

try:
    import runtime_pipeline as rp
    import streamdiffusion_host as builder
    import streamdiffusion_host_runtime as runtime
except ImportError:
    from touchdesigner import runtime_pipeline as rp
    from touchdesigner import streamdiffusion_host as builder
    from touchdesigner import streamdiffusion_host_runtime as runtime


PROFILES = {'3080': '3080ti_16gb', '4090': '4090', '5090': '5090'}
ADAPTER = 'WORKING_PIPELINE/SOURCES/STREAMDIFFUSION_ADAPTER'
BRIDGES = ('MOGE2_BRIDGE', 'DEPTH_ANYTHING_GEOMETRY_BRIDGE')


def _project(project_object):
    current = rp._symbol('project') if project_object is None else project_object
    if current is None:
        raise RuntimeError('Open the destination machine-local TOE in TouchDesigner first')
    return current


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str,
                                     allow_nan=False).encode('utf-8')).hexdigest()


def _parameters(node, excluded=()):
    values = {}
    for par in node.customPars:
        if par.name in excluded or par.isPulse or getattr(par, 'readOnly', False):
            continue
        # Preserve the binding/expression as well as its raw value. Evaluating
        # status expressions would make freshness/cook changes look like edits.
        mode = str(par.mode)
        raw = str(par.val) if mode.rsplit('.', 1)[-1].lower() == 'constant' else ''
        values[par.name] = (mode, raw, str(par.expr),
                            str(getattr(par, 'bindExpr', '')))
    return values


def _settings(root):
    """Fingerprint public settings only; never descend into the private adapter."""
    pipeline = root.op('WORKING_PIPELINE')
    show = pipeline.op('SHOW_CONTROL')
    adapter = root.op(ADAPTER)
    data = {'show': _parameters(show, builder.VALUE_CONTROLS +
                               builder.PULSE_CONTROLS + builder.STATUS_CONTROLS)}
    # These components contain public builders, not private model components.
    # Only their root custom controls are inspected; no DAT contents or prompts.
    for node in pipeline.children:
        if node.family == 'COMP' and node.name not in ('SOURCES', 'SHOW_CONTROL'):
            data[node.path] = _parameters(node)
    for name in BRIDGES:
        data[name] = _parameters(adapter.op(name), ('Sourceactive', 'Sourceepoch',
                                                   'Resultvalid', 'Sourceframeid'))
    # Keep unrelated adapter user settings (paths/prompts stay only in memory).
    data['adapter'] = _parameters(adapter)
    return _digest(data)


def preflight(root, expected_machine, backup_path, project_object=None):
    """Read-only identity, saved rollback and inactive-generator checks."""
    if expected_machine not in PROFILES:
        raise ValueError('Expected machine must be 3080, 4090 or 5090')
    current = _project(project_object)
    folder = Path(current.folder).resolve()
    source = folder / str(current.name)
    backup = Path(backup_path).expanduser().resolve()
    if root is None or root.path != '/project1/flexgpu':
        raise RuntimeError('The approved /project1/flexgpu root is required')
    for path in (source, backup):
        if (path.suffix.lower() != '.toe' or path.parent != folder or
                not path.is_file() or path.stat().st_size < 1024):
            raise ValueError('Current TOE and saved rollback must exist in the project folder')
        if runtime.machine_tag(path.name) != expected_machine:
            raise ValueError('Project/rollback belongs to a different machine')
    if source.resolve() == backup:
        raise ValueError('Save a distinct rollback TOE before applying this upgrade')
    show = root.op('WORKING_PIPELINE/SHOW_CONTROL')
    adapter = root.op(ADAPTER)
    if show is None or adapter is None:
        raise RuntimeError('Existing public Show Control and adapter are required')
    if rp._value(show, 'Qualityprofile', '') != PROFILES[expected_machine]:
        raise ValueError('Show Control quality profile does not match the destination machine')
    bridges = [adapter.op(name) for name in BRIDGES]
    if any(node is None or node.op('bridge_runtime') is None for node in bridges):
        raise RuntimeError('Both existing generated-geometry bridges are required')
    if any(rp._value(node, 'Profile', '') != PROFILES[expected_machine] for node in bridges):
        raise ValueError('Both geometry bridge profiles must match the destination machine')
    host = adapter.op(builder.HOST_NAME)
    if host is not None and host.fetch('managed_owner', None) != builder.OWNER:
        raise RuntimeError('Unrecognized host; refusing to overwrite it')
    slot = None if host is None else host.op('PAYLOAD_SLOT')
    generators = [node for node in adapter.children
                  if node.family == 'COMP' and node.name.startswith('StreamDiffusionTD')]
    if slot is not None:
        generators += [node for node in slot.children if node.family == 'COMP']
    for generator in generators:
        if runtime.server_flags(generator) != (False, False):
            raise RuntimeError('Stop each generator through its native controls; both public activity flags must be OFF')
    return {'version': 'flexgpu-host-upgrade/v1', 'status': 'preflight_pass',
            'machine': expected_machine, 'profile': PROFILES[expected_machine],
            'inactive_generators': len(generators),
            'settings_digest': _settings(root),
            'backup_sha256': hashlib.sha256(backup.read_bytes()).hexdigest(),
            'applied': False, 'saved': False, 'models_started': False,
            'legacy_generators_removed': False, 'live_acceptance': False}


def upgrade(root, expected_machine, backup_path, *, apply=False, project_object=None):
    """Default dry-run; apply only the host builder after preflight succeeds.

    A preservation mismatch raises immediately. This is not a transaction: do
    not save a failed upgrade; reopen the accepted rollback instead.
    """
    report = preflight(root, expected_machine, backup_path, project_object)
    if not apply:
        return report
    existing = root.op(ADAPTER).op(builder.HOST_NAME)
    if existing is not None:
        runtime.disarm(existing, 'host upgrade in progress')
    host = builder.install(root)
    runtime.disarm(host, 'host upgrade: reconnect RGB only after preview acceptance')
    # Refresh the visible delivery switch without changing unrelated controls.
    runtime._set(root.op('WORKING_PIPELINE/SHOW_CONTROL'), 'Sdstreamenabled', False)
    if _settings(root) != report['settings_digest']:
        raise RuntimeError('Public settings changed unexpectedly; do not save, reopen the rollback TOE')
    report.update(status='upgraded', applied=True, rgb_delivery_enabled=False)
    return report
