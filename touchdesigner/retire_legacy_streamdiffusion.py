"""Explicit, backup-gated retirement of the known 3080 dual-generator branch.

Not called by normal installation. Private components are deleted as whole
operators only; their children/code are never inspected or externalized.
"""
from pathlib import Path

try:
    import streamdiffusion_host as builder
except ImportError:
    from touchdesigner import streamdiffusion_host as builder


LEGACY_TYPES = {
    'StreamDiffusionTD': 'container', 'StreamDiffusionTD4': 'container',
    'StreamDiffusionTD_callbacks': 'text', 'StreamDiffusionTD_callbacks4': 'text',
    'null1': 'null', 'null2': 'null', 'noise2': 'noise',
    'color_hsv_1': 'hsvadj', 'color_hsv_2': 'hsvadj',
    'color_level_1': 'level', 'color_level_2': 'level',
    'color_switch_1': 'switch', 'color_switch_2': 'switch',
    'color_out_1': 'null', 'color_out_2': 'null',
    'story_edge_fade_1': 'level', 'story_edge_fade_2': 'level',
    'semantic_test_image': 'constant', 'semantic_neutral_colour': 'constant',
    'semantic_neutral_output_1': 'switch', 'semantic_neutral_output_2': 'switch',
    'syphonspoutin1': 'syphonspoutin',
}
RETIRED_COLOR_CONTROLS = (
    'Colorenabled', 'Brightness', 'Contrast', 'Gamma', 'Blacklevel', 'Opacity',
    'Hue', 'Saturation', 'Value', 'Resetcolor',
)


def controller_guard(source):
    marker = '# FLEXGPU_RETIRED_DUAL_GENERATOR'
    construction_marker = '# FLEXGPU_SINGLE_CONTROLLER_CONSTRUCTION'
    if construction_marker not in source:
        needle = '    _CONTROLLER = module.PodcastVisualController(owner)\n'
        if source.count(needle) != 1:
            raise RuntimeError('Unrecognized public controller constructor; review before retirement')
        source = source.replace(needle,
            '    ' + construction_marker + '\n'
            '    host = owner.op("STREAMDIFFUSION_HOST")\n'
            '    if host is not None and host.fetch("legacy_retired", False):\n'
            '        _CONTROLLER = host.op("host_runtime").module.construct_podcast_controller(\n'
            '            host, module.PodcastVisualController, owner)\n'
            '    else:\n'
            '        _CONTROLLER = module.PodcastVisualController(owner)\n', 1)
    if marker in source:
        return source
    if source.count('return _CONTROLLER') != 2:
        raise RuntimeError('Unrecognized public controller factory; review before retirement')
    return source.replace('return _CONTROLLER', 'return _single_generator_controller(_CONTROLLER)') + '''

# FLEXGPU_RETIRED_DUAL_GENERATOR
def _single_generator_controller(controller):
    host = parent().op('STREAMDIFFUSION_HOST')
    if host is not None:
        return host.op('host_runtime').module.attach_podcast_controller(host, controller)
    return controller
'''


def plan(adapter):
    """Inspect only the named roots and their public outgoing connections."""
    host = adapter.op(builder.HOST_NAME)
    if host is None or host.fetch('managed_owner', '') != builder.OWNER:
        raise RuntimeError('Install the managed single-generator host first')
    nodes = []
    for name, expected_type in LEGACY_TYPES.items():
        node = adapter.op(name)
        if node is None:
            continue
        if node.parent() != adapter or node.type != expected_type:
            raise RuntimeError('Unexpected legacy operator: ' + name)
        nodes.append(node)
    for node in nodes:
        for output in node.outputs:
            if output not in nodes and output != host and not output.path.startswith(host.path + '/'):
                raise RuntimeError('Legacy node has an unrelated consumer: ' + node.name + ' -> ' + output.path)
    callbacks = adapter.op('execute_callbacks')
    if callbacks is None or bool(builder.rp._value(callbacks, 'syncfile', False)):
        raise RuntimeError('Expected an embedded public podcast callback')
    controller_guard(callbacks.text)  # Validate before any deletion.
    return nodes


def _destroy_reviewed_roots(adapter, names):
    # TD also deletes docked callback DATs with their owning component. Resolve
    # each name again rather than dereferencing a now-invalid cached OP handle.
    for name in names:
        if name not in LEGACY_TYPES:
            raise RuntimeError('Unreviewed deletion target: ' + name)
        node = adapter.op(name)
        if node is not None:
            node.destroy()


def retire(root, *, backup_path, project_folder, project_name, confirm=False):
    """Delete only this reviewed branch after explicit user authorization/backup."""
    if confirm is not True:
        raise RuntimeError('Explicit confirmation is required; normal installs never delete legacy nodes')
    if '3080' not in str(project_name) or '5090' in str(project_name):
        raise RuntimeError('This reviewed retirement is for the 3080 working copy only')
    backup = Path(backup_path).resolve()
    if (backup.parent != Path(project_folder).resolve() or backup.suffix.lower() != '.toe'
            or '3080' not in backup.name or not backup.is_file() or backup.stat().st_size < 1024):
        raise RuntimeError('A nonempty, machine-local 3080 TOE backup is required')
    adapter = root.op('WORKING_PIPELINE/SOURCES/STREAMDIFFUSION_ADAPTER')
    nodes = plan(adapter)
    host = adapter.op(builder.HOST_NAME)
    host.op('host_runtime').module.disarm(host, 'retire legacy generators')
    callbacks = adapter.op('execute_callbacks')
    callbacks.text = controller_guard(callbacks.text)
    host.store('legacy_retired', True)
    host.store('legacy_retirement_backup', str(backup))
    for connector in host.inputConnectors:
        connector.disconnect()
    host.op('PAYLOAD_RGB').par.top = host.op('DISCONNECTED').path
    for connector in host.op('RAW_RGB').inputConnectors:
        connector.disconnect()
    old_input = host.op('EXISTING_RGB')
    if old_input is not None:
        if old_input.type != 'in':
            raise RuntimeError('Unexpected legacy input type')
        old_input.destroy()
    paths = [node.path for node in nodes]
    _destroy_reviewed_roots(adapter, [node.name for node in nodes])
    parameter = getattr(adapter.par, 'Streamdiffusionpath', None)
    if parameter is not None:
        parameter.val = ''
        parameter.readOnly = True
    # Keep historical public control identifiers for playlist compatibility,
    # but clearly disable the obsolete per-source two-lane grading controls.
    control = adapter.op('show_control')
    if control is not None:
        for name in RETIRED_COLOR_CONTROLS:
            p = getattr(control.par, name, None)
            if p is not None:
                p.enable = False
                p.readOnly = True
    builder.install(root)
    report = {'removed': paths, 'count': len(paths), 'backup': str(backup),
              'audio_preserved': True, 'private_internals_inspected': False}
    host.store('legacy_retirement_report', report)
    return report
