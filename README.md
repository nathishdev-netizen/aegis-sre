# Aegis

**Every observability tool tells you what happened. None of them tell you
whether it worked.**

Aegis reads the logs you already produce, reads the code that wrote them, and
closes the loop from *"something is wrong"* to *"here is a fix, and here is the
test that proves it"* — then keeps measuring whether that fix actually held.

| | What it does |
|---|---|
| **Judges every run** | Including the verdict nobody else computes: *finished cleanly, achieved nothing*. |
| **Reads your codebase** | Maps log lines to source lines, finds unguarded calls, names the log line you failed to write. |
| **Proves a fix first** | A failing test, a patch, the same test passing. 15 of its 16 outcomes are refusals. |
| **Grades its own work** | Re-measures every fix, downgrades the ones that stopped holding, reports how often it was right. |

```
175 log lines → 175 events → 9 templates → 3 signals → 2 incidents
                   ↳ grouped into 26 runs
                     20 achieved · 3 HOLLOW · 2 failed · 1 degraded
```

Storage and search are solved problems. Deciding which five of a thousand lines
matter, and whether the run they describe actually did its job, is not.

- **Python 3.10+**, **zero dependencies** — the standard library only, by design
- **378 tests**, 30 suites, no network and no API key needed to run them
- **MIT licensed**

## Install and run

```bash
git clone <this-repo> aegis && cd aegis
python3 run.py
```

Open **http://127.0.0.1:3000**. That is the whole install — no build step, no
database to provision, no account.

Four of the nine layers are pure arithmetic and need no model at all. Only
explanation and fix-proposal call one, only when you click, and the free tier is
the default:

```bash
cp .env.example .env     # GROQ_API_KEY (free tier) is enough
```

## The thing it does that others do not

One order from [`examples/shipyard.log`](examples/shipyard.log). Every tool on
the market renders it green:

```
09:08:28.605 INFO  checkout-api   order.received order_id=ORD-88409 cart_items=4
09:08:28.625 INFO  checkout-api   cart.validated order_id=ORD-88409 subtotal=12790
09:08:28.695 INFO  payment-svc    payment.authorised order_id=ORD-88409 amount=17490
09:08:28.995 INFO  notify-svc     email.queued order_id=ORD-88409 template=order_confirmed
09:08:29.015 INFO  checkout-api   order.completed order_id=ORD-88409 status=200 in 495ms
```

Zero errors. HTTP 200. 495ms. Payment taken, confirmation emailed.

**`inventory-svc` never ran.** No stock was reserved, no warehouse knows to ship
anything, and the customer has an email saying their order is confirmed.

Aegis calls that run `hollow`: completed cleanly, achieved nothing. It is the
failure no error can show you — and in that log it happens three times in 26
orders, while an error-based tool reports only the two declined payments.

| Verdict | Meaning |
|---|---|
| `achieved` | did what this kind of run exists to do |
| `failed` | errored, and said so |
| `hollow` | completed cleanly, achieved nothing |
| `degraded` | achieved it, but abnormally |
| `unknown` | nobody has said what this kind of run is for |

## It grades its own work

Every tool in this category stops at the fix. Aegis keeps measuring:

```
mark a fix as working  →  the numbers at that moment are kept
                       →  every 5 min: did the problem come back?
                       →  it did: downgraded, with the count that proves it
```

```
7 of 11 judged incident(s) were diagnosed correctly (64%)
```

A survey of the open-source field (October 2026) found nothing that asks whether
a fix held, nothing that learns from recorded outcomes, and nothing that reports
its own accuracy on your incidents. The one project with a real learning loop
marks it `Open Source: ⛔️` in its own docs. Details and sources in
[docs/AEGIS.md](docs/AEGIS.md#on-the-three-rows-above).

## The claim worth checking

> **No patch reaches your code without a test that failed before it and passes
> after.**

Not a model saying "I fixed it" — a measurement. **15 of the 16 outcomes in the
fix protocol are refusals.** Independent benchmarks put AI root-cause accuracy
at 3.9–12.5% where vendors claim 82–90%, so a tool that always has an answer is
a liability.

If someone asks "will it fix any bug?", the honest answer is the strong one:
*no — it fixes bugs it can prove it fixed, and says so when it can't.*

## Documentation

**→ [docs/AEGIS.md](docs/AEGIS.md)** — the complete document: concepts, the nine
layers, every feature, a step-by-step tutorial, how it compares to other tools,
and its honest limits.

- [FINDINGS.md](FINDINGS.md) — open issues found by real use, not by tests
- [docs/design-history/](docs/design-history/) — the original spec and per-phase
  build logs, kept as a record rather than as current docs

## Verify it yourself

```bash
for t in tests/test_*.py; do python3 "$t"; done        # 378 tests, no network
python3 -m aegis.demo.conformance examples/shipyard.log # the verdicts above
python3 -m aegis.demo.remediate                        # the fix loop, sandboxed
```

## The promises, enforced rather than stated

- Watched logs are opened **read-only** (sha256-verified in the demos)
- Aegis writes only under `~/.aegis/` — never into a monitored project
- Each project is a **separate database file**; no shared tables
- Redaction happens at ingest; the model only ever sees redacted text
- Model spend is budget-capped and audited to `~/.aegis/audit.jsonl`
- Detection never calls a model; explanation never detects

## Licence

MIT. See [LICENSE](LICENSE).
