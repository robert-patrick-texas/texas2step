"""Command line: ``texas2step serve`` and the discrete measurement commands."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Callable, Coroutine
from typing import Any

import httpx

from . import __version__, engine


def _fmt(value: float | None, unit: str = "", digits: int = 2) -> str:
    return "—" if value is None else f"{value:,.{digits}f}{unit}"


class _Live:
    """One-line progress on stderr when attached to a terminal."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled and sys.stderr.isatty()

    def __call__(self, phase: str, data: dict[str, Any]) -> None:
        if not self.enabled:
            return
        if "mbps" in data:
            text = f"{phase:<9} {data['mbps']:>10,.1f} Mbps   t={data['elapsed']:4.1f}s"
        elif "sample" in data:
            text = f"{phase:<9} {data['sample']:>10.2f} ms     #{data['index'] + 1}"
        else:
            text = f"{phase:<9} {data.get('sent', 0):>10} packets sent"
        sys.stderr.write("\r\033[2K  " + text)
        sys.stderr.flush()

    def clear(self) -> None:
        if self.enabled:
            sys.stderr.write("\r\033[2K")
            sys.stderr.flush()


def _latency_line(label: str, s: dict[str, Any] | None) -> str:
    if not s or s.get("median") is None:
        return f"  {label:<11} —"
    lost = f", {s['lost']} lost" if s.get("lost") else ""
    return (
        f"  {label:<11} {_fmt(s['median'], ' ms'):>14}   jitter {_fmt(s['jitter'], ' ms')}"
        f"   (min {_fmt(s['min'])} · p90 {_fmt(s['p90'])} · {s['count']} samples{lost})"
    )


def _throughput_line(label: str, t: dict[str, Any] | None, idle: float | None) -> str:
    if not t:
        return f"  {label:<11} —"
    loaded = (t.get("loaded_latency") or {}).get("median")
    added = f" · loaded {loaded - idle:+.1f} ms" if loaded is not None and idle is not None else ""
    return (
        f"  {label:<11} {_fmt(t['mbps'], ' Mbps', 1):>14}   peak {_fmt(t['peak_mbps'], '', 1)}"
        f"   ({t['streams']} streams{added})"
    )


def _loss_line(loss: dict[str, Any] | None) -> str:
    if not loss:
        return "  Loss        —"
    if "error" in loss:
        return f"  Loss        unavailable ({loss['error']})"
    rtt = loss.get("rtt") or {}
    parts = [f"{loss['lost']}/{loss['sent']} lost"]
    if loss.get("up_loss_pct") is not None:
        parts.append(f"↑ {_fmt(loss['up_loss_pct'], ' %')} ↓ {_fmt(loss['down_loss_pct'], ' %')}")
    if rtt.get("median") is not None:
        parts.append(f"UDP RTT {_fmt(rtt['median'], ' ms')} · jitter {_fmt(rtt['jitter'], ' ms')}")
    return f"  {'Loss':<11} {_fmt(loss['loss_pct'], ' %'):>14}   ({' · '.join(parts)})"


def _report(result: dict[str, Any]) -> str:
    server, client = result.get("server", {}), result.get("client", {})
    idle = (result.get("latency") or {}).get("median")
    lines = [
        f"texas2step → {server.get('url')}  ({server.get('name')} · v{server.get('version')})",
        f"client {client.get('ip')} ({client.get('family')}, {client.get('protocol')})",
        "",
        _latency_line("Latency", result.get("latency")),
        _throughput_line("Download", result.get("download"), idle),
        _throughput_line("Upload", result.get("upload"), idle),
        _loss_line(result.get("loss")),
    ]
    grade = (result.get("bufferbloat") or {}).get("grade")
    if grade:
        lines.append(f"  {'Bufferbloat':<11} {grade:>14}")
    if result.get("id"):
        lines += ["", f"saved: {server.get('url')}/r/{result['id']}"]
    return "\n".join(lines)


def _run(coro: Coroutine[Any, Any, Any]) -> Any:
    try:
        return asyncio.run(coro)
    except KeyboardInterrupt:
        sys.exit(130)
    except httpx.HTTPStatusError as exc:
        response = exc.response
        try:
            detail = response.json().get("detail") or response.reason_phrase
        except (ValueError, AttributeError, httpx.ResponseNotRead):
            detail = response.reason_phrase
        sys.exit(f"texas2step: {response.status_code} from {exc.request.url.path}: {detail}")
    except httpx.HTTPError as exc:
        sys.exit(f"texas2step: {type(exc).__name__}: {exc}")


def cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    if args.config:
        os.environ["TEXAS2STEP_CONFIG"] = os.path.abspath(args.config)
    uvicorn.run(
        "texas2step.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        workers=args.workers,
        log_level=args.log_level,
        proxy_headers=False,  # client addresses are resolved by texas2step's trusted_proxies
        ws_max_size=1 << 20,
    )


async def _save(target: str, result: dict[str, Any]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(f"{engine._base(target)}{engine.API}/results", json=result)
        r.raise_for_status()
        return r.json()


def cmd_run(args: argparse.Namespace) -> None:
    live = _Live(not args.json)
    result = _run(
        engine.run_all(
            args.url,
            seconds=args.seconds,
            streams=args.streams,
            packets=args.packets,
            interval_ms=args.interval,
            loss=not args.no_loss,
            progress=live,
        )
    )
    live.clear()
    if args.save:
        result["id"] = _run(_save(args.url, result))["id"]
    print(json.dumps(result, indent=2) if args.json else _report(result))


def _discrete(
    func: Callable[..., Coroutine[Any, Any, dict[str, Any]]],
    render: Callable[[dict[str, Any]], str],
    **kwargs: Any,
) -> Callable[[argparse.Namespace], None]:
    def command(args: argparse.Namespace) -> None:
        live = _Live(not args.json)
        options = {name: getattr(args, attr) for name, attr in kwargs.items()}
        result = _run(func(args.url, progress=live, **options))
        live.clear()
        print(json.dumps(result, indent=2) if args.json else render(result))

    return command


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="texas2step",
        description="Throughput, latency, jitter and packet-loss measurement between endpoints.",
    )
    parser.add_argument("--version", action="version", version=f"texas2step {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the web server")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument("--workers", type=int, default=1)
    serve.add_argument("--config", help="TOML settings file (see docs/configuration.md)")
    serve.add_argument("--log-level", default="info")
    serve.set_defaults(func=cmd_serve)

    def target(p: argparse.ArgumentParser) -> None:
        p.add_argument("url", help="base URL of a texas2step server")
        p.add_argument("--json", action="store_true", help="print the raw result as JSON")

    def throughput(p: argparse.ArgumentParser) -> None:
        p.add_argument("--seconds", type=float, default=10.0)
        p.add_argument("--streams", type=int, default=4)

    run = sub.add_parser("run", help="full test: latency, download, upload, packet loss")
    target(run)
    throughput(run)
    run.add_argument("--packets", type=int, default=500)
    run.add_argument("--interval", type=float, default=10.0, help="ms between UDP probes")
    run.add_argument("--no-loss", action="store_true", help="skip the UDP packet-loss test")
    run.add_argument("--save", action="store_true", help="store the result on the server")
    run.set_defaults(func=cmd_run)

    lat = sub.add_parser("latency", help="idle HTTP round-trip time and jitter")
    target(lat)
    lat.add_argument("--count", type=int, default=20)
    lat.set_defaults(
        func=_discrete(engine.measure_latency, lambda r: _latency_line("Latency", r), count="count")
    )

    for direction, func in (
        ("download", engine.measure_download),
        ("upload", engine.measure_upload),
    ):
        p = sub.add_parser(direction, help=f"{direction} throughput (multi-stream)")
        target(p)
        throughput(p)
        p.set_defaults(
            func=_discrete(
                func,
                lambda r, label=direction.title(): _throughput_line(label, r, None),
                seconds="seconds",
                streams="streams",
            )
        )

    loss = sub.add_parser("loss", help="UDP packet loss, RTT and jitter over WebRTC")
    target(loss)
    loss.add_argument("--packets", type=int, default=500)
    loss.add_argument("--interval", type=float, default=10.0, help="ms between probes")
    loss.add_argument("--size", type=int, default=64, help="probe size in bytes")
    loss.set_defaults(
        func=_discrete(
            engine.measure_loss,
            _loss_line,
            packets="packets",
            interval_ms="interval",
            size="size",
        )
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)
