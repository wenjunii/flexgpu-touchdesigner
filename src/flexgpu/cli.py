"""Command-line interface used by the thin ``tools/flexgpu.py`` entry point."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Mapping, Sequence

from .config import load_config
from .diagnostics import diagnostic_summary, run_diagnostics
from .discovery import discover_nvidia_gpus
from .models import (
    ConfigError,
    DiscoveryError,
    FlexConfig,
    FlexGPUError,
    PlanError,
    RuntimeControlError,
    redact_text,
    sensitive_environment_values,
)
from .planner import build_process_plan
from .runtime import MAX_RECOVERY_ATTEMPTS, recover_managed, runtime_status, start_plan, stop_managed


def _add_output_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="emit compact machine-readable JSON")


def _add_config_option(parser: argparse.ArgumentParser, required: bool = True) -> None:
    parser.add_argument("--config", required=required, help="path to a JSON or TOML show config")


def _add_overrides(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--experience", choices=("installation", "vr", "combined"))
    parser.add_argument("--completion", choices=("fog", "procedural", "hybrid"))
    parser.add_argument(
        "--tier", choices=("auto", "3080ti_16gb", "4090", "5090", "custom")
    )


def _add_execution_mode(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--execute", action="store_true", help="perform the requested mutation")
    group.add_argument("--dry-run", action="store_true", help="preview without changing processes")


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flexgpu",
        description="Plan and safely launch role-based TouchDesigner GPU processes.",
    )
    subparsers = parser.add_subparsers(dest="action", required=True)

    discover = subparsers.add_parser("discover", help="list NVIDIA GPUs and tuned tiers")
    _add_config_option(discover, required=False)
    discover.add_argument("--nvidia-smi", help="explicit nvidia-smi executable")
    _add_output_option(discover)

    validate = subparsers.add_parser("validate", help="validate config without probing hardware")
    _add_config_option(validate)
    _add_output_option(validate)

    for name, help_text in (
        ("plan", "resolve local process roles and GPU affinity"),
        ("diagnose", "run read-only hardware and path preflight checks"),
        ("start", "preview or start the resolved process plan"),
        ("recover", "preview or recover only a separate AI process"),
    ):
        command = subparsers.add_parser(name, help=help_text)
        _add_config_option(command)
        _add_overrides(command)
        command.add_argument("--nvidia-smi", help="explicit nvidia-smi executable")
        if name in {"diagnose", "start", "recover"}:
            _add_execution_mode(command)
        if name == "recover":
            command.add_argument(
                "--attempts",
                type=int,
                default=1,
                help="bounded AI start attempts (1-%d)" % MAX_RECOVERY_ATTEMPTS,
            )
            command.add_argument(
                "--restart-running",
                action="store_true",
                help="gracefully restart a healthy AI process instead of reusing it",
            )
        if name in {"start", "recover"}:
            command.add_argument(
                "--wait-ready-ms",
                type=int,
                default=None,
                help="bounded application-readiness wait; config default when omitted",
            )
        _add_output_option(command)

    status = subparsers.add_parser("status", help="report manifest-owned process state")
    _add_config_option(status)
    _add_output_option(status)

    stop = subparsers.add_parser("stop", help="preview or stop manifest-managed processes")
    _add_config_option(stop)
    _add_execution_mode(stop)
    _add_output_option(stop)
    return parser


def _overrides(args: argparse.Namespace) -> dict[str, str]:
    return {
        key: value
        for key in ("experience", "completion", "tier")
        if (value := getattr(args, key, None)) is not None
    }


def _config_summary(config: FlexConfig) -> dict[str, Any]:
    return {
        "status": "valid",
        "source": config.source_path,
        "topology": config.topology,
        "experience": config.experience,
        "completion": config.completion,
        "tier": config.tier,
        "node_role": config.node_role,
        "gpu": {role: selector.to_dict() for role, selector in config.gpu.items()},
        "process_roles": sorted(config.processes),
        "transport": dict(config.transport),
        "runtime_dir": config.runtime_dir,
    }


def _scrub_payload(value: Any, sensitive_values: Sequence[str]) -> Any:
    if isinstance(value, str):
        return redact_text(value, sensitive_values)
    if isinstance(value, Mapping):
        return {str(key): _scrub_payload(item, sensitive_values) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub_payload(item, sensitive_values) for item in value]
    return value


def _emit(
    payload: Mapping[str, Any] | Sequence[Any],
    compact: bool = False,
    sensitive_values: Sequence[str] = (),
) -> None:
    print(
        json.dumps(
            _scrub_payload(payload, sensitive_values),
            indent=None if compact else 2,
            separators=(",", ":") if compact else None,
            sort_keys=not compact,
        )
    )


def _emit_error(
    exc: BaseException,
    compact: bool = False,
    sensitive_values: Sequence[str] = (),
) -> None:
    if isinstance(exc, ConfigError):
        payload: dict[str, Any] = {
            "status": "error",
            "type": "config",
            "errors": [redact_text(error, sensitive_values) for error in exc.errors],
        }
    else:
        payload = {
            "status": "error",
            "type": exc.__class__.__name__,
            "error": redact_text(exc, sensitive_values),
        }
    print(
        json.dumps(payload, indent=None if compact else 2, sort_keys=not compact),
        file=sys.stderr,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    compact = bool(getattr(args, "json", False))
    sensitive_values: tuple[str, ...] = ()
    try:
        if args.action == "discover":
            gpus = discover_nvidia_gpus(args.nvidia_smi)
            _emit({"status": "ok", "gpus": [gpu.to_dict() for gpu in gpus]}, compact)
            return 0

        config = load_config(args.config, _overrides(args))
        sensitive_values = sensitive_environment_values(os.environ) + tuple(
            value
            for process in config.processes.values()
            for value in sensitive_environment_values(process.env)
        )
        if args.action == "validate":
            _emit(_config_summary(config), compact, sensitive_values)
            return 0
        if args.action == "status":
            _emit({"status": "ok", "runtime": runtime_status(config)}, compact, sensitive_values)
            return 0
        if args.action == "stop":
            result = stop_managed(config, execute=bool(args.execute))
            result["status"] = "ok"
            _emit(result, compact, sensitive_values)
            return 0

        gpus = discover_nvidia_gpus(args.nvidia_smi)
        plan = build_process_plan(config, gpus)
        if args.action == "plan":
            _emit({"status": "ok", "plan": plan.to_dict()}, compact, sensitive_values)
            return 0

        checks = run_diagnostics(config, gpus, plan)
        summary = diagnostic_summary(checks)
        diagnostics_payload = {
            "summary": summary,
            "checks": [check.to_dict() for check in checks],
        }
        if args.action == "diagnose":
            _emit(
                {
                    "status": summary["status"],
                    "mode": "execute" if args.execute else "dry-run",
                    "note": "Diagnostics are read-only; no show process was started.",
                    "diagnostics": diagnostics_payload,
                    "plan": plan.to_dict(),
                    "runtime": runtime_status(config),
                },
                compact,
                sensitive_values,
            )
            return 3 if summary["status"] == "fail" else 0

        if args.action == "recover":
            if not 1 <= args.attempts <= MAX_RECOVERY_ATTEMPTS:
                raise RuntimeControlError(
                    "recovery attempts must be between 1 and %d" % MAX_RECOVERY_ATTEMPTS
                )
            if args.execute and summary["status"] == "fail":
                _emit(
                    {
                        "status": "refused",
                        "reason": "preflight diagnostics failed",
                        "diagnostics": diagnostics_payload,
                        "plan": plan.to_dict(),
                    },
                    compact,
                    sensitive_values,
                )
                return 3
            result = recover_managed(
                plan,
                config,
                attempts=args.attempts,
                restart_running=bool(args.restart_running),
                execute=bool(args.execute),
                wait_ready_ms=args.wait_ready_ms,
            )
            _emit(
                {
                    "status": "recovered" if args.execute else "dry-run",
                    "runtime": result,
                    "diagnostics": diagnostics_payload,
                    "plan": plan.to_dict(),
                },
                compact,
                sensitive_values,
            )
            return 0

        if args.action == "start":
            if args.execute and summary["status"] == "fail":
                _emit(
                    {
                        "status": "refused",
                        "reason": "preflight diagnostics failed",
                        "diagnostics": diagnostics_payload,
                        "plan": plan.to_dict(),
                    },
                    compact,
                    sensitive_values,
                )
                return 3
            result = start_plan(
                plan,
                config,
                execute=bool(args.execute),
                wait_ready_ms=args.wait_ready_ms,
            )
            _emit(
                {
                    "status": "started" if args.execute else "dry-run",
                    "runtime": result,
                    "diagnostics": diagnostics_payload,
                    "plan": plan.to_dict(),
                },
                compact,
                sensitive_values,
            )
            return 3 if not args.execute and summary["status"] == "fail" else 0
        parser.error("unsupported action")
        return 2
    except ConfigError as exc:
        _emit_error(exc, compact, sensitive_values)
        return 2
    except (DiscoveryError, PlanError, RuntimeControlError, FlexGPUError) as exc:
        _emit_error(exc, compact, sensitive_values)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
