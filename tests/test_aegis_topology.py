"""C5-lite topology + the doc gaps closed with it: upstream cause ranking,
ErrorRatio, suppression, archive retention, MCP topology tools."""

from __future__ import annotations

import itertools
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aegis.contracts.events import Event, Signal  # noqa: E402
from aegis.l3_storage.store import MAX_ARCHIVED_INCIDENTS, ProjectStore  # noqa: E402
from aegis.l4_understanding.topology import Topology, component_of  # noqa: E402
from aegis.l5_detection.detectors import DetectionEngine  # noqa: E402
from aegis.l6_correlation.incidents import IncidentManager  # noqa: E402

_ids = itertools.count(1)


def _ev(text, ts="10:00:00", level="INFO", template="T-x", fields=None):
    return Event(id="E", ts=ts, service="svc", level=level, text_redacted=text,
                 template_id=template, fields=fields or {})


def _trace(*component_tags):
    return [_ev(f"[{tag}] step of {tag}", template=f"T-{tag}-{i}")
            for i, tag in enumerate(component_tags)]


def _mined():
    traces = {f"t{i}": _trace("api", "identity", "orch", "tts", "filler")
              for i in range(5)}
    return Topology.mine(traces)


# --- topology ----------------------------------------------------------------

def test_component_order_is_learned_from_traces():
    topo = _mined()
    assert topo.is_upstream("api", "tts")
    assert topo.is_upstream("identity", "filler")
    assert not topo.is_upstream("filler", "api")


def test_indistinguishable_components_are_not_ordered():
    """Sequence edges made the graph fully cyclic (components alternate in
    conversation), so ordering comes from first-appearance rank - and two
    components inside the margin get an honest None, not a coin flip."""
    traces = {"t1": _trace("a", "b"), "t2": _trace("b", "a")}
    topo = Topology.mine(traces)
    assert topo.most_upstream(["a", "b"]) is None


def test_blast_radius_is_what_runs_after():
    topo = _mined()
    radius = topo.blast_radius("identity")
    assert "tts" in radius["downstream"] and "filler" in radius["downstream"]
    assert "api" in radius["upstream"]


def test_component_of_reads_tags_and_loggers():
    assert component_of("[tts] Synthesised 60 chars") == "tts"
    assert component_of("INFO voice.identity: Lookup FAILED") == "identity"
    assert component_of("plain line", fallback="svc") == "svc"


# --- upstream-aware cause ranking --------------------------------------------

def _sig(text, ts, trace="t-1", severity="P2"):
    return Signal(id=f"S-{next(_ids)}", detector="NoveltyDetector", service="svc",
                  metric="m", observed=1, baseline=0, ratio=0, started_at=ts,
                  severity=severity, trace_id=trace, evidence=[text])


def test_topology_annotates_but_never_overrides_onset():
    """Two live experiments let flow position arbitrate; both demoted a true
    root cause to a symptom - within one service, "earlier in the flow" is
    the caller as often as the feeder. Earliest onset ranks; topology adds
    the flow context line and nothing more."""
    manager = IncidentManager()
    manager.set_topology(_mined())
    manager.observe(_sig("[orch] classification failed", "10:00:00"), now=0.0)
    manager.observe(_sig("[api] brain exceeded grace window", "10:00:02"), now=2.0)
    manager.observe(_sig("[tts] synth crawling", "10:00:03"), now=3.0)
    incident = manager.incidents[0]
    cause = next(m for m in incident.members if m.id == incident.ranked_cause)
    assert "[orch]" in cause.evidence[0], incident.cause_why
    context = [r for r in incident.cause_why if r.startswith("flow context:")]
    assert context and "api" in context[0] and "tts" in context[0]


def test_without_topology_ranking_admits_timing_alone():
    manager = IncidentManager()
    manager.observe(_sig("[tts] synth timed out", "10:00:00"), now=0.0)
    manager.observe(_sig("[identity] Lookup FAILED", "10:00:02"), now=2.0)
    incident = manager.incidents[0]
    assert any("no flow graph yet" in reason for reason in incident.cause_why)


# --- ErrorRatio ---------------------------------------------------------------

