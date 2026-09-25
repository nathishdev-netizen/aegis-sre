"""Regression tests - did the fix actually hold?

The loop stopped one step short: Aegis found a problem, diagnosed it,
drafted a fix, the user applied it by hand, and nothing ever checked whether
the thing got better. The outcome label records what the user BELIEVED; this
records what the measurements did, which is a different fact and sometimes a
contradicting one.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aegis.l6_correlation.verify import (  # noqa: E402
    MIN_SAMPLES, snapshot_operations, verify)


def _ops(median, p95, count, name="process_event completed"):
    return [{"operation": name, "median_ms": median, "p95_ms": p95,
             "count": count}]


def test_a_fix_that_removed_the_hangs_is_reported_as_held():
    """The real shape: a missing timeout produced rare 260-second hangs.
    Adding one moves the TAIL and can leave the median almost untouched."""
    before = snapshot_operations(_ops(1746.0, 260838.0, 200))
    result = verify(before, _ops(1700.0, 8000.0, 260))
    row = result["operations"][0]
    assert row["verdict"] == "held", row
    assert "p95" in row["detail"]


def test_a_fix_that_changed_nothing_says_so():
    """Silence is not success. An unchanged measurement must not be
    reported as a fix that worked."""
    before = snapshot_operations(_ops(1746.0, 260838.0, 200))
    result = verify(before, _ops(1740.0, 259000.0, 260))
    assert result["operations"][0]["verdict"] == "unchanged"


def test_a_fix_that_made_it_worse_is_not_hidden():
    before = snapshot_operations(_ops(1000.0, 5000.0, 200))
    result = verify(before, _ops(1800.0, 9000.0, 260))
    row = result["operations"][0]
    assert row["verdict"] == "worse"
    assert "SLOWER" in row["detail"]


def test_a_verdict_is_refused_until_there_are_new_runs():
    """A fix marked while the system was quiet would compare idle traffic
    against busy traffic. Both sides need samples."""
    before = snapshot_operations(_ops(1746.0, 260838.0, 200))
    result = verify(before, _ops(900.0, 9000.0, 202))   # only 2 new runs
    row = result["operations"][0]
    assert row["verdict"] == "too-early"
    assert str(MIN_SAMPLES) in row["detail"]


def test_an_operation_that_stopped_running_is_reported_not_guessed():
    before = snapshot_operations(_ops(1746.0, 260838.0, 200))
    result = verify(before, _ops(500.0, 900.0, 50, name="something else"))
    verdicts = {r["operation"]: r["verdict"] for r in result["operations"]}
    assert verdicts["process_event completed"] == "gone"


def test_nothing_recorded_means_nothing_claimed():
    assert verify(None, _ops(1.0, 2.0, 99))["ok"] is False


def test_the_worst_news_is_listed_first():
    """A reader scanning one line must not find "held" above "worse"."""
    before = snapshot_operations([
        {"operation": "a", "median_ms": 100.0, "p95_ms": 200.0, "count": 10},
        {"operation": "b", "median_ms": 100.0, "p95_ms": 200.0, "count": 10},
    ])
    result = verify(before, [
        {"operation": "a", "median_ms": 20.0, "p95_ms": 40.0, "count": 40},
        {"operation": "b", "median_ms": 400.0, "p95_ms": 800.0, "count": 40},
    ])
    assert result["operations"][0]["verdict"] == "worse"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except AssertionError as exc:
                failures += 1
                print(f"  FAIL  {name}: {exc}")
    print(f"\n{'FAILED' if failures else 'All verification tests passed'}")
    sys.exit(1 if failures else 0)
