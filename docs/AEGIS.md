# Aegis

**Every observability tool tells you what happened. None of them tell you whether it worked.**

Aegis reads the logs you already produce, learns what normal looks like without
being configured, and gives every run a verdict — including the one nobody else
computes: *finished cleanly, achieved nothing*.

```
175 log lines → 26 runs → 9 templates → 3 signals → 2 incidents
                        ↳ 20 achieved · 3 HOLLOW · 2 failed · 1 degraded
```

Those three `hollow` runs returned HTTP 200, charged the customer, emailed a
confirmation — and never reserved the stock. No error was logged because
nothing errored. Every dashboard renders them green.

That funnel is the product, and those are real numbers from
[`examples/shipyard.log`](../examples/shipyard.log) in this repo, which the
[tutorial](#4-tutorial-from-zero-to-a-proven-fix) walks through end to end. On a
production service the same funnel reads 1288 → 776 → 94 → 73 → 13. Storage and search are solved problems. Deciding
which five of a thousand lines matter, and whether the run they describe
actually did its job, is not.

- **Language:** Python 3.10+
- **Dependencies:** none. The standard library only, by design.
- **Licence:** MIT
- **Size:** ~17,700 lines, 371 tests across 30 suites, all passing.

---

## Table of contents

1. [The problem, in one real log](#1-the-problem-in-one-real-log)
2. [What Aegis does differently](#2-what-aegis-does-differently)
3. [Install and first run](#3-install-and-first-run)
4. [Tutorial: from zero to a proven fix](#4-tutorial-from-zero-to-a-proven-fix)
5. [Architecture: the nine layers](#5-architecture-the-nine-layers)
6. [Core concepts](#6-core-concepts)
7. [Every feature, and what it is for](#7-every-feature-and-what-it-is-for)
8. [How agentic it actually is](#8-how-agentic-it-actually-is)
9. [Compared to other tools](#9-compared-to-other-tools)
10. [Design decisions worth defending](#10-design-decisions-worth-defending)
11. [Honest limits](#11-honest-limits)
12. [Configuration reference](#12-configuration-reference)
13. [Contributing](#13-contributing)

---

## 1. The problem, in one log

Throughout this document the examples come from **Shipyard**, a fictional
order-fulfilment platform of five services. The log is in this repo at
[`examples/shipyard.log`](../examples/shipyard.log), so every number below can
be reproduced.

| Service | Job |
|---|---|
| `checkout-api` | receives the order, returns the response |
| `payment-svc` | authorises the card |
| `inventory-svc` | reserves the stock |
| `notify-svc` | emails the customer |
| `search-svc` | catalogue lookup |

Here is one order. Every observability tool on the market renders it green:

```
09:08:28.605 INFO  checkout-api   order.received order_id=ORD-88409 cart_items=4
09:08:28.625 INFO  checkout-api   cart.validated order_id=ORD-88409 subtotal=12790
09:08:28.655 INFO  search-svc     catalogue.lookup order_id=ORD-88409 results=20 in 40ms
09:08:28.695 INFO  payment-svc    payment.authorised order_id=ORD-88409 amount=17490 in 233ms
09:08:28.995 INFO  notify-svc     email.queued order_id=ORD-88409 template=order_confirmed
09:08:29.015 INFO  checkout-api   order.completed order_id=ORD-88409 status=200 in 495ms
```

Zero errors. HTTP 200. 495ms — comfortably fast. Payment authorised for 17490.
The customer has an email saying their order is confirmed.

**Nothing was ever reserved. Compare it to a healthy order:**

```
09:00:00.422 INFO  inventory-svc  stock.reserved order_id=ORD-88401 sku=SKU-165 qty=4
                   ↑ this line has no counterpart in ORD-88409
```

`inventory-svc` never ran. The money left the customer's account, the
confirmation email went out, and no warehouse anywhere knows to ship anything.

No dashboard shows this, because nothing went wrong in the way dashboards
measure. The run completed. The status code was 200. The only thing that failed
was *the purpose of the order* — and no tool on the market has an opinion about
what an order is for.

In `examples/shipyard.log` this happens **three times in 26 orders**. An
error-based tool reports two failures, which are the two declined payments it
was always going to catch. It is silent on the three orders that took money and
delivered nothing.

That gap is the whole product.

### The second problem: nobody can read 3,000 lines

The same service writes 400,000 lines a day. When something breaks, the
engineer's job is to find the five lines that matter. Search helps only if you
already know what to search for.

### The third problem: the evidence often isn't there

Research on root-cause analysis found **26.9% of failures could not be
diagnosed because the evidence was never captured**. No amount of model quality
recovers a log line that was never written. Aegis is the only tool here that
tells you which line to add.

---

## 2. What Aegis does differently

Existing tools answer **"what happened?"**
Aegis answers **"did it work?"**

The difference is a verdict on every run:

| Verdict | Meaning |
|---|---|
| **`achieved`** | Completed and did what this kind of run exists to do |
| **`failed`** | Errored, and said so |
| **`hollow`** | Completed cleanly, achieved nothing — *the case above* |
| **`degraded`** | Achieved it, but slower or stranger than normal |
| **`unknown`** | Nobody has told Aegis what this kind of run is for |

`hollow` is the point. It is the failure no error can show you, and the verdict
no other vendor computes.

`unknown` matters too, and it is deliberate: Aegis will not invent a purpose it
was never told. A run is judged only against a purpose a human confirmed.

---

## 3. Install and first run

### Requirements

Python 3.10 or newer. Nothing else. No database to provision, no agent to
deploy, no account to create.

```bash
git clone <your-fork-url> aegis
cd aegis
python3 run.py
```

Open **http://127.0.0.1:3000**.

That is a complete install. There is no build step because there are no
dependencies — every byte of logic is in the standard library.

### Optional: a model key

Four of the nine layers are pure arithmetic and need no model at all. Reading,
learning, detection and correlation all work with no key.

Only explanation and fix-proposal call a model, only when you click, and the
free tier is the default:

```bash
cp .env.example .env
# GROQ_API_KEY=...   free tier, used for everything by default
# OPENAI_API_KEY=... optional, only touched on explicit request
```

Without a key Aegis runs in pattern mode: it reports what matched, shows no
confidence score, and answers questions with "unknown" rather than guessing.

---

## 4. Tutorial: from zero to a proven fix

End to end on the Shipyard log shipped in this repo, so you can follow along
with no system of your own. Every number below is what the current code
actually printed.

Once you have seen it work, swap `examples/shipyard.log` for your own
application's log file. Nothing in Aegis is tuned to Shipyard — there is no
parser to write and no schema to declare, so your log needs no preparation.

### Step 1 — start it

```bash
python3 run.py
```

Open **http://127.0.0.1:3000**. No build step, no dependencies, no account.

### Step 2 — attach a log

Go to **Sources** and attach `examples/shipyard.log`.

(Three other ways in: a **port**, where Aegis finds the real log of a running
service from its process; a **SigNoz connector**; or any **MCP** server. The
file is the simplest place to start.)

Within seconds the funnel fills:

```
log lines 175 → events 175 → templates 9 → signals 3 → incidents 2
```

Read it as a sequence of narrowings. 175 raw lines became 9 distinct *shapes* of
line. Three of those shapes were statistically unusual. Those three grouped into
two stories.

**You are now reading two incidents instead of 175 lines.**

### Step 3 — read the verdicts

The **Runs** tab shows 26 runs. Every one reads `unknown`:

```
unknown  @09:00:00 · 0s · 7 events
unknown  @09:01:20 · 0s · 7 events
unknown  @09:02:26 · 1s · 7 events
```

This is the honest first-run state, and it is worth sitting with. Aegis can see
exactly what each order *did*. It will not tell you whether that was correct,
because **nobody has told it what an order is for.** A tool willing to guess
here is a tool willing to be confidently wrong.

Two runs do read `failed` — the two declined payments. Those are the ones every
tool catches, and they need no configuration to find.

### Step 4 — say what a run is for

This is the step that unlocks everything. Click **Mark purpose**.

Aegis shows the steps it mined from your own runs, each with the fraction of
runs containing it:

```
1.00  checkout-api   order.received
1.00  checkout-api   cart.validated
1.00  search-svc     catalogue.lookup
0.92  payment-svc    payment.authorised
0.81  inventory-svc  stock.reserved        ← mark this one
0.88  notify-svc     email.queued
0.88  checkout-api   order.completed
```

Nobody wrote that list. It was derived from the log.

Mark `stock.reserved`. It is the step a checkout exists to produce: an order
that validated, charged and emailed but never reserved stock has achieved
nothing, whatever its status code says.

Notice `0.81`. Four of five orders reserve stock; one in five does not. The
presence fraction is already pointing at the bug before any verdict is computed.

**Why a human marks this, and not the model:** a greeting appears in every run,
so frequency marks it required — but a checkout that only validates has done
nothing. The step that makes a run worth having usually appears *only in the
runs that worked*, which is precisely why frequency cannot find it. (A model can
propose the marking on your click; the spec records who decided.)

### Step 5 — read the verdicts again

```
achieved  20
hollow     3     ← paid, confirmed, nothing reserved
failed     2     ← declined payments
degraded   1     ← 6s against a 1s p95, still HTTP 200
```

Open a `hollow` card and it states the finding:

```
hollow   ORD-88409
  completed cleanly - no errors, normal teardown - but none of its
  purpose steps ever ran

  What a run of this kind must do
    ✗ inventory-svc  stock.reserved   - never ran

  What this run actually did · 6 steps
    09:08:28  order.received      order_id=ORD-88409
    09:08:28  cart.validated      subtotal=12790
    09:08:28  catalogue.lookup    results=20 in 40ms
    09:08:28  payment.authorised  amount=17490 in 233ms
    09:08:28  email.queued        template=order_confirmed
    09:08:29  order.completed     status=200 in 495ms
```

That is the claim and the evidence for it, together. Three orders took money and
shipped nothing, and nothing else in your stack was ever going to say so.

The `degraded` one is the other case worth knowing: `catalogue.lookup` took six
seconds against a one-second p95 and still returned 200. No threshold was
configured — the baseline came from the log.

### Step 6 — see the incident

The **Incidents** tab groups related signals into one story, cause ranked first:

```
INC-1   RateSpike @09:14  payment.declined
RANKED CAUSE
        why ranked: earliest onset of 2 members ·
                    no flow graph yet - ranked by timing alone
```

Note what it says about itself: *ranked by timing alone*. Without a flow graph
it cannot do better, and it says so rather than implying confidence it does not
have.

### Step 7 — explain it (the first paid step)

Click **Explain · 1 call**.

One model call, with the evidence attached. The reply is checked against the
logs before you see it: a claim citing evidence not present in the log is
rejected, not softened. Free tier by default — nothing expensive runs without a
click.

### Step 8 — investigate (the agentic loop)

Click **Investigate · runs tools**. Now the model drives, choosing from six
tools over up to six steps:

```
1. search_logs       — other occurrences of this shape
2. get_baseline      — what normal looks like for this operation
3. get_run_verdicts  — which runs were affected
4. get_similar_past  — has this happened before?

ruled out: a downstream outage - get_dependencies reported no
           observed dependency failures in the window
```

It records every step, the reason for it, and the hypotheses it discarded. A
surviving hypothesis is marked `GROUNDED` with its confidence.

A tool that cannot answer is not offered: `get_code_for` appears only once the
project has been analysed, because a tool that can only reply "not available"
still costs one of six steps to find that out.

### Step 9 — propose a fix

This step needs a repository, so it applies when you are watching your own
service rather than the example log.

Point **Repository path** at the checkout that produced the logs — Aegis
pre-fills it from the traceback — and click **Propose fix**:

```
incident
   ↓  map the error to an exact line           checkout.py:214
   ↓  write a test that FAILS                  proves the bug is understood
   ↓  write a minimal patch                    ≤ 40 changed lines
   ↓  the same test now PASSES                 proof, not a claim
   ↓  DRAFT
```

**No patch is proposed without a test that failed before it and passes after.**
If the test does not flip, Aegis refuses, tells you it refused, and shows you
what it tried. Fifteen of the sixteen outcomes in this protocol are refusals.

### Step 10 — apply it, or do not

Applying runs your own test suite — but only the tests that touch the code the
patch changed, selected by measured duration and module imports. On a production
Python service whose full suite is 136 tests and ten minutes, that was **38
tests in 1.6 seconds with no API cost**. A cheap test that cannot catch the
change is false comfort, not verification.

Aegis refuses to apply while your repo has uncommitted changes, so its work
stays separable from yours. Every change is revertible. Nothing is ever
committed.

### Step 11 — when it cannot explain something

Where the trail goes cold, Aegis names the log line that would have let it
continue:

```
The trail ends at services/checkout/reserve.py:88

GAP  reserve.py:89
  This is the last thing logged in this function. Whatever it calls next
  says nothing about itself, so a run that stops there cannot be told
  from one that never got there.
  → Log one line when this function finishes, with how long it took.
```

Research on root-cause analysis found **26.9% of failures could not be diagnosed
because the evidence was never captured.** That loop — read the code, find the
blind spot, name the line — is the one no competitor closes, because closing it
requires the repository, not just the logs.

### Step 12 — point it at your own system

```bash
# a log file
python3 run.py          # attach /var/log/yourapp/app.log in the UI

# or a running service, by port — Aegis finds its real log file
# or a SigNoz connector, or any MCP server
```

Then repeat steps 3 and 4: read the mined steps, mark the one your runs exist to
produce. That is the whole setup. There is no parser to write, no schema to
declare, and no instrumentation to add.

## 5. Architecture: the nine layers

Each layer has one job and hands a defined contract to the next. If a layer's
output is not an order of magnitude smaller than its input, that layer is broken.

| Layer | Name | LOC | Job |
|---|---|---|---|
| **L1** | Ingestion | 1,677 | Read from a file, a port, or a connector. Read-only. |
| **L2** | Normalization | 889 | One record per event. Redact, fingerprint, correlate. |
| **L3** | Storage | 409 | One SQLite file per project. No shared tables. |
| **L4** | Understanding | 2,138 | Mine templates, baselines, flow specs. Read the repo. |
| **L5** | Detection | 570 | Seven statistical detectors. No model. |
| **L6** | Correlation | 685 | Group signals into incidents, rank the cause. |
| **L7** | Reasoning | 782 | The only layer allowed to call a model. |
| **L8** | Action | 2,437 | Propose, verify and apply a fix. |
| **L9** | Interface | — | Web UI, HTTP API, MCP server. |

### L1 — Ingestion

Three source kinds:

- **File** — tails a log file. Handles rotation by inode (logrotate renames and
  recreates, so a size check alone reads mid-file garbage), caps backfill on
  huge files, truncates pathological lines, and holds back a trailing fragment
  until the writer finishes it.
- **Port** — finds a service's real log from its process. It ranks a file the
  app opened for itself above one the shell redirected into stdout, which is how
  it tells `checkout.app.log` (the application's own logger, thousands of
  lines) from `checkout.log` (two lines of server startup the shell
  redirected) — two files in the same directory with near-identical names.
- **Connector** — SigNoz over its v5 query API, and anything exposing an MCP
  server. Aegis *reads from* these; it does not compete with them.

### L2 — Normalization

Four things happen here, in order:

1. **Multi-line folding.** One Java exception with a 40-frame stack and a
   `Caused by` chain is one event, not 41.
2. **Redaction.** Emails, phones, cards, tokens and IPs become `<EMAIL>`,
   `<PHONE>` and so on *before* storage. There is no unredacted copy anywhere
   downstream, because redaction after storage is not redaction.
3. **Fingerprinting.** `"/chat FAILED after 2889ms"` and `"/chat FAILED after
   1531ms"` are one template, not two. Status classes are a hard split: an
   access log's 200s and 502s differ in almost nothing else, and merging them
   makes a 502 storm statistically invisible.
4. **Correlation.** A `trace_id` is *extracted* when the line states one, and
   *inferred* only when exactly one session is open. With two or more open, the
   line gets no trace at all — attributing a line to the wrong customer's trace
   is worse than attributing it to none. Every event records which of the two it
   was.

A service that emits no correlation id still produces runs: a request/response
service marks its own boundaries (`request received` … `responded`), and a
synthetic session is capped so a bad guess stays small instead of swallowing
the file.

### L3 — Storage

One SQLite file per project under `~/.aegis/projects/<name>/store.db`. Per-project
scoping is physical, not a `WHERE` clause — there are no shared tables, so one
project's data cannot leak into another's analysis.

Raw text is never stored. The `templates` table holds the masked template; a
single readable example is kept alongside it.

### L4 — Understanding

Where Aegis works out what your system does, without being told:

- **Templates** — the distinct shapes of line, mined with a Drain-style tree.
- **Baselines** — rolling p50/p95 per `(component, operation)`, from durations
  the parser already extracts. Reported only with ≥10 samples; below that it
  says "not enough history yet."
- **Flow specs** — what a run of this kind normally does, mined from real runs.
  Human-editable, stored as a versioned file you can open and fix. Every
  auto-derived step carries its presence fraction so you know what to review.
- **Codebase analysis** — parses your repo's AST to map log statements to source
  lines, find unguarded external calls, and locate instrumentation gaps.

`critical` — which step is the run's *purpose* — is never auto-derived. The spec
records who set it: a human, or the model on a human's click.

### L5 — Detection

**Seven detectors, all statistics, no model** — so detection cannot hallucinate
an alert:

| Detector | Catches |
|---|---|
| `NoveltyDetector` | a line shape never seen before |
| `RateSpike` | a template firing far more than usual |
| `RateDrop` | a template that has nearly stopped |
| `SilenceDetector` | a whole service going quiet mid-stream |
| `LatencyShift` | 6,946ms against an 800ms baseline — *and it returned 200* |
| `ErrorRatio` | the error share of a window moving |
| `CostAnomaly` | tokens per unit of work, against that work's own history |

The research is explicit on why this split matters: vendors ship statistical
anomaly detection unhedged while labelling their LLM assistants with
hallucination warnings. Same reasoning here.

### L6 — Correlation

Twenty alerts about one outage is noise. Signals that share a trace, a service,
a template or a time window become one incident, and one member is ranked as the
likely cause. The ranking always states its own basis — "earliest onset of 2
members · ranked by timing alone" is a weaker claim than one backed by a flow
graph, and it says so.

Incidents are remembered. A new incident is matched against past ones, so
"this happened before, and here is what fixed it" is available on the second
occurrence.

### L7 — Reasoning

The only layer that may call a model, under three constraints:

- **Governed.** A hard call budget per shell (10) and per investigation (12),
  with a minimum interval between calls. Every call is logged to an audit
  trail with its purpose.
- **Grounded.** A hypothesis citing evidence not present in the logs is
  rejected, not softened. The same check that validates a quote validates a
  verdict.
- **Free by default.** Groq's free tier runs everything. A paid key is touched
  only on explicit request.

### L8 — Action

The fix protocol, and the refusals that make it trustworthy. See
[section 8](#8-how-agentic-it-actually-is).

---

## 6. Core concepts

### Event

One normalized, redacted log record. The name `text_redacted` carries the
guarantee: by the time an Event exists, sensitive values are already gone.

### Template

The shape of a line with its variables masked. `INFO Request received <*>
<PATH>` is one template covering thousands of lines. Templates are what make
"this has never happened before" computable.

### Signal

One statistical finding from one detector: what was observed, what the baseline
was, the ratio between them, and the evidence line.

### Incident

A group of related signals, with one ranked as the cause, plus blast radius,
timeline and precedents.

### Run (trace)

A sequence of events sharing a trace id. The unit a verdict applies to.

### Flow spec

What a run of this kind is *supposed* to do. Mined from real runs, confirmed by
a human. Without one, every verdict is `unknown` — by design.

### Verdict

`achieved` / `failed` / `hollow` / `degraded` / `unknown`. See
[section 2](#2-what-aegis-does-differently).

### Correlation basis

Whether a run's membership is `extracted` (the line stated its id — evidence) or
`inferred` (exactly one session was open — an assumption). Downstream layers
know which one they are holding.

---

## 7. Every feature, and what it is for

### Watching

| Feature | What it does |
|---|---|
| **File watching** | Tails a log file. Rotation-safe, backfill-capped. |
| **Port watching** | Finds a running service's real log from its process. |
| **SigNoz connector** | Reads logs and traces over the v5 query API. |
| **MCP connector** | Reads from anything exposing an MCP server. |
| **Multi-service** | One connector holding 12 services polls each fairly, and each record keeps its own service identity. |

### Understanding

| Feature | What it does |
|---|---|
| **Template mining** | Discovers line shapes. No parser to write, no schema to define. |
| **Baselines** | p50/p95 per operation, from durations already in your logs. |
| **Flow mining** | Derives what a run normally does from real runs. |
| **Purpose marking** | You confirm which steps are the run's reason to exist. |
| **Code analysis** | Maps log lines to source lines by parsing your repo's AST. |
| **Dependency map** | Which external services this code calls, and whether those calls are guarded. |

### Finding

| Feature | What it does |
|---|---|
| **Seven detectors** | Novelty, rate spike/drop, silence, latency, error ratio, cost. |
| **Incident grouping** | Many signals, one story, one ranked cause. |
| **Precedent memory** | "This happened before; here is what fixed it." |
| **Run verdicts** | Including `hollow` — completed cleanly, achieved nothing. |
| **Live brief** | What is happening right now, in plain English. |

### Explaining

| Feature | What it does |
|---|---|
| **Explain** | One governed model call, with evidence, grounded against the logs. |
| **Investigate** | The model drives its own tools for up to six steps, recording what it ruled out. |
| **Gap reports** | Which missing log line stopped the analysis, named by file and line. |
| **Simulation** | What would break if this component failed. |
| **Audit report** | Downloadable record of what was found and what was claimed. |

### Fixing

| Feature | What it does |
|---|---|
| **Propose fix** | Reproducer must fail, then pass. No proof, no proposal. |
| **Scope gate** | ≤40 changed lines; never a test file, a secret, CI or a dependency manifest. |
| **Patch coherence** | A diff whose hunks would corrupt the file is rejected before it is applied. |
| **Syntax check** | Every patched Python file must still parse, or the patch is named as the cause. |
| **Relevant tests only** | Runs the tests that touch the changed code — 38 of 136, 1.6s, no API cost. |
| **Clean-tree rule** | Refuses to apply while you have uncommitted changes. |
| **Revert** | Every applied change is undoable. Nothing is ever committed. |
| **Circuit breaker** | Two failed attempts and it stops, and says a person is needed. |

### Interfaces

| Feature | What it does |
|---|---|
| **Web UI** | Now / Runs / Incidents / Audit, light and dark. |
| **HTTP API** | Everything the UI does, as JSON. |
| **MCP server** | Aegis as a tool for your own agent. |
| **CLI** | `log-agent`, `log-agent-mcp`. |
| **/healthz** | Liveness and the running version. |

---

## 8. How agentic it actually is

"Agentic" is usually a claim about autonomy. Here it is a claim about
**verification and refusal**, which is a different and more defensible thing.

### Three agentic loops

**1. The investigation loop.** Given an incident, the model chooses its own
tools over up to six steps, from six available: `search_logs`, `get_baseline`,
`get_code_for`, `get_dependencies`, `get_run_verdicts`, `get_similar_past`. It
records every step, the reason for it, and the hypotheses it ruled out.

A tool that cannot answer is not offered. `get_code_for` appears only when the
project has been analysed — a tool that can only reply "not available" still
costs one of six steps to find that out.

**2. The remediation loop.** Evidence → map to code → write a failing test →
write a patch → the test must pass → draft. Each stage can refuse.

**3. The verification loop.** On apply: run the tests that touch the changed
code, and revert if they fail.

### Why it refuses, and how often

**15 of the 16 outcomes in the fix protocol are refusals.** One is a draft.
Four kinds:

| Outcome | Meaning |
|---|---|
| **DRAFT** | Proven. The test failed before and passes after. |
| **BLOCKED** | Could not get far enough to try. |
| **STOPPED** | The diagnosis was wrong, or the test was backwards. |
| **ADVISE** | A person is needed; here is what was tried. |

This is the central design claim, and it is worth stating plainly:

> **No patch reaches your code without a test that failed before it and passes
> after.**

That is not a model saying "I fixed it." It is a measurement.

### Why a refusing tool is the right design

Independent benchmarks measure AI root-cause accuracy at **3.9–12.5%**, where
vendors claim 82–90% — across 1,675 runs and 1.38 billion tokens, with failures
persisting across every model tier. That is a framework problem, not a model
problem.

In that light, a tool that always produces an answer is a liability. If someone
asks "will it fix any bug?", the honest answer is the strong one:

> *"No. It fixes bugs it can prove it fixed. When it can't, it says so and shows
> you what it tried."*

### Guardrails, concretely

- Detection uses **no model** — statistics cannot hallucinate an alert.
- A model's output is **checked against the logs** before display.
- A patch is capped at **40 changed lines**; a rewritten function is rejected unread.
- Test files, secrets, CI config and dependency manifests are **never patchable**.
- **Read-only until a click.** Aegis writes only under `~/.aegis/`, never into a
  monitored project.
- Two failures and the **circuit breaker** stops it.

---

## 9. Compared to other tools

| | Logging tools (Datadog, SigNoz, ELK) | An LLM on logs | **Aegis** |
|---|---|---|---|
| Stores and searches logs | ✅ | ❌ | Doesn't try to |
| Says what happened | ✅ | ✅ | ✅ |
| **Says whether it worked** | ❌ | Guesses | ✅ verdict per run |
| **Catches "200 OK but wrong"** | ❌ | ❌ | ✅ |
| **Proves a fix before proposing it** | ❌ | ❌ | ✅ the test must flip |
| **Says what you failed to log** | ❌ | ❌ | ✅ named file and line |
| Needs instrumentation work | ✅ usually | ❌ | ❌ reads what you already write |
| Refuses when unsure | — | Rarely | ✅ 15 refusal points |
| Cost to run | $$$ per GB | $$ per query | Free tier by default |

**Aegis is not a competitor to SigNoz.** It *reads from* SigNoz — that is a
supported source, verified against a live instance. Storage and search are
solved, expensive, and commoditising. Aegis computes the thing nobody computes.

SigNoz's own documentation names the "200 OK but wrong" problem and does not
solve it. They ingest `gen_ai.evaluation.score.value` and never compute it.

### What this deliberately does not build

- **Log storage, search, dashboards, alerting** — solved, expensive, losing ground.
- **Autonomous root-cause analysis** — 3.9–12.5% measured accuracy; the
  category's central false promise.
- **NL→query, log clustering, MCP servers** — commodity; every vendor ships them.
- **A generic AI SRE** — a three-person team replicated most of one in 3.5 weeks.

---

## 10. Design decisions worth defending

These come up in technical review.

**Detection uses no AI.** Statistics cannot hallucinate an alert. The model is
allowed in only for explanation, on a click, with evidence attached — and its
output is checked against the logs before being shown.

**Nothing is claimed that isn't cited.** A verdict citing evidence not present
in the logs is a bug, not a near miss.

**`unknown` is a feature.** Aegis will not invent a purpose for your runs. Until
a human marks which steps matter, every verdict is `unknown` and says why.

**Correlation distinguishes evidence from assumption.** Every event records
whether its trace id was extracted from the line or inferred from context. With
two sessions open, an unkeyed line gets no trace at all.

**Redaction happens before storage.** There is no unredacted copy downstream,
because redaction after storage is not redaction.

**Per-project isolation is physical.** One SQLite file per project, no shared
tables. Aegis writes only under `~/.aegis/` and never into a monitored project.

**It never writes to your repo uninvited.** Read-only until a click. It refuses
to apply while you have uncommitted changes, so its work stays separable from
yours. Every change is revertible; nothing is ever committed.

**It verifies patches cheaply.** Running only the tests that touch the changed
code turned a production Python service's 136-test, ten-minute suite into 38
tests in 1.6 seconds with no API cost. A cheap test that cannot catch the change
is false comfort.

**Costs are the user's to authorise.** Free tier by default; nothing expensive
runs without a click; every call is audited.

**Explanations are shown with their sources.** Controlled study (N=308): fluent
explanations raise reliance on *wrong* answers as much as right ones. Only
sources calibrate trust. So every claim carries the line that proves it, inline
and visible — not behind a toggle.

---

## 11. Honest limits

Stated plainly, because a tool whose pitch is calibrated honesty cannot have a
dishonest limitations section.

**It fixes bugs that leave a traceback.** A wrong *value* with no error gives
nothing to map to code.

**Failures that produce no log output are invisible.** This limit is not
hypothetical. On a real service, a GPU driver bug corrupted memory and killed
the process outright — no error, no traceback, nothing in any log. The only
evidence was an OS-level crash report outside the application entirely. Aegis
could not see it, and no log-reading tool could have.

The gap report exists to name that class of blind spot, and it is the honest
framing of the limit: Aegis can tell you a run stopped at a line and that
nothing after it was ever logged. It cannot tell you why, when the cause left no
trace. Being able to say *"the evidence for this does not exist"* is more useful
than a confident guess, but it is not a diagnosis.

**Verdicts need a purpose.** Without marked steps every run is `unknown`. That
is the honest answer, but it is a setup cost.

**The reproducer is as heavy as your code.** If a bug is only reachable through
a live database and live model calls, the generated test needs them too, and may
fail for environmental reasons rather than logical ones.

**One connector is verified live.** SigNoz is tested against a real instance.
The MCP connector is implemented but not verified against every server.

**Inference is capped, not solved.** A service with genuinely concurrent
unkeyed requests will leave lines unattributed rather than guess. That is the
right trade, but it means fewer runs than a tool willing to guess would claim.

**Single-node, local-first.** No clustering, no HA, no multi-tenant auth. It is
a tool you run, not a service you operate.

---

## 12. Configuration reference

All configuration is environment variables, read from `.env` or the environment.
Every one has a working default.

| Variable | Default | What it does |
|---|---|---|
| `GROQ_API_KEY` | — | Free-tier model key; used for everything by default |
| `OPENAI_API_KEY` | — | Optional; touched only on explicit request |
| `LOG_AGENT_LLM` | `true` | `false` forces pattern mode even with a key |
| `LOG_AGENT_HOST` | `127.0.0.1` | Bind address |
| `LOG_AGENT_PORT` | `3000` | Port for `run.py` |
| `AEGIS_BACKFILL_MB` | `10` | How much of an existing file to read on attach |
| `AEGIS_MAX_LINE_BYTES` | `65536` | Pathological lines are truncated here |

### Data locations

| Path | Contents |
|---|---|
| `~/.aegis/projects/<name>/store.db` | That project's events, templates, signals |
| `~/.aegis/projects/<name>/flows/` | Flow specs — human-editable |
| `~/.aegis/projects/<name>/proposals/` | Fix proposals, reproducers, patches |
| `~/.aegis/providers.json` | Saved connectors |
| `~/.aegis/audit.jsonl` | Every model call, with its purpose |

Nothing is written outside `~/.aegis/` unless you click Apply on a fix.

### Ports

| Command | Port |
|---|---|
| `python3 run.py` | 3000 |
| `python3 -m aegis.server <log>` | 8600 |

### Running the tests

```bash
for t in tests/test_*.py; do python3 "$t"; done
```

371 tests, 30 suites, no dependencies, no network, no API keys required.

---

## 13. Contributing

### The conventions that matter

**Every test is named for the failure it prevents.** Not
`test_parse_timestamp` but `test_a_connector_record_keeps_its_own_timestamp`.
The name should tell a future reader what breaks if this test is deleted.

**Comments explain why, not what.** The codebase documents the bug each guard
exists for, because the next person to simplify that line needs to know what it
cost to learn.

**A new test must fail against the old code.** If it passes either way, it is
not testing what you think.

**Layer boundaries are real.** Each layer takes one contract and returns
another. `app/core/*` must stay free of `aegis` imports — a test enforces it.

**No dependencies.** Standard library only. This is a product promise, and a
test checks it.

### Verifying a change

Run against real logs, not synthetic ones. Nearly every bug that mattered in
this codebase was found by running it against a real service and almost none by
unit tests alone — including a capped backfill that fabricated its first log
line, a correlation layer silently disabled for whole files, and a model step
that crashed on every call.

---

## Appendix — evidence for the claims

- RCA accuracy 3.9–12.5% vs 82–90% claimed: [OpenRCA analysis](https://arxiv.org/html/2602.09937v2)
- Explanations raise reliance on wrong answers; sources reduce it: [arXiv 2502.08554](https://arxiv.org/abs/2502.08554)
- 5× better bug resolution from clarifying turns: [arXiv 2402.06229](https://arxiv.org/abs/2402.06229)
- Trace-invisible failures across six tools tested: [Do Automated Evals Work?](https://parlance-labs.com/blog/posts/auto-evals/index.html)
- SigNoz ingests but does not compute evals: [LLM observability docs](https://signoz.io/docs/llm-observability/)
- SigNoz anomaly detection is metrics-only: [docs](https://signoz.io/docs/alerts-management/anomaly-based-alerts/)
- Co-founder on the AI SRE moat: [Unhyped take on MCP servers](https://signoz.io/blog/unhyped-take-on-mcp-servers/)
- Dashboards as cached stale questions: [Charity Majors](https://charity.wtf/2021/08/09/notes-on-the-perfidy-of-dashboards/)
- 97% of alerts need no immediate action: [incident.io](https://incident.io/blog/sre-alerting-best-practices)
