# Phase 1 — Correlation

**Status:** complete · **Branch:** `dev-v2` · **Tests:** 8 (`tests/test_aegis_phase1.py`)

Architecture doc reference: C3 `TraceLinker`, and the §11 rule that `trace_id`
is the join key for the entire platform.

---

## What this phase is for, in one sentence

Decide which lines belong to which request — as evidence when the line says so,
as a labelled assumption when it can be deduced safely, and as an honest blank
when it cannot.

## The measured problem

Only **142 of 776 events (18%)** in the reference log name their call
(`call=<uuid>`). The other 82% are emitted between a call's start and end
without naming it. The doc warned exactly this: where the key is absent,
correlation degrades from a join to a guess. This phase makes the guess safe by
making it *narrow* and *labelled*.

## The rule

| Situation | Action | `correlation_basis` |
|---|---|---|
| Line names its key | attach it | `extracted` (evidence) |
| No key, exactly **one** session open | attach that session | `inferred` (assumption) |
| No key, zero or several sessions open | attach nothing | `none` |

A session opens when its key is first seen, closes on its `CALL END`/hangup
line, and expires after 5 minutes of silence — a crash never logs its END, and
without the timeout every later line would be inferred into a call that
finished an hour ago.

The reference service handles one call at a time, which is why inference is
safe *there*. A production service overlaps — and attributing a line to the
wrong customer's trace is worse than attributing it to none. Hence the refusal
row in the table.

## Measured result

```
776 events
142 extracted (18%)  ·  173 inferred (22%)  ·  461 none (59%)
coverage: 18% → 41%  ·  12 distinct traces
```

The 59% is honest, not a miss: the file spans 16 gateway restarts, and startup
banners, filler-clip warmup and shutdown belong to no call.

## How to verify it yourself

```bash
python3 -m aegis.demo.phase1              # coverage, traces, one full journey
python3 tests/test_aegis_phase1.py        # 8 regression tests
```

Then pick a call id from the output and check independently:

```bash
grep 'call=<id>' <logfile> | wc -l    # lines naming it
```

The trace's event count minus that number is what inference recovered. Read the
journey and judge whether any `[i]` line plausibly belongs to a different call.

## A fabrication caught while building this

The largest trace showed an event at `10:54:24` inside a call that ran
19:02–19:03. That time exists **nowhere in the file** — it was the wall-clock
time of the analysis run. v1's parser stamps "now" on lines that carry no
timestamp, which is right for live tailing and wrong for reading history. The
normalizer now inherits the last real timestamp instead, and a regression test
(`test_timestampless_line_inherits_instead_of_fabricating`) pins it.

## What this phase deliberately does not do

- No cross-service joins yet — one file, one service. Multi-source correlation
  needs the L1 collectors.
- No OpenTelemetry. The linker prefers a real `trace_id=` over the app's own
  `call=` key, so when OTel arrives it wins automatically with no refactor.
- No model calls. Correlation is pattern matching and bookkeeping.

## Next

With traces assembled, the moat becomes buildable: flow conformance (cut-down
C6/C7) — what does a *complete* call look like, and which of these 12 traces
deviated from that shape without throwing an error?
