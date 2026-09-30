"""C6 - the flow spec: what a run of this kind is SUPPOSED to do.

The doc's C6 reads the codebase; this implementation reads the traces - the
TimingEstimator half of the component, deriving the spec from what the system
demonstrably does rather than guessing. The output honours the doc's
requirements that matter most:

  - human-editable, stored as a versioned file a person can open and fix
  - honest about confidence: every auto-derived step carries its presence
    fraction, so a human knows what to review
  - `critical` is NOT auto-derived. Auto-extraction reaches 70-85% (the doc's
    number); marking which step is the run's PURPOSE is the human's or the
    FlowSynthesizer model's call, and the spec records who made it.

Why `critical` matters more than `required`: a greeting appears in every call,
so mining marks it required - but a call that only greets achieved nothing.
The step that makes a run worth having usually appears only in the runs that
worked, which is exactly why frequency cannot find it.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from typing import Any

from app.core.baselines import extract_duration

from aegis.contracts.events import Event

REQUIRED_PRESENCE = 0.7
MIN_TRACE_EVENTS = 4


@dataclass
class FlowStep:
    template_id: str
    label: str
    presence: float            # fraction of traces containing this step
    required: bool
    critical: bool = False
    critical_source: str = ""  # "human" | "model" | "" - who marked it
    median_rank: float = 0.0
    max_duration_ms: float = 0.0
    note: str = ""
    # Where in the codebase this step is written (from C6), e.g.
    # "orchestrator.py:2399 in stream_ask". Empty when the step was only ever
    # seen in logs and no analysis matched it.
    code_site: str = ""
    # Steps the CODE declares but which no run has ever produced. A step that
    # exists in source and never appears is the strongest signal this layer
    # can carry - dead code, or a path that silently never executes.
    never_ran: bool = False
    # Where in the codebase this step is written (from C6), e.g.
    # "orchestrator.py:2399 in stream_ask". Empty when the step was only ever
    # seen in logs and no analysis matched it.
    code_site: str = ""
    # Steps the CODE declares but which no run has ever produced. A step that
    # exists in source and never appears is the strongest signal this layer
    # can carry - dead code, or a path that silently never executes.
    never_ran: bool = False
    # Where in the codebase this step is written (from C6), e.g.
    # "orchestrator.py:2399 in stream_ask". Empty when the step was only ever
    # seen in logs and no analysis matched it.
    code_site: str = ""
    # Steps the CODE declares but which no run has ever produced. A step that
    # exists in source and never appears is the strongest signal this layer
    # can carry - dead code, or a path that silently never executes.
    never_ran: bool = False
    # Where in the codebase this step is written (from C6), e.g.
    # "orchestrator.py:2399 in stream_ask". Empty when the step was only ever
    # seen in logs and no analysis matched it.
    code_site: str = ""
    # Steps the CODE declares but which no run has ever produced. A step that
    # exists in source and never appears is the strongest signal this layer
    # can carry - dead code, or a path that silently never executes.
    never_ran: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


@dataclass
class FlowSpec:
    name: str
    version: int = 1
    source: str = "learned-from-traces"
    steps: list[FlowStep] = field(default_factory=list)
    expected_duration_s: dict[str, float] = field(default_factory=dict)
    traces_mined: int = 0

    def step(self, template_id: str) -> FlowStep | None:
        return next((s for s in self.steps if s.template_id == template_id), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "version": self.version, "source": self.source,
            "traces_mined": self.traces_mined,
            "expected_duration_s": self.expected_duration_s,
            "steps": [s.to_dict() for s in self.steps],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FlowSpec":
        spec = cls(name=data["name"], version=int(data.get("version", 1)),
                   source=data.get("source", ""),
                   expected_duration_s=dict(data.get("expected_duration_s") or {}),
                   traces_mined=int(data.get("traces_mined", 0)))
        for raw in data.get("steps", []):
            spec.steps.append(FlowStep(**{k: raw.get(k, FlowStep.__dataclass_fields__[k].default
                                          if k not in ("template_id", "label", "presence", "required")
                                          else raw[k]) for k in FlowStep.__dataclass_fields__}))
        return spec

    def save(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self.name}.json"
        path.write_text(json.dumps(self.to_dict(), indent=2) + "\n")
        return path

    @classmethod
    def load(cls, path: Path) -> "FlowSpec":
        return cls.from_dict(json.loads(Path(path).read_text()))


def _seconds(ts: str) -> float | None:
    """Seconds on a comparable scale, from either timestamp shape we see.

    A log FILE usually prints a bare clock ("10:12:04"). A connector returns a
    full ISO instant ("2026-09-29T10:12:04.640311808Z"), which splits on ":"
    into three parts that are not all numbers - so this returned None for every
    connector-backed run and every duration came out 0.0.

    The ISO branch also fixes a second bug the clock branch still has by
    construction: a run that crosses midnight cannot be measured from a clock
    alone, because 00:00:05 looks like it comes before 23:59:58.
    """
    text = ts.strip()
    if not text:
        return None

    # Full instant first: it carries the date, so it is the only shape that can
    # measure a run spanning midnight.
    if "-" in text[:11] and "T" in text:
        iso = text.replace("Z", "+00:00")
        # Python parses at most 6 fractional digits; connectors send 9.
        iso = re.sub(r"(\.\d{6})\d+", r"\1", iso)
        try:
            return datetime.fromisoformat(iso).timestamp()
        except ValueError:
            return None

    parts = text.split(":")
    if len(parts) != 3:
        return None
    try:
        h, m, s = (float(p) for p in parts)
    except ValueError:
        return None
    return h * 3600 + m * 60 + s


def _label(event: Event) -> str:
    head = event.text_redacted.splitlines()[0]
    head = re.sub(r"^\s*\w+\s+[\w.]+:\s*", "", head)  # logger scaffold
    return head[:70]


# Placeholders in a source format string ({name}, %s, {}) and the values that
# replaced them in the logged line are the parts that CANNOT match, so both
# sides are reduced to their fixed words before comparing.
_FORMAT_HOLE = re.compile(r"\{[^}]*\}|%[sdrfi]|%\([^)]*\)[sdrfi]")


def _words(text: str) -> set[str]:
    without_holes = _FORMAT_HOLE.sub(" ", text or "")
    return {w for w in re.findall(r"[A-Za-z][\w/.-]*", without_holes.lower())
            if len(w) > 1}


def _overlap(logged: str, statement: str) -> float:
    """How much of a source log statement appears in a logged line, 0..1.

    Used to walk backwards from a line in a log file to the logging call that
    wrote it. Scored against the STATEMENT's own words (not the union) so a
    long runtime line carrying ids and values is not penalised for the extra
    words the format string never contained - the question is "is all of this
    statement present here", not "are these two strings alike".
    """
    wanted = _words(statement)
    if not wanted:
        return 0.0
    return len(wanted & _words(logged)) / len(wanted)


class FlowMiner:
    """Derive a FlowSpec from assembled traces. Deterministic; no model."""

    def mine(self, traces: dict[str, list[Event]], name: str) -> FlowSpec:
        usable = {tid: evs for tid, evs in traces.items()
                  if len(evs) >= MIN_TRACE_EVENTS}
        spec = FlowSpec(name=name, traces_mined=len(usable))
        if not usable:
            return spec

        presence: dict[str, int] = {}
        ranks: dict[str, list[float]] = {}
        durations: dict[str, list[float]] = {}
        labels: dict[str, str] = {}
        spans: list[float] = []

        for events in usable.values():
            seen: set[str] = set()
            for index, event in enumerate(events):
                tid = event.template_id
                labels.setdefault(tid, _label(event))
                if tid not in seen:
                    seen.add(tid)
                    presence[tid] = presence.get(tid, 0) + 1
                    ranks.setdefault(tid, []).append(index / max(len(events) - 1, 1))
                found = extract_duration(event.text_redacted.splitlines()[0])
                if found:
                    durations.setdefault(tid, []).append(found[1])
            start = _seconds(events[0].ts) if events[0].ts else None
            end = _seconds(events[-1].ts) if events[-1].ts else None
            if start is not None and end is not None and end >= start:
                spans.append(end - start)

        for tid, count in presence.items():
            fraction = count / len(usable)
            step_durations = sorted(durations.get(tid, []))
            spec.steps.append(FlowStep(
                template_id=tid,
                label=labels[tid],
                presence=round(fraction, 2),
                required=fraction >= REQUIRED_PRESENCE,
                median_rank=round(median(ranks[tid]), 3),
                max_duration_ms=round(step_durations[int(len(step_durations) * 0.95)]
                                      if step_durations else 0.0, 1),
            ))
        spec.steps.sort(key=lambda s: s.median_rank)
        if spans:
            ordered = sorted(spans)
            spec.expected_duration_s = {
                "p50": round(median(ordered), 1),
                "p95": round(ordered[int(len(ordered) * 0.95)], 1),
            }
        return spec


_SYNTH_PROMPT = """These are the steps of one operation ("{name}"), mined from {n} real runs. presence = fraction of runs containing the step.
{context}
{steps}

