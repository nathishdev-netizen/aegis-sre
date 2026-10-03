# AEGIS — AI SRE & Log Intelligence Platform
### Complete Architecture and Component Specification

> "Aegis" is a placeholder codename used throughout this document. Replace it with your product name.
>
> This document is **standalone**. It describes one system, with no dependency on or reference to any other project. All examples use a single fictional reference application described in §1.4.

---

## Table of Contents

| § | Section |
|---|---|
| 1 | What this system is, and what it is not |
| 2 | Vocabulary (read this first) |
| 3 | Design principles |
| 4 | Layer map |
| 5 | Component specifications (C1–C17) |
| 6 | Agent architecture |
| 7 | Skill library |
| 8 | MCP layer |
| 9 | Model routing — where MiniMax fits and where it does not |
| 10 | End-to-end technical workflow |
| 11 | Data schemas |
| 12 | Build sequence |
| 13 | Anti-patterns |

---

# 1. What this system is, and what it is not

## 1.1 The one-sentence definition

**Aegis is a system that learns what an application is supposed to do, watches what it actually does, explains the difference in plain language, and — when permitted — repairs the code that caused it.**

## 1.2 The plain-language version

Imagine hiring a very good site-reliability engineer on their first day.

A bad onboarding looks like this: you sit them in front of a screen full of scrolling logs and say "tell me if something looks wrong." They have no idea what normal is. They flag harmless things. They miss real things. They summarise text back to you that you could have read yourself.

A good onboarding looks like this. First, they read the codebase and write down how the system is *supposed* to work — the order of operations, which service calls which, how long each step should take. Then they watch. Because they know the intended shape, they notice when reality departs from it — including departures that throw no error at all. When something breaks, they don't read you the error message; they tell you the earliest point where reality diverged from the plan. And eventually, they open a pull request that fixes it, with the evidence attached.

**Aegis is the second version of that engineer, as software.**

## 1.3 What it should be — and what it should not be

| Aegis **should** be | Aegis should **not** be |
|---|---|
| A layer that sits **on top of** existing observability data | Another metrics database competing with Prometheus/ClickHouse |
| Opinionated about *meaning* — what happened and why | Opinionated about *storage* — where bytes live |
| Mostly deterministic, with LLMs at the edges | An "LLM reads all logs" system |
| A producer of **incidents** (few, high-signal) | A producer of **alerts** (many, low-signal) |
| Able to say "step 4 never ran" | Limited to saying "an exception was thrown" |
| Vendor-neutral through adapters | Hard-wired to one telemetry vendor |
| Safe by default: suggests, then acts with permission | An autonomous agent with write access to production |
| Cheap at rest, expensive only during incidents | Uniformly expensive |

**The single most important line in this document:**

> Detection is a statistics problem. Explanation is a language problem. Never solve the first with the second.

Almost every failed "AI for logs" product violates that line. It sends log text to a language model and asks "is this bad?" That is slow, expensive, non-deterministic, and worse at the job than a counter.

## 1.4 The reference application used in all examples

Every example below refers to **ShopFlow**, a fictional online ordering system. It is deliberately ordinary.

```
                      ┌──────────────┐
   customer  ───────► │   gateway    │  :8080   (nginx / API gateway)
                      └──────┬───────┘
                             │
                      ┌──────▼───────┐
                      │  order-svc   │  :8081
                      └──┬────┬───┬──┘
              ┌──────────┘    │   └──────────┐
     ┌────────▼──────┐ ┌──────▼──────┐ ┌─────▼────────┐
     │ inventory-svc │ │ payment-svc │ │  notify-svc  │
     │    :8082      │ │   :8083     │ │    :8084     │
     └───────┬───────┘ └──────┬──────┘ └──────────────┘
             │                │
        ┌────▼────┐      ┌────▼─────────────┐
        │postgres │      │ external payment │
        │  :5432  │      │    provider      │
        └─────────┘      └──────────────────┘
```

The intended business flow for placing an order:

1. Request arrives at `gateway`
2. `order-svc` validates the cart
3. `inventory-svc` reserves stock
4. `payment-svc` authorises the card
5. `order-svc` writes the order record
6. `notify-svc` sends a confirmation email

Keep this diagram in mind. Every component below is illustrated against it.

---

# 2. Vocabulary (read this first)

The rest of the document is unreadable without these ten words. Each has a plain definition and a ShopFlow example.

**Log line** — one text record emitted by a service.
> `2026-09-05T10:14:22Z inventory-svc ERROR reserve failed for sku A19: insufficient stock`

**Template (or fingerprint)** — the reusable shape of a log line with the variable parts removed. Thousands of lines collapse into a handful of templates.
> `<TS> inventory-svc ERROR reserve failed for sku <STR>: <MSG>`

**Span** — a timed unit of work with a name, start, duration, and status.
> `inventory.reserve` — 12ms — OK

**Trace** — the tree of all spans belonging to one request, tied together by a shared `trace_id`.
> One customer's checkout, spanning all five services.

**Trace ID** — the identifier propagated from the first service to the last so that everything belonging to one request can be joined.

**Topology** — the graph of which service depends on which.
> `order-svc → inventory-svc → postgres`

**Flow spec** — a written declaration of what *should* happen for a given operation, in what order, within what time. Generated from the codebase, editable by a human.

**Conformance** — the act of comparing an actual trace against a flow spec.

**Anomaly** — a statistically detected departure from baseline. Produced by arithmetic, not by a model.

**Incident** — one real-world problem, with all its symptoms grouped underneath it. The unit humans care about.

**Agent** — an LLM given a goal, a set of tools, and permission to loop until it reaches a conclusion.

**Tool** — a single callable function an agent may invoke (`query_traces`, `read_file`, `run_tests`).

**Skill** — a written procedure that tells an agent *how* to do a recurring task well. Not code, not a tool — instructions plus checklists plus examples.

**MCP (Model Context Protocol)** — a standard way of exposing tools to an agent over a network connection. It is how Aegis talks to third-party systems, and how third-party systems talk to Aegis.

---

# 3. Design principles

These are the rules that resolve arguments later. Each one exists because violating it produces a specific, predictable failure.

### P1 — Deterministic first, LLM last
Every piece of work that can be done by counting, matching, or graph traversal must be done that way. LLMs enter only when the task is genuinely linguistic: explaining, hypothesising, writing code.
*Violation produces:* a $9,000/month bill and non-reproducible alerts.

### P2 — Detection is statistics, explanation is language
A threshold decides *whether* to speak. A model decides *what* to say.
*Violation produces:* alerts that fire differently on identical input.

### P3 — One incident, not forty alerts
A single root cause produces symptoms in every downstream service. Grouping them is not a nice-to-have; it is the difference between a tool people keep and a tool people mute.
*Violation produces:* alert fatigue, then abandonment.

### P4 — Report the earliest deviation, not the loudest error
The service that screams is rarely the service that failed.
*Violation produces:* engineers debugging the wrong service for an hour.

### P5 — Every automated action ships with its evidence
No patch, no alert, no claim without the trace, the numbers, and the reasoning attached.
*Violation produces:* nobody trusts the output, so nobody uses it.

### P6 — Providers are adapters, never assumptions
No vendor's schema may leak past the connector boundary.
*Violation produces:* a rewrite when you add the second provider.

### P7 — Privacy and cost are architecture, not features
Redaction happens at ingest, before any model boundary. Spend limits are enforced by the runtime, not by prompt instructions.
*Violation produces:* a data-protection incident, or a runaway bill during an outage — usually both at once.

### P8 — Degrade, never block
If the LLM layer is down, detection still works. If a provider is unreachable, local collection still works. Intelligence is an enhancement layer, not a dependency.

---

# 4. Layer map

```
┌────────────────────────────────────────────────────────────────────┐
│ L9  INTERFACE        Web UI · Incident chat · Aegis MCP server     │
├────────────────────────────────────────────────────────────────────┤
│ L8  ACTION           Remediation agent · Simulation · Notifier     │
├────────────────────────────────────────────────────────────────────┤
│ L7  REASONING        Agent runtime · Skills · Model router         │
├────────────────────────────────────────────────────────────────────┤
│ L6  CORRELATION      Incident manager · Grouping · Ranking         │
├────────────────────────────────────────────────────────────────────┤
│ L5  DETECTION        Statistical detectors · Threshold engine      │
├────────────────────────────────────────────────────────────────────┤
│ L4  UNDERSTANDING    Topology mapper · Project analyzer ·          │
│                      Flow spec store · Conformance engine          │
├────────────────────────────────────────────────────────────────────┤
│ L3  STORAGE          Event store · Metric store · Vector store     │
├────────────────────────────────────────────────────────────────────┤
│ L2  NORMALIZATION    Parser · Redactor · Fingerprinter · Enricher  │
├────────────────────────────────────────────────────────────────────┤
│ L1  INGESTION        Local collectors · Provider connectors        │
├────────────────────────────────────────────────────────────────────┤
│ L0  SOURCES          stdout · files · ports · OTel · SigNoz · Opik │
└────────────────────────────────────────────────────────────────────┘

                    GOVERNANCE  (cuts across L1–L9)
        Redaction · Cost budgets · Autonomy tiers · Audit log
```

**Read the map this way:** data flows upward and gets smaller at every step. Millions of log lines (L0) become thousands of templates (L2), become dozens of anomalies (L5), become one or two incidents (L6), become a single explanation (L7), become one pull request (L8).

That funnel *is* the product. If volume is not shrinking by an order of magnitude at each layer, a layer is broken.

---

# 5. Component specifications

Each component below is specified with: purpose, an analogy, what it should be, what it should **not** be, its sub-components, its internal flow, and a worked ShopFlow example.

---

## C1 — Source Adapter Layer

### Purpose
Get raw signal off machines and into the pipeline, reliably, without losing data and without the rest of the system needing to know where it came from.

### Analogy
The microphones. They do not decide what is interesting; they only make sure nothing said in the room is missed.

### Should be
- **Dumb and reliable.** Read, tag, forward. No parsing, no judgement.
- **Back-pressure aware.** If downstream is slow, buffer to disk and keep going.
- **At-least-once delivery** with de-duplication downstream.
- **Low overhead.** Under 2% CPU on the host it monitors.

### Should NOT be
- Not a filter. Filtering decisions belong in L2 where they are visible and configurable.
- Not a parser. A collector that understands log formats becomes a collector you must redeploy every time a format changes.
- Not stateful beyond a read offset.

### Sub-components

| Sub-component | Job |
|---|---|
| `StdoutTailer` | Attaches to a process's stdout/stderr |
| `FileTailer` | Follows log files, survives rotation, tracks byte offset |
| `PortProbe` | Health-checks and latency-samples a TCP/HTTP port |
| `OTelReceiver` | Accepts OTLP traces/metrics/logs pushed by instrumented apps |
| `ContainerWatcher` | Discovers new containers and auto-attaches |
| `SocketInspector` | Periodically snapshots open sockets (for topology, see C5) |
| `BufferQueue` | Disk-backed queue with a bounded retry policy |
| `SourceRegistry` | The list of what is being watched, with health per source |

### Flow

```
1. Registry declares a source        → { id, type, target, labels }
2. Adapter starts, records offset    → resume point on restart
3. Raw record read                   → bytes + arrival timestamp
4. Envelope attached                 → source_id, host, service, collected_at
5. Pushed to BufferQueue
6. BufferQueue → Normalizer (L2)
7. Offset committed only after ack   → no silent data loss
```

### Example
You point Aegis at ShopFlow's host. `ContainerWatcher` finds six containers. `SourceRegistry` now holds six entries. `inventory-svc` writes a line to stdout; `StdoutTailer` wraps it in an envelope tagging `service=inventory-svc, host=web-01` and hands it on. It does not know or care that the line says "ERROR".

### Interface
```python
class SourceAdapter(Protocol):
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def health(self) -> SourceHealth: ...
    # emits: RawRecord(envelope, payload_bytes)
```

