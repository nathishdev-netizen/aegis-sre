"""Phase 7 verification - the moat: did each run actually achieve anything?

    python3 -m aegis.demo.conformance [logfile] [--no-llm]

Mines the flow spec from the project's own traces, has the FlowSynthesizer
model mark which steps are the run's PURPOSE (C6's only model step - exactly
one call, skipped with --no-llm), then gives every trace a verdict. The case
this exists for: a run that completed cleanly - no errors, normal teardown,
green on every dashboard - and achieved nothing.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.config import load_env  # noqa: E402
load_env()

from aegis.l4_understanding.conformance import ConformanceEngine  # noqa: E402
from aegis.l4_understanding.flowspec import FlowMiner, synthesize_critical  # noqa: E402
from aegis.l7_reasoning.governance import Budget  # noqa: E402
from aegis.l7_reasoning.router import ModelRouter  # noqa: E402
from aegis.pipeline import Pipeline  # noqa: E402
from aegis.l3_storage.store import AEGIS_HOME  # noqa: E402

DEFAULT_LOG = "/Users/nathish/Desktop/Nathish/tt/Demos/voice-gateway/.logs/voice-gateway.log"
VERDICT_ORDER = {"failed": 0, "hollow": 1, "degraded": 2, "achieved": 3, "unknown": 4}


def rule(title: str) -> None:
    print(f"\n{'-' * 74}\n{title}\n{'-' * 74}")


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("--")]
    use_llm = "--no-llm" not in argv
    path = Path(args[0]) if args else Path(DEFAULT_LOG)
    if not path.exists():
        print(f"No such log file: {path}")
        return 1
    project = path.stem

    pipeline = Pipeline(project, path, store_root=tempfile.mkdtemp())
    pipeline.run_once()
    pipeline.drain()
    traces = pipeline.trace_index.traces()

    print("\nAEGIS PHASE 7 - flow conformance (C6+C7). The differentiator.")
    print(f"Source: {path}")

    rule("1. THE SPEC, MINED FROM THIS PROJECT'S OWN TRACES")
    spec = FlowMiner().mine(traces, name=f"{project}-call")
    print(f"  {spec.traces_mined} traces mined -> {len(spec.steps)} steps,"
          f" typical duration p50={spec.expected_duration_s.get('p50')}s"
          f" p95={spec.expected_duration_s.get('p95')}s")
    for step in spec.steps:
        flags = ("required" if step.required else "optional")
        print(f"    {step.presence:>4.0%}  {flags:8}  {step.label[:56]}")

    rule("2. MARKING THE PURPOSE STEPS (C6's only model call)")
    if use_llm:
        router = ModelRouter(budget=Budget(max_calls=1, min_interval_s=0))
        print(f"  {synthesize_critical(spec, router)}")
    else:
        print("  --no-llm: critical flags left for a human to set in the spec file")
    for step in spec.steps:
        if step.critical:
            print(f"    CRITICAL ({step.critical_source}): {step.label[:56]}")

    spec_path = spec.save(AEGIS_HOME / "projects" / project / "flows")
    print(f"\n  spec saved, human-editable: {spec_path}")

    rule("3. EVERY TRACE, JUDGED AGAINST THE SPEC")
    engine = ConformanceEngine(mode="shadow")
    reports = [engine.check(tid, events, spec)[0]
               for tid, events in traces.items() if len(events) >= 4]
    reports.sort(key=lambda r: (VERDICT_ORDER.get(r.verdict, 9), r.trace_id))
    counts: dict[str, int] = {}
    for report in reports:
        counts[report.verdict] = counts.get(report.verdict, 0) + 1
    print("  " + "   ".join(f"{v}: {n}" for v, n in sorted(counts.items())))
    print()
    for report in reports:
        when = traces[report.trace_id][0].ts
        print(f"  {report.verdict.upper():9} {report.trace_id[:8]} @{when}"
              f"  ({report.duration_s:.0f}s)")
        print(f"            {report.reason[:70]}")

    hollow = [r for r in reports if r.verdict == "hollow"]
    if hollow:
        rule("4. WHY THIS MATTERS")
        print(f"  {len(hollow)} call(s) completed with zero errors and NORMAL teardown -")
        print("  green on every dashboard that exists - and achieved nothing. No")
        print("  error-based tool can see these; there is no exception to catch.")
        print("  Conformance sees them because it knows what a call is FOR.")

    rule("HOW TO CHECK THIS YOURSELF")
    print("  Pick a HOLLOW trace id and read its lines in the raw file:")
    print(f"    grep '<trace-id>' {path}")
    print("  You should find a greeting, a prompt, a hangup - and no completed")
    print("  turn. Then edit the spec file above (mark steps critical/not) and")
    print("  rerun: the verdicts must follow YOUR spec, not the model's.")
    print("\n  Conformance ran in SHADOW mode: verdicts recorded, nothing paged.")
    print()
    pipeline.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
