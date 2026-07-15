#!/usr/bin/env python3
"""WorldBus v1 reference receiver, loopback demo, and replay utility."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Sequence


REPOSITORY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE_ROOT = os.path.join(REPOSITORY_ROOT, "src")
if SOURCE_ROOT not in sys.path:
    sys.path.insert(0, SOURCE_ROOT)

from flexgpu.worldbus import (  # noqa: E402
    TCPFrameSender,
    UDPJsonEndpoint,
    WorldBusError,
    WorldBusReceiver,
    generate_replay_frames,
    make_control,
    make_heartbeat,
    make_metadata_message,
    replay_summary,
    send_replay,
    write_replay,
)


def _positive_float(value: str) -> float:
    result = float(value)
    if result <= 0 or not result < float("inf"):
        raise argparse.ArgumentTypeError("must be a positive finite number")
    return result


def _port(value: str) -> int:
    result = int(value)
    if result < 0 or result > 65535:
        raise argparse.ArgumentTypeError("port must be between 0 and 65535")
    return result


def _print(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Dependency-free WorldBus v1 reference tools"
    )
    subparsers = parser.add_subparsers(dest="action", required=True)

    loopback = subparsers.add_parser(
        "loopback", help="exercise TCP frames and UDP messages on 127.0.0.1"
    )
    loopback.add_argument("--frames", type=int, default=4)
    loopback.add_argument("--width", type=int, default=32)
    loopback.add_argument("--height", type=int, default=16)
    loopback.add_argument("--timeout", type=_positive_float, default=3.0)

    generate = subparsers.add_parser(
        "replay-generate", help="generate a deterministic binary .wbr replay"
    )
    generate.add_argument("--output", required=True)
    generate.add_argument("--frames", type=int, default=8)
    generate.add_argument("--width", type=int, default=32)
    generate.add_argument("--height", type=int, default=16)
    generate.add_argument("--interval-ms", type=_positive_float, default=100.0)
    generate.add_argument("--overwrite", action="store_true")

    inspect = subparsers.add_parser(
        "replay-inspect", help="validate and summarize a .wbr replay"
    )
    inspect.add_argument("path")

    send = subparsers.add_parser(
        "replay-send", help="send a replay to a WorldBus TCP receiver"
    )
    send.add_argument("path")
    send.add_argument("--host", default="127.0.0.1")
    send.add_argument("--tcp-port", type=_port, required=True)
    send.add_argument("--speed", type=_positive_float, default=1.0)
    send.add_argument("--no-pacing", action="store_true")

    receive = subparsers.add_parser(
        "receive", help="run a bounded reference receiver for a fixed duration"
    )
    receive.add_argument("--host", default="127.0.0.1")
    receive.add_argument("--tcp-port", type=_port, default=9101)
    receive.add_argument("--udp-port", type=_port, default=9100)
    receive.add_argument("--duration", type=_positive_float, default=10.0)
    receive.add_argument("--stale-after", type=_positive_float, default=1.0)
    return parser


def _wait_until(predicate: Any, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def run_loopback(args: argparse.Namespace) -> dict[str, Any]:
    if args.timeout > 30:
        raise ValueError("loopback timeout cannot exceed 30 seconds")
    if args.frames < 1 or args.frames > 256:
        raise ValueError("loopback frames must be between 1 and 256")
    if args.width < 1 or args.height < 1:
        raise ValueError("loopback dimensions must be positive")
    if args.frames * args.width * args.height > 16 * 1024 * 1024:
        raise ValueError("loopback request exceeds the 16M aggregate-pixel limit")
    with WorldBusReceiver(stale_after_s=1.0) as receiver:
        tcp_host, tcp_port = receiver.tcp_address
        udp_host, udp_port = receiver.udp_address
        with UDPJsonEndpoint() as udp_sender:
            udp_sender.send(make_heartbeat("ai"), udp_host, udp_port)
            udp_sender.send(
                make_control("/flexgpu/v1/control/freeze_ai", False),
                udp_host,
                udp_port,
            )
            sent_frames = 0
            last_frame = None
            with TCPFrameSender(tcp_host, tcp_port) as tcp_sender:
                for frame in generate_replay_frames(
                    args.frames, args.width, args.height
                ):
                    udp_sender.send(
                        make_metadata_message(frame.metadata), udp_host, udp_port
                    )
                    tcp_sender.send(frame)
                    sent_frames += 1
                    last_frame = frame
            assert last_frame is not None

            captured_controls: list[dict[str, Any]] = []

            def ready() -> bool:
                captured_controls.extend(receiver.pop_controls())
                return (
                    receiver.frames.stats["accepted"] >= sent_frames
                    and receiver.heartbeats.status("ai")["state"] == "alive"
                    and bool(
                        receiver.metadata_for(
                            last_frame.metadata.frame_id,
                            str(last_frame.metadata.extensions.get("producer_session_id")),
                        )
                    )
                    and bool(captured_controls)
                )

            complete = _wait_until(ready, args.timeout)
            latest = receiver.frames.get(args.timeout)
            metadata = receiver.metadata_for(
                latest.metadata.frame_id,
                str(latest.metadata.extensions.get("producer_session_id")),
            )
            return {
                "status": "pass" if complete and not receiver.errors else "fail",
                "tcp": {"host": tcp_host, "port": tcp_port},
                "udp": {"host": udp_host, "port": udp_port},
                "sent_frames": sent_frames,
                "received_frame_id": latest.metadata.frame_id,
                "udp_metadata_match": bool(
                    metadata and metadata.frame_id == latest.metadata.frame_id
                ),
                "controls_received": len(captured_controls),
                "heartbeat": receiver.heartbeats.status("ai"),
                "heartbeat_stats": receiver.heartbeats.stats,
                "queue": receiver.frames.stats,
                "errors": receiver.errors,
            }


def run_receive(args: argparse.Namespace) -> dict[str, Any]:
    if args.duration > 86400:
        raise ValueError("duration cannot exceed 86400 seconds")
    with WorldBusReceiver(
        args.host,
        args.tcp_port,
        args.udp_port,
        stale_after_s=args.stale_after,
    ) as receiver:
        tcp_host, tcp_port = receiver.tcp_address
        udp_host, udp_port = receiver.udp_address
        # Emit the endpoints immediately so another terminal can connect.
        _print(
            {
                "status": "listening",
                "tcp": {"host": tcp_host, "port": tcp_port},
                "udp": {"host": udp_host, "port": udp_port},
                "duration_s": args.duration,
            }
        )
        deadline = time.monotonic() + args.duration
        received_ids: list[int] = []
        omitted_ids = 0
        while time.monotonic() < deadline:
            try:
                frame = receiver.frames.get(
                    max(0.0, min(0.25, deadline - time.monotonic()))
                )
            except TimeoutError:
                continue
            if len(received_ids) < 10000:
                received_ids.append(frame.metadata.frame_id)
            else:
                omitted_ids += 1
        return {
            "status": "complete" if not receiver.errors else "complete_with_errors",
            "received_frame_ids": received_ids,
            "omitted_frame_ids": omitted_ids,
            "queue": receiver.frames.stats,
            "heartbeats": receiver.heartbeats.snapshot(),
            "heartbeat_stats": receiver.heartbeats.stats,
            "controls": receiver.pop_controls(),
            "errors": receiver.errors,
        }


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.action == "loopback":
            payload = run_loopback(args)
        elif args.action == "replay-generate":
            interval_ns = int(args.interval_ms * 1_000_000)
            payload = write_replay(
                args.output,
                generate_replay_frames(
                    args.frames,
                    args.width,
                    args.height,
                    interval_ns=interval_ns,
                ),
                overwrite=args.overwrite,
            )
            payload["status"] = "generated"
        elif args.action == "replay-inspect":
            payload = replay_summary(args.path)
            payload["status"] = "valid"
        elif args.action == "replay-send":
            if args.tcp_port == 0:
                raise ValueError("replay-send requires a non-zero --tcp-port")
            payload = send_replay(
                args.path,
                args.host,
                args.tcp_port,
                realtime=not args.no_pacing,
                speed=args.speed,
            )
            payload["status"] = "sent"
        else:
            payload = run_receive(args)
        _print(payload)
        return 0 if payload.get("status") not in {"fail", "complete_with_errors"} else 2
    except (OSError, ValueError, WorldBusError) as exc:
        _print({"status": "error", "error": type(exc).__name__, "message": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
