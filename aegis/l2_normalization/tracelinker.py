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
    re.compile(r"\btrace[_-]?id[\"']?\s*[=:]\s*[\"']?([\w-]{8,})", re.I),
    re.compile(r"\bcall(?:_?uuid|_?id)?[=:]\s*([\w-]{8,})", re.I),
    # Quotes are optional so the same rule reads logfmt (requestID=abc) and
    # JSON ("reqId":"req-9f1c") - the world's most common structured formats,
    # and the first corpus run showed neither was being read at all.
    re.compile(r"\b(?:req|request|correlation|session)[_-]?id[\"']?\s*[=:]\s*[\"']?([\w-]{6,})", re.I),
    # A BUSINESS key, last because it is the weakest claim: two services can
    # legitimately log the same order_id hours apart, where a request id is
    # one request by construction. But a commercial system very often has no
    # trace id at all and correlates on the thing it is actually processing -
    # order_id, payment_id, job_id - and without this such a system produced
    # NO runs whatsoever, so nothing above L3 could see it. Deliberately a
    # fixed list rather than `\w+_id`: matching any *_id would join two
    # unrelated runs that happen to touch the same customer_id or user_id.
    re.compile(r"\b(?:order|payment|invoice|booking|shipment|job|task|batch"
               r"|transaction|txn|workflow|run)[_-]?id[\"']?\s*[=:]\s*"
               r"[\"']?([\w-]{4,})", re.I),
)

# Session boundaries, learned from the project's own vocabulary rather than
# hardcoded. The first attempt said "CALL END" - the reference project's word -
# and every other project's sessions stayed open forever. The second attempt
# said any "END" - and the reference project's "TURN END" closed the whole
# call mid-conversation. The rule that survives both: an opener like
# "CALL START" or "ORDER START" names the session's own noun, and only that
# noun's END closes it. Hard closers (hangup, disconnected) close regardless,
# and a session opened without a named noun accepts any end-word.
_OPENER = re.compile(r"\b(\w+)\s+START(?:ED)?\b", re.I)
_NOUN_END = re.compile(r"\b(\w+)\s+END(?:ED)?\b", re.I)
_HARD_CLOSERS = re.compile(r"\b(?:hangup|hung up|disconnected)\b", re.I)
# A request/response service that emits no correlation id at all still marks
# its own boundaries - "request received" ... "responded". Without this, every
# line of such a service was unattributable, so it had no runs, no verdicts and
# no duration: the service was invisible to every layer above L3 even though
# its log said plainly where each request began and ended.
_UNKEYED_OPEN = re.compile(
    r"\b(?:request|call)\s+(?:received|started|start|incoming)\b"
    r"|\brequest\s+received\b", re.I)
_UNKEYED_CLOSE = re.compile(
    r"\b(?:responded|response\s+sent|stream\s+closed|request\s+completed"
    r"|request\s+finished)\b", re.I)
_GENERIC_END = re.compile(r"\b(?:end(?:ed)?|closed|completed?|finished)\b", re.I)
# ...or when it has been silent this long. Without a timeout, a crash that
# never logs its END would leave the session open forever and every later
# unkeyed line would be inferred into a call that finished an hour ago.
IDLE_TIMEOUT_S = 300.0
# A synthetic session is a GUESS that a stretch of unkeyed lines is one
# request. Past this many events the guess has clearly failed - a startup
# burst with no response line ran to 568 events in one log, which is not a
# request by any reading. Capping it keeps a bad guess small instead of
# letting it swallow the file; the lines simply go unattributed, which is
# what they were before this inference existed.
MAX_SYNTHETIC_EVENTS = 60


def _epoch(ts: str) -> float | None:
    """Seconds for the idle timeout, from any timestamp shape we store.

    The normalizer keeps a bare clock ("15:49:03") for a line whose log prints
    one, and this only understood a full date - so `now` was always None, the
    idle timeout never ran, and sessions accumulated forever. Twenty-two piled
    up in one log; since inference requires exactly ONE open session, that
    silently disabled correlation for the entire file.

    A bare clock gives elapsed seconds within the day. That is enough for a
    timeout, which only ever reads DIFFERENCES - and where the difference goes
    negative across midnight, the session is expired rather than kept, which
    is the safe direction.
    """
    if not ts:
        return None
    text = ts.replace("T", " ").split("+")[0].strip()
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        pass
    parts = text.split(":")
    if len(parts) == 3:
        try:
            h, m, sec = (float(part) for part in parts)
        except ValueError:
            return None
        return h * 3600 + m * 60 + sec
    return None


