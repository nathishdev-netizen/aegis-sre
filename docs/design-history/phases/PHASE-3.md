# Phase 3 — Topology (C5-lite) + the doc-audit batch

**Status:** complete · **Branch:** `dev-v2` · **Tests:** 13 (`tests/test_aegis_topology.py`)

A fresh audit of aegis-architecture.md found six items previously marked
"blocked" or silently skipped that were in fact buildable. All six now exist.

## 1. Topology (C5, at component granularity)

Mined from the project's own traces: each component's typical first-appearance
position gives a stable flow order (`api → identity → orch → intent → tts →
filler` on the reference log — the actual call flow). Sequence *edges* proved
the wrong upstream signal (conversational components alternate, making the
transition graph fully cyclic), so ordering is rank-based, with a margin below
which the graph refuses to order two components rather than coin-flip.

**The honest limit, learned by experiment:** two live attempts let flow
position *arbitrate* cause ranking, and both demoted a true root cause to a
symptom — within one service, "earlier in the flow" is the caller as often as
the feeder, so rank cannot tell cause from observer in either direction. That
arbitration needs the doc's real service-dependency graph. Until multi-service
traces exist, **topology annotates and never arbitrates**: incidents keep
earliest-onset ranking, gain a `flow context: api → orch → tts` line, and
blast-radius queries work.

## 2. Cause ranking (C9) — clarified

Earliest onset ranks, full stop, with the flow context attached. The "no
topology yet" caveat only appears when there genuinely is none.

## 3. ErrorRatio detector (C8)

Previously skipped as "needs structured fields". Error-*like* events (ERROR/
CRITICAL level, or the `sc5xx` status class — how an access log says "error"
at INFO level) as a share of the stream, against the stream's own historical
share, with hysteresis. A 502 storm in an nginx log now fires P2 even though
every line is INFO.

## 4. SuppressionRules (C8)

The mute switch: templates in the suppression list are invisible to every
detector (but still mark the stream as alive, so muting the only chatty
template cannot fake a silence). Persisted per project (`suppressions` table),
survives restarts, exposed as `/api/aegis/suppress` (undo supported).

## 5. Retention (C4)

"Every table has a TTL, enforced" — the incident archive was unbounded. It now
keeps the most recent 500 precedents, enforced at each write. (The per-minute
counters were already naturally bounded at 1440 time-of-day buckets.)

## 6. MCP tools (C12)

`get_topology` (nodes, edges, flow order) and `get_blast_radius(component)` —
both in the doc's tool list, both now served. Blast radius answers the doc's
IDE scenario: *"what is starved if this component fails?"*

## Verify

```bash
python3 tests/test_aegis_topology.py
```

On the real log: INC-12 ranks `[intent] Classification failed` (the true
root) with `flow context: api → tts → orch → intent` attached.
