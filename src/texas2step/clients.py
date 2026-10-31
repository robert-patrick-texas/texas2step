"""Who is using the server now, and who did in the last hour: each page and each
command-line run, as ``texas2step clients`` and ``GET /api/v1/clients`` show them
(docs/clients.md).

The page makes a random id when it loads and keeps it only in its memory, never in a
cookie or the browser's storage. It sends the id with every request as ``?session=``, and
the page shown as ``?page=``. So two browsers on one computer, behind one address, are two
sessions, and a reload starts a new one. The command line makes one id for each run.
Requests without one, such as curl's or an earlier version's, are grouped by address and
User-Agent. A client's port tells nothing here: one page uses several connections, and
each gets a new port when it reconnects.

A session is active while it sends: Monitor and Diag always do, the Test page while a test
runs. One quiet for ``IDLE`` seconds has ended, and stays listed for ``HISTORY`` seconds.
The journal is told when each starts, when it moves to another page, and when it ends.

At most ``clients_max`` sessions are listed, active and ended together: to make room, the
ended ones go first, the oldest first. With that many active, new sessions go unlisted
until some end, and the journal is warned, with the addresses that have the most. No one
address is held to fewer: behind NAT, a whole office can share one.

Each worker keeps its sessions in memory and, every ``FLUSH`` seconds, writes those that
changed to a small SQLite file (``clients_file``, on the RAM-backed /run), one row per
session and worker, which the other worker, the command line and the API read. The
journal's lines are claimed through the same file, so each is written once, whichever
worker sees it first.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from starlette.requests import HTTPConnection

from .store import create_owned, prepare
from .traffic import size

log = logging.getLogger(__name__)

IDLE = 60.0  # seconds without a request after which a session has ended
HISTORY = 3600.0  # seconds an ended session stays listed
RECENT = 5.0  # a short request, such as a ping, says what a session is doing for this long
FLUSH = 2.0  # seconds between a worker's writes
FRESH = 3 * FLUSH  # a row older than this is from a worker that stopped: not what it's doing
WARN_EVERY = 600.0  # seconds between the journal's warnings that the list is full
LIMIT = 1000  # clients_max's default
ADDRESSES = 8  # the most addresses kept for one session

SESSION_ID = re.compile(r"[A-Za-z0-9_-]{4,32}")
PAGES = {
    "test": "Test",
    "monitor": "Monitor",
    "diag": "Diag",
    "history": "History",
    "help": "Help",
    "api": "API",
    "cli": "CLI",
    "server": "Server",  # another texas2step server testing toward this one (/measure)
}
# What a request to each path is doing; any other request is the page's own ("page").
ACTIVITIES = {
    "/api/v1/down": "download",
    "/api/v1/up": "upload",
    "/api/v1/ping": "latency",
    "/api/v1/ws": "websocket",
    "/api/v1/diag/http": "http",
}
LABELS = {
    "download": "Download",
    "upload": "Upload",
    "latency": "Latency",
    "websocket": "WebSocket echo",
    "http": "HTTP checks",
    "measure": "Test toward another server",
    "udp:path": "UDP probes",
    "udp:voice": "Voice call over UDP",
    "udp:video": "Video call over UDP",
}
STREAMS = ("download", "upload")  # labelled with how many run at once

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    key       TEXT NOT NULL,     -- the session's id, or its address and program
    worker    INTEGER NOT NULL,  -- the worker's process id
    session   TEXT,              -- the id the client sent, if any
    page      TEXT,
    page_at   REAL NOT NULL,     -- when it named that page
    agent     TEXT NOT NULL,     -- its User-Agent
    ip        TEXT NOT NULL,     -- its latest address
    addresses TEXT NOT NULL,     -- JSON: each address it came from, and when last
    first     REAL NOT NULL,     -- when this worker first saw it, since it last ended
    last      REAL NOT NULL,     -- its latest request here
    sent      INTEGER NOT NULL,  -- bytes this worker sent it, and received from it
    received  INTEGER NOT NULL,
    doing     TEXT NOT NULL,     -- JSON: what it was doing here when written
    written   REAL NOT NULL,
    PRIMARY KEY (key, worker)
);
CREATE TABLE IF NOT EXISTS told (
    key     TEXT PRIMARY KEY,  -- a session the journal was told had started, until it ends
    page    TEXT,
    page_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS ended (
    id        INTEGER PRIMARY KEY,
    session   TEXT,
    page      TEXT,
    agent     TEXT NOT NULL,
    addresses TEXT NOT NULL,     -- JSON: the latest first
    first     REAL NOT NULL,
    last      REAL NOT NULL,
    sent      INTEGER NOT NULL,
    received  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ended_last ON ended (last);
CREATE TABLE IF NOT EXISTS notices (
    name     TEXT PRIMARY KEY,           -- "full": the list held clients_max sessions
    seen     REAL NOT NULL DEFAULT 0,    -- when a worker last left a new session out
    told     REAL NOT NULL DEFAULT 0,    -- when the journal was last warned
    unlisted INTEGER NOT NULL DEFAULT 0  -- sessions left out since
);
"""

