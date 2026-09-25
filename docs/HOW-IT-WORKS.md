# How Aegis works — one line's journey, the full vision, and what exists

*(Written 2026-09-07, at the close of the dev-v2 build. Numbers below are real
measurements from the reference voice-gateway log, not examples.)*

---

## Part 1 — Today: the journey of one log line

```
2026-09-01 19:02:10 INFO voice.tts: [tts] Synthesised 60 chars in 1046ms - 60648 bytes wav
```

| Stage | What happens to the line | Measured |
|---|---|---|
| **① Collect** (L1) | read from the file, read-only, offset-only state, logrotate-safe, half-written lines held back | file sha256-identical after every run |
| **② Normalize** (L2) | **fold**: traceback frames and dump rows join the entry above (one exception = one event) | 1288 lines → 776 events |
| | **redact**: `<PHONE>`, `<EMAIL>`, `<CARD>` (Luhn-checked), tokens — *before* storage, no second chance | 203 phones + 45 emails removed |
| | **fingerprint**: the line's shape gets a stable template id; values collapse to `<NUM>/<DUR>/<STR>`; HTTP status *classes* never merge | 776 events → 94 templates |
| | **link**: `call=UUID` extracted as evidence; unkeyed lines inferred only when exactly ONE session is open, and labelled as assumption | 18% → 41% coverage, 12 traces |
| **③ Store** (L3) | per-minute counts into this project's own SQLite file — physical isolation, raw text never copied | `~/.aegis/projects/<name>/store.db` |
| **④ Detect** (L5) | five arithmetic detectors (spike/drop/novelty/latency/silence) against the project's *own* history; hysteresis on every threshold; zero model calls | 73 signals |
| **⑤ Correlate** (L6) | two-of-four grouping rules → one incident per story; cause = earliest onset, rationale recorded; P4 notes page nobody | 13 incidents + 27 notes |
| **⑥ Judge** (L4 — the moat) | flow spec mined from the project's own traces; purpose steps marked (model or human, human wins); every run: achieved / failed / **hollow** / degraded / unknown | **achieved 4 · failed 2 · hollow 6** |
| **⑦ Remember** (C15) | resolved incidents archive with signature + outcome; matches are "precedent, not conclusion" | 2nd identity failure matched the 1st at 0.545 |
| **⑧ Explain** (L7) | one governed Groq call on click; precedents injected first; every citation checked against evidence or stamped UNVERIFIED; gap reports say what to log when it can't answer | budget + audit in `~/.aegis/audit.jsonl` |
| **⑨ Act** (L8) | failing reproducer FIRST (passes ⇒ diagnosis wrong ⇒ STOP) → minimal gated patch → sandbox proof → draft bundle. Never merges; never writes the target repo | proven live in 2 model calls |
| **⑩ See** (L9) | dashboard (`python3 -m aegis.server <log>`, :8600) and MCP server (`claude mcp add aegis -- python3 -m aegis.mcp_server <log>`) | — |

Hollow — the verdict this system exists for: zero errors, normal teardown,
green on every dashboard, and the run achieved nothing. Half the reference
log's calls. No error-based tool can see it, because there is no error.

## Part 2 — The full vision (aegis-architecture.md)

Same spine, three things bigger:

1. **Many services with real OTel trace ids** → correlation becomes a join;
   a topology map (C5) makes cause-ranking use upstream-ness and answers
   blast radius.
2. **Provider history** → SigNoz/Opik connectors (sockets built,
   fixture-tested) give weeks of seasonal baselines and LLM-span costs.
3. **Prevention** → simulation (C14), T2 auto-merge for trivial fixes after
   earned trust, and the IDE moment: an assistant, via Aegis-MCP, warns you
   *before* you repeat the change that caused two past incidents.

## Part 3 — Completion map

```
COLLECT → NORMALIZE → STORE → DETECT → CORRELATE → JUDGE → REMEMBER → EXPLAIN → ACT → SEE
  ✅         ✅         ✅       ✅         ✅         ✅        ✅          ✅      ✅*    ✅
                                                                        (*draft-only)
```

| Doc phase | Status |
|---|---|
| 0 collect/normalize/store · 2 detect · 4 incidents · 5 connectors · 6 reason+govern · 7 conformance · 8 memory · 9 remediation(T0/T1) · 11 MCP server | ✅ built, tested, on real logs |
| 1 OTel | yours to add in your services; platform already prefers real trace ids |
| 3 topology | needs 2+ services |
| 5 (live) | SigNoz adapter awaits a live instance; Opik = McpProvider + tool map |
| 10 simulation · 12 T2/T3 | need topology / earned trust |

~200 tests across sixteen suites; every phase documented in docs/phases/ with
what was built, what was deliberately not, and the bugs found by running.
