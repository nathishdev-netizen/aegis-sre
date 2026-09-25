"""Gap report tests - the platform saying what it could not see.

Each gap is an observed inability with its measurement attached, never an
opinion: 26.9% of RCA failures are evidence that was never captured, and this
is the loop that closes it.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aegis.l7_reasoning.gaps import GapReporter  # noqa: E402


def _report(**kw):
    defaults = dict(stats={"events": 100, "extracted": 60, "inferred": 20},
                    verdicts=[], spec={"purpose_marked": True}, incidents=[],
                    timed_components=set())
    defaults.update(kw)
    return GapReporter().report(**defaults)


def test_poor_correlation_is_reported_with_its_number():
    gaps = _report(stats={"events": 200, "extracted": 10, "inferred": 5})
    assert any(g["kind"] == "correlation" and "8%" in g["measured"] for g in gaps)


def test_good_correlation_reports_nothing():
    assert not [g for g in _report() if g["kind"] == "correlation"]


def test_tiny_files_do_not_trigger_correlation_advice():
    """Fifty startup lines with no ids is a boot, not a gap."""
    gaps = _report(stats={"events": 30, "extracted": 0, "inferred": 0})
    assert not [g for g in gaps if g["kind"] == "correlation"]


def test_all_unknown_verdicts_ask_for_an_outcome_line():
    gaps = _report(verdicts=[{"verdict": "unknown"}] * 4,
                   spec={"purpose_marked": False})
    outcome = [g for g in gaps if g["kind"] == "outcome"]
    assert outcome and "ACHIEVES" in outcome[0]["what_to_add"]
    assert "4 completed runs" in outcome[0]["measured"]


def test_marked_spec_means_unknowns_are_not_a_logging_gap():
    """If purpose steps ARE marked and verdicts are still unknown, the fix is
    elsewhere - advising more logging would be wrong advice."""
    gaps = _report(verdicts=[{"verdict": "unknown"}] * 4,
                   spec={"purpose_marked": True})
    assert not [g for g in gaps if g["kind"] == "outcome"]


def test_an_ungrounded_hypothesis_names_the_missing_evidence():
    incident = {"id": "INC-7",
                "evidence": ["[orch] /chat returned status=500"],
                "signals": [],
                "hypothesis": {"verified": False, "statement": "x"}}
    gaps = _report(incidents=[incident])
    explain = [g for g in gaps if g["kind"] == "explainability"]
    assert explain and "status=500" in explain[0]["what_to_add"]
    assert "INC-7" in explain[0]["measured"]


def test_a_grounded_hypothesis_is_not_a_gap():
    incident = {"id": "INC-8", "evidence": ["e"], "signals": [],
                "hypothesis": {"verified": True, "statement": "x"}}
    assert not [g for g in _report(incidents=[incident])
                if g["kind"] == "explainability"]


def test_untimed_noisy_components_are_asked_for_durations():
    incident = {"id": "INC-9", "evidence": [], "hypothesis": None,
                "signals": [{"service": "worker", "detector": "RateSpike"}] * 3}
    gaps = _report(incidents=[incident])
    timing = [g for g in gaps if g["kind"] == "timing"]
    assert timing and "worker" in timing[0]["what_to_add"]
    # already-timed components are left alone
    gaps2 = _report(incidents=[incident], timed_components={"worker"})
    assert not [g for g in gaps2 if g["kind"] == "timing"]


def test_every_gap_carries_its_measurement():
    gaps = _report(stats={"events": 200, "extracted": 0, "inferred": 0},
                   verdicts=[{"verdict": "unknown"}],
                   spec={"purpose_marked": False})
    assert gaps
    for gap in gaps:
        assert gap["measured"].strip(), "a gap without its measurement is an opinion"


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
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"  ERROR {name}: {exc.__class__.__name__}: {exc}")
    print(f"\n{'FAILED' if failures else 'All gap tests passed'}"
          f"{f' ({failures} failing)' if failures else ''}")
    sys.exit(1 if failures else 0)