---

## C2 — Provider Connector Layer

### Purpose
Let Aegis query external observability platforms (SigNoz, Opik, Grafana, Langfuse, Datadog, Jaeger) as if they were local, without their schemas leaking into the rest of the system.

### Analogy
The collectors in C1 are microphones in the room right now. The connectors are the archive room downstairs — you go there to ask what happened last Tuesday, and to see the parts of the building your microphones don't cover.

### Should be
- A **narrow, uniform interface** that all providers implement.
- **Read-heavy, write-rare.** Reads are free and frequent; writes (creating alert rules, annotating deploys) are deliberate and audited.
- **Cached and rate-limited.** Providers charge money and enforce quotas.
- **Degradable.** A dead provider must not stall detection.

### Should NOT be
- Not a mirror. Do not copy the provider's entire dataset into Aegis. Query on demand.
- Not the place for vendor-specific logic. If your anomaly detector contains the word "SigNoz", the abstraction has failed.
- Not a write path for production changes.

### Sub-components

| Sub-component | Job |
|---|---|
| `ProviderRegistry` | Configured providers, credentials, capabilities |
| `CapabilityMatrix` | Which provider supports which operation |
| `TraceQueryAdapter` | `query_traces()` per provider |
| `MetricQueryAdapter` | `query_metrics()` per provider |
| `LLMTraceAdapter` | `query_llm_spans()` — Opik, Langfuse, Phoenix |
| `ServiceMapAdapter` | `get_service_map()` — feeds topology (C5) |
| `WriteAdapter` | `create_alert_rule()`, `annotate()` — audited |
| `ResponseNormalizer` | Vendor JSON → canonical Aegis types |
| `QueryCache` | TTL cache keyed on query hash |
| `QuotaGuard` | Per-provider rate and spend ceiling |

### The canonical interface (this is the important part)

```python
class TelemetryProvider(Protocol):
    name: str
    capabilities: set[Capability]

    def query_traces(self, filter: TraceFilter) -> list[Trace]: ...
    def query_metrics(self, filter: MetricFilter) -> list[Series]: ...
    def query_logs(self, filter: LogFilter) -> list[Event]: ...
    def get_service_map(self, window: TimeRange) -> ServiceGraph: ...
    def query_llm_spans(self, filter: LLMFilter) -> list[LLMSpan]: ...   # optional
    def create_alert_rule(self, rule: AlertRule) -> RuleRef: ...          # optional
```

`SigNozProvider`, `OpikProvider`, `GrafanaProvider` are all implementations. Nothing above this layer knows which one answered.

### Flow

```
1. Caller asks:  provider_hub.query_traces(service="order-svc", window=1h, status=error)
2. Hub consults CapabilityMatrix → which providers can answer?
3. QueryCache checked            → hit? return
4. QuotaGuard checked            → over budget? return degraded result + warning
5. Adapter translates to vendor query
        SigNoz  → ClickHouse SQL over its API
        Opik    → REST/MCP call with its filter DSL
6. Response → ResponseNormalizer → canonical Trace objects
7. Cached, returned, logged for audit
```

### Example
An anomaly fires on `order-svc` at 14:32. The reasoning agent asks three questions through one interface:

- `query_metrics(service="order-svc", metric="latency.p95", window=30d)` → SigNoz answers → baseline is 240ms, current is 3.1s.
- `query_traces(service="order-svc", status=error, window=10m)` → SigNoz answers → 74 failing traces, 71 of them contain a span from `payment-svc`.
- `query_llm_spans(window=10m)` → Opik answers → the fraud-check LLM call is retrying 6× per request, tokens up 11×.

Three vendors, one interface, one coherent picture. Nothing above C2 knew there were three vendors.

### Why both local collectors *and* connectors

| | Local collectors (C1) | Provider connectors (C2) |
|---|---|---|
| Latency | Milliseconds | Seconds |
| History | Minutes to hours | Weeks to months |
| Detail | Full raw text | Sampled, structured |
| Coverage | Machines you control | Everything already instrumented |
| Cost | Free | Metered |

Detection runs on C1 for speed. Investigation uses C2 for context. Both are required.

---

## C3 — Normalizer & Fingerprint Engine

### Purpose
Turn arbitrary text into a small, countable vocabulary — and remove sensitive data before anything else in the system sees it.

### Analogy
A librarian receiving a truckload of loose paper. Every page is stamped, stripped of personal details, and filed under one of a few hundred categories. Nobody downstream reads pages; they read category counts.

### Should be
- **Fast.** Tens of thousands of lines per second per core.
- **Redacting first.** Redaction happens *before* storage, before embeddings, before any model call. There is no second chance.
- **Self-learning.** New templates are discovered automatically, not configured by hand.
- **Stable.** The same line must always yield the same template ID.

### Should NOT be
- Not a summariser. This layer produces no prose.
- Not an LLM. Fingerprinting is a string algorithm (Drain, Spell, or similar), not a model call.
- Not lossy for the raw payload — keep the original (redacted) text; store the template ID beside it.

### Sub-components

| Sub-component | Job |
|---|---|
| `FormatDetector` | JSON, logfmt, plain text, syslog |
| `FieldExtractor` | Pull timestamp, level, service, trace_id |
| `Redactor` | Emails, card numbers, tokens, names, IPs → typed placeholders |
| `Fingerprinter` | Drain-style tree → template ID |
| `TemplateStore` | template_id → pattern, first_seen, count, example |
| `NoveltyFlag` | Marks the *first* occurrence of any new template |
| `Enricher` | Attaches deploy version, environment, host metadata |
| `TraceLinker` | Attaches trace_id where present; infers where absent |

### Flow

```
raw line
   │
   ▼ FormatDetector      → "JSON"
   ▼ FieldExtractor      → ts, level=ERROR, service=inventory-svc, trace_id=4f2a…
   ▼ Redactor            → "customer j***@***.com" → "customer <EMAIL>"
   ▼ Fingerprinter       → template_id = T-0912  (new? → NoveltyFlag)
   ▼ Enricher            → deploy=v2.14.3, env=prod
   ▼ Event emitted       → { ts, service, level, template_id, trace_id, text, fields }
```

### Example

400 lines arrive in 10 seconds:

```
reserve failed for sku A19: insufficient stock
reserve failed for sku B42: insufficient stock
reserve failed for sku C88: insufficient stock
... ×397
```

The fingerprinter collapses all 400 into **one** template, `T-0912`, with `count=400`. Downstream, the detection engine sees a single row:

```
T-0912  "reserve failed for sku <STR>: insufficient stock"
        count=400  window=10s  baseline=0.2/min  →  2000× baseline
```

That is the entire cost of noticing this problem: one row, one comparison, zero model calls.

### The novelty signal — worth its own paragraph
When `T-0913` appears for the very first time in the system's history, that fact alone is worth surfacing. A log line nobody has ever seen before usually means code took a path nobody has taken before. It is the cheapest high-value signal in the entire platform, and it requires no baseline, no training, and no model.

---

## C4 — Telemetry Store

### Purpose
Hold enough recent data to detect, investigate, and explain — and nothing more.

### Analogy
A kitchen, not a warehouse. You keep what you'll cook with this week close at hand, and send everything else to cold storage.

### Should be
- **Tiered.** Hot (minutes, in-memory), warm (days, columnar on disk), cold (delegated to providers via C2).
- **Time-partitioned** with automatic expiry.
- **Queryable by trace_id, service, template_id, and time.** Those four cover 95% of access.
- **Small.** Aegis stores derived data heavily and raw data lightly.

### Should NOT be
- Not a replacement for SigNoz/Loki/ClickHouse. Aegis is not in the log-retention business.
- Not the system of record for compliance retention.
- Not unbounded. Every table has a TTL, enforced.

### Sub-components

| Sub-component | Job |
|---|---|
| `HotRing` | In-memory ring buffer, last 5–15 minutes, feeds detectors |
| `WarmStore` | Columnar (ClickHouse/DuckDB), 7–30 days of events |
| `MetricSeries` | Per-template and per-service counters, rates, percentiles |
| `TraceIndex` | trace_id → spans, for fast assembly |
| `VectorStore` | Embeddings for incident memory (C15) |
| `Retention` | TTL enforcement and downsampling |

### Flow
```
Event → HotRing (always)
      → WarmStore (batched every 5s)
      → MetricSeries (counters updated in-place)
      → TraceIndex (if trace_id present)

Detectors read:      HotRing + MetricSeries      (fast path)
Investigation reads: WarmStore + C2 providers    (slow path)
```

### Example
At 14:32, a detector needs "how many `T-0912` in the last 60 seconds versus the same minute over the last 14 days." The first half comes from `MetricSeries` in under a millisecond. The second half comes from a pre-computed baseline table, also sub-millisecond. No provider call, no scan.

---

## C5 — Topology Mapper

### Purpose
Know which service depends on which, so that when five things break at once, Aegis can say which one broke *first* and which four are victims.

### Analogy
A hospital where cardiology, radiology, and the pharmacy each keep separate notebooks. Every department holds a piece of the story and nobody holds the patient. The topology map is the patient chart.

### Should be
- **Multi-source.** Built from code, from runtime, and from traces, then merged.
- **Confidence-scored.** Each edge carries how it was discovered and how sure we are.
- **Continuously refreshed.** Topology drifts; a stale map produces confident wrong answers.
- **Directional.** `A → B` means A calls B. Direction is what makes cause-ranking possible.

### Should NOT be
- Not hand-maintained. A diagram someone drew in 2024 is worse than no diagram.
- Not inferred from a single method. Each discovery method has blind spots.
- Not limited to services you own. External dependencies (payment provider, S3) are nodes too.

### Sub-components

| Sub-component | Job |
|---|---|
| `StaticDiscoverer` | Parses code, env vars, compose files, nginx conf, k8s manifests |
| `RuntimeDiscoverer` | Snapshots open sockets (`ss`, `lsof`) → real connections |
| `TraceDiscoverer` | Derives edges from parent/child spans (most reliable) |
| `CorrelationDiscoverer` | Statistical coupling for un-instrumented services |
| `GraphMerger` | Reconciles the four into one weighted graph |
| `GraphStore` | Versioned graph with change history |
| `BlastRadiusCalculator` | Given an unhealthy node, list affected nodes and flows |
| `DriftDetector` | Alerts when a new edge appears that isn't in the code |

### The four discovery methods, ranked

| Method | How | Reliability | Blind spot |
|---|---|---|---|
| **Trace-based** | Shared `trace_id`, parent span → child span | Highest | Needs instrumentation |
| **Static** | `INVENTORY_URL=http://inventory-svc:8082` in config | High | Misses dynamic/runtime targets |
| **Runtime sockets** | `order-svc` PID holds a connection to `:8082` | Medium | Shows connections, not intent |
| **Temporal correlation** | 8082 errors rise, 8081 errors rise 200ms later, always | Low | Correlation, not causation |

Use all four. Merge with confidence weights. Trace-based edges override the rest.

### Flow

```
On connect:
1. StaticDiscoverer parses repo + configs      → candidate edges
2. RuntimeDiscoverer snapshots sockets         → observed edges
3. TraceDiscoverer reads recent traces (C2)    → confirmed edges
4. GraphMerger reconciles                      → weighted ServiceGraph
5. Human is shown the graph and can correct it
6. GraphStore versions it

Continuously:
7. TraceDiscoverer updates edge weights every 5 minutes
8. DriftDetector flags unexpected new edges
```

### Example — the "can we combine these?" moment

You add `order-svc` (:8081) as a monitored source. Aegis runs discovery and reports:

> **`order-svc` has 3 dependencies, and you already have access to all of them.**
> `order-svc → inventory-svc (:8082)` — confirmed by traces, confidence 0.98
> `order-svc → payment-svc (:8083)` — confirmed by traces, confidence 0.97
> `order-svc → postgres (:5432)` — from config + sockets, confidence 0.85
>
> **Combine into a single monitored group `checkout-cluster`?** Anomalies will then be evaluated across all four together, and root-cause ranking will be enabled.

