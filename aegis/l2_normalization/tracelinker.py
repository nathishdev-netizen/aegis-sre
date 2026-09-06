"""Attach a trace_id to every event that can honestly carry one.

trace_id is the join key for the entire platform - every layer above L3 gets
measurably worse without it. But in the reference log only 11% of lines carry
an explicit key (call=<uuid>); the other 89% are emitted between a call's start
and end without naming it. So this component does two jobs, and never confuses
them:

  extract   the line names its key. Evidence. basis = "extracted"
  infer     the line names nothing, but exactly ONE session is open, so it can
            only belong to that one. Assumption. basis = "inferred"

When zero or MORE THAN ONE session is open, an unkeyed line gets no trace_id at
all. The reference service happens to handle one call at a time, which makes
inference safe there - but a production service overlaps, and attributing a
line to the wrong customer's trace is worse than attributing it to none.
Correlation degrades from a join to a guess exactly here, and the basis field
is how downstream layers know which one they are holding.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from aegis.contracts.events import (
    CORRELATION_EXTRACTED,
    CORRELATION_INFERRED,
    CORRELATION_NONE,
    Event,
)

# Explicit keys, in order of trust. A real trace_id outranks an application's
# own call id, which outranks a generic request id.
_KEY_PATTERNS = (
    re.compile(r"\btrace[_-]?id[=:]\s*([\w-]{8,})", re.I),
    re.compile(r"\bcall(?:_?uuid|_?id)?[=:]\s*([\w-]{8,})", re.I),
    re.compile(r"\b(?:request|correlation|session)[_-]?id[=:]\s*([\w-]{8,})", re.I),
)

# A session is over when its owner says so...
_CLOSERS = re.compile(r"\b(?:CALL END|hangup|session (?:closed|ended)|disconnected)\b", re.I)
# ...or when it has been silent this long. Without a timeout, a crash that
# never logs its END would leave the session open forever and every later
# unkeyed line would be inferred into a call that finished an hour ago.
IDLE_TIMEOUT_S = 300.0


def _epoch(ts: str) -> float | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("T", " ").split("+")[0].strip()).timestamp()
    except ValueError:
        return None


@dataclass
class _Session:
    key: str
    last_seen: float | None


class TraceLinker:
    """Stateful: remembers which sessions are open, in arrival order."""

    def __init__(self, idle_timeout_s: float = IDLE_TIMEOUT_S) -> None:
        self.idle_timeout_s = idle_timeout_s
        self._open: dict[str, _Session] = {}
        self.extracted = 0
        self.inferred = 0
        self.unattributed = 0

    def link(self, event: Event) -> Event:
        """Fill in trace_id and correlation_basis on the event, in place."""
        now = _epoch(event.ts)
        self._expire(now)

        key = self._extract_key(event.text_redacted)
        if key:
            event.trace_id = key
            event.correlation_basis = CORRELATION_EXTRACTED
            self.extracted += 1
            if _CLOSERS.search(event.text_redacted):
                # The closing line itself still belongs to the session.
                self._open.pop(key, None)
            else:
                self._open[key] = _Session(key=key, last_seen=now)
            return event

        if len(self._open) == 1:
            session = next(iter(self._open.values()))
            event.trace_id = session.key
            event.correlation_basis = CORRELATION_INFERRED
            session.last_seen = now or session.last_seen
            self.inferred += 1
        else:
            # Zero open: nothing to belong to. Two or more open: guessing
            # would attribute the line to the wrong trace half the time.
            event.correlation_basis = CORRELATION_NONE
            self.unattributed += 1
        return event

    def _extract_key(self, text: str) -> str | None:
        for pattern in _KEY_PATTERNS:
            match = pattern.search(text)
            if match:
                return match.group(1)
        return None

    def _expire(self, now: float | None) -> None:
        if now is None:
            return
        expired = [
            key for key, session in self._open.items()
            if session.last_seen is not None
            and now - session.last_seen > self.idle_timeout_s
        ]
        for key in expired:
            del self._open[key]

    @property
    def open_sessions(self) -> list[str]:
        return list(self._open)
