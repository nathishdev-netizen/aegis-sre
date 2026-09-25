"""Phase 9 verification - remediation: reproduce first, patch minimally, draft only.

    python3 -m aegis.demo.remediate [repo_path]

Runs the doc's C13 flow live against the fixture app (a real, runnable bug of
the doc's own worked-example shape): the model writes a reproducer, the
sandbox proves it FAILS, the model writes a minimal patch, the sandbox proves
the reproducer now PASSES, and the result is a draft proposal bundle. The
target repo is never written. Two model calls, budget-capped at three.
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.config import load_env  # noqa: E402
load_env()

from aegis.l7_reasoning.governance import Budget  # noqa: E402
from aegis.l7_reasoning.router import ModelRouter  # noqa: E402
from aegis.l8_action.remediate import RemediationAgent  # noqa: E402

FIXTURE = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "sample_app"


def digest(repo: Path) -> str:
    parts = [p.read_bytes() for p in sorted(repo.rglob("*")) if p.is_file()]
    return hashlib.sha256(b"".join(parts)).hexdigest()


def main(argv: list[str]) -> int:
    repo = Path(argv[1]).expanduser() if len(argv) > 1 else FIXTURE
    before = digest(repo)

    incident = {
        "id": "INC-DEMO",
        "evidence": ["ERROR worker: pool exhausted: 1 connections active"],
    }
    hypothesis = {"statement": "POOL_MAXSIZE was tuned down to 1; under normal "
                               "concurrency the pool saturates immediately"}

    print("\nAEGIS PHASE 9 - remediation (C13, tier T1)")
    print(f"target repo (read-only): {repo}")

    router = ModelRouter(budget=Budget(max_calls=3, min_interval_s=1.0))
    agent = RemediationAgent(router, repo, tier="T1", project="sample-app")
    proposal = agent.propose(incident, hypothesis)

    print(f"\nstatus : {proposal.status.upper()}")
    print(f"detail : {proposal.detail}")
    if proposal.locations:
        print("mapped :")
        for loc in proposal.locations[:3]:
            print(f"  {loc['file']}:{loc['line']}  {loc['source'][:60]}")
    if proposal.test_before:
        print(f"reproducer before patch: exit {proposal.test_before['exit_code']}"
              " (non-zero = fails, as required)")
    if proposal.test_after:
        print(f"reproducer after patch : exit {proposal.test_after['exit_code']}"
              " (zero = fixed, proven)")
    if proposal.patch:
        print("\ndiff:")
        for line in proposal.patch.splitlines()[:12]:
            print("  " + line)
    print(f"\nbundle : {proposal.bundle_path}")
    print(f"model calls spent: {router.budget.calls_made}")

    after = digest(repo)
    print(f"target repo untouched: {'YES' if before == after else 'NO - BUG'}")
    print("\nAegis never merges. Applying the patch is your decision:")
    print(f"  cd {repo} && patch -p1 < <bundle dir>/fix.patch")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
