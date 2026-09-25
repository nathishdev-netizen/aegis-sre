"""Phase 2 verification - detection: what deserved attention, at zero model cost.

    python3 -m aegis.demo.phase2 [path/to/logfile] [project]

Every signal below was produced by arithmetic - counting, ratios, medians.
No model was called. That is the doc's rule P2: a threshold decides WHETHER to
speak; a model (later, C10) decides what to say.
"""

from __future__ import annotations

import sys
import tempfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from aegis.contracts.events import Event  # noqa: E402
from aegis.l5_detection.detectors import DetectionEngine  # noqa: E402
from aegis.pipeline import Pipeline  # noqa: E402

DEFAULT_LOG = "/Users/nathish/Desktop/Nathish/tt/Demos/voice-gateway/.logs/voice-gateway.log"


def rule(title: str) -> None:
    print(f"\n{'-' * 74}\n{title}\n{'-' * 74}")


def main(argv: list[str]) -> int:
    path = Path(argv[1]) if len(argv) > 1 else Path(DEFAULT_LOG)
    if not path.exists():
        print(f"No such log file: {path}")
        return 1
    project = argv[2] if len(argv) > 2 else path.stem

    pipeline = Pipeline(project, path, store_root=tempfile.mkdtemp())
    pipeline.run_once()
    pipeline.drain()
    stats = pipeline.stats()
    signals = pipeline.detect.signals

    print(f"\nAEGIS PHASE 2 - detection (C8). Zero model calls.")
    print(f"Source: {path}")

    rule("1. THE FUNNEL, NOW WITH ITS POINT")
    important = [s for s in signals if s.severity in ("P1", "P2", "P3")]
    print(f"  {stats['lines_in']:>6} log lines")
    print(f"  {stats['events']:>6} events")
    print(f"  {stats['templates']:>6} templates")
    print(f"  {len(signals):>6} signals")
    print(f"  {len(important):>6} worth a person's attention (P1-P3)")
    print("\n  Volume shrinks at every layer. The doc's rule: if it is not")
    print("  shrinking by an order of magnitude per layer, a layer is broken.")

    rule("2. WHAT THE DETECTORS FOUND")
    by_detector = Counter(s.detector for s in signals)
    by_severity = Counter(s.severity for s in signals)
    for detector, count in by_detector.most_common():
        print(f"  {detector:18} {count:3} signal(s)")
    print(f"  severity: {dict(sorted(by_severity.items()))}")

    rule("3. THE P1-P3 SIGNALS, EACH WITH ITS EVIDENCE")
    for signal in important:
        line = signal.evidence[0][:64] if signal.evidence else ""
        detail = (f"observed={signal.observed:g} vs baseline={signal.baseline:g}"
                  if signal.detector != "NoveltyDetector" else "first occurrence ever")
        print(f"  [{signal.severity}] {signal.detector:16} @{signal.started_at:>8}  {detail}")
        print(f"        {line}")
    quiet = len(signals) - len(important)
    if quiet:
        print(f"\n  (+{quiet} P4 first-occurrence notes, kept peripheral on purpose -")
        print("   ~97% of alerts need no immediate action, and frequent alerts make")
        print("   people skim the important ones.)")

    rule("4. THE DOC'S WORKED EXAMPLE - hysteresis on a burst")
    engine = DetectionEngine(service="shopflow")
    fires = 0

    def feed(text: str, t: int, template: str) -> None:
        nonlocal fires
        ts = f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}"
        event = Event(id="E", ts=ts, service="inventory-svc", level="ERROR",
                      text_redacted=text, template_id=template)
        fires += sum(1 for s in engine.observe(event) if s.detector == "RateSpike")

    # Ten minutes of a quiet baseline, then the doc's 400-line burst.
    for minute in range(10):
        feed("reserve failed for sku <STR>: insufficient stock", minute * 60, "T-0912")
    for i in range(400):
        feed("reserve failed for sku <STR>: insufficient stock", 660 + i // 40, "T-0912")
    print(f"  400 identical error lines in 10 seconds -> {fires} RateSpike signal(s).")
    print("  One incident-worthy signal, not four hundred alerts. Hysteresis holds")
    print("  it FIRING until the rate stays low; there is no flapping to page on.")

    rule("HOW TO CHECK THIS YOURSELF")
    print("  Pick any P2 above and grep its evidence line in the raw file - the")
    print("  numbers in section 3 must be reproducible from the file alone:")
    print(f"    grep -n 'Lookup FAILED' {path} | head")
    print("  A signal that cites evidence you cannot find in the file is a bug.")
    print()
    pipeline.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
