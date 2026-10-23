"""Wrong access codes per network, and the lockouts and blocks they lead to.

Wrong codes count against the visitor's network, its IPv4 /24 or IPv6 /48, not its
address alone: one host can take a new IPv6 address for every guess. After
``access_lockout_after`` wrong codes in a row the network is locked out. While it is,
every code from it is refused unread, the right one too, with the same answer as a wrong
code, and each one pushes the end of the lockout to ``access_lockout_minutes`` after it.
A network quiet for that long starts again from nothing, and so does one that enters a
right code. A block, set from the command line or the API, is a lockout set by hand.

The counts live in a small SQLite file (``access_lockouts_file``) that the service's
workers and the command line share. Checking a code and counting it happen in one
transaction, so guesses sent all at once can't slip past the count.
"""

from __future__ import annotations

import ipaddress
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .access import address
from .store import create_owned, prepare

V4_PREFIX = 24
V6_PREFIX = 48
WRONG_CODES = "wrong codes"  # the source of a lockout the server made itself
LOOPBACK = ("127.0.0.0/8", "::1/128")

SCHEMA = """
CREATE TABLE IF NOT EXISTS lockouts (
    network   TEXT PRIMARY KEY,  -- 203.0.113.0/24, 2001:db8:5::/48
    family    INTEGER NOT NULL,  -- 4 or 6; 0 for a client without an address
    first     TEXT NOT NULL,     -- the network's first and last address, in fixed-width
    last      TEXT NOT NULL,     -- hex, so SQLite can compare them as text
    failures  INTEGER NOT NULL,  -- wrong codes in a row
    locked_at REAL,              -- when the lockout or block began; NULL while counting
    expires   REAL NOT NULL,     -- when the lockout ends, or a quiet network is forgotten
    source    TEXT NOT NULL      -- wrong codes, the command line, or the API and its caller
);
CREATE INDEX IF NOT EXISTS lockouts_span ON lockouts (family, first, last);
"""


@dataclass(frozen=True)
class Lockout:
    network: str
    failures: int
    locked_at: float | None
    expires: float
    source: str


@dataclass(frozen=True)
class Outcome:
    """What became of a code: ``right``, or why not."""

    right: bool
    network: str  # the network the code counted against
    locked: bool = False  # refused unread: the network is locked out
    locked_now: bool = False  # this wrong code began the lockout
    until: float = 0.0  # when a lockout ends, unless more codes come
    note: str = ""  # what the check said, for the journal


def network_of(ip: str) -> str:
    """The network a client's wrong codes count against: its /24 or /48."""
    addr = address(ip)
    if addr is None:
        return ip  # such as "unknown": every client without an address counts together
    prefix = V4_PREFIX if addr.version == 4 else V6_PREFIX
    return str(ipaddress.ip_network(f"{addr}/{prefix}", strict=False))


def parse_network(text: str) -> ipaddress.IPv4Network | ipaddress.IPv6Network:
    """An address or network as typed, such as 203.0.113.7 or 2001:db8:5::/48."""
    try:
        net = ipaddress.ip_network(text.strip(), strict=False)
    except ValueError:
        raise ValueError(f"{text!r} isn't an address or a network") from None
    if isinstance(net, ipaddress.IPv6Network) and net.network_address.ipv4_mapped:
        mapped = net.network_address.ipv4_mapped
        net = ipaddress.ip_network(f"{mapped}/{max(net.prefixlen - 96, 0)}", strict=False)
    return net


def inside_of(
    net: ipaddress.IPv4Network | ipaddress.IPv6Network, internal: list[str]
) -> str | None:
    """The internal network or loopback range ``net`` overlaps, which no block may cover."""
    for other in (*internal, *LOOPBACK):
        if net.overlaps(ipaddress.ip_network(other, strict=False)):
            return other
    return None


def _span(net: str) -> tuple[int, str, str]:
    try:
        parsed = ipaddress.ip_network(net, strict=False)
    except ValueError:
        return 0, net, net
    width = 8 if parsed.version == 4 else 32
    first, last = int(parsed.network_address), int(parsed.broadcast_address)
    return parsed.version, f"{first:0{width}x}", f"{last:0{width}x}"


def _point(ip: str) -> tuple[int, str]:
    addr = address(ip)
    if addr is None:
        return 0, ip
    return addr.version, f"{int(addr):0{8 if addr.version == 4 else 32}x}"


