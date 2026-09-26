"""LLM-backed interpretation of a run.

The rest of the app derives *facts* (stages, transitions, timeline). This module is
the only place that produces *judgements* - what the run means, why it failed, and
whether the evidence supports an answer.

Design rule: when no model is configured, every function here returns None. Callers
fall back to describing what was pattern-matched, and label it as such. Nothing in
this file ever invents a cause, and no output is presented as reasoning unless a
model actually did the reasoning.
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.config import settings


# Why the last model call failed, so the UI can be specific rather than silent.
_LAST_ERROR: str | None = None


def _client() -> Any | None:
    """Return an OpenAI client, or None when the SDK or API key is unavailable."""
    if not settings.llm_available:
        return None
    try:
        from openai import OpenAI
    except ImportError:
        return None
    try:
        return OpenAI(api_key=settings.openai_api_key)
    except Exception:
        return None


def is_available() -> bool:
    return _client() is not None


def status() -> dict[str, Any]:
    """Describe the interpretation backend so the UI can be honest about it."""
    if not settings.llm_enabled:
        return {
            "mode": "patterns",
            "available": False,
            "detail": "LLM disabled (LOG_AGENT_LLM=false) - showing pattern matches only.",
        }
    if not settings.openai_api_key:
        return {
            "mode": "patterns",
            "available": False,
            "detail": "No OPENAI_API_KEY set - showing pattern matches only, not reasoning. Add it to .env",
        }
    try:
        import openai  # noqa: F401
    except ImportError:
        return {
            "mode": "patterns",
            "available": False,
            "detail": "openai package not installed (pip install -r requirements.txt).",
        }
    if _LAST_ERROR:
        return {"mode": "patterns", "available": False, "detail": _LAST_ERROR}
    return {"mode": "llm", "available": True, "detail": f"Interpreting with {settings.model}."}


RUN_START_RE = re.compile(
    r"\b(request received|incoming request|received request|call start|"
    r"job start(?:ed|ing)?|task start(?:ed|ing)?|run start(?:ed|ing)?|"
    r"starting (?:run|job|task|request)|invocation start)\b",
    re.I,
)


def _latest_run(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Trim to the most recent run, if the logs mark where one begins.

    Telling the model to "describe the latest run" is not enough when it is handed
    200 lines spanning five calls - it described an old failure while the newest call
    had succeeded. Cutting the context is what actually scopes the answer.
    """
    for index in range(len(logs) - 1, -1, -1):
        if RUN_START_RE.search(str(logs[index].get("message", ""))):
            return logs[index:]
    return logs


def _run_context(snapshot: dict[str, Any]) -> str:
    """Compact, factual view of the run. Only observed data - no derived guesses."""
    timeline = snapshot.get("timeline", [])[-settings.llm_timeline_window:]
    logs = _latest_run(snapshot.get("log_lines", []))[-settings.llm_log_window:]
    stages = [s for s in snapshot.get("stages", []) if s.get("seen")]

    lines = [
        f"Source: {snapshot.get('source', {}).get('label', 'unknown')}",
        f"Events observed: {snapshot.get('metrics', {}).get('total_events', 0)}",
        f"Stages reached: {', '.join(s['label'] + '=' + s['status'] for s in stages) or 'none'}",
        "",
        "TIMELINE (oldest to newest):",
    ]
    for item in timeline:
        lines.append(f"  [{item.get('time','')}] {item.get('status','')}: {item.get('message','')}")
    lines.append("")
    lines.append("RAW LOG LINES (oldest to newest):")
    for item in logs:
        lines.append(f"  {item.get('level','')} {item.get('message','')}")
        # Stack traces are folded into their parent entry; include them so the model
        # can name the failing frame instead of guessing from the summary line.
        for detail in (item.get("detail") or [])[:20]:
            lines.append(f"      {detail}")
    return "\n".join(lines)


