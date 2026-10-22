"""Access codes: the word code a service desk reads out, and the pass it buys.

An Internet-facing server can ask for a code before it runs tests (``access_code``).
Every server of an organization holds the same random key (``access_key_file``), and
works out the same code from it for the current period: daily, weekly, monthly, or one
that stays fixed. Nothing is shared between servers at run time:

* With the built-in words, the key draws the organization's 512 words from
  ``words.txt``, and a code is two of them and some digits: ``maple.river.42``.
* With the site's own list (``access_words``), a code is one entry and the digits:
  ``austin.42``, ``fort.bragg.68``.

A code someone types is read with any case and any separators, so ``Maple River 42``
counts. The previous period's code still works for a day after the change.

A right code buys a pass: an expiry time signed with the key. The browser sends it as
``?pass=`` with every request, to this server and to the others in the picker, which
check the signature with their copy of the key. A query parameter, not a header: a
header would make browsers preflight every ping and download, and skew the timings.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import ipaddress
import os
import re
import secrets
import time
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from .config import Settings

INTERVALS = ("off", "daily", "weekly", "monthly", "fixed")
WORDS = Path(__file__).with_name("words.txt")
KEY_BYTES = 32
DRAW = 512  # built-in words each organization draws by its key
GRACE = 24 * 3600  # the previous code still works this long after a change
MIN_SITE_WORDS = 100
MAX_ENTRY = 40
GROUP = "texas2step"  # the service's group, which must read the key


class AccessError(ValueError):
    """The access settings can't be used, with a message for the operator."""


def normalize(text: str) -> str:
    """Lowercase letters and digits, with any run of other characters as one dot."""
    return ".".join(re.findall(r"[a-z0-9]+", text.lower()))


# --- The key -----------------------------------------------------------------------


def new_key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(KEY_BYTES)).rstrip(b"=").decode()


def parse_key(text: str) -> bytes:
    text = text.strip()
    try:
        key = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except ValueError:
        key = b""
    if len(key) != KEY_BYTES or not re.fullmatch(r"[A-Za-z0-9_-]+", text):
        raise AccessError(f"not an access key: expected {KEY_BYTES} bytes in base64url")
    return key


def read_key(path: str | Path) -> bytes:
    try:
        text = Path(path).read_text()
    except FileNotFoundError:
        raise AccessError(
            f"{path} is missing; make one with: sudo texas2step access key --new"
        ) from None
    except OSError as exc:
        raise AccessError(f"can't read {path}: {exc.strerror}") from exc
    try:
        return parse_key(text)
    except AccessError as exc:
        raise AccessError(f"{path}: {exc}") from None


def write_key(path: str | Path, text: str) -> None:
    """Replace the key file atomically: readable by root and the service's group only."""
    parse_key(text)
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
    try:
        os.write(fd, (text.strip() + "\n").encode())
        if os.geteuid() == 0:
            import grp

            # Without the service's group yet, root's, until the package makes it.
            with contextlib.suppress(KeyError):
                os.fchown(fd, 0, grp.getgrnam(GROUP).gr_gid)
        os.fchmod(fd, 0o640)
    finally:
        os.close(fd)
    os.replace(tmp, path)


# --- Words -------------------------------------------------------------------------


def _lines(text: str) -> list[tuple[int, str]]:
    return [
        (n, line.strip())
        for n, line in enumerate(text.splitlines(), 1)
        if line.strip() and not line.lstrip().startswith("#")
    ]


def builtin_words() -> list[str]:
    return [word for _, word in _lines(WORDS.read_text())]


def drawn_words(key: bytes) -> list[str]:
    """The organization's share of the built-in words, chosen by its key."""

    def rank(word: str) -> bytes:
        return hmac.new(key, b"draw|" + word.encode(), hashlib.sha256).digest()

    return sorted(sorted(builtin_words(), key=rank)[:DRAW])


