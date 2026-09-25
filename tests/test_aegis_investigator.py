"""The investigation loop. Fake router, real tools - the point is the LOOP:
does it test before concluding, refuse hallucinated tools, stay bounded, and
ground its citations in what tools actually returned."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aegis.l7_reasoning.investigator import Investigation  # noqa: E402


class ScriptedRouter:
    """Replays a fixed sequence of model replies, recording what it was asked."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    def chat(self, task, messages, purpose="", **kw):
        self.prompts.append(messages[-1]["content"])
        return self.replies.pop(0) if self.replies else None


TOOLS = {
    "get_baseline": lambda a: "shop stock reserved: usually 74.0ms (30 samples)",
    "get_code_for": lambda a: "written at stock.py:8 in reserve()\n"
                              "  reserve() calls http://localhost:9100 - NOT guarded, NO timeout",
    "boom": lambda a: (_ for _ in ()).throw(RuntimeError("tool exploded")),
}

INCIDENT = {"id": "INC-1", "severity": "P2", "opened_at": "09:30",
            "signals": [{"detector": "LatencyShift", "observed": 11400, "baseline": 74}],
            "evidence": ["stock reserved order_id=A-1026 in 11400ms"]}


def test_it_tests_before_concluding_and_grounds_in_tool_output():
    """The whole point: a conclusion citing a line only a TOOL revealed -
    one-shot Explain could never have quoted stock.py:8."""
    router = ScriptedRouter([
        json.dumps({"tool": "get_code_for", "args": {"log_text": "stock reserved"},
                    "why": "is the call guarded?"}),
        json.dumps({"done": True, "statement": "unguarded call to :9100",
                    "confidence": "high",
                    "evidence_refs": ["written at stock.py:8 in reserve()"],
                    "ruled_out": ["the operation being inherently slow"],
                    "immediate_action": "add a timeout",
                    "durable_fix": "guard the dependency"}),
    ])
    result = Investigation(router, TOOLS).run(INCIDENT)
    assert result["verified"] is True
    assert result["evidence_refs"] == ["written at stock.py:8 in reserve()"]
    assert result["ruled_out"]
    assert result["steps_taken"] == 2
    assert len(result["trace"]) == 1 and result["trace"][0]["tool"] == "get_code_for"


def test_a_hallucinated_tool_is_refused_with_the_real_list():
    """The literature's most common derailment: a made-up tool call the loop
    never recovers from. It must be told what actually exists."""
    router = ScriptedRouter([
        json.dumps({"tool": "query_prometheus", "args": {}, "why": "metrics"}),
        json.dumps({"done": True, "statement": "x", "confidence": "low",
                    "evidence_refs": []}),
    ])
    Investigation(router, TOOLS).run(INCIDENT)
    correction = router.prompts[-1]
    assert "No tool named" in correction
    assert "get_baseline" in correction, "the refusal must name the real tools"


def test_a_failing_tool_does_not_end_the_investigation():
    router = ScriptedRouter([
        json.dumps({"tool": "boom", "args": {}, "why": "try it"}),
        json.dumps({"done": True, "statement": "recovered", "confidence": "low",
                    "evidence_refs": []}),
    ])
    result = Investigation(router, TOOLS).run(INCIDENT)
    assert result is not None and result["statement"] == "recovered"
    assert any("tool failed" in prompt for prompt in router.prompts), \
        "the loop must be told the tool failed, so it can try something else"


def test_the_loop_is_bounded_and_says_why_it_stopped():
    """Unconstrained loops accumulate errors. It must stop and conclude with
    what it has, rather than spin."""
    router = ScriptedRouter(
        [json.dumps({"tool": "get_baseline", "args": {"operation": "x"},
                     "why": "again"})] * 10 +
        [json.dumps({"done": True, "statement": "partial", "confidence": "low",
                     "evidence_refs": []})])
    result = Investigation(router, TOOLS, max_steps=3).run(INCIDENT)
    assert result["steps_taken"] == 3
    assert result["stopped_because"] == "step budget reached"
    assert len(result["trace"]) == 3


def test_a_fabricated_citation_is_dropped():
    """Same rule as Explain: quote what the tools returned, or it does not
    survive - here checked against TOOL OUTPUT, not a fixed payload."""
    router = ScriptedRouter([
        json.dumps({"tool": "get_baseline", "args": {}, "why": "normal?"}),
        json.dumps({"done": True, "statement": "the disk was full",
                    "confidence": "high",
                    "evidence_refs": ["ERROR disk full at /var/lib"]}),
    ])
    result = Investigation(router, TOOLS).run(INCIDENT)
    assert result["evidence_refs"] == []
    assert result["dropped_refs"] == 1
    assert result["verified"] is False
    assert result["confidence"] == "low"


def test_no_model_means_no_answer_not_a_guess():
    result = Investigation(ScriptedRouter([]), TOOLS).run(INCIDENT)
    assert result is None


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except AssertionError as exc:
                failures += 1
                print(f"  FAIL  {name}: {exc}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"  ERROR {name}: {exc.__class__.__name__}: {exc}")
    print(f"\n{'FAILED' if failures else 'All investigator tests passed'}"
          f"{f' ({failures} failing)' if failures else ''}")
    sys.exit(1 if failures else 0)
