"""C15 - incident memory: make the system better the longer it runs.

An experienced engineer's advantage is not intelligence, it is having seen
this before. Every resolved incident becomes a retrievable precedent, so the
next investigation starts from a hypothesis instead of a blank page.

Doc rules kept, each one load-bearing:

  signature-based   a structured fingerprint (services, detectors, templates,
                    deviation types), not prose. Within one project the
                    template ids ARE the semantic layer - fingerprinting
                    already canonicalised the wording - which is why matching
                    works without an embedding index at this scale. pgvector
                    is the documented upgrade path, not a day-one dependency.
  outcome-labelled  whether the fix worked is recorded, and a remembered
                    WRONG diagnosis is kept just as carefully as a right one.
  a hint, never a conclusion   every match carries its score and the words
                    "precedent, not conclusion" all the way into the prompt -
                    systems change, and a stale match asserted as fact is
                    worse than no memory at all.
  redacted only     archives store what the pipeline produced, which was
                    redacted at ingest. There is no unredacted variant here.
"""

from __future__ import annotations

import json
import time
from typing import Any

# Below this a match is noise, not a precedent.
MIN_SIMILARITY = 0.35

_WEIGHTS = {"templates": 0.5, "detectors": 0.3, "services": 0.1, "severity": 0.1}


class SignatureExtractor:
    @staticmethod
    def extract(incident: dict[str, Any]) -> dict[str, Any]:
        signals = incident.get("signals") or []
        return {
            "services": sorted({s.get("service", "") for s in signals} - {""}),
            "detectors": sorted({s.get("detector", "") for s in signals} - {""}),
            "templates": sorted({s.get("template_id", "") for s in signals} - {""}),
            "severity": incident.get("severity", ""),
            "deviation_types": sorted({d.get("type", "") for d in
                                       incident.get("deviations", [])} - {""}),
        }


def _jaccard(a: list[str], b: list[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def similarity(sig_a: dict[str, Any], sig_b: dict[str, Any]) -> float:
    score = 0.0
    for key, weight in _WEIGHTS.items():
        if key == "severity":
            score += weight * (1.0 if sig_a.get(key) == sig_b.get(key)
                               and sig_a.get(key) else 0.0)
        else:
            score += weight * _jaccard(sig_a.get(key, []), sig_b.get(key, []))
    return round(score, 3)


class IncidentMemory:
    """Archive, match, and label outcomes - backed by the project's own store,
    so one project's history never informs another's."""

    def __init__(self, store: Any) -> None:
        self.store = store

    # -- IncidentArchive -----------------------------------------------------

    def remember(self, incident: dict[str, Any],
                 hypothesis: dict[str, Any] | None = None) -> None:
        cause_evidence = (incident.get("evidence") or [""])[0]
        self.store.archive_incident({
            "id": str(incident.get("id", "")),
            "opened_at": incident.get("opened_at", ""),
            "resolved_at": incident.get("resolved_at") or "",
            "severity": incident.get("severity", ""),
            "signature": json.dumps(SignatureExtractor.extract(incident)),
            "cause": cause_evidence[:200],
            "hypothesis": json.dumps({
                "statement": hypothesis.get("statement", ""),
                "confidence": hypothesis.get("confidence", ""),
            }) if hypothesis else "",
            "outcome": "",
            "outcome_note": "",
            "archived_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })

    # -- OutcomeTracker ------------------------------------------------------

    def record_outcome(self, incident_id: str, outcome: str, note: str = "") -> None:
        """"worked" / "did_not_work" / "wrong_diagnosis" - the third is worth
        as much as the first: it stops the same bad hypothesis being handed
        out with a precedent's authority."""
        self.store.set_incident_outcome(incident_id, outcome, note)

    # -- SimilarityMatcher ---------------------------------------------------

    def similar(self, incident: dict[str, Any], top: int = 3) -> list[dict[str, Any]]:
        target = SignatureExtractor.extract(incident)
        own_id = str(incident.get("id", ""))
        matches = []
        for row in self.store.archived_incidents():
            if row["id"] == own_id:
                continue
            try:
                signature = json.loads(row["signature"] or "{}")
            except ValueError:
                continue
            score = similarity(target, signature)
            if score >= MIN_SIMILARITY:
                try:
                    hypothesis = json.loads(row["hypothesis"] or "{}")
                except ValueError:
                    hypothesis = {}
                matches.append({
                    "id": row["id"],
                    "similarity": score,
                    "opened_at": row["opened_at"],
                    "severity": row["severity"],
                    "cause": row["cause"],
                    "diagnosis_then": hypothesis.get("statement", ""),
                    "outcome": row["outcome"] or "not recorded",
                    "outcome_note": row["outcome_note"],
                    "note": "precedent, not conclusion - systems change",
                })
        matches.sort(key=lambda m: -m["similarity"])
        return matches[:top]

    # -- PatternMiner --------------------------------------------------------

    def patterns(self, min_count: int = 2) -> list[dict[str, Any]]:
        """Recurring themes across the archive - the report that turns the
        system from explaining incidents to preventing a category of them."""
        rows = self.store.archived_incidents()
        counts: dict[tuple[str, str], list[str]] = {}
        for row in rows:
            try:
                signature = json.loads(row["signature"] or "{}")
            except ValueError:
                continue
            for kind in ("templates", "detectors"):
                for value in signature.get(kind, []):
                    counts.setdefault((kind, value), []).append(row["id"])
        findings = []
        for (kind, value), incident_ids in counts.items():
            if len(incident_ids) >= min_count:
                findings.append({
                    "kind": kind[:-1], "value": value,
                    "incidents": incident_ids,
                    "text": f"{kind[:-1]} {value} appears in {len(incident_ids)}"
                            f" of {len(rows)} archived incident(s)",
                })
        findings.sort(key=lambda f: -len(f["incidents"]))
        return findings
