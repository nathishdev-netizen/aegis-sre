"""The shared types every layer agrees on (architecture doc, section 11).

One rule governs this file: `trace_id` is the join key for the entire platform.
Every component that can attach one, must. Where it is absent, correlation
degrades from a join to a guess - and because that guess is sometimes all we
have, it is recorded with its confidence rather than silently presented as fact.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


# How a correlation id came to be attached. The distinction is not cosmetic: an
# extracted id is evidence, an inferred one is an assumption, and the UI and the
# model must be able to tell them apart before asserting anything.
CORRELATION_EXTRACTED = "extracted"   # read verbatim from the line
CORRELATION_INFERRED = "inferred"     # deduced from an open session window
CORRELATION_NONE = "none"             # no basis at all


@dataclass
class RawRecord:
    """What a collector hands to normalization. No parsing, no judgement."""

    source_id: str
    payload: str
    host: str = ""
    service: str = ""
    collected_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Event:
    """One normalized, redacted log record.

    `text_redacted` is named for the guarantee it carries: by the time an Event
    exists, sensitive values are already gone. There is no unredacted variant
    held anywhere downstream, because redaction after storage is not redaction.
    """

    id: str
    ts: str
    service: str
    level: str
    text_redacted: str
    template_id: str = ""
    trace_id: str = ""
    span_id: str = ""
    host: str = ""
    env: str = ""
    deploy_version: str = ""
    fields: dict[str, Any] = field(default_factory=dict)
    # Provenance of trace_id - see the CORRELATION_* constants above.
    correlation_basis: str = CORRELATION_NONE
    # What was removed, by type, so a redaction bug is visible rather than silent.
    redactions: dict[str, int] = field(default_factory=dict)
    # True the first time this template is ever seen. The cheapest high-value
    # signal in the platform: it needs no baseline, no training, no model.
    is_novel: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Template:
    """The reusable shape of a log line, with variable parts removed."""

    id: str
    pattern: str
    first_seen: str = ""
    last_seen: str = ""
    total_count: int = 0
    example_event_id: str = ""
    service: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Signal:
    """A detector's finding. Produced by arithmetic, never by a model."""

    id: str
    detector: str
    service: str
    metric: str
    observed: float
    baseline: float
    ratio: float
    started_at: str = ""
    sustained_s: float = 0.0
    severity: str = "P3"
    trace_id: str = ""
    template_id: str = ""
    # The lines that prove it. A signal without evidence is an opinion.
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