def _chat(messages: list[dict[str, str]], *, max_tokens: int = 700) -> str | None:
    client = _client()
    if client is None:
        return None
    try:
        response = client.chat.completions.create(
            model=settings.model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=0,
            response_format={"type": "json_object"},
        )
        return response.choices[0].message.content
    except Exception as exc:
        # A model/network failure must never fabricate a result; callers degrade.
        # But record WHY, so the UI can say "out of credits" instead of silently
        # behaving as though no key were configured.
        global _LAST_ERROR
        text = str(exc)
        if "insufficient_quota" in text or "no credits remaining" in text:
            _LAST_ERROR = "OpenAI account has no credits remaining - add credits to re-enable AI answers"
        elif "rate_limit" in text.lower() or "429" in text:
            _LAST_ERROR = "OpenAI rate limit hit - retrying shortly"
        elif "invalid_api_key" in text or "Incorrect API key" in text:
            _LAST_ERROR = "OPENAI_API_KEY is not valid"
        else:
            _LAST_ERROR = f"{exc.__class__.__name__}: {text[:120]}"
        return None


INTERPRET_SYSTEM = """You interpret backend execution logs for a developer watching a run live.

The lines may span SEVERAL separate runs (requests, calls, jobs). Describe the MOST
RECENT one, and say so. Do not blend outcomes: if an earlier run succeeded and the
latest failed, the answer is that the latest failed - never "completed successfully"
in the same breath as a failure. Where the logs mark boundaries (CALL START/CALL END,
Request received/Response sent, a correlation id), use them to find where the last
run begins.

You are given only what was actually observed. Ground every claim in that evidence.

Rules:
- If the logs do not show why something failed, say the cause is unknown. Never guess a
  cause that has no support in the log lines.
- Distinguish what is observed from what is inferred. Inference is fine; state it as inference.
- confidence is your genuine certainty given the evidence: high only when the logs are
  explicit, low when you are reading between the lines.
- If the run has not failed, reason and causes must be empty and status must not be "failed".

Write the summary the way a colleague would say it out loud. State what happened -
never narrate your own reading of the logs. Say "A call from 916... was answered and
greeted", not "The most recent run involved a call". No phrases like "the run", "the
system", "the logs indicate", "it appears that".

Lead with the outcome. If something failed, the first sentence says what failed and
why; the detail comes after.

Also report what the numbers in the run mean. Durations, counts and repeated
patterns are where the useful detail is - a step that took 6.9s, a turn that was
skipped, a retry that succeeded. Only state what the lines actually show.

The logs usually hold SEVERAL runs. After the outcome of the latest one, say how
it compares with the ones before it - the same, slower, newly failing, or doing
something none of the others did. "It behaved the same as the earlier request,
which also completed in 7 steps" tells a developer more than the latest run's
numbers alone.

Walk the run: which component handled it, what each step did, and the
numbers on them. "The api took the request, the orchestrator ran 7 steps -
scope check, generate, quality check - and delivered 136 characters in
4969ms" beats "the request completed in 7 steps". The specifics ARE the
brief; do not hold them back for a list somewhere else.

Then name what is worth looking at next, when the logs support one. A run with no
errors can still be worth a second look: work that was offered and never
completed, a step that always skips, a value that never arrives. End on that when
it exists - "check why lead capture is offered but never captured" - and end on
the outcome when it does not. Never invent a concern the lines do not show.

Return JSON only:
{
  "summary": "4-7 sentences, plain spoken English, and DETAILED - this paragraph is the whole brief, so put the specifics in it rather than saving them for another field. Outcome first, then the run step by step with its real numbers (what each stage did and how long it took), then how it compares with earlier runs, then what is worth checking next. Name the components, routes and identifiers the lines actually show. No log jargon.",
  "status": "running" | "failed" | "success" | "idle",
  "reason": "why it failed, or empty string if it has not failed",
  "causes": ["likely causes, most probable first; empty if not failed or if unknowable"],
  "fixes": ["concrete next steps a developer can take; empty if nothing is wrong"],
  "insights": ["notable observations about THIS run - slow steps with their timings, skipped work and why, retries, anything a developer would want flagged. Empty if nothing stands out."],
  "confidence": 0-100,
  "evidence": ["exact log lines that support your reading"]
}"""


