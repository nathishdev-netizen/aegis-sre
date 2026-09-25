---
name: log-instrumentation
description: Add execution logging to a project so a log-intelligence agent can reconstruct what the app does at runtime - what ran, what failed, why, and what was skipped. Use when asked to instrument a project for log analysis, make logs machine-readable, prepare a codebase for a log agent, or improve logging so failures are diagnosable. Works with any language or framework. Only ever ADDS logging; never changes behavior.
---

# Log Instrumentation

You are instrumenting someone else's project so a log-intelligence agent can read its
execution at runtime and explain what happened.

Your job is to work out **where this specific project needs logging**, then propose it.
Not to apply a checklist - to read the code, find the real execution path, and see what
a person debugging a 2am failure would wish had been logged.

---

## The one hard rule

**You may only add logging. Nothing else.**

The project's behavior after your change must be byte-for-byte identical, minus the new
log output. Someone reviewing your diff should never have to ask "does this still work?"

Specifically forbidden:

- Changing what any function returns, raises, or yields
- Adding, removing, moving or merging `try`/`except`/`catch` blocks - log *inside* the
  handler that already exists; if there is no handler, that is a finding to report, not
  a thing to fix
- Reordering operations, or moving code between branches
- Changing existing log lines - other tools, dashboards or alerts may already parse them.
  Add new lines alongside them
- Adding dependencies. Use the logger the project already uses. If it has none, use the
  language's standard library and say so in your report
- Touching config, CI, build files, or anything outside the execution path
- "While I was here" fixes. You are not here for those. Note them separately if they matter

If instrumenting something properly would require a behavioral change, **do not make it**.
Report it as a finding: "`process_batch` swallows exceptions at line 88 - no handler to
log from. Consider adding one."

A logging call must also never be able to break the app:

- No expression that can raise inside a log call - no `obj.field.subfield`, no `[0]`,
  no division, no `json.dumps` of something that may not serialize
- Never log inside a tight loop that runs thousands of times per request
- Never `await` or block inside a log call
- Never log secrets: tokens, passwords, API keys, full auth headers, PII. Log that a
  token was present, never its value

---

## What the log agent needs

It reconstructs runs from lines. Each line should carry four things:

```
<timestamp> <LEVEL> [<component>] <message>
```

| Part | Why it matters |
|---|---|
| **timestamp** | Orders the run and measures durations. Any standard format - `10:00:01`, ISO 8601, or `2026-08-28 16:00:18.807`. Most loggers emit one already |
| **LEVEL** | `INFO`, `WARN`, `ERROR`, `DEBUG`. Drives failure detection |
| **[component]** | **The most important part.** A short stable tag naming the subsystem: `[api]`, `[db]`, `[embedder]`, `[payments]`. The agent builds its flow diagram from these, so they must be consistent - the same subsystem always uses the same tag |
| **message** | Human prose. Written for a person, not for a regex |

If the project already emits timestamp and level (nearly all loggers do), your work is
mostly **adding the `[component]` tag and the missing lines**.

Structured/JSON logs work too - the agent unwraps `{"line": "..."}` and
`{"message": "...", "level": "..."}`. If the project logs JSON, keep logging JSON and put
the component in a field.

---

## Step 1 - Understand the project before writing anything

Do not start editing. Build a picture first.

1. **Find the entry points.** HTTP handlers, CLI commands, queue consumers, cron jobs,
   scheduled tasks. These are where runs begin.
2. **Trace one run end to end.** Pick the most important entry point and follow it through
   every module it touches until it returns. Write down that path - it *is* the pipeline
   the agent will visualize.
3. **Identify the components.** Usually they map to modules, packages, or service
   boundaries. Prefer names already in the codebase over names you invent - if the
   directory is `tools/vector_tools.py`, the tag is `[vector_tools]`, not `[embeddings]`.
4. **Find the existing logger.** What is it, how is it configured, what format does it
   emit, is there a request-id or correlation-id already threaded through?
5. **Find what is currently invisible.** This is the real work - see Step 2.

Report this picture to the human before proposing changes. If you have misread the
architecture, that is the cheapest possible moment to find out.

---

## Step 2 - Decide where logging is needed

Most projects log the happy path adequately and go dark exactly where debugging starts.
Look for these, in priority order:

**1. Run boundaries** - the single highest-value addition.
Without a clear start and end, the agent cannot tell one request from the next; they blur
into one stream and metrics become meaningless. Every entry point needs one line in and
one line out, and the exit line should carry the outcome and the duration.

**2. Stage transitions.**
When control moves between components: entering retrieval, calling the model, opening the
database. One line per boundary. This is what draws the flow diagram.

**3. Failures with their target.**
Existing handlers usually log *that* something failed, rarely *what it was doing*.
`ERROR db failed` is nearly useless. `ERROR [db] Connection refused: ws://127.0.0.1:8000`
names the target and is immediately diagnosable.

