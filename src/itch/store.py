"""SQLite landing zone for derived per-symbol state.

Design notes that matter more than the SQL:

* **Prices are stored as INTEGERs**, in the same fixed-point units as the wire format.
  SQLite's ``REAL`` is a C double and would reintroduce exactly the representation
  problem the decoder avoids. Conversion to dollars happens in the presentation layer
  or in a view, never in storage.

* **Timestamps are stored as INTEGER nanoseconds since midnight**, not as text. Text
  timestamps cost ~25 bytes each, sort lexicographically (which happens to work here
  but is fragile), and cannot be arithmetically differenced without a parse.

* **Writes go through one transaction per batch with ``executemany``.** SQLite's default
  is an implicit transaction per statement, each with an fsync; inserting 100k snapshot
  rows that way takes minutes. Batching them into one transaction takes well under a
  second. This is the single biggest performance factor in the load step and the reason
  the loader looks the way it does.

* **The schema is idempotent** (``CREATE TABLE IF NOT EXISTS``) and each run is tagged
  with a ``run_id``, so re-running the pipeline appends a new run rather than corrupting
  the previous one -- which is what makes the whole pipeline safely resumable.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from contextlib import closing
from typing import Any

from .replay import Snapshot

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file   TEXT    NOT NULL,
    source_kind   TEXT    NOT NULL,  -- 'real' or 'synthetic'
    bytes_read    INTEGER NOT NULL,
    messages      INTEGER NOT NULL,
    created_utc   TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS symbols (
    run_id        INTEGER NOT NULL,
    stock_locate  INTEGER NOT NULL,
    symbol        TEXT    NOT NULL,
    PRIMARY KEY (run_id, stock_locate)
);

-- Top-of-book and depth sampled on a fixed exchange-time schedule.
-- All prices are fixed-point integers with 4 implied decimals.
CREATE TABLE IF NOT EXISTS book_snapshots (
    run_id        INTEGER NOT NULL,
    stock_locate  INTEGER NOT NULL,
    ts_ns         INTEGER NOT NULL,
    bid           INTEGER,
    ask           INTEGER,
    spread        INTEGER,
    bid_qty       INTEGER NOT NULL,
    ask_qty       INTEGER NOT NULL,
    bid_levels    INTEGER NOT NULL,
    ask_levels    INTEGER NOT NULL,
    depth_bid_qty INTEGER NOT NULL,
    depth_ask_qty INTEGER NOT NULL,
    imbalance     REAL
);

CREATE TABLE IF NOT EXISTS message_counts (
    run_id     INTEGER NOT NULL,
    msg_type   TEXT    NOT NULL,
    n          INTEGER NOT NULL,
    PRIMARY KEY (run_id, msg_type)
);

CREATE TABLE IF NOT EXISTS symbol_activity (
    run_id           INTEGER NOT NULL,
    stock_locate     INTEGER NOT NULL,
    symbol           TEXT,
    messages         INTEGER NOT NULL,
    adds             INTEGER NOT NULL,
    executions       INTEGER NOT NULL,
    executed_volume  INTEGER NOT NULL,
    executed_notional INTEGER NOT NULL,
    PRIMARY KEY (run_id, stock_locate)
);

CREATE TABLE IF NOT EXISTS oracle_report (
    run_id  INTEGER NOT NULL,
    metric  TEXT    NOT NULL,
    value   REAL    NOT NULL,
    PRIMARY KEY (run_id, metric)
);

CREATE INDEX IF NOT EXISTS ix_snap_run_locate_ts
    ON book_snapshots (run_id, stock_locate, ts_ns);
CREATE INDEX IF NOT EXISTS ix_activity_volume
    ON symbol_activity (run_id, messages DESC);
"""


def connect(path: str) -> sqlite3.Connection:
    """Open a connection with the pragmas that matter for a bulk-load workload.

    ``journal_mode=WAL`` and ``synchronous=NORMAL`` trade a small durability window for
    a large write speedup. Appropriate here: this database is a derived artefact that
    can be regenerated from the source file by re-running the pipeline.
    """
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Create tables and indexes if absent. Safe to call on every run."""
    conn.executescript(SCHEMA)
    conn.commit()


def start_run(
    conn: sqlite3.Connection,
    *,
    source_file: str,
    source_kind: str,
    bytes_read: int,
    messages: int,
) -> int:
    """Record a new pipeline run and return its id."""
    with closing(conn.cursor()) as cur:
        cur.execute(
            "INSERT INTO runs (source_file, source_kind, bytes_read, messages) "
            "VALUES (?, ?, ?, ?)",
            (source_file, source_kind, bytes_read, messages),
        )
        run_id = int(cur.lastrowid or 0)
    conn.commit()
    return run_id


def insert_snapshots(
    conn: sqlite3.Connection, run_id: int, snapshots: Iterable[Snapshot]
) -> int:
    """Bulk-insert snapshot rows in a single transaction.

    Uses a generator into ``executemany`` so a multi-million-row snapshot set never has
    to be materialised as a list of tuples alongside the snapshot objects themselves.
    """
    rows = (
        (
            run_id,
            s.stock_locate,
            s.timestamp,
            s.bid,
            s.ask,
            s.spread,
            s.bid_qty,
            s.ask_qty,
            s.bid_levels,
            s.ask_levels,
            s.depth_bid_qty,
            s.depth_ask_qty,
            s.imbalance,
        )
        for s in snapshots
    )
    with conn:  # one transaction, one fsync
        cur = conn.executemany(
            "INSERT INTO book_snapshots (run_id, stock_locate, ts_ns, bid, ask, "
            "spread, bid_qty, ask_qty, bid_levels, ask_levels, depth_bid_qty, "
            "depth_ask_qty, imbalance) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        return cur.rowcount


def insert_message_counts(
    conn: sqlite3.Connection, run_id: int, counts: Mapping[str, int]
) -> None:
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO message_counts (run_id, msg_type, n) VALUES (?,?,?)",
            [(run_id, t, int(n)) for t, n in counts.items()],
        )


def insert_symbols(
    conn: sqlite3.Connection, run_id: int, directory: Mapping[str, int]
) -> None:
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO symbols (run_id, stock_locate, symbol) "
            "VALUES (?,?,?)",
            [(run_id, int(loc), sym) for sym, loc in directory.items()],
        )


def insert_symbol_activity(
    conn: sqlite3.Connection, run_id: int, rows: Iterable[tuple[Any, ...]]
) -> None:
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO symbol_activity (run_id, stock_locate, symbol, "
            "messages, adds, executions, executed_volume, executed_notional) "
            "VALUES (?,?,?,?,?,?,?,?)",
            [(run_id, *row) for row in rows],
        )


def insert_oracle(
    conn: sqlite3.Connection, run_id: int, report: Mapping[str, float | int]
) -> None:
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO oracle_report (run_id, metric, value) VALUES (?,?,?)",
            [(run_id, k, float(v)) for k, v in report.items()],
        )


def latest_run_id(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT MAX(run_id) FROM runs").fetchone()
    return int(row[0]) if row and row[0] is not None else None