Say yes, and the behaviour changes materially:

**Before grouping** — four independent alerts:
```
14:32:01  order-svc      ERROR RATE HIGH   (47/min, baseline 0.5)
14:32:01  gateway        502 RATE HIGH     (44/min, baseline 0)
14:32:02  payment-svc    LATENCY HIGH      (p95 8.1s, baseline 300ms)
14:32:04  notify-svc     QUEUE DEPTH HIGH  (1,204, baseline 12)
```
Four pages. Four engineers. Nobody knows where to start.

**After grouping** — one incident:
```
INCIDENT #418  ·  checkout-cluster degraded  ·  4 services affected

  ROOT (most upstream unhealthy node):
    payment-svc — p95 latency 8.1s (27× baseline) from 14:31:58

  DOWNSTREAM SYMPTOMS (suppressed as separate alerts):
    order-svc     error rate ↑   — waits on payment-svc, 5s timeout exceeded
    gateway       502s ↑         — waits on order-svc
    notify-svc    queue depth ↑  — no confirmations to send

  BLAST RADIUS: checkout flow DOWN · search flow UNAFFECTED
```

**The rule that produced this:** when multiple nodes in a connected group are unhealthy simultaneously, the node furthest *upstream* is the prime suspect and everything downstream of it is presumed symptomatic. That one heuristic resolves the majority of multi-alert storms.

---

## C6 — Project Analyzer & Flow Spec Generator

### Purpose
On connection, read the codebase and write down what the system is *supposed* to do — as a machine-checkable specification.

### Analogy
Before a QA engineer watches the production line, someone hands them the assembly manual. Everything they see afterwards is judged against it. Without the manual they're just a person watching a conveyor belt with an opinion.

### Should be
- **Run once on connect, refreshed on merge to main.**
- **Output human-editable YAML** stored in the repository, versioned with the code.
- **Honest about confidence.** Mark auto-derived steps as `confidence: 0.7` so humans know what to review.
- **Incremental.** On the second run, only re-analyse changed files.
- **Bounded.** Analyse entrypoints and the call graph reachable from them, not every utility function.

### Should NOT be
- Not a one-shot "send the whole repo to an LLM" job. Large repos exceed any context window and the output is unverifiable.
- Not fully automatic. Auto-extraction reaches roughly 70–85%; a human closes the gap in minutes.
- Not a static snapshot. A flow spec that isn't updated becomes a source of false alarms, which is worse than having none.

### Sub-components

| Sub-component | Job |
|---|---|
| `RepoIndexer` | Walks the repo, builds a file/symbol index |
| `EntrypointDetector` | Finds HTTP routes, queue consumers, cron jobs, CLI commands, event handlers |
| `CallGraphBuilder` | Static analysis (AST / tree-sitter / LSP) — who calls what |
| `ExternalCallDetector` | HTTP clients, DB queries, queue publishes, third-party SDKs |
| `ErrorPathAnalyzer` | try/except, retries, timeouts, fallbacks, circuit breakers |
| `FlowSynthesizer` | **LLM step** — turns the call graph into a readable flow spec |
| `TimingEstimator` | Derives expected durations from historical traces (C2), not from guesses |
| `SpecStore` | Versioned flow specs, diffable, PR-reviewable |
| `SpecValidator` | Rejects specs that reference unknown services or contradict topology |

### Flow

```
 1. RepoIndexer          → 412 files, 3,180 symbols
 2. EntrypointDetector   → 24 HTTP routes, 3 consumers, 2 cron jobs
 3. For each entrypoint:
 4.   CallGraphBuilder   → reachable call tree (deterministic, no LLM)
 5.   ExternalCallDetector → which hops leave the process
 6.   ErrorPathAnalyzer  → what is guarded, what is not
 7.   FlowSynthesizer    → LLM converts the tree into a flow spec  ◄── only LLM step
 8.   TimingEstimator    → fills expected durations from real traces
 9. SpecValidator        → cross-check against topology (C5)
10. Written to  .aegis/flows/*.yaml  →  opened as a PR for human review
```

Note step 4: the call graph is built **deterministically**. The LLM's job in step 7 is only to name the flow, group steps meaningfully, and mark which steps are business-critical. This keeps the expensive, hallucination-prone part small and reviewable.

### Example — generated flow spec

```yaml
flow: place_order
version: 3
source: auto-generated + human-edited
entrypoint:
  method: POST
  path: /api/orders
  service: gateway

steps:
  - id: validate_cart
    service: order-svc
    span: order.validate
    required: true
    max_duration_ms: 200
    confidence: 0.95

  - id: reserve_inventory
    service: inventory-svc
    span: inventory.reserve
    required: true
    max_duration_ms: 500
    critical: true
    note: "Must occur before payment. Skipping this oversells stock."
    on_failure: abort_flow
    confidence: 0.92

  - id: authorize_payment
    service: payment-svc
    span: payment.authorize
    required: true
    max_duration_ms: 3000
    depends_on: [reserve_inventory]
    external: true
    retry: {max: 2, backoff_ms: 500}
    confidence: 0.90

  - id: persist_order
    service: order-svc
    span: order.persist
    required: true
    max_duration_ms: 150
    confidence: 0.94

  - id: send_confirmation
    service: notify-svc
    span: notify.email
    required: false            # async, failure is not fatal
    max_duration_ms: 5000
    confidence: 0.88

invariants:
  - id: inv_before_pay
    rule: "reserve_inventory.end < authorize_payment.start"
    severity: critical
  - id: no_orphan_charge
    rule: "authorize_payment.success implies persist_order.success"
    severity: critical

expected_rate:
  business_hours: 40-120/min
  overnight: 2-15/min
```

Two things to notice. First, this is readable by a product manager, not just an engineer. Second, `invariants` encode business rules that no exception handler enforces — and those are what the next component checks.

---

## C7 — Flow Conformance Engine

### Purpose
Compare what actually happened against what the flow spec says should have happened, and flag the difference — **including differences that produced no error at all**.

This is the component that makes Aegis different from every log tool on the market. Everything else is table stakes.

### Analogy
An air-traffic controller doesn't wait for a crash. They have a filed flight plan and a radar track, and they raise the alarm the moment the aircraft is somewhere the plan doesn't say it should be — long before anything goes wrong.

### Should be
- **Streaming.** Evaluate traces as they complete, within seconds.
- **Tolerant.** Optional steps, valid branches, and known variations must not produce noise.
- **Rate-based, not instance-based.** One deviating trace is a curiosity. Two hundred is an incident.
- **Explicit about deviation type.** "Missing step" and "slow step" require different responses.

### Should NOT be
- Not a hard gate. Conformance observes; it never blocks or rejects real traffic.
- Not noisy on first deployment. Start in shadow mode, learn the real branch distribution for a week, then start alerting.
- Not a replacement for error monitoring — it is the layer *above* it.

### Deviation types (the complete taxonomy)

| Type | Meaning | ShopFlow example |
|---|---|---|
| `MISSING_STEP` | A required step never ran | `reserve_inventory` absent, order still succeeded |
| `EXTRA_STEP` | An unexpected operation appeared | `payment.authorize` called twice in one trace |
| `OUT_OF_ORDER` | Steps ran in the wrong sequence | Payment authorised before inventory reserved |
| `TIMING_VIOLATION` | Step exceeded declared bound | `reserve_inventory` took 4.2s against a 500ms bound |
| `BRANCH_SHIFT` | A branch's frequency changed sharply | Fallback path taken 100% of the time, baseline 3% |
| `INVARIANT_BREACH` | A declared business rule was violated | Payment succeeded, order record never written |
| `PREMATURE_END` | Trace stopped mid-flow with no error | Trace ends after step 3, no exception logged |
| `RATE_ANOMALY` | Flow executing far above or below expected rate | 4 orders/min at 11am, expected 40–120 |

### Sub-components

| Sub-component | Job |
|---|---|
| `TraceAssembler` | Gathers all spans/logs for one trace_id, waits for completion or timeout |
| `SpecMatcher` | Picks which flow spec this trace belongs to |
| `SequenceChecker` | Order and presence of steps |
| `TimingChecker` | Duration bounds per step and end-to-end |
| `InvariantChecker` | Evaluates declared business rules |
| `BranchProfiler` | Learns and tracks the normal distribution across branches |
| `DeviationAggregator` | Groups individual deviations into rate-based signals |
| `ShadowMode` | Records deviations without alerting during the learning period |

### Flow

```
1. TraceAssembler collects spans for trace_id 4f2a…
   (completion = root span closed, or 30s timeout)
2. SpecMatcher: entrypoint POST /api/orders → flow "place_order" v3
3. SequenceChecker  → observed [validate, reserve, authorize, persist, notify]
                      expected [validate, reserve, authorize, persist, notify]  ✓
4. TimingChecker    → reserve took 620ms, bound 500ms  ⚠ TIMING_VIOLATION
5. InvariantChecker → inv_before_pay ✓ · no_orphan_charge ✓
6. BranchProfiler   → no branches taken, normal
7. DeviationAggregator: 1 timing violation in this trace.
                      Over the last 5 min: 340 of 400 traces show the same.
                      → escalate as a signal to Detection (C8)
```

### Example 1 — the silent failure that no error monitor catches

```
Trace 9c14…  flow: place_order  status: HTTP 201 CREATED  ✅

  ✓ validate_cart        order-svc        42ms
  ✗ reserve_inventory    —                MISSING
  ✓ authorize_payment    payment-svc      890ms
  ✓ persist_order        order-svc        61ms
  ✓ send_confirmation    notify-svc       1.2s
```

**Zero exceptions. HTTP 201. The customer got a confirmation email. Every error-based monitor on earth reports this system as perfectly healthy.**

Aegis reports:

> **CRITICAL — Invariant breach, flow `place_order`**
> `reserve_inventory` (marked `critical: true`) did not execute in 1,847 of 1,850 orders over the last 22 minutes.
> Declared consequence: *"Must occur before payment. Skipping this oversells stock."*
> Business impact: approximately 1,847 orders taken against stock that was never reserved.
> Earliest affected trace: 14:09:33. Correlates with deploy `v2.14.3` at 14:08:51.

That is the product in one screenshot.

### Example 2 — misleading errors, corrected

The loud symptom:
```
16:04:12  notify-svc  ERROR  email send failed: template render error, order_total is null
```
An engineer reading logs goes to `notify-svc` and starts debugging the email template. Wrong service, wrong file, forty minutes lost.

Aegis reads the same trace against the spec:
```
Trace 2b81…  flow: place_order

  ✓ validate_cart        42ms
  ✓ reserve_inventory    180ms
  ✓ authorize_payment    910ms
  ⚠ persist_order        PREMATURE_END — span opened, never closed, no error logged
  ✗ send_confirmation    ERROR (template render: order_total is null)
```
And reports:

> **The email failure is a symptom, not the cause.**
> Earliest deviation: `persist_order` (step 4) opened at 16:04:11 and never completed. The order record was never written, so `order_total` was null when step 5 read it.
> Also: invariant `no_orphan_charge` breached — payment succeeded but no order record exists. **114 customers have been charged for orders that do not exist.**
> Investigate `order-svc` persistence, not the notification template.

**This is principle P4 in action: earliest deviation, not loudest error.** Note that it also surfaced a far more serious problem than the one that was reported.

### Example 3 — branch shift, the invisible degradation

A service has an LLM-based classifier with a fallback path for when the API errors. The API key expires. The code catches the exception and quietly returns the fallback. Nothing errors. Latency is *better*. Every conventional monitor is green.

```
BRANCH_SHIFT — flow: classify_request, step: intent_classification
  Branch "llm_path"      baseline 96%  →  now 0%
  Branch "default_path"  baseline  4%  →  now 100%
  Held for 38 minutes across 4,102 requests.
```
Every request has been silently handled by the dumb fallback for over half an hour. This class of failure is *only* detectable by comparing behaviour to a declared expectation.

