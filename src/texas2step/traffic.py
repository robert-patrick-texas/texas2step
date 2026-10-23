"""The traffic tests move, and a daily and monthly limit on it.

Clouds and some hosting providers bill a server's traffic, so a site can cap what the
tests move in a day (``traffic_day_gb``) and in a month (``traffic_month_gb``), counting
what the server sends, what it receives, or both (``traffic_counts``). Days and months
begin at midnight in ``access_timezone``. Traffic with ``internal_networks`` counts too,
unless ``traffic_count_internal`` is off; the server's own loopback traffic never does.

Each worker adds up the bytes of the download and upload tests it serves, and of the
tests it runs toward other servers (/api/v1/measure), and adds them once a second to a
small SQLite file (``traffic_file``) that the other worker and the command line share.

A limit refuses new tests, not running ones. The browser and the CLI give each test an id
(``?test=``), and requests carrying one the server has let in keep going for up to
``TEST_SECONDS``. So the tests running when a limit is reached carry on past it. Monitor,
Diag and the UDP probe move a few hundred bytes a second and aren't counted.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as clock
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .access import address, is_internal
from .store import create_owned, prepare

log = logging.getLogger(__name__)

GB = 1_000_000_000  # as providers bill; a limit errs low against one billing 2**30
MESSAGE = "Testing temporarily disabled."  # traffic_message
TEST_SECONDS = 600  # how long a test let in before a limit was reached may keep going
KEEP_DAYS = 400  # days of figures kept for texas2step traffic
OUTSIDE, INSIDE, LOCAL = "outside", "inside", "local"  # whose traffic: see TrafficMeter.kind

SCHEMA = """
CREATE TABLE IF NOT EXISTS days (
    day             TEXT PRIMARY KEY,           -- 2026-10-23, in access_timezone
    sent            INTEGER NOT NULL DEFAULT 0, -- with clients outside internal_networks
    received        INTEGER NOT NULL DEFAULT 0,
    inside_sent     INTEGER NOT NULL DEFAULT 0, -- with clients in internal_networks
    inside_received INTEGER NOT NULL DEFAULT 0,
    extra           INTEGER NOT NULL DEFAULT 0  -- allowed by hand: texas2step traffic allow
);
CREATE TABLE IF NOT EXISTS notices (
    period TEXT PRIMARY KEY,  -- a day, or a month such as 2026-10
    level  INTEGER NOT NULL   -- the share of its allowance the journal was told of: 80, 100
);
CREATE TABLE IF NOT EXISTS tests (
    id      TEXT PRIMARY KEY, -- the ?test= of a test let in
    started REAL NOT NULL
);
"""
FIGURES = "sent, received, inside_sent, inside_received, extra"


@dataclass(frozen=True)
class Usage:
    sent: int = 0
    received: int = 0
    inside_sent: int = 0
    inside_received: int = 0
    extra: int = 0


@dataclass(frozen=True)
class Period:
    """Today or this month: what counted against what was allowed."""

    key: str  # 2026-10-23, or 2026-10
    whose: str  # "today's", "this month's"
    usage: Usage
    counted: int
    allowance: int | None  # the limit and any bytes allowed by hand; None without a limit
    ends: datetime
    starts: str  # when the next one starts: "midnight", "November 1"

    @property
    def full(self) -> bool:
        return self.allowance is not None and self.counted >= self.allowance

    @property
    def again(self) -> str:
        return ("at " if len(self.key) == 10 else "on ") + self.starts


@dataclass(frozen=True)
class Refusal:
    until: datetime
    reason: str  # for the desk: which allowance, and when tests start again


class Closed(Exception):
    """A limit refused a test; the app answers 503 with what visitors are told."""

    def __init__(self, notice: dict[str, Any]) -> None:
        super().__init__(notice["detail"])
        self.notice = notice


def size(n: float) -> str:
    """Such as 8.2 GB, 10 GB, 1,000 GB (as providers bill) or 350 MB."""
    for unit, scale in (("GB", GB), ("MB", 10**6), ("KB", 10**3)):
        if abs(n) >= scale:
            return f"{n / scale:,.1f}".removesuffix(".0") + f" {unit}"
    return f"{n:,.0f} bytes"


class TrafficMeter:
    def __init__(
        self,
        path: str | Path,
        *,
        day_gb: float = 0.0,
        month_gb: float = 0.0,
        counts: str = "both",
        internal: bool = True,
        zone: str = "UTC",
        networks: list[str] | None = None,
        message: str = MESSAGE,
        url: str = "",
    ) -> None:
        self.path = str(path)
        self.message = message
        self.url = url
        self.day_limit = round(day_gb * GB) or None
        self.month_limit = round(month_gb * GB) or None
        self.counts = counts
        self.internal = internal
        self.zone = ZoneInfo(zone)
        self.networks = list(networks or [])
        self._lock = threading.Lock()  # the connection, and the tests let in
        self._pending_lock = threading.Lock()
        self._pending = [0, 0, 0, 0]  # sent, received, inside_sent, inside_received
        self._admitted: dict[str, float] = {}
        self._pruned = 0.0
        if self.path != ":memory:":
            create_owned(Path(self.path))
        self._db = sqlite3.connect(
            self.path, check_same_thread=False, isolation_level=None, timeout=10
        )
        with self._lock:
            prepare(self._db, self.path, SCHEMA)
            self._db.execute("PRAGMA synchronous=NORMAL")

    @classmethod
    def load(cls, settings: Any) -> TrafficMeter | None:
        """The server's meter, or None while it has no traffic limit."""
        if not settings.traffic_limited:
            return None
        return cls.of(settings)

    @classmethod
    def of(cls, settings: Any) -> TrafficMeter:
        return cls(
            settings.traffic_file,
            day_gb=settings.traffic_day_gb,
            month_gb=settings.traffic_month_gb,
            counts=settings.traffic_counts,
            internal=settings.traffic_count_internal,
            zone=settings.access_timezone,
            networks=settings.internal_networks,
            message=settings.traffic_message,
            url=settings.traffic_url,
        )

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # --- Counting -----------------------------------------------------------------

    def kind(self, ip: str) -> str:
        """Whose traffic: outside internal_networks, inside them, or this server's own."""
        addr = address(ip)
        if addr is not None and addr.is_loopback:
            return LOCAL
        return INSIDE if is_internal(ip, self.networks) else OUTSIDE

    def counted_for(self, kind: str) -> bool:
        return kind == OUTSIDE or (kind == INSIDE and self.internal)

    def add(self, kind: str, sent: int = 0, received: int = 0) -> None:
        """Bytes a test moved; written to the file by the next flush()."""
        if kind == LOCAL:
            return
        i = 2 if kind == INSIDE else 0
        with self._pending_lock:
            self._pending[i] += sent
            self._pending[i + 1] += received

    def flush(self, now: float | None = None) -> None:
        """Add this worker's bytes to the file, and tell the journal of 80% and 100%."""
        now = time.time() if now is None else now
        with self._pending_lock:
            pending, self._pending = self._pending, [0, 0, 0, 0]
        if any(pending):
            try:
                with self._lock:
                    self._db.execute(
                        "INSERT INTO days (day, sent, received, inside_sent, inside_received) "
                        "VALUES (?, ?, ?, ?, ?) ON CONFLICT (day) DO UPDATE SET "
                        "sent = sent + excluded.sent, received = received + excluded.received, "
                        "inside_sent = inside_sent + excluded.inside_sent, "
                        "inside_received = inside_received + excluded.inside_received",
                        (self._today(now).isoformat(), *pending),
                    )
            except sqlite3.Error:
                self._restore(pending)
                raise
            self._notify(now)
        if now - self._pruned >= 60:
            self._prune(now)

    def _restore(self, pending: list[int]) -> None:
        with self._pending_lock:
            self._pending = [a + b for a, b in zip(self._pending, pending, strict=True)]

    def _notify(self, now: float) -> None:
        for period in self.periods(now):
            if period.allowance is None:
                continue
            level = 100 if period.full else 80 if period.counted * 5 >= period.allowance * 4 else 0
            if not level:
                continue
            with self._lock:
                self._db.execute("INSERT OR IGNORE INTO notices VALUES (?, 0)", (period.key,))
                told = self._db.execute(
                    "UPDATE notices SET level = ? WHERE period = ? AND level < ?",
                    (level, period.key, level),
                ).rowcount
            if not told:
                continue  # the other worker told it, or it was told before
            used, allowed = size(period.counted), size(period.allowance)
            if level == 100:
                log.warning(
                    "traffic: %s allowance of %s is used up (%s); new tests are refused "
                    "until %s (%s)",
                    period.whose,
                    allowed,
                    used,
                    period.starts,
                    self.zone.key,
                )
            else:
                log.warning(
                    "traffic: 80%% of %s allowance is used: %s of %s", period.whose, used, allowed
                )

    def _prune(self, now: float) -> None:
        cutoff = (self._today(now) - timedelta(days=KEEP_DAYS)).isoformat()
        with self._lock:
            self._db.execute("DELETE FROM tests WHERE started < ?", (now - TEST_SECONDS,))
            self._db.execute("DELETE FROM days WHERE day < ?", (cutoff,))
            self._db.execute("DELETE FROM notices WHERE period < ?", (cutoff[:7],))
            self._admitted = {t: at for t, at in self._admitted.items() if now - at < TEST_SECONDS}
        self._pruned = now

    # --- The limit ----------------------------------------------------------------

    def admit(self, test: str | None, kind: str, now: float | None = None) -> Refusal | None:
        """Let a download or upload request in, unless a limit refuses it.

        A request of a test let in before stays in: running tests finish."""
        if not self.counted_for(kind):
            return None
        now = time.time() if now is None else now
        if test and self._known(test, now):
            return None
        if refusal := self.refusal(*self.periods(now)):
            return refusal
        if test:
            with self._lock:
                self._db.execute("INSERT OR IGNORE INTO tests VALUES (?, ?)", (test, now))
                self._admitted[test] = now
        return None

    def _known(self, test: str, now: float) -> bool:
        with self._lock:
            started = self._admitted.get(test)
            if started is None:
                row = self._db.execute("SELECT started FROM tests WHERE id = ?", (test,))
                if found := row.fetchone():
                    started = self._admitted[test] = found[0]
        return started is not None and now - started < TEST_SECONDS

    def refusal(self, day: Period, month: Period) -> Refusal | None:
        full = [p for p in (day, month) if p.full]
        if not full:
            return None
        last = max(full, key=lambda p: p.ends)
        return Refusal(
            last.ends,
            f"{last.whose.capitalize()} allowance is used up; "
            f"tests start again {last.again} ({self.zone.key})",
        )

    def notice(self) -> dict[str, Any]:
        """What a refused visitor is told: the site's message and link, never when tests
        start again."""
        link = f" Click here to continue: {self.url}" if self.url else ""
        return {"detail": self.message + link, "message": self.message, "url": self.url or None}

    def periods(self, now: float | None = None) -> tuple[Period, Period]:
        """Today and this month, with this worker's bytes not yet in the file."""
        now = time.time() if now is None else now
        today = self._today(now)
        first = today.replace(day=1)
        with self._lock:
            day = self._db.execute(
                f"SELECT {FIGURES} FROM days WHERE day = ?", (today.isoformat(),)
            ).fetchone()
            sums = ", ".join(f"coalesce(sum({f}), 0)" for f in FIGURES.split(", "))
            month = self._db.execute(
                f"SELECT {sums} FROM days WHERE day >= ? AND day < ?",
                (first.isoformat(), _next_month(first).isoformat()),
            )
            month = month.fetchone()
        with self._pending_lock:
            pending = [*self._pending, 0]
        tomorrow, next_month = today + timedelta(days=1), _next_month(first)
        return (
            self._period(
                today.isoformat(), "today's", day, pending, self.day_limit, tomorrow, "midnight"
            ),
            self._period(
                today.isoformat()[:7],
                "this month's",
                month,
                pending,
                self.month_limit,
                next_month,
                f"{next_month:%B} 1",
            ),
        )

    def _period(
        self,
        key: str,
        whose: str,
        row: tuple[int, ...] | None,
        pending: list[int],
        limit: int | None,
        ends: date,
        starts: str,
    ) -> Period:
        figures = [a + b for a, b in zip(row or (0, 0, 0, 0, 0), pending, strict=True)]
        usage = Usage(*figures)
        sent = usage.sent + (usage.inside_sent if self.internal else 0)
        received = usage.received + (usage.inside_received if self.internal else 0)
        counted = {"sent": sent, "received": received}.get(self.counts, sent + received)
        allowance = limit + usage.extra if limit else None
        start = datetime.combine(ends, clock(), tzinfo=self.zone)
        return Period(key, whose, usage, counted, allowance, start, starts)

    def _today(self, now: float) -> date:
        return datetime.fromtimestamp(now, self.zone).date()

    def allow(self, gb: float, now: float | None = None) -> tuple[Period, Period]:
        """Add to today's allowance, and so this month's (texas2step traffic allow)."""
        now = time.time() if now is None else now
        today = self._today(now).isoformat()
        with self._lock:
            self._db.execute("INSERT OR IGNORE INTO days (day) VALUES (?)", (today,))
            self._db.execute(
                "UPDATE days SET extra = max(0, extra + ?) WHERE day = ?", (round(gb * GB), today)
            )
            # Tell the journal again as the new allowance fills.
            self._db.execute("DELETE FROM notices WHERE period IN (?, ?)", (today, today[:7]))
        return self.periods(now)

    def days(self, count: int = 30) -> list[tuple[str, Usage]]:
        """The most recent days with traffic, newest first."""
        with self._lock:
            rows = self._db.execute(
                f"SELECT day, {FIGURES} FROM days ORDER BY day DESC LIMIT ?", (count,)
            ).fetchall()
        return [(row[0], Usage(*row[1:])) for row in rows]

    def public(self, kind: str, now: float | None = None) -> dict[str, Any]:
        """For /api/v1/info: whether this client may test, and if not, what it is told. The
        desk also gets the figures, and when tests start again."""
        now = time.time() if now is None else now
        day, month = self.periods(now)
        limit = self.refusal(day, month)
        refused = limit is not None and self.counted_for(kind)
        out: dict[str, Any] = {
            "open": not refused,
            "message": self.message if refused else None,
            "url": (self.url or None) if refused else None,
        }
        if kind != OUTSIDE:
            out |= {
                "until": limit.until.isoformat() if limit else None,
                "reason": limit.reason if limit else None,
                "counts": self.counts,
                "count_internal": self.internal,
                "today": {"counted": day.counted, "allowance": day.allowance},
                "month": {"counted": month.counted, "allowance": month.allowance},
            }
        return out


def _next_month(first: date) -> date:
    return (first + timedelta(days=32)).replace(day=1)


async def keep(meter: TrafficMeter) -> None:
    """Flush the meter once a second, for as long as the server runs."""
    while True:
        await asyncio.sleep(1)
        try:
            await asyncio.to_thread(meter.flush)
        except sqlite3.Error as exc:
            log.warning("traffic: can't write %s: %s", meter.path, exc)
