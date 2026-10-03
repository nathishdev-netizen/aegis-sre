# Phase 2 — Detection (C8)

**Status:** complete · **Branch:** `dev-v2` · **Tests:** 14 (`tests/test_aegis_phase2.py`)

Architecture doc reference: C8 (Detection Engine), principles P1/P2, the
three-tier funnel, and the anti-patterns table.

---

## What this phase is for, in one sentence

Decide, cheaply and deterministically, whether something deserves attention —
nothing above this layer runs until this layer says so.

## The rule this phase obeys absolutely

**Zero model calls.** Rules P1/P2: detection is statistics, explanation is
language. If any part of detection called a model it would be misplaced — slow,
expensive, and non-reproducible. The LLM enters at the reasoning layer (C10,
doc Phase 6) to *explain* what these detectors found, never to find it.

## The detector catalogue (implemented subset)

| Detector | Watches | Fires when |
|---|---|---|
| `NoveltyDetector` | template store | a template's very first occurrence (ERROR/WARN always; INFO after warmup) |
| `RateSpike` | events/min per template | rate ≫ that template's own history, sustained (hysteresis) |
| `RateDrop` | steady templates | one that always ran has stopped — noticed from *other* events' arrivals |
| `LatencyShift` | durations in lines | an operation vs its own median (reuses the proven `app.core.baselines`) |
| `SilenceDetector` | the whole stream | a quiet longer than any quiet the service has already shown |

Deliberately not implemented yet: `ErrorRatio`/`CardinalityShift` (need
structured success/attempt fields), `ConformanceRate` (needs C7), seasonal
hour-of-day baselines (this log format carries no dates on lines).

## Hysteresis — every threshold has four parameters

Fire only after the breach sustains (20s); clear only after recovery holds
below a lower bar (60s). The doc's flapping example is FIRE/CLEAR ×40 in
ninety seconds; the tests pin that hovering never fires and one burst is one
signal.

## Measured, against the real logs

```
voice-gateway:  1288 lines → 776 events → 73 templates → 73 signals
                → 26 worth attention (P1–P3), 47 quiet P4 first-occurrence notes
demo-app:       82 lines → 5 templates → 1 signal (the ERROR novelty)
```

The P2 list is the file's actual story, found by arithmetic alone: identity
`Lookup FAILED`, `TURN SKIPPED` (the hollow 13:40 call), intent classification
failure, 30/47 filler clips failing to synthesise, a TTS burst at 32/min
against a 1.45/min baseline, and a 93-second call against a 15.5s baseline.

## Bugs found by running, then pinned as tests

| Bug | Symptom | Fix |
|---|---|---|
| Quoted text fragmented templates | 15 copies of one warning = 15 templates = 15 novelty signals | quoted strings mask to `<STR>` (the doc's own template example does this) |
| Silence judged against the median gap | 10 alarms for a call service being normally idle between calls | the bar is the longest quiet already shown |
| Demo burst shorter than the sustain window | "worked example" produced 0 signals while claiming 1 | burst now spans 40s; the zero was hysteresis correctly refusing a blip |

## How to verify it yourself

```bash
python3 -m aegis.demo.phase2 [logfile] [project]
python3 tests/test_aegis_phase2.py
```

Then pick any P2 from section 3 and grep its evidence line in the raw file —
every number must be reproducible from the file alone. A signal citing
evidence you cannot find is a bug.

## Next, per the doc's build sequence

Phase 3 — C5 topology mapper (needs multiple services to be interesting), or
directly Phase 4 — C9 incident manager, which turns these 73 signals into the
handful of incidents a person actually works with ("one incident, not forty
alerts"). The 47 P4 novelty notes exist precisely to be grouped by it.