---

## C8 — Detection Engine

### Purpose
Decide, cheaply and deterministically, whether something is worth a human's or a model's attention. Nothing above this layer runs until this layer says so.

### Analogy
A hospital monitor does not narrate every heartbeat. It counts silently and speaks only when the rate leaves a band. Summarising every log line is the equivalent of a nurse reading every heartbeat aloud — technically thorough, practically useless, and it drowns out the alarm.

### Should be
- **Free at rest.** Zero model calls when the system is healthy.
- **Baseline-aware and seasonal.** Traffic at 3am is not traffic at 10am Monday.
- **Hysteretic.** Fire after a condition holds for N seconds; clear only after it drops below a *lower* threshold.
- **Composable.** Multiple detector types running side by side over the same stream.

### Should NOT be
- Not LLM-driven. If any part of detection calls a model, that part is misplaced.
- Not fixed-threshold. Static thresholds either scream every morning or miss everything.
- Not per-line. Detection operates on aggregates over templates and flows.

### The detector catalogue

| Detector | Watches | Fires when | ShopFlow example |
|---|---|---|---|
| `RateSpike` | Template counts | Rate ≫ seasonal baseline | `T-0912` at 2000× baseline |
| `RateDrop` | Template/flow counts | Rate ≪ baseline | Zero orders for 6 min at 2pm |
| `NoveltyDetector` | Template store | Template never seen before | New DB driver exception |
| `LatencyShift` | p50/p95/p99 | Percentile beyond baseline band | p95 240ms → 3.1s |
| `ErrorRatio` | Success ÷ attempt | Ratio drops past floor | 0.98 → 0.31 |
| `CardinalityShift` | Distinct affected entities | Many distinct entities failing | 400 distinct users, not 3 |
| `ConformanceRate` | C7 deviations | Deviation rate crosses threshold | 92% of traces missing a step |
| `SaturationDetector` | Queues, pools, threads | Utilisation past safe band | Connection pool 100% for 90s |
| `CostAnomaly` | Token/API spend | Spend per unit of work spikes | Tokens/order up 11× |
| `SilenceDetector` | Any source | Expected source stops emitting | `inventory-svc` silent for 3 min |

`RateDrop` and `SilenceDetector` deserve emphasis: **the absence of logs is a signal**. A crashed consumer produces silence, not errors, and naive systems interpret silence as health.

### Sub-components

| Sub-component | Job |
|---|---|
| `BaselineStore` | Seasonal baselines: per template, per hour-of-day, per day-of-week |
| `BaselineLearner` | EWMA update, outlier-resistant, excludes past incident windows |
| `WindowManager` | Tumbling and sliding windows over the hot ring |
| `DetectorRegistry` | Enabled detectors and their parameters |
| `HysteresisGate` | Sustain-and-clear logic to stop flapping |
| `SuppressionRules` | Maintenance windows, known-noisy templates, deploy pauses |
| `SignalEmitter` | Emits a typed `Signal` to the Incident Manager |

### The three-tier funnel

```
TIER 0 — ALWAYS ON, ZERO COST
  Counting, fingerprinting, percentile updates.
  ~50,000 events/sec/core.  No model.  This is 99.9% of runtime.
         │  a threshold trips
         ▼
TIER 1 — STATISTICAL CONFIRMATION, ZERO COST
  Is it sustained? Seasonal? Suppressed? Correlated with other detectors?
  Milliseconds.  No model.  Filters out ~80% of tier-0 trips.
         │  confirmed
         ▼
TIER 2 — REASONING, EXPENSIVE
  Assemble evidence, query providers, run the agent, produce an explanation.
  Seconds. Model calls. Hard budget: max N per minute, queued beyond that.
```

**Cost illustration.** 50 million log lines/day.
- Per-line LLM summarisation: 50M model calls/day. Unaffordable, and it produces 50M paragraphs nobody reads.
- Aegis: 50M lines → ~380 templates → ~40 tier-0 trips/day → ~8 confirmed signals/day → ~3 incidents/day → **3 to 12 model invocations per day.**

Same coverage. Six orders of magnitude less spend. And the output is three incidents instead of fifty million paragraphs.

### Example — hysteresis in practice

Without hysteresis, a metric hovering at the threshold produces this:
```
14:30:01 FIRE   14:30:04 CLEAR   14:30:09 FIRE   14:30:11 CLEAR   … ×40
```
With hysteresis (`fire_above: 20/min, sustain: 30s, clear_below: 8/min, clear_sustain: 120s`):
```
14:30:31 FIRE (sustained 30s above 20/min)
14:47:12 CLEAR (sustained 120s below 8/min)
```
One incident, one page, one resolution. Every threshold in the system must have four parameters, not one.

---

## C9 — Incident Manager

### Purpose
Convert a stream of signals into a small number of incidents — the objects humans actually work with.

### Analogy
An emergency room triage desk. Forty people arrive after a bus crash. Triage does not create forty independent cases; it creates one event with forty patients, ranked by severity, with the cause recorded once.

### Should be
- **The unit of everything downstream.** Notifications, chat, remediation, postmortems all attach to an incident.
- **Stateful with an explicit lifecycle:** `detected → investigating → identified → mitigating → resolved → reviewed`.
- **Aggressively deduplicating.** When in doubt, group; humans can split.
- **Append-only in its timeline.** The record of what was known when is the postmortem.

### Should NOT be
- Not one incident per signal. That's just alerts with extra steps.
- Not auto-closing on a single healthy reading. Require sustained recovery.
- Not silent about grouping decisions. Always show what was grouped and why.

### Sub-components

| Sub-component | Job |
|---|---|
| `SignalBuffer` | Short holding window (30–60s) so related signals arrive before grouping |
| `GroupingEngine` | Decides same-incident vs new-incident |
| `CauseRanker` | Orders members by topological upstream-ness and timing |
| `SeverityScorer` | Severity from blast radius, business criticality, user impact |
| `TimelineBuilder` | Append-only event log for the incident |
| `EvidenceCollector` | Pulls traces, metrics, deploys, conformance results |
| `LifecycleManager` | State transitions and auto-resolution rules |
| `NotificationRouter` | Who is told, through which channel, at what severity |

### The four grouping rules

1. **Trace identity** — signals sharing `trace_id`s are the same incident. Strongest.
2. **Topological adjacency** — signals from services connected in the graph within a short window.
3. **Temporal proximity** — signals starting within N seconds, where N scales with severity.
4. **Flow membership** — signals affecting steps of the same flow spec.

Grouping requires **two of the four** to agree. One alone over-groups.

### Flow

```
1. Signals arrive → SignalBuffer holds 45s
2. GroupingEngine: match against open incidents using the four rules
3. Match → attach to existing incident, extend timeline
   No match → create new incident
4. CauseRanker orders members using the topology graph
5. SeverityScorer assigns P1–P4
6. EvidenceCollector assembles the bundle
7. If severity ≥ threshold → hand to the Reasoning layer (C10)
8. NotificationRouter delivers
9. LifecycleManager watches for sustained recovery → resolved
```

### Example

```
INCIDENT #418
  status:    identified
  severity:  P1
  opened:    14:32:01     resolved: —
  flows:     place_order (DOWN) · search_products (healthy)

  MEMBERS (5 signals grouped — 2 of 4 rules matched: topology + temporal)
    ① payment-svc      LatencyShift      14:31:58   ◄ RANKED CAUSE
    ② order-svc        ErrorRatio        14:32:01     downstream
    ③ gateway          RateSpike (502)   14:32:01     downstream
    ④ notify-svc       SaturationDetect  14:32:04     downstream
    ⑤ place_order      ConformanceRate   14:32:06     downstream

  WHY ① IS RANKED CAUSE
    · Most upstream unhealthy node in the dependency graph
    · Earliest onset by 3 seconds
    · All other members are transitively downstream of it
    · 71 of 74 failing traces contain a payment-svc span exceeding its bound

  BLAST RADIUS   4 services · 1 of 2 flows down · ~2,100 requests affected

  TIMELINE
    14:31:58  payment-svc p95 crosses 3× baseline
    14:32:01  incident opened, 3 signals grouped
    14:32:06  conformance: 94% of place_order traces failing at step 3
    14:32:11  evidence assembled (14 traces, 6 metric series, 1 deploy)
    14:32:14  reasoning agent invoked
    14:33:02  hypothesis published (see C10)
```

Note the reasoning agent was invoked **once**, at 14:32:14, for the whole incident — not once per signal, and certainly not once per log line.

---

## C10 — Agent Runtime

### Purpose
Run the LLM-powered reasoning that turns an evidence bundle into an explanation, a hypothesis, and a recommendation.

### Analogy
Everything before this component is instruments and charts. This is the doctor who reads them and says what is wrong. The doctor is expensive, occasionally wrong, and should never be consulted about every heartbeat — only about the patient who is actually unwell.

### Should be
- **Invoked rarely, by an incident, never by a log line.**
- **Tool-using, not text-guessing.** The agent must fetch evidence, not speculate from a prompt.
- **Bounded.** Maximum iterations, maximum tokens, maximum wall-clock, maximum spend — enforced by the runtime, not by asking the model nicely.
- **Skill-guided.** Recurring investigation types get written procedures (see §7).
- **Auditable.** Every tool call, every intermediate conclusion, stored.

### Should NOT be
- Not an agent with production write access.
- Not one giant do-everything agent. Specialised sub-agents with narrow tools outperform a generalist and are far easier to debug.
- Not trusted without evidence links. Any claim in the output must cite the trace, metric, or file it came from.
- Not the detection layer, ever.

### Sub-components

| Sub-component | Job |
|---|---|
| `Orchestrator` | Receives the incident, plans which sub-agents to run, merges results |
| `SubAgentPool` | The specialists (see §6) |
| `ToolRegistry` | Every callable tool, with schema and permission level |
| `SkillLoader` | Selects and injects the right skill for the task |
| `ContextAssembler` | Builds the evidence bundle within a token budget |
| `ModelRouter` | Chooses which model runs which step (see §9) |
| `BudgetGuard` | Hard ceilings on tokens, calls, spend, time |
| `TraceRecorder` | Full audit log of agent reasoning and tool calls |
| `OutputValidator` | Rejects outputs missing evidence citations or required fields |

### The context assembly problem (the real engineering work)

An agent is only as good as what you hand it. `ContextAssembler` must fit, inside a token budget:

```
INCIDENT #418  ·  P1  ·  4 services
  ├─ Ranked signals with baselines and deltas            ~400 tokens
  ├─ 3 representative failing traces (not 74)          ~1,200 tokens
  ├─ 1 successful trace from before onset, for contrast  ~400 tokens
  ├─ Relevant flow spec section                          ~300 tokens
  ├─ Conformance deviations, aggregated                  ~250 tokens
  ├─ Topology subgraph (affected nodes only)             ~200 tokens
  ├─ Deploys in the preceding 60 minutes                 ~150 tokens
  ├─ Similar past incidents from memory (C15)            ~600 tokens
  └─ Relevant source files (from the fix agent's index)~2,000 tokens
                                                        ─────────────
                                                        ~5,500 tokens
```

**Selection, not accumulation.** Three representative traces beat seventy-four. One healthy contrast trace is worth more than ten more failing ones. This assembly logic determines output quality far more than the choice of model does.

### Flow

```
1. Incident handed over with severity ≥ threshold
2. Orchestrator loads the incident-triage skill
3. ContextAssembler builds the bundle
4. ModelRouter picks the model for the task class
5. Agent loop (max 8 iterations):
      think → call tool → observe → repeat
      tools: query_traces, query_metrics, read_file, search_memory,
             get_topology, get_flow_spec, list_deploys
6. OutputValidator checks: hypothesis present? evidence cited? confidence stated?
7. Result attached to the incident timeline
8. If confidence ≥ threshold and remediation is enabled → hand to C13
```

