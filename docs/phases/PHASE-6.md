# Phase 6 — Reasoning under governance (C10 + C16)

**Status:** complete · **Branch:** `dev-v2` · **Tests:** 8 (`tests/test_aegis_reasoning.py`)

Architecture doc reference: C10 (Agent Runtime), C16 (Governance), §9 (model
routing), P5 (every claim ships with evidence), P7 (cost is architecture),
P8 (degrade, never block).

---

## What this phase is for, in one sentence

The model finally enters — to explain what the detectors found, under rules
the runtime enforces rather than requests, and never to detect.

## The three enforced constraints

**1. Governed spend (C16).** A hard `Budget` ceiling (default 10 calls/session,
demo capped at 4) and a minimum interval between calls, enforced in the router
— not the prompt. Every call is appended to `~/.aegis/audit.jsonl` *before*
it is attempted, so a crash mid-call still leaves a record. Tested: 11 requests
against a budget of 3 → exactly 3 calls, 8 refusals, all audited.

**2. Grounded output (C10).** The model must copy its evidence citations
verbatim from the incident. Every citation is checked against the incident's
own evidence; fabricated ones are dropped, and a hypothesis with zero surviving
citations is delivered as UNVERIFIED with confidence forced to `low`. Research
basis (from the v2 plan): explanations raise reliance on wrong answers as much
as right ones — only checkable sources calibrate trust.

**3. Redacted input only.** The prompt is built from the incident dict, whose
evidence was redacted at ingest (Phase 0). There is no unredacted variant to
send — the guarantee is structural, and tested.

## Provider strategy (per the user's decision)

Groq free-tier is the default route for every task; the paid OpenAI key is
spent only on an explicit override or `--compare`. Routing is configuration:

```
AEGIS_MODEL_EXPLAIN_INCIDENT="openai:gpt-4o-mini"   # env override, no code change
```

Current route: `groq : openai/gpt-oss-120b` (verified against `GET /models` —
the first pick had already been retired, which is exactly why routing must be
configuration).

## Verified live, with one call

```
INC-12 [P2] 11 members — the 13:48 degraded call
  [groq] confidence=medium (grounded)
  "The voice service hit timeouts and latency spikes (brain exceeded its 2s
   grace window, TTS took >3s), causing intent classification failures..."
   evidence: 3 verbatim lines, all survived grounding
   now: check the brain component  ·  later: improve brain latency
```

Two wiring failures found on the way, both now handled: Groq's Cloudflare edge
rejects urllib's default user-agent (403 error 1010), and the routed model had
been retired (404) — the router's audit log identified both in seconds.

## How to verify it yourself

```bash
python3 tests/test_aegis_reasoning.py     # ZERO real API calls - fake transport
python3 -m aegis.demo.explain             # 1 Groq call, budget-capped at 4
python3 -m aegis.demo.explain --compare   # +1 OpenAI call, for the side-by-side
tail ~/.aegis/audit.jsonl                 # every call, allowed or refused
```

The test suite never touches the network, so running it costs nothing and
drains no key.

## Next, per the doc

Phase 7 — **the moat**: C6 flow specs + C7 conformance. What should a call look
like, and which calls deviated without throwing an error — the hollow 13:40
call is the target case.
