"""What is normal for this system, and what is not.

Statistics, not a model. A duration only means something next to the same operation's
own history: 6946ms is unremarkable in isolation and alarming against a 800ms baseline.
Nothing here calls an LLM, so it keeps working when no key is configured and never
invents a number it did not measure.

Three questions, all answered from the same store:

  spike    this run against the operation's own history
  drift    the history itself moving over days - the shape no threshold catches
  absence  an operation that always ran and has stopped
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from typing import Any

# Below this a "baseline" is an anecdote. Report "not enough history" instead of a
# comparison the user would reasonably read as authoritative.
MIN_SAMPLES = 10

# How far past the usual range counts as worth mentioning. Deliberately not a
# threshold on the value - a threshold cannot know that 6946ms is 8x normal while
# 6946ms elsewhere is routine.
SPIKE_RATIO = 3.0

# Keep the window bounded: a baseline should track how the system behaves now, not
# average away a regression against last month's good behaviour.
MAX_SAMPLES = 200


# Durations as real loggers write them. Ordered longest-match first so
# "after 5000ms" is not read as a bare number.
DURATION_PATTERNS = (
    re.compile(r"\bin\s+(\d+(?:\.\d+)?)\s*ms\b", re.I),
    re.compile(r"\bafter\s+(\d+(?:\.\d+)?)\s*ms\b", re.I),
    re.compile(r"\btook\s+(\d+(?:\.\d+)?)\s*ms\b", re.I),
    re.compile(r"\bduration[=:\s]+(\d+(?:\.\d+)?)\s*ms\b", re.I),
    re.compile(r"\belapsed[=:\s]+(\d+(?:\.\d+)?)\s*ms\b", re.I),
    # JSON idiom: the unit lives in the KEY ("durationMs": 45, latency_ms=12).
    re.compile(r"\b(?:duration|latency|elapsed|took)[_]?ms[\"']?\s*[=:]\s*[\"']?(\d+(?:\.\d+)?)", re.I),
    re.compile(r"\b(\d+(?:\.\d+)?)\s*ms\b", re.I),
)

# Seconds, converted to ms so everything compares on one scale.
SECONDS_PATTERNS = (
    re.compile(r"\bin\s+(\d+(?:\.\d+)?)\s*s(?:ec|econds?)?\b", re.I),
    re.compile(r"\bafter\s+(\d+(?:\.\d+)?)\s*s(?:ec|econds?)?\b", re.I),
    re.compile(r"\bduration[=:\s]+(\d+(?:\.\d+)?)\s*s(?:ec|econds?)?\b", re.I),
    re.compile(r"\btook\s+(\d+(?:\.\d+)?)\s*s(?:ec|econds?)?\b", re.I),
    # "- 16s," as a call/run total. Comma- or dash-delimited so a version string
    # like "v3s" or an id fragment cannot be read as a measurement.
    re.compile(r"[-\u2013]\s*(\d+(?:\.\d+)?)\s*s\s*[,.]", re.I),
)

# Values that are identifiers or settings, not measurements of this run. Without this
# a startup banner's "timeout=45.0s" becomes a 45-second operation.
NOT_A_MEASUREMENT = re.compile(
    r"\b(?:timeout|ttl|interval|max|min|limit|threshold|budget|deadline|expiry|retry_after)\b",
    re.I,
)


def _operation_name(message: str, duration_text: str) -> str:
    """Name the operation from the words leading up to its duration.

    Real logs put the verb before the number - "Synthesised 60 chars in 1136ms",
    "Gateway ready in 8ms". The words before the duration are what distinguishes two
    operations of the same component, so a slow TTS synth is not averaged together
    with a fast one.
    """
    head = message[: message.find(duration_text)] if duration_text in message else message

    # Drop any logger scaffolding the parser left in place ("INFO voice.tts:") - it is
    # the same on every line of a component, so including it collapses every distinct
    # operation into one bucket named after the logger.
    head = re.sub(
        r"^\s*(?:INFO|WARN|WARNING|ERROR|DEBUG|TRACE|CRITICAL|FATAL|SUCCESS)\s+",
        "", head, flags=re.I,
    )
    head = re.sub(r"^\s*[\w.]+:\s*", "", head)
    # Drop the component tag; the caller records that separately.
    head = re.sub(r"^\s*\[[^\]]+\]\s*", "", head)
    # Drop identifiers and values - they differ per run and would make every run its
    # own "operation", so nothing would ever accumulate a baseline.
    head = re.sub(r"\b[\w.]+=[^\s]+", "", head)
    head = re.sub(r"\b[0-9a-f]{8}-[0-9a-f-]{8,}\b", "", head, flags=re.I)
    head = re.sub(r"\b\d+\b", "", head)
    head = re.sub(r"https?://\S+", "", head)

    words = [w for w in re.findall(r"[A-Za-z/][\w/]*", head) if len(w) > 1]
    if not words:
        return "operation"
    # Three words is enough to separate "Gateway ready" from "Handing call to
    # /voice/process" without splitting on incidental wording.
    return " ".join(words[:3]).lower()


def extract_duration(message: str) -> tuple[str, float] | None:
    """Pull (operation, milliseconds) out of a log line, or None.

    Returns None rather than guessing: a line with no measurement must not contribute
    a fabricated sample to a baseline other answers are drawn from.
    """
    if not message:
        return None
    if NOT_A_MEASUREMENT.search(message):
        return None

    for pattern in DURATION_PATTERNS:
        match = pattern.search(message)
        if match:
            return _operation_name(message, match.group(0)), float(match.group(1))

    for pattern in SECONDS_PATTERNS:
        match = pattern.search(message)
        if match:
            return _operation_name(message, match.group(0)), float(match.group(1)) * 1000.0

    return None


@dataclass
class Baseline:
    """One operation's measured history."""

    component: str
    operation: str
    samples: list[float] = field(default_factory=list)
    first_seen: str = ""
    last_seen: str = ""

    def add(self, value: float, when: str = "") -> None:
        self.samples.append(value)
        if len(self.samples) > MAX_SAMPLES:
            self.samples = self.samples[-MAX_SAMPLES:]
        if when:
            self.first_seen = self.first_seen or when
            self.last_seen = when

    @property
    def count(self) -> int:
        return len(self.samples)

    @property
    def ready(self) -> bool:
        """Whether there is enough history to compare against honestly."""
        return self.count >= MIN_SAMPLES

    @property
    def median(self) -> float:
        return statistics.median(self.samples) if self.samples else 0.0

    @property
    def p95(self) -> float:
        if not self.samples:
            return 0.0
        ordered = sorted(self.samples)
        return ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]

    def as_dict(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "operation": self.operation,
            "count": self.count,
            "ready": self.ready,
            "median_ms": round(self.median, 1),
            "p95_ms": round(self.p95, 1),
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
        }