### Example output

> **Hypothesis (confidence: high)**
> `payment-svc` is exhausting its HTTP connection pool to the external payment provider.
>
> **Evidence**
> · Pool utilisation reached 100% at 14:31:52 and has held (metric `payment.pool.active`, source: SigNoz)
> · 71 of 74 failing traces show `payment.authorize` spans blocked in acquisition, not in the provider call itself (traces `4f2a…`, `9c1b…`, `2e77…`)
> · Provider response times are unchanged at p95 210ms — **the provider is healthy; we are not**
> · Deploy `v2.14.3` at 14:08:51 modified `payment/client.py`, changing `pool_maxsize` from 50 to 5
> · Similar past incident #291 (2026-06-14): identical signature, resolved by restoring pool size
>
> **Recommended immediate action**
> Roll back `v2.14.3`, or hot-patch `pool_maxsize` to 50.
>
> **Recommended durable fix**
> Add a saturation alert on `payment.pool.active` at 80%, and a config test asserting `pool_maxsize >= 2 × expected_concurrency`.

Every claim carries a source. That is what makes it actionable rather than plausible.

---

## C11 — Skill Library

### Purpose
Capture *how to do a recurring job well* as written procedure, so agents perform consistently instead of improvising each time.

### Analogy
A pilot's checklist. The pilot knows how to fly. The checklist exists because under pressure, humans and models both skip steps. The checklist is not a replacement for skill — it is the shape that reliable skill takes.

### Should be
- **Written in plain markdown, versioned in the repo**, reviewable by anyone.
- **Loaded selectively** — the relevant skill only, not all of them.
- **Procedural and specific**: steps, checks, worked examples, failure modes, output format.
- **Improvable from outcomes.** When a skill produces a bad investigation, the skill gets edited.

