"""Regression tests - a vendor's table becoming log lines.

An MCP tool answers in whatever prose its vendor chose. Detection wants a
timestamp, a name, a duration and an error. Every case here is a way that
translation can quietly go wrong and put a wrong number into a baseline.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aegis.l1_ingestion.tabular import to_log_lines, looks_tabular  # noqa: E402

OPIK = """[list: trace | filters: source = "sdk"]
Found 3 traces (page 1, showing 3 of 3):

id | name | start_time | duration_ms | error_type | total_estimated_cost
t1 | summarize | 2026-09-09T06:09:14Z | 6846 |  | 0.0093075
t2 | process_event | 2026-09-09T06:09:11Z | 231348 |  |
t3 | next_action | 2026-09-09T06:09:09Z | 2772 | Timeout | 0.0002379"""


def test_every_field_in_the_line_came_from_the_table():
    lines = to_log_lines(OPIK)
    assert len(lines) == 3, f"expected 3 rows, got {len(lines)}"
    assert "2026-09-09T06:09:14Z" in lines[0]
    assert "summarize" in lines[0]
    assert "6846ms" in lines[0]
    assert "cost=0.0093075" in lines[0]
    assert "trace_id=t1" in lines[0]


def test_the_header_row_is_not_ingested_as_data():
    """The header opened an incident citing "id | name | start_time" as its
    evidence - a column heading presented as a symptom."""
    for line in to_log_lines(OPIK):
        assert "duration_ms" not in line, "the header became an event"
        assert "error_type" not in line


def test_an_error_column_decides_the_level():
    lines = to_log_lines(OPIK)
    assert lines[0].split()[1] == "INFO"
    assert lines[2].split()[1] == "ERROR", "a named error must raise the level"
    assert "error=Timeout" in lines[2]


def test_a_missing_duration_is_absent_not_invented():
    """A fabricated number here would be averaged into a baseline that
    answers "is this normal?" for every future run."""
    table = ("name | start_time | duration_ms\n"
             "a | 2026-09-09T06:00:00Z | \n")
    line = to_log_lines(table)[0]
    assert "in ms" not in line and "in 0ms" not in line


def test_prose_is_left_alone_rather_than_mangled():
    """Not every tool answers with a table; those replies must fall through
    to being treated as plain text, not silently dropped."""
    assert to_log_lines("No traces found.") == []
    assert looks_tabular("No traces found.") is False


def test_a_vendor_that_names_its_columns_differently_still_reads():
    table = ("operation | timestamp | latency_ms | status_code\n"
             "checkout | 2026-09-09T06:00:00Z | 1200 | 500\n")
    line = to_log_lines(table)[0]
    assert "checkout" in line and "1200ms" in line
    assert line.split()[1] == "ERROR", "status_code 500 is an error column"


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
    print(f"\n{'FAILED' if failures else 'All tabular tests passed'}")
    sys.exit(1 if failures else 0)