def site_words(path: str | Path) -> list[str]:
    """A site's own list: one entry per line, each normalized, and checked."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise AccessError(f"access_words: can't read {path}: {exc.strerror}") from exc
    except UnicodeDecodeError:
        raise AccessError(f"access_words: {path} isn't UTF-8 text") from None
    seen: dict[str, int] = {}
    problems: list[str] = []
    for n, line in _lines(text):
        entry = normalize(line)
        where = f"{path}:{n}"
        if not line.isascii():
            problems.append(f"{where}: {line!r} has letters outside ASCII")
        elif not entry or not re.search(r"[a-z]", entry):
            problems.append(f"{where}: {line!r} has no letters")
        elif len(entry) > MAX_ENTRY:
            problems.append(f"{where}: {entry!r} is over {MAX_ENTRY} characters")
        elif entry in seen:
            problems.append(f"{where}: {entry!r} repeats line {seen[entry]}")
        else:
            seen[entry] = n
    if problems:
        more = f" (and {len(problems) - 5} more)" if len(problems) > 5 else ""
        raise AccessError("access_words: " + "; ".join(problems[:5]) + more)
    if len(seen) < MIN_SITE_WORDS:
        raise AccessError(
            f"access_words: {path} has {len(seen)} entries; a list needs at least "
            f"{MIN_SITE_WORDS}, so codes aren't easy to guess"
        )
    return list(seen)


# --- Codes and passes --------------------------------------------------------------


@dataclass(frozen=True)
class Period:
    id: str  # what the code is made from, e.g. 2026-10-22, 2026-W43, 2026-10, fixed
    start: datetime | None  # None for a fixed code
    end: datetime | None


@dataclass(frozen=True)
class Current:
    code: str
    period: Period
    previous: str | None  # still accepted until previous_until
    previous_until: datetime | None


class Codes:
    """The codes and passes of one server, from its settings, key and words."""

    def __init__(
        self,
        key: bytes,
        interval: str,
        zone: str,
        digits: int,
        words: list[str],
        site: bool,
        pass_hours: float,
    ) -> None:
        self.key = key
        self.interval = interval
        self.zone = ZoneInfo(zone)
        self.zone_name = zone
        self.digits = digits
        self.words = words
        self.site = site
        self.pass_seconds = int(pass_hours * 3600)

    @classmethod
    def load(cls, settings: Settings, interval: str | None = None) -> Codes | None:
        """None when access codes are off; AccessError when they can't work. ``interval``
        stands in for ``access_code``, as ``texas2step access show`` does while it's off."""
        interval = interval or settings.access_code
        if interval == "off":
            return None
        key = read_key(settings.access_key_file)
        site = bool(settings.access_words)
        words = site_words(settings.access_words) if site else drawn_words(key)
        return cls(
            key,
            interval,
            settings.access_timezone,
            settings.access_digits,
            words,
            site,
            settings.access_pass_hours,
        )

    def _mac(self, *parts: str) -> bytes:
        return hmac.new(self.key, "|".join(parts).encode(), hashlib.sha256).digest()

    # The periods, in the organization's time zone, so every server changes together.

    def _midnight(self, day: date) -> datetime:
        return datetime(day.year, day.month, day.day, tzinfo=self.zone)

    def period(self, now: datetime) -> Period:
        day = now.astimezone(self.zone).date()
        if self.interval == "daily":
            return Period(day.isoformat(), self._midnight(day), self._midnight(day + timedelta(1)))
        if self.interval == "weekly":
            monday = day - timedelta(day.weekday())
            year, week, _ = monday.isocalendar()
            start, end = self._midnight(monday), self._midnight(monday + timedelta(7))
            return Period(f"{year}-W{week:02d}", start, end)
        if self.interval == "monthly":
            first = day.replace(day=1)
            following = (first + timedelta(32)).replace(day=1)
            return Period(f"{first:%Y-%m}", self._midnight(first), self._midnight(following))
        return Period("fixed", None, None)

    def code_for(self, period: Period) -> str:
        mac = self._mac("code", self.interval, period.id)
        n = len(self.words)
        first = int.from_bytes(mac[0:8]) % n
        parts = [self.words[first]]
        if not self.site:  # two different words
            second = int.from_bytes(mac[8:16]) % (n - 1)
            parts.append(self.words[second + (second >= first)])
        number = int.from_bytes(mac[16:24]) % 10**self.digits
        return ".".join([*parts, f"{number:0{self.digits}d}"])

    def current(self, now: datetime) -> Current:
        period = self.period(now)
        previous = until = None
        if period.start is not None and now < period.start + timedelta(seconds=GRACE):
            previous = self.code_for(self.period(period.start - timedelta(seconds=1)))
            until = period.start + timedelta(seconds=GRACE)
        return Current(self.code_for(period), period, previous, until)

    def check(self, text: str, now: datetime) -> bool:
        """Whether ``text`` is the current code, or the previous one in its grace day."""
        given = normalize(text)
        head, _, tail = given.rpartition(".")
        if tail.isdigit() and len(tail) <= self.digits:  # "7" for "07"
            given = f"{head}.{int(tail):0{self.digits}d}"
        current = self.current(now)
        return any(
            hmac.compare_digest(given.encode(), code.encode())
            for code in (current.code, current.previous)
            if code
        )

    def fingerprint(self) -> str:
        """The same on every server that would give the same codes; reveals nothing."""
        words = hashlib.sha256("\n".join(self.words).encode()).hexdigest()
        what = (
            self.interval,
            self.zone_name,
            str(self.digits),
            "site" if self.site else "built-in",
        )
        mac = self._mac("fingerprint", *what, words).hex()
        return f"{mac[0:4]}-{mac[4:8]}-{mac[8:12]}"

    def issue(self, now: float | None = None) -> tuple[str, int]:
        expires = int(now if now is not None else time.time()) + self.pass_seconds
        return f"{expires}.{self._sign(expires)}", expires

    def _sign(self, expires: int) -> str:
        mac = self._mac("pass", str(expires))[:18]
        return base64.urlsafe_b64encode(mac).decode()

    def pass_expires(self, token: str, now: float | None = None) -> int | None:
        """The pass's expiry, if it is ours, current, and no longer than passes are."""
        stamp, _, sig = (token or "").partition(".")
        if not stamp.isdigit() or len(stamp) > 12:
            return None
        expires = int(stamp)
        now = now if now is not None else time.time()
        if not now < expires <= now + self.pass_seconds + 300:
            return None
        return expires if hmac.compare_digest(sig.encode(), self._sign(expires).encode()) else None


