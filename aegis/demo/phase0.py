"""Phase 0 verification - run this to see exactly what Phase 0 built.

    python3 -m aegis.demo.phase0 [path/to/logfile]

Prints, against a real log file: how much PII was removed before anything was
stored, how far the volume funnel actually reduced, and which templates the
fingerprinter discovered. Every number shown is measured from the file given,
not from a fixture.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from aegis.l2_normalization.fingerprint import Fingerprinter  # noqa: E402
from aegis.l2_normalization.redactor import Redactor  # noqa: E402
from app.core.parser import parse_log_line  # noqa: E402

# A demo with no argument uses AEGIS_DEMO_LOG - a product has no
# business hardcoding one machine's paths.
DEFAULT_LOG = os.environ.get("AEGIS_DEMO_LOG", "")


def rule(title: str) -> None:
    print(f"\n{'-' * 74}\n{title}\n{'-' * 74}")


def main(argv: list[str]) -> int:
    path = Path(argv[1]) if len(argv) > 1 else Path(DEFAULT_LOG)
    # A FILE, not just something that exists: Path("") is ".", which exists.
    if not path.is_file():
        if str(path) in ("", "."):
            print("Usage: python3 -m aegis.demo.phase0 <logfile>\n"
                  "No log file given. Make one with:\n"
                  "  python3 demo/log_generator.py --scenario mixed "
                  "--count 120 --file /tmp/demo.log --seed 7")
        else:
            print(f"No such log file: {path}")
        return 1

    fingerprinter = Fingerprinter()
    redactor = Redactor()

    lines = 0
    redacted_lines = 0
    totals: dict[str, int] = {}
    examples: list[tuple[str, str]] = []
    novel_first_seen: list[str] = []

    for raw in path.open(errors="replace"):
        if not raw.strip():
            continue
        lines += 1
        message = parse_log_line(raw.rstrip())["message"]
        result = redactor.redact(message)
        if result.counts:
            redacted_lines += 1
            for kind, n in result.counts.items():
                totals[kind] = totals.get(kind, 0) + n
            if len(examples) < 3:
                examples.append((message, result.text))
        match = fingerprinter.add(result.text)
        if match.is_novel and len(novel_first_seen) < 5:
            novel_first_seen.append(result.text)

    print(f"\nAEGIS PHASE 0 - normalization, redaction, fingerprinting")
    print(f"Source: {path}")

    rule("1. REDACTION - what was removed before anything was stored")
    if totals:
        for kind, n in sorted(totals.items(), key=lambda kv: -kv[1]):
            print(f"  {kind:8} {n:5} value(s) removed")
        print(f"\n  {redacted_lines} of {lines} lines contained sensitive data "
              f"({redacted_lines / lines * 100:.0f}%).")
        print("  These would otherwise have been written to disk and sent to an LLM.")
        print("\n  Before/after:")
        for before, after in examples:
            print(f"    - {before[:70]}")
            print(f"    + {after[:70]}")
    else:
        print("  No sensitive values found in this file.")

    rule("2. FINGERPRINTING - the volume funnel")
    templates = fingerprinter.templates()
    reduction = lines / max(1, len(templates))
    print(f"  {lines:>6} log lines")
    print(f"  {len(templates):>6} distinct templates")
    print(f"  {reduction:>5.1f}x reduction\n")
    print("  Everything above this layer counts templates, not lines. That is what")
    print("  makes detection affordable: one comparison per template, not per line.")

    rule("3. TOP TEMPLATES - what this service actually says")
    for template in templates[:10]:
        print(f"  {template.count:>5}x  {template.pattern[:66]}")

    rule("4. NOVELTY - the cheapest high-value signal")
    print("  The first time a template is ever seen is worth surfacing on its own:")
    print("  a line nobody has seen usually means code took a path nobody has taken.")
    print(f"  First 5 of {len(templates)} novel templates in this file:")
    for text in novel_first_seen:
        print(f"    - {text[:68]}")

    rule("HOW TO CHECK THIS YOURSELF")
    print(f"  Count the raw lines:      wc -l {path}")
    print(f"  Find PII that was caught: grep -o 'from=[0-9]*' {path} | sort -u | head")
    print("  Confirm none survives:    the redaction counts above must be non-zero,")
    print("                            and no <PHONE>/<EMAIL> value appears in output.")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
