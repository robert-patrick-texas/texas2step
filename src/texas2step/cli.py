"""Command line: ``texas2step serve`` and the discrete measurement commands."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from . import __version__, engine, quality


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
        return f"  {label:<14} —"
    lost = f", {s['lost']} lost" if s.get("lost") else ""
    return (
        f"  {label:<14} {_fmt(s['median'], ' ms'):>14}   jitter {_fmt(s['jitter'], ' ms')}"
        f"   (min {_fmt(s['min'])} · p90 {_fmt(s['p90'])} · {s['count']} samples{lost})"
    )


def _throughput_line(label: str, t: dict[str, Any] | None, idle: float | None) -> str:
    if not t:
        return f"  {label:<14} —"
    loaded = (t.get("loaded_latency") or {}).get("median")
    added = f" · loaded {loaded - idle:+.1f} ms" if loaded is not None and idle is not None else ""
    return (
        f"  {label:<14} {_fmt(t['mbps'], ' Mbps', 1):>14}   peak {_fmt(t['peak_mbps'], '', 1)}"
        f"   ({t['streams']} streams{added})"
    )


def _limit_line(t: dict[str, Any] | None) -> list[str]:
    """A transfer's rate limit, under its line; nothing when it had none."""
    limit = (t or {}).get("rate_limit")
    if not limit:
        return []
    moved = limit["before_bytes"] / 1e6
    before = f"{moved:,.0f} MB" if moved < 1000 else f"{moved / 1000:,.1f} GB"
    return [
        f"  {'':<14} rate limit: {_fmt(limit['from_mbps'], ' Mbps', 1)} until {limit['at_s']:g}s,"
        f" {before} in, then {_fmt(limit['to_mbps'], ' Mbps', 1)} to the end"
    ]


def _bound_line(t: dict[str, Any] | None) -> list[str]:
    """A transfer the server's CPU held back, under its line; nothing otherwise."""
    from .cpu import BOUND

    cpu = (t or {}).get("server_cpu")
    if not isinstance(cpu, int | float) or cpu < BOUND:
        return []
    return [
        f"  {'':<14} server CPU-bound: a worker ran at {cpu * 100:.0f}% of a core, so the "
        "server, not the network, set this rate"
    ]


def _one_stream_line(result: dict[str, Any]) -> list[str]:
    """One stream's download, beside the multi-stream rate; nothing when not measured."""
    one, multi = result.get("one_stream"), result.get("download") or {}
    if not one:
        return []
    share = (
        f" · {one['mbps'] / multi['mbps'] * 100:.0f} % of {multi['streams']} streams"
        if multi.get("mbps")
        else ""
    )
    return [
        f"  {'One stream':<14} {_fmt(one['mbps'], ' Mbps', 1):>14}"
        f"   peak {_fmt(one['peak_mbps'], '', 1)}   (a single file copy{share})",
        *_limit_line(one),
        *_bound_line(one),
    ]


def _loss_line(loss: dict[str, Any] | None) -> str:
    if not loss:
        return f"  {'Loss':<14} —"
    if "error" in loss:
        return f"  {'Loss':<14} unavailable ({loss['error']})"
    rtt = loss.get("rtt") or {}
    parts = [f"{loss['lost']}/{loss['sent']} lost"]
    if loss.get("up_loss_pct") is not None:
        parts.append(f"↑ {_fmt(loss['up_loss_pct'], ' %')} ↓ {_fmt(loss['down_loss_pct'], ' %')}")
    if rtt.get("median") is not None:
        parts.append(f"UDP RTT {_fmt(rtt['median'], ' ms')} · jitter {_fmt(rtt['jitter'], ' ms')}")
    return f"  {'Loss':<14} {_fmt(loss['loss_pct'], ' %'):>14}   ({' · '.join(parts)})"


def _rated(side: dict[str, Any]) -> str:
    """A call's rating, and why when a gap a caller would hear lowered it."""
    rated = side["rating"]
    if rated != side.get("mos_rating", rated):
        rated += f", for its gap (the score alone: {side['mos_rating']})"
    return rated


def _call_line(call: dict[str, Any] | None) -> str:
    if not call:
        return f"  {'Call quality':<14} —"
    up, down = call.get("up"), call.get("down")
    if up and down:  # each direction timed with the server's stamps
        detail = (
            f"you hear {down['mos']:.1f} · they hear {up['mos']:.1f}"
            f" · longest gap ↓ {down['longest_gap_ms']:g} ↑ {up['longest_gap_ms']:g} ms"
            f" · late ↓ {_fmt(down['late_pct'], ' %')} ↑ {_fmt(up['late_pct'], ' %')}"
        )
    else:
        detail = (
            f"late {_fmt(call['late_pct'], ' %')} · longest gap {call['longest_gap_ms']:g} ms"
            " · from round trips"
        )
    return (
        f"  {'Call quality':<14} {_fmt(call['mos'], ' MOS', 1):>14}   {_rated(call)}   ({detail})"
    )


def _rpm_line(rpm: dict[str, Any] | None) -> str:
    if not rpm:
        return f"  {'Responsiveness':<14} —"
    detail = f"↓ {_fmt(rpm['download_rpm'], '', 0)} · ↑ {_fmt(rpm['upload_rpm'], '', 0)}"
    setups = [rpm[d]["setup_ms"] for d in ("download", "upload") if rpm.get(d)]
    if setups:  # the IETF draft's method, with new connections
        idle = rpm.get("idle_setup_ms")
        opened = f"{_fmt(idle, ' ms', 1)} idle, " if idle is not None else ""
        detail += f" · a new connection {opened}{_fmt(max(setups), ' ms', 1)} under load"
    else:
        detail += ", under load"
    return f"  {'Responsiveness':<14} {rpm['rpm']:>10,} RPM   ({detail})"


def _report(result: dict[str, Any]) -> str:
    server, client = result.get("server", {}), result.get("client", {})
    idle = (result.get("latency") or {}).get("median")
    # A server keeps its version from addresses outside internal_networks: then none is said.
    version = f" · v{server['version']}" if server.get("version") else ""
    lines = [
        f"texas2step → {server.get('url')}  ({server.get('name')}{version})",
        f"client {client.get('ip')} ({client.get('family')}, {client.get('protocol')})",
        "",
        _latency_line("Latency", result.get("latency")),
        _throughput_line("Download", result.get("download"), idle),
        *_limit_line(result.get("download")),
        *_bound_line(result.get("download")),
        *_one_stream_line(result),
        _throughput_line("Upload", result.get("upload"), idle),
        *_limit_line(result.get("upload")),
        *_bound_line(result.get("upload")),
        _loss_line(result.get("loss")),
    ]
    if client.get("local_ip"):
        mac = f" · MAC {client['mac']}" if client.get("mac") else ""
        lines.insert(2, f"this computer {client['local_ip']}{mac}")
    lines += [_call_line(result.get("call")), _rpm_line(result.get("responsiveness"))]
    grade = (result.get("bufferbloat") or {}).get("grade")
    if grade:
        lines.append(f"  {'Bufferbloat':<14} {grade:>14}")
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

    from . import logs
    from .access import Codes
    from .config import load_settings

    if args.config:
        os.environ["TEXAS2STEP_CONFIG"] = os.path.abspath(args.config)
    # Each worker loads the settings again; this read is for logging, set up before them,
    # and to stop here, with one line, when the access key or word list can't be used.
    try:
        settings = load_settings()
        Codes.load(settings)
    except (OSError, ValueError) as exc:
        sys.exit(f"texas2step: {exc}")
    debug = settings.debug
    uvicorn.run(
        "texas2step.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        workers=args.workers,
        log_config=logs.config(debug),
        log_level=args.log_level,
        access_log=False,  # app.RequestLog writes each request, with the client's address
        proxy_headers=False,  # client addresses are resolved by texas2step's trusted_proxies
        ws_max_size=1 << 20,
    )


def _show_diff(path: Path, current: str, new: str, label: str) -> None:
    import difflib

    diff = difflib.unified_diff(
        current.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=str(path),
        tofile=f"{path} ({label})",
    )
    sys.stdout.writelines(diff)
    print()


def _write_settings(path: Path, text: str) -> None:
    """Replace ``path`` with ``text`` atomically, keeping a dated backup and its owner."""
    import shutil
    import time

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.{stamp}.bak")
    n = 1
    while backup.exists():  # two edits in one second must not lose the first backup
        n += 1
        backup = path.with_name(f"{path.name}.{stamp}-{n}.bak")
    tmp = path.with_name(path.name + ".tmp")
    try:
        if path.exists():
            shutil.copy2(path, backup)
        tmp.write_text(text)
        if path.exists():
            shutil.copymode(path, tmp)
            st = path.stat()
            if os.geteuid() == 0:
                os.chown(tmp, st.st_uid, st.st_gid)
        os.replace(tmp, path)
    except OSError as exc:
        sys.exit(f"texas2step: {exc}")
    print(f"Wrote {path}" + (f"; the previous file is {backup}." if backup.exists() else "."))


