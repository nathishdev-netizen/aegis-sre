# Aegis

**Every observability tool tells you what happened. None of them tell you
whether it worked.**

One order from [`examples/shipyard.log`](examples/shipyard.log). Every tool on
the market renders it green:

```
09:08:28.605 INFO  checkout-api   order.received order_id=ORD-88409 cart_items=4
09:08:28.625 INFO  checkout-api   cart.validated order_id=ORD-88409 subtotal=12790
09:08:28.695 INFO  payment-svc    payment.authorised order_id=ORD-88409 amount=17490
09:08:28.995 INFO  notify-svc     email.queued order_id=ORD-88409 template=order_confirmed
09:08:29.015 INFO  checkout-api   order.completed order_id=ORD-88409 status=200 in 495ms
```

Zero errors. HTTP 200. 495ms. The card was charged and the customer has an email
saying their order is confirmed.

**No stock was ever reserved.** No warehouse knows to ship anything. There is no
error to alert on, because nothing errored — and in that log it happens three
times in twenty-six orders.

Aegis calls that run **`hollow`**: completed cleanly, achieved nothing. It is
the failure no error can show you.

### See it run

A 10-minute narrated walkthrough, built from real captured output — every
number on screen comes from a live run against
[`examples/shipyard.log`](examples/shipyard.log), not a mock-up:

**[demo-video/out/aegis-demo.mp4](demo-video/out/aegis-demo.mp4)** · subtitles
included ([.srt](demo-video/out/aegis-demo.srt))

Or run the walkthrough yourself, scene by scene, against your own log:

```bash
./demo.sh              # eight scenes, pausing between each
./demo.sh --check      # verify the environment, run nothing
```

**Then it does something about it, and then it checks whether that worked.**

```
WATCH  ──→  NOTICE  ──→  JUDGE  ──→  EXPLAIN  ──→  FIX  ──→  LEARN
 logs        7 stat.     verdict     evidence +    test     did it hold?
 & code      detectors   per run     your code     must     grade it,
 read-only   statistical incl.       mapped to     flip     publish the
             involved    hollow      the line               hit rate
                                                               │
             ┌─────────────────────────────────────────────────┘
             ↓
      what it learned changes what it does next
```

Six stages, one tool, **no dependencies**. The last one is the point: every
other tool in this category stops at FIX.

### What you actually get, in front of you

**An analytics layer over logs that are still arriving.** Not a dashboard you
configured — a live view that builds itself from the stream: templates, baselines,
runs, verdicts, incidents, all recomputed as lines land. Point it at a file, a
port or a connector and it is reading within two seconds, with no schema, no
parser and no instrumentation added to your app.

**A plain-English brief of what is happening right now**, written as the stream
moves — and a switch to turn it off, because it is the one part that is
generated prose rather than a measurement. Off, every number stays and nothing
is spent narrating.

**A chatbot over your own logs.** Ask *"did any order complete without
reserving stock?"* and the answer comes back with **the log lines it is based
on**, a confidence, and a stated source — or an honest refusal. Every claim
cites evidence that is actually in your logs, or it is rejected rather than
softened.

