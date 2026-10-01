# Log Agent + Aegis

Local-first log intelligence. Point it at a running app's log file and it
tells you, in plain English, what happened, what failed, why — and the thing
no error-based tool can see: **which runs completed cleanly and achieved
nothing.**

Two programs in one repo:

| | What | Start |
|---|---|---|
| **v1 — Log Agent** (`app/`) | live single-file watcher with an AI brief | `python3 run.py` → :3000 |
| **v2 — Aegis** (`aegis/`) | the layered platform below | `python3 -m aegis.server <logfile>` → :8600 |

## Aegis in one run

```bash
cp .env.example .env      # GROQ_API_KEY (free tier) is enough; OPENAI optional
python3 -m aegis.server /path/to/your-app.log
```

Against a real voice service's log, the funnel ends in a point:

```
1288 log lines → 776 events → 94 templates → 73 signals → 13 incidents
verdicts: achieved 4 · failed 2 · HOLLOW 6   ← six calls: zero errors,
                                                normal teardown, nothing done
```

## What each layer does

- **Normalize** — fold multi-line dumps, redact PII *before* storage
  (203 phone numbers and 45 emails in the reference log), fingerprint lines
  into countable templates, assemble per-request traces
- **Detect** — spikes, drops, novelty, latency, silence: pure arithmetic,
  hysteresis on every threshold, zero model calls at rest
- **Correlate** — one incident, not forty alerts; cause ranked by earliest
  onset, with the grouping rationale recorded
- **Judge** — a flow spec mined from the project's own traces; every run gets
  a verdict: achieved / failed / **hollow** / degraded / unknown
- **Remember** — resolved incidents become precedents ("seen this before?"),
  outcome-labelled, hint-never-conclusion
- **Explain** — one governed model call (Groq free tier by default), every
  citation checked against the evidence or delivered as UNVERIFIED
- **Advise back** — gap reports: when something couldn't be explained, what
  logging would have let it (pairs with `skill/log-instrumentation/SKILL.md`)
- **Propose fixes** — failing reproducer → minimal patch → draft bundle.
  Never merges; never writes to your repo
- **Integrate** — an MCP server (`python3 -m aegis.mcp_server <log>`) so any
  agent can ask; provider connectors (SigNoz HTTP, any vendor MCP) so Aegis
  can ask others

## The promises, enforced not stated

- Watched logs are opened **read-only** (sha256-verified in the demos)
- Each project's data is a **separate database file** under `~/.aegis/projects/`
- Redaction happens at ingest — the model only ever sees redacted text
- Model spend is budget-capped and audited (`~/.aegis/audit.jsonl`)
- Detection never calls a model; explanation never detects

## Verify everything yourself

```bash
for t in tests/test_*.py; do python3 $t; done     # ~200 tests, no network
python3 -m aegis.demo.phase0 <logfile>            # redaction + funnel
python3 -m aegis.demo.conformance                 # verdicts
python3 -m aegis.demo.remediate                   # the fix loop, sandboxed
```

Design record: `aegis-architecture.md` (the spec) and `docs/phases/` (what
was built per phase, what was deliberately not, and every bug found by
running against real logs).
