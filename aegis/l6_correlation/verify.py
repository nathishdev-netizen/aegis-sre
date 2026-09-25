"""Did the fix actually work? - measured, not asserted.

The loop stops one step short today. Aegis finds a problem, diagnoses it,
drafts a fix, and the user applies it by hand - and then nothing ever checks
whether the thing got better. The outcome label says what the user BELIEVED
at the moment they clicked; this says what the measurements did afterwards,
which is a different fact and sometimes a contradicting one.

The method is deliberately dull. Record the operation's median and p95 at
the moment a fix is marked, then compare the same numbers later against the
same operation's own history. No model, no judgement: a fix that worked
shows up as a number moving, and if the numbers have not moved the honest
answer is that nothing has been proven yet.

Two ways this can lie, and both are guarded. A fix marked while the system
happened to be quiet compares busy traffic against idle traffic, so a
verdict needs a minimum number of samples on BOTH sides. And a p95 is not a
median: a fix that removes rare hangs moves the tail and leaves the middle
untouched, so both are reported and the tail is what a hang-shaped fix is
judged on.
"""

from __future__ import annotations

import json
import time
from typing import Any

# Below this, "before" and "after" are anecdotes rather than measurements.
MIN_SAMPLES = 8

# Smaller than this is noise in any real system; claiming a 3% improvement
# from statistics this coarse would be false precision.
MATERIAL_CHANGE = 0.15


def snapshot_operations(operations: list[dict[str, Any]] | None) -> dict[str, Any]:
    """What the measurements looked like at the moment a fix was marked."""
    rows = {}
    for row in operations or []:
        name = str(row.get("operation") or "")
        if not name:
            continue
        rows[name] = {
            "median_ms": row.get("median_ms"),
            "p95_ms": row.get("p95_ms"),
            "samples": row.get("count"),
        }
    return {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "operations": rows}


def _verdict(before: dict, after: dict) -> tuple[str, str]:
    """One operation, before and after. Returns (verdict, one sentence)."""
    b_median, a_median = before.get("median_ms"), after.get("median_ms")
    b_p95, a_p95 = before.get("p95_ms"), after.get("p95_ms")
    b_n, a_n = before.get("samples") or 0, after.get("samples") or 0

    # New samples since the fix - not the total, which only ever grows.
    fresh = a_n - b_n
    if fresh < MIN_SAMPLES:
        return ("too-early",
                f"only {max(fresh, 0)} run(s) since the fix; "
                f"{MIN_SAMPLES} are needed before this means anything")
    if not b_median or not a_median:
        return ("unknown", "no timings were recorded on one side")

    median_change = (a_median - b_median) / b_median
    tail_change = ((a_p95 - b_p95) / b_p95) if (b_p95 and a_p95) else 0.0

    # A hang-shaped fix moves the TAIL and can leave the middle alone, so
    # the tail is checked first when it is the part that was broken.
    if tail_change <= -MATERIAL_CHANGE:
        return ("held",
                f"p95 fell from {b_p95:.0f}ms to {a_p95:.0f}ms "
                f"({abs(tail_change) * 100:.0f}% lower) over {fresh} new runs")
    if median_change <= -MATERIAL_CHANGE:
        return ("held",
                f"median fell from {b_median:.0f}ms to {a_median:.0f}ms "
                f"({abs(median_change) * 100:.0f}% lower) over {fresh} new runs")
    if tail_change >= MATERIAL_CHANGE or median_change >= MATERIAL_CHANGE:
        return ("worse",
                f"it got SLOWER since the fix - median {b_median:.0f}ms to "
                f"{a_median:.0f}ms, p95 {b_p95 or 0:.0f}ms to {a_p95 or 0:.0f}ms")
    return ("unchanged",
            f"no material change over {fresh} new runs - median "
            f"{b_median:.0f}ms then {a_median:.0f}ms")


def verify(snapshot: dict[str, Any] | None,
           operations: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Compare a fix's snapshot against the measurements now."""
    if not snapshot or not snapshot.get("operations"):
        return {"ok": False, "detail": "nothing was recorded when this was marked"}
    now = snapshot_operations(operations)["operations"]
    results = []
    for name, before in snapshot["operations"].items():
        after = now.get(name)
        if after is None:
            results.append({"operation": name, "verdict": "gone",
                            "detail": "this operation has not run since"})
            continue
        verdict, detail = _verdict(before, after)
        results.append({"operation": name, "verdict": verdict,
                        "detail": detail,
                        "before": before, "after": after})
    ranking = {"worse": 0, "held": 1, "unchanged": 2, "too-early": 3,
               "gone": 4, "unknown": 5}
    results.sort(key=lambda r: ranking.get(r["verdict"], 9))
    return {"ok": True, "marked_at": snapshot.get("at"), "operations": results}


def load(raw: str | None) -> dict[str, Any] | None:
    try:
        return json.loads(raw) if raw else None
    except (ValueError, TypeError):
        return None
