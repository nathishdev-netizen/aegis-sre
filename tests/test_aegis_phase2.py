"""Phase 2 regression tests - the C8 detection engine.

Zero model calls anywhere in this file: detection is arithmetic, and these
tests pin both the arithmetic and the restraint - most of what a detector must
do is refuse to fire.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aegis.contracts.events import Event  # noqa: E402
from aegis.l2_normalization.fingerprint import Fingerprinter  # noqa: E402
from aegis.l5_detection.detectors import DetectionEngine, HysteresisGate  # noqa: E402


def _ts(t: int) -> str:
    return f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}"


def _ev(text: str, t: int, level: str = "INFO", novel: bool = False,
        template: str = "T-x") -> Event:
    return Event(id="E", ts=_ts(t), service="svc", level=level,
                 text_redacted=text, template_id=template, is_novel=novel)


# --- Hysteresis: the four-parameter threshold --------------------------------

def test_hovering_at_the_line_never_fires():
    """The doc's flapping example: FIRE/CLEAR forty times in ninety seconds
    from one metric hovering at a one-parameter threshold. With a sustain
    requirement, a breach that keeps lapsing never fires at all."""
    gate = HysteresisGate(sustain_s=20, clear_sustain_s=60)
    for t in range(0, 200, 2):
        change = gate.update(float(t), above_fire=(t % 4 == 0), below_clear=(t % 4 != 0))
        assert change is None, f"flapped at t={t}"


def test_sustained_breach_fires_exactly_once():
    gate = HysteresisGate(sustain_s=20, clear_sustain_s=60)
    changes = [gate.update(float(t), above_fire=True, below_clear=False)
               for t in range(0, 120, 5)]
    assert changes.count("fire") == 1
    assert gate.firing


def test_clear_requires_sustained_recovery():
    """Auto-closing on a single healthy reading reopens on the next bad one -
    the doc requires recovery to hold below a LOWER bar."""
    gate = HysteresisGate(sustain_s=10, clear_sustain_s=60)
    for t in range(0, 30, 5):
        gate.update(float(t), above_fire=True, below_clear=False)
    assert gate.firing
    # 30s of recovery: not enough for a 60s clear_sustain.
    for t in range(30, 60, 5):
        assert gate.update(float(t), above_fire=False, below_clear=True) != "clear"
    # A relapse resets the recovery clock entirely.
    gate.update(60.0, above_fire=True, below_clear=False)
    for t in range(65, 120, 5):
        change = gate.update(float(t), above_fire=False, below_clear=True)
    assert change == "clear"


# --- RateSpike ---------------------------------------------------------------

def _burst(engine: DetectionEngine, start: int, count: int, per_second: int) -> int:
    fires = 0
    for i in range(count):
        signals = engine.observe(_ev("reserve failed", start + i // per_second,
                                     level="ERROR", template="T-b"))
        fires += sum(1 for s in signals if s.detector == "RateSpike")
    return fires


def test_spike_needs_five_minutes_of_history():
    """A rate comparison against two minutes of history is an anecdote. The
    burst that opens a file must not fire - there is nothing to compare with."""
    engine = DetectionEngine()
    assert _burst(engine, start=0, count=400, per_second=10) == 0


def test_burst_against_a_real_baseline_fires_once():
    """The doc's worked example: a steady trickle, then 400 lines. One signal,
    not four hundred - and none at all until the breach has sustained."""
    engine = DetectionEngine()
    for minute in range(10):
        engine.observe(_ev("reserve failed", minute * 60, level="ERROR", template="T-b"))
    assert _burst(engine, start=660, count=400, per_second=10) == 1


# --- Novelty -----------------------------------------------------------------

def test_error_novelty_fires_even_on_a_cold_start():
    engine = DetectionEngine()
    signals = engine.observe(_ev("Lookup FAILED after 3 retries", 10,
                                 level="ERROR", novel=True, template="T-n"))
    assert [s.detector for s in signals] == ["NoveltyDetector"]
    assert signals[0].severity == "P2"


def test_info_novelty_waits_out_the_warmup():
    """On a cold store every template is novel. Without a warmup, backfilling
    a file produces one signal per template - a flood that says nothing."""
    engine = DetectionEngine()
    signals = engine.observe(_ev("Gateway ready", 10, novel=True, template="T-n"))
    assert signals == []
    for i in range(engine.NOVELTY_WARMUP_EVENTS):
        engine.observe(_ev("steady line", 20 + i, template="T-s"))
    late = engine.observe(_ev("something new", 500, novel=True, template="T-n2"))
    assert any(s.detector == "NoveltyDetector" and s.severity == "P4" for s in late)


def test_second_occurrence_is_never_novel():
    engine = DetectionEngine()
    engine.observe(_ev("boom", 10, level="ERROR", novel=True, template="T-n"))
    signals = engine.observe(_ev("boom", 20, level="ERROR", novel=False, template="T-n"))
    assert not any(s.detector == "NoveltyDetector" for s in signals)


def test_quoted_variants_share_one_template():
    """Fifteen occurrences of the same warning, each quoting different filler
    text, became fifteen templates and fifteen novelty signals. Quoted strings
    are variables; the doc's own template example writes sku <STR>."""
    fingerprinter = Fingerprinter()
    first = fingerprinter.add(
        "WARNING voice: could not pre-synthesise filler 'Okay. Let me search.': error")
    second = fingerprinter.add(
        "WARNING voice: could not pre-synthesise filler 'Right. One moment.': error")
    assert first.template_id == second.template_id


