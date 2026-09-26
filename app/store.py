"""Durable memory across restarts.

v1 held everything in RAM, which meant "is 6,946ms normal?" was unanswerable until the
agent had watched long enough to build history - and then lost it the moment you closed
the window. A baseline is only worth anything if it outlives the process that learned it.

SQLite because it is in the standard library and needs no server: this stays a local-first
tool that a user runs by typing one command. One file under ~/.loganalyst/, alongside the
session file v1 already writes.

Deliberately narrow. This is not log storage - lines still live in the user's own file and
are never copied here. What is stored is what cannot be recomputed cheaply: measurements
already reduced to numbers, and the verdicts reached about past runs.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path(os.path.expanduser("~/.loganalyst/history.db"))

# Split from the indexes below: a database created before recorded_at existed has
# the tables already, via CREATE TABLE IF NOT EXISTS, which does not add a missing
# column - _migrate() must run and add it BEFORE any index on that column is
# created, or CREATE INDEX itself fails with "no such column".
TABLES = """
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    started_at  TEXT,
    ended_at    TEXT,
    label       TEXT,
    verdict     TEXT,
    reason      TEXT,
    evidence    TEXT,
    events      INTEGER DEFAULT 0,
    errors      INTEGER DEFAULT 0,
    -- Always this machine's own wall clock, in ISO 8601 (sortable, always has a
    -- date). started_at/ended_at are often the log line's OWN stated time - useful
    -- for display, sometimes a bare "11:31:24" with no date, useless for filtering
    -- "the last 24 hours". Every time-windowed query in this file uses this column.
    recorded_at TEXT
);

CREATE TABLE IF NOT EXISTS durations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    component   TEXT NOT NULL,
    operation   TEXT NOT NULL,
    value_ms    REAL NOT NULL,
    observed_at TEXT,
    -- Same reasoning as runs.recorded_at: observed_at is the log line's own stated
    -- time, not reliably sortable or dateable. Time-windowed baseline queries use
    -- this column instead.
    recorded_at TEXT
);
"""

INDEXES = """
-- Baselines are read per (source, component, operation) on every duration observed,
-- so this index is what keeps observe() cheap as history grows.
CREATE INDEX IF NOT EXISTS durations_key
    ON durations (source, component, operation, id);