# Browsers name the others they resemble, so the more particular first.
BROWSERS = (
    ("Edg/", "Edge"),
    ("OPR/", "Opera"),
    ("FxiOS/", "Firefox"),
    ("CriOS/", "Chrome"),
    ("Firefox/", "Firefox"),
    ("Chrome/", "Chrome"),
    ("Version/", "Safari"),
)
SYSTEMS = (
    ("Windows", "Windows"),
    ("iPhone", "iPhone"),
    ("iPad", "iPad"),
    ("Android", "Android"),
    ("CrOS", "ChromeOS"),
    ("Mac OS X", "macOS"),
    ("Linux", "Linux"),
)
UNPRINTABLE = re.compile(r"[^\x20-\x7e]")


def untracked(path: str, query: bytes) -> bool:
    """Monitoring's liveness checks and scrapes, and the desk's own view of this list and of
    the CPU, aren't clients.
    A page's own /health, as Monitor asks a server that has gone quiet, is."""
    if path in ("/api/v1/clients", "/api/v1/cpu", "/api/v1/metrics"):
        return True
    return path == "/api/v1/health" and b"session=" not in query


def printable(text: str, limit: int = 200) -> str:
    """Text a client sent, safe for a terminal or the journal: no control characters."""
    return UNPRINTABLE.sub("?", text)[:limit]


def program(agent: str) -> str:
    """A User-Agent, shortly: "Chrome 141 on Windows", "texas2step 2.5.0", "curl 8.5.0"."""
    if match := re.match(r"texas2step-engine/(\S+)", agent):
        return printable(f"texas2step {match[1]}", 40)
    for token, name in BROWSERS:
        if (at := agent.find(token)) >= 0:
            version = re.match(r"\d+", agent[at + len(token) :])
            system = next((s for mark, s in SYSTEMS if mark in agent), None)
            text = f"{name} {version[0]}" if version else name
            return printable(f"{text} on {system}" if system else text, 40)
    first = agent.split(" ", 1)[0].replace("/", " ", 1)
    return printable(first, 40) or "unknown"


