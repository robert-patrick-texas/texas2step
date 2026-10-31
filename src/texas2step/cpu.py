"""The server's CPU: what texas2step's workers use, what the rest of the host uses, and
the traffic the workers serve meanwhile (docs/cpu.md).

Every ``SAMPLE`` seconds each worker writes a row to a small SQLite file (``cpu_file``)
that the other worker, the command line and the API read: the CPU time and memory it has
used, the bytes its tests moved over TCP (downloads, uploads and WebSocket echoes), the
UDP probes it took in, the UDP sessions it has open, and the host's CPU counters from
/proc/stat with nginx's share. The rows are kept ``RAW_KEEP`` seconds; each minute is
summed into one row, kept ``KEEP_DAYS`` days.

**Other work** is the host's busy time less texas2step's and nginx's, which carries the
tests' TCP traffic: what the host's other programs use, as a share of all its cores. The
kernel's interrupt time, mostly its network processing, counts for neither side, as at
100 Gb/s the tests' own traffic would otherwise count against the next test. A virtual
machine's steal time counts as other work: the host under it gave the core to another.

With ``cpu_busy_percent`` set, new download and upload tests, tests toward other servers
and Monitor's Video call are refused while other work, averaged over ``cpu_busy_seconds``,
is at least that share, and Video calls already open are held to their Voice call. Tests
let in before go on: the browser and the CLI give each an id (``?test=``), as a traffic
limit uses it (traffic.py).

A UDP probe costs about the same CPU whatever its size, so Monitor's calls cost CPU out of
proportion to their bytes: five Video calls can take a third of a core while moving under
10 Mb/s. The figures show probes beside bytes for this reason. The journal is warned when
the workers use CPU with nothing to serve, when that idle CPU climbs from one week to the
next, and when the host's other work starts and stops keeping tests out.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sqlite3
import statistics
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .store import create_owned, prepare
from .tcpinfo import Held, Window
from .traffic import TEST_SECONDS

log = logging.getLogger(__name__)

SAMPLE = 5.0  # seconds between a worker's rows
NOW = 30.0  # seconds the figures for "now" are averaged over
RAW_KEEP = 7200.0  # seconds the rows are kept
KEEP_DAYS = 30  # days of minutes kept
SCAN = 60.0  # seconds between looks through /proc for nginx's processes
BOUND = 0.9  # a worker at this share of a core held a transfer back: "Server CPU-bound"
READY = 0.5  # the share of a download the server waited with bytes ready: the client held it
CLEAR = 0.9  # tests kept out are let in again under this share of cpu_busy_percent
QUIET_CPU = 0.10  # the workers' share of a core, with nothing to serve, worth a warning...
QUIET_MINUTES = 10  # ...for this many minutes in a row
WARN_EVERY = 3600.0  # seconds between those warnings
BASELINE_DAYS = 7  # the days a day's idle CPU is compared with
BASELINE_MINUTES = 60  # the quiet minutes a day needs for its idle CPU to count
BASELINE_RISE = 0.02  # a rise in idle CPU worth a warning: at least this much of a core...
BASELINE_FACTOR = 2.0  # ...and to at least this many times the days before
TESTS_KEEP = 3600.0  # seconds a test's CPU figures are kept for its client to ask
MESSAGE = "This server is busy. Try again in a few minutes."
PARTS = ("download", "one_stream", "upload")  # a test's transfers, as its result names them
TICK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
PAGE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    worker   INTEGER NOT NULL, -- the worker's process id
    at       REAL NOT NULL,
    cpu      REAL NOT NULL,    -- seconds of CPU the worker has used since it started
    memory   INTEGER NOT NULL, -- its resident memory, in bytes
    sent     INTEGER NOT NULL, -- bytes its tests sent over TCP since it started
    received INTEGER NOT NULL, -- and received
    probes   INTEGER NOT NULL, -- UDP probes it took in since it started
    sessions INTEGER NOT NULL, -- UDP sessions open now
    busy     REAL,             -- the host's busy time, all cores, in seconds; NULL off Linux
    total    REAL,             -- and all its time, idle included
    nginx    REAL,             -- seconds of CPU nginx's processes used, as this worker saw
    cores    INTEGER,
    PRIMARY KEY (worker, at)
);
CREATE INDEX IF NOT EXISTS samples_at ON samples (at);
CREATE TABLE IF NOT EXISTS minutes (
    minute     INTEGER PRIMARY KEY, -- its start, in seconds since 1970
    texas2step REAL NOT NULL,       -- the cores the workers used, together
    busiest    REAL NOT NULL,       -- the busiest worker's share of a core
    nginx      REAL,                -- the cores nginx used
    host       REAL,                -- the host's busy share of all its cores
    other      REAL,                -- other work's share of all its cores
    cores      INTEGER,
    sent       REAL NOT NULL,       -- bytes a second the tests sent over TCP
    received   REAL NOT NULL,
    probes     REAL NOT NULL,       -- UDP probes a second
    sessions   INTEGER NOT NULL,    -- the most UDP sessions open at once
    memory     INTEGER NOT NULL,    -- the workers' memory, together
    workers    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS tests (
    id      TEXT NOT NULL,    -- a test's ?test=
    part    TEXT NOT NULL,    -- download, one_stream or upload
    worker  INTEGER NOT NULL,
    cpu     REAL NOT NULL,    -- seconds of CPU the worker used while it served the transfer's
    seconds REAL NOT NULL,    -- requests, each from its first byte to its last, and those seconds
    at      REAL NOT NULL,
    PRIMARY KEY (id, part, worker)
);
CREATE TABLE IF NOT EXISTS windows (
    id      TEXT NOT NULL,    -- a test's ?test=
    part    TEXT NOT NULL,    -- download or one_stream
    worker  INTEGER NOT NULL,
    seconds REAL NOT NULL,    -- the requests' seconds on their connections, summed (tcpinfo.py)
    idle    REAL NOT NULL,    -- of those, with nothing in flight (HTTP/2 only: tcpinfo.Window)
    rwnd    REAL NOT NULL,    -- and held back by the client's TCP receive window
    rtt_sum REAL NOT NULL,    -- the kernel's RTTs while they ran, in ms, summed
    rtt_n   INTEGER NOT NULL, -- and counted
    min_rtt REAL,             -- the lowest RTT the kernel saw on the connections, ms
    ready   REAL NOT NULL,    -- seconds the server had the next bytes ready, waiting on them
    at      REAL NOT NULL,
    PRIMARY KEY (id, part, worker)
);
CREATE TABLE IF NOT EXISTS admitted (
    id TEXT PRIMARY KEY, -- a test let in while other work left room (cpu_busy_percent)
    at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS notices (
    name  TEXT PRIMARY KEY, -- what the journal was told of: busy, quiet, baseline
    at    REAL NOT NULL,
    state TEXT NOT NULL
);
"""
COLUMNS = "worker, at, cpu, memory, sent, received, probes, sessions, busy, total, nginx, cores"
MINUTE_COLUMNS = (
    "minute, texas2step, busiest, nginx, host, other, cores, sent, received, probes, "
    "sessions, memory, workers"
)


