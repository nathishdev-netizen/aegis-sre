"""C7 - conformance: compare what happened against what should have happened,
including differences that produced no error at all.

This is the component that makes the platform different. Error-based
monitoring is structurally incapable of seeing a run that completed cleanly
and achieved nothing - there is no exception to catch, the hangup cause is
NORMAL, every dashboard is green. Conformance sees it because it holds the
flow spec: the run was supposed to contain its purpose step, and did not.

Each checked trace gets deviations (the doc's Deviation schema) and a VERDICT:

  achieved   contained its critical steps, no errors, normal duration
  failed     errored, and said so - the easy case every tool catches
  hollow     completed cleanly, but its critical step never ran
  degraded   achieved its purpose, but abnormally slowly
  unknown    the spec has no critical steps marked, so hollowness cannot
             be judged - said plainly rather than guessed

Doc rules kept: conformance observes, never blocks; optional steps produce no
noise; shadow mode is the default (conformance alerting from day one is in
the anti-pattern table - every valid branch looks like a deviation until the
spec has settled).
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any

from aegis.contracts.events import Event, Signal
from aegis.l4_understanding.flowspec import FlowSpec, _seconds

SLOW_FACTOR = 1.5


@dataclass
class TraceReport:
    trace_id: str
    flow: str
    verdict: str
    reason: str
    deviations: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    duration_s: float = 0.0


class ConformanceEngine:
    """mode="shadow" records everything and signals nothing - the default.
    mode="enforce" additionally emits a Signal per P2 deviation, which the
    incident manager will group by trace like any other signal."""

    def __init__(self, mode: str = "shadow") -> None:
        self.mode = mode
        self._ids = itertools.count(1)

    def check(self, trace_id: str, events: list[Event],
              spec: FlowSpec) -> tuple[TraceReport, list[Signal]]:
        present = {event.template_id for event in events}
        deviations: list[dict[str, Any]] = []
        evidence: list[str] = []

        errored = [e for e in events if e.level in ("ERROR", "CRITICAL")]

        for step in spec.steps:
            if step.template_id in present:
                continue
            if step.required and not step.critical:
                deviations.append(self._deviation(
                    trace_id, spec, "missing_step", step,
                    severity="P3", observed="step absent"))
            # Optional steps absent: silence. Tolerance is a requirement,
            # not a kindness - noise here is why conformance tools get muted.

        # Critical steps are judged as a PURPOSE FAMILY, not one by one. The
        # same logical step ("process the caller's request") often mines into
        # several template variants - different code paths word their line
        # differently - and demanding every variant condemned every real
        # conversation as hollow for missing the OTHER calls' variants. A run
        # is hollow when NONE of its purpose family ran.
        critical_steps = [s for s in spec.steps if s.critical]
        purpose_ran = any(s.template_id in present for s in critical_steps)
        if critical_steps and not purpose_ran:
            family = ", ".join(s.label[:36] for s in critical_steps[:3])
            deviations.append({
                "trace_id": trace_id, "flow": spec.name,
                "type": "purpose_never_ran", "step_id": "",
                "expected": f"one of the purpose steps ({family}...)",
                "observed": "no purpose step ran; no error raised",
                "severity": "P2",
            })

        duration = self._span(events)
        p95 = float(spec.expected_duration_s.get("p95") or 0.0)
        slow = bool(p95 and duration > p95 * SLOW_FACTOR)
        if slow:
            deviations.append({
                "trace_id": trace_id, "flow": spec.name, "type": "slow_flow",
                "step_id": "", "expected": f"<= {p95:.0f}s (p95)",
                "observed": f"{duration:.0f}s", "severity": "P3",
            })

        if errored:
            verdict, reason = "failed", (
                f"{len(errored)} error event(s) in the trace")
            evidence = [e.text_redacted.splitlines()[0][:160] for e in errored[:3]]
        elif not critical_steps:
            verdict, reason = "unknown", (
                "no critical steps are marked in the spec, so whether this run"
                " achieved anything cannot be judged")
        elif not purpose_ran:
            verdict = "hollow"
            reason = ("completed cleanly - no errors, normal teardown - but none"
                      " of its purpose steps ever ran")
            evidence = [events[0].text_redacted.splitlines()[0][:160],
                        events[-1].text_redacted.splitlines()[0][:160]]
        elif slow:
            verdict, reason = "degraded", (
                f"achieved its purpose, but took {duration:.0f}s against a"
                f" {p95:.0f}s p95")
            evidence = [events[-1].text_redacted.splitlines()[0][:160]]
        else:
            verdict, reason = "achieved", "purpose steps ran, no errors, normal duration"

        report = TraceReport(trace_id=trace_id, flow=spec.name, verdict=verdict,
                             reason=reason, deviations=deviations,
                             evidence=evidence, duration_s=round(duration, 1))

        signals: list[Signal] = []
        if self.mode == "enforce":
            for deviation in deviations:
                if deviation["severity"] == "P2":
                    signals.append(Signal(
                        id=f"CF-{next(self._ids)}",
                        detector="ConformanceRate",
                        service=events[0].service if events else "",
                        metric=deviation["type"],
                        observed=0.0, baseline=1.0, ratio=0.0,
                        started_at=events[-1].ts if events else "",
                        severity="P2", trace_id=trace_id,
                        template_id=deviation.get("step_id", ""),
                        evidence=([f"flow '{spec.name}': {deviation['expected']}"
                                   f" - observed: {deviation['observed']}"]
                                  + (report.evidence or [])),
                    ))
        return report, signals

    @staticmethod
    def _deviation(trace_id: str, spec: FlowSpec, kind: str, step,
                   severity: str, observed: str) -> dict[str, Any]:
        return {
            "trace_id": trace_id, "flow": spec.name, "type": kind,
            "step_id": step.template_id,
            "expected": f"step '{step.label[:40]}' (presence {step.presence:.0%})",
            "observed": observed, "severity": severity,
        }

    @staticmethod
    def _span(events: list[Event]) -> float:
        start = _seconds(events[0].ts) if events and events[0].ts else None
        end = _seconds(events[-1].ts) if events and events[-1].ts else None
        if start is None or end is None or end < start:
            return 0.0
        return end - start
