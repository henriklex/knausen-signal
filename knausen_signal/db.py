"""SQLite buffer for modem and probe samples.

Each sample carries a `pushed_at` column that the push worker sets once the
row has been accepted by Grafana Cloud. Until then it sits here, so a WAN
outage during sample-time does not lose the data covering the outage.

The DB is the long-term archive too — rows are never auto-pruned, so the
local history exceeds the cloud's 14-day retention.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS modem_sample (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL    NOT NULL,           -- unix seconds, float
    payload      TEXT    NOT NULL,           -- JSON-serialized ModemSample
    pushed_at    REAL                        -- NULL until successfully pushed
);

CREATE INDEX IF NOT EXISTS idx_modem_unpushed
    ON modem_sample (ts) WHERE pushed_at IS NULL;

CREATE TABLE IF NOT EXISTS probe_sample (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL    NOT NULL,
    payload      TEXT    NOT NULL,
    pushed_at    REAL
);

CREATE INDEX IF NOT EXISTS idx_probe_unpushed
    ON probe_sample (ts) WHERE pushed_at IS NULL;
"""


@dataclass(frozen=True)
class StoredSample:
    id: int
    ts: float
    payload: dict


def open_db(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False because the asyncio supervisor calls insert/select
    # from the default thread pool. SQLite is internally thread-safe; our access
    # pattern is at most a few writes per minute so contention is non-existent.
    conn = sqlite3.connect(
        str(path), isolation_level=None, timeout=30, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("BEGIN")
    try:
        yield
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def insert_modem_sample(conn: sqlite3.Connection, ts: float, payload: dict) -> int:
    return _insert(conn, "modem_sample", ts, payload)


def insert_probe_sample(conn: sqlite3.Connection, ts: float, payload: dict) -> int:
    return _insert(conn, "probe_sample", ts, payload)


def _insert(conn: sqlite3.Connection, table: str, ts: float, payload: dict) -> int:
    cur = conn.execute(
        f"INSERT INTO {table} (ts, payload) VALUES (?, ?)",
        (ts, json.dumps(payload, default=str)),
    )
    return cur.lastrowid


def select_unpushed(
    conn: sqlite3.Connection, table: str, limit: int = 500
) -> list[StoredSample]:
    rows = conn.execute(
        f"SELECT id, ts, payload FROM {table} WHERE pushed_at IS NULL ORDER BY ts LIMIT ?",
        (limit,),
    ).fetchall()
    return [StoredSample(id=r["id"], ts=r["ts"], payload=json.loads(r["payload"])) for r in rows]


def mark_pushed(
    conn: sqlite3.Connection, table: str, ids: Iterable[int], pushed_at: float
) -> None:
    ids = list(ids)
    if not ids:
        return
    placeholders = ",".join("?" * len(ids))
    conn.execute(
        f"UPDATE {table} SET pushed_at = ? WHERE id IN ({placeholders})",
        (pushed_at, *ids),
    )
