"""C4 - the telemetry store: enough recent data to detect and explain, no more.

Two parts, matching the doc's tiers:

  HotRing       the last few thousand events, in memory, for detectors
  ProjectStore  derived data on disk - templates and their counts

Per-project isolation is physical, not conventional. Each project gets its own
database FILE under ~/.aegis/projects/<project>/, so one project's templates
and counts cannot leak into another's even through a bug - there is no shared
table for a WHERE clause to get wrong. Aegis stores derived data heavily and
raw data lightly: templates and counts persist; raw lines stay in the
project's own log file, where they already live.
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
from collections import deque
from pathlib import Path
from typing import Any

from aegis.contracts.events import Event

AEGIS_HOME = Path(os.path.expanduser("~/.aegis"))

# Enough history for a p95 to mean something, bounded so a long-running
# watch does not grow without limit.
KEEP_PER_OPERATION = 500

_SCHEMA = """
CREATE TABLE IF NOT EXISTS templates (
    id          TEXT PRIMARY KEY,
    pattern     TEXT NOT NULL,
    service     TEXT DEFAULT '',
    count       INTEGER DEFAULT 0,
    first_seen  TEXT DEFAULT '',
    last_seen   TEXT DEFAULT '',
    example     TEXT DEFAULT ''
);
-- Per-minute counts per template: the raw material of every detector in C8.
CREATE TABLE IF NOT EXISTS template_minutes (
    template_id TEXT NOT NULL,
    minute      TEXT NOT NULL,
    count       INTEGER DEFAULT 0,
    PRIMARY KEY (template_id, minute)
);
-- C15's memory: a resolved incident and what its fix turned out to do.
-- One row per incident (INSERT OR REPLACE), because an incident is archived
-- when it forms and again when it resolves.
-- Measured durations: the raw material of every baseline in C8.
CREATE TABLE IF NOT EXISTS durations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    source     TEXT NOT NULL,
    component  TEXT NOT NULL,
    operation  TEXT NOT NULL,
    value_ms   REAL NOT NULL,
    observed_at TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS durations_key
    ON durations (source, component, operation, id);
