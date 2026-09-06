"""Generality tests - the product claim, pinned.

Everything before this suite was validated against one project. Each fixture
here is a realistic sample of a log format the rest of the world writes -
JSON lines, Spring/Java, nginx, syslog, Go logfmt, Rails, Docker-prefixed -
and each assertion is a capability the product claims for ANY project.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aegis.pipeline import Pipeline  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _run(name: str) -> Pipeline:
    pipeline = Pipeline(name, FIXTURES / f"{name}.log", store_root=tempfile.mkdtemp())
    pipeline.run_once()
    pipeline.drain()
    return pipeline


def test_every_format_flows_through_without_crashing():
    for fixture in sorted(FIXTURES.glob("*.log")):
        pipeline = _run(fixture.stem)
        assert pipeline.stats()["events"] > 0, f"{fixture.stem}: nothing came through"
        pipeline.close()


def test_no_format_leaks_pii_into_the_store():
    """The stored templates and examples are what gets persisted and sent to
    a model. No format may carry raw PII through."""
    raw_values = ("jane.doe@example.com", "4111 1111 1111 1111",
                  "203.0.113.9", "192.168.1.20")
    for fixture in sorted(FIXTURES.glob("*.log")):
        pipeline = _run(fixture.stem)
        for template in pipeline.store.templates(limit=100):
            blob = template["pattern"] + template["example"]
            for value in raw_values:
                assert value not in blob, f"{fixture.stem} leaked {value}"
        pipeline.close()


def test_json_logs_keep_their_structured_fields():
    """v1's parser unwrapped {"msg": ...} and threw the rest away - reqId and
    durationMs, the very fields correlation and baselines feed on. A JSON log
    must correlate and measure like any other."""
    pipeline = _run("json-lines")
    stats = pipeline.stats()
    assert stats["extracted"] >= 6, f"reqId not read: {stats['extracted']}"
    pipeline.close()


def test_status_classes_do_not_merge():
    """An access log's lines differ in almost nothing, so plain similarity
    merged 200s and 502s into ONE template - a 502 storm was statistically
    invisible. Success and failure must be separately countable."""
    pipeline = _run("nginx-access")
    patterns = [t["pattern"] for t in pipeline.store.templates(limit=20)]
    assert any("sc2xx" in p for p in patterns), patterns
    assert any("sc5xx" in p for p in patterns), patterns
    pipeline.close()


def test_java_stack_traces_fold_into_one_event():
    """One exception is one event. The Caused-by chain and every at-frame
    belong to the ERROR above them, not to twelve junk templates."""
    pipeline = _run("spring-boot")
    stats = pipeline.stats()
    assert stats["lines_in"] == 15 and stats["events"] == 7
    folded = [e for e in pipeline.hot.recent(50)
              if "SocketTimeoutException" in e.text_redacted]
    assert len(folded) == 1
    assert "Caused by" in folded[0].text_redacted
    assert folded[0].level == "ERROR"


def test_logfmt_correlates_and_measures():
    pipeline = _run("go-logfmt")
    stats = pipeline.stats()
    assert stats["extracted"] >= 6, "requestID= not read"
    pipeline.close()


def test_an_error_is_noticed_in_every_format_that_has_one():
    """Whatever the format, its ERROR line must produce at least one signal -
    novelty needs no baseline, so a short file is no excuse."""
    for name in ("json-lines", "spring-boot", "go-logfmt", "docker-compose"):
        pipeline = _run(name)
        assert pipeline.detect.signals, f"{name}: an error passed unnoticed"
        pipeline.close()


def test_durations_are_measured_where_the_format_carries_them():
    expectations = {"json-lines": 5, "spring-boot": 5, "rails": 3,
                    "go-logfmt": 5, "docker-compose": 3}
    from app.core.baselines import extract_duration
    for name, minimum in expectations.items():
        pipeline = _run(name)
        measured = sum(1 for e in pipeline.hot.recent(500)
                       if extract_duration(e.text_redacted.splitlines()[0]))
        assert measured >= minimum, f"{name}: {measured} < {minimum}"
        pipeline.close()


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
    print(f"\n{'FAILED' if failures else 'All generality tests passed'}"
          f"{f' ({failures} failing)' if failures else ''}")
    sys.exit(1 if failures else 0)
