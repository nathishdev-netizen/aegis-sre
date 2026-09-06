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
            "affected_flows": [],        # needs C7
            "timeline": list(self.timeline),
            "evidence": evidence[:20],
            "hypothesis": None,          # needs C10
            "similar_incidents": [],     # needs C15
        }


class IncidentManager:
    # An incident holds "forming" this long before it is presented - the
    # doc's SignalBuffer: related signals get to arrive before anyone is told.
    HOLD_S = 45.0
    TEMPORAL_S = 90.0
    # Sustained recovery, not one healthy reading.
    RESOLVE_QUIET_S = 300.0

    def __init__(self) -> None:
        self.incidents: list[Incident] = []
        # Called with the incident dict when one resolves - how memory (C15)
        # learns without this layer knowing memory exists. A callback failure
        # must never break incident handling.
        self.on_resolve = None
        # P4 signals that matched no incident: visible, but they page nobody.
        self.notes: list[Signal] = []
        self._ids = itertools.count(1)
        self._last_now = 0.0

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
            if incident.status == "resolved":
                # A resolved incident stays resolved; recurrence is a NEW
                # incident that will point back via similarity (C15, later).
                continue
            rules = self._match(incident, signal, now)
            if len(rules) >= 2 and len(rules) > len(best_rules):
                best, best_rules = incident, rules

        if best is not None:
            self._attach(best, signal, now, best_rules)
            return best

        if _SEVERITY_RANK.get(signal.severity, 4) <= 3:
            return self._open(signal, now)

        # A lone P4 first-occurrence note is worth a line, never a page.
        self.notes.append(signal)
        return None

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
        return rules

    def _attach(self, incident: Incident, signal: Signal, now: float,
                rules: list[str]) -> None:
        incident.members.append(signal)
        incident.member_times.append(now)
        incident.last_signal_s = now
        incident.services.add(signal.service)
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
        """Earliest onset ranks first - report the earliest deviation, not the
        loudest error (P4 of the doc). With no topology yet, onset order is
        the whole basis, and the 'why' says so rather than implying more."""
        if not incident.members:
            return
        earliest_index = min(range(len(incident.members)),
                             key=lambda i: incident.member_times[i])
        cause = incident.members[earliest_index]
        incident.ranked_cause = cause.id
        why = [f"earliest onset of {len(incident.members)} member(s)"]
        if len(incident.member_times) > 1:
            gap = sorted(incident.member_times)[1] - incident.member_times[earliest_index]
            why.append(f"preceded the next signal by {gap:.0f}s")
        shared = sum(1 for m in incident.members
                     if m.trace_id and m.trace_id == cause.trace_id)
        if cause.trace_id and shared > 1:
            why.append(f"{shared} member(s) share its trace {cause.trace_id[:12]}")
        why.append("no topology yet - ranking is by timing alone (C5 will sharpen this)")
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
                incident.resolved_at = incident.members[-1].started_at if incident.members else ""
                incident.timeline.append({
                    "ts": "", "kind": "resolved",
                    "detail": f"no further signals for {self.RESOLVE_QUIET_S:.0f}s"
                              " - sustained recovery",
                })
                if self.on_resolve is not None:
                    try:
                        self.on_resolve(incident.to_dict())
                    except Exception:
                        pass

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
