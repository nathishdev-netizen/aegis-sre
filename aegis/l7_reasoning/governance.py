"""C16 - governance: the rules the runtime enforces, not the prompt.

Principle P7: spend limits are enforced by the runtime, not by prompt
instructions. A model cannot be trusted to ration itself, and an outage is
exactly when an ungoverned reasoning layer would spend the most - the doc's
failure mode is "a runaway bill during an outage", usually alongside the
outage itself.

Two mechanisms, both dumb on purpose:

  Budget    a hard ceiling on model calls per session and a floor between
            calls. When it is exhausted, the answer is no - queued is not
            implemented yet, so refused-and-logged is the honest behaviour.
  AuditLog  every model call appended to ~/.aegis/audit.jsonl: when, which
            provider and model, for what, and whether it was allowed. The
            record exists BEFORE the call is attempted, so a crash mid-call
            still leaves a trace.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

AUDIT_PATH = Path(os.path.expanduser("~/.aegis/audit.jsonl"))


@dataclass
class Decision:
    allowed: bool
    reason: str


class Budget:
    """Hard ceilings. The defaults are deliberately small: the funnel's whole
    point is single-digit model calls per day, so a session wanting dozens is
    evidence of a bug upstream, not a reason to raise the limit."""

    def __init__(self, max_calls: int = 10, min_interval_s: float = 1.0) -> None:
        self.max_calls = max_calls
        self.min_interval_s = min_interval_s
        self.calls_made = 0
        self._last_call = 0.0
        self._lock = threading.Lock()

    def request(self) -> Decision:
        with self._lock:
            if self.calls_made >= self.max_calls:
                return Decision(False, f"budget exhausted ({self.max_calls} calls)")
            wait = self.min_interval_s - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self.calls_made += 1
            self._last_call = time.monotonic()
            return Decision(True, "ok")


class AuditLog:
    def __init__(self, path: Path | str = AUDIT_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def record(self, **fields: Any) -> None:
        entry = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), **fields}
        try:
            with self._lock, self.path.open("a") as handle:
                handle.write(json.dumps(entry) + "\n")
        except OSError:
            # Auditing must never take the reasoning layer down with it,
            # but a silent audit failure would defeat its purpose - so the
            # failure itself is printed, once per process would be nicer but
            # plainly is honest enough.
            print(f"[aegis] audit write failed: {self.path}")