@dataclass
class _Session:
    key: str
    last_seen: float | None
    # The session's own word for itself ("CALL", "ORDER"), from its opener.
    noun: str = ""
    # Opened from a boundary line rather than a key the log stated. Closed by
    # the matching response line, not by a noun's END.
    synthetic: bool = False
    # How many events this synthetic guess has absorbed so far.
    seen: int = 0


class TraceLinker:
    """Stateful: remembers which sessions are open, in arrival order."""

    def __init__(self, idle_timeout_s: float = IDLE_TIMEOUT_S) -> None:
        self.idle_timeout_s = idle_timeout_s
        self._open: dict[str, _Session] = {}
        self._synthetic = 0
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
            session = self._open.get(key)
            if session is None:
                session = _Session(key=key, last_seen=now)
                self._open[key] = session
            else:
                session.last_seen = now or session.last_seen
            opener = _OPENER.search(event.text_redacted)
            if opener and not session.noun:
                session.noun = opener.group(1).upper()
            if self._closes(event.text_redacted, session):
                # The closing line itself still belongs to the session.
                self._open.pop(key, None)
            return event

        # An unkeyed line that OPENS a request starts a session of its own, so
        # a service that emits no correlation id at all still produces runs.
        # Only when nothing else is open: while a session is running we cannot
        # tell a genuinely new request from a line that merely reads like one,
        # and inventing a second session would split one run in two.
        if _UNKEYED_OPEN.search(event.text_redacted):
            # A second "request received" means the previous request ended,
            # whether or not it logged a response - a crash never writes one.
            # Without retiring it first, one session absorbed 580 lines over
            # 26 minutes because a new request could not displace it.
            stale = [k for k, v in self._open.items() if v.synthetic]
            for key in stale:
                self._open.pop(key, None)
            # Only with nothing else running: while a KEYED session is open we
            # cannot tell a genuinely new request from a line that merely
            # reads like one, and a second session would split one run in two.
            if not self._open:
                self._synthetic += 1
                new_key = f"req-{self._synthetic:06d}"
                self._open[new_key] = _Session(
                    key=new_key, last_seen=now, synthetic=True)

        closing = bool(_UNKEYED_CLOSE.search(event.text_redacted))

        if len(self._open) == 1:
            session = next(iter(self._open.values()))
            event.trace_id = session.key
            event.correlation_basis = CORRELATION_INFERRED
            session.last_seen = now or session.last_seen
            session.seen += 1
            self.inferred += 1
            if (closing or self._closes(event.text_redacted, session)
                    or (session.synthetic
                        and session.seen >= MAX_SYNTHETIC_EVENTS)):
                self._open.pop(session.key, None)
        elif closing and self._open:
            # The line that ENDS a run usually names no key - "stream closed",
            # "responded" - so it never reached the keyed close path above and
            # its session stayed open forever. Forty-two accumulated in one
            # log, and since inference needs exactly ONE open session, that
            # silently disabled correlation for the entire file.
            #
            # With several open we cannot say which one ended, so the line
            # itself stays unattributed - but the most recently active session
            # is the only honest candidate to close, and closing it is what
            # lets the count fall back to one.
            newest = max(self._open.values(),
                         key=lambda x: (x.last_seen or 0.0))
            self._open.pop(newest.key, None)
            event.correlation_basis = CORRELATION_NONE
            self.unattributed += 1
        else:
            # Zero open: nothing to belong to. Two or more open: guessing
            # would attribute the line to the wrong trace half the time.
            event.correlation_basis = CORRELATION_NONE
            self.unattributed += 1
        return event

    @staticmethod
    def _closes(text: str, session: _Session) -> bool:
        if _HARD_CLOSERS.search(text):
            return True
        if session.noun:
            match = _NOUN_END.search(text)
            return bool(match and match.group(1).upper() == session.noun)
        return bool(_GENERIC_END.search(text))

    def _extract_key(self, text: str) -> str | None:
        for pattern in _KEY_PATTERNS:
            match = pattern.search(text)
            if match:
                return match.group(1)
        return None

    def _expire(self, now: float | None) -> None:
        if now is None:
            return
        # abs(), because a bare clock wraps at midnight: a session last seen
        # at 23:59 and a line at 00:01 give a large NEGATIVE difference, which
        # is still a session that should be closed, not one two minutes old.
        expired = [
            key for key, session in self._open.items()
            if session.last_seen is not None
            and abs(now - session.last_seen) > self.idle_timeout_s
        ]
        for key in expired:
            del self._open[key]

    @property
    def open_sessions(self) -> list[str]:
        return list(self._open)
