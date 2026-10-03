# Phase 7 — Flow conformance (C6 + C7). The moat.

**Status:** complete · **Branch:** `dev-v2` · **Tests:** 13 (`tests/test_aegis_conformance.py`)

Architecture doc reference: C6 (Flow Spec), C7 (Conformance), the build-order
note "Phase 7 is the moat. Do not let it slip."

---

## What this phase is for, in one sentence

Compare what a run actually did against what a run of its kind is supposed to
do — and catch the failure that raises no error at all.

## Measured, against the real log — the headline result

```
12 calls:   achieved: 4    failed: 2    hollow: 6
```

**Half of all calls were hollow**: zero errors, normal `NORMAL_CLEARING`
teardown, green on every dashboard that exists — and the caller never completed
a single turn. The v2 plan's target case, the 13:40:26 call, is among them:

```
HOLLOW  82050189 @13:40:26 (20s)
        completed cleanly - no errors, normal teardown -
        but none of its purpose steps ever ran
```

The plan's other target, 12:18:41, came back `failed` as predicted. One honest
divergence: the plan guessed 13:26:47 was `achieved`; the data shows that
caller also hung up without speaking — hollow. The system out-judged the guess.

## How it works

**C6 — the spec is mined, then marked.** `FlowMiner` derives steps from the
project's own traces: presence fraction, order, durations, `required` at ≥70%
presence. All deterministic. Then the one model step the doc allows in C6:
the FlowSynthesizer marks which steps are the run's *purpose* — the model may
only flag steps that exist (unknown ids are dropped), the flag records who set
it (`model`/`human`), and the spec is saved human-editable at
`~/.aegis/projects/<p>/flows/*.json`. Your edit outranks the model's.

**C7 — every trace gets a verdict**: `achieved` / `failed` / `hollow` /
`degraded` / `unknown` — with `unknown` said plainly when no purpose steps are
marked, rather than guessed. Optional steps absent produce zero noise. Shadow
mode is the default; `enforce` emits a P2 `ConformanceRate` signal per hollow
run, which the incident manager groups by trace like any other signal.

## The bug the first run exposed (now the key regression test)

The first live run condemned **every** real conversation as hollow. The
`process` step mines into per-code-path template variants (each call's
wording differs), the model marked six variants critical, and the checker
demanded *all* of them — so each call was hollow for lacking the *other*
calls' variants. Fix: critical steps are a **purpose family** — a run is
hollow only when *none* of them ran. `test_purpose_is_a_family_not_a_checklist`
pins it.

## How to verify it yourself

```bash
python3 -m aegis.demo.conformance             # 1 Groq call (marks purpose steps)
python3 -m aegis.demo.conformance --no-llm    # zero calls; you mark the spec by hand
python3 tests/test_aegis_conformance.py       # 13 tests, zero network
```

Then pick a HOLLOW trace id and `grep` it in the raw file: you should find a
greeting, a prompt, a hangup — and no completed turn. Edit the spec file and
rerun: verdicts must follow *your* spec.

## What this deliberately does not do

- No paging on conformance — shadow mode by default (anti-pattern table).
- No invariants (`reserve.end < pay.start`) yet — they need span timing that
  single-line logs don't carry; the spec format has room for them.
- No repo analysis — the doc's full C6 reads the codebase; this is the
  learned-from-traces half, which works with zero access to the source.

## Doc build-order status

| Doc phase | Component | Status |
|---|---|---|
| 0 | C1 + C3 + C4 | ✅ |
| 1 | OTel in your services | platform trace-ready; yours when wanted |
| 2 | C8 detectors | ✅ |
| 3 | C5 topology | deferred — needs multiple services |
| 4 | C9 incidents | ✅ |
| 5 | C2 provider connectors | not started |
| 6 | C10 + C16 | ✅ |
| **7** | **C6 + C7** | **✅ — the moat is built** |
