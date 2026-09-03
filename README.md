# Log Intelligence Agent

Local AI agent that watches a running app and turns its execution logs into a live
explanation of what is happening, what failed, and why.

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env          # add your OPENAI_API_KEY
python3 run.py
```

Open http://127.0.0.1:3000

The startup banner tells you which mode you are in:

```
Log Intelligence Agent running at http://127.0.0.1:3000
  interpretation: llm - Interpreting with gpt-4o-mini.
  auto-attach:    on
```

## Testing it works

`demo/log_generator.py` emits realistic logs so you can verify the whole path -
attach, parse, stage tracking, explanation, Q&A.

```bash
# 22 lines of a pipeline that fails on the embedding service
python3 demo/log_generator.py --scenario failure --count 22 --port 5060

# then attach the agent (or pick the port in the UI)
curl -XPOST localhost:3000/api/trace-port \
  -H 'content-type: application/json' -d '{"port":5060}'
```

Scenarios: `success`, `failure`, `db`, `ratelimit`, `traceback`, `messy`, `mixed`.

`messy` is the stress test - JSON-wrapped lines, tracebacks, ANSI colour, blank
lines, lines with no level or timestamp.

```bash
python3 demo/log_generator.py --scenario mixed --count 0        # loop forever
python3 demo/log_generator.py --count 100 --file /tmp/test.log  # test file attach
python3 demo/log_generator.py --help
```

There is also a fixed hotel-booking demo: `python3 demo/hotel_pipeline.py` (port 5055).

### What good looks like

Attached to `--scenario failure`, the agent should report something like:

```
status : failed        interpretation: llm    confidence: 80
summary: ...encountered issues with the embedding service, leading to timeouts.
         As a result, the pipeline has aborted due to a required stage failing.
causes : Connection timeout after 5000ms calling embedding service
fixes  : Investigate the embedding service for performance issues or downtime
```

And answer questions about that run:

| Question | Expected |
|---|---|
| Did the embedding service fail? | **yes** - cites the timeout line |
| Did the graph traversal run? | **no** - skipped, no embeddings available |
| Did the database fail? | **unknown** - nothing in the logs says so |

That last one matters: the agent says "unknown" rather than guessing.

Multi-line tracebacks fold into the entry they belong to, so one exception is one
event and the full trace travels with it as evidence. Try `--scenario traceback` and
ask *"which file and line raised the exception?"*.


## Tests

```bash
python3 tests/test_regressions.py     # or: python3 -m pytest tests/ -v
```

Every test corresponds to a bug found by running the agent against a live source -
attaching to itself, a stalled client freezing the server, answers that contradicted
their own evidence.

## Interpretation modes

**LLM mode** (with `OPENAI_API_KEY`): summary, causes, fixes and answers come from the
model, grounded in the observed timeline. An answer citing no evidence is downgraded
to `unknown` rather than asserted.

**Pattern mode** (no key): reports which regex patterns matched, shows no confidence
score, and answers `unknown`. Keyword search cannot tell you whether something
*failed* - only whether a word appears - so it does not pretend otherwise.

The UI badge shows which mode is live.

## Structure

```
app/
  config.py         settings + .env loading (all tunables live here)
  server.py         HTTP + SSE server
  core/
    discovery.py    find listening TCP ports
    sources.py      which ports are plausible sources, endpoint probing
    parser.py       log line -> timestamp/level/message, stage inference
    state.py        runtime state engine, ingestion, graph
    llm.py          model-backed interpretation and Q&A
  web/index.html    dashboard UI
demo/
  log_generator.py  configurable test log source
  hotel_pipeline.py fixed booking-pipeline demo
tests/
  test_regressions.py
```

## Configuration

All settings live in `.env` (see `.env.example`). Real environment variables take
precedence, so you can override for one run:

```bash
LOG_AGENT_LLM=false python3 run.py     # force pattern mode
LOG_AGENT_PORT=4000 python3 run.py
```

| Variable | Default | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | - | Enables LLM interpretation |
| `LOG_AGENT_MODEL` | `gpt-4o-mini` | Model for summary and Q&A |
| `LOG_AGENT_LLM` | `true` | Set false to force pattern mode |
| `LOG_AGENT_HOST` / `LOG_AGENT_PORT` | `127.0.0.1` / `3000` | Server bind |
| `LOG_AGENT_AUTO_ATTACH` | `true` | Auto-attach to the likeliest source |
| `LOG_AGENT_PROBE_PATHS` | `/events,/stream,...` | Endpoints tried when attaching |
| `LOG_AGENT_MAX_LOG_LINES` | `200` | Lines retained in memory |
| `LOG_AGENT_LLM_LOG_WINDOW` | `60` | Lines sent to the model |

## Known limitations

- **Attachment is still heuristic** - it probes common endpoints. A normal app on a
  port is not necessarily a readable log source. A declared manifest per project is
  the intended fix.
