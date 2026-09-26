"""Rollups of what happened over a time window - the last 24 hours, week, month.

Verdicts and baselines answer "is THIS run/operation normal". Nobody asked yet
whether the last 24 hours as a whole were fine - that number does not exist until
something adds up the runs already sitting in the store. Deliberately no LLM call:
every figure here is a count or a ratio over rows Store already has, so a report
never costs a model call and never invents a number it did not measure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.baselines import DRIFT_RATIO, MIN_SAMPLES_PER_HALF
from app.store import Store

# Named windows a report can be asked for, in hours. "24h"/"week"/"month" are the
# vocabulary the rest of the app (and the person reading a report) actually uses;
# an arbitrary hour count is also accepted for anything finer-grained.
WINDOWS = {"24h": 24, "week": 24 * 7, "month": 24 * 30}


@dataclass
class AuditReport:
    window: str
    since: str
    until: str
    total_runs: int
    verdict_counts: dict[str, int] = field(default_factory=dict)
    notable_runs: list[dict[str, Any]] = field(default_factory=list)
    drifts: list[dict[str, Any]] = field(default_factory=list)
    spikes: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "window": self.window,
            "since": self.since,
            "until": self.until,
            "total_runs": self.total_runs,
            "verdict_counts": self.verdict_counts,
            "notable_runs": self.notable_runs,
            "drifts": self.drifts,
            "spikes": self.spikes,
            "summary": self.summary,
        }


def _window_hours(window: str) -> float:
    if window in WINDOWS:
        return float(WINDOWS[window])
    try:
        hours = float(window)
    except (TypeError, ValueError):
        return float(WINDOWS["24h"])
    return hours if hours > 0 else float(WINDOWS["24h"])


# A run with no verdict at all (never reached a completion line, or a store write
# failed) is real history but answers a different question than "how did runs
# turn out" - counted in total_runs, excluded from verdict_counts and notability.
_NOTABLE_VERDICTS = {"hollow", "failed", "degraded"}


def _spikes_in_window(durations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Re-derive spikes from the window's own measurements, oldest-first per
    operation, using the same rule Baselines.observe() applies live: a value at
    least SPIKE_RATIO times the median of what came before it in this window.

    Recomputed rather than read from the live findings list, which only keeps the
    last 20 - a month-long report needs the window's own history, not whatever
    happened to still be in memory.
    """
    from app.core.baselines import SPIKE_RATIO
    import statistics

    by_key: dict[tuple[str, str], list[float]] = {}
    for row in durations:
        key = (row["component"], row["operation"])
        by_key.setdefault(key, []).append(row["value_ms"])

    found = []
    for (component, operation), values in by_key.items():
        if len(values) < MIN_SAMPLES_PER_HALF:
            continue
        worst = None
        for i in range(MIN_SAMPLES_PER_HALF, len(values)):
            history = values[:i]
            median = statistics.median(history)
            if median > 0 and values[i] >= median * SPIKE_RATIO:
                ratio = values[i] / median
                # One sustained level shift (a drift, reported separately) would
                # otherwise qualify on every value after the shift - keep only the
                # single worst instance per operation, not one entry per repeat.
                if worst is None or ratio > worst["ratio"]:
                    worst = {
                        "component": component,
                        "operation": operation,
                        "value_ms": round(values[i], 1),
                        "median_ms": round(median, 1),
                        "ratio": round(ratio, 1),
                    }
        if worst:
            found.append(worst)
    # Worst ratio first - the one most worth a human's attention.
    found.sort(key=lambda f: f["ratio"], reverse=True)
    return found[:10]