def interpret_run(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    """Explain the current run. Returns None when no model is configured."""
    if not snapshot.get("log_lines"):
        return None

    raw = _chat(
        [
            {"role": "system", "content": INTERPRET_SYSTEM},
            {"role": "user", "content": _run_context(snapshot)},
        ]
    )
    if raw is None:
        return None

    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None

    return {
        "summary": str(data.get("summary", "")).strip(),
        "status": data.get("status"),
        "reason": str(data.get("reason", "")).strip(),
        # A cause of "unknown" is not a cause - it fills the panel with a word that
        # says less than an empty panel would.
        "causes": [
            str(c) for c in data.get("causes", [])
            if str(c).strip() and str(c).strip().lower() not in {"unknown", "n/a", "none", "unclear"}
        ],
        "fixes": [str(f) for f in data.get("fixes", []) if str(f).strip()],
        "insights": [str(i) for i in data.get("insights", []) if str(i).strip()][:5],
        "confidence": max(0, min(100, int(data.get("confidence", 0) or 0))),
        "evidence": [str(e) for e in data.get("evidence", []) if str(e).strip()],
        "source": "llm",
    }


OUTCOME_SYSTEM = """You judge whether ONE run actually achieved what it exists to do - not
whether it errored, but whether anything real happened.

A run can complete with zero errors and a clean hangup and still have accomplished
nothing: a caller who spoke to nobody, a request that returned 200 with an empty
answer, a job that ran every step and produced no output. That verdict - clean logs,
hollow outcome - is the one every error-based check misses, and it is the reason this
judgement exists separately from "did it error".

First, work out from the run's own opening lines and the components it touched what
this kind of run is FOR - what a caller/request/job like this one exists to accomplish.
Then check whether the logs show that actually happening, not just that steps ran.

Verdicts:
- "achieved": it did what it set out to do.
- "failed": it errored, and the logs say so.
- "hollow": it completed (or ended) cleanly, but achieved nothing - the purpose was
  never met even though nothing crashed. Requires citing the specific evidence that
  shows nothing was achieved (e.g. "no speech captured", "response body was empty",
  "no output written") - a hollow verdict with no such citation is not trustworthy
  enough to state; use "unknown" instead.
- "degraded": it achieved its purpose, but abnormally - much slower than the run's own
  earlier steps suggest is normal, retried repeatedly before succeeding, or completed
  with a symptom that would concern the person who built this.
- "unknown": the logs do not show enough to say either way. Prefer this over guessing.

Ground every claim in the log lines actually given. Never invent what "normal" looks
like for this system beyond what the run's own logs show.

Return JSON only:
{
  "verdict": "achieved" | "failed" | "hollow" | "degraded" | "unknown",
  "purpose": "one sentence: what this run appears to exist to accomplish",
  "reason": "1-3 sentences: why this verdict, in plain language, citing what happened",
  "confidence": 0-100,
  "evidence": ["exact log lines that support this verdict, copied verbatim"]
}"""


def interpret_outcome(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    """Judge whether a just-finished run achieved anything. Returns None without a model.

    Separate from interpret_run(): that call explains what happened narratively; this
    one answers a different question - not "what happened" but "did it work" - which
    is exactly the question a clean-looking, purposeless run answers wrong if you only
    check for errors.
    """
    if not snapshot.get("log_lines"):
        return None

    raw = _chat(
        [
            {"role": "system", "content": OUTCOME_SYSTEM},
            {"role": "user", "content": _run_context(snapshot)},
        ],
        max_tokens=500,
    )
    if raw is None:
        return None

    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None

    verdict = data.get("verdict")
    if verdict not in {"achieved", "failed", "hollow", "degraded", "unknown"}:
        verdict = "unknown"

    evidence = [str(e) for e in data.get("evidence", []) if str(e).strip()]

    # The plan's one hard rule: "hollow" is the verdict no error check computes, which
    # makes it the one most tempting to over-assert. Require it to point at the actual
    # lines showing nothing was achieved, verified against this run's real logs -
    # exactly the same grounding answer_question() already applies to "yes"/"no".
    corpus = " ".join(
        str(item.get("message", "")) + " " + " ".join(item.get("detail") or [])
        for item in snapshot.get("log_lines", [])
    ).lower()
    if verdict == "hollow":
        grounded = [e for e in evidence if corpus and _appears_in(e, corpus)]
        if not grounded:
            verdict = "unknown"
        else:
            evidence = grounded

    return {
        "verdict": verdict,
        "purpose": str(data.get("purpose", "")).strip(),
        "reason": str(data.get("reason", "")).strip(),
        "confidence": max(0, min(100, int(data.get("confidence", 0) or 0))),
        "evidence": evidence,
        "source": "llm",
    }


ASK_SYSTEM = """You answer a developer's question about ONE specific run, using only its logs.

FIRST, before anything else: check whether the thing the question asks about appears in
the logs at all. A question can name a component that this run never touched. If the
logs contain no mention of it, the verdict is "unknown" and you say it does not appear
in this run - do NOT answer about a different component that happens to have failed,
and do NOT accept the question's premise that it was involved.

Example: asked "did the embedding service fail?" about a run whose only errors are
"could not connect to postgres", the correct answer is "unknown - this run shows no
embedding service activity", NOT "yes, it failed".

Then, if it does appear, read the question precisely: "did X fail?" is a question about
failure, not about whether the word X appears. A run that mentions X while succeeding
means the answer is "no".

Rules:
- Answer only from the evidence given. If the logs cannot settle it, verdict is "unknown".
- Never treat the presence of a keyword as proof of the event the developer asked about.
- Quote the specific lines you relied on. Every quoted line must appear verbatim in the
  logs above. If you cannot quote a line that is genuinely about the thing asked about,
  the verdict is "unknown".
- Honour negation and outcome words in the question (failed, succeeded, skipped, retried).
- Do not infer that a component exists because the question implies it does.
- Prefer the EARLIEST line that explains an outcome over the last line that states it.
  A summary line often reports a decision whose real cause was logged just before it -
  "refused as not whitelisted" may follow "lookup FAILED ... 500 Internal Server Error",
  in which case the honest answer is that the lookup errored and the service failed
  closed, NOT that the user was genuinely off the list. Read the lines before the
  outcome before concluding why it happened.

Return JSON only:
{
  "answer": "a direct 1-2 sentence answer to the question as asked",
  "verdict": "yes" | "no" | "unknown",
  "confidence": 0-100,
  "evidence": ["exact log lines you relied on, copied verbatim"]
}"""


def _appears_in(quoted: str, corpus: str) -> bool:
    """Whether a quoted evidence line really occurs in the run's logs.

    Compared on content words rather than exact text, since the model reasonably
    reformats timestamps and level prefixes when quoting.
    """
    words = [w for w in re.findall(r"[a-z0-9_:/.-]+", quoted.lower()) if len(w) > 3]
    if not words:
        return False
    hits = sum(1 for w in words if w in corpus)
    return hits >= max(2, len(words) // 2)


def answer_question(snapshot: dict[str, Any], question: str) -> dict[str, Any] | None:
    """Answer a question about this run. Returns None when no model is configured."""
    raw = _chat(
        [
            {"role": "system", "content": ASK_SYSTEM},
            {
                "role": "user",
                "content": f"{_run_context(snapshot)}\n\nQUESTION: {question}",
            },
        ],
        max_tokens=500,
    )
    if raw is None:
        return None

    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None

    verdict = data.get("verdict")
    if verdict not in {"yes", "no", "unknown"}:
        verdict = "unknown"

    evidence = [str(e) for e in data.get("evidence", []) if str(e).strip()]
    # An answer with no cited evidence is not a grounded answer.
    if not evidence and verdict != "unknown":
        verdict = "unknown"

    # Verify each quoted line actually exists in the run. A model asked "did the
    # embedding service fail?" about a database failure will otherwise accept the
    # premise and answer yes, citing lines that say nothing about embeddings.
    corpus = " ".join(
        str(item.get("message", "")) + " " + " ".join(item.get("detail") or [])
        for item in snapshot.get("log_lines", [])
    ).lower()
    if corpus:
        grounded = [e for e in evidence if _appears_in(e, corpus)]
        if verdict != "unknown" and not grounded:
            return {
                "answer": "I cannot answer that from this run's logs - nothing in them supports it.",
                "verdict": "unknown",
                "confidence": 0,
                "evidence": [],
                "source": "llm",
            }
        evidence = grounded or evidence

    return {
        "answer": str(data.get("answer", "")).strip(),
        "verdict": verdict,
        "confidence": max(0, min(100, int(data.get("confidence", 0) or 0))),
        "evidence": evidence,
        "source": "llm",
    }
