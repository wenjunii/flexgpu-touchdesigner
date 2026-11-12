"""Reversible stopped-generator host mapping audit (no model/file pulses).

Native Start/Stop/Offload and UI previews need separate asynchronous tests.
Loading/replacing/removing a private generator is deliberately not exercised.
"""
import json
from pathlib import Path

try:
    import streamdiffusion_host_runtime as runtime
    import validate_show_controls as controls
except ImportError:
    from touchdesigner import streamdiffusion_host_runtime as runtime
    from touchdesigner import validate_show_controls as controls


TRIALS = {
    'Sdsmoothenabled': (False, True), 'Sdsmoothms': (0, 75, 250),
    'Sdnoiseenabled': (False, True), 'Sdnoisewidth': (256, 1024),
    'Sdnoiseheight': (144, 576), 'Sdnoiseseed': (0, 17),
    'Sdpromptformat': ('auto', 'sdtd_blocks', 'simple'),
    'Sdpromptparameter': ('Auditprompt',), 'Sdseedparameter': ('Auditseed',),
    'Sdstartparameter': ('Auditstart',), 'Sdstopparameter': ('Auditstop',),
    'Sdtoxfile': ('', 'missing-control-audit.tox'),
    'Sdpayload': ('missing_control_audit_payload',),
    'Sdrgboutput': ('missing_control_audit_rgb',),
    'Sdoffloadbackup': ('', 'missing-rollback-audit.toe'),
}


def validate(root, report_path=None):
    host = root.op('WORKING_PIPELINE/SOURCES/STREAMDIFFUSION_ADAPTER/STREAMDIFFUSION_HOST')
    show = root.op('WORKING_PIPELINE/SHOW_CONTROL')
    payload = runtime.selected_payload(host)
    if (runtime.server_flags(payload) != (False, False)
            or host.par.Followpodcast.eval() or host.par.Streamenabled.eval()):
        raise RuntimeError('Stop generation, turn audio following off and disconnect RGB first')
    names = tuple(runtime.CONTROL_MAP.values())
    before = {name: controls._parameter_state(host, name) for name in names}
    checks = []
    restoration_errors = []
    try:
        for mirror, values in TRIALS.items():
            source = runtime.CONTROL_MAP[mirror]
            for value in values:
                show.par[mirror].val = value
                host.op('host_runtime').module.control_changed(host, show.par[mirror])
                checks.append({'control': mirror, 'passed':
                    host.par[source].eval() == value and show.par[mirror].eval() == value})
            controls._restore_parameter_state(host, source, before[source])
            host.op('host_runtime').module.tick(host)
        checks.append({'control': 'retired source is slot-only', 'passed':
            host.par.Source.eval() == 'slot' and host.par.Source.readOnly})
        checks.append({'control': 'no engine COMP', 'passed':
            not any(node.type == 'engine' for node in host.children)})
    finally:
        # Follow remains OFF throughout: no generator prompt/seed writes.
        for name, state in before.items():
            try:
                controls._restore_parameter_state(host, name, state)
            except Exception:
                restoration_errors.append(name)
        host.op('host_runtime').module.tick(host)
    restored = all(controls._state_contract(state) == controls._state_contract(
        controls._parameter_state(host, name)) for name, state in before.items())
    unchanged_payload = runtime.selected_payload(host) == payload
    checks.extend(({'control': 'all host settings restored', 'passed': restored},
                   {'control': 'selected payload preserved', 'passed': unchanged_payload},
                   {'control': 'no generator started', 'passed': runtime.server_flags(payload) == (False, False)}))
    failures = [c for c in checks if not c['passed']]
    result = {'status': 'fail' if failures or restoration_errors else 'pass',
              'checks': len(checks), 'failures': failures,
              'restoration_errors': restoration_errors,
              'scope': 'public host value mappings; no model start/load/removal/save pulses',
              'native_buttons': 'separate asynchronous validation required'}
    if report_path:
        Path(report_path).write_text(json.dumps(result, indent=2), encoding='utf-8')
    return result