def duration(seconds: float) -> str:
    """Such as "8s", "12 min", "1 h 5 min"."""
    seconds = max(0, round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60} min" if minutes % 60 else f"{minutes // 60} h"


def describe(doing: dict[str, int]) -> list[str]:
    """What a session is doing, in words, in a steady order."""
    words = []
    for activity in LABELS:
        n = doing.get(activity, 0)
        if n <= 0:
            continue
        label = LABELS[activity]
        if activity in STREAMS:
            label += f", {n} stream{'' if n == 1 else 's'}"
        words.append(label)
    return words


def who(session: str | None, what: str, page: str | None, agent: str, addresses: list[str]) -> str:
    """A session for the journal, such as "k7q2m9xd started: Monitor, Chrome 141 on
    Windows, from 203.0.113.7"."""
    page_name = PAGES.get(page or "", "no page named")
    where = ", ".join(addresses) or "unknown"
    return f"{session or '(no id)'} {what}: {page_name}, {program(agent)}, from {where}"


@dataclass
class Seen:
    """One session, as this worker sees it."""

    key: str
    session: str | None
    page: str | None
    agent: str
    first: float
    last: float
    page_at: float = 0.0
    addresses: dict[str, float] = field(default_factory=dict)  # each, and when last seen
    sent: int = 0
    received: int = 0
    doing: Counter[str] = field(default_factory=Counter)  # requests and sockets open now
    recent: dict[str, float] = field(default_factory=dict)  # when each was last done
    dirty: bool = True  # changed since it was last written


@dataclass
class Session:
    """One session, as all the workers together see it."""

    key: str
    session: str | None
    page: str | None
    agent: str
    addresses: list[str]
    first: float
    last: float
    sent: int
    received: int
    doing: Counter[str] = field(default_factory=Counter)

    @property
    def label(self) -> str:
        return self.session or "(no id)"

    def public(self, now: float) -> dict[str, Any]:
        return {
            "session": self.session,
            "page": self.page,
            "program": program(self.agent),
            "user_agent": self.agent,
            "addresses": self.addresses,
            "started": self.first,
            "last_seen": self.last,
            "seconds": round(self.last - self.first if self.ended(now) else now - self.first, 1),
            "sent": self.sent,
            "received": self.received,
            "doing": describe(self.doing),
            "activities": dict(self.doing),
        }

    def ended(self, now: float) -> bool:
        return self.last < now - IDLE

    def line(self, what: str) -> str:
        return who(self.session, what, self.page, self.agent, self.addresses)


@dataclass
class Board:
    """What the file says: the sessions active now, those that ended in the last hour (the
    latest first), and whether the list was too full for a new session in the last minute."""

    active: list[Session]
    ended: list[Session]
    full: bool

    def public(self, now: float, limit: int) -> dict[str, Any]:
        return {
            "time": now,
            "idle_seconds": IDLE,
            "history_seconds": HISTORY,
            "clients_max": limit,
            "full": self.full,
            "clients": [s.public(now) for s in self.active],
            "ended": [s.public(now) for s in self.ended],
        }


def merge(rows: list[sqlite3.Row], now: float) -> list[Session]:
    """The workers' rows as one Session each, the oldest first."""
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(row["key"], []).append(row)
    sessions = []
    for key, group in grouped.items():
        latest = max(group, key=lambda r: r["last"])
        named = max(group, key=lambda r: r["page_at"])
        addresses: dict[str, float] = {}
        doing: Counter[str] = Counter()
        for row in group:
            for ip, at in json.loads(row["addresses"]):
                addresses[ip] = max(addresses.get(ip, 0), at)
            if row["written"] >= now - FRESH:
                doing.update(json.loads(row["doing"]))
        sessions.append(
            Session(
                key=key,
                session=latest["session"],
                page=named["page"],
                agent=latest["agent"],
                addresses=sorted(addresses, key=lambda ip: -addresses[ip])[:ADDRESSES],
                first=min(r["first"] for r in group),
                last=latest["last"],
                sent=sum(r["sent"] for r in group),
                received=sum(r["received"] for r in group),
                doing=+doing,
            )
        )
    return sorted(sessions, key=lambda s: s.first)


def _ended(row: sqlite3.Row) -> Session:
    return Session(
        key=str(row["id"]),
        session=row["session"],
        page=row["page"],
        agent=row["agent"],
        addresses=json.loads(row["addresses"]),
        first=row["first"],
        last=row["last"],
        sent=row["sent"],
        received=row["received"],
    )


def connect(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=10)
    db.row_factory = sqlite3.Row
    return db


def read(path: str, now: float | None = None) -> Board:
    """The sessions in the file the workers write. FileNotFoundError before the service
    has made it."""
    now = time.time() if now is None else now
    if path != ":memory:" and not Path(path).exists():
        raise FileNotFoundError(path)
    db = connect(path)
    try:
        prepare(db, path, SCHEMA)
        rows = db.execute("SELECT * FROM seen").fetchall()
        ended = db.execute(
            "SELECT * FROM ended WHERE last >= ? ORDER BY last DESC", (now - HISTORY,)
        ).fetchall()
        full = db.execute("SELECT seen FROM notices WHERE name = 'full'").fetchone()
    finally:
        db.close()
    # A worker's quiet row still counts while another worker serves the session.
    active = [s for s in merge(rows, now) if not s.ended(now)]
    return Board(active, [_ended(r) for r in ended], bool(full and full["seen"] >= now - IDLE))


class Clients:
    """This worker's sessions, and its share of the file."""

    def __init__(self, path: str, rtc: Any = None, limit: int = LIMIT) -> None:
        self.path = path
        self.rtc = rtc  # the UDP probe's sessions (rtc.RtcEcho), whose bytes count here too
        self.limit = limit  # clients_max
        self.worker = os.getpid()
        self.seen: dict[str, Seen] = {}
        self.active: set[str] = set()  # sessions active on any worker, at the last flush
        self.left_out = 0  # new sessions not listed since the last flush: the list was full
        self._look = True  # read the file at the first flush: sessions left from before
        self._udp: dict[str, tuple[int, int]] = {}  # each UDP session's bytes counted so far
        self._told: dict[str, float] = {}  # sessions the journal knows of: their page's time
        self._refused: OrderedDict[str, float] = OrderedDict()  # left out for want of room
        self._db: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        self._failed = False

    # --- Seeing requests ------------------------------------------------------------

    def key(self, conn: HTTPConnection, ip: str) -> tuple[str, str | None, str | None, str]:
        """The session a request belongs to: its key, the id and page it named, its agent."""
        query = parse_qs(conn.scope.get("query_string", b"").decode("latin-1"))
        session = (query.get("session") or [""])[0]
        page = (query.get("page") or [""])[0]
        agent = printable(conn.headers.get("user-agent", ""), 300)
        if not SESSION_ID.fullmatch(session):
            digest = hashlib.sha256(f"{ip}\n{agent}".encode()).hexdigest()[:16]
            return f"~{digest}", None, page if page in PAGES else None, agent
        return session, session, page if page in PAGES else None, agent

    def full(self) -> bool:
        """Whether a new session would go past clients_max: this worker holds that many,
        or the list did at the last flush. Between flushes the workers may each take some;
        the file has the last word (_tell)."""
        return len(self.seen) >= self.limit or len(self.active) >= self.limit

    def begin(
        self, key: str, session: str | None, page: str | None, agent: str, ip: str, activity: str
    ) -> Seen | None:
        """A request starts: its session, or None if the list is too full for a new one."""
        now = time.time()
        seen = self.seen.get(key)
        if seen is None or (seen.last < now - IDLE and not seen.doing and key not in self.active):
            if seen is None and key not in self.active:
                if self._refused.get(key, 0) > now - IDLE:
                    return None  # refused a moment ago: tried again after a minute
                self._refused.pop(key, None)
                if self.full():
                    self.left_out += 1
                    self._refuse(key, now)
                    return None
            seen = self.seen[key] = Seen(key, session, None, agent, first=now, last=now)
        seen.last = now
        if page and page != seen.page:
            seen.page, seen.page_at = page, now
        seen.agent = agent or seen.agent
        seen.addresses[printable(ip, 64)] = now
        if len(seen.addresses) > ADDRESSES:
            del seen.addresses[min(seen.addresses, key=seen.addresses.__getitem__)]
        seen.doing[activity] += 1
        seen.dirty = True
        return seen

    def _refuse(self, key: str, now: float) -> None:
        """Leave a session out for a minute, so its next requests aren't counted again."""
        self._refused[key] = now
        while len(self._refused) > self.limit:
            self._refused.popitem(last=False)  # the oldest

    def end(self, seen: Seen, activity: str) -> None:
        now = time.time()
        seen.doing[activity] -= 1
        if seen.doing[activity] <= 0:
            del seen.doing[activity]
        seen.recent[activity] = now
        seen.last = now
        seen.dirty = True

    def _udp_sessions(self, now: float) -> dict[str, Counter[str]]:
        """Add what each UDP probe session moved since the last flush to its page's session,
        and say what each is doing."""
        doing: dict[str, Counter[str]] = {}
        sessions = getattr(self.rtc, "sessions", {})
        clock = time.monotonic()
        for rid, s in sessions.items():
            seen = self.seen.get(s.client) if s.client else None
            if seen is None:
                continue
            got, gave = self._udp.get(rid, (0, 0))
            if (s.bytes_in, s.bytes_out) != (got, gave):
                seen.received += s.bytes_in - got
                seen.sent += s.bytes_out - gave
                self._udp[rid] = (s.bytes_in, s.bytes_out)
                seen.dirty = True
            last = now - (clock - s.last_seen)
            if last > seen.last:
                seen.last = last
                seen.dirty = True
            if last >= now - RECENT:
                doing.setdefault(seen.key, Counter())[f"udp:{s.profile}"] += 1
            with contextlib.suppress(Exception):
                if (ip := s.addresses()["ip"]) and str(ip) not in seen.addresses:
                    seen.addresses[printable(str(ip), 64)] = last
        for rid in [rid for rid in self._udp if rid not in sessions]:
            del self._udp[rid]
        return doing

    def _rows(self, now: float) -> list[dict[str, Any]]:
        """The sessions that changed since the last flush, as rows to write."""
        udp = self._udp_sessions(now)
        rows = []
        for seen in self.seen.values():
            if seen.doing:
                seen.last = now  # a download or an open socket is active as it runs
                seen.dirty = True
            if not seen.dirty:
                continue
            seen.dirty = False
            doing = Counter({a: n for a, n in seen.doing.items() if a != "page"})
            for activity, at in seen.recent.items():
                if activity != "page" and at >= now - RECENT:
                    doing[activity] = max(doing[activity], 1)
            doing.update(udp.get(seen.key, {}))
            addresses = sorted(seen.addresses.items(), key=lambda a: -a[1])
            rows.append(
                {
                    "key": seen.key,
                    "worker": self.worker,
                    "session": seen.session,
                    "page": seen.page,
                    "page_at": seen.page_at,
                    "agent": seen.agent,
                    "ip": addresses[0][0] if addresses else "unknown",
                    "addresses": json.dumps(addresses),
                    "first": seen.first,
                    "last": seen.last,
                    "sent": seen.sent,
                    "received": seen.received,
                    "doing": json.dumps(doing),
                    "written": now,
                }
            )
        return rows

    # --- Writing them down ----------------------------------------------------------

    async def flush(self, now: float | None = None) -> None:
        """Write this worker's changed sessions, end those quiet everywhere, keep the last
        hour's within clients_max, and tell the journal."""
        now = time.time() if now is None else now
        rows = self._rows(now)
        left, self.left_out = self.left_out, 0
        if not rows and not self.active and not left and not self._look:
            return
        self._look = False
        refused: set[str] = set()
        try:
            active, lines, warnings, refused = await asyncio.to_thread(self._write, rows, now, left)
        except (OSError, sqlite3.Error) as exc:
            if not self._failed:
                log.warning("clients: can't write %s: %s", self.path, exc)
            self._failed = True
            for seen in self.seen.values():
                seen.dirty = True  # all of it again, next time
            active = {k for k, s in self.seen.items() if s.last >= now - IDLE}
            lines, warnings = [], []
        else:
            self._failed = False
        self.active = active
        for key in refused:  # no room in the file: no longer followed here
            self.seen.pop(key, None)
            self._refuse(key, now)
        for key, seen in list(self.seen.items()):
            if not seen.doing and seen.last < now - IDLE and key not in active:
                del self.seen[key]
        for line in lines:
            log.info("clients: %s", line)
        for line in warnings:
            log.warning("clients: %s", line)

    def _open(self) -> sqlite3.Connection:
        if self._db is None:
            if self.path != ":memory:":
                create_owned(Path(self.path))
            db = connect(self.path)
            prepare(db, self.path, SCHEMA)
            db.execute("PRAGMA synchronous=NORMAL")
            self._db = db
        return self._db

    def _write(
        self, rows: list[dict[str, Any]], now: float, left: int
    ) -> tuple[set[str], list[str], list[str], set[str]]:
        with self._lock:
            db = self._open()
            db.execute("BEGIN IMMEDIATE")
            try:
                lines, refused = self._tell(db, rows)
                db.executemany(
                    "INSERT OR REPLACE INTO seen (key, worker, session, page, page_at, agent, ip, "
                    "addresses, first, last, sent, received, doing, written) VALUES (:key, "
                    ":worker, :session, :page, :page_at, :agent, :ip, :addresses, :first, "
                    ":last, :sent, :received, :doing, :written)",
                    [row for row in rows if row["key"] not in refused],
                )
                active = {
                    r["key"]
                    for r in db.execute(
                        "SELECT key FROM seen GROUP BY key HAVING MAX(last) >= ?", (now - IDLE,)
                    )
                }
                lines += self._end(db, now)
                self._told = {k: at for k, at in self._told.items() if k in active}
                self._trim(db, now, len(active))
                warnings = self._warn(db, now, left + len(refused), len(active))
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        return active, lines, warnings, refused

    def _tell(
        self, db: sqlite3.Connection, rows: list[dict[str, Any]]
    ) -> tuple[list[str], set[str]]:
        """Claim the start of each new session, and each move to another page, for the
        journal: once for all the workers. A new session claimed with clients_max already
        listed isn't: it is refused, which keeps the list to clients_max exactly, as one
        worker writes at a time."""
        lines: list[str] = []
        refused: set[str] = set()
        (listed,) = db.execute("SELECT COUNT(*) FROM told").fetchone()
        for row in rows:
            key, page, at = row["key"], row["page"], row["page_at"]
            if key not in self._told:
                if (
                    listed >= self.limit
                    and not db.execute("SELECT 1 FROM told WHERE key = ?", (key,)).fetchone()
                ):
                    refused.add(key)
                    continue
                claim = db.execute(
                    "INSERT OR IGNORE INTO told (key, page, page_at) VALUES (?, ?, ?)",
                    (key, page, at),
                )
                if claim.rowcount == 1:
                    listed += 1
                    self._told[key] = at
                    ips = [ip for ip, _ in json.loads(row["addresses"])]
                    lines.append(who(row["session"], "started", page, row["agent"], ips))
                    continue
                self._told[key] = 0.0  # another worker told it: this one's page may be newer
            if page and at > self._told[key]:
                self._told[key] = at
                moved = db.execute(
                    "UPDATE told SET page = ?, page_at = ? "
                    "WHERE key = ? AND page_at < ? AND page IS NOT ?",
                    (page, at, key, at, page),
                )
                if moved.rowcount == 1:
                    lines.append(f"{row['session'] or '(no id)'} now on {PAGES[page]}")
        return lines, refused

    @staticmethod
    def _end(db: sqlite3.Connection, now: float) -> list[str]:
        """End the sessions quiet on every worker: tell the journal once, and keep each in
        the last hour's list."""
        lines = []
        quiet = db.execute(
            "SELECT key FROM seen GROUP BY key HAVING MAX(last) < ?", (now - IDLE,)
        ).fetchall()
        for (key,) in quiet:
            (s,) = merge(db.execute("SELECT * FROM seen WHERE key = ?", (key,)).fetchall(), now)
            if db.execute("DELETE FROM told WHERE key = ?", (key,)).rowcount == 1:
                moved = size(s.sent + s.received)
                lines.append(
                    s.line("ended") + f", after {duration(s.last - s.first)}, moved {moved}"
                )
            db.execute(
                "INSERT INTO ended (session, page, agent, addresses, first, last, sent, received) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    s.session,
                    s.page,
                    s.agent,
                    json.dumps(s.addresses),
                    s.first,
                    s.last,
                    s.sent,
                    s.received,
                ),
            )
            db.execute("DELETE FROM seen WHERE key = ?", (key,))
        return lines

    def _trim(self, db: sqlite3.Connection, now: float, active: int) -> None:
        """Keep the last hour's ended sessions, and of them only as many as clients_max has
        room for beside the active ones: the oldest go first."""
        db.execute("DELETE FROM ended WHERE last < ?", (now - HISTORY,))
        room = max(0, self.limit - active)
        (count,) = db.execute("SELECT COUNT(*) FROM ended").fetchone()
        if count > room:
            db.execute(
                "DELETE FROM ended WHERE id IN (SELECT id FROM ended ORDER BY last LIMIT ?)",
                (count - room,),
            )
        db.execute("DELETE FROM told WHERE key NOT IN (SELECT key FROM seen)")

    def _warn(self, db: sqlite3.Connection, now: float, left: int, active: int) -> list[str]:
        """While new sessions go unlisted, warn the journal every WARN_EVERY seconds, once
        for all the workers, with the addresses that have the most sessions."""
        if not left:
            return []
        db.execute(
            "INSERT INTO notices (name, seen, unlisted) VALUES ('full', ?, ?) ON CONFLICT (name) "
            "DO UPDATE SET seen = excluded.seen, unlisted = unlisted + excluded.unlisted",
            (now, left),
        )
        claim = db.execute(
            "UPDATE notices SET told = ? WHERE name = 'full' AND told < ?", (now, now - WARN_EVERY)
        )
        if claim.rowcount != 1:
            return []
        (unlisted,) = db.execute("SELECT unlisted FROM notices WHERE name = 'full'").fetchone()
        db.execute("UPDATE notices SET unlisted = 0 WHERE name = 'full'")
        most = db.execute(
            "SELECT ip, COUNT(DISTINCT key) AS n FROM seen GROUP BY ip ORDER BY n DESC LIMIT 3"
        ).fetchall()
        tops = ", ".join(f"{r['ip']} ({r['n']:,})" for r in most)
        return [
            f"the list is full: {active:,} sessions active, clients_max = {self.limit:,}; "
            f"{unlisted:,} new ones weren't listed. The most sessions come from {tops}"
        ]

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None


async def keep(clients: Clients) -> None:
    """Flush every FLUSH seconds, for as long as the server runs."""
    while True:
        await asyncio.sleep(FLUSH)
        await clients.flush()
