"""Phase 8 regression tests - C15 incident memory.

The layer's failure modes: a stale match asserted as fact, a wrong diagnosis
forgotten, one project's history leaking into another's, and matching so
coarse the true precedent ties with noise - each one pinned below.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aegis.l3_storage.store import ProjectStore  # noqa: E402
from aegis.l6_correlation.memory import IncidentMemory, SignatureExtractor  # noqa: E402
from aegis.l7_reasoning.explainer import Explainer  # noqa: E402
from aegis.l7_reasoning.governance import AuditLog, Budget  # noqa: E402
from aegis.l7_reasoning.router import ModelRouter  # noqa: E402


def _memory() -> IncidentMemory:
    return IncidentMemory(ProjectStore("svc", root=tempfile.mkdtemp()))


def _incident(id: str, evidence: str, detectors: list[str],
              templates: list[str], severity: str = "P2") -> dict:
    return {
        "id": id, "severity": severity, "opened_at": "12:00:00",
        "resolved_at": "12:05:00", "evidence": [evidence],
        "signals": [{"service": "svc", "detector": d, "template_id": t}
                    for d, t in zip(detectors, templates)],
    }


LOOKUP_A = _incident("INC-1", "ERROR identity: Lookup FAILED for <PHONE> after 229ms",
                     ["NoveltyDetector"], ["T-lookup-a"])
LOOKUP_B = _incident("INC-2", "ERROR identity: Lookup FAILED for <PHONE> after 36ms",
                     ["NoveltyDetector"], ["T-lookup-b"])
UNRELATED = _incident("INC-3", "WARNING filler: could not pre-synthesise clip",
                      ["NoveltyDetector"], ["T-filler"])


def test_the_true_precedent_outranks_a_shared_detector():
    """Every novelty-driven incident shares its detector, and the same error
    can land in different templates (tails differ) - so the true precedent
    TIED with unrelated incidents at 0.5. The cause line's own words are
    what separate them."""
    memory = _memory()
    memory.remember(LOOKUP_A)
    memory.remember(UNRELATED)
    matches = memory.similar(LOOKUP_B)
    assert matches, "no precedent found at all"
    assert matches[0]["id"] == "INC-1"
    others = [m for m in matches if m["id"] != "INC-1"]
    assert all(matches[0]["similarity"] > m["similarity"] for m in others), \
        "the true precedent tied with noise"


def test_a_match_never_includes_the_incident_itself():
    memory = _memory()
    memory.remember(LOOKUP_A)
    assert all(m["id"] != "INC-1" for m in memory.similar(LOOKUP_A))


def test_every_match_says_hint_not_conclusion():
    """A past match is a hint - systems change. The label travels WITH the
    match so no consumer can present it as a finding."""
    memory = _memory()
    memory.remember(LOOKUP_A)
    for match in memory.similar(LOOKUP_B):
        assert "not conclusion" in match["note"]


def test_wrong_diagnoses_are_kept_and_surfaced():
    """A remembered wrong diagnosis is worth as much as a right one: it stops
    the same bad hypothesis being handed out with a precedent's authority."""
    memory = _memory()
    memory.remember(LOOKUP_A, hypothesis={"statement": "the disk was full",
                                          "confidence": "high"})
    memory.record_outcome("INC-1", "wrong_diagnosis",
                          "it was the upstream identity service, not the disk")
    match = memory.similar(LOOKUP_B)[0]
    assert match["outcome"] == "wrong_diagnosis"
    assert "identity service" in match["outcome_note"]
    assert match["diagnosis_then"] == "the disk was full"


def test_memory_survives_a_restart():
    root = tempfile.mkdtemp()
    store = ProjectStore("svc", root=root)
    IncidentMemory(store).remember(LOOKUP_A)
    store.close()
    reopened = IncidentMemory(ProjectStore("svc", root=root))
    assert reopened.similar(LOOKUP_B), "the archive did not survive"


def test_projects_do_not_share_memory():
    root = tempfile.mkdtemp()
    IncidentMemory(ProjectStore("project-a", root=root)).remember(LOOKUP_A)
    other = IncidentMemory(ProjectStore("project-b", root=root))
    assert other.similar(LOOKUP_B) == [], "project-b saw project-a's history"


def test_dissimilar_incidents_are_not_matched():
    memory = _memory()
    memory.remember(UNRELATED)
    different = _incident("INC-9", "gateway timeout calling orchestrator",
                          ["LatencyShift"], ["T-orch"], severity="P3")
    assert memory.similar(different) == []


def test_signature_is_structure_not_prose():
    signature = SignatureExtractor.extract(LOOKUP_A)
    assert signature["detectors"] == ["NoveltyDetector"]
    assert signature["templates"] == ["T-lookup-a"]
    assert "lookup" in signature["cause_tokens"]
    assert "failed" in signature["cause_tokens"]


def test_pattern_miner_finds_recurring_themes():
    memory = _memory()
    memory.remember(LOOKUP_A)
    memory.remember(_incident("INC-5", "ERROR identity: Lookup FAILED again",
                              ["NoveltyDetector"], ["T-lookup-a"]))
    memory.remember(UNRELATED)
    patterns = memory.patterns(min_count=2)
    assert any(f["value"] == "T-lookup-a" and len(f["incidents"]) == 2
               for f in patterns)


def test_precedents_reach_the_prompt_as_hints():
    """KnowledgeInjector: memory is fed into context BEFORE the hypothesis is
    formed, and the prompt itself carries the hint-not-conclusion framing."""
    captured = {}
    def transport(url, headers, body, timeout):
        captured["prompt"] = body["messages"][0]["content"]
        return {"choices": [{"message": {"content": json.dumps(
            {"statement": "x", "confidence": "low", "evidence_refs": []})}}]}
    import os
    os.environ.setdefault("GROQ_API_KEY", "test-key")
    router = ModelRouter(budget=Budget(min_interval_s=0), transport=transport,
                         audit=AuditLog(Path(tempfile.mkdtemp()) / "a.jsonl"))
    memory = _memory()
    memory.remember(LOOKUP_A, hypothesis={"statement": "identity svc was down",
                                          "confidence": "medium"})
    memory.record_outcome("INC-1", "worked", "restarted it")
    precedents = memory.similar(LOOKUP_B)
    result = Explainer(router).explain(LOOKUP_B, precedents=precedents)
    assert "PRECEDENTS (past incidents" in captured["prompt"]
    assert "hints, not conclusions" in captured["prompt"]
    assert "identity svc was down" in captured["prompt"]
    assert result["precedents_considered"] == ["INC-1"]


def test_no_precedents_means_a_clean_prompt():
    captured = {}
    def transport(url, headers, body, timeout):
        captured["prompt"] = body["messages"][0]["content"]
        return {"choices": [{"message": {"content": json.dumps(
            {"statement": "x", "confidence": "low", "evidence_refs": []})}}]}
    import os
    os.environ.setdefault("GROQ_API_KEY", "test-key")
    router = ModelRouter(budget=Budget(min_interval_s=0), transport=transport,
                         audit=AuditLog(Path(tempfile.mkdtemp()) / "a.jsonl"))
    Explainer(router).explain(LOOKUP_B)
    # The static rules text may MENTION precedents; the injected block - with
    # its "(past incidents" header - must be absent.
    assert "PRECEDENTS (past incidents" not in captured["prompt"]


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
    print(f"\n{'FAILED' if failures else 'All memory tests passed'}"
          f"{f' ({failures} failing)' if failures else ''}")
    sys.exit(1 if failures else 0)