class Work:
    """What this worker's tests moved since it started, for the figures. The wire
    endpoints add to it; rtc.RtcEcho counts its own probes."""

    def __init__(self) -> None:
        self.sent = 0
        self.received = 0


def host_times() -> tuple[float, float, int] | None:
    """The host's busy and total CPU time in seconds, all cores together, and its cores;
    None without /proc/stat. Busy is user, nice, system and steal time: not idle, not
    waiting on disk, and not the kernel's interrupts."""
    try:
        with open("/proc/stat") as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    if not lines or not lines[0].startswith("cpu "):
        return None
    fields = [int(n) for n in lines[0].split()[1:9]]
    fields += [0] * (8 - len(fields))
    user, nice, system, _idle, _iowait, _irq, _softirq, steal = fields
    cores = sum(1 for line in lines if line.startswith("cpu") and line[3:4].isdigit())
    return (user + nice + system + steal) / TICK, sum(fields) / TICK, cores


def own_memory() -> int:
    try:
        with open("/proc/self/statm") as f:
            return int(f.read().split()[1]) * PAGE
    except (OSError, ValueError, IndexError):
        return 0


def _ticks(pid: int) -> tuple[bytes, int] | None:
    """A process's name and its CPU time in clock ticks, from /proc/<pid>/stat."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            stat = f.read()
    except OSError:
        return None
    # The name is in parentheses and may hold spaces; utime and stime are the 14th and
    # 15th fields, the 12th and 13th after it.
    left, right = stat.find(b"("), stat.rfind(b")")
    fields = stat[right + 2 :].split()
    try:
        return stat[left + 1 : right], int(fields[11]) + int(fields[12])
    except (IndexError, ValueError):
        return None


def _pids() -> list[int]:
    try:
        return [int(e.name) for e in os.scandir("/proc") if e.name.isdigit()]
    except OSError:
        return []


class Nginx:
    """The CPU nginx's processes have used since this worker first looked: they carry the
    tests' TCP traffic, so their work is the tests', not other work. Its workers are
    found by a look through /proc every ``SCAN`` seconds, and read in between."""

    def __init__(self) -> None:
        self.total = 0.0
        self._ticks: dict[int, int] = {}
        self._scanned = -SCAN  # the first read looks
        self._started = False

    def read(self, now: float) -> float:
        if now - self._scanned >= SCAN:
            self._scanned = now
            pids = _pids()
        else:
            pids = list(self._ticks)
        seen: dict[int, int] = {}
        for pid in pids:
            found = _ticks(pid)
            if found is not None and found[0] == b"nginx":
                seen[pid] = found[1]
        for pid, ticks in seen.items():
            before = self._ticks.get(pid)
            if before is not None:
                self.total += max(0, ticks - before) / TICK
            elif self._started:  # started since the last look, as after a reload
                self.total += ticks / TICK
        self._ticks = seen
        self._started = True
        return self.total


@dataclass(frozen=True)
class Worker:
    """One worker over a span: its CPU in shares of a core, its traffic each second."""

    pid: int
    cpu: float
    memory: int
    sessions: int
    probes: float
    sent: float
    received: float
    at: float = 0.0  # its last row's time


@dataclass(frozen=True)
class Span:
    """The figures over a span of time, from the rows in it."""

    seconds: float
    workers: list[Worker]
    nginx: float | None  # the cores nginx used
    host: float | None  # the host's busy share of all its cores
    cores: int | None

    @property
    def texas2step(self) -> float:
        """The cores the workers used, together."""
        return sum(w.cpu for w in self.workers)

    @property
    def other(self) -> float | None:
        """Other work's share of all the host's cores."""
        if self.host is None or not self.cores:
            return None
        ours = (self.texas2step + (self.nginx or 0.0)) / self.cores
        return max(0.0, self.host - ours)

    def public(self) -> dict[str, Any]:
        return {
            "seconds": round(self.seconds, 1),
            "texas2step": round(self.texas2step, 4),
            "nginx": _rounded(self.nginx),
            "host": _rounded(self.host),
            "other": _rounded(self.other),
            "cores": self.cores,
            "sessions": sum(w.sessions for w in self.workers),
            "probes_per_s": round(sum(w.probes for w in self.workers), 1),
            "sent_mbps": round(sum(w.sent for w in self.workers) * 8 / 1e6, 3),
            "received_mbps": round(sum(w.received for w in self.workers) * 8 / 1e6, 3),
            "workers": [
                {
                    "pid": w.pid,
                    "cpu": round(w.cpu, 4),
                    "memory": w.memory,
                    "sessions": w.sessions,
                    "probes_per_s": round(w.probes, 1),
                    "sent_mbps": round(w.sent * 8 / 1e6, 3),
                    "received_mbps": round(w.received * 8 / 1e6, 3),
                }
                for w in self.workers
            ],
        }


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


def span(
    rows: list[sqlite3.Row],
    alive: float | None = None,
    start: float | None = None,
    length: float | None = None,
) -> Span | None:
    """The figures from each worker's first row to its last; None with no worker that
    has two. With ``alive``, only the workers with a row since then. With ``start``, each
    worker's figures begin at its latest row by then, or its first after, and are spread
    over ``length`` seconds: so the minutes, which share their boundary rows, add up."""
    by_worker: dict[int, list[sqlite3.Row]] = {}
    for row in rows:
        by_worker.setdefault(row["worker"], []).append(row)
    workers: list[Worker] = []
    host: tuple[float, sqlite3.Row, sqlite3.Row] | None = None
    longest = 0.0
    for pid, mine in sorted(by_worker.items()):
        if start is not None:
            before = [r for r in mine if r["at"] <= start]
            mine = before[-1:] + [r for r in mine if r["at"] > start]
        first, last = mine[0], mine[-1]
        took = last["at"] - first["at"]
        if took <= 0 or (alive is not None and last["at"] < alive):
            continue
        longest = max(longest, took)
        over = length or took
        workers.append(
            Worker(
                pid,
                max(0.0, (last["cpu"] - first["cpu"]) / over),
                last["memory"],
                max(r["sessions"] for r in mine),
                max(0, last["probes"] - first["probes"]) / over,
                max(0, last["sent"] - first["sent"]) / over,
                max(0, last["received"] - first["received"]) / over,
                last["at"],
            )
        )
        # The host's counters are the same for every worker: read them from the latest.
        readable = first["total"] is not None and last["total"] is not None
        if readable and (host is None or last["at"] > host[2]["at"]):
            host = (took, first, last)
    if not workers:
        return None
    share = nginx = cores = None
    if host is not None:
        took, first, last = host
        total = last["total"] - first["total"]
        if total > 0:
            share = min(1.0, max(0.0, (last["busy"] - first["busy"]) / total))
        if first["nginx"] is not None and last["nginx"] is not None:
            nginx = max(0.0, (last["nginx"] - first["nginx"]) / (length or took))
        cores = last["cores"]
    return Span(length or longest, workers, nginx, share, cores)


@dataclass(frozen=True)
class Busy:
    """Other work is keeping tests out."""

    since: float
    other: float  # its share of the host's cores when this was decided


class CpuFile:
    """The file the workers write and everything else reads. It is opened at first use,
    so a server whose file can't be used still serves, without the figures; each use
    raises OSError or sqlite3.Error until it can be."""

    def __init__(self, path: str | Path, zone: str = "UTC") -> None:
        self.path = str(path)
        self.zone = ZoneInfo(zone)
        self._lock = threading.Lock()  # the connection, and the tests let in
        self._conn: sqlite3.Connection | None = None

    @property
    def _db(self) -> sqlite3.Connection:
        """The connection, opened if it isn't yet. Use it holding the lock."""
        if self._conn is None:
            if self.path != ":memory:":
                create_owned(Path(self.path))
            db = sqlite3.connect(
                self.path, check_same_thread=False, isolation_level=None, timeout=10
            )
            try:
                db.row_factory = sqlite3.Row
                prepare(db, self.path, SCHEMA)
                db.execute("PRAGMA synchronous=NORMAL")
            except sqlite3.Error:
                db.close()
                raise
            self._conn = db
        return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def _rows(self, since: float, until: float) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(
                f"SELECT {COLUMNS} FROM samples WHERE at > ? AND at <= ? ORDER BY worker, at",
                (since, until),
            ).fetchall()

    def now(self, seconds: float = NOW, now: float | None = None) -> Span | None:
        """The last ``seconds``, from the workers still writing."""
        now = time.time() if now is None else now
        return span(self._rows(now - seconds - SAMPLE / 2, now), alive=now - 3 * SAMPLE)

    def minutes(self, since: float) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(
                f"SELECT {MINUTE_COLUMNS} FROM minutes WHERE minute >= ? ORDER BY minute",
                (since,),
            ).fetchall()

    def latest(self, now: float | None = None) -> list[sqlite3.Row]:
        """Each live worker's latest row: its counters, for /metrics."""
        now = time.time() if now is None else now
        with self._lock:
            return self._db.execute(
                f"SELECT {COLUMNS} FROM samples WHERE at > ? AND (worker, at) IN "
                "(SELECT worker, max(at) FROM samples GROUP BY worker) ORDER BY worker",
                (now - 3 * SAMPLE,),
            ).fetchall()

    def test(self, test: str, bound_percent: float = 0.0) -> dict[str, dict[str, Any]]:
        """Each transfer of a test that a worker served for a second or more: the busiest
        such worker's share of a core while it did, and whether that held it back. For a
        download, also how long the client held the server back, and the kernel's RTT
        while it did (``window``): the device, not the network, set its rate when that was
        ``bound_percent`` of the time or more, unless the server's CPU had (0: never)."""
        with self._lock:
            rows = self._db.execute(
                "SELECT part, max(cpu / seconds) FROM tests WHERE id = ? AND seconds >= 1 "
                "GROUP BY part",
                (test,),
            ).fetchall()
            windows = self._db.execute(
                "SELECT part, sum(seconds), sum(idle), sum(rwnd), sum(rtt_sum), sum(rtt_n), "
                "min(min_rtt), sum(ready) FROM windows WHERE id = ? GROUP BY part "
                "HAVING sum(seconds) >= 1",
                (test,),
            ).fetchall()
        out: dict[str, dict[str, Any]] = {
            part: {"cpu": round(cpu, 3), "bound": cpu >= BOUND}
            for part, cpu in rows
            if part in PARTS
        }
        for part, seconds, idle, rwnd, rtt_sum, rtt_n, min_rtt, ready in windows:
            if part not in PARTS:
                continue
            held = min(1.0, (idle + rwnd) / seconds)
            # Held back by the client, not by a server that couldn't keep up: its CPU
            # wasn't full, and it had the next bytes ready most of the time.
            ours = out.get(part, {}).get("bound", False) or ready < READY * seconds
            out.setdefault(part, {})["window"] = {
                "held": round(held, 3),
                "idle": round(idle / seconds, 3),
                "rwnd": round(rwnd / seconds, 3),
                "seconds": round(seconds, 1),
                "rtt_ms": round(rtt_sum / rtt_n, 3) if rtt_n else None,
                "min_rtt_ms": round(min_rtt, 3) if min_rtt is not None else None,
                "ready": round(ready / seconds, 3),
                "bound": bool(bound_percent) and held * 100 >= bound_percent and not ours,
            }
        return out

    def noted_busy(self, percent: float, seconds: float) -> dict[str, Any] | None:
        """Whether other work keeps tests out, as the workers last told the journal: for
        the command line, which can't ask them. None without cpu_busy_percent."""
        if not percent:
            return None
        with self._lock:
            row = self._db.execute("SELECT at, state FROM notices WHERE name = 'busy'").fetchone()
        refusing = row is not None and row["state"] == "busy"
        return {
            "percent": percent,
            "seconds": seconds,
            "refusing": refusing,
            "since": datetime.fromtimestamp(row["at"], self.zone).isoformat() if refusing else None,
        }

    def day(self, at: float) -> date:
        return datetime.fromtimestamp(at, self.zone).date()

    def days(self, now: float | None = None, count: int = KEEP_DAYS) -> list[dict[str, Any]]:
        """Each day's figures, the latest first: the workers' CPU with nothing to serve
        (its median, from at least ``BASELINE_MINUTES`` quiet minutes), on average and at
        the busiest minute, other work's share, and what the tests moved."""
        now = time.time() if now is None else now
        rows = self.minutes(now - count * 86400)
        by_day: dict[date, list[sqlite3.Row]] = {}
        for row in rows:
            by_day.setdefault(self.day(row["minute"]), []).append(row)
        out = []
        for day, mine in sorted(by_day.items(), reverse=True)[:count]:
            quiet = [r["texas2step"] for r in mine if is_quiet(r)]
            others = [r["other"] for r in mine if r["other"] is not None]
            out.append(
                {
                    "day": day.isoformat(),
                    "minutes": len(mine),
                    "quiet_minutes": len(quiet),
                    "idle": (
                        round(statistics.median(quiet), 4)
                        if len(quiet) >= BASELINE_MINUTES
                        else None
                    ),
                    "average": round(statistics.fmean(r["texas2step"] for r in mine), 4),
                    "peak": round(max(r["texas2step"] for r in mine), 4),
                    "other": round(statistics.fmean(others), 4) if others else None,
                    "bytes": round(sum((r["sent"] + r["received"]) * 60 for r in mine)),
                    "probes": round(sum(r["probes"] * 60 for r in mine)),
                    "memory": max(r["memory"] for r in mine),
                }
            )
        return out

    def report(
        self,
        now: float | None = None,
        busy: dict[str, Any] | None = None,
        days: int = KEEP_DAYS,
    ) -> dict[str, Any]:
        """What texas2step cpu and GET /api/v1/cpu show: now, the last hour by the minute,
        and ``days`` days."""
        now = time.time() if now is None else now
        current = self.now(NOW, now)
        hour = [
            {
                "minute": datetime.fromtimestamp(r["minute"], self.zone).isoformat(),
                "texas2step": round(r["texas2step"], 4),
                "busiest": round(r["busiest"], 4),
                "nginx": _rounded(r["nginx"]),
                "host": _rounded(r["host"]),
                "other": _rounded(r["other"]),
                "sent_mbps": round(r["sent"] * 8 / 1e6, 3),
                "received_mbps": round(r["received"] * 8 / 1e6, 3),
                "probes_per_s": round(r["probes"], 1),
                "sessions": r["sessions"],
                "workers": r["workers"],
            }
            for r in self.minutes(now - 3600)
        ]
        return {
            "now": current.public() if current is not None else None,
            "busy": busy,
            "hour": hour,
            "days": self.days(now, days) if days else [],
        }

    def metrics(self, now: float | None = None, busy: dict[str, Any] | None = None) -> str:
        """The figures in Prometheus's text format, for GET /api/v1/metrics."""
        now = time.time() if now is None else now
        lines: list[str] = []

        def metric(name: str, kind: str, about: str, values: list[tuple[str, float]]) -> None:
            if not values:
                return
            lines.extend((f"# HELP texas2step_{name} {about}", f"# TYPE texas2step_{name} {kind}"))
            lines.extend(f"texas2step_{name}{labels} {_number(value)}" for labels, value in values)

        latest = self.latest(now)
        workers = [(f'{{worker="{r["worker"]}"}}', r) for r in latest]
        metric(
            "worker_cpu_seconds_total",
            "counter",
            "CPU time each worker process has used.",
            [(w, r["cpu"]) for w, r in workers],
        )
        metric(
            "worker_memory_bytes",
            "gauge",
            "Each worker's resident memory.",
            [(w, r["memory"]) for w, r in workers],
        )
        metric(
            "test_bytes_total",
            "counter",
            "Bytes the tests moved over TCP: downloads, uploads and WebSocket echoes.",
            [
                (f'{{worker="{r["worker"]}",direction="{d}"}}', r[d])
                for r in latest
                for d in ("sent", "received")
            ],
        )
        metric(
            "udp_probes_total",
            "counter",
            "UDP probes taken in: Monitor, Diag and the packet-loss test.",
            [(w, r["probes"]) for w, r in workers],
        )
        metric(
            "udp_sessions",
            "gauge",
            "UDP probe sessions open.",
            [(w, r["sessions"]) for w, r in workers],
        )
        current = self.now(NOW, now)
        if current is not None:
            about = f"over the last {NOW:g}s"
            metric("cpu_cores", "gauge", "The host's cores.", [("", current.cores or 0)])
            metric(
                "nginx_cpu_cores",
                "gauge",
                f"Cores nginx used, {about}.",
                [("", current.nginx)] if current.nginx is not None else [],
            )
            metric(
                "host_cpu_ratio",
                "gauge",
                f"The host's busy share of all its cores, {about}.",
                [("", current.host)] if current.host is not None else [],
            )
            metric(
                "other_cpu_ratio",
                "gauge",
                f"Other work's share of all the host's cores, {about}: not texas2step or nginx.",
                [("", current.other)] if current.other is not None else [],
            )
        if busy is not None:
            metric(
                "busy",
                "gauge",
                "1 while other work keeps new tests out (cpu_busy_percent).",
                [("", 1 if busy["refusing"] else 0)],
            )
        return "\n".join(lines) + "\n"