**4. The branch not taken.**
The single most under-logged thing in real code, and the thing users most often ask about.
A skipped step is invisible unless you log it. Always include the reason:

```
WARN [retrieval] Skipping rerank - no candidates returned
INFO [cache] Cache hit, skipping upstream fetch
WARN [notify] Skipping email - user has notifications disabled
```

**5. Retries and fallbacks.**
Log the attempt number and why the retry happened. Log when a fallback path is taken and
what it fell back *from*. Otherwise a degraded success looks identical to a clean one.

**6. Slow or external calls.**
Anything crossing a network boundary: log the call, its target, and its duration on return.
Durations are how the agent finds bottlenecks.

**Do not** log every function entry and exit. Volume is not the goal - a log that narrates
one clear story per run is far more useful than one that dumps everything. If you cannot
say what question a line would answer, do not add it.

---

## Step 3 - Write the lines

**Include identifiers.** URLs, ports, model names, record counts, durations, status codes.
These turn a vague summary into a specific diagnosis. `Calling embedder` is weak;
`Calling embedder bge-m3 at http://localhost:8080` is diagnosable.

**Write prose, not tokens.** `state=3 rc=-1` means nothing to a reader or a model.
`Connection refused after 3 attempts` means something to both.

**Match the project's existing voice.** If it logs lowercase without punctuation, do the
same. Consistency matters more than your preference.

**Keep the component tag stable.** `[db]` in one file and `[database]` in another produces
two components in the diagram where there should be one.

**Correlation ids**: if the project already threads a request id, include it - it lets the
agent separate interleaved concurrent runs. If it does not, do not add one; that is
plumbing, and plumbing is a behavior change.

Shape of the result, in two languages, to show the pattern rather than prescribe a stack:

```python
# Python - using whatever logger the project already has
logger.info("[api] Request received %s %s id=%s", method, path, request_id)
try:
    result = embedder.embed(query)          # unchanged
    logger.info("[embedder] Embedded query in %dms model=%s", elapsed_ms, model_name)
except TimeoutError as exc:                 # existing handler, unchanged
    logger.error("[embedder] Timeout after %dms calling %s: %s", elapsed_ms, url, exc)
    raise                                   # unchanged
logger.info("[api] Response sent status=%d in %dms", status, total_ms)
```

```javascript
// Node - same shape, project's existing logger
log.info(`[api] Request received ${req.method} ${req.path} id=${reqId}`);
if (!candidates.length) {
  log.warn(`[rerank] Skipping rerank - no candidates returned`);
}
log.info(`[api] Response sent status=${res.statusCode} in ${Date.now() - t0}ms`);
```

Note what the examples do *not* do: no new try/except, no changed return, no reordering.
The logging sits alongside code that is otherwise untouched.

---

## Step 4 - Propose, then apply

**Show a diff and explain the reasoning before applying anything.** For each change, one
line on what question it answers:

> `services/chat/api.py:118` - add run-start line.
> Without it the agent cannot separate one request from the next.

> `services/chat/retrieval.py:88` - log the skipped rerank and why.
> Currently invisible; a user asking "did reranking run?" gets no answer.

Then let the human approve. Apply only what they accept.

**Verify after applying:**

1. Run the project's test suite. It must pass exactly as before - any change in behavior
   is a bug in your instrumentation, not in the tests
2. Exercise one real run and read the output. Does it tell a coherent story? Could someone
   who has never seen the code follow what happened?
3. Confirm no secrets appear in the output

**Report at the end:**

- The pipeline you found, as a component sequence
- What you added and where
- What you deliberately did not add, and why
- Findings you could not fix without changing behavior (swallowed exceptions, missing
  error handlers, places where a failure is genuinely silent)

That last list is often the most valuable thing you produce.

---

## Optional - expose the logs for live reading

Only if the human asks. A log agent can read a file directly, so this is not required.

If they do want live streaming, the lowest-risk approach is a read-only endpoint serving
recent lines - `/logs` as plain text, or `/events` as SSE - backed by an in-memory ring
buffer that the logger also writes to. Add it as a separate module and mount it; do not
weave it into existing request handling.

If the project writes to a log file already, prefer that. A file needs no new code at all.

---

## Anti-patterns

| Do not | Instead |
|---|---|
| Log inside hot loops | Log once before, once after, with a count |
| `logger.info("here")` / `"step 2"` | Say what is happening and to what |
| Add a try/except so you can log an error | Report the missing handler as a finding |
| Rewrite existing log lines to fit the format | Add new lines; leave existing ones alone |
| Log full request/response bodies | Log size, type, and identifiers |
| Invent component names | Use the project's own module names |
| Log every function entry | Log stage boundaries only |
| Apply changes without showing them | Diff first, explain, then apply |
