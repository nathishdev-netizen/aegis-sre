"""Gap reports: what should have been logged, so the next incident explains.

The failure these prevent is a gap report nobody can act on. "Add more logging"
is an opinion; "add a line at vector_agent.py:55" is a gap report.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.gaps import generate  # noqa: E402

_CODE = {
    "log_statements": [
        {"text": "[vector_agent] Tool chosen: search_content (hybrid + rerank) sources=",
         "level": "INFO", "function": "search_content",
         "file": "agents/vector_agent.py", "line": 54},
        {"text": "[api] /chat request received thread=", "level": "INFO",
         "function": "chat", "file": "api.py", "line": 322},
        {"text": "[api] /chat stream closed thread=", "level": "INFO",
         "function": "chat", "file": "api.py", "line": 360},
    ],
    "external_calls": [
        {"kind": "http", "target": "https://api.openai.com", "function": "chat",
         "file": "api.py", "line": 340, "guarded": True, "has_timeout": False},
    ],
}


def test_the_gap_is_where_the_trail_actually_ends():
    """Today's real failure. The process died inside search_and_rerank and the
    last thing anyone knew was line 54. A gap report has to name line 55 - the
    call that says nothing about itself - not a general opinion about logging."""
    report = generate(
        "why did this run stop without finishing?",
        ["[vector_agent] Tool chosen: search_content (hybrid + rerank) sources=['uws']"],
        _CODE)
    assert "agents/vector_agent.py:54" in report.detail, report.detail
    assert report.gaps, "no gap named for a run that went silent"
    gap = report.gaps[0]
    assert gap.file == "agents/vector_agent.py"
    assert gap.line == 55, gap.line


def test_a_function_that_logs_its_own_exit_is_not_a_gap():
    """chat() logs both entry and exit, so a run that stops inside it is
    already diagnosable. Reporting a gap here would be noise, and noise is
    what makes people stop reading these."""
    report = generate(
        "why did this run stop?",
        ["[api] /chat request received thread='x'"],
        _CODE)
    exit_gaps = [g for g in report.gaps
                 if "last thing logged" in g.why]
    assert not exit_gaps, [g.to_dict() for g in exit_gaps]


def test_an_untimed_external_call_is_named_as_its_own_gap():
    """A call with no timeout cannot fail loudly - it waits forever, which is
    the difference between an incident with evidence and one with none."""
    report = generate(
        "why did this run stop?",
        ["[api] /chat request received thread='x'"],
        _CODE)
    timeouts = [g for g in report.gaps if "no timeout" in g.why]
    assert timeouts, [g.to_dict() for g in report.gaps]
    assert timeouts[0].line == 340


def test_without_the_code_map_it_says_so_instead_of_guessing():
    """A gap report that cannot name a file and a line is an opinion. It has to
    say Analyze first, not invent advice that sounds like analysis."""
    report = generate("why did this fail?", ["something broke"], None)
    assert report.gaps == []
    assert "Analyze this project first" in report.detail


def test_evidence_from_a_library_is_not_blamed_on_this_repo():
    """A line this project never wrote cannot point at a line in it. Saying so
    is honest; picking the nearest match would be a fabricated location."""
    report = generate(
        "why did this fail?",
        ["urllib3.connectionpool: Retrying (Retry(total=2)) after connection broken"],
        _CODE)
    assert report.gaps == []
    assert "does not match any log statement" in report.detail


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
    print(f"\n{'FAILED' if failures else 'All gap tests passed'}"
          f"{f' ({failures} failing)' if failures else ''}")
    sys.exit(1 if failures else 0)
