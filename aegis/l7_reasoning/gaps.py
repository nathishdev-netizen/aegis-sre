"""Gap reports - what should this project log next?

The research behind the v2 plan is blunt: 26.9% of root-cause failures were
caused by evidence that was NEVER CAPTURED. Reasoning harder over what exists
is worth less than noticing what is missing. So when the platform cannot
answer something, it says exactly what logging would have let it - and each
suggestion feeds the log-instrumentation skill, which already knows how to
add lines additively.

Everything here is deterministic: a gap is an observed inability, not an
opinion, and each one carries the measurement that proves it.
"""

from __future__ import annotations

from typing import Any


def _gap(kind: str, what: str, why: str, measured: str) -> dict[str, str]:
    return {"kind": kind, "what_to_add": what, "why": why, "measured": measured}


class GapReporter:
    """Reads the platform's own outputs and reports what blocked them."""

    # Below this correlation coverage, per-request reasoning degrades to
    # guessing about which lines belong together.
    MIN_CORRELATION = 0.5
    # A component with this many signals but no measurable durations cannot
    # be judged slow or fast, only present or absent.
    MIN_SIGNALS_FOR_TIMING = 3

    def report(self, *, stats: dict[str, Any],
               verdicts: list[dict[str, Any]],
               spec: dict[str, Any],
               incidents: list[dict[str, Any]],
               timed_components: set[str] | None = None,
               sees_cost: bool = True,
               looks_like_llm: bool = False) -> list[dict[str, str]]:
        gaps: list[dict[str, str]] = []
        gaps += self._cost_gap(stats, sees_cost, looks_like_llm)
        gaps += self._correlation_gap(stats)
        gaps += self._purpose_gap(verdicts, spec)
        gaps += self._grounding_gaps(incidents)
        gaps += self._timing_gaps(incidents, timed_components or set())
        return gaps

    def _correlation_gap(self, stats: dict[str, Any]) -> list[dict[str, str]]:
        linked = stats.get("extracted", 0) + stats.get("inferred", 0)
        total = max(1, stats.get("events", 0))
        coverage = linked / total
        # Startup/idle events legitimately belong to no request; only flag
        # when even the linked share is poor AND real traffic exists.
        if total < 50 or coverage >= self.MIN_CORRELATION:
            return []
        return [_gap(
            "correlation",
            "a request/trace id (request_id=... or trace_id=...) on every "
            "log line inside request handling",
            "lines that name their request can be joined into one story; "
            "without the id, correlation degrades from a join to a guess",
            f"only {coverage:.0%} of {total} events could be tied to a request",
        )]

    def _purpose_gap(self, verdicts: list[dict[str, Any]],
                     spec: dict[str, Any]) -> list[dict[str, str]]:
        unknown = sum(1 for v in verdicts if v.get("verdict") == "unknown")
        if not verdicts or unknown < len(verdicts):
            return []
        if spec.get("purpose_marked"):
            return []
        return [_gap(
            "outcome",
            "one explicit line when a run ACHIEVES its purpose - e.g. "
            "'ORDER COMPLETED order=... total=...' - distinct from the line "
            "that says it merely finished",
            "'finished' and 'achieved something' are different facts; without "
            "an outcome line, hollow runs (clean teardown, nothing done) are "
            "indistinguishable from good ones",
            f"all {len(verdicts)} completed runs came back verdict=unknown",
        )]

    def _grounding_gaps(self, incidents: list[dict[str, Any]]) -> list[dict[str, str]]:
        gaps = []
        for incident in incidents:
            hypothesis = incident.get("hypothesis")
            if not hypothesis or hypothesis.get("verified", True):
                continue
            cause = (incident.get("evidence") or ["the failing step"])[0]
            gaps.append(_gap(
                "explainability",
                f"the response body / error detail at the failure behind: "
                f"{cause[:80]!r}",
                "the model's explanation could not be grounded in any logged "
                "line - the WHY of this failure is not in the logs at all",
                f"{incident.get('id')}: every citation the model offered was "
                "fabricated and had to be dropped",
            ))
        return gaps

    def _cost_gap(self, stats: dict[str, Any], sees_cost: bool,
                  looks_like_llm: bool) -> list[dict[str, str]]:
        """An LLM app that never logs token usage cannot be watched for the
        failure that only shows up on the bill."""
        if sees_cost or not looks_like_llm:
            return []
        return [_gap(
            "cost",
            "token usage on every model call - "
            "'LLM usage node=generate total_tokens=1510'",
            "a prompt that silently doubles, or a retry loop re-asking the "
            "model, costs more for the same work and errors nowhere; without "
            "a token count it is invisible until the invoice",
            f"model calls were observed in {stats.get('events', 0)} events, "
            "and not one carried a token count",
        )]

    def _cost_gap(self, stats: dict[str, Any], sees_cost: bool,
                  looks_like_llm: bool) -> list[dict[str, str]]:
        """An LLM app that never logs token usage cannot be watched for the
        failure that only shows up on the bill."""
        if sees_cost or not looks_like_llm:
            return []
        return [_gap(
            "cost",
            "token usage on every model call - "
            "'LLM usage node=generate total_tokens=1510'",
            "a prompt that silently doubles, or a retry loop re-asking the "
            "model, costs more for the same work and errors nowhere; without "
            "a token count it is invisible until the invoice",
            f"model calls were observed in {stats.get('events', 0)} events, "
            "and not one carried a token count",
        )]

    def _cost_gap(self, stats: dict[str, Any], sees_cost: bool,
                  looks_like_llm: bool) -> list[dict[str, str]]:
        """An LLM app that never logs token usage cannot be watched for the
        failure that only shows up on the bill."""
        if sees_cost or not looks_like_llm:
            return []
        return [_gap(
            "cost",
            "token usage on every model call - "
            "'LLM usage node=generate total_tokens=1510'",
            "a prompt that silently doubles, or a retry loop re-asking the "
            "model, costs more for the same work and errors nowhere; without "
            "a token count it is invisible until the invoice",
            f"model calls were observed in {stats.get('events', 0)} events, "
            "and not one carried a token count",
        )]

    def _cost_gap(self, stats: dict[str, Any], sees_cost: bool,
                  looks_like_llm: bool) -> list[dict[str, str]]:
        """An LLM app that never logs token usage cannot be watched for the
        failure that only shows up on the bill."""
        if sees_cost or not looks_like_llm:
            return []
        return [_gap(
            "cost",
            "token usage on every model call - "
            "'LLM usage node=generate total_tokens=1510'",
            "a prompt that silently doubles, or a retry loop re-asking the "
            "model, costs more for the same work and errors nowhere; without "
            "a token count it is invisible until the invoice",
            f"model calls were observed in {stats.get('events', 0)} events, "
            "and not one carried a token count",
        )]

    def _timing_gaps(self, incidents: list[dict[str, Any]],
                     timed: set[str]) -> list[dict[str, str]]:
        gaps = []
        seen: set[str] = set()
        for incident in incidents:
            for signal in incident.get("signals", []):
                service = signal.get("service", "")
                if not service or service in timed or service in seen:
                    continue
                if sum(1 for s in incident.get("signals", [])
                       if s.get("service") == service) >= self.MIN_SIGNALS_FOR_TIMING:
                    seen.add(service)
                    gaps.append(_gap(
                        "timing",
                        f"durations on {service}'s operations "
                        "('done in 123ms', or durationMs=123)",
                        "without measured durations this component can only be "
                        "judged present or absent, never slow - and slow is "
                        "how it will actually fail",
                        f"{service} produced signals in {incident.get('id')} "
                        "but not one measurable duration",
                    ))
        return gaps