def _settings_text(path: Path) -> str:
    """The settings file, or this version's template when there is none yet."""
    from . import config_merge

    try:
        return path.read_text() if path.exists() else config_merge.TEMPLATE.read_text()
    except OSError as exc:
        sys.exit(f"texas2step: {exc}")


def cmd_config_merge(args: argparse.Namespace) -> None:
    import tomllib

    from . import config_merge

    path = Path(args.config)
    source = Path(args.source) if args.source else path
    try:
        merged = config_merge.merge(source.read_text(), config_merge.TEMPLATE.read_text())
        current = path.read_text() if path.exists() else ""
    except (OSError, tomllib.TOMLDecodeError) as exc:
        sys.exit(f"texas2step: {exc}")
    except (ValueError, RuntimeError) as exc:
        sys.exit(f"texas2step: the merged settings would not load: {exc}\nNothing written.")

    if merged.text == current:
        print(f"{path} is up to date with this version's template.")
        return
    if not args.write:
        _show_diff(path, current, merged.text, "merged")
    print(f"Kept {len(merged.kept)} setting(s): {', '.join(merged.kept) or 'none'}")
    if merged.renamed:
        print(f"Renamed: {', '.join(merged.renamed)}")
    if merged.retired:
        print(f"No longer used, kept as comments: {', '.join(merged.retired)}")
    if not args.write:
        print("Nothing written. To apply: sudo texas2step config merge --write")
        return

    _write_settings(path, merged.text)
    for leftover in (
        path.with_name(path.name + ".dpkg-dist"),
        path.with_name(path.name + ".dpkg-old"),
    ):
        if leftover.exists():
            print(f"{leftover} is no longer needed and can be deleted.")
    print("Apply the settings: sudo systemctl restart texas2step")


def _apply_edit(args: argparse.Namespace, edit: Callable[[str], Any], again: str) -> None:
    """Show or write one config_edit change to the settings file."""
    import tomllib

    path = Path(args.config)
    current = _settings_text(path)
    try:
        edited = edit(current)
    except tomllib.TOMLDecodeError as exc:
        sys.exit(f"texas2step: {path}: {exc}")
    except (ValueError, RuntimeError) as exc:
        sys.exit(f"texas2step: {exc}\nNothing written.")
    if not edited.changes:
        print(f"{path} unchanged: {'; '.join(edited.notes)}.")
        return
    if not args.write:
        _show_diff(path, current, edited.text, "edited")
    for change in edited.changes:
        print(f"  {change}")
    if not args.write:
        print(f"Nothing written. To apply: sudo {again} --write")
        return
    _write_settings(path, edited.text)
    print("Apply the settings: sudo systemctl restart texas2step")


def cmd_config_set(args: argparse.Namespace) -> None:
    import shlex

    from . import config_edit

    values: dict[str, Any] = {}
    for pair in args.pairs:
        name, sep, raw = pair.partition("=")
        if not sep:
            sys.exit(f"texas2step: expected KEY=VALUE, got {pair!r}")
        try:
            values[name.strip()] = config_edit.parse_value(name.strip(), raw.strip())
        except ValueError as exc:
            sys.exit(f"texas2step: {exc}")
    again = shlex.join(["texas2step", "config", "set", *args.pairs])
    _apply_edit(args, lambda text: config_edit.set_values(text, values), again)


