"""Command line: ``texas2step serve`` and the discrete measurement commands."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Callable, Coroutine
from pathlib import Path
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
    if client.get("local_ip"):
        mac = f" · MAC {client['mac']}" if client.get("mac") else ""
        lines.insert(2, f"this computer {client['local_ip']}{mac}")
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

    location = {
        "id": args.id,
        "name": args.name,
        "url": args.url,
        "location": args.location,
        "url_v4": args.url_v4,
        "url_v6": args.url_v6,
    }
    if not all(location.values()):
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
        # Its single-family URLs, so the IP drop-down works for it here too.
        for key in ("url_v4", "url_v6"):
            found = str(own.get(key) or "")
            if not location[key] and found.startswith(("https://", "http://")):
                location[key] = found
    again = shlex.join(
        ["texas2step", "config", "add-location", args.url]
        + [f"--{k.replace('_', '-')}={v}" for k, v in location.items() if v and k != "url"]
    )
    _apply_edit(args, lambda text: config_edit.add_location(text, location), again)


def cmd_update(args: argparse.Namespace) -> None:
    from . import update
    from .config import load_settings

    if args.auto and (args.check or args.release or args.allow_downgrade or args.source):
        sys.exit("texas2step update: --auto takes no other options")
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


def cmd_access_show(args: argparse.Namespace) -> None:
    from datetime import UTC, datetime

    settings = _server_settings(args)
    codes = _access_codes(settings)
    current = codes.current(datetime.now(UTC))
    this, last = {
        "daily": ("Today's code", "Yesterday's"),
        "weekly": ("This week's code", "Last week's"),
        "monthly": ("This month's code", "Last month's"),
        "fixed": ("The code", ""),
    }[codes.interval]
    if settings.access_code == "off":
        print('Access codes are off (access_code = "off"). Were they daily, the code would be:')
    elif codes.interval == "fixed":
        print("Access codes: fixed. The code changes only with a new key.")
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
    rows += [
        ("Words", f"{len(codes.words)} {words}"),
        ("Phrases", ", ".join(standing) or "none"),
        ("Key", settings.access_key_file),
        ("Fingerprint", f"{codes.fingerprint()}   (the same on servers that give the same codes)"),
    ]
    width = max(len(label) for label, _ in rows) + 2
    for label, value in rows:
        print(f"{label + ':':<{width}}{value}")
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
