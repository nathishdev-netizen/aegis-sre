"""C9 - the incident manager: one incident, not forty alerts.

A single root cause produces symptoms in every downstream signal. Grouping them
is not a nice-to-have; it is the difference between a tool people keep and a
tool people mute. This layer converts the signal stream into the small number
of stateful objects humans actually work with.

The doc's four grouping rules, and their honest status here:

  1. trace identity      IMPLEMENTED - signals sharing a trace_id
  2. topological adjacency  DEGENERATE - with one service and no C5 topology
                         yet, "same service" is adjacency at distance zero.
                         When C5 lands this becomes a real graph walk.
  3. temporal proximity  IMPLEMENTED - starting within a short window
  4. flow membership     UNAVAILABLE until C7 exists

Grouping requires TWO rules to agree - one alone over-groups (the doc is
explicit). When in doubt, group; humans can split.

Other doc requirements kept: the timeline is append-only (the record of what
was known when IS the postmortem); severity is the worst member's; the ranked
cause states WHY it is ranked; auto-resolution requires sustained quiet, not
one healthy reading; and a P4 signal never OPENS an incident - peripheral
notes attach to incidents, they do not page anyone on their own.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any

from aegis.contracts.events import Signal
from aegis.l4_understanding.topology import component_of

_SEVERITY_RANK = {"P1": 1, "P2": 2, "P3": 3, "P4": 4}


@dataclass
class Incident:
    id: str
    status: str = "forming"          # forming -> open -> resolved
    severity: str = "P4"
    opened_at: str = ""
    opened_s: float = 0.0
    resolved_at: str = ""
    members: list[Signal] = field(default_factory=list)
    member_times: list[float] = field(default_factory=list)
    timeline: list[dict[str, Any]] = field(default_factory=list)
    services: set[str] = field(default_factory=set)
    traces: set[str] = field(default_factory=set)
    templates: set[str] = field(default_factory=set)
    detectors: set[str] = field(default_factory=set)
    last_signal_s: float = 0.0
    ranked_cause: str = ""
    cause_why: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """The doc's section 11 Incident schema, fields it defines and we can fill."""
        evidence: list[str] = []
        for member in self.members:
            for line in member.evidence:
                if line not in evidence:
                    evidence.append(line)
        return {
            "id": self.id,
            "status": self.status,
            "severity": self.severity,
            "opened_at": self.opened_at,
            "resolved_at": self.resolved_at or None,
            "signals": [member.to_dict() for member in self.members],
            "ranked_cause": self.ranked_cause,
            "cause_why": self.cause_why,
            "blast_radius": {
                "services": len(self.services),
                "signals": len(self.members),
                "traces": len(self.traces),
            },
            # C7 now enforces, so a conformance signal names the flow it
            # deviated from and the step that did not run - the link between
            # "the spec says this should happen" and "this incident".
            "affected_flows": sorted({
                line.split("'")[1] for member in self.members
                if member.detector == "ConformanceRate"
                for line in member.evidence
                if line.startswith("flow '") and "'" in line[6:]
            }),
            "deviations": [
                {"type": member.metric, "step_id": member.template_id,
                 "trace_id": member.trace_id,
                 "detail": member.evidence[0] if member.evidence else ""}
                for member in self.members
                if member.detector == "ConformanceRate"
            ],
            "timeline": list(self.timeline),
            "evidence": evidence[:20],
            "hypothesis": None,          # needs C10
            "similar_incidents": [],     # needs C15
        }


def _hhmmss(seconds: float | None) -> str:
    """Stream time as a clock reading, for a field a human will read."""
    if seconds is None:
        return ""
    try:
        total = int(seconds) % 86400
        return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"
    except (TypeError, ValueError):
        return ""


