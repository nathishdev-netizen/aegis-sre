"""Phase 1 verification - correlation: which lines belong to which call?

    python3 -m aegis.demo.phase1 [path/to/logfile]

Prints, against a real log file: how many events name their own trace key, how
many were safely inferred, how many honestly belong to nothing - and then one
complete call's journey, assembled from lines that mostly never named it.
"""

from __future__ import annotations

import os
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from aegis.l2_normalization.normalizer import Normalizer  # noqa: E402
from aegis.l2_normalization.tracelinker import TraceLinker  # noqa: E402

# A demo with no argument uses AEGIS_DEMO_LOG - a product has no
# business hardcoding one machine's paths.
DEFAULT_LOG = os.environ.get("AEGIS_DEMO_LOG", "")


def rule(title: str) -> None:
    print(f"\n{'-' * 74}\n{title}\n{'-' * 74}")


def main(argv: list[str]) -> int:
    path = Path(argv[1]) if len(argv) > 1 else Path(DEFAULT_LOG)
    # A FILE, not just something that exists. DEFAULT_LOG is empty unless
    # AEGIS_DEMO_LOG is set, and Path("") is ".", which exists - so running
    # any demo with no argument passed this check and then died on
    # IsADirectoryError deep in the collector.
    if not path.is_file():
        if not str(path) or str(path) == ".":
            print(
                "Usage: python3 -m aegis.demo.phase1 <logfile>\n"
                "   or: AEGIS_DEMO_LOG=/path/to/app.log "
                "python3 -m aegis.demo.phase1\n\n"
                "No log file given. Make one with:\n"
                "  python3 demo/log_generator.py --scenario mixed "
                "--count 120 --file /tmp/demo.log --seed 7"
            )
        else:
            print(f"No such log file: {path}")
        return 1

    normalizer = Normalizer(service=path.stem)
    linker = TraceLinker()
    traces: dict[str, list] = defaultdict(list)
    unattributed_heads: dict[str, int] = defaultdict(int)

    for event in normalizer.feed_all(path.open(errors="replace")):
        linker.link(event)
        if event.trace_id:
            traces[event.trace_id].append(event)
        else:
            unattributed_heads[event.text_redacted.splitlines()[0][:60]] += 1

    total = normalizer.events_out
    print(f"\nAEGIS PHASE 1 - correlation")
    print(f"Source: {path}")

    rule("1. COVERAGE - evidence, assumption, and honest absence")
    print(f"  {total:>5} events")
    print(f"  {linker.extracted:>5} extracted - the line names its key. Evidence.")
    print(f"  {linker.inferred:>5} inferred  - no key, but exactly one session open. Assumption.")
    print(f"  {linker.unattributed:>5} none      - zero or several sessions open. No guess made.")
    with_ = (linker.extracted + linker.inferred) / max(1, total) * 100
    without = linker.extracted / max(1, total) * 100
    print(f"\n  Coverage without inference: {without:.0f}%.  With it: {with_:.0f}%.")
    print("  An inferred link is never presented as evidence: every event carries")
    print("  its correlation_basis, and downstream layers must honour it.")

    rule("2. TRACES - one request, assembled")
    print(f"  {len(traces)} distinct traces found.\n")
    for trace_id, events in sorted(traces.items(), key=lambda kv: -len(kv[1]))[:5]:
        span = f"{events[0].ts[-8:]} - {events[-1].ts[-8:]}" if events[0].ts else ""
        print(f"  {trace_id[:16]}...  {len(events):3} events  {span}")

    biggest = max(traces.values(), key=len, default=[])
    if biggest:
        print(f"\n  The largest, line by line ([E]=extracted, [i]=inferred):")
        shown = biggest[:8] + ([...] if len(biggest) > 12 else []) + biggest[-4:] \
            if len(biggest) > 12 else biggest
        for event in shown:
            if event is ...:
                print(f"       ... {len(biggest) - 12} more ...")
                continue
            mark = "E" if event.correlation_basis == "extracted" else "i"
            print(f"    [{mark}] {event.ts[-8:]}  {event.text_redacted.splitlines()[0][:58]}")

    rule("3. WHAT WAS LEFT UNATTRIBUTED - and why that is correct")
    print("  Lines with no key while zero or several sessions were open. For this")
    print("  service that is startup, warmup and shutdown - work owned by no call:")
    for head, count in sorted(unattributed_heads.items(), key=lambda kv: -kv[1])[:5]:
        print(f"    {count:4}x  {head}")

    rule("HOW TO CHECK THIS YOURSELF")
    print(f"  Pick a call id from section 2, then:")
    print(f"    grep 'call=<that id>' {path} | wc -l     # lines naming it")
    print(f"  Compare with the trace's event count above - the difference is what")
    print(f"  inference recovered. Then read the journey and judge whether any")
    print(f"  [i] line plausibly belongs to a different call.")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
