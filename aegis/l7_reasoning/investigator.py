"""The investigation loop: observe -> hypothesise -> test -> conclude.

Explain was one call over a fixed payload. That is a summary, not an
investigation: it could say "stock reservation took 11,400ms" but never
"because stock.py:7 has no timeout", because it was never allowed to go and
look.

This is the observe-hypothesise-test loop real AI SREs run, with the two
guards the literature says decide whether it works at all:

  bounded    a hard step cap. Unconstrained loops accumulate errors and
             derail; the loop stops when confident OR when the budget ends,
             and says which.
  grounded   every tool answers from data the platform already measured. A
             hallucinated tool name is refused with the list of real ones,
             and no tool can invent a value - each returns what was observed
             or says nothing was.

The agent may only call tools. It cannot browse, cannot execute, and cannot
touch the watched project - the tools ARE the whole world it can see.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

MAX_STEPS = 6            # the literature's error-accumulation ceiling, halved

_SYSTEM = """You are investigating one incident in a running system. Work like an engineer: form a hypothesis, TEST it with a tool, and let the answer change your mind.

Each turn, reply with ONLY a JSON object, one of:

  {"tool": "<name>", "args": {...}, "why": "what you expect to learn"}
  {"done": true, "statement": "...", "confidence": "high|medium|low",
   "evidence_refs": ["lines copied VERBATIM from what tools returned"],
   "ruled_out": ["hypotheses you tested and eliminated, with why"],
   "immediate_action": "...", "durable_fix": "..."}

Rules:
- Test before concluding. A hypothesis you have not checked is a guess.
- Prefer the tool that could DISPROVE your hypothesis.
- evidence_refs must be copied exactly from tool output. Never invent a line.
- If the tools cannot settle it, finish with confidence "low" and say what is missing.
- Do not repeat a tool call you already made with the same arguments."""


class Investigation:
    """One incident, investigated. Every tool call and result is recorded, so
    the conclusion can be read back as a chain rather than an assertion."""

    def __init__(self, router: Any, tools: dict[str, Callable[[dict], Any]],
                 max_steps: int = MAX_STEPS) -> None:
        self.router = router
        self.tools = tools
        self.max_steps = max_steps
        self.trace: list[dict[str, Any]] = []

    def _tool_catalogue(self) -> str:
        return "\n".join(f"  {name}: {fn.__doc__ or ''}".strip()
                         for name, fn in self.tools.items())

    def run(self, incident: dict[str, Any]) -> dict[str, Any] | None:
        opening = {
            "id": incident.get("id"), "severity": incident.get("severity"),
            # The brief changes which hypotheses are worth testing first: a
            # background task and a request path fail differently.
            "project": (incident.get("project_brief") or "")[:900] or None,
            "opened_at": incident.get("opened_at"),
            "signals": [{"detector": s.get("detector"), "at": s.get("started_at"),
                         "observed": s.get("observed"), "baseline": s.get("baseline")}
                        for s in (incident.get("signals") or [])][:6],
            "evidence": [e[:160] for e in (incident.get("evidence") or [])][:5],
        }
        messages = [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content":
                f"TOOLS AVAILABLE:\n{self._tool_catalogue()}\n\n"
                f"INCIDENT:\n{json.dumps(opening, indent=1)}\n\n"
                "Investigate. Start by testing your most likely hypothesis."},
        ]

        for step in range(self.max_steps):
            raw = self.router.chat("explain_incident", messages,
                                   purpose=f"investigate {incident.get('id')} step {step+1}")
            if raw is None:
                break
            move = _parse(raw)
            if move is None:
                messages.append({"role": "user", "content":
                                 "That was not valid JSON. Reply with one JSON object."})
                continue

            if move.get("done"):
                return self._conclude(move, step + 1, "reached a conclusion")

            name = str(move.get("tool", ""))
            args = move.get("args") or {}
            if name not in self.tools:
                # A hallucinated tool is refused WITH the real list, rather
                # than silently ignored - the literature's most common
                # derailment is a made-up tool call the loop never recovers from.
                observation = (f"No tool named {name!r}. Available: "
                               f"{', '.join(self.tools)}")
            else:
                try:
                    observation = self.tools[name](args)
                except Exception as exc:
                    observation = f"tool failed: {exc.__class__.__name__}: {exc}"

            rendered = observation if isinstance(observation, str) else \
                json.dumps(observation, indent=1)[:2500]
            self.trace.append({"step": step + 1, "tool": name, "args": args,
                               "why": move.get("why", ""), "result": rendered[:600]})
            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content":
                             f"RESULT of {name}:\n{rendered}\n\n"
                             "Continue: test another hypothesis, or finish."})

        # Budget spent. Ask for the best conclusion the evidence supports
        # rather than returning nothing - a partial answer with its limits
        # stated beats silence.
        messages.append({"role": "user", "content":
                         "Step budget reached. Conclude now with what the tool "
                         "results support, and say what remains unchecked."})
        raw = self.router.chat("explain_incident", messages,
                               purpose=f"conclude {incident.get('id')}")
        move = _parse(raw) if raw else None
        if move is None:
            return None
        return self._conclude(move, self.max_steps, "step budget reached")

    def _conclude(self, move: dict[str, Any], steps: int, why: str) -> dict[str, Any]:
        # Ground citations against what the TOOLS returned, not against a
        # fixed payload: the investigator saw more than the incident carried.
        seen = "\n".join(entry["result"] for entry in self.trace)
        cited = [str(r) for r in (move.get("evidence_refs") or [])]
        surviving = [r for r in cited if _grounded(r, seen)]
        confidence = str(move.get("confidence", "low")).lower()
        if not surviving:
            confidence = "low"
        return {
            "statement": str(move.get("statement", "")).strip(),
            "confidence": confidence if confidence in ("high", "medium", "low") else "low",
            "evidence_refs": surviving,
            "dropped_refs": len(cited) - len(surviving),
            "verified": bool(surviving),
            "ruled_out": [str(r) for r in (move.get("ruled_out") or [])][:5],
            "immediate_action": str(move.get("immediate_action", "")).strip(),
            "durable_fix": str(move.get("durable_fix", "")).strip(),
            "steps_taken": steps,
            "stopped_because": why,
            "trace": self.trace,
            "model_used": "routed",
        }


def _parse(raw: str) -> dict[str, Any] | None:
    match = re.search(r"\{.*\}", raw or "", re.S)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _grounded(ref: str, corpus: str) -> bool:
    needle = re.sub(r"\s+", " ", ref).strip().lower()
    if len(needle) < 12:
        return False
    hay = re.sub(r"\s+", " ", corpus).lower()
    return needle in hay or needle[:60] in hay