def _drifts_in_window(durations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Same split-in-half comparison as Baseline.drift(), applied to just this
    window's own samples - a trend the window itself contains, not one blended
    in from months of history outside it."""
    import statistics

    by_key: dict[tuple[str, str], list[float]] = {}
    for row in durations:
        key = (row["component"], row["operation"])
        by_key.setdefault(key, []).append(row["value_ms"])

    found = []
    for (component, operation), values in by_key.items():
        half = len(values) // 2
        if half < MIN_SAMPLES_PER_HALF:
            continue
        earlier = statistics.median(values[:half])
        recent = statistics.median(values[half:])
        if earlier <= 0:
            continue
        ratio = recent / earlier
        if DRIFT_RATIO > ratio > 1 / DRIFT_RATIO:
            continue
        found.append({
            "component": component,
            "operation": operation,
            "earlier_median_ms": round(earlier, 1),
            "recent_median_ms": round(recent, 1),
            "ratio": round(ratio, 1),
            "samples": len(values),
        })
    found.sort(key=lambda f: abs(f["ratio"] - 1), reverse=True)
    return found[:10]


def _summarize(report: AuditReport) -> str:
    """One or two plain sentences - no model, so this never costs a call and
    never says more than the counts actually support."""
    extras = []
    if report.drifts:
        extras.append(f"{len(report.drifts)} operation(s) trending "
                       f"slower or faster than earlier in the window")
    if report.spikes:
        extras.append(f"{len(report.spikes)} spike(s) against the window's own history")
    extra_sentence = (" and ".join(extras).capitalize() + ".") if extras else ""

    if report.total_runs == 0:
        # Duration measurements can exist with no completed run behind them yet
        # (a run still in flight) - drift/spikes are still real findings even then.
        base = f"No runs recorded in the last {report.window}."
        return base + (" " + extra_sentence if extra_sentence else "")

    parts = [f"{report.total_runs} run(s) in the last {report.window}"]
    counted = sum(report.verdict_counts.values())
    if counted:
        breakdown = ", ".join(
            f"{n} {v}" for v, n in sorted(report.verdict_counts.items(),
                                           key=lambda kv: -kv[1])
        )
        parts.append(breakdown)
    concerning = sum(report.verdict_counts.get(v, 0) for v in _NOTABLE_VERDICTS)
    if concerning:
        parts.append(f"{concerning} worth a look")
    sentence = parts[0] + (" - " + parts[1] + "." if len(parts) > 1 else ".")
    if len(parts) > 2:
        sentence = sentence[:-1] + f" ({parts[2]})."

    if extra_sentence:
        sentence += " " + extra_sentence
    return sentence


def generate(store: Store, source: str, window: str = "24h") -> AuditReport:
    """Build a report for one source over one named window.

    Every figure comes from rows already in the store - nothing here calls a
    model. A report costs nothing to generate and can be requested as often as
    wanted, including from an automatic scheduled job.
    """
    hours = _window_hours(window)
    until_dt = datetime.now(timezone.utc)
    since_dt = until_dt - timedelta(hours=hours)
    since_iso = since_dt.isoformat(timespec="seconds")
    until_iso = until_dt.isoformat(timespec="seconds")

    runs = store.runs_since(source, since_iso)
    durations = store.durations_since(source, since_iso)

    verdict_counts: dict[str, int] = {}
    notable_runs = []
    for run in runs:
        verdict = run.get("verdict")
        if not verdict:
            continue
        verdict_counts[verdict] = verdict_counts.get(verdict, 0) + 1
        if verdict in _NOTABLE_VERDICTS:
            notable_runs.append({
                "trace_id": str(run["id"]),
                "verdict": verdict,
                "reason": run.get("reason") or "",
                "opened_at": run.get("started_at") or "",
                "events": run.get("events") or 0,
            })
    # Most recent first - a report is read for what to look at now.
    notable_runs.sort(key=lambda r: r["trace_id"], reverse=True)

    report = AuditReport(
        window=window,
        since=since_iso,
        until=until_iso,
        total_runs=len(runs),
        verdict_counts=verdict_counts,
        notable_runs=notable_runs[:20],
        drifts=_drifts_in_window(durations),
        spikes=_spikes_in_window(durations),
    )
    report.summary = _summarize(report)
    return report