It answers about **the run in front of you** — the latest run's last 60 lines.
Questions across the whole archive are [not built yet](#honest-limits).

**Then the SRE loop on top of that layer**: an incident with its cause ranked,
a diagnosis grounded in the lines that prove it, a fix with a test that had to
fail before it could pass, and a grade on that fix five minutes later.

### Where it fits in production

It sits **beside** what you already run, not instead of it. Storage, search and
dashboards are solved — Aegis reads *from* SigNoz as one of its sources. What it
adds is the layer above: the per-run verdict, the ranked incident, the proven
fix, and the measurement of whether that fix held.

Concretely: your dashboards keep telling you the checkout API returns 200 in
495ms. Aegis tells you three of those orders never reserved stock, which line of
code is responsible, and what to change.

- **Language:** Python 3.10+
- **Dependencies:** none. The standard library only, by design.
- **Licence:** MIT
- **Size:** ~18,200 lines, 379 tests across 30 suites, all passing.

---

## Contents

1. [The problem](#the-problem)
2. [What Aegis does differently](#what-aegis-does-differently)
3. [Install](#install)
4. [The loop, end to end](#the-loop-end-to-end)
5. [Stage 1 — Watch](#stage-1--watch)
6. [Stage 2 — Notice](#stage-2--notice)
7. [Stage 3 — Judge](#stage-3--judge)
8. [Stage 4 — Explain](#stage-4--explain)
9. [Stage 5 — Fix](#stage-5--fix)
10. [**Stage 6 — Learn**](#stage-6--learn) ← the stage nobody else has
11. [How agentic it actually is](#how-agentic-it-actually-is)
12. [Tutorial: from zero to a proven fix](#tutorial-from-zero-to-a-proven-fix)
13. [Every feature, and what it is for](#every-feature-and-what-it-is-for)
14. [Compared to other tools](#compared-to-other-tools)
15. [Architecture and concepts](#architecture-and-concepts)
16. [Design decisions worth defending](#design-decisions-worth-defending)
17. [Honest limits](#honest-limits)
18. [Reference](#reference)
19. [Contributing](#contributing)

---

## The problem

Throughout this document the examples come from **Shipyard**, a fictional
order-fulfilment platform of five services. The log is in this repo at
[`examples/shipyard.log`](examples/shipyard.log), so every number below can
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

## What Aegis does differently

Existing tools answer **"what happened?"** Aegis answers **"did it work?"** —
and then acts on the answer. Four things follow from that, and no other tool in
this category does all four:

**1. A verdict on every run, not just the failed ones.** Including `hollow`:
completed cleanly, achieved nothing. That is the failure no error can show you,
and the verdict no other vendor computes.

**2. It reads your code, not only your logs.** The AST is parsed to map log
statements to the source lines that wrote them — which is what lets it point at
`reserve.py:89` instead of saying "something went wrong in the checkout
service", and what lets it name the log line you never wrote.

**3. A fix has to prove itself.** A test that failed before the patch and passes
after, or no proposal at all. Fifteen of its sixteen outcomes are refusals.

**4. It grades its own past work.** Five minutes after you mark a fix as
working, it checks whether the problem came back — and downgrades its own
verdict if it did. It then publishes its hit rate on your incidents.

The four build on each other: you cannot prove a fix without reading the code,
and you cannot grade the fix without a verdict to measure against.

---

## Install

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

Reading, learning, detection and correlation all work with no key. Explanation
and fix-proposal need one, and the free tier is the default:

```bash
cp .env.example .env
# GROQ_API_KEY=...   free tier, used for everything by default
# OPENAI_API_KEY=... optional, only touched on explicit request
```

Without a key Aegis runs in pattern mode: it reports what matched, shows no
confidence score, and answers questions with "unknown" rather than guessing.

---

---

## The loop, end to end

Six stages, running in order. Nothing to configure and nothing to instrument.

| Stage | What happens |
|---|---|
| **1. Watch** | Read a log file, a port, or a connector — and the repository behind them. Read-only. |
| **2. Notice** | Seven statistical detectors; related signals grouped into one incident with a ranked cause. |
| **3. Judge** | Every run gets a verdict, including `hollow`: completed cleanly, achieved nothing. |
| **4. Explain** | A diagnosis with the evidence attached, checked against the logs before you see it. |
| **5. Fix** | Map to the line, write a failing test, patch, the test must pass. 15 of 16 outcomes refuse. |
| **6. Learn** | Re-measure the fix, downgrade it if the problem returned, publish the hit rate. |

**Detection is deterministic, so it cannot hallucinate an alert** — statistics
do the noticing, and reasoning is only ever asked to explain what they found.

The loop closes because stage 6 feeds stage 4: an incident that recurs arrives
with its own history — what was diagnosed, what was done, whether it worked.

---

## Stage 1 — Watch

Three kinds of source, all read-only:

- **A log file** — tailed, rotation-safe by inode, backfill capped, pathological
  lines truncated, and a trailing fragment held back until the writer finishes it.
- **A port** — Aegis finds the running service's real log from its process. It
  ranks a file the app opened for itself above one the shell redirected into
  stdout, which is how it tells `checkout.app.log` (the application's own
  logger, thousands of lines) from `checkout.log` (two lines of server startup).
- **A connector** — SigNoz over its v5 query API, or anything exposing an MCP
  server. Aegis *reads from* these; it does not compete with them.

And then the part most tools skip: **it reads the repository too.** The AST is
parsed to map log statements to their source lines — 73 of them on one real
service — which external calls are unguarded, and what each component depends
on. That is what makes stages 4 and 5 possible.

| Feature | What it does |
|---|---|
| **File watching** | Tails a log file. Rotation-safe, backfill-capped. |
| **Port watching** | Finds a running service's real log from its process. |
| **SigNoz connector** | Reads logs and traces over the v5 query API. |
| **MCP connector** | Reads from anything exposing an MCP server. |
| **Multi-service** | One connector holding 12 services polls each fairly, and each record keeps its own service identity. |

### What it works out on its own

| Feature | What it does |
|---|---|
| **Template mining** | Discovers line shapes. No parser to write, no schema to define. |
| **Baselines** | p50/p95 per operation, from durations already in your logs. |
| **Flow mining** | Derives what a run normally does from real runs. |
| **Purpose marking** | You confirm which steps are the run's reason to exist. |
| **Code analysis** | Maps log lines to source lines by parsing your repo's AST. |
| **Dependency map** | Which external services this code calls, and whether those calls are guarded. |

Nobody writes a parser. Nobody declares a schema.

---

## Stage 2 — Notice

**Seven detectors, all statistics** — so detection cannot hallucinate
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

Twenty alerts about one outage is noise, so signals sharing a trace, a service,
a template or a time window become **one incident** with one member ranked as
the likely cause. The ranking always states its own basis — *"earliest onset of
2 members · ranked by timing alone"* is a weaker claim than one backed by a flow
graph, and it says so.

| Feature | What it does |
|---|---|
| **Seven detectors** | Novelty, rate spike/drop, silence, latency, error ratio, cost. |
| **Incident grouping** | Many signals, one story, one ranked cause. |
| **Precedent memory** | "This happened before; here is what fixed it." |
| **Run verdicts** | Including `hollow` — completed cleanly, achieved nothing. |
| **Live brief** | What is happening right now, in plain English — [a switch](#the-live-brief-is-a-switch), on by default. |

---

## Stage 3 — Judge

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

---

## Stage 4 — Explain

| Feature | What it does |
|---|---|
| **Explain** | A diagnosis with its evidence, grounded against the logs. |
| **Investigate** | The model drives its own tools for up to six steps, recording what it ruled out. |
| **Gap reports** | Which missing log line stopped the analysis, named by file and line. |
| **Simulation** | What would break if this component failed. |
| **Audit report** | Downloadable record of what was found and what was claimed. |

### The live brief is a switch

The brief is the one part of this page that is **generated prose rather than a
measurement**, and the only thing that costs anything just by being watched.
So it is a switch, in the card's own header, on by default:

```
LIVE BRIEF                                    ●── on
The run reached its end, but a step failed.
```

```
LIVE BRIEF                                   ──○ off
Off — measurements only. Nothing is spent narrating the run.
```

**Off means not written, not merely not shown.** Nothing is spent to
narrate the run, and whatever brief was on screen is cleared — the last one
written before the switch would otherwise sit there looking current.

Everything else is arithmetic and unaffected: funnel, templates, signals,
incidents, verdicts, detection. With the brief off, the Shipyard log still
reports all 175 events, 9 templates and 26 verdicts — nothing measured
depends on the narration.

Why this is a switch and not a setting buried in a menu: a controlled study
(N=308) found that fluent explanations raise reliance on *wrong* answers as
much as right ones, and that only sources calibrate trust. A reader who wants
measurements without narration should be one click away from them.

Start it off with `LOG_AGENT_DEV_MODE=false`, or toggle at
`POST /api/dev-mode`.

---

## Stage 5 — Fix

```
incident
   ↓  map the error to an exact line           checkout.py:214
   ↓  write a test that FAILS                  proves the bug is understood
   ↓  write a minimal patch                    ≤ 40 changed lines
   ↓  the same test now PASSES                 proof, not a claim
   ↓  DRAFT
```

> **No patch reaches your code without a test that failed before it and passes
> after.**

See it run, in one command — no key, no setup, nothing touched:

```bash
$ python3 -m aegis.demo.remediate

status : DRAFT
detail : reproducer failed before the patch and passes after it
mapped :
  app.py:14  if _active >= POOL_MAXSIZE:
reproducer before patch: exit 1 (non-zero = fails, as required)
reproducer after patch : exit 0 (zero = fixed, proven)
target repo untouched: YES
```

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
| **Retry** | Clears the breaker so a stopped incident can be tried again. |

See [How agentic it actually is](#how-agentic-it-actually-is)
for the four outcomes and why refusing is the right design.

---

## Stage 6 — Learn

**This is the stage every other tool in this category skips**, and it is why
Aegis gets better at your system instead of staying the same.

A survey of the open-source field in October 2026 — Keep (12.4k stars), k8sgpt
(8.2k), HolmesGPT (3.5k, CNCF Sandbox), Robusta (3.1k), SigNoz (32.3k) — found
nothing that asks whether a fix held, nothing that learns from recorded
outcomes, and nothing that reports its own accuracy. The one project with a real
learning loop marks it `Open Source: ⛔️` in its own documentation. That a vendor
chose this specific capability as its commercial moat is the best available
evidence of which part is worth having.

### What gets remembered

Every incident is archived with a **signature** — the services, detectors,
templates, deviation types and cause tokens it was made of:

```json
{
  "services":        ["checkout-api", "inventory-svc"],
  "detectors":       ["NoveltyDetector", "RateSpike"],
  "templates":       ["T-65b79888"],
  "deviation_types": ["purpose_never_ran"],
  "cause_tokens":    ["stock", "reserve", "timeout"]
}
```

A new incident is matched against that archive by Jaccard similarity on each
field, so the second occurrence of a problem arrives with its own history
attached: *what was diagnosed last time, what was done, and whether that
worked.*

### Did the fix actually hold?

The outcome label was the one thing in the whole pipeline that needed a human to
come back later and record — and nobody ever does. So every precedent read
`outcome: not recorded` forever, while similar incidents were handed diagnoses
with a precedent's authority they had never earned.

Marking a fix as working now **keeps the numbers to judge it by**, and every
five minutes those numbers are re-measured:

```
mark a fix worked  →  baselines + template counts frozen at that moment
                   →  5 min later: compare
                   →  recurred? downgrade it, with the count that proves it
```

**Two measures, because one does not generalise.** Latency answers a slow-flow
incident. For an error, a novelty, or a run of hollow checkouts the timing never
moves at all — so the measure that always works is whether the incident's own
templates fired again:

| Verdict | Meaning |
|---|---|
| `held` | none of its templates has fired since the fix |
| `recurred` | `T-65b79888 has fired 14 more time(s) since the fix` |
| `too-early` | fewer than 8 new runs since the fix — not enough to mean anything yet |
| `worse` | the operation got slower after the fix |
| `unchanged` | no material change — nothing proven either way |
| `unknown` | no timings on one side to compare |

Both are arithmetic over counts the store has always kept, so there is nothing
to hallucinate.

**It only ever downgrades.** A verdict of `held` is never promoted to proof: a
problem that has not recurred *yet* is not a problem that is fixed, and
overstating that is the failure this whole project exists to avoid. A fix is
also never credited for silence alone — a template that fired twice in its
entire history and has not fired since proves nothing, so a verdict needs three
prior occurrences before absence means anything.

### It publishes its own hit rate

```
7 of 11 judged incident(s) were diagnosed correctly (64%)
```

That is this project's accuracy on *your* incidents, and it is the number no
tool in this category publishes about itself. Published benchmarks put AI
root-cause accuracy at **3.9–12.5%** while vendors claim 82–90%, which makes a
measured rate the only honest version of the claim — and the only one that moves
as the loop closes.

Incidents with no recorded outcome are counted separately and **never scored as
misses**, so the denominator is always visible:

```json
{
  "incidents": 11, "judged": 11, "unknown": 0,
  "right": 7, "fix_did_not_work": 3, "wrong_diagnosis": 1,
  "auto_verified": 2, "rate": 0.636
}
```

`wrong_diagnosis` is worth as much as `worked`, and is the worse of the two
failures: a fix failing can be a bad patch, but the diagnosis being wrong means
the reasoning was.

### What it learns, and what it does not

Being precise about this, because the category blurs it:

| | |
|---|---|
| **Does** | Archive every incident with a signature, match new ones against it, re-measure fixes, downgrade ones that stopped holding, carry the outcome with the precedent, report its own accuracy |
| **Does** | Mine recurring patterns across the archive — `detector X appears in 4 of 11 archived incidents` — which turns explaining incidents into preventing a category of them |
| **Does not** | Retrain anything. There are no weights here; the loop is arithmetic over recorded outcomes |
| **Does not** | Auto-suppress a signal because you dismissed it. Suppression is a click, and stays one |
| **Does not** | Promote an unverified fix to "proven" on the strength of silence |

### Patterns across incidents

```
detector NoveltyDetector appears in 4 of 11 archived incident(s)
template T-65b79888     appears in 3 of 11 archived incident(s)
```

One incident is a thing to fix. The same signature four times is a thing to
design away — and a report that says so is the difference between firefighting
and engineering.

---

## How agentic it actually is

"Agentic" is usually a claim about autonomy. Here it is a claim about
**verification and refusal**, which is a different and more defensible
thing: the agent decides what to look at, and then has to prove what it
concluded.

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

- Detection is **arithmetic** — statistics cannot hallucinate an alert.
- Every claim is **checked against the logs** before display.
- A patch is capped at **40 changed lines**; a rewritten function is rejected unread.
- Test files, secrets, CI config and dependency manifests are **never patchable**.
- **Read-only until a click.** Aegis writes only under `~/.aegis/`, never into a
  monitored project.
- Two failures and the **circuit breaker** stops it.

---

## Tutorial: from zero to a proven fix

Fifteen minutes, on the log shipped in this repo, so you can follow along with
no system of your own. Every number below is what the current code printed.

### 1. Start it and attach

```bash
python3 run.py
```

Open **http://127.0.0.1:3000**, go to **Sources**, attach
`examples/shipyard.log`. Within seconds:

```
log lines 175 → events 175 → templates 9 → signals 3 → incidents 2
```

175 raw lines became 9 distinct *shapes* of line; three of those were
statistically unusual; those three grouped into two stories. Separately the 175
events were correlated into **26 runs** by their `order_id` — there is no trace
id in this log, which is true of a great many real systems.

**You are now reading two incidents instead of 175 lines.**

### 2. Read the verdicts — all 26 say `unknown`

```
unknown  @09:00:00 · 0s · 7 events
unknown  @09:01:20 · 0s · 7 events
```

This is the honest first-run state and it is worth sitting with. Aegis can see
exactly what each order *did*. It will not tell you whether that was correct,
because **nobody has told it what an order is for.** A tool willing to guess
here is a tool willing to be confidently wrong.

Two runs do read `failed` — the declined payments. Those are the ones every tool
catches.

### 3. Say what a run is for

Click **Mark purpose**. Aegis shows the steps it mined from your own runs, each
with the fraction of runs containing it:

```
1.00  checkout-api   order.received
1.00  checkout-api   cart.validated
1.00  search-svc     catalogue.lookup
0.92  payment-svc    payment.authorised
0.81  inventory-svc  stock.reserved        ← mark this one
0.88  notify-svc     email.queued
0.88  checkout-api   order.completed
```

Nobody wrote that list; it was derived from the log. Mark `stock.reserved` — the
step a checkout exists to produce.

Notice `0.81`. Four in five orders reserve stock; one in five does not. **The
presence fraction is already pointing at the bug before any verdict is
computed.**

Why a human marks this and not the model: a step appearing in every run is
marked required by frequency — but a checkout that only validates has done
nothing. The step that makes a run worth having usually appears *only in the
runs that worked*, which is exactly why frequency cannot find it.

### 4. Read them again

```
achieved  20
hollow     3     ← paid, confirmed, nothing reserved
failed     2     ← declined payments
degraded   1     ← 6s against a 1s p95, still HTTP 200
```

Open a `hollow` card:

```
hollow   ORD-88409
  completed cleanly - no errors, normal teardown - but none of its
  purpose steps ever ran

  What a run of this kind must do
    ✗ inventory-svc  stock.reserved   - never ran

  What this run actually did · 6 steps
    09:08:28  order.received      order_id=ORD-88409
    09:08:28  payment.authorised  amount=17490 in 233ms
    09:08:28  email.queued        template=order_confirmed
    09:08:29  order.completed     status=200 in 495ms
```

The claim and its evidence, together. Three orders took money and shipped
nothing, and nothing else in your stack was going to say so.

### 5. Explain, then investigate

**Explain** — a diagnosis with the evidence attached, checked against the logs
before you see it.

**Investigate · runs tools** — now the model drives, choosing from six tools
over up to six steps, and recording what it ruled out:

```
ruled out: a downstream outage - get_dependencies reported no
           observed dependency failures in the window
```

### 6. Propose a fix, and let it grade itself

This step needs a repository, so it applies when watching your own service.
Point **Repository path** at the checkout that produced the logs — Aegis
pre-fills it from the traceback — and click **Propose fix**. See
[Stage 5](#stage-5--fix) for what happens and how often it refuses.

When you mark a fix as working, the numbers at that moment are kept, and from
then on [Stage 6](#stage-6--learn) re-measures it every five minutes without
being asked.

### 7. Point it at your own system

```bash
python3 run.py        # then attach /var/log/yourapp/app.log
                      # or watch a port, or add a connector
```

Then repeat steps 2 and 3: read the mined steps, mark the one your runs exist to
produce. That is the whole setup — no parser to write, no schema to declare, no
instrumentation to add.

---

## Every feature, and what it is for

The stages above explain the mechanism. This is the whole surface in one place.

### Watching

| Feature | What it is for |
|---|---|
| **File watching** | Tails a log file. Rotation-safe by inode, backfill-capped, long lines truncated. |
| **Port watching** | Finds a running service's real log from its process, preferring the app's own logger over redirected stdout. |
| **SigNoz connector** | Reads logs and traces over the v5 query API. Your data stays where it is. |
| **MCP connector** | Reads from anything exposing an MCP server. |
| **Multi-service** | One connector holding 12 services polls each fairly, so a quiet service is not drowned out by a busy one. |

### Understanding, without being configured

| Feature | What it is for |
|---|---|
| **Template mining** | Discovers line shapes. No parser to write, no schema to declare. |
| **Baselines** | p50/p95 per operation, from durations already in your logs. |
| **Flow mining** | Derives what a run normally does, from your real runs. |
| **Purpose marking** | You confirm which step is the run's reason to exist. One click, and every run after it is judged. |
| **Code analysis** | Maps log lines to the source lines that wrote them, by parsing your repo's AST. |
| **Dependency map** | Which external services this code calls, and whether those calls are guarded. |

### Finding what matters

| Feature | What it is for |
|---|---|
| **Seven detectors** | Novelty, rate spike, rate drop, silence, latency shift, error ratio, cost anomaly. |
| **Incident grouping** | Twenty alerts about one outage become one story with a ranked cause. |
| **Run verdicts** | Including `hollow` — completed cleanly, achieved nothing. |
| **Precedent memory** | "This happened before, and here is what fixed it." |
| **Live brief** | Plain English on what is happening now — [a switch](#the-live-brief-is-a-switch), on by default. |
| **Ask about this run** | A question answered from your own log lines, cited, or refused. |

### Explaining

| Feature | What it is for |
|---|---|
| **Explain** | A diagnosis with its evidence, grounded against the logs before you see it. |
| **Investigate** | Chooses its own tools over up to six steps, recording what it ruled out. |
| **Gap reports** | Which missing log line stopped the analysis, named by file and line. |
| **Simulation** | What would break if this component failed. |
| **Audit report** | A downloadable record of what was found and what was claimed. |

### Fixing, with proof

| Feature | What it is for |
|---|---|
| **Propose fix** | The reproducer must fail, then pass. No proof, no proposal. |
| **Scope gate** | ≤40 changed lines; never a test file, a secret, CI config or a dependency manifest. |
| **Patch coherence** | A diff whose hunks would corrupt the file is rejected before it is applied. |
| **Syntax check** | Every patched file must still parse, or the patch is named as the cause. |
| **Relevant tests only** | Runs the tests that touch the changed code — 38 of 136, 1.6s, no API cost. |
| **Clean-tree rule** | Refuses to apply while you have uncommitted changes, so its work stays separable from yours. |
| **Revert** | Every applied change is undoable. Nothing is ever committed. |
| **Circuit breaker** | Two failed attempts and it stops, and says a person is needed. |
| **Retry** | Clears the breaker so a stopped incident can be tried again. |

### Learning

| Feature | What it is for |
|---|---|
| **Fix verification** | Measures whether a fix held — latency, and whether the problem itself recurred. |
| **Automatic grading** | Every five minutes, a fix whose problem came back is downgraded, with the count that proves it. |
| **Accuracy report** | How often this project's own diagnoses turned out right, on your incidents. |
| **Pattern mining** | The same signature four times is a thing to design away, not to fix again. |

### Interfaces

| Feature | What it is for |
|---|---|
| **Web UI** | Now / Runs / Incidents / Audit. |
| **HTTP API** | Everything the UI does, as JSON. |
| **MCP server** | Aegis as a tool for your own agent. |
| **CLI** | `log-agent`, `log-agent-mcp`. |
| **/healthz** | Liveness and the running version. |

---

## Compared to other tools

| | Logging tools (Datadog, SigNoz, ELK) | An LLM on logs | **Aegis** |
|---|---|---|---|
| Stores and searches logs | ✅ | ❌ | Doesn't try to |
| Says what happened | ✅ | ✅ | ✅ |
| **Says whether it worked** | ❌ | Guesses | ✅ verdict per run |
| **Catches "200 OK but wrong"** | ❌ | ❌ | ✅ |
| **Proves a fix before proposing it** | ❌ | ❌ | ✅ the test must flip |
| **Says what you failed to log** | ❌ | ❌ | ✅ named file and line |
| **Checks whether the fix held** | ❌ | ❌ | ✅ measured, every 5 min |
| **Learns from recorded outcomes** | ❌ | ❌ | ✅ |
| **Reports its own accuracy** | ❌ | ❌ | ✅ |
| Needs instrumentation work | ✅ usually | ❌ | ❌ reads what you already write |
| Refuses when unsure | — | Rarely | ✅ 15 refusal points |
| Cost to run | $$$ per GB | $$ per query | Free tier by default |

**Aegis is not a competitor to SigNoz.** It *reads from* SigNoz — that is a
supported source, verified against a live instance. Storage and search are
solved, expensive, and commoditising. Aegis computes the thing nobody computes.

SigNoz's own documentation names the "200 OK but wrong" problem and does not
solve it. They ingest `gen_ai.evaluation.score.value` and never compute it.

### What it adds to the tool you already run

You already pay to collect, store and search these logs. That part is solved.
What none of it does is tell you whether the run actually worked — so Aegis
reads **the same data you are already paying for** and computes the layer above
it.

Concretely, on the order from the top of this page:

| Your current tool shows you | Aegis adds |
|---|---|
| `checkout-api 200 in 495ms` — green | **this order achieved nothing** — payment taken, nothing reserved |
| a spike in `payment.declined` | **one incident**, cause ranked, with the three signals that belong to it |
| the error, and the stack | **the line that raised it**, and a patch with a test that failed before and passes after |
| that you closed the ticket | **whether the fix held** — re-measured, and downgraded if the problem returns |
| nothing, when a line was never written | **which log line to add**, named by file and line |

Per tool:

**SigNoz** — add a connector with the base URL and an API key. Aegis then reads
your existing logs over `POST /api/v5/query_range` and every verdict, incident
and gap report above applies to telemetry already in SigNoz. Their own
documentation names the "200 OK but wrong" problem and does not solve it; this
is that gap filled, on their data, without moving any of it.

**Datadog, Elastic, Loki, CloudWatch** — no native connector yet, and you do not
need one: point Aegis at the log file or the port your service already writes to.
**That route requires nothing from your vendor** — no agent, no schema, no
instrumentation, no export. Or write a `TelemetryProvider`: two methods,
`query_logs` and `capabilities`.

**Anything exposing an MCP server** — add it as a connector directly.

**What it will not do to your setup.** It does not forward, store or re-index
your logs, so it adds nothing to your ingest bill. It reads a window, computes
over it, and keeps only what it derived — templates, baselines, verdicts,
incidents — in one SQLite file per project under `~/.aegis/`. Your logs never
move. Connector polling is capped at 30 calls a minute behind a 30-second cache,
so it cannot run up a bill or trip a rate limit on a backend you share.

### On the three rows above

Those are the rows worth checking, because they are the ones the category
leaves empty. A survey of the open-source field in October 2026 — Keep (12.4k
stars), k8sgpt (8.2k), HolmesGPT (3.5k, CNCF Sandbox), Robusta (3.1k), SigNoz
(32.3k) — found:

- **Nothing asks whether the fix worked.** The nearest is Robusta marking an
  alert resolved when it clears. Commercial tools have the *word* — "Monitoring"
  in incident.io, "Mitigated" in FireHydrant — as a human status field, not a
  measurement.
- **Nothing in open source learns from outcomes.** Keep is the one project with
  a real retraining loop, and its own documentation marks that feature
  `Keep Open Source: ⛔️` — Cloud and Enterprise only. That a vendor chose this
  specific capability as its commercial moat is the best available evidence of
  which part is worth having.
- **Nothing measures its own accuracy on your incidents.** One project treats
  evaluation seriously and does it *offline*, against scripted failure
  scenarios — which answers "is this agent any good in general?", not "did we
  get better at our incidents?"

Retrieval of past incidents is not the same as learning from them, and several
projects blur the two. Aegis records an outcome, grades it against what the logs
did afterwards, and lets that outcome travel with the precedent.

### What this deliberately does not build

- **Log storage, search, dashboards, alerting** — solved, expensive, losing ground.
- **Autonomous root-cause analysis** — 3.9–12.5% measured accuracy; the
  category's central false promise.
- **NL→query, log clustering, MCP servers** — commodity; every vendor ships them.
- **A generic AI SRE** — a three-person team replicated most of one in 3.5 weeks.

---

---

## Architecture and concepts

Each layer has one job and hands a defined contract to the next. If a layer's
output is not an order of magnitude smaller than its input, that layer is broken.

| Layer | Name | LOC | Job |
|---|---|---|---|
| **L1** | Ingestion | 1,677 | Read from a file, a port, or a connector. Read-only. |
| **L2** | Normalization | 889 | One record per event. Redact, fingerprint, correlate. |
| **L3** | Storage | 409 | One SQLite file per project. No shared tables. |
| **L4** | Understanding | 2,138 | Mine templates, baselines, flow specs. Read the repo. |
| **L5** | Detection | 570 | Seven statistical detectors. |
| **L6** | Correlation | 685 | Group signals into incidents, rank the cause. |
| **L7** | Reasoning | 782 | Explanation and investigation, governed and audited. |
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

**Seven detectors, all statistics** — so detection cannot hallucinate
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

The only layer that reasons rather than measures, under three constraints:

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
[Stage 5](#stage-5--fix).

---
### Core concepts

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
[Stage 3](#stage-3--judge).

### Correlation basis

Whether a run's membership is `extracted` (the line stated its id — evidence) or
`inferred` (exactly one session was open — an assumption). Downstream layers
know which one they are holding.

---

---

## Design decisions worth defending

These come up in technical review.

**Detection is statistical.** Statistics cannot hallucinate an alert. Reasoning
is allowed in only to explain what detection found, with the evidence attached,
and its output is checked against the logs before being shown.

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

---

## Honest limits

Stated plainly, because a tool whose pitch is calibrated honesty cannot have a
dishonest limitations section.

**It fixes bugs that leave a traceback.** A wrong *value* with no error gives
nothing to map to code.

**The chatbot answers about the run in front of you, not the archive.** The
window is the latest run's last 60 lines. Incidents and verdicts are kept and
matched across the whole history, but you cannot yet ask a question of
last Tuesday — the retrieval layer for that is not built.

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
a live database and live external calls, the generated test needs them too, and
may fail for environmental reasons rather than logical ones.


**One connector is verified live.** SigNoz is tested against a real instance.
The MCP connector is implemented but not verified against every server.

**Inference is capped, not solved.** A service with genuinely concurrent
unkeyed requests will leave lines unattributed rather than guess. That is the
right trade, but it means fewer runs than a tool willing to guess would claim.

**Single-node, local-first.** No clustering, no HA, no multi-tenant auth. It is
a tool you run, not a service you operate.

---

---

### Known open issues

Found by running it, not by tests. Kept here rather than in a separate file
because a limitations section that omits the ones you already know about is
worth nothing.

| | Issue |
|---|---|
| **HIGH** | The incident card explains the *mechanism* — "earliest onset of 2 members" — where it should explain the consequence. That is how Aegis thinks, not what happened. |
| **HIGH** | On a large log the generated brief has produced a confident sentence at 0% confidence while the detectors below it were working correctly. The detectors are right; the summariser is the weak part, which is one reason it is [a switch](#the-live-brief-is-a-switch). |
| **MED** | Simulation reasons only from observed runtime dependencies, and ignores the project brief and the code graph even though both exist. |
| **MED** | A fresh start is not discoverable. Three things persist across restarts and "Clear run" clears none of them. |
| **LOW** | Port suggestions still include stray processes — a browser helper once appeared as a project. Harmless, but it costs trust in a list whose whole job is to be trustworthy. |

---

## Reference

All configuration is environment variables, read from `.env` or the environment.
Every one has a working default.

| Variable | Default | What it does |
|---|---|---|
| `GROQ_API_KEY` | — | Free-tier model key; used for everything by default |
| `OPENAI_API_KEY` | — | Optional; touched only on explicit request |
| `LOG_AGENT_LLM` | `true` | `false` forces pattern mode even with a key |
| `LOG_AGENT_DEV_MODE` | `true` | `false` starts with the live brief off — see [the live brief](#the-live-brief-is-a-switch) |
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
| `~/.aegis/audit.jsonl` | Every reasoning call, with its purpose |

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

379 tests, 30 suites, no dependencies, no network, no API keys required.

---

---

## Contributing

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