Which step IDs represent the run's actual PURPOSE - the work the run exists to do, without which a run that completed cleanly still achieved nothing? Greetings, prompts, setup and teardown are not the purpose.

{guidance}Reply with ONLY JSON:
{{"critical_step_ids": ["..."], "flow_purpose": "one sentence"}}"""


def synthesize_critical(spec: FlowSpec, router: Any) -> str:
    """The doc's FlowSynthesizer - C6's ONLY model step. The model may only
    flag steps that exist in the spec; anything else it says is discarded.
    Returns a one-line summary of what happened, for the audit trail."""
    listing = "\n".join(f"  {s.template_id}  presence={s.presence:.2f}  {s.label}"
                        for s in spec.steps)
    raw = router.chat("synthesize_flow",
                      [{"role": "user", "content": _SYNTH_PROMPT.format(
                          name=spec.name, n=spec.traces_mined, steps=listing)}],
                      purpose=f"mark critical steps of {spec.name}")
    if raw is None:
        return "model unavailable - critical flags left for a human to set"
    match = re.search(r"\{.*\}", raw, re.S)
    if not match:
        return "model reply unparseable - critical flags left for a human"
    try:
        parsed = json.loads(match.group(0))
    except ValueError:
        return "model reply unparseable - critical flags left for a human"
    marked = []
    for step_id in parsed.get("critical_step_ids", []):
        step = spec.step(str(step_id))
        if step is not None:            # unknown ids are silently dropped
            step.critical = True
            step.critical_source = "model"
            marked.append(step.label[:40])
    return (f"model marked {len(marked)} critical step(s): {marked}"
            if marked else "model marked nothing critical")
