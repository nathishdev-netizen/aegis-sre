"""Phase 8 verification - incident memory: the system gets better with age.

    python3 -m aegis.demo.memory [logfile] [--no-llm]

Runs the pipeline (resolved incidents archive automatically), then replays
the doc's compounding example on real data: when the second identity failure
of the day is investigated, memory hands the investigator the first one -
with its outcome - before any hypothesis is formed. One model call at the
end shows the explanation WITH the precedent in context (--no-llm skips it).
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.config import load_env  # noqa: E402
load_env()

from aegis.l7_reasoning.explainer import Explainer  # noqa: E402
from aegis.l7_reasoning.governance import Budget  # noqa: E402
from aegis.l7_reasoning.router import ModelRouter  # noqa: E402
from aegis.pipeline import Pipeline  # noqa: E402

DEFAULT_LOG = "/Users/nathish/Desktop/Nathish/tt/Demos/voice-gateway/.logs/voice-gateway.log"


def rule(title: str) -> None:
    print(f"\n{'-' * 74}\n{title}\n{'-' * 74}")


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("--")]
    use_llm = "--no-llm" not in argv
    path = Path(args[0]) if args else Path(DEFAULT_LOG)
    if not path.exists():
        print(f"No such log file: {path}")
        return 1

    pipeline = Pipeline(path.stem, path, store_root=tempfile.mkdtemp())
    pipeline.run_once()
    pipeline.drain()

    print("\nAEGIS PHASE 8 - incident memory (C15)")
    print(f"Source: {path}")

    rule("1. THE ARCHIVE - resolved incidents became precedents, automatically")
    archived = pipeline.store.archived_incidents()
    print(f"  {len(archived)} incident(s) archived on resolution. Nothing was")
    print("  configured; resolving IS archiving.")

    rule("2. THE MATCH - the doc's compounding example, on real data")
    ordered = sorted(pipeline.incidents.incidents, key=lambda i: i.opened_at)
    target, matches = None, []
    for incident in reversed(ordered):
        found = pipeline.memory.similar(incident.to_dict())
        if found:
            target, matches = incident, found
            break
    if target is None:
        print("  no incident had a qualifying precedent in this file")
    else:
        # Label the best match's outcome the way an engineer would have.
        best = matches[0]
        pipeline.memory.record_outcome(
            best["id"], "worked",
            "restarted the affected component; recovered within minutes")
        matches = pipeline.memory.similar(target.to_dict())
        print(f"  investigating {target.id} (opened @{target.opened_at}),")
        print(f"  memory reports, BEFORE any hypothesis is formed:\n")
        for match in matches:
            print(f"    similar past incident: {match['id']}"
                  f" (similarity {match['similarity']})")
            print(f"      cause then:     {match['cause'][:60]}")
            print(f"      outcome:        {match['outcome']}"
                  + (f" - {match['outcome_note']}" if match['outcome_note'] else ""))
            print(f"      ({match['note']})")

    rule("3. THE PATTERN MINER - from explaining incidents to preventing them")
    patterns = pipeline.memory.patterns()
    if patterns:
        for finding in patterns[:4]:
            print(f"  {finding['text']}")
        print("\n  A theme recurring across incidents is a candidate for a fix at")
        print("  the source, not another explanation.")
    else:
        print("  no recurring themes yet - the archive is young")

    if use_llm and target is not None:
        rule("4. THE EXPLANATION, WITH MEMORY IN CONTEXT (1 model call)")
        router = ModelRouter(budget=Budget(max_calls=1, min_interval_s=0))
        hypothesis = Explainer(router).explain(target.to_dict(), precedents=matches)
        if hypothesis is None:
            print("  model unavailable - which changes nothing above this line")
        else:
            mark = "grounded" if hypothesis["verified"] else "UNVERIFIED"
            print(f"  confidence={hypothesis['confidence']} ({mark}),"
                  f" precedents considered: {hypothesis['precedents_considered']}")
            print(f"  {hypothesis['statement']}")

    rule("HOW TO CHECK THIS YOURSELF")
    print("  The archive is plain SQLite - read it directly:")
    print(f"    sqlite3 '{pipeline.store.path}' 'SELECT id, severity, cause"
          " FROM incident_archive'")
    print("  Verify the match is real: both incidents' cause lines are in the raw")
    print("  file hours apart. Then record a WRONG outcome and watch it surface")
    print("  in the match too - a remembered wrong diagnosis is protection.")
    print()
    pipeline.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
