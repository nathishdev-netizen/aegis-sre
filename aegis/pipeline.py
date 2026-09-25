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


class Pipeline:
    def __init__(self, project: str, log_path: str | Path,
                 store_root: Path | str | None = None) -> None:
        self.project = project
        self.collector = FileCollector(log_path, service=project)
        self.normalizer = Normalizer(service=project)
        # Secondary sources: a dependency's log watched ALONGSIDE the primary,
        # feeding the same linker/store/detectors, so one request's story can
        # span both processes. Each source keeps its own collector+normalizer
        # (folding is per-stream state) but everything downstream is shared.
        self.secondaries: list[tuple[FileCollector, Normalizer]] = []
        # Secondary sources: a dependency's log watched ALONGSIDE the primary,
        # feeding the same linker/store/detectors, so one request's story can
        # span both processes. Each source keeps its own collector+normalizer
        # (folding is per-stream state) but everything downstream is shared.
        self.secondaries: list[tuple[FileCollector, Normalizer]] = []
        # Secondary sources: a dependency's log watched ALONGSIDE the primary,
        # feeding the same linker/store/detectors, so one request's story can
        # span both processes. Each source keeps its own collector+normalizer
        # (folding is per-stream state) but everything downstream is shared.
        self.secondaries: list[tuple[FileCollector, Normalizer]] = []
        # Secondary sources: a dependency's log watched ALONGSIDE the primary,
        # feeding the same linker/store/detectors, so one request's story can
        # span both processes. Each source keeps its own collector+normalizer
        # (folding is per-stream state) but everything downstream is shared.
        self.secondaries: list[tuple[FileCollector, Normalizer]] = []
        # Secondary sources: a dependency's log watched ALONGSIDE the primary,
        # feeding the same linker/store/detectors, so one request's story can
        # span both processes. Each source keeps its own collector+normalizer
        # (folding is per-stream state) but everything downstream is shared.
        self.secondaries: list[tuple[FileCollector, Normalizer]] = []
        self.linker = TraceLinker()
        self.store = ProjectStore(project, root=store_root)
        self.hot = HotRing()

    def add_secondary(self, log_path: str | Path, service: str) -> dict:
        """Watch a dependency's log together with the primary."""
        path = Path(log_path).expanduser()
        if not path.is_file():
            return {"ok": False, "detail": f"not a file: {path}"}
        if any(str(c.path) == str(path) for c, _n in self.secondaries) \
                or str(path) == str(self.collector.path):
            return {"ok": False, "detail": "already watching that file"}
        self.secondaries.append(
            (FileCollector(path, service=service), Normalizer(service=service)))
        return {"ok": True, "service": service, "path": str(path)}

    def run_once(self) -> int:
        """Process every line currently available. Returns events committed."""
        committed = 0
        for record in self.collector.poll():
            event = self.normalizer.feed(record.payload)
            if event is not None:
                self._commit(event)
                committed += 1
        for collector, normalizer in self.secondaries:
            for record in collector.poll():
                event = normalizer.feed(record.payload)
                if event is not None:
                    self._commit(event)
                    committed += 1
        # Pending events are NOT flushed here: the next poll may bring the
        # continuation lines that belong to them.
        return committed

    def drain(self) -> int:
        """End of stream: release the final pending event and persist."""
        committed = 0
        event = self.normalizer.flush()
        if event is not None:
            self._commit(event)
            committed = 1
        for _collector, normalizer in self.secondaries:
            event = normalizer.flush()
            if event is not None:
                self._commit(event)
                committed += 1
        for _collector, normalizer in self.secondaries:
            event = normalizer.flush()
            if event is not None:
                self._commit(event)
                committed += 1
        for _collector, normalizer in self.secondaries:
            event = normalizer.flush()
            if event is not None:
                self._commit(event)
                committed += 1
        for _collector, normalizer in self.secondaries:
            event = normalizer.flush()
            if event is not None:
                self._commit(event)
                committed += 1
        for _collector, normalizer in self.secondaries:
            event = normalizer.flush()
            if event is not None:
                self._commit(event)
                committed += 1
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
        }

    def close(self) -> None:
        self.store.flush()
        self.store.close()

    def _commit(self, event) -> None:
        self.linker.link(event)
        self.store.record_event(event)
        self.hot.add(event)
