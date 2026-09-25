"""C10 - the reasoning layer: explain what the detectors found. Never detect.

This is the first and only place in the platform a model runs, and it runs
under three constraints the code enforces rather than requests:

  1. It sees only redacted text. Redaction happened at ingest (Phase 0), so
     there is no unredacted variant TO send - the guarantee is structural.
  2. It is governed: the router's budget and audit apply to every call.
  3. It is grounded: every evidence line the model cites must actually appear
     in the incident. Fabricated citations are dropped; if none survive, the
     hypothesis is delivered as UNVERIFIED with confidence forced to "low".
     Research is blunt on this - explanations raise reliance on wrong answers
     as much as right ones; only checkable sources calibrate trust.

The output is the doc's Hypothesis schema: statement, confidence,
evidence_refs, immediate_action, durable_fix, model_used.
"""

from __future__ import annotations

import json
import re
from typing import Any

from aegis.l7_reasoning.router import ModelRouter

_PROMPT = """You are explaining one incident from a production log to the engineer who owns the service. Be plain and concrete; no hedging filler.

INCIDENT (signals were produced by statistical detectors, not by you):
{incident}
{precedents}
{precedents}
{precedents}
{precedents}
{precedents}

Reply with ONLY a JSON object:
{{
  "statement": "one or two sentences: what happened and the most likely why",
  "confidence": "high" | "medium" | "low",
  "evidence_refs": ["lines copied VERBATIM from the evidence above that support the statement"],
  "immediate_action": "the one thing to do right now, or 'none needed'",
  "durable_fix": "what would stop this recurring, or 'unknown'"
}}

Rules:
- evidence_refs must be copied exactly from the evidence lines given. Do not paraphrase them.
- If the evidence cannot settle the cause, say so in the statement and use confidence "low".
- Never invent services, numbers, or errors that do not appear above.
- PRECEDENTS, if present, are hints from past incidents - not conclusions. Systems change. Use one only if THIS incident's evidence is consistent with it, and say when you did."""


def _precedent_block(precedents: list[dict[str, Any]] | None) -> str:
    if not precedents:
        return ""
    lines = ["\\nPRECEDENTS (past incidents with a similar signature - hints, not conclusions):"]
    for p in precedents[:3]:
        lines.append(
            f"- {p.get('id')} (similarity {p.get('similarity')}): "
            f"cause line then: {p.get('cause', '')[:90]!r}; "
            f"diagnosis then: {p.get('diagnosis_then') or 'none recorded'}; "
            f"outcome: {p.get('outcome')}"
            + (f" ({p.get('outcome_note')})" if p.get("outcome_note") else ""))
    return "\\n".join(lines)


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _grounded(ref: str, evidence: list[str]) -> bool:
    """A citation counts only if it appears in the incident's own evidence.
    Prefix match tolerates the model trimming a long line's tail."""
    needle = _normalise(ref)
    if len(needle) < 12:
        return False
    return any(needle in _normalise(line) or _normalise(line) in needle
               or _normalise(line).startswith(needle[:60])
               for line in evidence)


def _extract_json(text: str) -> dict[str, Any] | None:
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


class Explainer:
    def __init__(self, router: ModelRouter | None = None) -> None:
        self.router = router or ModelRouter()

    def explain(self, incident: dict[str, Any], provider: str | None = None,
                model: str | None = None,
                precedents: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
        """One incident in, one grounded Hypothesis out. None when the model
        layer is unavailable - everything below keeps working (P8)."""
        evidence = list(incident.get("evidence") or [])
        payload = {
            "id": incident.get("id"),
            # What this project IS, when a brief exists - so "slow" can be
            # read against purpose (a background classifier taking 30s is a
            # different fact from a login endpoint taking 30s).
            "project": (incident.get("project_brief") or "")[:900] or None,
            "severity": incident.get("severity"),
            "status": incident.get("status"),
            "opened_at": incident.get("opened_at"),
            "blast_radius": incident.get("blast_radius"),
            "signals": [
                {
                    "detector": s.get("detector"),
                    "at": s.get("started_at"),
                    "observed": s.get("observed"),
                    "baseline": s.get("baseline"),
                    "severity": s.get("severity"),
                }
                for s in (incident.get("signals") or [])
            ],
            "evidence": evidence,
        }
        raw = self.router.chat(
            "explain_incident",
            [{"role": "user",
              "content": _PROMPT.format(incident=json.dumps(payload, indent=1),
                                        precedents=_precedent_block(precedents))}],
            purpose=f"explain {incident.get('id')}",
            provider=provider, model=model,
        )
        if raw is None:
            return None
        parsed = _extract_json(raw)
        if parsed is None:
            return None

        cited = [str(r) for r in (parsed.get("evidence_refs") or [])]
        surviving = [r for r in cited if _grounded(r, evidence)]
        dropped = len(cited) - len(surviving)
        confidence = str(parsed.get("confidence", "low")).lower()
        verified = bool(surviving)
        if not verified:
            # A hypothesis whose every citation was fabricated is an opinion.
            confidence = "low"

        return {
            "statement": str(parsed.get("statement", "")).strip(),
            "confidence": confidence if confidence in ("high", "medium", "low") else "low",
            "evidence_refs": surviving,
            "dropped_refs": dropped,
            "verified": verified,
            "immediate_action": str(parsed.get("immediate_action", "")).strip(),
            "durable_fix": str(parsed.get("durable_fix", "")).strip(),
            "model_used": model or "routed",
            "precedents_considered": [p.get("id") for p in (precedents or [])],
        }
