"""One project's pipeline: collector -> normalizer -> linker -> store.

A Pipeline is bound to exactly ONE project. Its collector opens that project's
log read-only; its store writes only inside that project's own directory under
~/.aegis/projects/. Two pipelines for two projects share no state, no tables
and no files - isolation is structural, not a convention someone must remember.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from aegis.l1_ingestion.file_collector import FileCollector
from aegis.l2_normalization.normalizer import Normalizer
from aegis.l2_normalization.tracelinker import TraceLinker
from aegis.l3_storage.store import HotRing, ProjectStore
from aegis.l5_detection.detectors import DetectionEngine
from aegis.l6_correlation.incidents import IncidentManager


class Pipeline:
    def __init__(self, project: str, log_path: str | Path,
                 store_root: Path | str | None = None) -> None:
        self.project = project
        self.collector = FileCollector(log_path, service=project)
        self.normalizer = Normalizer(service=project)
        self.linker = TraceLinker()
        self.store = ProjectStore(project, root=store_root)
        self.hot = HotRing()
        self.detect = DetectionEngine(service=project)
        self.incidents = IncidentManager()

    def run_once(self) -> int:
        """Process every line currently available. Returns events committed."""
        committed = 0
        for record in self.collector.poll():
            event = self.normalizer.feed(record.payload)
            if event is not None:
                self._commit(event)
                committed += 1
        # The normalizer's pending event is NOT flushed here: the next poll may
        # bring the continuation lines that belong to it.
        return committed

    def drain(self) -> int:
        """End of stream: release the final pending event and persist."""
        committed = 0
        event = self.normalizer.flush()
        if event is not None:
            self._commit(event)
            committed = 1
        self.incidents.finalize(self.detect.last_now)
        self.store.flush()
        return committed

    def stats(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "store": str(self.store.path),
            "lines_in": self.normalizer.lines_in,
            "events": self.normalizer.events_out,
            "templates": self.store.template_count(),
            "extracted": self.linker.extracted,
            "inferred": self.linker.inferred,
            "unattributed": self.linker.unattributed,
            "signals": len(self.detect.signals),
            **self.incidents.summary(),
        }

    def close(self) -> None:
        self.store.flush()
        self.store.close()

    def _commit(self, event) -> None:
        self.linker.link(event)
        self.store.record_event(event)
        self.hot.add(event)
        for signal in self.detect.observe(event):
            self.incidents.observe(signal, self.detect.last_now)
