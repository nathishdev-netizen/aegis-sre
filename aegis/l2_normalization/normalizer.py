"""The C3 pipeline: fold, extract, redact, fingerprint, emit.

One raw line in, zero or one Event out. Zero, because a continuation line
(a traceback frame, a payload dump row) belongs to the event before it: one
exception is one event, and a twenty-line JSON dump is one event, not twenty.

Order inside the pipeline is load-bearing:

  fold        first - a dump's rows must join their parent before they are
              fingerprinted, or each row becomes its own junk template
  extract     timestamp, level, message (delegated to the proven v1 parser)
  redact      BEFORE storage and before fingerprinting - there is no second
              chance once a phone number is on disk
  fingerprint on redacted text, so a thousand distinct phone numbers do not
              produce a thousand distinct templates

The import direction is one-way by design: aegis reuses app.core.parser, but
nothing in app/ imports aegis - the existing agent stays provably unaffected.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass
from typing import Iterator

from app.core.parser import is_continuation, parse_log_line

from aegis.contracts.events import Event
from aegis.l2_normalization.fingerprint import Fingerprinter
from aegis.l2_normalization.redactor import Redactor

# A dump can be long, but an unbounded fold would let one malformed stream
# swallow the whole file into a single event.
MAX_CONTINUATION_LINES = 200

# Logger scaffolding ("INFO voice.tts:") that sits between the timestamp and the
# actual content in this format family.
_SCAFFOLD = re.compile(
    r"^\s*(?:INFO|WARN|WARNING|ERROR|DEBUG|TRACE|CRITICAL|FATAL|SUCCESS)\s+[\w.]+:\s*",
    re.I,
)

# Rows of a structured dump (a table printed row by row through the logger).
# Each row is a fully-formed log line with its own timestamp - so indentation
# folding never sees them - but a box-drawing prefix or a bare closing brace
# marks the CONTENT as a continuation of the entry above.
_DUMP_ROW = re.compile(r"^(?:[\u2500-\u257f]|[}\]],?$)")

# A top-left corner opens a dump: it is the dump's TITLE, so it starts the
# event the following rows fold into, rather than folding itself.
_DUMP_START = re.compile(r"^[\u250c\u256d]")


def _continues_previous(raw_line: str, parsed_message: str) -> bool:
    """Continuation by indentation (tracebacks) or by content (dump rows)."""
    if is_continuation(raw_line):
        return True
    content = _SCAFFOLD.sub("", parsed_message or "").strip()
    if _DUMP_START.match(content):
        return False
    return bool(content) and bool(_DUMP_ROW.match(content))


@dataclass
class _Pending:
    """An event under construction, still open to continuation lines."""

    parsed: dict
    extra_lines: list[str]
    service: str


class Normalizer:
    """Turns raw lines into redacted, fingerprinted Events.

    Stateful for one reason only: a line cannot be emitted until the next line
    proves it is not going to be continued. Call flush() at end of stream to
    release the final event.
    """

    def __init__(self, service: str = "", redactor: Redactor | None = None,
                 fingerprinter: Fingerprinter | None = None) -> None:
        self.service = service
        self.redactor = redactor or Redactor()
        self.fingerprinter = fingerprinter or Fingerprinter()
        self._pending: _Pending | None = None
        self._last_real_ts = ""
        self._ids = itertools.count(1)
        self.lines_in = 0
        self.events_out = 0

    # -- pipeline ------------------------------------------------------------

    def feed(self, raw_line: str) -> Event | None:
        """Offer one raw line; returns the previous event if this line closed it."""
        if not raw_line.strip():
            return None
        self.lines_in += 1

        parsed = parse_log_line(raw_line.rstrip())
        # The v1 parser stamps wall-clock "now" on a line that carries no
        # timestamp - right for live tailing, but on a historical file it
        # fabricates a time that appears nowhere in the evidence. A timestamp
        # is real only if the line actually contains it; otherwise the line
        # inherits the last real one, like a reader would assume.
        if parsed["timestamp"] and parsed["timestamp"] not in raw_line:
            parsed["timestamp"] = self._last_real_ts
        else:
            self._last_real_ts = parsed["timestamp"]
        if self._pending is not None \
                and _continues_previous(raw_line, parsed["message"]) \
                and len(self._pending.extra_lines) < MAX_CONTINUATION_LINES:
            # Keep only the content: repeating the timestamp and logger name on
            # every folded row buries the values the row exists to show.
            content = _SCAFFOLD.sub("", parsed["message"] or raw_line.rstrip())
            self._pending.extra_lines.append(content)
            return None

        completed = self._finalize()
        self._pending = _Pending(parsed=parsed, extra_lines=[], service=self.service)
        return completed

    def flush(self) -> Event | None:
        """Release the last event once the stream is known to be over."""
        return self._finalize()

    def feed_all(self, raw_lines) -> Iterator[Event]:
        """Convenience for whole files: yields every completed event."""
        for raw_line in raw_lines:
            event = self.feed(raw_line)
            if event is not None:
                yield event
        final = self.flush()
        if final is not None:
            yield final

    # -- internals -----------------------------------------------------------

    def _finalize(self) -> Event | None:
        if self._pending is None:
            return None
        pending, self._pending = self._pending, None

        message = pending.parsed["message"] or pending.parsed["raw_line"]
        if pending.extra_lines:
            message = "\n".join([message, *pending.extra_lines])

        redaction = self.redactor.redact(message)

        # Fingerprint on the head line only: a template names the SHAPE of the
        # entry, and the head line is its shape - the folded body is detail
        # (frame paths, dump values) that varies per occurrence.
        head = redaction.text.splitlines()[0] if redaction.text else redaction.text
        match = self.fingerprinter.add(
            head,
            when=pending.parsed["timestamp"],
            service=pending.service,
        )

        self.events_out += 1
        return Event(
            id=f"E-{next(self._ids)}",
            ts=pending.parsed["timestamp"],
            service=pending.service,
            level=pending.parsed["level"],
            text_redacted=redaction.text,
            template_id=match.template_id,
            redactions=redaction.counts,
            is_novel=match.is_novel,
            fields={"folded_lines": len(pending.extra_lines)} if pending.extra_lines else {},
        )
