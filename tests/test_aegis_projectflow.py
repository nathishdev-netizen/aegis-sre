"""Regression tests - the project drawn from facts that already existed.

The failure this prevents: the brief could describe a service's flow in
prose while the screen called Flow graph drew one box. Everything needed was
computed and stored; nothing assembled it, and no node could be clicked into.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aegis.l4_understanding.projectflow import build_flow  # noqa: E402

CODE = {
    "entrypoints": [
        {"kind": "http", "method": "POST", "path": "/process-event",
         "function": "process_event", "file": "main.py", "line": 233},
        {"kind": "http", "method": "GET", "path": "/health",
         "function": "health", "file": "main.py", "line": 218},
    ],
    "external_calls": [
        {"kind": "config", "target": "https://api.openai.com/v1/chat/completions",
         "function": "complete", "file": "llm.py", "line": 64,
         "guarded": False, "has_timeout": False},
        {"kind": "http", "target": "http://cms.localhost:8000",
         "function": "fetch", "file": "cms.py", "line": 12,
         "guarded": True, "has_timeout": True},
    ],
}
OPERATIONS = [
    {"operation": "process_event completed", "median_ms": 1746.0,
     "p95_ms": 260838.0, "count": 200},
    {"operation": "summarize completed", "median_ms": 5758.5,
     "p95_ms": 34851.0, "count": 180},
]
DEPENDENCIES = [{"host": "api.openai.com", "evidence": 12, "running_here": False}]
INCIDENTS = [{"id": "INC-3",
              "evidence": ["INFO process_event completed in 72551ms"]}]
EVENTS = [{"text": "INFO process_event completed in 72551ms trace_id=abc"}]


def test_three_bands_are_built_from_real_facts():
    flow = build_flow(CODE, OPERATIONS, DEPENDENCIES, INCIDENTS, EVENTS)
    counts = flow["counts"]
    assert counts["entry"] == 2
    assert counts["work"] == 2
    assert counts["depends"] >= 2
    assert counts["unguarded"] == 1, "the unguarded OpenAI call was not counted"


def test_a_hang_is_visible_on_the_node_itself():
    """p95 149x the median is the shape of an occasional hang. It took
    twenty separate incidents to notice it; the node should say it."""
    flow = build_flow(CODE, OPERATIONS)
    node = [n for n in flow["nodes"] if n.get("label") == "process_event"][0]
    assert node["spread"] > 100, "the spread between median and p95 is not shown"
    assert node["observed"] is True


def test_every_node_carries_its_own_evidence():
    """A diagram nobody can click into answers nothing - finding 22."""
    flow = build_flow(CODE, OPERATIONS, DEPENDENCIES, INCIDENTS, EVENTS)
    node = [n for n in flow["nodes"] if n.get("label") == "process_event"][0]
    assert "INC-3" in node["incidents"], "the incident here is not linked"
    assert node["log_lines"], "no log line reaches the node that produced it"


def test_declared_and_observed_are_never_blurred():
    """Code says what CAN be called; logs say what WAS. A node resting only
    on source must not look like one seen running."""
    flow = build_flow(CODE, OPERATIONS, DEPENDENCIES)
    by_label = {n["label"]: n for n in flow["nodes"]}
    assert by_label["api.openai.com"]["observed"] is True
    assert by_label["cms.localhost:8000"]["observed"] is False
    assert by_label["cms.localhost:8000"]["in_code"] is True


def test_a_guarded_call_is_not_reported_as_unguarded():
    flow = build_flow(CODE, [], [])
    cms = [n for n in flow["nodes"] if n["label"] == "cms.localhost:8000"][0]
    assert cms["unguarded"] == 0
    site = cms["call_sites"][0]
    assert site["guarded"] is True and site["has_timeout"] is True


def test_nothing_analyzed_draws_nothing():
    """A flow drawn from no facts would be a fiction."""
    flow = build_flow({}, [], [])
    assert flow["counts"]["entry"] == 0
    assert flow["counts"]["work"] == 0


def test_an_entrypoint_links_to_the_operation_of_the_same_name():
    flow = build_flow(CODE, OPERATIONS)
    edges = [(e["from"], e["to"]) for e in flow["edges"]]
    assert ("entry:process_event", "op:process_event") in edges


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
    print(f"\n{'FAILED' if failures else 'All project-flow tests passed'}")
    sys.exit(1 if failures else 0)
