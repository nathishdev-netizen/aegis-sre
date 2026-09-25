"""Learn a source's shape from its own output.

The agent must not assume a project's pipeline. A chatbot, an ETL job and a CLI have
nothing in common except that each repeats its own vocabulary, so the stages are
discovered from the lines themselves rather than declared in a config file.

Two passes, both pure so they can be tested without threads or a server:

  detect_format()      what shape are these lines - loguru, JSON, uvicorn, plain?
  extract_components() which recurring names are this project's pipeline stages?

Neither invents anything. When the evidence is thin they return "unknown" and an empty
list, and the caller reports that rather than drawing a diagram of imagined stages.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from app.core.parser import strip_ansi


# Enough lines to see a pattern repeat, few enough to decide quickly.
MIN_LINES_FOR_FORMAT = 8
MIN_LINES_FOR_COMPONENTS = 25

# A component must recur - a name seen once is a noun, not a stage.
MIN_OCCURRENCES = 2
MAX_COMPONENTS = 12

# Words that look like components but never are: language builtins and type names that
# appear in bracketed annotations, plus log levels themselves.
NOT_COMPONENTS = frozenset({
    # Debug scaffolding, not pipeline stages: header/payload dumps tag every line the
    # same way a real component does, and would otherwise dominate the diagram.
    "hdr", "header", "headers", "body", "payload", "req", "res", "request", "response",
    "dump", "raw", "in", "out",
    "info", "warn", "warning", "error", "debug", "trace", "critical", "fatal", "success",
    "dict", "list", "str", "int", "float", "bool", "any", "none", "null", "true", "false",
    "object", "array", "string", "number", "type", "value", "field", "key", "item",
    "optional", "union", "tuple", "set", "map", "self", "cls", "args", "kwargs",
})


@dataclass
class SourceProfile:
    """What has been learned about a source so far."""
    line_format: str = "unknown"          # loguru | json | uvicorn | bracketed | plain
    timestamp_style: str = "unknown"      # iso | datetime | time | none
    components: list[str] = field(default_factory=list)
    component_counts: dict[str, int] = field(default_factory=dict)
    has_continuations: bool = False
    lines_seen: int = 0
    confident: bool = False               # enough evidence to publish a diagram
    method: str = "none"                  # how components were found

    def as_dict(self) -> dict[str, Any]:
        return {
            "line_format": self.line_format,
            "timestamp_style": self.timestamp_style,
            "components": list(self.components),
            "component_counts": dict(self.component_counts),
            "has_continuations": self.has_continuations,
            "lines_seen": self.lines_seen,
            "confident": self.confident,
            "method": self.method,
        }

    def describe(self) -> str:
        """One line for the UI - honest about how far along learning is."""
        if self.lines_seen < MIN_LINES_FOR_FORMAT:
            return f"Reading source... {self.lines_seen} lines"
        if not self.components:
            if self.lines_seen < MIN_LINES_FOR_COMPONENTS:
                return f"Detected {self.line_format} format... {self.lines_seen} lines"
            return f"Detected {self.line_format} format - no component tags found"
        names = ", ".join(self.components[:4])
        more = f" +{len(self.components) - 4}" if len(self.components) > 4 else ""
        return f"Found {len(self.components)} components: {names}{more}"


# --- format detection ---------------------------------------------------------

# "2026-08-28 16:00:18.807 | INFO | api:lifespan:44 - message"
LOGURU = re.compile(r"\|\s*(?:INFO|WARN|WARNING|ERROR|DEBUG|TRACE|CRITICAL|SUCCESS)\s*\|\s*[\w.]+:", re.I)
# "INFO:     Uvicorn running on ..."
UVICORN = re.compile(r"^(?:INFO|WARNING|ERROR|DEBUG|CRITICAL):\s{2,}")
# "[component] message" anywhere in the line
BRACKETED = re.compile(r"\[[a-zA-Z][\w.\-/]{1,30}\]")

ISO_TS = re.compile(r"^\s*\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
DATETIME_TS = re.compile(r"^\s*\d{4}-\d{2}-\d{2}\s+\d{1,2}:\d{2}:\d{2}")
TIME_TS = re.compile(r"^\s*\d{1,2}:\d{2}:\d{2}")


def _is_json_line(line: str) -> bool:
    text = line.strip()
    if not (text.startswith("{") and text.endswith("}")):
        return False
    try:
        return isinstance(json.loads(text), dict)
    except (json.JSONDecodeError, ValueError):
        return False


def detect_format(lines: list[str]) -> tuple[str, str]:
    """Return (line_format, timestamp_style) for a sample of raw lines.

    Decided by majority rather than by the first line, since real logs interleave
    framework output with application output.
    """
    clean = [strip_ansi(l) for l in lines if l and l.strip()]
    if not clean:
        return "unknown", "unknown"

    fmt = Counter()
    for line in clean:
        if _is_json_line(line):
            fmt["json"] += 1
        elif LOGURU.search(line):
            fmt["loguru"] += 1
        elif UVICORN.match(line):
            fmt["uvicorn"] += 1
        elif BRACKETED.search(line):
            fmt["bracketed"] += 1
        else:
            fmt["plain"] += 1

    # "plain" only wins if nothing more specific showed up at all: a structured format
    # appearing in a meaningful minority still tells us more than the plain majority.
    structured = [(name, n) for name, n in fmt.items() if name != "plain"]
    if structured:
        line_format = max(structured, key=lambda item: item[1])[0]
    else:
        line_format = "plain"

    ts = Counter()
    for line in clean:
        if ISO_TS.match(line):
            ts["iso"] += 1
        elif DATETIME_TS.match(line):
            ts["datetime"] += 1
        elif TIME_TS.match(line):
            ts["time"] += 1
        else:
            ts["none"] += 1
    timestamp_style = ts.most_common(1)[0][0]

    return line_format, timestamp_style


# --- component extraction -----------------------------------------------------

BRACKET_TAG = re.compile(r"\[([a-zA-Z][\w.\-]{1,30})\]")
# "module.submodule:function:42 -" as Loguru emits it
LOGURU_MODULE = re.compile(r"\|\s*([\w.]+):[\w.<>]+:\d+\s*[-—]")
JSON_KEYS = ("component", "service", "module", "logger", "name", "source", "stage")


def _normalise(name: str) -> str:
    """Reduce a raw tag to a stable component name."""
    name = name.strip().strip(".-_/")
    # tools.vector_tools -> vector_tools; a dotted path's tail is the useful part
    if "." in name:
        name = name.rsplit(".", 1)[-1]
    return name.lower()


def _viable(name: str) -> bool:
    if len(name) < 2 or len(name) > 30:
        return False
    if name in NOT_COMPONENTS:
        return False
    if name.isdigit():
        return False
    # A component name is a word, not a sentence fragment or a value.
    return bool(re.fullmatch(r"[a-z][\w\-]*", name))


def extract_components(lines: list[str], line_format: str = "unknown") -> tuple[list[str], dict[str, int], str]:
    """Find this project's pipeline stages in its own log lines.

    Returns (components, counts, method). Ordered by first appearance rather than by
    frequency, since a pipeline's order is what the diagram needs to show.
    """
    counts: Counter[str] = Counter()
    first_seen: dict[str, int] = {}
    method = "none"

    def record(name: str, index: int) -> None:
        norm = _normalise(name)
        if _viable(norm):
            counts[norm] += 1
            first_seen.setdefault(norm, index)

    for index, raw in enumerate(lines):
        if not raw or not raw.strip():
            continue
        line = strip_ansi(raw)

        if _is_json_line(line):
            try:
                payload = json.loads(line.strip())
            except (json.JSONDecodeError, ValueError):
                payload = {}
            for key in JSON_KEYS:
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    record(value, index)
                    method = "json-field"
                    break
            continue

        # Bracketed tags are the strongest signal and the convention the skill teaches.
        tags = BRACKET_TAG.findall(line)
        if tags:
            for tag in tags[:2]:   # a line rarely carries more than one real component
                record(tag, index)
            method = "bracket-tag" if method in ("none", "bracket-tag") else method
            continue

        module = LOGURU_MODULE.search(line)
        if module:
            record(module.group(1), index)
            if method == "none":
                method = "logger-module"

    viable = {name: n for name, n in counts.items() if n >= MIN_OCCURRENCES}
    if not viable:
        return [], {}, "none"

    ordered = sorted(viable, key=lambda name: first_seen[name])[:MAX_COMPONENTS]
    return ordered, {name: counts[name] for name in ordered}, method


def learn(lines: list[str]) -> SourceProfile:
    """Build a profile from everything seen so far."""
    profile = SourceProfile(lines_seen=len([l for l in lines if l and l.strip()]))
    if profile.lines_seen < MIN_LINES_FOR_FORMAT:
        return profile

    profile.line_format, profile.timestamp_style = detect_format(lines)
    profile.has_continuations = any(
        re.match(r"^\s+\S", strip_ansi(l)) for l in lines if l
    )

    components, counts, method = extract_components(lines, profile.line_format)
    profile.components = components
    profile.component_counts = counts
    profile.method = method
    profile.confident = bool(components) and profile.lines_seen >= MIN_LINES_FOR_COMPONENTS
    return profile