### Should NOT be
- Not code. A skill that needs to execute is a tool (C10's `ToolRegistry`), not a skill.
- Not a personality prompt. "Be helpful and thorough" is not a skill.
- Not all loaded at once. Loading twelve skills into one context degrades all of them.

### Sub-components

| Sub-component | Job |
|---|---|
| `SkillStore` | Files on disk, versioned |
| `SkillIndex` | Name, description, trigger conditions |
| `SkillSelector` | Chooses skills for the current task |
| `SkillValidator` | Enforces required sections at authoring time |
| `SkillMetrics` | Tracks outcome quality per skill |

### Skill file format

```markdown
---
name: connection-pool-exhaustion
triggers: [saturation_detector, latency_shift, pool_metric_present]
applies_to: [investigation]
version: 4
---

# Diagnosing connection pool exhaustion

## When this applies
Latency rises sharply while the downstream dependency's own latency
is unchanged. This asymmetry is the signature.

## Procedure
1. Confirm downstream health first. Query the dependency's own latency.
   If the dependency is slow, this skill does not apply — stop.
2. Locate pool metrics: active, idle, max, wait_time.
3. If active == max and wait_time > 0, saturation is confirmed.
4. Determine whether max was reduced (config change) or demand rose (traffic).
   Check deploys touching client/pool configuration in the last 24h.
5. Check for leaks: connections acquired without release in error paths.

## Required output
- Which of the three causes: config reduction / demand increase / leak
- The specific file and line if config-related
- Immediate mitigation AND durable fix, stated separately

## Common mistakes
- Blaming the downstream service. Its latency being flat is the proof it is fine.
- Recommending a pool size increase when the real cause is a leak.
  Raising the ceiling on a leak delays the outage; it does not prevent it.
```

### The initial skill set

**Investigation skills:** `connection-pool-exhaustion`, `deploy-correlation`, `cascading-failure-analysis`, `silent-degradation`, `data-integrity-breach`, `resource-exhaustion`, `external-dependency-failure`, `flow-deviation-triage`

**Authoring skills:** `flow-spec-extraction`, `flow-spec-review`, `baseline-tuning`

**Remediation skills:** `reproduce-before-fix`, `minimal-patch`, `test-authoring-from-trace`, `blast-radius-review`

**Communication skills:** `incident-summary`, `postmortem-writing`, `oncall-handoff`

### Example of a skill changing the outcome

Without the pool skill, an agent seeing high latency in `payment-svc` typically concludes "the payment provider is slow" — the obvious reading, and wrong. The skill's step 1 forces it to check the provider's own latency first. Flat provider latency plus rising local latency inverts the conclusion entirely. **One checklist step changes the diagnosis from wrong to right.**

---

## C12 — MCP Layer

MCP is used in **both directions**, and they are different products.

### Direction A — Aegis as MCP *client* (consuming external systems)

**Purpose:** reach into SigNoz, Opik, GitHub, and others without writing bespoke integration code for each.

**Should be:** the transport under C2's `TelemetryProvider` interface. **Should NOT be:** exposed directly to agents. An agent given raw vendor MCP tools will write vendor-specific reasoning, breaking P6.

```
Agent
  │  calls canonical tool: query_traces(service, window, status)
  ▼
TelemetryProvider interface  (C2)
  │
  ├─ SigNozProvider  ──MCP──►  SigNoz MCP server
  ├─ OpikProvider    ──MCP──►  Opik MCP server
  └─ GrafanaProvider ──MCP──►  Grafana MCP server
```

**Servers typically consumed**

| Server | Tools used for |
|---|---|
| SigNoz | Traces, metrics, service map, alert rules |
| Opik | LLM spans, prompt/response, token cost, eval scores |
| Grafana / Prometheus | Infrastructure metrics |
| GitHub / GitLab | Deploys, commits, PRs, file contents |
| Kubernetes | Pod state, restarts, resource limits |
| PagerDuty / Slack | Notification delivery, on-call schedule |

### Direction B — Aegis as MCP *server* (exposing itself)

**Purpose:** let *other* agents and IDEs query Aegis. This is what makes Aegis part of a workflow rather than another dashboard to remember to open.

**Should be:** read-heavy, with writes gated behind explicit permission scopes. **Should NOT be:** an unauthenticated firehose — these tools expose production behaviour.

**Tools Aegis exposes**

```
get_incident(incident_id)                 → full incident with evidence
list_incidents(status, severity, window)  → open and recent incidents
get_service_health(service)               → current state vs baseline
get_flow_spec(flow_name)                  → the declared expected behaviour
check_conformance(flow_name, window)      → deviation summary
get_topology(service, depth)              → dependency subgraph
query_incident_history(query)             → semantic search over past incidents
simulate_scenario(scenario)               → what-if result (C14)
get_blast_radius(service)                 → what breaks if this service fails
```

**Why this matters, concretely.** A developer in their IDE asks their coding assistant: *"I'm changing the retry logic in payment-svc. Anything I should know?"* The assistant calls Aegis over MCP:

- `get_blast_radius("payment-svc")` → order-svc, gateway, notify-svc, checkout flow
- `query_incident_history("payment-svc retry")` → 3 past incidents, 2 caused by retry storms
- `get_flow_spec("place_order")` → `authorize_payment` declares `retry: {max: 2}`

And answers: *"This service is on the critical path for checkout. Two past incidents (#291, #377) were caused by retry storms here. The flow spec declares max 2 retries — if you raise that, add a circuit breaker."*

Aegis has now prevented an incident instead of explaining one. **That is the highest-value thing this system can do, and it comes almost free once the other components exist.**

---

## C13 — Code Intelligence & Remediation Agent

### Purpose
Take a diagnosed incident, locate the responsible code, reproduce the failure as a test, produce a minimal fix, prove it works, and open a pull request with the full evidence chain.

### Analogy
A surgeon who is required to show the scan, mark the site, explain the procedure, and have a colleague sign off before cutting. The skill is necessary but not sufficient; the protocol is what makes it safe.

### Should be
- **Reproduce-first.** The first artefact is a *failing test derived from the trace*, produced before any fix is attempted.
- **Minimal.** Change the fewest lines that resolve the issue. No refactoring, no cleanup, no style changes.
- **Evidence-bundled.** The PR contains the incident, the trace, the deviation, the failing test, the passing test, and the blast radius.
- **Sandboxed.** Tests run in an isolated environment with no production credentials.
- **Loop-closing.** After merge, watch whether the anomaly actually stopped.

### Should NOT be
- **Never auto-merge to production.** Not at any confidence level. This is not a tuning parameter.
- Not permitted to modify tests to make them pass. Explicitly forbidden and checked.
- Not permitted to touch infrastructure, secrets, CI configuration, or dependency versions without a separate approval scope.
- Not a refactoring agent. Scope creep in a fix PR destroys reviewability.

### Sub-components

| Sub-component | Job |
|---|---|
| `RepoIndex` | Symbol-level index, incremental, shared with C6 |
| `TraceToCodeMapper` | Maps span names, stack frames, and log lines to files and lines |
| `ReproducerAgent` | Writes a failing test that reproduces the incident |
| `PatchAgent` | Writes the minimal fix |
| `TestRunner` | Sandboxed execution |
| `BlastRadiusReviewer` | Which other flows touch the changed code |
| `RegressionChecker` | Full suite plus targeted tests for affected flows |
| `EvidenceBundler` | Assembles the PR body |
| `PRPublisher` | Opens the PR, never merges |
| `OutcomeWatcher` | Post-merge: did the anomaly stop? |
| `AutonomyGate` | Enforces which change classes are permitted at which tier |

### The reproduce-first rule

This is the rule that separates a useful fix agent from a dangerous one.

```
WRONG                              RIGHT
  incident → patch → hope            incident → failing test → patch
                                     → test passes → suite passes → PR
```

Without a reproducer you cannot distinguish "fixed" from "the symptom stopped for unrelated reasons." Models are extremely good at producing confident, plausible patches for problems they have diagnosed incorrectly. The test is the only defence.

### Autonomy tiers

| Tier | Permitted change class | Approval |
|---|---|---|
| **T0 — Advise** | No code. Explanation and recommendation only. | None needed |
| **T1 — Draft** | Any change, opened as a draft PR | Human reviews and merges |
| **T2 — Propose** | Config values, retry/timeout tuning, null guards, log improvements | Human merges; auto-merge after N successful T2 PRs, per team choice |
| **T3 — Auto-revert** | Revert a specific deploy that correlates with a P1 | Pre-authorised revert only; never a novel patch |

**Business logic never exceeds T1.** Ever.

### Flow

```
 1. Incident with hypothesis, confidence ≥ threshold
 2. AutonomyGate: is this change class permitted at the configured tier?
 3. TraceToCodeMapper: failing span "payment.authorize"
       → payment/client.py:88, payment/pool.py:34
 4. RepoIndex: pull those files plus their callers and tests
 5. ReproducerAgent: write a test that fails for the right reason
 6. TestRunner: confirm it fails ✓  (if it passes, the diagnosis is wrong — STOP)
 7. PatchAgent: minimal fix
 8. TestRunner: reproducer now passes ✓
 9. RegressionChecker: full suite ✓
10. BlastRadiusReviewer: which other flows touch this code?
11. EvidenceBundler → PRPublisher (draft PR, humans notified)
12. Post-merge: OutcomeWatcher confirms the anomaly stopped
13. Outcome recorded in Incident Memory (C15)
```

### Example — the generated PR

```
PR #1183  [aegis] fix: restore payment client pool size
STATUS: DRAFT — requires human approval

INCIDENT #418 (P1) · 14:31:58–15:06:22 · ~2,100 requests affected

DIAGNOSIS
  Deploy v2.14.3 reduced pool_maxsize from 50 to 5 in payment/client.py.
  Under normal concurrency (~30) the pool saturates, blocking authorize
  calls in acquisition. The external provider is healthy (p95 210ms flat).

REPRODUCER  tests/test_payment_pool.py::test_pool_saturation_under_concurrency
  Simulates 30 concurrent authorize calls against a stub provider.
  Before patch: FAILED — 25 calls exceeded 5s acquiring a connection
  After patch:  PASSED — max acquisition wait 12ms

DIFF
  payment/client.py
  - pool_maxsize=5,
  + pool_maxsize=50,

REGRESSION  411 passed · 0 failed · 3 skipped
BLAST RADIUS  payment/client.py is used by place_order and refund_order.
              Both suites pass.

EVIDENCE
  Traces: 4f2a…, 9c1b…, 2e77…
  Metric: payment.pool.active 100% from 14:31:52
  Deploy: v2.14.3 @ 14:08:51 (commit a93f1c2)
  Prior:  incident #291, identical signature, same resolution

NOT INCLUDED (recommended as separate work)
  · Saturation alert on payment.pool.active at 80%
  · Config test asserting pool_maxsize >= 2 × expected_concurrency
```

Note the last block. The agent stays in scope but records what else should happen. A reviewer approves this in under a minute because there is nothing left to work out.

---

## C14 — Simulation Engine

### Purpose
Answer "what happens if X?" before X happens.

### Reality check
Full behavioural simulation of a distributed system is a research problem. Do not build it. Build the ladder below, where the bottom rung delivers most of the value for a fraction of the effort.

### The four rungs

**Rung 1 — Static reachability (build this; it is nearly free)**
You already have the topology graph, the flow specs, and the error-path analysis from C6. That is enough to answer many what-ifs by traversal alone, with no execution.

> **Q:** What happens if `inventory-svc` starts returning 503?
>
> **A:** 2 flows affected.
> `place_order` — step 2 `reserve_inventory` is `required: true, on_failure: abort_flow`. `order-svc` calls it at `order/service.py:112` with a try/except catching only `TimeoutError`, so a 503 propagates as an unhandled 500. **Checkout fails hard for all users.**
> `check_availability` — calls the same service at `catalog/api.py:45` with a cached fallback. **Degrades to stale data, does not fail.**
> **Gap identified:** `order-svc` has no fallback for inventory 5xx. `catalog` does. Same dependency, inconsistent handling.

That answer required zero execution and is immediately actionable.

**Rung 2 — Trace replay.** Take a real recorded trace, mutate one input (slow response, error code, malformed payload), replay it against staging, observe. Real behaviour, controlled variable.

**Rung 3 — Fault injection.** A proxy in front of a staging service injects latency, errors, or packet loss on demand. Verifies rung 1's predictions empirically.

**Rung 4 — Predictive modelling.** Load simulation, queueing models, capacity forecasting. Genuinely hard. Only worth it at significant scale.

### Should be
- **Read-only against production.** Rungs 2 and 3 run on staging exclusively.
- **Honest about its basis.** Label every answer as `static-analysis`, `replayed`, or `injected` so nobody mistakes a traversal for an experiment.
- **Wired into pre-deploy checks.** The highest-value placement is a PR comment, not a dashboard.

### Should NOT be
- Not fault injection in production without explicit, scoped, human-initiated authorisation.
- Not presented as certainty. Static analysis predicts; it does not prove.

### Sub-components
`ScenarioParser` · `StaticPredictor` · `ReplayEngine` · `FaultInjector` · `StagingOrchestrator` · `PredictionRecorder` (compares predictions against real incidents to calibrate confidence) · `PreDeployChecker`

### Example — the pre-deploy comment
```
🛡 Aegis pre-deploy check on PR #1201

This PR modifies inventory/reserve.py (flow: place_order, step 2, critical).

  Static analysis
    · 1 flow affected: place_order
    · Step 2 is marked critical: "Skipping this oversells stock"
    · order-svc catches only TimeoutError from this call
    · The new code path can raise ValidationError → would propagate as 500

  History
    · 2 past incidents in this file (#188, #291)
    · Median time-to-detect for failures here: 22 minutes

  Recommendation
    Add ValidationError to the except clause in order/service.py:112,
    or raise TimeoutError-compatible errors from reserve().

  RISK: MEDIUM-HIGH
```

---

## C15 — Incident Memory

### Purpose
Make the system better the longer it runs. Every resolved incident becomes a retrievable precedent.

### Analogy
An experienced engineer's real advantage over a talented new hire is not intelligence — it is having seen this before. Memory is how you give a system seniority.

### Should be
- **Semantic, not keyword.** "Connection pool exhausted" must match "pool saturation" and "too many open connections."
- **Signature-based.** Store a structured fingerprint (services, deviation types, metric shape), not just prose.
- **Outcome-labelled.** Record whether the fix actually worked. A remembered *wrong* diagnosis is worth as much as a right one.
- **Fed back into context** at investigation time, before the agent forms a hypothesis.

### Should NOT be
- Not a dumb log archive. Raw history without structure is unsearchable.
- Not authoritative. A past match is a hint, not a conclusion — systems change.
- Not storing unredacted content. Redaction happened at C3; embeddings must inherit it.

### Sub-components
`IncidentArchive` · `SignatureExtractor` · `EmbeddingIndex` · `SimilarityMatcher` · `OutcomeTracker` · `PatternMiner` (finds recurring themes across incidents) · `KnowledgeInjector`

### Example — the compounding effect

**Month 1, incident #291.** Agent investigates from scratch, takes 90 seconds and 11 tool calls, reaches the right answer.

**Month 3, incident #418.** Before the agent starts, `SimilarityMatcher` returns:

> Similar past incident: **#291** (similarity 0.91)
> Signature match: `payment-svc` + `LatencyShift` + downstream `ErrorRatio` + flat external dependency latency
> Diagnosis then: connection pool exhaustion after a config change
> Fix then: restored `pool_maxsize`
> Outcome: **resolved, anomaly stopped within 4 minutes of merge, no recurrence for 71 days**

The agent now starts from a hypothesis instead of a blank page: 4 tool calls, 30 seconds, higher confidence.

**Month 9, `PatternMiner` reports:**
> `payment-svc` has produced 5 of the last 12 P1 incidents. 4 of 5 involved resource limits changed by deploys. **Recommendation: add config-boundary tests and a saturation alert to this service.**

The system has stopped explaining incidents and started preventing a category of them.

---

## C16 — Governance Layer

### Purpose
Enforce privacy, cost, and permission boundaries structurally, so that no prompt, misconfiguration, or clever agent can bypass them.

### Analogy
Building codes. Not a feature of the building, and not something you inspect for after construction — something the structure is built within.

### Should be
- **Enforced in the runtime**, never by instructing a model.
- **Fail-closed.** If the redactor is unavailable, ingestion stops. It does not pass data through.
- **Fully audited.** Every model call, every tool call, every provider query, every automated action.

### Should NOT be
- Not a post-processing step. Redacting after storage means the unredacted data was already stored.
- Not overridable by an agent, a prompt, or an urgent incident.

### Sub-components

| Sub-component | Job |
|---|---|
| `RedactionPipeline` | Pattern + NER based, before storage and before any model boundary |
| `RedactionAudit` | What was redacted, how much, from where |
| `DataBoundary` | Declares which data classes may cross to which model endpoints |
| `CostBudget` | Per-incident, per-hour, per-day ceilings with hard stops |
| `RateLimiter` | Model call and provider query throttles |
| `PermissionMatrix` | Which agent may call which tool at which autonomy tier |
| `AuditLog` | Append-only, tamper-evident |
| `KillSwitch` | Disable automated action instantly, globally |

### The data boundary — the decision that shapes deployment

```
DATA CLASS                     MAY CROSS TO
  Redacted templates            any endpoint, including hosted APIs
  Metric series                 any endpoint
  Topology / flow specs         any endpoint
  Raw log text                  self-hosted models ONLY
  Source code                   per policy — often self-hosted only
  Customer PII                  NEVER — must not survive C3
```

This single table decides where models run. If policy says raw logs and source code cannot leave your infrastructure, then the components that need them — the fix agent, the flow synthesizer — must run on self-hosted models. **This is the primary architectural argument for a self-hostable model in the stack, and it is addressed directly in §9.**

### Cost ceilings

```yaml
budgets:
  per_incident:
    max_model_calls: 25
    max_tokens: 400_000
    max_wall_clock_seconds: 300
  per_hour:
    max_incidents_investigated: 12   # beyond this, queue
    max_spend_usd: 15
  per_day:
    max_spend_usd: 120
  on_breach: queue_and_notify        # never: silently_continue
```

An outage produces many incidents at once. Without ceilings, the outage generates a second, financial outage. This is a real failure mode, not a hypothetical one.

---

## C17 — Interface Layer

### Purpose
Present incidents to humans, and let them interrogate the system in natural language.

### Should be
- **Incident-first.** The landing view is open incidents, not a wall of graphs.
- **Evidence-linked.** Every claim clicks through to the trace, metric, or line that supports it.
- **Scoped chat.** The chatbot answers *about a specific incident*, with that incident's evidence bundle in context.
- **Timeline-centric.** What was known, when.

### Should NOT be
- Not a general-purpose dashboard builder. That is Grafana's job, and it is better at it.
- Not an unscoped chatbot over all logs — that reintroduces the cost problem the whole architecture exists to avoid.

### Sub-components
`IncidentFeed` · `IncidentDetail` · `TimelineView` · `TopologyView` (live health overlay) · `FlowView` (spec vs actual, side by side) · `ConversationalInterface` · `SpecEditor` · `ConfigUI` · `AuditView`

### Example chat exchange (scoped to incident #418)

> **User:** why did notify-svc queue blow up?
>
> **Aegis:** It didn't fail on its own. `notify-svc` consumes confirmation jobs published by `order-svc` after step 4. When `payment-svc` saturated at 14:31:58, orders stopped reaching step 4, so nothing was consumed — but retry jobs from *earlier* partially-completed orders kept arriving. Queue depth rose to 1,204. It will drain on its own once payment recovers; no action needed on `notify-svc`.
>
> **User:** how do you know payment is the cause and not order-svc?
>
> **Aegis:** Three reasons. `payment-svc` degraded 3 seconds before `order-svc` (14:31:58 vs 14:32:01). In the dependency graph `order-svc → payment-svc`, so the causal direction can only run one way. And 71 of 74 failing traces show `order-svc` spans blocked *waiting on* `payment.authorize`, with `order-svc`'s own processing time unchanged at 41ms median.

Note the second answer: it cites topology, timing, and traces. This is what an evidence-linked interface looks like in text.

---

# 6. Agent architecture

## 6.1 The three-word distinction

People conflate these constantly. They are different things.

| | What it is | Example | Analogy |
|---|---|---|---|
| **Tool** | A single function an agent can call | `query_traces()` | A stethoscope |
| **Skill** | Written procedure for doing a task well | `connection-pool-exhaustion.md` | A diagnostic checklist |
| **Agent** | An LLM with a goal, tools, skills, and a loop | `InvestigatorAgent` | The doctor |

An agent uses tools, guided by skills, to achieve a goal. Confusing them produces bad architecture: procedures written as code (rigid), or tools written as prompts (unreliable).

## 6.2 The agent roster

Specialised sub-agents with narrow tool sets outperform one generalist and are dramatically easier to debug.

| Agent | Triggered by | Goal | Tools | Skills | Autonomy |
|---|---|---|---|---|---|
| **Orchestrator** | New incident, severity ≥ P3 | Plan and merge sub-agent work | `spawn_subagent`, `get_incident` | `incident-triage` | Read-only |
| **Investigator** | Orchestrator | Produce a ranked hypothesis with evidence | `query_traces`, `query_metrics`, `get_topology`, `get_flow_spec`, `list_deploys`, `search_memory` | Investigation set | Read-only |
| **Conformance Analyst** | Flow deviations present | Explain *which* deviation matters and why | `get_flow_spec`, `query_traces`, `get_conformance_report` | `flow-deviation-triage`, `silent-degradation` | Read-only |
| **Code Locator** | Hypothesis formed | Map the failure to files and lines | `search_repo`, `read_file`, `get_symbol_refs`, `git_blame` | `trace-to-code` | Repo read |
| **Reproducer** | Code located | Write a test that fails for the right reason | `read_file`, `write_test`, `run_tests` | `test-authoring-from-trace` | Sandbox write |
| **Patcher** | Reproducer passes verification | Minimal fix | `read_file`, `write_patch`, `run_tests` | `minimal-patch` | Sandbox write |
| **Reviewer** | Patch produced | Independently critique the patch | `read_file`, `get_blast_radius`, `run_tests` | `blast-radius-review` | Read-only |
| **Flow Synthesizer** | Connect, or merge to main | Turn call graphs into flow specs | `read_file`, `get_call_graph`, `query_traces` | `flow-spec-extraction` | Repo read |
| **Simulator** | What-if query, or PR opened | Predict scenario outcome | `get_topology`, `get_flow_spec`, `read_file`, `run_replay` | `scenario-analysis` | Staging only |
| **Chat Agent** | User message | Answer questions about an incident | Read tools, scoped to one incident | `incident-explanation` | Read-only |
| **Scribe** | Incident resolved | Write the postmortem | `get_incident`, `get_timeline`, `search_memory` | `postmortem-writing` | Read-only |

## 6.3 The Reviewer is not optional

The **Patcher → Reviewer** split matters more than it looks. A model that wrote a patch is a poor judge of that patch — it has already committed to the reasoning. A second agent, with different context and an adversarial instruction ("find why this is wrong"), catches a materially higher share of bad fixes. Where possible, use a *different model* for the Reviewer than for the Patcher.

## 6.4 Loop discipline

Every agent runs under hard limits enforced by `BudgetGuard`, not by prompt instruction:

```yaml
investigator:
  max_iterations: 8
  max_tokens: 120_000
  max_wall_clock_s: 90
  on_limit: return_partial_with_flag   # never: silently_truncate
```

A partial hypothesis marked "incomplete — hit iteration limit" is honest and useful. A silently truncated one is worse than nothing.

---

# 7. Skill lifecycle

Skills are specified in C11. What matters operationally is how they improve.

```
1. AUTHOR      A recurring investigation type is identified
2. WRITE       Procedure, checks, worked example, common mistakes
3. VALIDATE    SkillValidator enforces required sections
4. DEPLOY      Versioned into the repo, indexed
5. MEASURE     SkillMetrics tracks: was the diagnosis correct?
                                    did the fix hold?
                                    how many tool calls were needed?
6. REVISE      Bad outcomes become new "common mistakes" entries
```

**Where skills come from:** the postmortem. When a human corrects an agent's diagnosis, that correction is skill material. Over a year this converts your team's tacit debugging knowledge into a reviewable artefact — which is valuable even if you later replace every model in the stack.

---

# 8. MCP deployment topology

C12 specifies the two directions. Physically it looks like this:

```
        ┌──────────────────────────────────────────────┐
        │              AEGIS CORE                      │
        │  detection · conformance · incidents · agents│
        └───────┬──────────────────────────┬───────────┘
                │                          │
      MCP CLIENT│                          │MCP SERVER
                ▼                          ▼
   ┌────────────────────────┐   ┌──────────────────────────┐
   │  SigNoz MCP            │   │  Consumers of Aegis:     │
   │  Opik MCP              │   │   · IDE coding assistants│
   │  GitHub MCP            │   │   · CI pipelines         │
   │  Kubernetes MCP        │   │   · Chat-ops bots        │
   │  Slack / PagerDuty MCP │   │   · Other internal agents│
   └────────────────────────┘   └──────────────────────────┘
```

**Security notes for Direction B:** scope tokens per consumer; read tools and write tools require different scopes; `simulate_scenario` must never be reachable with production fault-injection permission from an external caller; log every external tool call to `AuditLog`.

---

# 9. Model routing — where MiniMax fits, and where it does not

## 9.1 The principle

**Model choice is the least important decision in this architecture, and the routing policy is one of the most important.**

Output quality depends overwhelmingly on the quality of context assembly (C10) and the skill guiding the task (C11). A well-briefed mid-tier model beats a badly-briefed frontier model on almost every task here. What routing gets you is the ability to spend money only where it changes the answer.

## 9.2 The four model tiers

| Tier | Class | Used for | Cost profile |
|---|---|---|---|
| **M0** | No model | Detection, fingerprinting, counting, graph traversal, conformance | Free |
| **M1** | Small local (embeddings, classifiers) | Similarity search, template clustering, severity pre-scoring | Negligible |
| **M2** | Mid-tier, self-hostable — **MiniMax** | Code work, repo analysis, test authoring, high-volume drafting, anything touching raw logs or source | Low, predictable |
| **M3** | Frontier hosted | Final root-cause narrative on P1/P2, ambiguous multi-hypothesis reasoning, patch review, postmortems | High, rare |

## 9.3 Task-to-model routing table

| Task | Tier | Why |
|---|---|---|
| Fingerprinting, thresholds, baselines | **M0** | Arithmetic. Never a model. |
| Conformance checking | **M0** | Sequence matching against a spec. Deterministic. |
| Cause ranking within an incident | **M0** | Graph traversal on the topology. |
| Incident similarity search | **M1** | Embeddings. |
| **Flow spec synthesis from call graphs** | **M2 (MiniMax)** | High volume — hundreds of entrypoints. Long context. Structured output. Verifiable against a deterministic call graph, so errors are catchable. |
| **Trace-to-code mapping** | **M2 (MiniMax)** | Code comprehension over many files. Repetitive. |
| **Reproducer test authoring** | **M2 (MiniMax)** | Code generation with an objective success criterion — the test must fail, then pass. Self-checking. |
| **Patch generation** | **M2 (MiniMax)** | Its strongest use. Verified by tests, reviewed by another agent, gated by a human. |
| **Raw-log explanation when policy forbids external endpoints** | **M2 (MiniMax, self-hosted)** | The data boundary requires it. |
| Bulk incident summarisation (P3/P4) | **M2 (MiniMax)** | Volume task, low stakes. |
| **Root-cause narrative for P1/P2** | **M3** | Ambiguous, high-stakes, human-facing. Worth the spend a few times a day. |
| **Patch review (adversarial)** | **M3** | Should differ from the model that wrote the patch. |
| Postmortem writing | **M3** | Human-facing, once per incident. |
| Interactive incident chat | **M3**, downgrade to M2 on budget breach | Latency and quality matter to a human waiting. |

## 9.4 Where MiniMax specifically earns its place

**Reason 1 — the data boundary (the strongest reason).**
Section C16 defines classes of data that may not leave your infrastructure: raw log text and source code. The fix agent needs both. If your policy is strict, a hosted frontier model is simply not an option for that component. A self-hostable model with strong coding ability is not a cost optimisation there — it is the only way the component can exist.

**Reason 2 — the volume tasks are code tasks.**
Flow synthesis, trace-to-code mapping, test authoring, and patch generation are the four highest-volume LLM tasks in the platform, and all four are code comprehension and generation. That is precisely the shape MiniMax-class models are strongest at, and they are an order of magnitude cheaper per token than frontier models.

**Reason 3 — the outputs are verifiable.**
The tasks routed to M2 all have objective checks: the call graph validates the flow spec, the test suite validates the patch, the reproducer must fail before it passes. Where verification is automatic, a cheaper model is a straightforward win — mistakes are caught by machinery, not by trust.

**Reason 4 — budget headroom during outages.**
Incidents cluster. During a bad hour you may investigate twelve incidents. Routing the bulk work to M2 keeps you inside the budget ceiling (C16) instead of hitting the hard stop mid-outage.

## 9.5 The draft-then-review pattern

The pattern that gets most of frontier quality at close to mid-tier cost:

```
MiniMax (M2)                          Frontier (M3)
  writes the reproducer     ──────►     reviews only the final patch
  writes the patch                      ~2,000 tokens in
  runs the tests                        ~500 tokens out
  iterates until green                  once per PR
  ~40,000 tokens of work
```

The expensive model never sees the iteration — only the finished artefact and the test results. On a typical fix, the M3 portion is under 3% of total tokens.

## 9.6 Where MiniMax should NOT be used

- **Never for detection.** No model belongs at M0. This is not about capability.
- **Not as the sole judge of its own patch.** Use a different model for the Reviewer agent.
- **Not for the final P1 narrative** that a human reads under pressure — unless your data boundary forbids the alternative, in which case accept the trade-off knowingly.
- **Not hard-coded.** Every model reference goes through `ModelRouter` config.

## 9.7 How to actually decide (do not take this table on faith)

Build a **golden set** of 30–50 past incidents with known correct diagnoses and known correct fixes. Replay them through the pipeline with different routing configurations. Measure:

- Diagnosis accuracy (correct root cause identified?)
- Patch validity (reproducer passes, suite passes, human approves unchanged?)
- Cost per incident
- Latency to first hypothesis

Then set routing from data. This golden set is also your regression suite whenever you change a prompt, a skill, or a model — and it is the single highest-leverage internal asset the project will produce.

```yaml
# config/models.yaml
routing:
  flow_synthesis:       {tier: M2, model: minimax, max_tokens: 32000}
  trace_to_code:        {tier: M2, model: minimax, max_tokens: 24000}
  reproducer:           {tier: M2, model: minimax, max_tokens: 16000}
  patch:                {tier: M2, model: minimax, max_tokens: 16000}
  patch_review:         {tier: M3, model: frontier, max_tokens: 4000}
  root_cause_p1:        {tier: M3, model: frontier, max_tokens: 8000}
  root_cause_p3:        {tier: M2, model: minimax, max_tokens: 8000}
  postmortem:           {tier: M3, model: frontier, max_tokens: 6000}
  chat:                 {tier: M3, model: frontier, fallback: minimax}

data_boundary:
  raw_logs:    [minimax_selfhosted]
  source_code: [minimax_selfhosted]
  redacted:    [minimax_selfhosted, frontier_hosted]
```

---

# 10. End-to-end technical workflow

One incident, start to finish, showing exactly which component acts at each moment. This is the document's summary — if you read only one section, read this one.

## Setup phase (once, at connect time)

```
T-7d  10:00  C1  Collectors attached: 6 containers discovered
T-7d  10:00  C2  Providers registered: SigNoz, Opik, GitHub
T-7d  10:02  C5  StaticDiscoverer parses repo → 11 candidate edges
T-7d  10:03  C5  RuntimeDiscoverer snapshots sockets → 9 observed edges
T-7d  10:04  C5  TraceDiscoverer reads 24h of traces → 9 confirmed edges
T-7d  10:04  C5  GraphMerger → ServiceGraph v1, shown to human, approved
T-7d  10:05  C6  RepoIndexer → 412 files, EntrypointDetector → 29 entrypoints
T-7d  10:06  C6  CallGraphBuilder builds trees (deterministic)
T-7d  10:11  C6  FlowSynthesizer [MiniMax] → 29 draft flow specs
T-7d  10:12  C6  TimingEstimator fills bounds from 30d of real traces
T-7d  10:12  C6  PR opened with .aegis/flows/*.yaml
T-7d  14:00  —   Human reviews, corrects 6 specs, merges
T-7d  14:01  C7  ShadowMode begins — observing, not alerting
T-7d  14:01  C8  BaselineLearner begins accumulating seasonal baselines
T-0d  14:01  C7  Shadow period ends → conformance alerting enabled
```

## Incident phase

```
14:08:51  —    Deploy v2.14.3 ships. Aegis records it via GitHub MCP.

14:31:52  C1   Normal collection. No signal yet.
14:31:52  C3   Lines fingerprinted. Pool metric updated in MetricSeries.
14:31:58  C8   LatencyShift fires: payment-svc p95 crosses 3× baseline
14:31:58  C8   HysteresisGate: not yet sustained. Held.
14:32:01  C8   ErrorRatio fires: order-svc success ratio 0.98 → 0.31
14:32:01  C8   RateSpike fires: gateway 502s, 44/min vs baseline 0
14:32:04  C8   SaturationDetector fires: notify-svc queue depth 1,204
14:32:06  C7   TraceAssembler completes 400 traces for place_order
14:32:06  C7   SequenceChecker: 94% fail at step 3 authorize_payment
14:32:06  C7   DeviationAggregator escalates ConformanceRate signal
14:32:06  C8   HysteresisGate: latency sustained 30s → signal confirmed

               ── everything above cost zero model calls ──

14:32:07  C9   SignalBuffer holds 5 signals for 45s
14:32:07  C9   GroupingEngine: topology + temporal rules both match
                 → single incident #418
14:32:08  C9   CauseRanker: payment-svc is most upstream + earliest
14:32:08  C9   SeverityScorer: P1 (critical flow down, 4 services)
14:32:09  C9   EvidenceCollector pulls:
                 · 74 failing traces (C4 + C2/SigNoz)
                 · 6 metric series, 30d baselines (C2/SigNoz)
                 · LLM span costs (C2/Opik) — normal, ruled out
                 · Deploys in last 60m (C12/GitHub) → v2.14.3
                 · Conformance report (C7)
                 · Topology subgraph (C5)
14:32:10  C15  SimilarityMatcher → incident #291, similarity 0.91
14:32:11  C16  BudgetGuard: incident budget available ✓
14:32:11  C9   Notification sent. Humans now know. (11 seconds elapsed.)

               ── model layer begins ──

14:32:12  C10  Orchestrator loads skill: incident-triage
14:32:12  C10  ContextAssembler builds 5,500-token bundle:
                 3 failing traces + 1 healthy contrast trace,
                 flow spec section, deviations, topology,
                 deploy diff, incident #291 summary
14:32:13  C10  ModelRouter: root_cause_p1 → M3 frontier
14:32:14  C10  Investigator agent starts
                 iter 1: query_metrics(payment.pool.*)   → 100% active
                 iter 2: query_traces(payment-svc, error) → blocked in acquire
                 iter 3: query_metrics(provider latency) → flat 210ms
                 iter 4: read_file(payment/client.py)    → pool_maxsize=5
14:33:02  C10  OutputValidator ✓ — hypothesis, evidence, confidence present
14:33:02  C9   Hypothesis attached to incident timeline
14:33:02  C17  UI updates; on-call sees diagnosis 64 seconds after onset

               ── remediation ──

14:33:05  C13  AutonomyGate: config-value change at T2 → PR permitted
14:33:06  C13  TraceToCodeMapper → payment/client.py:88
14:33:08  C13  Reproducer [MiniMax] writes concurrency test
14:33:22  C13  TestRunner: test FAILS ✓ (correct — reproduces the issue)
14:33:24  C13  Patcher [MiniMax] → pool_maxsize 5 → 50
14:33:31  C13  TestRunner: reproducer PASSES ✓
14:33:58  C13  RegressionChecker: 411 passed, 0 failed ✓
14:34:02  C13  BlastRadiusReviewer: 2 flows touch this file, both green
14:34:04  C10  Reviewer agent [M3, different model] critiques patch
                 → "correct and minimal; recommend a config bound test
                    as separate work" ✓
14:34:20  C13  EvidenceBundler → PRPublisher → draft PR #1183
14:34:21  C17  PR linked on the incident. Human notified.

               ── resolution and learning ──

14:41:10  —    Human reviews PR (evidence complete), merges, deploys
14:46:33  C8   LatencyShift clears (sustained below clear threshold)
14:48:01  C7   Conformance rate returns to normal
14:49:12  C9   LifecycleManager → resolved (sustained recovery confirmed)
14:49:12  C13  OutcomeWatcher: anomaly stopped post-merge ✓
14:49:15  C15  Incident archived with signature, diagnosis, fix, outcome
14:50:00  C10  Scribe [M3] drafts postmortem → human edits
15:02:00  C15  PatternMiner: "3rd resource-limit incident in payment-svc.
                 Recommend config-boundary tests for this service."
```

## The scoreboard

| Metric | Value |
|---|---|
| Log lines processed during the window | ~2.1 million |
| Model calls made | **9** |
| Time to notification | 11 seconds |
| Time to correct diagnosis | 64 seconds |
| Time to reviewed fix PR | 2 min 23 sec |
| Alerts a human had to triage | **1** (not 5, not 2.1 million) |

## What a conventional setup produces for the same event

- Five separate pages to four engineers
- No indication which service is the cause
- The `notify-svc` queue alert sends someone to the wrong service entirely
- The `order-svc` error is the loudest, so it gets investigated first — also wrong
- Median time to correct diagnosis: 20–60 minutes
- No PR, no test, no memory of it next time

---

# 11. Data schemas

The minimum set of types every component agrees on.

```python
# ─── L1/L2 ────────────────────────────────────────────
RawRecord    = { source_id, host, service, collected_at, payload }
Event        = { id, ts, service, host, level, template_id,
                 trace_id?, span_id?, text_redacted, fields,
                 deploy_version, env }
Template     = { id, pattern, first_seen, last_seen, total_count,
                 example_event_id }

# ─── L3 ───────────────────────────────────────────────
Span         = { span_id, trace_id, parent_id, service, name,
                 start, duration_ms, status, attributes }
Trace        = { trace_id, root_span, spans[], entrypoint,
                 total_duration_ms, status }
Series       = { metric, service, points[(ts, value)], unit }

# ─── L4 ───────────────────────────────────────────────
ServiceGraph = { nodes[Service], edges[{from,to,confidence,method,
                 last_confirmed}] , version }
FlowSpec     = { name, version, entrypoint, steps[FlowStep],
                 invariants[], expected_rate, source }
FlowStep     = { id, service, span, required, critical,
                 max_duration_ms, depends_on[], on_failure,
                 retry?, confidence }
Deviation    = { trace_id, flow, type, step_id, expected,
                 observed, severity }

# ─── L5 ───────────────────────────────────────────────
Baseline     = { key, hour_of_day, day_of_week, mean, stddev,
                 p95, sample_count, updated_at }
Signal       = { id, detector, service, flow?, metric, observed,
                 baseline, ratio, started_at, sustained_s, severity }

# ─── L6 ───────────────────────────────────────────────
Incident     = { id, status, severity, opened_at, resolved_at?,
                 signals[], ranked_cause, blast_radius,
                 affected_flows[], timeline[], evidence, hypothesis?,
                 remediation?, similar_incidents[] }
TimelineEvent= { ts, actor, kind, detail, evidence_refs[] }

# ─── L7/L8 ────────────────────────────────────────────
Evidence     = { traces[], series[], deviations[], deploys[],
                 topology_subgraph, memory_matches[] }
Hypothesis   = { statement, confidence, evidence_refs[],
                 immediate_action, durable_fix, model_used }
Remediation  = { incident_id, files[], reproducer_test,
                 patch_diff, test_results, blast_radius,
                 pr_url, autonomy_tier, outcome? }
Scenario     = { question, basis: static|replay|injection,
                 affected_flows[], predicted_behaviour, gaps[],
                 confidence }
IncidentSignature = { services[], detectors[], deviation_types[],
                 metric_shape, embedding }
```

**One rule about schemas:** `trace_id` is the join key for the entire platform. Every component that can attach one, must. Where it is absent, correlation degrades from a join to a guess, and every layer above L3 gets measurably worse.

---

# 12. Build sequence

The dependency order is real. Several later items simply do not work without earlier ones.

| Phase | Build | Delivers | Blocked without |
|---|---|---|---|
| **0** | C1 collectors, C3 fingerprinting, C4 hot store | Volume reduction; the funnel exists | — |
| **1** | OpenTelemetry instrumentation, `trace_id` propagation | The join key | — |
| **2** | C8 detectors, baselines, hysteresis | Real alerting at zero model cost | 0 |
| **3** | C5 topology mapper | The dependency graph; grouping becomes possible | 1 |
| **4** | C9 incident manager, grouping, cause ranking | "One incident, not forty alerts" | 2, 3 |
| **5** | C2 provider connectors (SigNoz first, behind the interface) | History, baselines, breadth | — |
| **6** | C10 agent runtime, C11 skills, C16 governance | Explanations with evidence | 4, 5 |
| **7** | **C6 flow extraction + C7 conformance** | **The differentiator** | 1, 3, 6 |
| **8** | C15 incident memory | Compounding accuracy | 4, 6 |
| **9** | C13 remediation at T0/T1 only | Draft PRs with tests | 6, 7 |
| **10** | C14 rung 1 static simulation, pre-deploy checks | Prevention, not just response | 3, 6, 7 |
| **11** | C12 Direction B (Aegis as MCP server) | Integration into others' workflows | 4, 7 |
| **12** | C14 rungs 2–3, C13 tier T2/T3 | Verified prediction, safe automation | 9, 10 |

**Phase 1 is the one people skip and regret.** Without propagated trace IDs, phases 3, 4, and 7 all degrade from deterministic joins to statistical guessing. Instrument first.

**Phase 7 is the moat.** Phases 0–6 produce a good observability tool. Phase 7 produces one that catches failures nothing else catches. Do not let it slip.

---

# 13. Anti-patterns

Each of these is a specific way this class of project fails.

| Anti-pattern | Why it fails | Do instead |
|---|---|---|
| **LLM summarises every log line** | Unaffordable; turns noise into expensive noise; nobody reads it | Three-tier funnel (C8) |
| **One alert per signal** | Alert fatigue → the tool gets muted → the project dies | Incident grouping (C9) |
| **Static thresholds** | Fire every Monday morning, silent at 3am | Seasonal baselines (C8) |
| **No hysteresis** | Flapping; 40 pages from one metric hovering at the line | Four-parameter thresholds |
| **Vendor schema above the connector layer** | Adding a second provider becomes a rewrite | `TelemetryProvider` interface (C2) |
| **Fix without reproducing** | Confident patches for misdiagnosed problems | Reproduce-first rule (C13) |
| **Auto-merge to production** | One bad patch destroys trust permanently | Draft PR + evidence, always |
| **Redaction after storage** | The unredacted data was already stored — irreversible | Redact at C3, fail closed |
| **No budget ceiling** | An outage triggers a second, financial outage | Hard limits in C16 |
| **Flow specs that aren't maintained** | False alarms; worse than having none | Regenerate on merge; version in repo |
| **One giant do-everything agent** | Unpredictable, undebuggable, expensive | Narrow sub-agents (§6) |
| **Skills as personality prompts** | "Be thorough" changes nothing | Procedures with steps and checks (C11) |
| **Model hard-coded in components** | Cannot route, cannot self-host, cannot switch | `ModelRouter` config (§9) |
| **Treating silence as health** | Crashed consumers emit nothing at all | `SilenceDetector` and `RateDrop` (C8) |
| **Conformance alerting from day one** | Every valid branch looks like a deviation | Shadow mode for one week (C7) |
| **Skipping OTel because "we have logs"** | Every correlation becomes a guess | Instrument in phase 1 |

---

# Appendix — the one-page summary

**What it does:** learns the intended behaviour of an application from its code, watches actual behaviour, reports the earliest deviation with evidence, and opens a tested pull request to fix it.

**How it stays cheap:** millions of lines → hundreds of templates → dozens of anomalies → a handful of incidents → single-digit model calls. Detection is arithmetic; only explanation is a model.

**What makes it different:** flow conformance. It catches failures that raise no exception — a skipped step, a silently taken fallback, a violated business invariant, a payment charged with no order written. Error-based monitoring is structurally incapable of seeing these.

**What makes it safe:** redaction at ingest, hard budget ceilings, autonomy tiers, reproduce-before-fix, adversarial patch review, and no automated merge to production at any confidence level.

**Where the money goes:** MiniMax-class self-hosted models for code work and anything crossing the data boundary; frontier models for the few human-facing, high-stakes judgements per day.

**Build order that matters:** instrument with OpenTelemetry first, build the funnel second, build the flow specs seventh, and never let the fix agent merge anything on its own.
