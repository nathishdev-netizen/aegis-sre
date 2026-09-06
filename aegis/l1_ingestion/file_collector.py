"""C1 - the source adapter layer, file flavour.

The doc's spec for this layer is one sentence long and worth obeying exactly:
dumb and reliable. Read, tag, forward. No parsing, no filtering, no judgement -
those belong in L2 where they are visible and configurable. The only state a
collector may hold is its read offset.

The file is opened READ-ONLY and never modified. A collector watches a
project's log; it must never be able to change that project.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Iterator

from aegis.contracts.events import RawRecord


class FileCollector:
    """Tail one log file, yielding a tagged RawRecord per complete new line."""

    def __init__(self, path: str | Path, source_id: str = "", service: str = "",
                 from_start: bool = True) -> None:
        self.path = Path(path).expanduser()
        self.source_id = source_id or str(self.path)
        self.service = service or self.path.stem
        self._offset = 0
        self._partial = ""
        if not from_start and self.path.exists():
            self._offset = self.path.stat().st_size

    def poll(self) -> Iterator[RawRecord]:
        """Everything new since the last poll, as complete lines only.

        Two reliability rules, both learned from how real log files behave:

        - Rotation/truncation resets the offset to zero instead of raising. A
          collector that dies the night logrotate runs is not reliable.
        - A trailing fragment with no newline yet is held back until the writer
          finishes it. Emitting half a line would hand L2 a record that never
          existed.
        """
        if not self.path.exists():
            return
        size = self.path.stat().st_size
        if size < self._offset:
            self._offset = 0
            self._partial = ""
        if size == self._offset:
            return

        with self.path.open("r", errors="replace") as handle:
            handle.seek(self._offset)
            chunk = handle.read()
            self._offset = handle.tell()

        text = self._partial + chunk
        lines = text.split("\n")
        self._partial = lines.pop()  # incomplete tail; next poll completes it

        collected_at = datetime.now().isoformat(timespec="seconds")
        for line in lines:
            if line.strip():
                yield RawRecord(
                    source_id=self.source_id,
                    payload=line,
                    service=self.service,
                    collected_at=collected_at,
                )