def _number(value: float) -> str:
    """A sample's value as Prometheus reads it: counters keep every digit."""
    return str(value) if isinstance(value, int) else repr(round(value, 6))


def is_quiet(row: sqlite3.Row) -> bool:
    """A minute the workers had nothing to serve: no tests' bytes, no probes, no sessions."""
    return not (row["sent"] or row["received"] or row["probes"] or row["sessions"])


class CpuMonitor(CpuFile):
    """A worker's side: its rows, the minutes, the journal, and keeping tests out while
    other work is busy."""

    def __init__(
        self,
        path: str | Path,
        *,
        zone: str = "UTC",
        busy_percent: float = 0.0,
        busy_seconds: float = NOW,
        rtc: Any = None,
    ) -> None:
        super().__init__(path, zone)
        self.busy_share = busy_percent / 100  # 0: never keep tests out
        self.busy_seconds = busy_seconds
        self.rtc = rtc  # rtc.RtcEcho: its probes and sessions
        self.work = Work()
        self.worker = os.getpid()
        self.busy: Busy | None = None
        self._nginx = Nginx()
        self._admitted: dict[str, float] = {}
        self._rolled = 0
        self._pruned = 0.0

    @classmethod
    def of(cls, settings: Any, rtc: Any = None) -> CpuMonitor:
        return cls(
            settings.cpu_file,
            zone=settings.access_timezone,
            busy_percent=settings.cpu_busy_percent,
            busy_seconds=settings.cpu_busy_seconds,
            rtc=rtc,
        )

    # --- Each worker's rows --------------------------------------------------------

    def sample(self, now: float | None = None) -> None:
        """Write this worker's row, sum the minutes that have ended, and look at what the
        journal should hear. Call it off the event loop."""
        now = time.time() if now is None else now
        host = host_times()
        rtc = self.rtc
        row = (
            self.worker,
            now,
            time.process_time(),
            own_memory(),
            self.work.sent,
            self.work.received,
            rtc.probes if rtc is not None else 0,
            len(rtc.sessions) if rtc is not None else 0,
            host[0] if host else None,
            host[1] if host else None,
            self._nginx.read(now) if host else None,
            host[2] if host else None,
        )
        with self._lock:
            self._db.execute(
                f"INSERT OR REPLACE INTO samples ({COLUMNS}) VALUES ({', '.join('?' * 12)})", row
            )
        if self.busy_share:
            self._judge(now)
        if self._roll(now):
            self._watch(now)
        if now - self._pruned >= 600:
            self._prune(now)

    def _roll(self, now: float) -> bool:
        """Sum each minute that has ended into its row, once whichever worker comes first;
        whether there were any."""
        if not self._rolled:
            with self._lock:
                (latest,) = self._db.execute("SELECT max(minute) FROM minutes").fetchone()
            self._rolled = latest or 0
        # A minute has ended once each worker has written its row after it.
        last = int((now - 2 * SAMPLE) // 60 * 60) - 60
        first = max(self._rolled + 60, int((now - RAW_KEEP) // 60 * 60))
        rolled = False
        for minute in range(first, last + 1, 60):
            got = span(self._rows(minute - 3 * SAMPLE, minute + 60), start=minute, length=60)
            if got is None:
                continue
            # Memory is the workers' still running at its end: in a restart's minute, the
            # old ones and their replacements would count twice.
            ending = [w for w in got.workers if w.at >= minute + 60 - 2 * SAMPLE] or got.workers
            with self._lock:
                self._db.execute(
                    f"INSERT OR IGNORE INTO minutes ({MINUTE_COLUMNS}) "
                    f"VALUES ({', '.join('?' * 13)})",
                    (
                        minute,
                        got.texas2step,
                        max(w.cpu for w in got.workers),
                        got.nginx,
                        got.host,
                        got.other,
                        got.cores,
                        sum(w.sent for w in got.workers),
                        sum(w.received for w in got.workers),
                        sum(w.probes for w in got.workers),
                        sum(w.sessions for w in got.workers),
                        sum(w.memory for w in ending),
                        len(ending),
                    ),
                )
            rolled = True
        self._rolled = max(self._rolled, last)
        return rolled

    def _prune(self, now: float) -> None:
        with self._lock:
            self._db.execute("DELETE FROM samples WHERE at < ?", (now - RAW_KEEP,))
            self._db.execute("DELETE FROM minutes WHERE minute < ?", (now - KEEP_DAYS * 86400,))
            self._db.execute("DELETE FROM tests WHERE at < ?", (now - TESTS_KEEP,))
            self._db.execute("DELETE FROM windows WHERE at < ?", (now - TESTS_KEEP,))
            self._db.execute("DELETE FROM admitted WHERE at < ?", (now - TEST_SECONDS,))
            self._admitted = {t: at for t, at in self._admitted.items() if now - at < TEST_SECONDS}
        self._pruned = now

    # --- Keeping tests out ---------------------------------------------------------

    def _judge(self, now: float) -> None:
        got = self.now(self.busy_seconds, now)
        other = got.other if got is not None else None
        # Once busy, until other work is back under nine tenths of the setting: a load that
        # hovers at it would otherwise refuse and let in by turns.
        bar = self.busy_share * (CLEAR if self.busy is not None else 1.0)
        if other is not None and other >= bar:
            if self.busy is None:
                self.busy = Busy(now, other)
            if self._claim("busy", "busy", now):
                log.warning(
                    "cpu: other work is using %.0f%% of the host's CPU (cpu_busy_percent = "
                    "%g): new download and upload tests and Video calls are refused",
                    other * 100,
                    self.busy_share * 100,
                )
        elif other is not None or got is None:
            # Without the host's figures, or with no worker writing, nothing is kept out.
            self.busy = None
            with self._lock:
                row = self._db.execute("SELECT at FROM notices WHERE name = 'busy'").fetchone()
            if self._claim("busy", "clear", now):
                log.warning(
                    "cpu: other work is down to %.0f%% of the host's CPU: tests are let in again%s",
                    (other or 0) * 100,
                    f", after {_took(now - row[0])}" if row else "",
                )

    def _claim(self, name: str, state: str, now: float, unless_since: float | None = None) -> bool:
        """Whether this worker tells the journal of ``state``: the first to find it
        changed, or, with ``unless_since``, not told since then."""
        with self._lock:
            self._db.execute(
                "INSERT OR IGNORE INTO notices VALUES (?, 0, ?)",
                (name, "clear" if name == "busy" else ""),
            )
            if unless_since is None:
                changed = self._db.execute(
                    "UPDATE notices SET state = ?, at = ? WHERE name = ? AND state != ?",
                    (state, now, name, state),
                )
            else:
                changed = self._db.execute(
                    "UPDATE notices SET state = ?, at = ? WHERE name = ? AND at < ?",
                    (state, now, name, unless_since),
                )
            return changed.rowcount == 1

    def refusing(self) -> bool:
        return self.busy is not None

    def busy_state(self) -> dict[str, Any] | None:
        """For the figures: the setting, and whether tests are kept out now."""
        if not self.busy_share:
            return None
        return {
            "percent": round(self.busy_share * 100, 1),
            "seconds": self.busy_seconds,
            "refusing": self.busy is not None,
            "since": (
                datetime.fromtimestamp(self.busy.since, self.zone).isoformat()
                if self.busy
                else None
            ),
        }

    def admit(self, test: str | None, now: float | None = None) -> bool:
        """Whether a download, an upload or a test toward another server may start: always,
        unless other work keeps tests out and the request isn't from a test let in
        before. Reads and writes the file: call it off the event loop."""
        if not self.busy_share:
            return True
        now = time.time() if now is None else now
        if self.busy is None:
            if test and test not in self._admitted:
                with self._lock:
                    self._admitted[test] = now
                    with contextlib.suppress(OSError, sqlite3.Error):  # this worker knows
                        self._db.execute(
                            "INSERT OR IGNORE INTO admitted VALUES (?, ?)", (test, now)
                        )
            return True
        if not test:
            return False
        with self._lock:
            started = self._admitted.get(test)
            if started is None:
                with contextlib.suppress(OSError, sqlite3.Error):
                    query = "SELECT at FROM admitted WHERE id = ?"
                    if (row := self._db.execute(query, (test,)).fetchone()) is not None:
                        started = self._admitted[test] = row[0]
        return started is not None and now - started < TEST_SECONDS

    def notice(self) -> dict[str, Any]:
        """What a refused visitor is told, as for a traffic limit (traffic.Closed)."""
        return {"detail": MESSAGE, "message": MESSAGE, "url": None}

    # --- A test's transfers --------------------------------------------------------

    def record(self, test: str, part: str, cpu: float, seconds: float) -> None:
        """The CPU this worker used while it served one request of a test's transfer, and
        how long that took, added to the transfer's other requests here. Requests at once
        each see the whole worker, so the sums give its share of a core all the same."""
        with self._lock:
            self._db.execute(
                "INSERT INTO tests VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (id, part, worker) "
                "DO UPDATE SET cpu = cpu + excluded.cpu, seconds = seconds + excluded.seconds, "
                "at = excluded.at",
                (test, part, self.worker, cpu, seconds, time.time()),
            )

    def record_window(self, test: str, part: str, held: Held) -> None:
        """How long the client held one request of a download back, added to the
        transfer's other requests here (tcpinfo.Window)."""
        with self._lock:
            self._db.execute(
                "INSERT INTO windows VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (id, part, worker) DO UPDATE SET "
                "seconds = seconds + excluded.seconds, idle = idle + excluded.idle, "
                "rwnd = rwnd + excluded.rwnd, rtt_sum = rtt_sum + excluded.rtt_sum, "
                "rtt_n = rtt_n + excluded.rtt_n, "
                "min_rtt = min(coalesce(min_rtt, excluded.min_rtt), "
                "coalesce(excluded.min_rtt, min_rtt)), ready = ready + excluded.ready, "
                "at = excluded.at",
                (
                    test,
                    part,
                    self.worker,
                    held.seconds,
                    held.idle,
                    held.rwnd,
                    held.rtt_sum_ms,
                    held.rtt_n,
                    held.min_rtt_ms,
                    held.ready,
                    time.time(),
                ),
            )

    # --- The journal ---------------------------------------------------------------

    def _watch(self, now: float) -> None:
        """Warn of CPU with nothing to serve, and of idle CPU climbing over the days."""
        recent = self.minutes(now - (QUIET_MINUTES + 1) * 60 - 2 * SAMPLE)[-QUIET_MINUTES:]
        if (
            len(recent) == QUIET_MINUTES
            and recent[-1]["minute"] - recent[0]["minute"] == (QUIET_MINUTES - 1) * 60
            and all(is_quiet(r) for r in recent)
            and (used := statistics.fmean(r["texas2step"] for r in recent)) >= QUIET_CPU
            and self._claim("quiet", "told", now, unless_since=now - WARN_EVERY)
        ):
            got = self.now(NOW, now)
            each = ", ".join(f"{w.pid} {w.cpu:.0%}" for w in got.workers) if got else ""
            log.warning(
                "cpu: the workers used %.0f%% of a core for the last %d min with no tests, "
                "probes or UDP sessions to serve (now by worker: %s): not their traffic. "
                "py-spy top --pid <worker> shows where it goes (docs/cpu.md)",
                used * 100,
                QUIET_MINUTES,
                each or "none writing",
            )
        days = self.days(now, BASELINE_DAYS + 1)
        if not days or days[0]["day"] != self.day(now).isoformat() or days[0]["idle"] is None:
            return
        before = [d["idle"] for d in days[1:] if d["idle"] is not None]
        if len(before) < 3:
            return
        today, usual = days[0]["idle"], statistics.median(before)
        if (
            today >= usual * BASELINE_FACTOR
            and today - usual >= BASELINE_RISE
            and self._claim("baseline", days[0]["day"], now)
        ):
            log.warning(
                "cpu: with nothing to serve, the workers use %.1f%% of a core today, against "
                "%.1f%% on the %d days before: something in them uses more as they run. "
                "texas2step cpu has the figures (docs/cpu.md)",
                today * 100,
                usual * 100,
                len(before),
            )


def _took(seconds: float) -> str:
    return f"{seconds:.0f}s" if seconds < 120 else f"{seconds / 60:.0f} min"


async def keep(monitor: CpuMonitor) -> None:
    """Sample every SAMPLE seconds, for as long as the server runs."""
    failed = False
    while True:
        await asyncio.sleep(SAMPLE)
        try:
            await asyncio.to_thread(monitor.sample)
        except (OSError, sqlite3.Error) as exc:
            if not failed:
                log.warning("cpu: can't write %s: %s", monitor.path, exc)
            failed = True
            monitor.busy = None  # without the figures, nothing is kept out
        else:
            failed = False


class Watched:
    """Times one request of a test's transfer: this worker's CPU from its first byte to its
    last, recorded when it ends, so the client can ask whether the server held it back."""

    def __init__(
        self, monitor: CpuMonitor | None, test: str | None, part: str, window: Window | None = None
    ) -> None:
        self.monitor = monitor if test else None
        self.test = test
        self.part = part
        self.first: tuple[float, float] | None = None
        self.last: tuple[float, float] | None = None
        # A download's connection, timed for how long the client held it back.
        self.window = window if self.monitor is not None else None

    def moved(self) -> None:
        if self.monitor is None:
            return
        mark = (time.monotonic(), time.process_time())
        if self.first is None:
            self.first = mark
        self.last = mark
        if self.window is not None:
            self.window.moved()

    def _figures(self) -> tuple[str, str, float, float] | None:
        """What to record: the CPU and the seconds from its first byte to its last."""
        if self.monitor is None or self.first is None or self.last is None:
            return None
        took = self.last[0] - self.first[0]
        if took <= 0:
            return None
        return self.test or "", self.part, self.last[1] - self.first[1], took

    def done(self, tasks: set[asyncio.Task[Any]]) -> None:
        """Record it in the background, as a download ends with its client gone."""
        if (figures := self._figures()) is None or self.monitor is None:
            return
        held = self.window.held() if self.window is not None else None
        task = asyncio.get_running_loop().create_task(
            asyncio.to_thread(self._record, figures, held)
        )
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        task.add_done_callback(_quiet)

    def _record(self, figures: tuple[str, str, float, float], held: Held | None) -> None:
        assert self.monitor is not None
        self.monitor.record(*figures)
        if held is not None:
            self.monitor.record_window(figures[0], figures[1], held)

    async def save(self) -> None:
        """Record it before the answer goes, so the client finds it when it asks."""
        if (figures := self._figures()) is None or self.monitor is None:
            return
        try:
            await asyncio.to_thread(self.monitor.record, *figures)
        except (OSError, sqlite3.Error) as exc:
            log.debug("cpu: can't record a test's CPU: %s", exc)


def _quiet(task: asyncio.Task[Any]) -> None:
    # keep() warns once of a file that can't be written.
    with contextlib.suppress(asyncio.CancelledError):
        if (exc := task.exception()) is not None:
            log.debug("cpu: can't record a test's CPU: %s", exc)
