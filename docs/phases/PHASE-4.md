# Phase 4 — Incidents (C9)

**Status:** complete · **Branch:** `dev-v2` · **Tests:** 12 (`tests/test_aegis_incidents.py`)

Architecture doc reference: C9 (Incident Manager), principle P3 ("one incident,
not forty alerts"), P4 ("report the earliest deviation, not the loudest error").

---

## What this phase is for, in one sentence

Convert the signal stream into the small number of stateful objects a person
actually works with — grouped, ranked, and honest about both.

## Measured, against the real log

```
73 signals  →  13 incidents  +  27 quiet P4 notes
```

The richest incident is the proof it works. The 13:48 call:

```
INC-12  [P2]  resolved   13:48:54 → 13:50:15
  11 members · 1 trace · blast: 93-second call
    NoveltyDetector  Classification failed            ◄ RANKED CAUSE
    NoveltyDetector  Brain exceeded the 2.0s grace window
    LatencyShift     TTS 3132ms vs 748ms baseline
    LatencyShift     CALL END 93s vs 15.5s baseline
  why ranked first: earliest onset · preceded next by 2s ·
                    11 members share its trace · timing-only (no topology yet)
```

That is a real root-cause ordering — classification failure first, everything
else downstream of it — produced entirely by arithmetic.

## Doc rules kept

- **Two of four grouping rules must agree** (trace identity, adjacency,
  temporal, flow). One alone over-groups — tested.
- **Adjacency is honest about being degenerate**: with one service and no C5
  topology, "same service" is adjacency at distance zero. The code marks
  exactly where C5 sharpens it.
- **The ranked cause states its basis** — "timing alone, C5 will sharpen this"
  — instead of implying a topological analysis that never ran.
- **P4 signals never open an incident.** They attach to open ones as context
  or remain quiet notes. ~97% of alerts need no action.
- **The timeline is append-only** and records which rules grouped each member.
- **Resolution requires sustained quiet** (300s), a resolved incident stays
  resolved (recurrence is a new incident), and end-of-stream leaves open
  incidents open — the stream ending is not evidence of recovery.
- **Every signal is accounted for**: members + notes = signals in, tested.

## How to verify it yourself

```bash
python3 -m aegis.demo.incidents [logfile] [project]
python3 tests/test_aegis_incidents.py
```

Pick an incident's opening time and read the raw file around it. Every member's
evidence line must exist there, and the members must plausibly be one story.
Grouping two unrelated problems is a bug; splitting one across two is too.

## Deviation from strict doc order, stated

The doc sequences Phase 3 (C5 topology) before Phase 4 (C9). Topology needs
multiple services to mean anything; this is a one-service world so far, so C9
came first and its adjacency rule runs degenerate until C5 lands. Nothing in
C9's interface changes when it does — the `_match` rules just get sharper.

## Next, per the doc

Phase 6 — **C10 agent runtime + C16 governance**: the LLM finally enters, to
explain incidents with their evidence attached (never to detect). Then Phase 7,
the moat: C6 flow specs + C7 conformance.