-- Templates a human muted: noise they never want to see again.
CREATE TABLE IF NOT EXISTS suppressions (
    template_id TEXT PRIMARY KEY,
    reason      TEXT DEFAULT '',
    added_at    TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS incident_archive (
    id           TEXT PRIMARY KEY,
    opened_at    TEXT DEFAULT '',
    resolved_at  TEXT DEFAULT '',
    severity     TEXT DEFAULT '',
    signature    TEXT DEFAULT '',
    cause        TEXT DEFAULT '',
    hypothesis   TEXT DEFAULT '',
    outcome      TEXT DEFAULT '',
    outcome_note TEXT DEFAULT '',
    archived_at  TEXT DEFAULT '',
    -- Baseline medians captured when a fix was called "worked",
    -- so "did it hold?" can be measured rather than asked.
    fix_snapshot TEXT DEFAULT ''
);
"""


def _slug(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.")
    return cleaned or "default"


class HotRing:
    """The most recent events, bounded, in memory. Feeds detectors; costs nothing."""

    def __init__(self, capacity: int = 5000) -> None:
        self._items: deque[Event] = deque(maxlen=capacity)

    def add(self, event: Event) -> None:
        self._items.append(event)

    def recent(self, n: int = 100) -> list[Event]:
        items = list(self._items)
        return items[-n:]

    def __len__(self) -> int:
        return len(self._items)










class TraceIndex:
    """trace_id -> its events, for fast assembly (the doc's C4 sub-component).
    Bounded: conformance needs recent complete traces, not history."""

    def __init__(self, max_traces: int = 50) -> None:
        self.max_traces = max_traces
        self._traces: dict[str, list[Event]] = {}

    def add(self, event: Event) -> None:
        if not event.trace_id:
            return
        bucket = self._traces.setdefault(event.trace_id, [])
        bucket.append(event)
        while len(self._traces) > self.max_traces:
            oldest = next(iter(self._traces))
            del self._traces[oldest]

    def traces(self) -> dict[str, list[Event]]:
        return dict(self._traces)

    def __len__(self) -> int:
        return len(self._traces)


class ProjectStore:
    """One project's derived data, in that project's own database file."""

    def __init__(self, project: str, root: Path | str | None = None) -> None:
        self.project = project
        base = Path(root) if root else AEGIS_HOME / "projects"
        self.dir = base / _slug(project)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "store.db"
        self._lock = threading.Lock()
        self._conn = self._open_resilient()
        self._pending = 0

    def _connect(self) -> sqlite3.Connection:
        """One connection, configured for this store's actual access pattern.

        The ingest thread writes continuously while HTTP threads read on
        every dashboard poll. In SQLite's default journal mode a writer locks
        the whole file, so a poll can stall ingestion and an ingest burst can
        stall the page - the exact shape of the bug that once hung
        /api/attach until the v1 store was put into WAL. This store is newer
        and never inherited that fix. WAL lets readers and the writer run at
        the same time, and is a persistent property of the file, so opening
        an existing database upgrades it in place.
        """
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        # In-memory databases have no journal to write ahead of, and a
        # read-only directory cannot host the -wal sidecar; neither is a
        # reason to fail, so the mode is best-effort.
        if str(self.path) != ":memory:":
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.DatabaseError:
                pass
        conn.executescript(_SCHEMA)
        # IF NOT EXISTS leaves an existing table exactly as it was, so a
        # column added later never appears on a database written earlier.
        # Every store on disk predates fix_snapshot.
        try:
            columns = {row[1] for row in
                       conn.execute("PRAGMA table_info(incident_archive)")}
            if "fix_snapshot" not in columns:
                conn.execute("ALTER TABLE incident_archive "
                             "ADD COLUMN fix_snapshot TEXT DEFAULT ''")
        except sqlite3.DatabaseError:
            pass          # a store too broken to migrate is handled above
        conn.execute("PRAGMA user_version = 1")
        return conn

    def _open_resilient(self) -> sqlite3.Connection:
        """A corrupt database must cost history, never the product. The bad
        file is set aside (not deleted - it may be recoverable) and a fresh
        one is started."""
        try:
            return self._connect()
        except sqlite3.DatabaseError:
            import time as _time
            quarantine = self.path.with_suffix(
                f".corrupt-{_time.strftime('%Y%m%d-%H%M%S')}")
            try:
                self.path.rename(quarantine)
                print(f"[aegis] corrupt store set aside: {quarantine}")
            except OSError:
                pass
            return self._connect()

    def record_event(self, event: Event) -> None:
        """Fold one event into the derived tables. Raw text is NOT stored."""
        minute = event.ts[:5] if event.ts else ""
        with self._lock:
            self._conn.execute(
                "INSERT INTO templates (id, pattern, service, count, first_seen,"
                " last_seen, example) VALUES (?, ?, ?, 1, ?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET"
                # The pattern updates on conflict because Drain widens a
                # template as it learns; the id names the cluster, the pattern
                # is its current best description.
                "   count = count + 1, last_seen = excluded.last_seen,"
                "   pattern = excluded.pattern",
                (
                    event.template_id,
                    _first_line(event),
                    event.service,
                    event.ts,
                    event.ts,
                    _first_line(event)[:200],
                ),
            )
            if minute:
                self._conn.execute(
                    "INSERT INTO template_minutes (template_id, minute, count)"
                    " VALUES (?, ?, 1)"
                    " ON CONFLICT(template_id, minute) DO UPDATE SET count = count + 1",
                    (event.template_id, minute),
                )
            # Commit in small batches: per-event fsync would put disk latency
            # in the ingest path for no benefit.
            self._pending += 1
            if self._pending >= 50:
                self._conn.commit()
                self._pending = 0

    def archive_incident(self, record: dict[str, Any]) -> None:
        columns = ("id", "opened_at", "resolved_at", "severity", "signature",
                   "cause", "hypothesis", "outcome", "outcome_note", "archived_at")
        with self._lock:
            # An incident is archived when it FORMS and again when it
            # RESOLVES, so INSERT OR REPLACE keeps one row per incident
            # rather than a duplicate per lifecycle change.
            self._conn.execute(
                f"INSERT OR REPLACE INTO incident_archive ({','.join(columns)})"
                f" VALUES ({','.join('?' for _ in columns)})",
                tuple(str(record.get(c, "")) for c in columns),
            )

    def add_suppression(self, template_id: str, reason: str = "") -> None:
        import time as _time
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO suppressions (template_id, reason, added_at)"
                " VALUES (?, ?, ?)",
                (template_id, reason, _time.strftime("%Y-%m-%d %H:%M")))
            self._conn.commit()

    def suppressions(self) -> list[dict[str, Any]]:
        with self._lock:
            self._conn.commit()
            rows = self._conn.execute(
                "SELECT * FROM suppressions ORDER BY added_at DESC").fetchall()
        return [dict(row) for row in rows]

    def archived_incidents(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            self._conn.commit()
            rows = self._conn.execute(
                "SELECT * FROM incident_archive ORDER BY archived_at DESC, id DESC"
                " LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def set_incident_outcome(self, incident_id: str, outcome: str, note: str) -> None:
        # Accepts the bare incident id or the dated archive key.
        with self._lock:
            self._conn.execute(
                "UPDATE incident_archive SET outcome = ?, outcome_note = ?"
                " WHERE id = ? OR id LIKE ? || '@%'",
                (outcome, note, incident_id, incident_id))
            self._conn.commit()

    def fix_snapshot(self, incident_id: str) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT fix_snapshot FROM incident_archive"
                " WHERE id = ? OR id LIKE ? || '@%' LIMIT 1",
                (incident_id, incident_id)).fetchone()
        return (row["fix_snapshot"] if row and row["fix_snapshot"] else "")

    def known_operations(self, source: str) -> list[tuple[str, str, int]]:
        """Every (component, operation) seen for a source, with how many samples."""
        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                "SELECT component, operation, COUNT(*) AS n FROM durations"
                " WHERE source = ? GROUP BY component, operation",
                (source,),
            ).fetchall()
        return [(r["component"], r["operation"], r["n"]) for r in rows]

    def samples(self, source: str, component: str, operation: str) -> list[float]:
        """The most recent measurements, oldest first.

        Ordered by id rather than timestamp: log timestamps repeat, are sometimes absent,
        and in a replayed file are not monotonic - insertion order is the only reliable
        sequence.
        """
        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                "SELECT value_ms FROM durations"
                " WHERE source = ? AND component = ? AND operation = ?"
                " ORDER BY id DESC LIMIT ?",
                (source, component, operation, KEEP_PER_OPERATION),
            ).fetchall()
        return [row["value_ms"] for row in reversed(rows)]

    def prune(self, source: str) -> int:
        """Drop measurements past the keep window.

        Called occasionally rather than on every insert - deleting on each write would
        put a scan in the ingest path for no benefit.
        """
        with self._lock:
            conn = self._connect()
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

    # -- runs --------------------------------------------------------------------
    def record_duration(self, source: str, component: str, operation: str,
                        value_ms: float, observed_at: str = "") -> None:
        """One measurement. Detection is counting; this is what it counts."""
        with self._lock:
            conn = self._connect()
            conn.execute(
                "INSERT INTO durations (source, component, operation, value_ms,"
                " observed_at) VALUES (?, ?, ?, ?, ?)",
                (source, component, operation, float(value_ms), observed_at))

    def set_fix_snapshot(self, incident_id: str, snapshot: str) -> None:
        """Remember what the numbers looked like when a fix was called done."""
        with self._lock:
            self._conn.execute(
                "UPDATE incident_archive SET fix_snapshot = ?"
                " WHERE id = ? OR id LIKE ? || '@%'",
                (snapshot, incident_id, incident_id))
            self._conn.commit()

    def flush(self) -> None:
        with self._lock:
            self._conn.commit()
            self._pending = 0

    def templates(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            self._conn.commit()
            rows = self._conn.execute(
                "SELECT * FROM templates ORDER BY count DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def template_count(self) -> int:
        with self._lock:
            self._conn.commit()
            row = self._conn.execute("SELECT COUNT(*) AS n FROM templates").fetchone()
        return int(row["n"])

    def total_events(self) -> int:
        with self._lock:
            self._conn.commit()
            row = self._conn.execute(
                "SELECT COALESCE(SUM(count), 0) AS n FROM templates").fetchone()
        return int(row["n"])

    def close(self) -> None:
        with self._lock:
            self._conn.commit()
            self._conn.close()


def _first_line(event: Event) -> str:
    return event.text_redacted.splitlines()[0] if event.text_redacted else ""