class LockoutStore:
    def __init__(self, path: str | Path, after: int = 3, minutes: float = 30.0) -> None:
        self.path = str(path)
        self.after = after
        self.seconds = minutes * 60
        self._lock = threading.Lock()
        if self.path != ":memory:":
            create_owned(Path(self.path))
        # Autocommit: each method takes its own BEGIN IMMEDIATE, the write lock every
        # worker and the command line wait for.
        self._db = sqlite3.connect(
            self.path, check_same_thread=False, isolation_level=None, timeout=10
        )
        self._db.row_factory = sqlite3.Row
        with self._lock:
            prepare(self._db, self.path, SCHEMA)
            self._db.execute("PRAGMA synchronous=NORMAL")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _write(self, work: Callable[[], Any]) -> Any:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                result = work()
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")
            return result

    def _locked_over(self, family: int, point: str, now: float) -> list[sqlite3.Row]:
        return self._db.execute(
            "SELECT network, expires FROM lockouts WHERE family = ? AND first <= ? "
            "AND last >= ? AND locked_at IS NOT NULL AND expires > ?",
            (family, point, point, now),
        ).fetchall()

    def submit(
        self, ip: str, check: Callable[[], tuple[bool, str]], now: float | None = None
    ) -> Outcome:
        """Count a code from ``ip``. ``check`` says whether it is right, with a note for the
        journal; it isn't called while ``ip``'s network is locked out."""
        now = now if now is not None else time.time()
        network = network_of(ip)
        family, point = _point(ip)

        def work() -> Outcome:
            self._db.execute("DELETE FROM lockouts WHERE expires <= ?", (now,))
            if locked := self._locked_over(family, point, now):
                renewed = now + self.seconds
                self._db.execute(
                    "UPDATE lockouts SET expires = max(expires, ?) WHERE family = ? AND "
                    "first <= ? AND last >= ? AND locked_at IS NOT NULL",
                    (renewed, family, point, point),
                )
                until = max(renewed, *(row["expires"] for row in locked))
                return Outcome(False, locked[0]["network"], locked=True, until=until)
            right, note = check()
            if right:
                self._db.execute("DELETE FROM lockouts WHERE network = ?", (network,))
                return Outcome(True, network, note=note)
            row = self._db.execute(
                "SELECT failures FROM lockouts WHERE network = ?", (network,)
            ).fetchone()
            failures = (row["failures"] if row else 0) + 1
            locked_now = failures >= self.after
            expires = now + self.seconds
            self._db.execute(
                "INSERT INTO lockouts VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (network) "
                "DO UPDATE SET failures = excluded.failures, locked_at = excluded.locked_at, "
                "expires = excluded.expires",
                (
                    network,
                    *_span(network),
                    failures,
                    now if locked_now else None,
                    expires,
                    WRONG_CODES,
                ),
            )
            return Outcome(False, network, locked_now=locked_now, until=expires, note=note)

        return self._write(work)

    def block(self, net: str, minutes: float, source: str, now: float | None = None) -> Lockout:
        """Lock ``net`` out by hand, or keep its lockout going at least ``minutes`` more."""
        now = now if now is not None else time.time()
        network = str(parse_network(net))

        def work() -> Lockout:
            self._db.execute("DELETE FROM lockouts WHERE expires <= ?", (now,))
            self._db.execute(
                "INSERT INTO lockouts VALUES (?, ?, ?, ?, 0, ?, ?, ?) ON CONFLICT (network) "
                "DO UPDATE SET locked_at = coalesce(locked_at, excluded.locked_at), "
                "expires = max(expires, excluded.expires), source = excluded.source",
                (network, *_span(network), now, now + minutes * 60, source),
            )
            row = self._db.execute("SELECT * FROM lockouts WHERE network = ?", (network,))
            return _lockout(row.fetchone())

        return self._write(work)

    def clear(self, net: str | None) -> list[str]:
        """End the lockouts and counts that overlap ``net``; every one for None."""

        def work() -> list[str]:
            if net is None:
                rows = self._db.execute("SELECT network FROM lockouts").fetchall()
                self._db.execute("DELETE FROM lockouts")
            else:
                family, first, last = _span(str(parse_network(net)))
                where = "family = ? AND first <= ? AND last >= ?"
                args = (family, last, first)
                rows = self._db.execute(f"SELECT network FROM lockouts WHERE {where}", args)
                rows = rows.fetchall()
                self._db.execute(f"DELETE FROM lockouts WHERE {where}", args)
            return [row["network"] for row in rows]

        return self._write(work)

    def current(self, now: float | None = None) -> tuple[list[Lockout], int]:
        """The lockouts and blocks in force, oldest first, and how many networks are
        counting wrong codes without being locked out yet."""
        now = now if now is not None else time.time()
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM lockouts WHERE expires > ? ORDER BY locked_at, network", (now,)
            ).fetchall()
        locked = [_lockout(row) for row in rows if row["locked_at"] is not None]
        return locked, len(rows) - len(locked)


def _lockout(row: sqlite3.Row) -> Lockout:
    return Lockout(row["network"], row["failures"], row["locked_at"], row["expires"], row["source"])
