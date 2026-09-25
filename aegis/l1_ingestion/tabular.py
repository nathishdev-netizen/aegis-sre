"""Turning a vendor's table into lines the rest of the platform understands.

MCP tools answer in prose. Opik returns a pipe-delimited table; another
vendor returns something else. Downstream, nothing knows or should know
that: detectors want a timestamp, a name, a duration and an error, in the
shape v1's parser already reads off a log line.

So this is a translator, not an interpreter. It reads the header row to
learn which column is which, and re-emits each data row as a conventional
log line:

    2026-09-09T06:09:14Z INFO summarize completed in 6846ms cost=0.0093075

Every field in that line came from the table. Nothing is inferred, and a
row missing a duration simply has no duration - inventing one would put a
fabricated number into a baseline that answers "is this normal?".
"""

from __future__ import annotations

import re
from typing import Any

# Column names vendors actually use, mapped onto the four things detection
# needs. Extending this is how a new vendor's table becomes readable.
_TIME = ("start_time", "timestamp", "time", "created_at", "start")
_NAME = ("name", "operation", "span_name", "endpoint", "tool")
_DURATION = ("duration_ms", "duration", "latency_ms", "elapsed_ms", "took_ms")
_ERROR = ("error_type", "error", "status_code", "level", "exception")
_COST = ("total_estimated_cost", "cost", "total_cost", "estimated_cost")
_ID = ("id", "trace_id", "span_id", "request_id")

_SEPARATOR = re.compile(r"\s*\|\s*")


def _index_of(headers: list[str], candidates: tuple[str, ...]) -> int:
    for wanted in candidates:
        if wanted in headers:
            return headers.index(wanted)
    return -1


def looks_tabular(text: str) -> bool:
    """Whether this reply is a delimited table with a header we can read."""
    return header_of(text) is not None


def header_of(text: str) -> list[str] | None:
    """The header row's columns, if one of the first lines is a header."""
    for line in text.splitlines()[:8]:
        if "|" not in line:
            continue
        columns = [c.strip().lower() for c in _SEPARATOR.split(line.strip())]
        if len(columns) < 2:
            continue
        # A header names at least a time or a duration; a row of data does
        # not, so this distinguishes the two without guessing by position.
        if _index_of(columns, _TIME) >= 0 or _index_of(columns, _DURATION) >= 0:
            return columns
    return None


# Fields carried through as key=value when a vendor's detail record has
# them. Deliberately a DENYLIST of noise rather than an allowlist of four
# names: an observability backend knows things we have not thought to ask
# for, and hardcoding a short list guarantees the next question ("which
# model?", "how many spans?", "was it cached?") is unanswerable from data
# already fetched. Anything not listed here is kept.

# Values that are objects or long prose are not measurements; keeping them
# would put a paragraph of user input into a log line the model then reads.




# Fields carried through as key=value when a vendor's detail record has
# them. Deliberately a DENYLIST of noise rather than an allowlist of four
# names: an observability backend knows things we have not thought to ask
# for, and hardcoding a short list guarantees the next question ("which
# model?", "how many spans?", "was it cached?") is unanswerable from data
# already fetched. Anything not listed here is kept.
_SKIP_FIELDS = frozenset({
    "project_id", "created_at", "last_updated_at", "created_by",
    "last_updated_by", "visibility_mode", "id", "trace_id", "span_id",
    "parent_span_id", "start_time", "end_time", "input", "output",
    "metadata", "tags", "feedback_scores", "comments", "usage",
})

# Values that are objects or long prose are not measurements; keeping them
# would put a paragraph of user input into a log line the model then reads.
_MAX_VALUE_LEN = 60


def flatten_fields(record: dict, prefix: str = "") -> list[str]:
    """Every scalar in a vendor record, as key=value pairs.

    One level of nesting is followed (usage.total_tokens), because that is
    where vendors put the numbers that matter. Objects deeper than that are
    skipped rather than flattened into noise.
    """
    pairs: list[str] = []
    for key, value in (record or {}).items():
        if key in _SKIP_FIELDS and not prefix:
            continue
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            if not prefix:                      # one level only
                pairs.extend(flatten_fields(value, prefix=""))
            continue
        if isinstance(value, (list, tuple)):
            flat = ",".join(str(v) for v in value if not isinstance(v, (dict, list)))
            if flat and len(flat) <= _MAX_VALUE_LEN:
                pairs.append(f"{name}={flat}")
            continue
        if value is None or value == "":
            continue
        text = str(value)
        if len(text) > _MAX_VALUE_LEN or "\\n" in text:
            continue
        pairs.append(f"{name}={text.replace(' ', '_')}")
    return pairs


def to_log_lines(text: str) -> list[str]:
    """Re-emit a vendor's table as conventional log lines.

    Returns [] when the reply is not a table this can read, so the caller
    falls back to treating it as plain text rather than losing it.
    """
    headers = header_of(text)
    if headers is None:
        return []

    time_at = _index_of(headers, _TIME)
    name_at = _index_of(headers, _NAME)
    duration_at = _index_of(headers, _DURATION)
    error_at = _index_of(headers, _ERROR)
    cost_at = _index_of(headers, _COST)
    id_at = _index_of(headers, _ID)

    lines: list[str] = []
    seen_header = False
    for raw in text.splitlines():
        if "|" not in raw:
            continue
        cells = [c.strip() for c in _SEPARATOR.split(raw.strip())]
        if not seen_header:
            # The header itself is not data; skip exactly one of them.
            if [c.lower() for c in cells] == headers:
                seen_header = True
            continue
        if len(cells) < len(headers):
            continue

        def cell(index: int) -> str:
            return cells[index] if 0 <= index < len(cells) else ""

        name = cell(name_at) or "operation"
        error = cell(error_at)
        # An error column that names a failure is what makes a line ERROR.
        # An empty one is not a success claim, it is simply no error.
        level = "ERROR" if error and error.lower() not in ("none", "null") else "INFO"

        parts = [cell(time_at), level, name]
        parts.append("failed" if level == "ERROR" else "completed")
        duration = cell(duration_at)
        if duration:
            # Keep the unit the column declared rather than assuming ms.
            unit = "ms" if "ms" in headers[duration_at] else "s"
            parts.append(f"in {duration}{unit}")
        if error and level == "ERROR":
            parts.append(f"error={error}")
        cost = cell(cost_at)
        if cost:
            parts.append(f"cost={cost}")
        identifier = cell(id_at)
        if identifier:
            # The correlation key: this is what ties spans of one trace
            # together once they arrive as separate lines.
            parts.append(f"trace_id={identifier}")
        lines.append(" ".join(p for p in parts if p))
    return lines