CREATE INDEX IF NOT EXISTS runs_source ON runs (source, id);
CREATE INDEX IF NOT EXISTS runs_recorded_at ON runs (source, recorded_at);
CREATE INDEX IF NOT EXISTS durations_recorded_at ON durations (source, recorded_at);
"""

# Enough history to compare against, bounded so a long-running watch does not grow
# without limit and so a baseline tracks current behaviour rather than last month's.
KEEP_PER_OPERATION = 200


def _wall_clock() -> str:
    """This machine's own real time, UTC, ISO 8601 - always sortable, always dated.

    Deliberately not the log line's own timestamp (see recorded_at's comment in
    SCHEMA): a value written here must be safe to filter "the last 24 hours" by,
    which a bare "11:31:24" pulled from someone else's log format is not.
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    """Runs, durations and verdicts on disk.

    Every method is safe to call from the ingest thread and the HTTP threads at once:
    each call opens its own short-lived connection under a shared lock, so no thread
    can be left holding a stale one across a gap - which was the earlier design and
    the actual cause of a "database is locked" error that had nothing to do with
    genuine concurrency (see _connect's comment).
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else DEFAULT_PATH
        # ":memory:" is how tests get isolation without touching the user's real history.
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._memory_conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        conn = self._connect()
        try:
            conn.executescript(TABLES)
            self._migrate(conn)
            conn.executescript(INDEXES)
        finally:
            self._release(conn)
        # One writer thread owns every duration INSERT, so ingestion never
        # waits on the disk or on another connection's lock.
        import queue as _queue
        self._queue: _queue.Queue = _queue.Queue(maxsize=10000)
        self._writer_stop = threading.Event()
        self._writer = threading.Thread(target=self._writer_loop, daemon=True)
        self._writer.start()

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """Add columns a database created before recorded_at existed is missing.

        CREATE TABLE IF NOT EXISTS never adds a column to a table that already
        exists, so a user's existing ~/.loganalyst/history.db needs this to gain
        recorded_at rather than silently having every audit-report query find none.
        """
        for table, column in (("runs", "recorded_at"), ("durations", "recorded_at")):
            existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
        conn.commit()

    def _connect(self) -> sqlite3.Connection:
        # An in-memory database exists only as long as its connection, so it cannot be
        # per-thread; serialise it instead. On-disk opens a fresh connection per call.
        if str(self.path) == ":memory:":
            if self._memory_conn is None:
                self._memory_conn = sqlite3.connect(":memory:", check_same_thread=False)
                self._memory_conn.row_factory = sqlite3.Row
            return self._memory_conn

        # A connection cached for a thread's lifetime can end up pinned to a stale WAL
        # snapshot across a long gap between calls (an in-flight LLM interpretation
        # call left the ingest thread idle for several seconds once, and its next
        # write then collided with the writer thread's connection and hit SQLite's
        # busy timeout even though nothing else was actually still using the file).
        # A short-lived connection per call never lives long enough to go stale.
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        # Readers must not block the ingest thread's writes; a stalled dashboard
        # request froze v1's pipeline once already.
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _release(self, conn: sqlite3.Connection) -> None:
        """Close a connection from _connect() - except the shared in-memory one,
        which must outlive any single call since it IS the whole database."""
        if conn is not self._memory_conn:
            conn.close()

    # -- durations ---------------------------------------------------------------

    def record_duration(
        self, source: str, component: str, operation: str, value_ms: float, when: str = ""
    ) -> None:
        """Queue a measurement. NEVER touches the database on this thread.

        This used to INSERT inline, and it is the bug that made /api/attach
        hang: the ingest path ran one write per backfilled line, each waiting
        on a database another connection held, so a whole attach request
        blocked behind SQLite. History is an enhancement (P8) - it may not sit
        in front of ingestion. A single writer thread drains this queue.
        """
        try:
            self._queue.put_nowait(
                (source, component, operation, float(value_ms), when, _wall_clock()))
        except Exception:
            pass  # a full queue costs history, never ingestion

    def _writer_loop(self) -> None:
        import queue as _queue
        while not self._writer_stop.is_set():
            batch = []
            try:
                batch.append(self._queue.get(timeout=0.5))
            except _queue.Empty:
                continue
            while len(batch) < 200:
                try:
                    batch.append(self._queue.get_nowait())
                except _queue.Empty:
                    break
            try:
                with self._lock:
                    conn = self._connect()
                    try:
                        conn.executemany(
                            "INSERT INTO durations (source, component, operation,"
                            " value_ms, observed_at, recorded_at)"
                            " VALUES (?, ?, ?, ?, ?, ?)", batch)
                        conn.commit()
                    finally:
                        self._release(conn)
            except Exception:
                pass  # the measurements are lost; the product is not

    def flush_durations(self, timeout: float = 2.0) -> None:
        """Wait for queued measurements to land - for readers and tests."""
        import time as _time
        deadline = _time.time() + timeout
        while not self._queue.empty() and _time.time() < deadline:
            _time.sleep(0.02)

    def samples(self, source: str, component: str, operation: str) -> list[float]:
        """The most recent measurements, oldest first.

        Ordered by id rather than timestamp: log timestamps repeat, are sometimes absent,
        and in a replayed file are not monotonic - insertion order is the only reliable
        sequence.
        """
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT value_ms FROM durations"
                    " WHERE source = ? AND component = ? AND operation = ?"
                    " ORDER BY id DESC LIMIT ?",
                    (source, component, operation, KEEP_PER_OPERATION),
                ).fetchall()
            finally:
                self._release(conn)
        return [row["value_ms"] for row in reversed(rows)]

    def known_operations(self, source: str) -> list[tuple[str, str, int]]:
        """Every (component, operation) seen for a source, with how many samples."""
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT component, operation, COUNT(*) AS n FROM durations"
                    " WHERE source = ? GROUP BY component, operation",
                    (source,),
                ).fetchall()
            finally:
                self._release(conn)
        return [(r["component"], r["operation"], r["n"]) for r in rows]

    def prune(self, source: str) -> int:
        """Drop measurements past the keep window.

        Called occasionally rather than on every insert - deleting on each write would
        put a scan in the ingest path for no benefit.
        """
        with self._lock:
            conn = self._connect()
            try:
                cursor = conn.execute(
                    "DELETE FROM durations WHERE id IN ("
                    "  SELECT id FROM ("
                    "    SELECT id, ROW_NUMBER() OVER ("
                    "      PARTITION BY component, operation ORDER BY id DESC"
                    "    ) AS rank FROM durations WHERE source = ?"
                    "  ) WHERE rank > ?"
                    ")",
                    (source, KEEP_PER_OPERATION),
                )
                conn.commit()
                return cursor.rowcount
            finally:
                self._release(conn)

    # -- runs --------------------------------------------------------------------

    def start_run(self, source: str, started_at: str = "", label: str = "") -> int:
        with self._lock:
            conn = self._connect()
            try:
                cursor = conn.execute(
                    "INSERT INTO runs (source, started_at, label, recorded_at)"
                    " VALUES (?, ?, ?, ?)",
                    (source, started_at, label, _wall_clock()),
                )
                conn.commit()
                return int(cursor.lastrowid or 0)
            finally:
                self._release(conn)

    def finish_run(
        self,
        run_id: int,
        *,
        ended_at: str = "",
        verdict: str = "",
        reason: str = "",
        evidence: list[str] | None = None,
        events: int = 0,
        errors: int = 0,
    ) -> None:
        """Close out a run.

        Evidence is stored as JSON because a verdict without the lines that support it is
        exactly the fluent-but-unverifiable claim this project exists to avoid.
        """
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE runs SET ended_at = ?, verdict = ?, reason = ?, evidence = ?,"
                    " events = ?, errors = ?, recorded_at = ? WHERE id = ?",
                    (
                        ended_at,
                        verdict,
                        reason,
                        json.dumps(evidence or []),
                        int(events),
                        int(errors),
                        _wall_clock(),
                        run_id,
                    ),
                )
                conn.commit()
            finally:
                self._release(conn)

    def recent_runs(self, source: str, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT * FROM runs WHERE source = ? ORDER BY id DESC LIMIT ?",
                    (source, limit),
                ).fetchall()
            finally:
                self._release(conn)
        runs = []
        for row in rows:
            run = dict(row)
            try:
                run["evidence"] = json.loads(run.get("evidence") or "[]")
            except (ValueError, TypeError):
                # A malformed row must not take down the dashboard.
                run["evidence"] = []
            runs.append(run)
        return runs

    def verdict_counts(self, source: str) -> dict[str, int]:
        """How past runs turned out - the history a single verdict is read against."""
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT verdict, COUNT(*) AS n FROM runs"
                    " WHERE source = ? AND verdict IS NOT NULL AND verdict != ''"
                    " GROUP BY verdict",
                    (source,),
                ).fetchall()
            finally:
                self._release(conn)
        return {row["verdict"]: row["n"] for row in rows}

    def runs_since(self, source: str, since_iso: str) -> list[dict[str, Any]]:
        """Every run recorded at or after since_iso, oldest first.

        Filters on recorded_at, not started_at - the one column guaranteed to be
        this machine's own real, dated, sortable time regardless of what the
        source's own log format happens to write.
        """
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT * FROM runs WHERE source = ? AND recorded_at >= ?"
                    " ORDER BY id ASC",
                    (source, since_iso),
                ).fetchall()
            finally:
                self._release(conn)
        runs = []
        for row in rows:
            run = dict(row)
            try:
                run["evidence"] = json.loads(run.get("evidence") or "[]")
            except (ValueError, TypeError):
                run["evidence"] = []
            runs.append(run)
        return runs

    def durations_since(self, source: str, since_iso: str) -> list[dict[str, Any]]:
        """Every duration measurement recorded at or after since_iso."""
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT component, operation, value_ms, recorded_at FROM durations"
                    " WHERE source = ? AND recorded_at >= ? ORDER BY id ASC",
                    (source, since_iso),
                ).fetchall()
            finally:
                self._release(conn)
        return [dict(row) for row in rows]

    def close(self) -> None:
        self.flush_durations()
        self._writer_stop.set()
        self._writer.join(timeout=2.0)
        if self._memory_conn is not None:
            self._memory_conn.close()
            self._memory_conn = None
