"""Phase 6 regression tests - C10 reasoning + C16 governance.

Every test uses a fake transport: the suite makes ZERO real API calls, so
running it a thousand times costs nothing and drains no key. That is itself
one of the layer's rules under test - spend is governed by the runtime.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os  # noqa: E402

from aegis.l7_reasoning.explainer import Explainer  # noqa: E402
from aegis.l7_reasoning.governance import AuditLog, Budget  # noqa: E402
from aegis.l7_reasoning.router import ModelRouter  # noqa: E402


def _fake(reply: dict) -> tuple[list, callable]:
    calls: list = []
    def transport(url, headers, body, timeout):
        calls.append({"url": url, "body": body})
        return {"choices": [{"message": {"content": json.dumps(reply)}}]}
    return calls, transport


def _router(reply: dict, max_calls: int = 10) -> tuple[list, ModelRouter]:
    calls, transport = _fake(reply)
    audit = AuditLog(Path(tempfile.mkdtemp()) / "audit.jsonl")
    return calls, ModelRouter(budget=Budget(max_calls=max_calls, min_interval_s=0),
                              audit=audit, transport=transport)


def _incident(evidence: list[str]) -> dict:
    return {"id": "INC-1", "severity": "P2", "status": "open",
            "opened_at": "13:48:54", "blast_radius": {"signals": 2},
            "signals": [{"detector": "NoveltyDetector", "started_at": "13:48:54",
                         "observed": 1, "baseline": 0, "severity": "P2"}],
            "evidence": evidence}


EVIDENCE = ["WARNING voice.orch: [intent] Classification failed for session=voice",
            "INFO voice: [api] CALL END call=<UUID> - 93s, hangup_cause=NORMAL"]


# --- Governance --------------------------------------------------------------

def test_budget_is_a_hard_ceiling():
    """Spend limits are enforced by the runtime, not the prompt (P7). Call
    eleven times against a budget of three: eight refusals, no exceptions."""
    os.environ.setdefault("GROQ_API_KEY", "test-key")
    calls, router = _router({"statement": "x", "confidence": "low",
                             "evidence_refs": []}, max_calls=3)
    answers = [router.chat("explain_incident", [{"role": "user", "content": "hi"}])
               for _ in range(11)]
    assert len(calls) == 3
    assert answers.count(None) == 8


def test_every_call_is_audited_before_it_happens():
    calls, transport = _fake({"statement": "x"})
    audit_path = Path(tempfile.mkdtemp()) / "audit.jsonl"
    router = ModelRouter(budget=Budget(max_calls=1, min_interval_s=0),
                         audit=AuditLog(audit_path), transport=transport)
    os.environ.setdefault("GROQ_API_KEY", "test-key")
    router.chat("explain_incident", [{"role": "user", "content": "hi"}], purpose="p")
    router.chat("explain_incident", [{"role": "user", "content": "hi"}], purpose="p")
    entries = [json.loads(line) for line in audit_path.read_text().splitlines()]
    assert len(entries) == 2
    assert entries[0]["allowed"] is True
    assert entries[1]["allowed"] is False and "budget" in entries[1]["reason"]


def test_no_key_degrades_never_raises():
    """P8: if the model layer is down, everything else still works."""
    saved = os.environ.pop("GROQ_API_KEY", None)
    try:
        calls, transport = _fake({})
        router = ModelRouter(budget=Budget(), transport=transport,
                             audit=AuditLog(Path(tempfile.mkdtemp()) / "a.jsonl"))
        assert router.chat("explain_incident", [{"role": "user", "content": "hi"}]) is None
        assert calls == [], "a call was attempted with no key"
    finally:
        if saved:
            os.environ["GROQ_API_KEY"] = saved


def test_routing_is_configuration_not_code():
    """The anti-pattern table names hard-coded models. An env override must
    reroute a task without touching any component."""
    os.environ["AEGIS_MODEL_EXPLAIN_INCIDENT"] = "openai:gpt-4o-mini"
    os.environ.setdefault("OPENAI_API_KEY", "test-key")
    try:
        calls, router = _router({"statement": "x"})
        router.chat("explain_incident", [{"role": "user", "content": "hi"}])
        assert "openai.com" in calls[0]["url"]
        assert calls[0]["body"]["model"] == "gpt-4o-mini"
    finally:
        del os.environ["AEGIS_MODEL_EXPLAIN_INCIDENT"]


# --- Grounding ---------------------------------------------------------------

def test_fabricated_citations_are_dropped_and_confidence_forced_low():
    """The model cited a line that exists nowhere in the incident. Delivering
    it as evidence would be the fluent-but-unverifiable claim this project
    exists to avoid."""
    os.environ.setdefault("GROQ_API_KEY", "test-key")
    calls, router = _router({
        "statement": "The database ran out of connections.",
        "confidence": "high",
        "evidence_refs": ["ERROR db: connection pool exhausted at 13:48:54"],
        "immediate_action": "restart", "durable_fix": "bigger pool",
    })
    hypothesis = Explainer(router).explain(_incident(EVIDENCE))
    assert hypothesis["evidence_refs"] == []
    assert hypothesis["dropped_refs"] == 1
    assert hypothesis["verified"] is False
    assert hypothesis["confidence"] == "low", "an ungrounded claim kept high confidence"


def test_real_citations_survive_grounding():
    os.environ.setdefault("GROQ_API_KEY", "test-key")
    calls, router = _router({
        "statement": "Intent classification failed and the call degraded to 93s.",
        "confidence": "medium",
        "evidence_refs": [EVIDENCE[0], EVIDENCE[1]],
        "immediate_action": "none needed", "durable_fix": "add retry",
    })
    hypothesis = Explainer(router).explain(_incident(EVIDENCE))
    assert len(hypothesis["evidence_refs"]) == 2
    assert hypothesis["verified"] is True
    assert hypothesis["confidence"] == "medium"


def test_the_model_sees_only_what_the_incident_carries():
    """The prompt is built from the incident dict alone - already-redacted
    text. No raw line, no file path, no phone number has a route in."""
    os.environ.setdefault("GROQ_API_KEY", "test-key")
    calls, router = _router({"statement": "x", "confidence": "low",
                             "evidence_refs": []})
    Explainer(router).explain(_incident(["CALL START from <PHONE>"]))
    sent = json.dumps(calls[0]["body"])
    assert "<PHONE>" in sent, "redacted placeholder should be present"
    assert "916360722483" not in sent


def test_unparseable_model_output_yields_none_not_a_crash():
    os.environ.setdefault("GROQ_API_KEY", "test-key")
    calls, transport = _fake({})
    def bad_transport(url, headers, body, timeout):
        return {"choices": [{"message": {"content": "I think probably the disk?"}}]}
    router = ModelRouter(budget=Budget(min_interval_s=0), transport=bad_transport,
                         audit=AuditLog(Path(tempfile.mkdtemp()) / "a.jsonl"))
    assert Explainer(router).explain(_incident(EVIDENCE)) is None


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
    print(f"\n{'FAILED' if failures else 'All reasoning tests passed'}"
          f"{f' ({failures} failing)' if failures else ''}")
    sys.exit(1 if failures else 0)
