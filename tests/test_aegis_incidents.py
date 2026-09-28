"""Phase 4 regression tests - the C9 incident manager.

Grouping wrongly in either direction is the failure mode: two unrelated
problems in one incident misleads, one problem split across two pages twice.
"""

from __future__ import annotations

import itertools
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aegis.contracts.events import Signal  # noqa: E402
from aegis.l6_correlation.incidents import IncidentManager  # noqa: E402

_ids = itertools.count(1)


def _sig(detector: str = "NoveltyDetector", severity: str = "P2",
         service: str = "svc", trace: str = "", ts: str = "10:00:00",
         template: str = "") -> Signal:
    return Signal(id=f"S-{next(_ids)}", detector=detector, service=service,
                  metric="m", observed=1.0, baseline=0.0, ratio=0.0,
                  started_at=ts, severity=severity, trace_id=trace,
                  template_id=template,
                  evidence=["the line that proves it"])


def test_one_cause_many_symptoms_one_incident():
    """A single root cause produces symptoms in every downstream signal.
    The reference file's 18:22:55 cluster - a lookup failure plus four
    knock-on warnings in the same second - must be ONE incident."""
    manager = IncidentManager()
    manager.observe(_sig(trace="t-1", ts="18:22:55"), now=100.0)
    for offset in range(1, 5):
        manager.observe(_sig(trace="t-1", ts="18:22:56"), now=100.0 + offset)
    assert len(manager.incidents) == 1
    assert len(manager.incidents[0].members) == 5


def test_failures_far_apart_are_separate_incidents():
    manager = IncidentManager()
    manager.observe(_sig(ts="12:00:00"), now=0.0)
    manager.observe(_sig(ts="12:30:00"), now=1800.0)
    assert len(manager.incidents) == 2


def test_one_rule_alone_never_groups():
    """The doc: grouping requires two of the four rules. Same service alone
    (everything is the same service in a one-service world) must not glue
    every incident of the day together."""
    manager = IncidentManager()
    manager.observe(_sig(service="svc", ts="12:00:00"), now=0.0)
    # Same service, but 10 minutes later and a different trace.
    manager.observe(_sig(service="svc", trace="t-2", ts="12:10:00"), now=600.0)
    assert len(manager.incidents) == 2


def test_same_service_plus_same_minute_is_not_one_story():
    """The real failure: on a one-service chatbot, an Opik "continuing WITHOUT
    tracing" config warning and an unrelated ValueError crash 52s later became
    ONE incident - and because the benign warning came first it was ranked as
    the cause, so the crash the user was looking for was invisible behind it.

    Two rules matched, but neither was a FACT about these two signals:
    "adjacency" (same service) is true of every signal in a one-service
    project, and "temporal" only says they shared a minute. Nothing tied them
    together. test_one_rule_alone_never_groups already establishes this
    reasoning for a lone rule; the pair needs it too."""
    manager = IncidentManager()
    manager.observe(_sig(service="chatbot.app", trace="t-boot", ts="16:02:49"), now=0.0)
    manager.observe(_sig(service="chatbot.app", trace="t-req", ts="16:03:41"), now=52.0)
    assert len(manager.incidents) == 2, (
        "a config warning and an unrelated crash were merged into one story")


def test_a_shared_trace_still_groups_within_one_service():
    """The guard above must not cost real grouping: a trace id is a fact about
    these two signals, so one cause and its knock-on symptom stay ONE incident
    even though the service alone would not have justified it."""
    manager = IncidentManager()
    manager.observe(_sig(service="chatbot.app", trace="t-9", ts="16:03:41"), now=0.0)
    manager.observe(_sig(service="chatbot.app", trace="t-9", ts="16:03:41"), now=1.0)
    assert len(manager.incidents) == 1
    assert len(manager.incidents[0].members) == 2


def test_adjacency_still_counts_across_services():
    """Across services, "same service" is NOT vacuous - it is the thing being
    asserted - so adjacency+temporal remains valid evidence there. The guard is
    about a one-service world, not a weakening of the rule everywhere."""
    manager = IncidentManager()
    manager.observe(_sig(service="gateway", trace="t-1", ts="16:00:00"), now=0.0)
    manager.observe(_sig(service="worker", trace="t-2", ts="16:00:10"), now=10.0)
    # Different services, no shared trace: two rules never even match.
    assert len(manager.incidents) == 2
    # But once an incident spans two services, a third signal in one of them
    # groups on adjacency+temporal, because adjacency now says something.
    manager.incidents[0].services.add("worker")
    manager.observe(_sig(service="worker", trace="t-3", ts="16:00:20"), now=20.0)
    assert len(manager.incidents) == 2, "adjacency stopped counting across services"