# --- Who is inside -----------------------------------------------------------------


def address(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        addr = ipaddress.ip_address(text.strip())
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        return addr.ipv4_mapped
    return addr


def is_internal(ip: str, networks: list[str]) -> bool:
    """Loopback, or an address in ``internal_networks``."""
    addr = address(ip)
    if addr is None:
        return False
    return addr.is_loopback or any(
        addr in ipaddress.ip_network(net, strict=False) for net in networks
    )


class Attempts:
    """Wrong codes per client address, to slow down guessing (each worker counts its own)."""

    def __init__(self, limit: int = 10, window: float = 600.0) -> None:
        self.limit = limit
        self.window = window
        self.failures: dict[str, deque[float]] = {}

    def blocked(self, ip: str, now: float | None = None) -> bool:
        now = now if now is not None else time.monotonic()
        failures = self.failures.get(ip)
        while failures and failures[0] <= now - self.window:
            failures.popleft()
        if failures is not None and not failures:
            del self.failures[ip]
        return bool(failures) and len(failures) >= self.limit

    def failed(self, ip: str, now: float | None = None) -> None:
        now = now if now is not None else time.monotonic()
        self.failures.setdefault(ip, deque()).append(now)
        if len(self.failures) > 10_000:  # forget the oldest addresses first
            del self.failures[next(iter(self.failures))]
