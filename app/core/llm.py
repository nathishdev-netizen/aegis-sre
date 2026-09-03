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


def _run_context(snapshot: dict[str, Any]) -> str:
    """Compact, factual view of the run. Only observed data - no derived guesses."""
    timeline = snapshot.get("timeline", [])[-settings.llm_timeline_window:]
    logs = snapshot.get("log_lines", [])[-settings.llm_log_window:]
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

You are given only what was actually observed. Ground every claim in that evidence.

Rules:
- If the logs do not show why something failed, say the cause is unknown. Never guess a
  cause that has no support in the log lines.
- Distinguish what is observed from what is inferred. Inference is fine; state it as inference.
- confidence is your genuine certainty given the evidence: high only when the logs are
  explicit, low when you are reading between the lines.
- If the run has not failed, reason and causes must be empty and status must not be "failed".

Return JSON only:
{
  "summary": "2-3 sentences in plain English: what the run did and where it stands now.",
  "status": "running" | "failed" | "success" | "idle",
  "reason": "why it failed, or empty string if it has not failed",
  "causes": ["likely causes, most probable first; empty if not failed or if unknowable"],
  "fixes": ["concrete next steps a developer can take; empty if nothing is wrong"],
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
        "causes": [str(c) for c in data.get("causes", []) if str(c).strip()],
        "fixes": [str(f) for f in data.get("fixes", []) if str(f).strip()],
        "confidence": max(0, min(100, int(data.get("confidence", 0) or 0))),
        "evidence": [str(e) for e in data.get("evidence", []) if str(e).strip()],
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
