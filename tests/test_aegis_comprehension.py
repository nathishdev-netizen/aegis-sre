"""Regression tests - the project brief (C6's second model step).

The failure modes of a model-written description: naming things that do not
exist, silently costing a call on every view, pretending to work with no
model, and - the one that actually happened - feeding the model a truncated
fact sheet and then blaming it for not seeing the numbers.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aegis.l4_understanding.comprehension import (  # noqa: E402
    ProjectComprehension, build_fact_sheet, ground)


FACTS = build_fact_sheet(
    {
        "files_scanned": 10,
        "entrypoints": [{"kind": "http", "method": "POST",
                         "path": "/process-event",
                         "function": "process_event", "file": "main.py",
                         "line": 233}],
        "external_calls": [{"kind": "config", "target": "https://api.example.com",
                            "function": "call_api", "file": "a.py",
                            "line": 5, "guarded": False}],
        "dependencies": ["api.example.com"],
        "log_statements": [{}] * 7,
    },
    operations=[{"operation": "process_event completed",
                 "median_ms": 1746.0, "p95_ms": 260838.0, "count": 200}],
)


class FakeRouter:
    def __init__(self, reply):
        self.reply = reply
        self.calls = 0

    def chat(self, task, messages, purpose=""):
        self.calls += 1
        return self.reply


def test_a_fabricated_identifier_drops_its_sentence_not_the_document():
    kept, dropped = ground(
        "The service exposes process_event for events. "
        "Background work runs in the nightly_reconciler_task loop. "
        "It calls https://api.example.com without a timeout.", FACTS)
    assert "process_event" in kept
    assert "api.example.com" in kept
    assert "nightly_reconciler_task" not in kept, "fabrication survived"
    assert len(dropped) == 1 and "nightly_reconciler_task" in dropped[0]


def test_ordinary_prose_is_never_censored():
    kept, dropped = ground(
        "The system appears to be a web service. Its purpose is unclear.",
        FACTS)
    assert dropped == []
    assert "web service" in kept


def test_measured_numbers_survive_any_truncation():
    """The bug that actually happened: entrypoints and external calls came
    first in the fact sheet, the prompt truncates, and the model truthfully
    reported "no telemetry supplied" three runs in a row while the medians
    sat below the cut. Measured facts must serialise FIRST."""
    big = build_fact_sheet(
        {"entrypoints": [{"function": f"handler_{i}", "path": f"/route/{i}",
                          "kind": "http", "method": "GET"}
                         for i in range(40)],
         "external_calls": [{"target": f"https://svc{i}.example.com",
                             "function": f"caller_{i}"} for i in range(40)],
         "log_statements": []},
        operations=[{"operation": "slow_op completed",
                     "median_ms": 1746.0, "p95_ms": 260838.0, "count": 200}],
    )
    head = json.dumps(big, indent=1)[:6000]
    assert "260838" in head, "the measured p95 fell past the prompt cap again"


def test_duplicate_external_targets_collapse_to_one():
    facts = build_fact_sheet(
        {"external_calls": [{"target": "https://same.example.com",
                             "function": f"f{i}"} for i in range(30)],
         "log_statements": []})
    assert len(facts["external_calls"]) == 1


def test_the_cache_spends_no_call_when_nothing_changed():
    router = FakeRouter("The service exposes process_event.")
    comp = ProjectComprehension(Path(tempfile.mkdtemp()))
    first = comp.generate(FACTS, router)
    again = comp.generate(FACTS, router)
    assert first["ok"] and again.get("from_cache") is True
    assert router.calls == 1, "an unchanged project cost a second call"


def test_changed_facts_regenerate():
    router = FakeRouter("The service exposes process_event.")
    comp = ProjectComprehension(Path(tempfile.mkdtemp()))
    comp.generate(FACTS, router)
    changed = dict(FACTS, files_scanned=99)
    comp.generate(changed, router)
    assert router.calls == 2


def test_no_model_is_an_honest_absence_not_a_fake_brief():
    comp = ProjectComprehension(Path(tempfile.mkdtemp()))
    doc = comp.generate(FACTS, FakeRouter(None))
    assert doc["ok"] is False
    assert doc["brief"] == ""
    assert comp.brief() == "", "an absent brief must read as empty everywhere"


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
    print(f"\n{'FAILED' if failures else 'All comprehension tests passed'}")
    sys.exit(1 if failures else 0)