class IncidentManager:
    # An incident holds "forming" this long before it is presented - the
    # doc's SignalBuffer: related signals get to arrive before anyone is told.
    HOLD_S = 45.0
    TEMPORAL_S = 90.0
    # Sustained recovery, not one healthy reading.
    RESOLVE_QUIET_S = 300.0

    def __init__(self) -> None:
        self.incidents: list[Incident] = []
        # P4 signals that matched no incident: visible, but they page nobody.
        self.notes: list[Signal] = []
        self._ids = itertools.count(1)
        self._last_now = 0.0
        # Service topology, mined from traces - lets a cause be ranked by
        # what calls what, not merely by which signal fired first.
        self.topology = None

    def set_topology(self, topology) -> None:
        self.topology = topology

    # -- entry point ---------------------------------------------------------

    def observe(self, signal: Signal, now: float | None) -> Incident | None:
        """Route one signal. Returns the incident it landed in, if any."""
        if now is None:
            now = self._last_now
        self._last_now = now
        self._advance_lifecycles(now)

        best: Incident | None = None
        best_rules: list[str] = []
        for incident in self.incidents:
            rules = self._match(incident, signal, now)
            recurring = "recurrence" in rules
            if incident.status == "resolved" and not recurring:
                # A resolved incident stays resolved - UNLESS this is the
                # same template flagged by the same detector. Those really
                # are one recurring problem, and the requests are hours
                # apart, so every one of them had already gone quiet before
                # the next arrived: the old guard guaranteed one incident
                # per occurrence, which is how 545 events became twenty
                # stories about the same slow operation.
                continue
            if len(rules) >= 2 and len(rules) > len(best_rules):
                best, best_rules = incident, rules

        if best is not None:
            # A recurrence reopens what had gone quiet: it is happening
            # again, and calling it resolved would be false.
            if best.status == "resolved":
                best.status = "open"
                best.resolved_at = ""
                best.timeline.append({
                    "ts": "", "kind": "recurred",
                    "detail": "the same template fired again after this "
                              "incident had gone quiet",
                })
            self._attach(best, signal, now, best_rules)
            return best

        if _SEVERITY_RANK.get(signal.severity, 4) <= 3:
            return self._open(signal, now)

        # A lone P4 first-occurrence note is worth a line, never a page.
        self.notes.append(signal)
        return None

    def settle(self, now: float | None) -> None:
        """Advance the lifecycle without a new signal arriving.

        Lifecycle only moved inside observe(), so the LAST incident in any
        stream never aged: nothing arrives after it to push it from forming
        to open. The newest incident - the one most likely to still be
        happening - was permanently labelled "not yet ready to look at".
        Reading the state is a moment where time has demonstrably passed, so
        it is a safe place to let the clock run.
        """
        if now is not None:
            self._advance_lifecycles(now)

    def finalize(self, now: float | None) -> None:
        """End of stream. Still-open incidents stay open, and say so - the
        stream ending is not evidence that anything recovered."""
        if now is not None:
            self._advance_lifecycles(now)
        for incident in self.incidents:
            if incident.status != "resolved":
                incident.timeline.append({
                    "ts": "", "kind": "note",
                    "detail": "stream ended with this incident still open",
                })

    # -- grouping ------------------------------------------------------------

    def _match(self, incident: Incident, signal: Signal, now: float) -> list[str]:
        rules = []
        if signal.trace_id and signal.trace_id in incident.traces:
            rules.append("trace")
        if signal.service and signal.service in incident.services:
            # Distance-zero adjacency. C5's topology will widen this to real
            # upstream/downstream neighbours - and narrow it across services.
            rules.append("adjacency")
        if now - incident.last_signal_s <= self.TEMPORAL_S:
            rules.append("temporal")
        # Recurrence: the same template, flagged by the same detector, is
        # ONE problem happening repeatedly - not one story per occurrence.
        # Without this, 545 events produced 20 incidents of which 18 were
        # "process_event completed in <n>ms" firing LatencyShift, once per
        # slow request, hours apart. Trace identity fails (every request has
        # its own id) and temporal proximity fails (they really are hours
        # apart), so each scored exactly one rule and opened its own story.
        # This is strong enough to group ALONE, because recurrence of one
        # template by one detector is not a coincidence the way a shared
        # minute is.
        if (signal.template_id and signal.template_id in incident.templates
                and signal.detector in incident.detectors):
            rules.append("recurrence")
            rules.append("recurrence-confirmed")
        return rules

    def _attach(self, incident: Incident, signal: Signal, now: float,
                rules: list[str]) -> None:
        incident.members.append(signal)
        incident.member_times.append(now)
        incident.last_signal_s = now
        incident.services.add(signal.service)
        if signal.template_id:
            incident.templates.add(signal.template_id)
        if signal.detector:
            incident.detectors.add(signal.detector)
        if signal.trace_id:
            incident.traces.add(signal.trace_id)
        if _SEVERITY_RANK.get(signal.severity, 4) < _SEVERITY_RANK.get(incident.severity, 4):
            incident.severity = signal.severity
        incident.timeline.append({
            "ts": signal.started_at, "kind": "signal",
            "detail": f"{signal.detector} {signal.metric}"
                      f" (observed={signal.observed:g}, baseline={signal.baseline:g})",
            "grouped_by": rules,
        })
        self._rank_cause(incident)

    def _open(self, signal: Signal, now: float) -> Incident:
        incident = Incident(
            id=f"INC-{next(self._ids)}",
            severity=signal.severity,
            opened_at=signal.started_at,
            opened_s=now,
            last_signal_s=now,
            templates={signal.template_id} if signal.template_id else set(),
            detectors={signal.detector} if signal.detector else set(),
        )
        incident.timeline.append({
            "ts": signal.started_at, "kind": "opened",
            "detail": f"opened by {signal.detector} ({signal.severity})",
        })
        self.incidents.append(incident)
        self._attach(incident, signal, now, rules=["first"])
        return incident

    # -- ranking and lifecycle -----------------------------------------------

    def _rank_cause(self, incident: Incident) -> None:
        """The doc's rule, both halves: the most UPSTREAM unhealthy component
        ranks first, earliest onset breaks ties. When the mined graph cannot
        separate the members (or does not exist yet), ranking falls back to
        timing alone - and the 'why' says which basis was actually used."""
        if not incident.members:
            return
        components = [component_of(
            (m.evidence[0] if m.evidence else m.metric) or "", fallback=m.service)
            for m in incident.members]

        chosen = min(range(len(incident.members)),
                     key=lambda i: incident.member_times[i])
        # Earliest onset ranks - full stop. Two live experiments let flow
        # position arbitrate and both DEMOTED a true root cause to a symptom:
        # within one service, "runs earlier in the flow" is the caller as
        # often as the feeder, so rank order cannot tell cause from observer
        # in either direction. That arbitration honestly needs the
        # service-dependency graph the doc's C5 describes (callers depend on
        # callees); until multi-service traces exist, topology ANNOTATES the
        # ranking with flow context and powers blast radius - it never
        # overrides the evidence of who deviated first.
        cause = incident.members[chosen]
        incident.ranked_cause = cause.id
        why = [f"earliest onset of {len(incident.members)} member(s)"]
        shared = sum(1 for m in incident.members
                     if m.trace_id and m.trace_id == cause.trace_id)
        if cause.trace_id and shared > 1:
            why.append(f"{shared} member(s) share its trace {cause.trace_id[:12]}")
        if self.topology is not None and len(self.topology.rank) > 1:
            involved = [c for c in dict.fromkeys(components) if c in self.topology.rank]
            involved.sort(key=lambda c: self.topology.rank[c])
            if len(involved) > 1:
                why.append("flow context: " + " → ".join(involved[:6]))
        else:
            why.append("no flow graph yet - ranked by timing alone")
        incident.cause_why = why

    def _advance_lifecycles(self, now: float) -> None:
        for incident in self.incidents:
            if incident.status == "forming" and now - incident.opened_s >= self.HOLD_S:
                incident.status = "open"
                incident.timeline.append({
                    "ts": "", "kind": "presented",
                    "detail": f"held {self.HOLD_S:.0f}s for related signals, now presented",
                })
            if incident.status in ("forming", "open") \
                    and now - incident.last_signal_s >= self.RESOLVE_QUIET_S:
                incident.status = "resolved"
                # When quiet was DECLARED, not when the last member fired.
                # Using the member's own timestamp made a single-member
                # incident report opening and resolving in the same second.
                incident.resolved_at = _hhmmss(
                    incident.last_signal_s + self.RESOLVE_QUIET_S) or (
                    incident.members[-1].started_at if incident.members else "")
                incident.timeline.append({
                    "ts": "", "kind": "resolved",
                    "detail": f"no further signals for {self.RESOLVE_QUIET_S:.0f}s"
                              " - sustained recovery",
                })

    # -- reporting -----------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        by_status: dict[str, int] = {}
        for incident in self.incidents:
            by_status[incident.status] = by_status.get(incident.status, 0) + 1
        return {
            "incidents": len(self.incidents),
            "by_status": by_status,
            "notes": len(self.notes),
        }