def _fetch_peer(url: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """What a texas2step server at ``url`` says about itself: the ``server`` part of
    /api/v1/info, and its own entry in /api/v1/locations, which carries its IPv4-only
    and IPv6-only URLs ({} from a server that doesn't list them)."""
    import ssl

    # The system's trust store, so servers with certificates from an internal CA (added
    # with update-ca-certificates) are trusted, as browsers on managed machines trust them.
    verify = ssl.create_default_context()
    base = f"{engine._base(url)}{engine.API}"
    with httpx.Client(timeout=5, follow_redirects=True, verify=verify) as client:
        r = client.get(f"{base}/info")
        r.raise_for_status()
        server = r.json().get("server") or {}
        try:
            r = client.get(f"{base}/locations")
            r.raise_for_status()
            listed = r.json().get("locations") or []
        except (httpx.HTTPError, ValueError):
            listed = []
    own = next((loc for loc in listed if loc.get("id") == server.get("id")), {})
    return server, own


def cmd_config_add_location(args: argparse.Namespace) -> None:
    import shlex

    from . import config_edit
    from .config import PROTOCOL_URLS

    location = {
        "id": args.id,
        "name": args.name,
        "url": args.url,
        "location": args.location,
        "url_v4": args.url_v4,
        "url_v6": args.url_v6,
    }
    basics = list(location)
    location |= {key: getattr(args, key) for key in PROTOCOL_URLS}
    if not all(location[key] for key in basics):
        try:
            server, own = _fetch_peer(args.url)
        except (httpx.HTTPError, ValueError) as exc:
            if not (args.id and args.name):
                sys.exit(
                    f"texas2step: can't ask {args.url} who it is ({type(exc).__name__}: {exc}).\n"
                    "Check the URL, or give --id and --name (and --location) yourself."
                )
            server, own = {}, {}
            print(f"Couldn't reach {args.url}; adding it with what was given.")
        for key in ("id", "name", "location"):
            location[key] = location[key] or server.get(key) or ""
        # Its single-family URLs, so the IP drop-down works for it here too, and those that
        # serve one HTTP version, for Diag's rows.
        for key in ("url_v4", "url_v6", *PROTOCOL_URLS):
            found = str(own.get(key) or "")
            if not location[key] and found.startswith(("https://", "http://")):
                location[key] = found
    again = shlex.join(
        ["texas2step", "config", "add-location", args.url]
        + [f"--{k.replace('_', '-')}={v}" for k, v in location.items() if v and k != "url"]
    )
    _apply_edit(args, lambda text: config_edit.add_location(text, location), again)


def _proxy(value: str) -> str:
    from .config import proxy_problem

    value = value.strip()
    if why := proxy_problem(value):
        raise argparse.ArgumentTypeError(why)
    return value


def cmd_update(args: argparse.Namespace) -> None:
    from . import update
    from .config import load_settings

    if args.auto and (
        args.check
        or args.release
        or args.allow_downgrade
        or args.source
        or args.proxy
        or args.insecure
    ):
        sys.exit("texas2step update: --auto takes no other options (its proxy: update_proxy)")
    if args.source and (args.proxy or args.insecure):
        sys.exit("texas2step update: --from downloads nothing; leave out --proxy and --insecure")
    if args.release and args.source:
        sys.exit("texas2step update: --from installs the release in DIR; leave out --version")
    if args.allow_downgrade and not (args.release or args.source):
        sys.exit("texas2step update: --allow-downgrade goes with --version or --from")
    try:
        # A server without a settings file still has update_url's default (or its env).
        settings = load_settings(args.config if os.path.exists(args.config) else None)
    except (OSError, ValueError) as exc:
        sys.exit(f"texas2step: {exc}")
    try:
        sys.exit(update.main(args, settings))
    except update.UpdateError as exc:
        sys.exit(f"texas2step update: {exc}")
    except KeyboardInterrupt:
        sys.exit(130)


def _server_settings(args: argparse.Namespace) -> Any:
    from .config import load_settings

    try:
        # A server without a settings file still has the defaults (or its environment).
        return load_settings(args.config if os.path.exists(args.config) else None)
    except (OSError, ValueError) as exc:
        sys.exit(f"texas2step: {exc}")


def _access_codes(settings: Any) -> Any:
    """The server's codes; while they are off, the daily ones it would give."""
    from . import access

    interval = settings.access_code if settings.access_code != "off" else "daily"
    try:
        return access.Codes.load(settings, interval)
    except access.AccessError as exc:
        hint = " (try sudo)" if "Permission denied" in str(exc) else ""
        sys.exit(f"texas2step: {exc}{hint}")


def _when(moment: Any) -> str:
    from datetime import UTC, datetime

    left = int((moment - datetime.now(UTC)).total_seconds() // 60)
    days, hours, minutes = left // 1440, left % 1440 // 60, left % 60
    span = f"{days} d {hours} h" if days else f"{hours} h {minutes} min"
    return f"{moment:%a %Y-%m-%d %H:%M %Z}, in {span}"


# What each interval calls the current and the previous code.
CODE_NAMES = {
    "daily": ("Today's code", "Yesterday's"),
    "weekly": ("This week's code", "Last week's"),
    "monthly": ("This month's code", "Last month's"),
    "fixed": ("The code", ""),
}


def cmd_access_show(args: argparse.Namespace) -> None:
    from datetime import UTC, datetime

    settings = _server_settings(args)
    codes = _access_codes(settings)
    state = _access_state(settings)
    current = codes.current(datetime.now(UTC), state.shuffle, state.shuffled_at)
    this, last = CODE_NAMES[codes.interval]
    if settings.access_code == "off":
        print('Access codes are off (access_code = "off"). Were they daily, the code would be:')
    elif codes.interval == "fixed":
        print("Access codes: fixed. The code changes only when shuffled or with a new key.")
    else:
        starts = {"daily": "", "weekly": " on Mondays", "monthly": " on the 1st"}[codes.interval]
        print(f"Access codes: {codes.interval}, changing at midnight{starts}, {codes.zone_name}.")
    rows = [
        (
            this,
            current.code
            + (f"   (until {_when(current.period.end)})" if current.period.end else ""),
        )
    ]
    if current.previous and current.previous_until:
        rows.append(
            (last, f"{current.previous}   (still works until {_when(current.previous_until)})")
        )
    words = (
        "from " + settings.access_words if codes.site else "drawn from the built-in list by the key"
    )
    standing = [
        p.name + (f" ({_phrase_when(p, settings.access_timezone)})" if p.until else "")
        for p in codes.phrases
    ]
    zone = settings.access_timezone
    if state.shuffle:
        rows.append(("Shuffled", f"{_moment(state.shuffled_at, zone)}, {state.shuffle_source}"))
    lasting = f"{settings.access_pass_hours:g} h from when each is issued"
    if state.ended:
        lasting += f"; those issued before {_moment(state.ended, zone)} are ended"
    fingerprint = codes.fingerprint(state.shuffle)
    rows += [
        ("Passes", lasting),
        ("Words", f"{len(codes.words)} {words}"),
        ("Phrases", ", ".join(standing) or "none"),
        ("Key", settings.access_key_file),
        ("Fingerprint", f"{fingerprint}   (the same on servers that give the same codes)"),
    ]
    width = max(len(label) for label, _ in rows) + 2
    for label, value in rows:
        print(f"{label + ':':<{width}}{value}")
    if state.unread:
        print(
            f"\nCan't read {settings.access_lockouts_file} ({state.unread}), so the code shown "
            "leaves out any shuffle (try sudo)."
        )
    if settings.access_code != "off" and not settings.internal_networks:
        print(
            "\nNo internal_networks are set, so every visitor needs a code, and anyone with "
            "one can read saved results. See docs/access.md."
        )


def cmd_access_words(args: argparse.Namespace) -> None:
    codes = _access_codes(_server_settings(args))
    print("\n".join(codes.words))


def cmd_access_key(args: argparse.Namespace) -> None:
    from . import access

    path = Path(_server_settings(args).access_key_file)
    if not (args.new or args.init or args.import_):
        try:
            print(path.read_text().strip())
        except OSError as exc:
            hint = " (try sudo)" if isinstance(exc, PermissionError) else ""
            sys.exit(f"texas2step: can't read {path}: {exc.strerror}{hint}")
        return
    if args.init and path.exists():
        return  # the package's postinst: an existing key stays
    if args.import_:
        text = sys.stdin.read().strip()
        try:
            access.parse_key(text)
        except access.AccessError as exc:
            sys.exit(f"texas2step: {exc}")
    else:
        text = access.new_key()
    if path.exists() and not args.yes and not args.import_:
        if not sys.stdin.isatty():
            sys.exit("texas2step: there is a key already; add --yes to replace it")
        answer = input(f"Replace {path}? Codes and passes from the old key stop working. [y/N] ")
        if answer.strip().lower() not in {"y", "yes"}:
            sys.exit("Nothing changed.")
    try:
        access.write_key(path, text)
    except OSError as exc:
        hint = " (try sudo)" if isinstance(exc, PermissionError) else ""
        sys.exit(f"texas2step: can't write {path}: {exc.strerror}{hint}")
    if args.new:
        print(f"Wrote {path}. Copy it to the organization's other servers, then on each:")
        print("  sudo systemctl restart texas2step")
    elif args.import_:
        print(f"Wrote {path}. Then: sudo systemctl restart texas2step")


def _phrase_when(phrase: Any, zone: str) -> str:
    """A phrase's until day, as "until 2026-12-31" or "expired 2026-12-31"; "" without."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    if phrase.until is None:
        return ""
    today = datetime.now(ZoneInfo(zone)).date()
    return f"{'until' if today <= phrase.until else 'expired'} {phrase.until}"


def _read_phrases(path: Path) -> list[Any]:
    from . import access

    try:
        return access.read_phrases(path)
    except access.AccessError as exc:
        hint = " (try sudo)" if "Permission denied" in str(exc) else ""
        sys.exit(f"texas2step: {exc}{hint}")


def _write_phrases(path: Path, text: str, copy: bool = True) -> None:
    from . import access

    try:
        access.write_secret(path, text)
    except OSError as exc:
        hint = " (try sudo)" if isinstance(exc, PermissionError) else ""
        sys.exit(f"texas2step: can't write {path}: {exc.strerror}{hint}")
    if copy:
        print(f"Wrote {path}. To copy it to the organization's other servers, on each:")
        print(f"  ssh <this server> sudo cat {path} | sudo texas2step access phrase import")
        print("Then restart the service here and on each: sudo systemctl restart texas2step")
    else:
        print(f"Wrote {path}. Then: sudo systemctl restart texas2step")


def cmd_access_phrase_list(args: argparse.Namespace) -> None:
    settings = _server_settings(args)
    path = Path(settings.access_phrases_file)
    phrases = _read_phrases(path)
    if not phrases:
        print(f"No standing phrases in {path}." if path.exists() else f"There is no {path}.")
        return
    print(f"{path}:")
    names = max(len(p.name) for p in phrases) + 3
    texts = max(len(p.text) for p in phrases) + 3
    for p in phrases:
        when = _phrase_when(p, settings.access_timezone)
        print(f"  {p.name:<{names}}{p.text:<{texts}}{when}".rstrip())
    if settings.access_code == "off":
        print('\nAccess codes are off (access_code = "off"), so no visitor is asked for one.')


def cmd_access_phrase_add(args: argparse.Namespace) -> None:
    from . import access

    path = Path(_server_settings(args).access_phrases_file)
    phrases = _read_phrases(path)
    if any(p.name.lower() == args.name.lower() for p in phrases):
        sys.exit(f"texas2step: there is a phrase named {args.name} already; remove it first")
    text = args.phrase
    if text is None:  # kept out of the shell's history
        text = input(f"Phrase for {args.name}: ") if sys.stdin.isatty() else sys.stdin.readline()
    try:
        added = access.Phrase(args.name, text.strip(), access.as_day(args.until))
        checked = access.parse_phrases(access.render_phrases([*phrases, added]), str(path))
    except (access.AccessError, ValueError) as exc:
        sys.exit(f"texas2step: {exc}")
    _write_phrases(path, access.render_phrases(checked))


def cmd_access_phrase_remove(args: argparse.Namespace) -> None:
    from . import access

    path = Path(_server_settings(args).access_phrases_file)
    phrases = _read_phrases(path)
    kept = [p for p in phrases if p.name.lower() != args.name.lower()]
    if len(kept) == len(phrases):
        sys.exit(f"texas2step: there is no phrase named {args.name} in {path}")
    _write_phrases(path, access.render_phrases(kept))


def cmd_access_phrase_import(args: argparse.Namespace) -> None:
    from . import access

    path = Path(_server_settings(args).access_phrases_file)
    text = sys.stdin.read()
    try:
        access.parse_phrases(text, "the phrases given")
    except access.AccessError as exc:
        sys.exit(f"texas2step: {exc}")
    _write_phrases(path, text, copy=False)


def _lockouts(settings: Any) -> Any:
    """The service's lockout file; it is made, as the service's, if there is none yet."""
    import sqlite3

    from .lockouts import LockoutStore

    path = Path(settings.access_lockouts_file)
    try:
        return LockoutStore(path, settings.access_lockout_after, settings.access_lockout_minutes)
    except (OSError, sqlite3.Error) as exc:
        denied = isinstance(exc, PermissionError) or "readonly" in str(exc) or "open" in str(exc)
        sys.exit(f"texas2step: can't use {path}: {exc}" + (" (try sudo)" if denied else ""))


@dataclass
class AccessState:
    """What the command line and other servers set in access_lockouts_file, for show."""

    shuffle: str = ""
    shuffled_at: float = 0.0
    shuffle_source: str = ""
    ended: float = 0.0  # when every pass was last ended
    unread: str = ""  # why the file couldn't be read


def _access_state(settings: Any) -> AccessState:
    import sqlite3

    from .lockouts import LockoutStore

    path = Path(settings.access_lockouts_file)
    if not path.exists():
        return AccessState()
    try:
        store = LockoutStore(path)
    except (OSError, sqlite3.Error) as exc:
        return AccessState(unread=str(exc))
    try:
        shuffle, at, source = store.codes_shuffled()
        return AccessState(shuffle, at, source, store.passes_ended()[0])
    finally:
        store.close()


def _moment(stamp: float, zone: str) -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    return f"{datetime.fromtimestamp(stamp, ZoneInfo(zone)):%Y-%m-%d %H:%M %Z}"


def cmd_access_lockouts(args: argparse.Namespace) -> None:
    from .lockouts import WRONG_CODES, parse_network

    settings = _server_settings(args)
    if args.clear:
        try:
            target = None if args.clear == "all" else str(parse_network(args.clear))
        except ValueError as exc:
            sys.exit(f"texas2step: {exc}")
        store = _lockouts(settings)
        cleared = store.clear(target)
        store.close()
        here = " here" if settings.access_share_lockouts else ""
        print(f"Ended: {', '.join(cleared)}." if cleared else f"No lockout{here} overlaps that.")
        _share(settings, "clear", target or "all")
        if cleared:
            _hook(settings, "clear", target or "all", None, "ended from the command line")
        return
    if not Path(settings.access_lockouts_file).exists():
        print("No networks are locked out.")
        return
    store = _lockouts(settings)
    locked, counting = store.current()
    store.close()
    zone = settings.access_timezone
    if not locked:
        print("No networks are locked out.")
    else:
        print("Locked out (the right code is refused too, until the end, which each try moves):")
        rows = []
        for x in locked:
            why = f"{x.failures} wrong codes in a row" if x.source == WRONG_CODES else x.source
            rows.append((x.network, _moment(x.locked_at, zone), _moment(x.expires, zone), why))
        widths = [max(len(row[i]) for row in rows) for i in range(3)]
        print(f"  {'Network':<{widths[0]}}   {'Since':<{widths[1]}}   {'Until':<{widths[2]}}   Why")
        for row in rows:
            print(
                "  " + "   ".join(f"{cell:<{w}}" for cell, w in zip(row, [*widths, 0], strict=True))
            )
    if counting:
        print(f"{counting} more network{'s' if counting > 1 else ''} with wrong codes in a row.")
    if settings.access_code == "off":
        print('Access codes are off (access_code = "off"), so lockouts have no effect.')


def _share(
    settings: Any,
    action: str,
    network: str,
    minutes: float = 0,
    kind: str = "block",
    at: float = 0,
    value: str = "",
) -> None:
    """Send a block, a clear, an end of passes or a shuffle to the other servers too, with
    access_share_lockouts on. A block from here is set by hand, so it ends passes there
    too."""
    from . import cluster

    if not settings.access_share_lockouts:
        return
    key = cluster.peer_key(_access_codes(settings).key)
    results = _run(cluster.send(settings, key, action, network, minutes, kind, at, value))
    print(f"Other servers: {cluster.summary(results)}.")


def _hook(settings: Any, event: str, network: str, until: Any, why: str) -> None:
    from . import cluster

    if said := cluster.run_hook_now(settings, event, network, until, why):
        print(said + ".")


def cmd_access_block(args: argparse.Namespace) -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from . import cluster
    from .lockouts import inside_of, parse_network

    settings = _server_settings(args)
    try:
        net = parse_network(args.network)
    except ValueError as exc:
        sys.exit(f"texas2step: {exc}")
    if inside := inside_of(net, settings.internal_networks):
        sys.exit(f"texas2step: {net} overlaps {inside}, which is never locked out")
    if wide := cluster.too_wide(net, cluster.WIDEST_BY_HAND):
        sys.exit(f"texas2step: {wide}; block smaller networks")
    if args.minutes is not None and not 0 < args.minutes <= 525_600:
        sys.exit("texas2step: --minutes: expected more than 0, up to 525600 (a year)")
    minutes = args.minutes or settings.access_lockout_minutes
    store = _lockouts(settings)
    why = "blocked from the command line"
    block = store.block(str(net), minutes, why)
    store.close()
    until = _moment(block.expires, settings.access_timezone)
    print(f"{net} is locked out until {until}, longer if it keeps trying.")
    if settings.access_code == "off":
        print('Access codes are off (access_code = "off"), so it has no effect until they are on.')
    if settings.access_share_lockouts and (wide := cluster.too_wide(net, cluster.WIDEST)):
        print(f"Not sent to the other servers: {wide}, the most they take.")
    else:
        _share(settings, "block", str(net), min(minutes, cluster.MOST_MINUTES))
    ends = datetime.fromtimestamp(block.expires, ZoneInfo(settings.access_timezone))
    _hook(settings, "block", str(net), ends, why)


def cmd_access_passes(args: argparse.Namespace) -> None:
    import time

    settings = _server_settings(args)
    zone = settings.access_timezone
    if args.end:
        if not args.yes:
            if not sys.stdin.isatty():
                sys.exit("texas2step: add --yes to end every pass")
            answer = input(
                "End every pass issued so far? Everyone testing from outside enters the "
                "code again. [y/N] "
            )
            if answer.strip().lower() not in {"y", "yes"}:
                sys.exit("Nothing changed.")
        store = _lockouts(settings)
        at = store.end_passes(time.time(), "from the command line")
        store.close()
        print(
            f"Every pass issued before {_moment(at, zone)} is ended here, within seconds; "
            "tests running with one stop too."
        )
        if settings.access_code == "off":
            print('Access codes are off (access_code = "off"), so there are no passes now.')
        if settings.access_share_lockouts:
            _share(settings, "end-passes", "", at=at)
        elif any(loc.url for loc in settings.locations):
            print(
                "The other servers in [[locations]] still take them: run this there too, "
                "or turn on access_share_lockouts (docs/access.md)."
            )
        return
    hours = f"{settings.access_pass_hours:g}"
    print(f"A pass lasts {hours} h from when it is issued here (access_pass_hours).")
    ended, source = (0.0, "")
    if Path(settings.access_lockouts_file).exists():
        store = _lockouts(settings)
        ended, source = store.passes_ended()
        store.close()
    if ended:
        print(f"Every pass issued before {_moment(ended, zone)} is ended, {source}.")
    else:
        print("No passes have been ended.")


def cmd_access_shuffle(args: argparse.Namespace) -> None:
    import secrets
    import time
    from datetime import UTC, datetime

    from . import access

    settings = _server_settings(args)
    codes = _access_codes(settings)
    if args.to is not None and not access.SHUFFLE.fullmatch(args.to):
        sys.exit("texas2step: --to: expected the 16 letters and digits another server printed")
    if args.to is None and not args.yes:
        if not sys.stdin.isatty():
            sys.exit("texas2step: add --yes to shuffle the codes")
        answer = input(
            "Shuffle the codes? The current and previous codes stop working, and the desk "
            "reads the new one. [y/N] "
        )
        if answer.strip().lower() not in {"y", "yes"}:
            sys.exit("Nothing changed.")
    store = _lockouts(settings)
    value, at = store.shuffle_codes(args.to or secrets.token_hex(8), time.time(), "by hand")
    ended = store.end_passes(at, "by hand, with a shuffle") if args.end_passes else 0.0
    store.close()
    current = codes.current(datetime.now(UTC), value, at)
    this = CODE_NAMES[codes.interval][0]
    print(f"Codes shuffled here. {this} is now {current.code}; the codes before it don't work.")
    if settings.access_code == "off":
        print('Access codes are off (access_code = "off"), so no code is asked for now.')
    if ended:
        print("Every pass issued so far is ended too, within seconds.")
    else:
        print("Passes already given keep working: add --end-passes to end them as well.")
    if settings.access_share_lockouts:
        _share(settings, "shuffle", "", at=at, value=value)
        if ended:
            _share(settings, "end-passes", "", at=ended)
    elif args.to is None and any(loc.url for loc in settings.locations):
        also = " --end-passes" if ended else ""
        print(
            "The other servers in [[locations]] still give the codes before it. On each:\n"
            f"  sudo texas2step access shuffle --to {value}{also}"
        )


def cmd_access_peers(args: argparse.Namespace) -> None:
    from . import cluster

    settings = _server_settings(args)
    state = "on" if settings.access_share_lockouts else "off (access_share_lockouts = false)"
    print(f"Sharing lockouts: {state}.")
    takes = ", ".join(settings.access_peer_networks) or "nowhere (access_peer_networks is empty)"
    print(f"Takes them from: {takes}.")
    if not any(loc.url for loc in settings.locations):
        print("No other servers in [[locations]] to check.")
        return
    key = cluster.peer_key(_access_codes(settings).key)
    results = _run(cluster.send(settings, key, "check", ""))
    print("A signed check, which changes nothing, to each server in [[locations]]:")
    width = max(len(peer) for peer, _ in results)
    for peer, why in results:
        print(f"  {peer:<{width}}   {'takes them from this server' if why is None else why}")
    if any(why for _, why in results):
        sys.exit(1)


def cmd_access_phrase_init(args: argparse.Namespace) -> None:
    from . import access

    path = Path(_server_settings(args).access_phrases_file)
    if path.exists():
        return  # the package's postinst: an existing file stays
    try:
        access.write_secret(path, access.render_phrases([]))
    except OSError as exc:
        hint = " (try sudo)" if isinstance(exc, PermissionError) else ""
        sys.exit(f"texas2step: can't write {path}: {exc.strerror}{hint}")
    print(f"Wrote {path}, with two examples commented out.")


def _traffic(settings: Any) -> Any:
    """The service's traffic file; it is made, as the service's, if there is none yet."""
    import sqlite3

    from .traffic import TrafficMeter

    try:
        return TrafficMeter.of(settings)
    except (OSError, sqlite3.Error) as exc:
        denied = isinstance(exc, PermissionError) or "readonly" in str(exc) or "open" in str(exc)
        sys.exit(
            f"texas2step: can't use {settings.traffic_file}: {exc}"
            + (" (try sudo)" if denied else "")
        )


def _left(periods: Any) -> list[str]:
    """Today's and this month's figures, as lines of a table."""
    from .traffic import size

    rows = [("", "Counted", "Allowed", "Left")]
    for name, p in zip(("Today", "This month"), periods, strict=True):
        if p.allowance is None:
            rows.append((name, size(p.counted), "no limit", ""))
        else:
            left = p.allowance - p.counted
            rows.append(
                (name, size(p.counted), size(p.allowance), size(left) if left > 0 else "none")
            )
    widths = [max(len(row[i]) for row in rows) for i in range(4)]
    return [
        "  " + "   ".join(f"{c:<{w}}" for c, w in zip(row, widths, strict=True)).rstrip()
        for row in rows
    ]


def _state(refusal: Any, settings: Any) -> str:
    """Whether tests are refused, why and until when, and what visitors read."""
    if refusal is None:
        return "Tests are open."
    url = settings.traffic_url
    link = f" Click here to continue. (links to {url})" if url else ""
    return (
        f"New tests are refused: {refusal.reason}.\nVisitors read: {settings.traffic_message}{link}"
    )


def cmd_traffic(args: argparse.Namespace) -> None:
    from .traffic import size

    settings = _server_settings(args)
    limited = settings.traffic_limited
    if not limited:
        print("No traffic limit is set (traffic_day_gb, traffic_month_gb), so nothing is counted.")
        if not Path(settings.traffic_file).exists():
            return
    meter = _traffic(settings)
    try:
        periods = meter.periods()
        refusal = meter.refusal(*periods)
        days = meter.days(args.days)
    finally:
        meter.close()
    zone = settings.access_timezone
    if limited:
        day_gb, month_gb = settings.traffic_day_gb, settings.traffic_month_gb
        day = f"{day_gb:,g} GB a day" if day_gb else "no daily limit"
        month = f"{month_gb:,g} GB a month" if month_gb else "no monthly limit"
        what = {"sent": "what the server sends", "received": "what it receives"}
        counts = what.get(settings.traffic_counts, "what the server sends and receives")
        inside = "included" if settings.traffic_count_internal else "not counted"
        print(f"Traffic limit: {day}, {month}.")
        print(f"It counts {counts}; internal_networks {inside}.")
        print(f"Days and months begin at midnight in {zone}.")
        print()
        print("\n".join(_left(periods)))
        print()
        print(_state(refusal, settings))
    if not days:
        return
    print()
    rows = [("Day", "Sent", "Received", "Internal sent", "Internal received", "Allowed by hand")]
    for key, u in days:
        figures = (u.sent, u.received, u.inside_sent, u.inside_received)
        rows.append((key, *(size(n) for n in figures), size(u.extra) if u.extra else ""))
    if not any(row[5] for row in rows[1:]):
        rows = [row[:5] for row in rows]
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    for row in rows:
        print("  " + "   ".join(f"{c:<{w}}" for c, w in zip(row, widths, strict=True)).rstrip())


def cmd_traffic_allow(args: argparse.Namespace) -> None:
    from .traffic import size

    settings = _server_settings(args)
    if not settings.traffic_limited:
        sys.exit("texas2step: no traffic limit is set (traffic_day_gb, traffic_month_gb)")
    if not args.gb or abs(args.gb) > 1_000_000:
        sys.exit("texas2step: expected a number of gigabytes, such as 5, or -5 to take it back")
    meter = _traffic(settings)
    try:
        periods = meter.allow(args.gb)
        refusal = meter.refusal(*periods)
    finally:
        meter.close()
    more = "more" if args.gb > 0 else "less"
    print(f"Allowed {size(abs(args.gb) * 1e9)} {more} today, and so this month.")
    print()
    print("\n".join(_left(periods)))
    print()
    print(_state(refusal, settings))


CLIENTS_ENDED_SHOWN = 20  # ended sessions texas2step clients lists without --history


def _table(rows: list[tuple[str, ...]]) -> list[str]:
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    return [
        "  " + "   ".join(f"{c:<{w}}" for c, w in zip(row, widths, strict=True)).rstrip()
        for row in rows
    ]


def _clients_table(board: Any, now: float, limit: int, everything: bool) -> list[str]:
    """The active sessions, then the last hour's ended ones, as tables; an address after a
    session's first gets its own line."""
    from .clients import HISTORY, IDLE, PAGES, describe, duration, program
    from .traffic import size

    def session(s: Any, what: str, took: float) -> list[tuple[str, ...]]:
        addresses = s.addresses or ["unknown"]
        page = PAGES.get(s.page or "", "-")
        first = (s.label, addresses[0], program(s.agent), page, what, duration(took))
        return [(*first, size(s.sent + s.received))] + [
            ("", ip, "", "", "", "", "") for ip in addresses[1:]
        ]

    active, ended = board.active, board.ended
    if active:
        pages = sum(1 for s in active if s.page not in ("cli", "server"))
        runs = len(active) - pages
        parts = [f"{pages} page{'' if pages == 1 else 's'}"] if pages else []
        parts += [f"{runs} command-line run{'' if runs == 1 else 's'}"] if runs else []
        lines = [f"{len(active)} active in the last {IDLE:g}s: {', '.join(parts)}."]
    else:
        lines = [f"No clients active in the last {IDLE:g}s."]
    if board.full:
        lines.append(
            f"The list is full (clients_max = {limit:,}): new sessions aren't listed. "
            "The journal has more: journalctl -u texas2step -g clients:"
        )
    rows: list[tuple[str, ...]]
    if active:
        rows = [("Session", "Address", "Program", "Page", "Doing now", "For", "Moved")]
        for s in active:
            rows += session(s, ", ".join(describe(s.doing)) or "-", now - s.first)
        lines += ["", *_table(rows)]
    if ended:
        shown = ended if everything else ended[:CLIENTS_ENDED_SHOWN]
        hour = "hour" if HISTORY == 3600 else f"{HISTORY:g}s"
        lines += ["", f"{len(ended)} ended in the last {hour}, the latest first:", ""]
        rows = [("Session", "Address", "Program", "Page", "Last seen", "For", "Moved")]
        for s in shown:
            rows += session(s, f"{duration(now - s.last)} ago", s.last - s.first)
        lines += _table(rows)
        if len(shown) < len(ended):
            lines += ["", f"{len(ended) - len(shown)} more: --history lists them all."]
    return lines


def _pct(value: float | None) -> str:
    """A share as a percentage: one decimal under 10 %, where small figures matter."""
    if value is None:
        return "-"
    return f"{value * 100:.1f}%" if value < 0.095 else f"{value * 100:.0f}%"


def _cpu_lines(report: dict[str, Any], hostname: str) -> list[str]:
    """texas2step cpu's figures: now, the last hour by ten minutes, and each day."""
    import textwrap
    from datetime import datetime

    from .cpu import NOW, SAMPLE
    from .traffic import size

    now, busy = report["now"], report["busy"]
    if now is None:
        return [
            f"No worker has written in the last {3 * SAMPLE:g}s: is the service running? "
            "(systemctl status texas2step)"
        ]
    cores = now["cores"]
    lines = [
        f"texas2step on {hostname}"
        + (f", {cores} cores" if cores else "")
        + f". Now, over the last {NOW:g}s:",
        "",
    ]
    workers = now["workers"]
    each = ", ".join(f"{w['pid']} {_pct(w['cpu'])}" for w in workers)
    rows = [("Workers", f"{_pct(now['texas2step'])} of a core: {each}")]
    if now["nginx"] is not None:
        rows.append(("nginx", f"{_pct(now['nginx'])} of a core"))
    if now["other"] is not None:
        other = f"{_pct(now['other'])} of the host's CPU"
        if busy is not None:
            since = datetime.fromisoformat(busy["since"]).strftime("%H:%M") if busy["since"] else ""
            state = f"refusing new tests since {since}" if busy["refusing"] else "tests let in"
            other += f"   (cpu_busy_percent = {busy['percent']:g}: {state})"
        rows += [("Other work", other), ("Host", f"{_pct(now['host'])} of the host's CPU")]
    sessions = now["sessions"]
    tests = now["sent_mbps"] + now["received_mbps"]
    rows.append(
        (
            "Serving",
            f"{sessions:,} UDP session{'' if sessions == 1 else 's'}, "
            f"{now['probes_per_s']:,.0f} probes/s; tests {tests:,.1f} Mb/s",
        )
    )
    memory = sum(w["memory"] for w in workers)
    rows.append(
        (
            "Memory",
            f"{size(memory)}: " + ", ".join(f"{w['pid']} {size(w['memory'])}" for w in workers),
        )
    )
    lines += [f"  {name:<12}{text}" for name, text in rows]

    table: list[tuple[str, ...]]
    hour = report["hour"]
    if hour:
        lines += ["", "The last hour, by ten minutes:", ""]
        table = [("From", "Workers", "Busiest", "Other", "Probes/s", "Sessions", "Tests")]
        for i in range(0, len(hour), 10):
            part = hour[i : i + 10]
            others = [m["other"] for m in part if m["other"] is not None]
            mbps = sum(m["sent_mbps"] + m["received_mbps"] for m in part) / len(part)
            table.append(
                (
                    datetime.fromisoformat(part[0]["minute"]).strftime("%H:%M"),
                    _pct(sum(m["texas2step"] for m in part) / len(part)),
                    _pct(max(m["busiest"] for m in part)),
                    _pct(sum(others) / len(others) if others else None),
                    f"{sum(m['probes_per_s'] for m in part) / len(part):,.0f}",
                    f"{max(m['sessions'] for m in part):,}",
                    f"{mbps:,.1f} Mb/s",
                )
            )
        lines += _table(table)

    days = report["days"]
    if days:
        lines += ["", "Each day, the latest first:", ""]
        table = [("Day", "Idle", "Average", "Peak", "Other", "Probes", "Tests", "Memory")]
        for d in days:
            table.append(
                (
                    d["day"],
                    _pct(d["idle"]),
                    _pct(d["average"]),
                    _pct(d["peak"]),
                    _pct(d["other"]),
                    f"{d['probes']:,}",
                    size(d["bytes"]),
                    size(d["memory"]),
                )
            )
        lines += _table(table)
    note = (
        "Workers, Busiest, Idle, Average and Peak are shares of one core, as top shows each "
        "process; Other work and Host, of all the host's cores. Idle: with no tests, probes or "
        "UDP sessions to serve (docs/cpu.md)."
    )
    return [*lines, "", *textwrap.wrap(note, 88)]


def cmd_cpu(args: argparse.Namespace) -> None:
    import json
    import socket
    import sqlite3
    import time

    from .cpu import SAMPLE, CpuFile

    settings = _server_settings(args)
    path = Path(settings.cpu_file)
    if not path.exists():
        sys.exit(f"texas2step: no figures yet: the service writes {path} as it runs")
    figures = CpuFile(path, settings.access_timezone)

    def show() -> None:
        try:
            busy = figures.noted_busy(settings.cpu_busy_percent, settings.cpu_busy_seconds)
            report = figures.report(time.time(), busy, args.days)
        except (OSError, sqlite3.Error) as exc:
            why = str(exc)
            denied = isinstance(exc, PermissionError) or "readonly" in why or "open" in why
            sys.exit(f"texas2step: can't read {path}: {why}" + (" (try sudo)" if denied else ""))
        if args.json:
            print(json.dumps(report, indent=2), flush=True)
        else:
            print("\n".join(_cpu_lines(report, socket.gethostname())), flush=True)

    if not args.watch:
        show()
        return
    try:
        while True:
            if not args.json:
                print("\x1b[H\x1b[2J", end="")  # to the top, and clear the screen
                print(f"Every {SAMPLE:g}s; Ctrl-C to stop.\n")
            show()
            time.sleep(SAMPLE)
    except KeyboardInterrupt:
        print()


def cmd_clients(args: argparse.Namespace) -> None:
    import json
    import sqlite3
    import time

    from . import clients

    settings = _server_settings(args)
    path = settings.clients_file

    def show() -> None:
        now = time.time()
        try:
            board = clients.read(path, now)
        except FileNotFoundError:
            board = clients.Board([], [], full=False)
            if not args.json and not Path(path).parent.exists():
                print(f"No clients: the service makes {Path(path).parent} as it starts.")
                return
        except (OSError, sqlite3.Error) as exc:
            why = str(exc)
            denied = isinstance(exc, PermissionError) or "readonly" in why or "open" in why
            sys.exit(f"texas2step: can't read {path}: {why}" + (" (try sudo)" if denied else ""))
        if args.json:
            print(json.dumps(board.public(now, settings.clients_max), indent=2), flush=True)
        else:
            table = _clients_table(board, now, settings.clients_max, args.history)
            print("\n".join(table), flush=True)

    if not args.watch:
        show()
        return
    try:
        while True:
            if not args.json:
                print("\x1b[H\x1b[2J", end="")  # to the top, and clear the screen
                print(f"Every {clients.FLUSH:g}s; Ctrl-C to stop.\n")
            show()
            time.sleep(clients.FLUSH)
    except KeyboardInterrupt:
        print()


def _results(count: int) -> str:
    return f"{count} saved result{'' if count == 1 else 's'}"


def cmd_results_clear(args: argparse.Namespace) -> None:
    import sqlite3
    from datetime import datetime

    from .store import ResultStore

    path = Path(_server_settings(args).results_path).absolute()
    if not path.exists():
        print(f"No saved results: there is no {path}.")
        return
    # The service keeps the file open and writes beside it (-wal, -shm), as its own user.
    if not (os.access(path, os.W_OK) and os.access(path.parent, os.W_OK)):
        hint = "" if os.name == "posix" and os.geteuid() == 0 else " (try sudo)"
        sys.exit(f"texas2step: can't change {path}{hint}")
    try:
        store = ResultStore(path)
        try:
            count, first, last, servers = store.tally()
            if not count:
                print(f"{path} holds no saved results.")
                return
            days = [f"{datetime.fromtimestamp(t).astimezone():%Y-%m-%d}" for t in (first, last)]
            span = f"on {days[0]}" if days[0] == days[1] else f"from {days[0]} to {days[1]}"
            print(f"{path} holds {_results(count)}, {span}.")
            print("By server id: " + ", ".join(f"{s or 'none'} {n}" for s, n in servers.items()))
            if not args.yes:
                if not sys.stdin.isatty():
                    sys.exit("texas2step: no terminal to ask on; add --yes to delete them")
                answer = input("Delete them all? Links to them stop working. [y/N] ")
                if answer.strip().lower() not in {"y", "yes"}:
                    sys.exit("Nothing changed.")
            deleted = store.clear()
        finally:
            store.close()
    except sqlite3.Error as exc:
        sys.exit(f"texas2step: {path}: {exc}")
    print(f"Deleted {_results(deleted)}. History starts over with the next test saved.")


async def _save(target: str, result: dict[str, Any]) -> dict[str, Any]:
    async with engine._client() as client:
        r = await client.post(f"{engine._base(target)}{engine.API}/results", json=result)
        r.raise_for_status()
        return r.json()


def _unlock(args: argparse.Namespace) -> None:
    """Trade --code for a pass, which every request then carries (docs/access.md)."""
    try:
        engine.PASS.set(_run(engine.unlock(args.url, args.code)))
    except PermissionError as exc:
        sys.exit(f"texas2step: {engine._base(args.url)} {exc}")
    except ValueError:
        pass  # not JSON: the test itself says what went wrong


def cmd_run(args: argparse.Namespace) -> None:
    _unlock(args)
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
            local=not args.no_local,
            one_stream=args.one_stream,
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
        _unlock(args)
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

    config = sub.add_parser("config", help="manage the settings file")
    config_sub = config.add_subparsers(dest="action", required=True)
    merge = config_sub.add_parser(
        "merge",
        help="bring the settings file up to date with this version, keeping its values",
        description="Rebuild the settings file from this version's template, keeping every "
        "value it sets. Shows the changes; --write applies them and keeps a backup.",
    )
    merge.add_argument(
        "--config",
        default=os.environ.get("TEXAS2STEP_CONFIG", "/etc/texas2step/config.toml"),
        help="settings file to update (default: %(default)s)",
    )
    merge.add_argument(
        "--from",
        dest="source",
        metavar="FILE",
        help="take the values from FILE instead, e.g. config.toml.dpkg-old",
    )
    merge.add_argument("--write", action="store_true", help="write the merged file")
    merge.set_defaults(func=cmd_config_merge)

    def settings_file(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--config",
            default=os.environ.get("TEXAS2STEP_CONFIG", "/etc/texas2step/config.toml"),
            help="settings file to change (default: %(default)s)",
        )
        p.add_argument("--write", action="store_true", help="write the changed file")

    config_set = config_sub.add_parser(
        "set",
        help="change settings in the settings file",
        description="Set each KEY to VALUE in place, keeping the rest of the file. VALUE is "
        'read as TOML when it parses (61000, true, "text", ["a", "b"]), otherwise as text. '
        "Shows the change; --write applies it and keeps a backup.",
    )
    config_set.add_argument("pairs", nargs="+", metavar="KEY=VALUE")
    settings_file(config_set)
    config_set.set_defaults(func=cmd_config_set)

    add_location = config_sub.add_parser(
        "add-location",
        help="list another texas2step server in the location picker",
        description="Add a [[locations]] table for the server at URL. Its id, name and "
        "location are read from the server itself unless given. Shows the change; --write "
        "applies it and keeps a backup.",
    )
    add_location.add_argument(
        "url", help="the server's base URL, e.g. https://houston.example.net/texas2step"
    )
    add_location.add_argument("--id", help="short id, e.g. houston")
    add_location.add_argument("--name", help="name in the picker, e.g. 'houston.example.net'")
    add_location.add_argument("--location", help="free text, e.g. 'Houston, TX'")
    add_location.add_argument("--url-v4", help="IPv4-only base URL")
    add_location.add_argument("--url-v6", help="IPv6-only base URL")
    add_location.add_argument("--url-v4-http1", help="IPv4-only base URL serving only HTTP/1.1")
    add_location.add_argument("--url-v4-http3", help="IPv4-only base URL that offers HTTP/3")
    add_location.add_argument("--url-v6-http1", help="IPv6-only base URL serving only HTTP/1.1")
    add_location.add_argument("--url-v6-http3", help="IPv6-only base URL that offers HTTP/3")
    settings_file(add_location)
    add_location.set_defaults(func=cmd_config_add_location)

    upd = sub.add_parser(
        "update",
        help="install a newer release on this server",
        description="Look for a newer texas2step release (at update_url: GitHub, or a "
        "mirror on a web server or in a directory), download its package and setup tool, "
        "check both against the SHA-256 published with it, show the setup tool's plan, "
        "then run its install script. "
        "With --from DIR, use a release's package and setup tool already on this server "
        "instead, without downloading. Needs sudo, except with --check.",
    )
    upd.add_argument(
        "--check", action="store_true", help="only say whether a newer release is out (exit 100)"
    )
    upd.add_argument(
        "--version",
        dest="release",
        metavar="VERSION",
        help="install this release instead of the newest, e.g. 1.2.0",
    )
    upd.add_argument(
        "--from",
        dest="source",
        metavar="DIR",
        help="install the release whose .deb and texas2step-setup are in DIR, without "
        "downloading (for a server without internet access)",
    )
    upd.add_argument(
        "--allow-downgrade",
        action="store_true",
        help="with --version or --from: install an older release",
    )
    upd.add_argument("-y", "--yes", action="store_true", help="install without asking")
    upd.add_argument(
        "--proxy",
        type=_proxy,
        metavar="URL",
        help="download through this proxy, e.g. http://proxy.example.net:3128 "
        "(default: update_proxy, else https_proxy)",
    )
    upd.add_argument(
        "-k",
        "--insecure",
        action="store_true",
        help="check no certificate, as curl -k: for a firewall that inspects TLS with a CA "
        "this server doesn't trust. Nothing then shows the release is genuine; trusting "
        "that CA is better (docs/install.md)",
    )
    upd.add_argument(
        "--auto",
        action="store_true",
        help="for the update timer: install only when the plan changes nothing but the "
        "package and the settings, without asking",
    )
    upd.add_argument(
        "--config",
        default=os.environ.get("TEXAS2STEP_CONFIG", "/etc/texas2step/config.toml"),
        help="settings file with update_url (default: %(default)s)",
    )
    upd.set_defaults(func=cmd_update)

    acc = sub.add_parser(
        "access",
        help="access codes for servers open to the Internet",
        description="Show the current access code, the words codes are made of, or this "
        "server's key, which every server of an organization must share (docs/access.md).",
    )
    acc_sub = acc.add_subparsers(dest="action", required=True)

    def settings_file(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--config",
            default=os.environ.get("TEXAS2STEP_CONFIG", "/etc/texas2step/config.toml"),
            help="settings file (default: %(default)s)",
        )

    show = acc_sub.add_parser(
        "show", help="the current code, when it changes, and a fingerprint to compare servers"
    )
    settings_file(show)
    show.set_defaults(func=cmd_access_show)
    words = acc_sub.add_parser("words", help="the words codes are made of, one per line")
    settings_file(words)
    words.set_defaults(func=cmd_access_words)
    key = acc_sub.add_parser(
        "key",
        help="print this server's key, or replace it",
        description="Print the key (to copy it to another server), make a new one, or "
        "take one from another server on stdin: "
        "ssh first sudo texas2step access key | sudo texas2step access key --import",
    )
    settings_file(key)
    what = key.add_mutually_exclusive_group()
    what.add_argument("--new", action="store_true", help="make a new random key")
    what.add_argument(
        "--import", dest="import_", action="store_true", help="take the key from stdin"
    )
    what.add_argument("--init", action="store_true", help="make a key only if there is none")
    key.add_argument("-y", "--yes", action="store_true", help="replace a key without asking")
    key.set_defaults(func=cmd_access_key)
    phrase = acc_sub.add_parser(
        "phrase",
        help="standing phrases, which work as codes until removed",
        description="Standing phrases, such as one the service desk gives a VIP from memory. "
        "Each works as an access code until it is removed, or until the end of its until "
        "day, and each use is logged by its name. They live in access_phrases_file, "
        "readable by root and the service only (docs/access.md).",
    )
    phrase_sub = phrase.add_subparsers(dest="phrase_action", required=True)
    phrase_list = phrase_sub.add_parser("list", help="the phrases, their names and until days")
    settings_file(phrase_list)
    phrase_list.set_defaults(func=cmd_access_phrase_list)
    phrase_add = phrase_sub.add_parser(
        "add",
        help="add a phrase",
        description="Add a standing phrase. Leave out PHRASE to type it when asked, which "
        "keeps it out of the shell's history.",
    )
    phrase_add.add_argument("name", help="what the journal and access show call it, e.g. vip")
    phrase_add.add_argument("phrase", nargs="?", help="the phrase; asked for when left out")
    phrase_add.add_argument(
        "--until", metavar="YYYY-MM-DD", help="the last day it works, in access_timezone"
    )
    settings_file(phrase_add)
    phrase_add.set_defaults(func=cmd_access_phrase_add)
    phrase_remove = phrase_sub.add_parser("remove", help="remove a phrase by its name")
    phrase_remove.add_argument("name")
    settings_file(phrase_remove)
    phrase_remove.set_defaults(func=cmd_access_phrase_remove)
    phrase_import = phrase_sub.add_parser(
        "import",
        help="replace the phrases with another server's, from stdin",
        description="ssh first sudo cat /etc/texas2step/access-phrases.toml "
        "| sudo texas2step access phrase import",
    )
    settings_file(phrase_import)
    phrase_import.set_defaults(func=cmd_access_phrase_import)
    phrase_init = phrase_sub.add_parser(
        "init", help="make the file, with two examples commented out, if there is none"
    )
    settings_file(phrase_init)
    phrase_init.set_defaults(func=cmd_access_phrase_init)
    lockouts = acc_sub.add_parser(
        "lockouts",
        help="networks locked out after wrong codes, and blocks",
        description="List the networks locked out of entering codes: after "
        "access_lockout_after wrong codes in a row from one /24 or /48, or blocked by hand. "
        "While locked out, every code from the network is refused, the right one too, until "
        "access_lockout_minutes pass without one (docs/access.md).",
    )
    lockouts.add_argument(
        "--clear", metavar="NETWORK", help="end the lockouts that overlap NETWORK; 'all' ends all"
    )
    settings_file(lockouts)
    lockouts.set_defaults(func=cmd_access_lockouts)
    block = acc_sub.add_parser(
        "block",
        help="lock a network out of entering codes",
        description="Lock an address or network out of entering codes, as if it had entered "
        "too many wrong ones, or keep its lockout going. It can't overlap internal_networks.",
    )
    block.add_argument("network", help="e.g. 203.0.113.0/24, 2001:db8:5::/48 or 203.0.113.7")
    block.add_argument(
        "--minutes", type=float, help="how long, at least (default: access_lockout_minutes)"
    )
    settings_file(block)
    block.set_defaults(func=cmd_access_block)
    passes = acc_sub.add_parser(
        "passes",
        help="how long passes last, and end every pass given so far",
        description="A right code or phrase buys a pass, which lasts access_pass_hours from "
        "when it was issued. --end ends every pass issued so far, on this server and, with "
        "access_share_lockouts on, on every server in [[locations]]: tests running with one "
        "stop, and everyone testing from outside enters the code again. Codes and phrases "
        "keep working (docs/access.md).",
    )
    passes.add_argument("--end", action="store_true", help="end every pass issued so far")
    passes.add_argument("-y", "--yes", action="store_true", help="with --end: don't ask first")
    settings_file(passes)
    passes.set_defaults(func=cmd_access_passes)
    shuffle = acc_sub.add_parser(
        "shuffle",
        help="change the codes now: the current and previous ones stop working",
        description="Mix a new random value into every code, so the current code and the "
        "previous one stop working at once and a new one takes their place, with no new key "
        "and no restart. With access_share_lockouts on, every server in [[locations]] takes "
        "the same; otherwise give each the value printed, with --to. Passes keep working "
        "unless --end-passes ends them too. access show gives the new code (docs/access.md).",
    )
    shuffle.add_argument("--to", metavar="VALUE", help="take the shuffle another server printed")
    shuffle.add_argument(
        "--end-passes", action="store_true", help="also end every pass issued so far"
    )
    shuffle.add_argument("-y", "--yes", action="store_true", help="don't ask first")
    settings_file(shuffle)
    shuffle.set_defaults(func=cmd_access_shuffle)
    peers = acc_sub.add_parser(
        "peers",
        help="check that the servers in [[locations]] take this server's lockouts",
        description="With access_share_lockouts on, lockouts, blocks and clears go to every "
        "server in [[locations]]. This sends each a signed check that changes nothing, and "
        "says which take them: those that hold the same access key and list this server's "
        "address in their access_peer_networks (docs/access.md).",
    )
    settings_file(peers)
    peers.set_defaults(func=cmd_access_peers)

    res = sub.add_parser("results", help="the saved results that History shows")
    res_sub = res.add_subparsers(dest="action", required=True)
    clear = res_sub.add_parser(
        "clear",
        help="delete every saved result, to start History over",
        description="Delete every saved result, for a fresh start or on a server cloned "
        "from another (docs/install.md). It shows what it would delete and asks first. The "
        "service keeps running.",
    )
    settings_file(clear)
    clear.add_argument("-y", "--yes", action="store_true", help="delete them without asking")
    clear.set_defaults(func=cmd_results_clear)

    tr = sub.add_parser(
        "traffic",
        help="the traffic tests moved, against traffic_day_gb and traffic_month_gb",
        description="Show the traffic the tests moved today and this month, against the "
        "limits, and per day (docs/traffic.md).",
    )
    settings_file(tr)
    tr.add_argument("--days", type=int, default=30, help="days to list (default: 30)")
    tr.set_defaults(func=cmd_traffic)
    tr_sub = tr.add_subparsers(dest="action")
    allow = tr_sub.add_parser(
        "allow",
        help="let more traffic through today",
        description="Add to today's traffic allowance, and so to this month's: such as when "
        "the day's limit is reached and a test must still run. A negative number takes it "
        "back. The running service sees it at once.",
    )
    settings_file(allow)
    allow.add_argument("gb", type=float, help="gigabytes (10^9 bytes), such as 5")
    allow.set_defaults(func=cmd_traffic_allow)

    cl = sub.add_parser(
        "clients",
        help="the pages and command-line runs using this server now",
        description="List each page and command-line run that sent a request in the last "
        "minute: its session, address, program, page, what it is doing now, for how long, and "
        "the bytes it moved; then the latest of those that ended in the last hour "
        "(docs/clients.md).",
    )
    settings_file(cl)
    cl.add_argument("--json", action="store_true", help="print them as JSON, for scripts")
    cl.add_argument("--watch", action="store_true", help="show them again every 2 seconds")
    cl.add_argument(
        "--history",
        action="store_true",
        help=f"list every session that ended in the last hour, not only the latest "
        f"{CLIENTS_ENDED_SHOWN}",
    )
    cl.set_defaults(func=cmd_clients)

    cp = sub.add_parser(
        "cpu",
        help="the server's CPU, and what it serves",
        description="Show the CPU texas2step's workers use now, nginx's, the host's and other "
        "work's, beside the UDP sessions and probes and the tests' traffic they serve; then the "
        "last hour by ten minutes and each day's figures, which say whether the workers' CPU "
        "with nothing to serve is growing (docs/cpu.md).",
    )
    settings_file(cp)
    cp.add_argument("--json", action="store_true", help="print the figures as JSON, for scripts")
    cp.add_argument("--watch", action="store_true", help="show them again every 5 seconds")
    cp.add_argument(
        "--days",
        type=int,
        default=30,
        choices=range(31),
        metavar="N",
        help="days of daily figures, 0 to 30 (default 30)",
    )
    cp.set_defaults(func=cmd_cpu)

    def target(p: argparse.ArgumentParser) -> None:
        p.add_argument("url", help="base URL of a texas2step server")
        p.add_argument("--json", action="store_true", help="print the raw result as JSON")
        p.add_argument(
            "--code",
            default=os.environ.get("TEXAS2STEP_CODE"),
            help="the access code, for a server that asks for one (default: $TEXAS2STEP_CODE)",
        )

    def throughput(p: argparse.ArgumentParser) -> None:
        p.add_argument("--seconds", type=float, default=10.0)
        p.add_argument("--streams", type=int, default=4)

    run = sub.add_parser("run", help="full test: latency, download, upload, packet loss")
    target(run)
    throughput(run)
    run.add_argument("--packets", type=int, default=500)
    run.add_argument("--interval", type=float, default=10.0, help="ms between UDP probes")
    run.add_argument("--no-loss", action="store_true", help="skip the UDP packet-loss test")
    run.add_argument(
        "--one-stream",
        action=argparse.BooleanOptionalAction,
        help="after the download, download over one stream too (default: as the server says)",
    )
    run.add_argument("--save", action="store_true", help="store the result on the server")
    run.add_argument(
        "--no-local",
        action="store_true",
        help="leave this computer's own address and MAC out of the result",
    )
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
    loss.add_argument(
        "--size", type=int, default=engine.PROBE_BYTES, help="probe size in bytes (default: 200)"
    )
    loss.set_defaults(
        func=_discrete(
            engine.measure_loss,
            lambda r: f"{_loss_line(r)}\n{_call_line(quality.call_quality(r))}",
            packets="packets",
            interval_ms="interval",
            size="size",
        )
    )
    return parser


def trimmed(args: argparse.Namespace) -> argparse.Namespace:
    """The arguments without spaces around their text: pasted values often bring them, with
    an address, a URL or a network."""
    for name, value in vars(args).items():
        if isinstance(value, str):
            setattr(args, name, value.strip())
        elif isinstance(value, list):
            setattr(args, name, [v.strip() if isinstance(v, str) else v for v in value])
    return args


def main(argv: list[str] | None = None) -> None:
    args = trimmed(build_parser().parse_args(argv))
    args.func(args)
