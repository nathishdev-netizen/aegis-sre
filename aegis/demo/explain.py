"""Phase 6 verification - one incident, explained by a governed model.

    python3 -m aegis.demo.explain [logfile] [--n 1] [--compare]

Runs the whole pipeline (all deterministic), then explains the N richest
incidents. The budget is hard-capped at 4 calls no matter what is asked;
--compare spends exactly one extra call on OpenAI for the first incident so
the two providers can be judged side by side.

Every hypothesis printed shows whether its citations survived grounding.
"""

from __future__ import annotations

import os
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

# A demo with no argument uses AEGIS_DEMO_LOG - a product has no
# business hardcoding one machine's paths.
DEFAULT_LOG = os.environ.get("AEGIS_DEMO_LOG", "")


def show(tag: str, hypothesis: dict | None) -> None:
    if hypothesis is None:
        print(f"  [{tag}] unavailable (no key, budget spent, or call failed) - "
              "everything below this layer already worked without it")
        return
    mark = "grounded" if hypothesis["verified"] else "UNVERIFIED - citations were fabricated"
    print(f"  [{tag}] confidence={hypothesis['confidence']} ({mark})")
    print(f"    {hypothesis['statement']}")
    for ref in hypothesis["evidence_refs"][:3]:
        print(f"      evidence: {ref[:70]}")
    if hypothesis["dropped_refs"]:
        print(f"      ({hypothesis['dropped_refs']} fabricated citation(s) dropped)")
    if hypothesis["immediate_action"]:
        print(f"    now:   {hypothesis['immediate_action'][:76]}")
    if hypothesis["durable_fix"]:
        print(f"    later: {hypothesis['durable_fix'][:76]}")


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("--")]
    compare = "--compare" in argv
    count = 1
    if "--n" in argv:
        count = max(1, min(3, int(argv[argv.index("--n") + 1])))
    path = Path(args[0]) if args else Path(DEFAULT_LOG)
    if not path.exists():
        print(f"No such log file: {path}")
        return 1

    pipeline = Pipeline(path.stem, path, store_root=tempfile.mkdtemp())
    pipeline.run_once()
    pipeline.drain()

    router = ModelRouter(budget=Budget(max_calls=4, min_interval_s=1.0))
    explainer = Explainer(router)

    print("\nAEGIS PHASE 6 - reasoning (C10) under governance (C16)")
    print(f"Source: {path}")
    print(f"Budget this run: {router.budget.max_calls} model calls, hard-capped.")

    richest = sorted(pipeline.incidents.incidents,
                     key=lambda i: len(i.members), reverse=True)[:count]
    for incident in richest:
        card = incident.to_dict()
        print(f"\n{'-' * 74}\n{incident.id} [{incident.severity}]"
              f" {len(incident.members)} member(s), opened @{incident.opened_at}"
              f"\n{'-' * 74}")
        show("groq", explainer.explain(card))
        if compare and incident is richest[0]:
            show("openai", explainer.explain(card, provider="openai",
                                             model="gpt-4o-mini"))

    print(f"\ncalls actually made: {router.budget.calls_made}"
          f"  (audit: ~/.aegis/audit.jsonl)")
    pipeline.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
