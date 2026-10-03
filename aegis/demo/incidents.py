"""Phase 4 verification - incidents: what a person actually works with.

    python3 -m aegis.demo.incidents [path/to/logfile] [project]

Prints the doc's INCIDENT-card view: members with the grouping rules that
matched, the ranked cause with its stated basis, blast radius, timeline.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from aegis.pipeline import Pipeline  # noqa: E402

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
                "Usage: python3 -m aegis.demo.incidents <logfile>\n"
                "   or: AEGIS_DEMO_LOG=/path/to/app.log "
                "python3 -m aegis.demo.incidents\n\n"
                "No log file given. Make one with:\n"
                "  python3 demo/log_generator.py --scenario mixed "
                "--count 120 --file /tmp/demo.log --seed 7"
            )
        else:
            print(f"No such log file: {path}")
        return 1
    project = argv[2] if len(argv) > 2 else path.stem

    pipeline = Pipeline(project, path, store_root=tempfile.mkdtemp())
    pipeline.run_once()
    pipeline.drain()
    stats = pipeline.stats()

    print("\nAEGIS PHASE 4 - incidents (C9). One incident, not forty alerts.")
    print(f"Source: {path}")

    rule("1. THE FULL FUNNEL")
    print(f"  {stats['lines_in']:>6} log lines")
    print(f"  {stats['events']:>6} events")
    print(f"  {stats['templates']:>6} templates")
    print(f"  {stats['signals']:>6} signals")
    print(f"  {stats['incidents']:>6} incidents   ({stats['by_status']})")
    print(f"  {stats['notes']:>6} quiet P4 notes - visible, paging nobody")

    rule("2. EVERY INCIDENT, IN ONE LINE EACH")
    for incident in pipeline.incidents.incidents:
        cause = next((m for m in incident.members if m.id == incident.ranked_cause), None)
        head = cause.evidence[0][:52] if cause and cause.evidence else ""
        print(f"  {incident.id:7} [{incident.severity}] {incident.status:8}"
              f" @{incident.opened_at:>8}  x{len(incident.members):<3} {head}")

    rule("3. THE RICHEST INCIDENT, THE DOC'S CARD FORMAT")
    richest = max(pipeline.incidents.incidents, key=lambda i: len(i.members), default=None)
    if richest:
        card = richest.to_dict()
        print(f"  {richest.id}")
        print(f"    status:   {card['status']}")
        print(f"    severity: {card['severity']}")
        print(f"    opened:   {card['opened_at']}"
              + (f"    resolved: {card['resolved_at']}" if card['resolved_at'] else ""))
        radius = card["blast_radius"]
        print(f"    blast:    {radius['signals']} signals"
              f" · {radius['traces']} trace(s) · {radius['services']} service(s)")
        print(f"\n    MEMBERS ({len(richest.members)} grouped)")
        for member in richest.members:
            mark = " ◄ RANKED CAUSE" if member.id == richest.ranked_cause else ""
            line = member.evidence[0][:54] if member.evidence else ""
            print(f"      {member.detector:16} @{member.started_at:>8}  {line}{mark}")
        print("\n    WHY THE CAUSE IS RANKED FIRST")
        for reason in card["cause_why"]:
            print(f"      · {reason}")
        print("\n    TIMELINE (append-only)")
        for entry in card["timeline"][:8]:
            grouped = f"  [{','.join(entry['grouped_by'])}]" if entry.get("grouped_by") else ""
            print(f"      {entry['ts']:>8}  {entry['kind']:9} {entry['detail'][:48]}{grouped}")

    rule("HOW TO CHECK THIS YOURSELF")
    print("  Pick an incident's opening time and read the raw file around it -")
    print("  every member's evidence line must exist there, and the members must")
    print("  plausibly be one story. An incident grouping two unrelated problems")
    print("  is a bug worth reporting; so is one problem split across two.")
    print()
    pipeline.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
