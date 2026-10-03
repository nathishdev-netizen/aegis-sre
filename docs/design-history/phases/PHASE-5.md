# Phase 5 — Provider connectors (C2) + MCP as transport (C12 Direction A)

**Status:** built, fixture-verified · **Branch:** `dev-v2` · **Tests:** 9 (`tests/test_aegis_providers.py`)

---

## What this phase is for, in one sentence

Query external observability platforms — SigNoz, Opik, Grafana, anyone with
an MCP server — through one canonical interface, without their schemas
leaking into the rest of the system.

## What exists

- **`TelemetryProvider`** — the canonical interface (`query_logs(LogFilter)`
  → `RawRecord`s). Nothing above this layer knows which vendor answered (P6:
  tested — callers see only `source_id/payload/host/service/collected_at`).
- **`SigNozProvider`** — logs via `POST /api/v5/query_range`, API-key auth,
  with a `QuotaGuard` (providers charge money; over budget = degraded result,
  not a surprise bill during an outage) and a TTL `QueryCache`.
- **`McpClient` + `McpProvider`** — Direction A of C12: any vendor's MCP
  server as the transport *under* the interface, never exposed to agents raw.
  Tool names and reply shapes are explicit configuration (`McpToolMap`) —
  guessing another vendor's tool names would be fabrication.
- Provider records flow through the **same pipeline** as local files — same
  redaction, same fingerprinting. There is no trusted side door (tested with
  PII in a fixture response).

## Verification — and its honest limit

The **MCP client is proven against a real server**: our own Phase 11 server
as a subprocess, full handshake, tools listed, tools called, replies mapped
to canonical records. Same wire any vendor MCP server speaks.

The **SigNoz adapter is fixture-verified only** — no live SigNoz instance
existed at build time. The request shape and response parsing follow the
published v5 API; when a real instance exists, any corrections land in
`aegis/l1_ingestion/providers.py` and nowhere else — that containment is the
point of C2. **Opik**: no dedicated adapter; when its MCP server is
available, `McpProvider` + a `McpToolMap` is the intended path, with an
`LLMTraceAdapter` (query_llm_spans) as follow-on work once real spans exist.

## Not built yet, per the doc's list

`query_traces` / `query_metrics` / `get_service_map` (need trace/metric
consumers — C5 topology first), `WriteAdapter` (write-rare, audited — needs a
reason to exist), `CapabilityMatrix`/hub across multiple simultaneous
providers (one provider at a time is today's reality).
