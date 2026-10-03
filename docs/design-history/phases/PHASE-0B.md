# Phase 0 — completed per the architecture doc

**Status:** complete · **Branch:** `dev-v2` · **Tests:** 9 (`tests/test_aegis_pipeline.py`) + earlier suites

The doc's Phase 0 requires **C1 collectors, C3 fingerprinting, C4 hot store**.
PHASE-0.md covered C3. This phase adds C1 and C4 and wires all of it into one
pipeline — Phase 0 is now complete as the document defines it.

---

## The two requirements this phase is really about

**1. Never affect any other project.** Enforced, not promised:

- The watched log is opened **read-only**; the demo prints a sha256 of the file
  before and after every run and reports `watched log untouched: YES`.
- A regression test (`test_pipeline_never_writes_to_the_watched_project`)
  asserts byte-for-byte identity.
- All Aegis data lives under `~/.aegis/` — never inside any project directory.

**2. Scoped to the particular project.** Isolation is physical, not a
convention: each project gets its **own database file** at
`~/.aegis/projects/<project>/store.db`. There is no shared table for a bug's
WHERE clause to get wrong. `test_projects_are_physically_isolated` pins it.

```
~/.aegis/projects/
  voice-gateway/store.db     76 KB
  demo-app/store.db          20 KB
```

---

## What was built

| File | Doc component | Job |
|---|---|---|
| `aegis/l1_ingestion/file_collector.py` | C1 | Read, tag, forward. Survives rotation; never emits half-written lines; only state is a read offset |
| `aegis/l3_storage/store.py` | C4 | `HotRing` (recent events, memory) + `ProjectStore` (templates and per-minute counts, per-project SQLite) |
| `aegis/pipeline.py` | — | collector → normalizer → linker → store, bound to exactly one project |
| `aegis/demo/pipeline.py` | — | the manual verification run |

Raw text is **not** persisted — the store keeps derived data (templates,
counts); raw lines stay in the project's own log file where they already live.
The per-minute counts per template are the raw material for the doc's Phase 2
detectors.

---

## How to verify it yourself

```bash
# run on any project's log; project name defaults to the file stem
python3 -m aegis.demo.pipeline <logfile> [project] [--fresh]
python3 tests/test_aegis_pipeline.py
```

Run it on two different logs and check:
- two directories under `~/.aegis/projects/`, one per project
- `watched log untouched: YES` on both
- each store's templates describe only its own project

Measured, voice-gateway: 1288 lines → 776 events → 94 templates (13.7×),
142 extracted / 173 inferred. A second, differently-shaped log (order/payment
vocabulary): 82 lines → 5 templates (16.4×), 0 unattributed.

---

## A generality bug the second project exposed

The trace linker's session closer was hardcoded to `CALL END` — the reference
project's word. On the order-shaped log, `ORDER END` closed nothing, sessions
piled up, and inference (correctly) refused: 2 inferred, 40 unattributed.

Making the closer *any* END-word overshot the other way: voice-gateway's
`TURN END` closed the whole call mid-conversation and inferred coverage fell
173 → 135.

The rule that survives both, with nothing hardcoded: an opener names the
session's own noun (`CALL START` → CALL, `ORDER START` → ORDER), and only that
noun's END closes it. Hard closers (hangup, disconnected) always close. Both
projects now sit at their best numbers simultaneously, and both cases are
regression-tested.

---

## Next, per the doc's build sequence

Phase 1 of the doc (OTel `trace_id` propagation) belongs in *your* services —
the platform is already trace-ready (a real `trace_id=` outranks app keys).
So next to build here: **Phase 2 — C8 detectors** (rate spike/drop, novelty,
latency shift, silence) over the per-minute counts this phase now persists.