# --- RateDrop ----------------------------------------------------------------

def test_a_steady_template_that_stops_is_reported_once():
    """Nothing fires when a step simply stops happening - the stopped template
    emits nothing to detect with, so its absence is noticed from OTHER events'
    arrivals, and exactly once."""
    engine = DetectionEngine()
    for t in range(0, 360, 5):
        engine.observe(_ev("heartbeat ok", t, template="T-steady"))
    drops = []
    for t in (1000, 1010, 1020):
        for signal in engine.observe(_ev("other work", t, template="T-other")):
            if signal.detector == "RateDrop":
                drops.append(signal)
    assert len(drops) == 1
    assert drops[0].template_id == "T-steady"
    assert drops[0].evidence, "a drop signal without the dropped line as evidence"


# --- Silence -----------------------------------------------------------------

def test_silence_bar_adapts_to_the_services_own_rhythm():
    """A call-based service is normally quiet between calls. Judged against its
    median gap (~1s), every routine idle fired - ten alarms in one file. The
    bar is the longest quiet already shown, so the same idle never fires twice."""
    engine = DetectionEngine()
    t = 0
    for _ in range(60):
        engine.observe(_ev("tick", t, template="T-t")); t += 1
    first_idle = engine.observe(_ev("tick", t + 400, template="T-t"))
    assert any(s.detector == "SilenceDetector" for s in first_idle)
    t += 400
    for _ in range(60):
        engine.observe(_ev("tick", t, template="T-t")); t += 1
    second_idle = engine.observe(_ev("tick", t + 400, template="T-t"))
    assert not any(s.detector == "SilenceDetector" for s in second_idle), \
        "the service already showed a 400s quiet; another one is its rhythm"


def test_clock_jumping_backwards_is_a_restart_not_a_silence():
    """The reference file spans days of restarts with time-of-day stamps only.
    19:00 followed by 09:00 is a new session, not a fourteen-hour outage."""
    engine = DetectionEngine()
    t = 68400  # 19:00:00
    for _ in range(60):
        engine.observe(_ev("tick", t, template="T-t")); t += 1
    signals = engine.observe(_ev("tick", 32400, template="T-t"))  # 09:00:00
    assert not any(s.detector == "SilenceDetector" for s in signals)


# --- LatencyShift ------------------------------------------------------------

def test_latency_spike_fires_with_cooldown():
    """Two spikes seconds apart are one story; the cooldown keeps the second
    from doubling the page."""
    engine = DetectionEngine()
    for i in range(12):
        engine.observe(_ev("[tts] done in 100ms", i * 10, template="T-l"))
    first = engine.observe(_ev("[tts] done in 900ms", 130, template="T-l"))
    assert any(s.detector == "LatencyShift" for s in first)
    second = engine.observe(_ev("[tts] done in 905ms", 135, template="T-l"))
    assert not any(s.detector == "LatencyShift" for s in second)


def test_every_signal_carries_evidence():
    """A signal without the line that proves it is an opinion (P5)."""
    engine = DetectionEngine()
    engine.observe(_ev("boom", 10, level="ERROR", novel=True, template="T-n"))
    for signal in engine.signals:
        assert signal.evidence and signal.evidence[0].strip()


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
    print(f"\n{'FAILED' if failures else 'All Phase 2 tests passed'}"
          f"{f' ({failures} failing)' if failures else ''}")
    sys.exit(1 if failures else 0)
