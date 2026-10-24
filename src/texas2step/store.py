"""Result history in SQLite (stdlib only; calls run in a worker thread)."""

from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

# Result IDs are read out over the phone: lowercase, without 0/o, 1/l/i or u/v.
ID_LETTERS = "23456789abcdefghjkmnpqrstwxyz"


def new_id() -> str:
    """Such as k7q2-m9xd. IDs from before 1.8.0 (token_urlsafe) still work."""
    chars = "".join(secrets.choice(ID_LETTERS) for _ in range(8))
    return f"{chars[:4]}-{chars[4:]}"


SCHEMA = """
CREATE TABLE IF NOT EXISTS results (
    id            TEXT PRIMARY KEY,
    created_at    REAL NOT NULL,
    client_ip     TEXT,
    server_id     TEXT,
    source        TEXT,
    download_mbps REAL,
    upload_mbps   REAL,
    latency_ms    REAL,
    jitter_ms     REAL,
    loss_pct      REAL,
    payload       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS results_created ON results (created_at DESC);
"""

SUMMARY_COLUMNS = (
    "id",
    "created_at",
    "client_ip",
    "server_id",
    "source",
    "download_mbps",
    "upload_mbps",
    "latency_ms",
    "jitter_ms",
    "loss_pct",
)

# Summary fields read from the saved result itself, so databases from before them need no
# new column: this computer's own address and a MAC, where the client could tell them.
PAYLOAD_FIELDS = {
    "client_local_ip": ("client", "local_ip"),
    "client_mac": ("client", "mac"),
    "client_mac_source": ("client", "mac_source"),
}
PAYLOAD_SQL = ", ".join(
    f"CASE WHEN json_type(payload, '$.{a}.{b}') = 'text' "
    f"THEN json_extract(payload, '$.{a}.{b}') END AS {name}"
    for name, (a, b) in PAYLOAD_FIELDS.items()
)
# And the bytes the test moved, its downloads plus its upload, as the client counted them.
TRANSFERS = ("download", "one_stream", "upload")
_NUMBER = "json_type(payload, '$.{}.bytes') IN ('integer', 'real')"
TRAFFIC_SQL = (
    f"CASE WHEN {' OR '.join(_NUMBER.format(d) for d in TRANSFERS)} THEN "
    + " + ".join(
        f"(CASE WHEN {_NUMBER.format(d)} THEN json_extract(payload, '$.{d}.bytes') ELSE 0 END)"
        for d in TRANSFERS
    )
    + " END AS traffic_bytes"
)


def prepare(db: sqlite3.Connection, path: str, schema: str) -> None:
    """WAL mode, so several workers may write, and the schema.

    Several uvicorn workers open the file at once. On a new file, SQLite reports "database
    is locked" immediately, without waiting, to every worker but the one switching it to
    WAL, so retry briefly.
    """
    for attempt in range(100):
        try:
            if path != ":memory:":
                db.execute("PRAGMA journal_mode=WAL")
            db.executescript(schema)
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) or attempt == 99:
                raise
            time.sleep(0.05)


def create_owned(path: Path) -> None:
    """A new file belongs to the service, as its folder does, even when root makes it."""
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
    try:
        if os.geteuid() == 0:
            folder = path.parent.stat()
            os.fchown(fd, folder.st_uid, folder.st_gid)
    finally:
        os.close(fd)


def _dig(data: Any, *path: str) -> float | None:
    for key in path:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return float(data) if isinstance(data, int | float) and not isinstance(data, bool) else None


def _traffic(result: dict[str, Any]) -> int | None:
    found = [n for d in TRANSFERS if (n := _dig(result, d, "bytes")) is not None]
    return round(sum(found)) if found else None


def _text(data: Any, *path: str) -> str | None:
    for key in path:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data if isinstance(data, str) else None


class ResultStore:
    def __init__(self, path: str | Path, max_rows: int = 10_000) -> None:
        self.path = str(path)
        self.max_rows = max_rows
        self._lock = threading.Lock()
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._setup()

    def _setup(self) -> None:
        prepare(self._db, self.path, SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def add(self, result: dict[str, Any], client_ip: str | None) -> dict[str, Any]:
        rid = new_id()
        created = time.time()
        result = {**result, "id": rid, "saved_at": created}
        row = (
            rid,
            created,
            client_ip,
            (result.get("server") or {}).get("id"),
            result.get("tool"),
            _dig(result, "download", "mbps"),
            _dig(result, "upload", "mbps"),
            _dig(result, "latency", "median"),
            _dig(result, "latency", "jitter"),
            _dig(result, "loss", "loss_pct"),
            json.dumps(result, separators=(",", ":")),
        )
        with self._lock, self._db:
            self._db.execute(f"INSERT INTO results VALUES ({','.join('?' * len(row))})", row)
            self._db.execute(
                "DELETE FROM results WHERE id NOT IN "
                "(SELECT id FROM results ORDER BY created_at DESC LIMIT ?)",
                (self.max_rows,),
            )
        summary = dict(zip(SUMMARY_COLUMNS, row[:-1], strict=True))
        summary |= {name: _text(result, *path) for name, path in PAYLOAD_FIELDS.items()}
        return summary | {"traffic_bytes": _traffic(result)}

    def recent(self, limit: int = 50, before: float | None = None) -> list[dict[str, Any]]:
        sql = f"SELECT {', '.join(SUMMARY_COLUMNS)}, {PAYLOAD_SQL}, {TRAFFIC_SQL} FROM results"
        args: list[Any] = []
        if before is not None:
            sql += " WHERE created_at < ?"
            args.append(before)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args)]

    def tally(self) -> tuple[int, float, float, dict[str | None, int]]:
        """The number of results, the first and last one's time (0 without), and per server."""
        with self._lock:
            count, first, last = self._db.execute(
                "SELECT count(*), coalesce(min(created_at), 0), coalesce(max(created_at), 0) "
                "FROM results"
            ).fetchone()
            servers = self._db.execute(
                "SELECT server_id, count(*) AS n FROM results "
                "GROUP BY server_id ORDER BY n DESC, server_id"
            ).fetchall()
        return count, first, last, {row[0]: row[1] for row in servers}

    def clear(self) -> int:
        """Delete every result, and leave none of them in the file or its WAL.

        secure_delete overwrites what the rows held, VACUUM shrinks the file, and the
        checkpoint empties the WAL. Other connections, the service's, carry on.
        """
        with self._lock:
            self._db.execute("PRAGMA secure_delete = ON")
            with self._db:
                deleted = self._db.execute("DELETE FROM results").rowcount
            self._db.execute("VACUUM")
            self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return deleted

    def get(self, rid: str) -> dict[str, Any] | None:
        """A result by its ID, as given, or as read out over the phone (see new_id)."""
        ids = [rid]
        spoken = "".join(re.findall(r"[a-z0-9]", rid.lower()))
        if len(spoken) == 8:
            ids.append(f"{spoken[:4]}-{spoken[4:]}")
        with self._lock:
            for one in ids:
                row = self._db.execute("SELECT payload FROM results WHERE id = ?", (one,))
                if found := row.fetchone():
                    return json.loads(found["payload"])
        return None