def test_p4_notes_never_open_an_incident():
    """~97% of alerts need no immediate action. A lone first-occurrence note
    is worth a line, never a page."""
    manager = IncidentManager()
    manager.observe(_sig(severity="P4"), now=0.0)
    assert manager.incidents == []
    assert len(manager.notes) == 1


def test_p4_attaches_to_a_matching_open_incident():
    """Peripheral notes ARE context once something real is open - the doc's
    aggressive-grouping rule: when in doubt, group; humans can split."""
    manager = IncidentManager()
    manager.observe(_sig(severity="P2", trace="t-1"), now=0.0)
    manager.observe(_sig(severity="P4", trace="t-1", ts="10:00:10"), now=10.0)
    assert len(manager.incidents) == 1
    assert len(manager.incidents[0].members) == 2
    assert manager.notes == []


def test_severity_is_the_worst_members():
    manager = IncidentManager()
    manager.observe(_sig(severity="P3", trace="t-1"), now=0.0)
    manager.observe(_sig(severity="P2", trace="t-1"), now=5.0)
    assert manager.incidents[0].severity == "P2"


def test_cause_is_earliest_onset_and_says_why():
    """Report the earliest deviation, not the loudest error (P4 of the doc).
    And with no topology yet, the ranking must admit its basis is timing."""
    manager = IncidentManager()
    first = _sig(detector="LatencyShift", severity="P3", trace="t-1", ts="10:00:00")
    manager.observe(first, now=0.0)
    manager.observe(_sig(detector="RateSpike", severity="P2", trace="t-1",
                         ts="10:00:03"), now=3.0)
    incident = manager.incidents[0]
    assert incident.ranked_cause == first.id, "the louder P2 outranked the earlier P3"
    assert any("timing alone" in reason for reason in incident.cause_why)


def test_resolution_requires_sustained_quiet():
    manager = IncidentManager()
    manager.observe(_sig(trace="t-1"), now=0.0)
    manager.observe(_sig(severity="P4"), now=100.0)  # a note advances the clock
    assert manager.incidents[0].status != "resolved"
    manager.observe(_sig(severity="P4"), now=0.0 + manager.RESOLVE_QUIET_S + 1)
    assert manager.incidents[0].status == "resolved"


def test_a_resolved_incident_stays_resolved():
    """Recurrence is a NEW incident. Reopening a resolved one erases the fact
    that it recovered - and the timeline is the postmortem."""
    manager = IncidentManager()
    manager.observe(_sig(trace="t-1", ts="10:00:00"), now=0.0)
    manager.observe(_sig(severity="P4"), now=manager.RESOLVE_QUIET_S + 1)
    assert manager.incidents[0].status == "resolved"
    manager.observe(_sig(trace="t-1", ts="10:20:00"), now=manager.RESOLVE_QUIET_S + 10)
    assert len(manager.incidents) == 2
    assert manager.incidents[0].status == "resolved"


def test_timeline_records_the_grouping_rules():
    """Never silent about grouping decisions: always show what was grouped
    and why (the doc's C9 should-not list)."""
    manager = IncidentManager()
    manager.observe(_sig(trace="t-1"), now=0.0)
    manager.observe(_sig(trace="t-1", ts="10:00:05"), now=5.0)
    joined = [entry for entry in manager.incidents[0].timeline
              if entry.get("grouped_by") and "trace" in entry["grouped_by"]]
    assert joined, "the second member's grouping rules were not recorded"


def test_every_signal_is_accounted_for():
    """members + notes must equal signals in - a signal silently dropped is
    a symptom nobody will ever see."""
    manager = IncidentManager()
    for index in range(20):
        severity = "P4" if index % 3 == 0 else "P2"
        manager.observe(_sig(severity=severity, trace=f"t-{index // 5}",
                             ts="10:00:00"), now=float(index))
    placed = sum(len(incident.members) for incident in manager.incidents)
    assert placed + len(manager.notes) == 20


def test_stream_end_leaves_open_incidents_open():
    """The stream ending is not evidence anything recovered."""
    manager = IncidentManager()
    manager.observe(_sig(trace="t-1"), now=0.0)
    manager.finalize(now=10.0)
    incident = manager.incidents[0]
    assert incident.status != "resolved"
    assert any("still open" in entry["detail"] for entry in incident.timeline)


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
    print(f"\n{'FAILED' if failures else 'All incident tests passed'}"
          f"{f' ({failures} failing)' if failures else ''}")
    sys.exit(1 if failures else 0)
