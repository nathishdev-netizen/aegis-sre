"""C1 - the source adapter layer, file flavour.

The doc's spec for this layer is one sentence long and worth obeying exactly:
dumb and reliable. Read, tag, forward. No parsing, no filtering, no judgement -
those belong in L2 where they are visible and configurable. The only state a
collector may hold is its read offset.

The file is opened READ-ONLY and never modified. A collector watches a
project's log; it must never be able to change that project.
"""

from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path
from typing import Iterator

from aegis.contracts.events import RawRecord

# Field guidance for tail tools, applied: bound the backfill (a multi-GB file
# must not freeze attach), cap pathological lines (Fluent Bit ships the same
# guard as Skip_Long_Lines), and detect rotation by inode - logrotate renames
# the old file and recreates the name, and if the new file outgrows the old
# offset before the next poll, a size check alone reads mid-file garbage.
BACKFILL_MAX_BYTES = int(os.environ.get("AEGIS_BACKFILL_MB", "10")) * 1024 * 1024
MAX_LINE_BYTES = int(os.environ.get("AEGIS_MAX_LINE_BYTES", "65536"))



def _size(num_bytes: int) -> str:
    """A byte count a person can read, at whatever unit fits.

    Integer-dividing into whole MB printed "the last 0MB of a 0MB file" for
    anything under a megabyte, which is most test fixtures and plenty of real
    logs.
    """
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}GB"


class FileCollector:
    """Tail one log file, yielding a tagged RawRecord per complete new line."""

    def __init__(self, path: str | Path, source_id: str = "", service: str = "",
                 from_start: bool = True) -> None:
        self.path = Path(path).expanduser()
        self.source_id = source_id or str(self.path)
        self.service = service or self.path.stem
        self._offset = 0
        self._partial = ""
        self._inode = 0
        self.backfill_note = ""
        # Why the last poll read nothing, when the reason was a failure
        # rather than an idle file. The caller swallows exceptions so that
        # one unreadable source cannot stop the others, which means a
        # permissions change or a full disk is otherwise completely silent:
        # the process stays up, the probe says ok, and nothing is read. The
        # count is what separates "quiet log" from "cannot read this file".
        self.read_error = ""
        self.consecutive_failures = 0
        self.last_read_at = 0.0
        if self.path.exists():
            stat = self.path.stat()
            self._inode = stat.st_ino
            if not from_start:
                self._offset = stat.st_size
            elif stat.st_size > BACKFILL_MAX_BYTES:
                self._offset = stat.st_size - BACKFILL_MAX_BYTES
                self._skip_partial_first = True
                self.backfill_note = (
                    f"backfilled the last {_size(BACKFILL_MAX_BYTES)}"
                    f" of a {_size(stat.st_size)} file")
        self._skip_partial_first = getattr(self, "_skip_partial_first", False)

    def _note_failure(self, reason: str) -> None:
        self.consecutive_failures += 1
        self.read_error = reason

    def _note_success(self) -> None:
        self.consecutive_failures = 0
        self.read_error = ""
        self.last_read_at = time.time()

    def poll(self) -> Iterator[RawRecord]:
        """Everything new since the last poll, as complete lines only.

        Two reliability rules, both learned from how real log files behave:

        - Rotation/truncation resets the offset to zero instead of raising. A
          collector that dies the night logrotate runs is not reliable.
        - A trailing fragment with no newline yet is held back until the writer
          finishes it. Emitting half a line would hand L2 a record that never
          existed.
        """
        # Every filesystem call below can fail in ways that are invisible
        # otherwise: the file deleted, its mode changed, the mount gone, the
        # disk full. Recording WHY beats raising, because the caller catches
        # and discards exceptions so one bad source cannot stop the rest.
        try:
            if not self.path.exists():
                self._note_failure("file does not exist")
                return
            stat = self.path.stat()
        except OSError as exc:
            self._note_failure(f"{exc.__class__.__name__}: {exc.strerror or exc}")
            return

        size = stat.st_size
        if self._inode and stat.st_ino != self._inode:
            # Rotated: same name, new file. Start it from the top.
            self._offset = 0
            self._partial = ""
        self._inode = stat.st_ino
        if size < self._offset:
            self._offset = 0
            self._partial = ""
        if size == self._offset:
            self._note_success()        # nothing new, but the file is readable
            return

        try:
            with self.path.open("r", errors="replace") as handle:
                handle.seek(self._offset)
                chunk = handle.read()
                self._offset = handle.tell()
        except OSError as exc:
            self._note_failure(f"{exc.__class__.__name__}: {exc.strerror or exc}")
            return
        self._note_success()

        text = self._partial + chunk
        lines = text.split("\n")
        self._partial = lines.pop()  # incomplete tail; next poll completes it

        # A capped backfill starts mid-file, so the first line is whatever the
        # byte offset landed inside - "ine number 1759" with the 'l' cut off.
        # The flag was set but never read here, so every capped attach emitted
        # one fabricated record, breaking this module's own promise that half a
        # line is never handed to L2.
        if self._skip_partial_first:
            self._skip_partial_first = False
            if lines:
                lines.pop(0)

        collected_at = datetime.now().isoformat(timespec="seconds")
        for line in lines:
            if line.strip():
                # MAX_LINE_BYTES was declared and never applied, so a single
                # pathological line - a dumped request body, a base64 blob, a
                # minified stack - went through whole, into templating, storage
                # and every prompt built from it. Truncating says so in the
                # payload rather than silently losing the tail.
                if len(line) > MAX_LINE_BYTES:
                    line = line[:MAX_LINE_BYTES] + "\u2026[truncated]"
                yield RawRecord(
                    source_id=self.source_id,
                    payload=line,
                    service=self.service,
                    collected_at=collected_at,
                )