class Baselines:
    """Every operation's history, and what stands out against it.

    Backed by a store when one is given, so history survives a restart - a baseline that
    is forgotten every time the window closes can never answer "is this normal?". Without
    a store it still works in memory, so nothing here requires setup.
    """

    def __init__(self, store: Any = None, source: str = "default") -> None:
        self._by_key: dict[tuple[str, str], Baseline] = {}
        self._store = store
        self._source = source
        self._observed = 0
        if store is not None:
            self._load()

    def _load(self) -> None:
        """Rehydrate baselines learned in previous sessions."""
        for component, operation, _count in self._store.known_operations(self._source):
            samples = self._store.samples(self._source, component, operation)
            if not samples:
                continue
            baseline = Baseline(component=component, operation=operation)
            baseline.samples = list(samples)
            self._by_key[(component, operation)] = baseline

    def observe(self, component: str, message: str, when: str = "") -> dict[str, Any] | None:
        """Record a measurement, and report it if it stands out.

        Returns a finding only when there is enough history to justify one - the
        comparison itself is the claim, so it must not rest on two samples.
        """
        found = extract_duration(message)
        if not found:
            return None
        operation, value = found

        key = (component, operation)
        baseline = self._by_key.get(key)
        if baseline is None:
            baseline = Baseline(component=component, operation=operation)
            self._by_key[key] = baseline

        # Compare BEFORE adding, so a spike is measured against prior history rather
        # than against a baseline it has already shifted.
        finding = None
        if baseline.ready:
            median = baseline.median
            if median > 0 and value >= median * SPIKE_RATIO:
                finding = {
                    "kind": "spike",
                    "component": component,
                    "operation": operation,
                    "value_ms": round(value, 1),
                    "median_ms": round(median, 1),
                    "ratio": round(value / median, 1),
                    "samples": baseline.count,
                    "when": when,
                }

        baseline.add(value, when)

        if self._store is not None:
            try:
                self._store.record_duration(self._source, component, operation, value, when)
                self._observed += 1
                # Trim occasionally rather than per insert; a scan on every log line
                # would put database work in the ingest path for no gain.
                if self._observed % 500 == 0:
                    self._store.prune(self._source)
            except Exception:
                # History is an enhancement. A database problem must never stop the
                # agent from reading logs, which is its actual job.
                pass

        return finding

    def absences(self, seen_components: set[str]) -> list[dict[str, Any]]:
        """Operations with real history that did not appear in this run.

        Absence is invisible to every threshold - nothing fires when a step simply
        stops happening - so it has to be looked for deliberately.
        """
        missing = []
        for (component, operation), baseline in self._by_key.items():
            if baseline.ready and component not in seen_components:
                missing.append({
                    "kind": "absence",
                    "component": component,
                    "operation": operation,
                    "samples": baseline.count,
                    "median_ms": round(baseline.median, 1),
                })
        return missing

    def summary(self) -> list[dict[str, Any]]:
        """Every baseline, slowest first - the useful order when scanning."""
        return sorted(
            (b.as_dict() for b in self._by_key.values()),
            key=lambda item: item["median_ms"],
            reverse=True,
        )

    def describe(self, finding: dict[str, Any]) -> str:
        """One plain sentence. No jargon, and it says why nothing alerted."""
        if finding.get("kind") == "absence":
            return (
                f"{finding['component']} usually runs at this point "
                f"({finding['samples']} previous runs) but did not appear."
            )
        return (
            f"{finding['component']} took {finding['value_ms']:.0f}ms - "
            f"{finding['ratio']}x its usual {finding['median_ms']:.0f}ms "
            f"across {finding['samples']} runs."
        )
