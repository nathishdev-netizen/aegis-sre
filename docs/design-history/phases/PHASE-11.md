# Phase 11 — Aegis as an MCP server (C12 Direction B)

**Status:** complete · **Branch:** `dev-v2` · **Tests:** 10 (`tests/test_aegis_mcp.py`)

---

## What this phase is for, in one sentence

Let other agents and IDEs query what Aegis knows — incidents with evidence,
run verdicts, the flow spec, precedent history — so Aegis is part of a
workflow rather than another dashboard to remember to open.

## Register it

```bash
claude mcp add aegis -- python3 -m aegis.mcp_server /path/to/your.log
# with model explanations enabled (spends governed calls):
claude mcp add aegis -- python3 -m aegis.mcp_server /path/to/your.log --allow-model
```

Then, from any Claude Code session: *"any incidents in my service today?"*,
*"was that call hollow?"*, *"have we seen this failure before?"* — answered
from Aegis's own analysis, with evidence.

## The tools

| Tool | Answers |
|---|---|
| `aegis_overview` | the funnel, verdict counts, incident count, model status |
| `list_incidents` | open/recent incidents with their ranked-cause line |
| `get_incident` | one incident in full: members, why-ranked, timeline, precedents |
| `get_run_verdicts` | every run judged: achieved / failed / **hollow** / degraded |
| `get_flow_spec` | what a run is supposed to do, with CRITICAL marks |
| `query_incident_history` | word-search over archived precedents ("not conclusion") |
| `explain_incident` | *only with `--allow-model`* — one governed, grounded call |

## Doc rules kept

- **Read-heavy, writes gated**: the one spender doesn't even *appear* in
  `tools/list` unless the operator started the server with `--allow-model` —
  a client cannot ask for what is not offered. Calling it anyway while gated
  returns a refusal, tested.
- **No stub tools**: `get_topology`, `simulate_scenario`, `get_blast_radius`
  are in the doc's list but need C5/C14. A tool that answers "not
  implemented" trains callers to distrust the server, so they are absent
  until real.
- A malformed request returns a JSON-RPC error; nothing kills the server.

## Verified live

A scripted stdio session against the real voice-gateway log:

```
initialize → aegis 0.1.0
tools      → 6 (explain absent: gated)
overview   → 1288 lines … 73 signals … 13 incidents · hollow: 6
history("lookup failed identity") → INC-9@18:22:55, INC-10@12:18:41
```

That last line is the doc's payoff scenario working: an agent asking "have we
seen this before?" gets both identity failures back from persistent memory,
each stamped precedent-not-conclusion.
