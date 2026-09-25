# Log Intelligence Agent — feature list (v1.0.0)

One command (`log-agent`), one app, any project's log file. Standard library
only. Everything below is built, tested (~225 tests), and verified against
real logs.

## See what's happening
- **Live Brief** — a plain-English story of the current run, updated live
- **App shell** — sidebar app (Overview · Runs · Incidents · Flow · Activity ·
  Improve · Sources), hash-routed, live badges, remembered last view
- **First-run welcome** — attach a log right from the opening screen
- **Live funnel** — lines → events → templates → signals → incidents
- **Timeline + raw log stream**, component flow diagram
- **Ask about this run** — chat grounded in the analysed evidence

## Judge the runs (the differentiator)
- **Run verdicts** — every completed run: *achieved / failed / **hollow** /
  degraded / unknown*. Hollow = finished cleanly, achieved nothing — the
  failure no error-based tool can see
- **Flow spec** — what a run is *supposed* to do, mined from the project's own
  runs, human-editable, purpose steps marked by one model call (your edit wins)

## Detect (pure arithmetic — $0, deterministic, model-free)
- Rate spikes & drops, first-ever-seen templates, latency vs each operation's
  own history, whole-stream silence, **error-ratio storms** (incl. HTTP 5xx
  storms hiding at INFO level) — all with 4-parameter hysteresis (no flapping)
- **Suppression** — mute a known-noisy template; persists, undoable
- Baselines survive restarts, kept per project

## Correlate & remember
- **One incident, not forty alerts** — grouped by trace/time (2-of-4 rules),
  cause ranked by earliest onset with mined **flow context** annotated
- **Precedent memory** — "similar past incident, diagnosis then, outcome:
  worked" — outcome-labelled (wrong diagnoses kept too), always
  *precedent-not-conclusion*
- **Pattern miner** — "this template appears in 6 of 12 incidents"

## Choosing models
- **`AEGIS_PROFILE=dev`** (default) — every AI task runs on Groq's free tier,
  so development and testing cost nothing
- **`AEGIS_PROFILE=prod`** — the same tasks run on paid OpenAI models
- A per-task override beats the profile, so one expensive task can stay cheap
- The UI shows which is active (`free · groq:…` / `PAID · openai:…`), because
  a paid profile should never be a surprise discovered on an invoice

## Choosing models
- **`AEGIS_PROFILE=dev`** (default) — every AI task runs on Groq's free tier,
  so development and testing cost nothing
- **`AEGIS_PROFILE=prod`** — the same tasks run on paid OpenAI models
- A per-task override beats the profile, so one expensive task can stay cheap
- The UI shows which is active (`free · groq:…` / `PAID · openai:…`), because
  a paid profile should never be a surprise discovered on an invoice

## Choosing models
- **`AEGIS_PROFILE=dev`** (default) — every AI task runs on Groq's free tier,
  so development and testing cost nothing
- **`AEGIS_PROFILE=prod`** — the same tasks run on paid OpenAI models
- A per-task override beats the profile, so one expensive task can stay cheap
- The UI shows which is active (`free · groq:…` / `PAID · openai:…`), because
  a paid profile should never be a surprise discovered on an invoice

## Explain & advise (governed AI — Groq free tier default, OpenAI optional)
- One-click **Explain** per incident — every citation checked against the
  evidence or the answer is stamped UNVERIFIED; budget-capped; audited to
  `~/.aegis/audit.jsonl`; precedents injected first
- **Gap reports** — "what should I log next", each with the measurement that
  proves it; pairs with the log-instrumentation skill (SKILL.md)

## Act (draft-only, by construction)
- **Propose fix** — failing reproducer first (passes ⇒ diagnosis wrong ⇒
  stop) → minimal gated patch → sandboxed proof → draft bundle. Never merges;
  never writes your repo; patches touching tests/CI/secrets/deps are rejected

## Integrate
- **MCP server** (`log-agent-mcp <log>`) — overview, incidents, verdicts,
  flow spec, precedent search, topology, blast radius; model tools gated
  behind `--allow-model`
- **Provider connectors** — SigNoz HTTP adapter + a generic MCP-client
  adapter for any vendor's MCP server, behind one canonical interface

## Works on any project
- Proven on 7 log formats: JSON lines, Spring/Java stack traces, nginx,
  syslog, Go logfmt, Rails, Docker-compose — plus loguru/uvicorn/plain
- Per-request correlation from the logs' own ids, safely inferred otherwise

## Production guarantees (each enforced by tests)
- Watched logs opened **read-only** (sha256-verified); your repo never written
- **PII redacted before storage or any model call** (phones, emails,
  Luhn-checked cards, tokens, IPs)
- Per-project isolation: separate SQLite file per project under `~/.aegis/`
- Bounded everything: 10MB backfill cap on huge files, 64KB line cap,
  inode-aware log-rotation handling, archive TTL (500 precedents)
- Corrupt database → quarantined and recreated, never fatal
- Model spend: hard budget + call audit; suites make **zero** network calls
- `/healthz` + `--version`; pip-installable, zero dependencies