def test_error_storm_fires_against_the_streams_own_share():
    engine = DetectionEngine()
    for i in range(200):  # healthy history, ~1% errors
        level = "ERROR" if i % 100 == 0 else "INFO"
        engine.observe(_ev("steady line", ts=f"10:{i // 60:02d}:{i % 60:02d}",
                           level=level, template="T-s"))
    fired = []
    for i in range(40):   # then a sustained error storm
        t = 400 + i
        fired += [s for s in engine.observe(
            _ev("upstream exploded", ts=f"10:{t // 60:02d}:{t % 60:02d}",
                level="ERROR", template="T-e2"))
            if s.detector == "ErrorRatio"]
    assert len(fired) == 1, f"{len(fired)} ErrorRatio signals"
    assert fired[0].severity == "P2"
    assert fired[0].evidence


def test_sc5xx_counts_as_error_even_at_info_level():
    """An access log says 'error' with a status class, not a level - the
    whole reason the fingerprinter preserves sc5xx."""
    engine = DetectionEngine()
    ok = {"template_pattern": "GET <PATH> HTTP/<NUM> sc2xx <NUM>"}
    bad = {"template_pattern": "GET <PATH> HTTP/<NUM> sc5xx <NUM>"}
    for i in range(150):
        engine.observe(_ev("access ok", ts=f"10:{i // 60:02d}:{i % 60:02d}",
                           template="T-ok", fields=ok))
    fired = []
    for i in range(40):
        t = 400 + i
        fired += [s for s in engine.observe(
            _ev("access 502", ts=f"10:{t // 60:02d}:{t % 60:02d}",
                template="T-bad", fields=bad))
            if s.detector == "ErrorRatio"]
    assert fired, "a 502 storm at INFO level went unnoticed"


# --- suppression --------------------------------------------------------------

def test_a_muted_template_is_invisible_to_every_detector():
    engine = DetectionEngine()
    engine.suppressed = {"T-noisy"}
    muted = Event(id="E", ts="10:00:00", service="svc", level="ERROR",
                  text_redacted="known-noisy warning", template_id="T-noisy",
                  is_novel=True)
    assert engine.observe(muted) == [], "a muted template produced a signal"


def test_muting_the_only_template_does_not_fake_a_silence():
    engine = DetectionEngine()
    engine.suppressed = {"T-noisy"}
    t = 0
    for _ in range(60):
        engine.observe(_ev("tick", ts=f"10:{t // 60:02d}:{t % 60:02d}",
                           template="T-noisy")); t += 1
    signals = engine.observe(_ev("tick", ts=f"10:{(t + 400) // 60:02d}:{(t + 400) % 60:02d}",
                                 template="T-noisy"))
    assert not any(s.detector == "SilenceDetector" for s in signals)


def test_suppression_survives_restart_via_the_store():
    root = tempfile.mkdtemp()
    store = ProjectStore("svc", root=root)
    store.add_suppression("T-noisy", "known flaky healthcheck")
    store.close()
    reopened = ProjectStore("svc", root=root)
    rows = reopened.suppressions()
    assert rows and rows[0]["template_id"] == "T-noisy"
    reopened.remove_suppression("T-noisy")
    assert reopened.suppressions() == []
    reopened.close()


# --- retention ----------------------------------------------------------------

def test_the_archive_is_bounded():
    """The doc allows no unbounded table. 600 archived incidents in, the
    oldest 100 age out."""
    store = ProjectStore("svc", root=tempfile.mkdtemp())
    for i in range(MAX_ARCHIVED_INCIDENTS + 100):
        store.archive_incident({"id": f"INC-{i:04d}@10:00:00",
                                "archived_at": f"2026-09-07 {i // 3600:02d}:{i // 60 % 60:02d}:{i % 60:02d}"})
    rows = store.archived_incidents(limit=1000)
    assert len(rows) == MAX_ARCHIVED_INCIDENTS
    ids = {r["id"] for r in rows}
    assert "INC-0000@10:00:00" not in ids, "the oldest precedent survived the TTL"
    assert f"INC-{MAX_ARCHIVED_INCIDENTS + 99:04d}@10:00:00" in ids
    store.close()


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
    print(f"\n{'FAILED' if failures else 'All topology-batch tests passed'}"
          f"{f' ({failures} failing)' if failures else ''}")
    sys.exit(1 if failures else 0)
