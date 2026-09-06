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


class ProjectStore:
    """One project's derived data, in that project's own database file."""

    def __init__(self, project: str, root: Path | str | None = None) -> None:
        self.project = project
        base = Path(root) if root else AEGIS_HOME / "projects"
        self.dir = base / _slug(project)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "store.db"
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._pending = 0

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
