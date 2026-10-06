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
from aegis.l3_storage.store import HotRing, ProjectStore, TraceIndex
from aegis.l4_understanding.conformance import ConformanceEngine
from aegis.l4_understanding.flowspec import _seconds
from aegis.l4_understanding.topology import Topology
from aegis.l5_detection.detectors import DetectionEngine
from aegis.l6_correlation.incidents import IncidentManager
from aegis.l6_correlation.memory import IncidentMemory


def _count_by_status(incidents) -> dict[str, int]:
    counts: dict[str, int] = {}
    for incident in incidents:
        counts[incident.status] = counts.get(incident.status, 0) + 1
    return counts


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
        self.linker = TraceLinker()
        self.store = ProjectStore(project, root=store_root)
        self.hot = HotRing()
        self.trace_index = TraceIndex()
        self.detect = DetectionEngine(service=project)
        self.incidents = IncidentManager()
        self.memory = IncidentMemory(self.store)
        self.incidents.on_resolve = self.memory.remember
        self.topology = Topology()
        # C7 in enforce mode: a run that deviates from the spec becomes a
        # signal like any other, so it lands in an incident and gets the
        # same explain -> propose -> verify treatment. Shadow mode computed
        # the same deviations and threw them away, which is why a run could
        # be judged "hollow" on the Runs page while Incidents stayed empty.
        self.conformance = ConformanceEngine(mode="enforce")
        self._judged: set[str] = set()
        # The server owns the spec (saved, human-editable, purpose-marked),
        # so the pipeline asks for it instead of mining a second one that
        # would disagree with what the Flow & spec page shows.
        self.spec_provider = None
        try:
            self.detect.suppressed = {row["template_id"]
                                      for row in self.store.suppressions()}
        except Exception:
            pass

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

    def _remine_topology(self) -> None:
        """Re-derive who calls whom from the traces seen so far.

        Cheap enough to redo periodically, and the incident manager ranks
        causes by it - a service that everything depends on is a better
        explanation than the one that merely reported first.
        """
        try:
            self.topology = Topology.mine(self.trace_index.traces())
            self.incidents.set_topology(self.topology)
        except Exception:
            pass

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
            # The funnel the UI draws: lines in, events out, templates
            # learned, signals raised, incidents opened. Every stage is a
            # count, because detection is counting.
            "signals": len(self.detect.signals),
            "incidents": len(self.incidents.incidents),
            "by_status": _count_by_status(self.incidents.incidents),
            "notes": len(self.incidents.notes),
        }

    def suppress_template(self, template_id: str, reason: str = "") -> None:
        """Mute one template for good. Persisted, so it survives a restart -
        a mute the user has to re-apply every session is not a mute."""
        self.store.add_suppression(template_id, reason)
        self.detect.suppressed.add(template_id)

    def unsuppress_template(self, template_id: str) -> None:
        """Undo a mute, on disk as well as live - otherwise the row outlives
        the undo and the next restart silently re-applies it."""
        self.store.remove_suppression(template_id)
        self.detect.suppressed.discard(template_id)

    def close(self) -> None:
        self.store.flush()
        self.store.close()

    def _commit(self, event) -> None:
        self.linker.link(event)
        self.store.record_event(event)
        self.hot.add(event)
        self.trace_index.add(event)
        if self.normalizer.events_out % 200 == 0:
            self._remine_topology()
        for signal in self.detect.observe(event):
            self.incidents.observe(signal, self.detect.last_now)
        self._judge_quiet_traces()

    # A trace is judged once it stops growing - judging on every event would
    # call every run "hollow" until the step that proves otherwise arrives.
    TRACE_QUIET_S = 20.0

    def _judge_quiet_traces(self) -> None:
        """A run that deviated from the spec becomes an incident.

        C7 used to run only in shadow mode on a read path, so its deviations
        were computed and thrown away: the Runs page could call a run hollow
        while Incidents stayed empty, and nothing could be proposed or
        verified. Judged here, once, when the run has actually finished.
        """
        spec = self.spec_provider() if self.spec_provider else None
        if spec is None:
            return
        now = self.detect.last_now
        if now is None:
            return
        for trace_id, events in self.trace_index.traces().items():
            # No minimum event count: a run that died early has FEWER events
            # precisely because it failed, and a "> 4 events" gate made
            # exactly those runs invisible. Quietness proves it is over.
            if trace_id in self._judged or len(events) < 2:
                continue
            last = _seconds(events[-1].ts) if events[-1].ts else None
            if last is None or now - last < self.TRACE_QUIET_S:
                continue
            self._judged.add(trace_id)
            try:
                _report, signals = self.conformance.check(trace_id, events, spec)
            except Exception:
                continue
            for signal in signals:
                self.incidents.observe(signal, now)
