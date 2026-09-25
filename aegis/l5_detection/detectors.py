"""C8 - the detection engine.

Decide, cheaply and deterministically, whether something deserves attention.
Nothing above this layer runs until this layer says so.

Every detector here is arithmetic - counting, ratios, medians. Rules P1/P2:
if any part of detection calls a model, that part is misplaced. The LLM enters
at the reasoning layer (C10) to EXPLAIN what these detectors found, never to
find it. That split is why detection costs nothing at rest and fires the same
way on the same input every time.

Detectors implemented (the doc's catalogue, the subset a single log stream
supports):

  NoveltyDetector  a template never seen before - no baseline, no model, and
                   the cheapest high-value signal in the platform
  RateSpike        a template's rate versus its own history, with hysteresis
  RateDrop         a steady template that has stopped appearing
  LatencyShift     a duration versus that operation's own history (reuses the
                   proven statistics in app.core.baselines)
  SilenceDetector  a whole service going quiet mid-stream
  ErrorRatio       the share of error-like events (ERROR level, or 5xx status
                   classes) against the stream's own historical share
  CostAnomaly      tokens (or any cost unit) per unit of work, against the
                   same work's own history - the failure that shows up on the
                   bill rather than in an error
  CostAnomaly      tokens (or any cost unit) per unit of work, against the
                   same work's own history - the failure that shows up on the
                   bill rather than in an error
  CostAnomaly      tokens (or any cost unit) per unit of work, against the
                   same work's own history - the failure that shows up on the
                   bill rather than in an error
  CostAnomaly      tokens (or any cost unit) per unit of work, against the
                   same work's own history - the failure that shows up on the
                   bill rather than in an error
  ErrorRatio       the share of error-like events (ERROR level, or 5xx status
                   classes) against the stream's own historical share
  ErrorRatio       the share of error-like events (ERROR level, or 5xx status
                   classes) against the stream's own historical share
  ErrorRatio       the share of error-like events (ERROR level, or 5xx status
                   classes) against the stream's own historical share
  ErrorRatio       the share of error-like events (ERROR level, or 5xx status
                   classes) against the stream's own historical share

Not implemented yet, honestly: CardinalityShift needs per-entity fields;
seasonal (hour-of-day) baselines need dated timestamps, which this format
family does not carry. ConformanceRate is emitted by C7's enforce mode.

Suppression (the doc's SuppressionRules): templates in `suppressed` are
invisible to every detector - the mute switch that keeps a known-noisy line
from training people to ignore the whole tool.
"""

from __future__ import annotations

import itertools
import re
from collections import deque
from dataclasses import dataclass, field
from statistics import median

from app.core.baselines import Baselines

from aegis.contracts.events import Event, Signal


# Cost as real code logs it. The unit is whatever the app counts - tokens
# here, but credits or units read the same way.
# What the tokens were spent ON. Without this, one node's prompt growth hides
# inside another's normal spend.




# Cost as real code logs it. The unit is whatever the app counts - tokens
# here, but credits or units read the same way.
# What the tokens were spent ON. Without this, one node's prompt growth hides
# inside another's normal spend.




# Cost as real code logs it. The unit is whatever the app counts - tokens
# here, but credits or units read the same way.
# What the tokens were spent ON. Without this, one node's prompt growth hides
# inside another's normal spend.




# Cost as real code logs it. The unit is whatever the app counts - tokens
# here, but credits or units read the same way.
_COST_PATTERNS = (
    re.compile(r"\b(?:total_tokens|tokens_total|total_cost)[=:\s]+(\d+(?:\.\d+)?)", re.I),
    re.compile(r"\b(?:prompt|input)_tokens[=:\s]+(\d+(?:\.\d+)?)", re.I),
    re.compile(r"\b(?:completion|output)_tokens[=:\s]+(\d+(?:\.\d+)?)", re.I),
    re.compile(r"\btokens[=:\s]+(\d+(?:\.\d+)?)", re.I),
    re.compile(r"\busage[=:\s]+(\d+(?:\.\d+)?)", re.I),
)
# What the tokens were spent ON. Without this, one node's prompt growth hides
# inside another's normal spend.
_COST_LABEL = re.compile(r"\b(?:node|step|operation|op|model)[=:]\s*([\w.\-/]+)", re.I)


def _extract_cost(message: str) -> tuple[str, float] | None:
    if not message:
        return None
    for pattern in _COST_PATTERNS:
        match = pattern.search(message)
        if match:
            label_match = _COST_LABEL.search(message)
            label = label_match.group(1).lower() if label_match else "overall"
            # The key names WHICH count this is, so input and output token
            # histories never average together.
            kind = match.re.pattern.split("(?:")[1].split(")")[0].split("|")[0]
            return f"{label}:{kind}", float(match.group(1))
    return None


def _seconds(ts: str) -> float | None:
    """'19:02:09' -> seconds since midnight. None when there is no timestamp -
    a detector must never invent a time."""
    parts = ts.strip().split(":")
    if len(parts) not in (2, 3):
        return None
    try:
        numbers = [float(p) for p in parts]
    except ValueError:
        return None
    if len(numbers) == 2:
        numbers.append(0.0)
    hours, minutes, secs = numbers
    return hours * 3600 + minutes * 60 + secs


class HysteresisGate:
    """The four-parameter threshold the doc insists on.

    Fire only after the condition has held for `sustain_s`; clear only after
    it has been below a LOWER bar for `clear_sustain_s`. Without this, one
    metric hovering at the line produces forty pages - the doc's flapping
    example is FIRE/CLEAR forty times in ninety seconds.
    """

    def __init__(self, sustain_s: float = 20.0, clear_sustain_s: float = 60.0) -> None:
        self.sustain_s = sustain_s
        self.clear_sustain_s = clear_sustain_s
        self.firing = False
        self._breach_since: float | None = None
        self._clear_since: float | None = None

    def update(self, now: float, above_fire: bool, below_clear: bool) -> str | None:
        """Returns "fire" or "clear" on a state change, else None."""
        if not self.firing:
            if above_fire:
                if self._breach_since is None:
                    self._breach_since = now
                if now - self._breach_since >= self.sustain_s:
                    self.firing = True
                    self._clear_since = None
                    return "fire"
            else:
                self._breach_since = None
        else:
            if below_clear:
                if self._clear_since is None:
                    self._clear_since = now
                if now - self._clear_since >= self.clear_sustain_s:
                    self.firing = False
                    self._breach_since = None
                    return "clear"
            else:
                self._clear_since = None
        return None


@dataclass
class _TemplateTrack:
    """One template's rate bookkeeping."""

    first_seen: float
    last_seen: float
    total: int = 0
    window: deque = field(default_factory=deque)  # event times in the last 60s
    gate: HysteresisGate = field(default_factory=HysteresisGate)
    dropped: bool = False
    recent: deque = field(default_factory=lambda: deque(maxlen=3))  # evidence


class DetectionEngine:
    """All detectors over one project's event stream. Free at rest."""

    WINDOW_S = 60.0
    # A rate comparison against under 5 minutes of history is an anecdote.
    MIN_HISTORY_S = 300.0
    SPIKE_RATIO = 8.0
    SPIKE_CLEAR_RATIO = 2.0
    SPIKE_MIN_EVENTS = 5
    DROP_FACTOR = 5.0
    DROP_MIN_RATE = 0.5      # per minute; rarer templates have no rhythm to lose
    DROP_MIN_EVENTS = 10
    # Every template is novel on a cold start. INFO novelty is suppressed until
    # this many events have passed; ERROR/WARN novelty always matters.
    NOVELTY_WARMUP_EVENTS = 100
    SILENCE_MIN_GAPS = 50
    SILENCE_FACTOR = 10.0
    SILENCE_MIN_S = 120.0
    LATENCY_COOLDOWN_S = 60.0
    COST_MIN_SAMPLES = 8      # below this a "usual cost" is an anecdote
    COST_RATIO = 2.5          # a 2.5x jump in tokens for the same work
    COST_COOLDOWN_S = 120.0
    COST_MIN_SAMPLES = 8      # below this a "usual cost" is an anecdote
    COST_RATIO = 2.5          # a 2.5x jump in tokens for the same work
    COST_COOLDOWN_S = 120.0
    COST_MIN_SAMPLES = 8      # below this a "usual cost" is an anecdote
    COST_RATIO = 2.5          # a 2.5x jump in tokens for the same work
    COST_COOLDOWN_S = 120.0
    COST_MIN_SAMPLES = 8      # below this a "usual cost" is an anecdote
    COST_RATIO = 2.5          # a 2.5x jump in tokens for the same work
    COST_COOLDOWN_S = 120.0
    ERR_MIN_HISTORY = 100      # events before an error share means anything
    ERR_MIN_WINDOW = 8
    ERR_RATIO = 4.0
    ERR_FLOOR = 0.30           # never fire under 30% errors regardless of baseline

    def __init__(self, service: str = "") -> None:
        self.service = service
        self.signals: list[Signal] = []
        self._ids = itertools.count(1)
        self._tracks: dict[str, _TemplateTrack] = {}
        self._latency = Baselines()          # deterministic duration statistics
        self._latency_last_fire: dict[str, float] = {}
        self._gaps: deque = deque(maxlen=200)
        self._last_event_at: float | None = None
        self._events_seen = 0
        self._day_offset = 0.0
        # C8 SuppressionRules: template ids muted by the operator.
        self.suppressed: set[str] = set()
        self._err_window: deque = deque()
        self._err_totals = [0, 0]           # events, errors (lifetime)
        self._err_gate = HysteresisGate(sustain_s=20.0, clear_sustain_s=60.0)
        self._err_recent: deque = deque(maxlen=3)
        # cost history per unit of work: {label: [tokens, ...]}
        self._cost: dict[str, deque] = {}
        self._cost_last_fire: dict[str, float] = {}
        # cost history per unit of work: {label: [tokens, ...]}
        self._cost: dict[str, deque] = {}
        self._cost_last_fire: dict[str, float] = {}
        # cost history per unit of work: {label: [tokens, ...]}
        self._cost: dict[str, deque] = {}
        self._cost_last_fire: dict[str, float] = {}
        # cost history per unit of work: {label: [tokens, ...]}
        self._cost: dict[str, deque] = {}
        self._cost_last_fire: dict[str, float] = {}
        # C8 SuppressionRules: template ids muted by the operator.
        self.suppressed: set[str] = set()
        self._err_window: deque = deque()
        self._err_totals = [0, 0]           # events, errors (lifetime)
        self._err_gate = HysteresisGate(sustain_s=20.0, clear_sustain_s=60.0)
        self._err_recent: deque = deque(maxlen=3)
        # C8 SuppressionRules: template ids muted by the operator.
        self.suppressed: set[str] = set()
        self._err_window: deque = deque()
        self._err_totals = [0, 0]           # events, errors (lifetime)
        self._err_gate = HysteresisGate(sustain_s=20.0, clear_sustain_s=60.0)
        self._err_recent: deque = deque(maxlen=3)
        # C8 SuppressionRules: template ids muted by the operator.
        self.suppressed: set[str] = set()
        self._err_window: deque = deque()
        self._err_totals = [0, 0]           # events, errors (lifetime)
        self._err_gate = HysteresisGate(sustain_s=20.0, clear_sustain_s=60.0)
        self._err_recent: deque = deque(maxlen=3)
        # C8 SuppressionRules: template ids muted by the operator.
        self.suppressed: set[str] = set()
        self._err_window: deque = deque()
        self._err_totals = [0, 0]           # events, errors (lifetime)
        self._err_gate = HysteresisGate(sustain_s=20.0, clear_sustain_s=60.0)
        self._err_recent: deque = deque(maxlen=3)

    # -- the one entry point -------------------------------------------------

    def observe(self, event: Event) -> list[Signal]:
        self._events_seen += 1
        emitted: list[Signal] = []

        now = _seconds(event.ts)
        clock_jumped = False
        if now is not None:
            now += self._day_offset
            if self._last_event_at is not None and now + 60 < self._last_event_at:
                # Time went backwards: midnight rollover, or the file spans a
                # restart days later. Either way the rhythm before the jump
                # says nothing about the rhythm after it - reset, don't fire.
                self._day_offset += 86400
                now += 86400
                self._gaps.clear()
                for track in self._tracks.values():
                    track.window.clear()
                clock_jumped = True

        if event.template_id in self.suppressed:
            # Muted: it still marks the stream as alive (silence must not fire
            # because the only chatty template was muted), but no detector
            # may speak about it.
            if now is not None:
                self._last_event_at = now
            return []

        emitted += self._novelty(event)
        if now is not None:
            if not clock_jumped:
                emitted += self._silence(event, now)
            emitted += self._rates(event, now)
            emitted += self._latency_shift(event, now)
            emitted += self._error_ratio(event, now)
            emitted += self._cost_anomaly(event, now)
            self._last_event_at = now

        self.signals.extend(emitted)
        return emitted

    @staticmethod
    def _error_like(event: Event) -> bool:
        if event.level in ("ERROR", "CRITICAL"):
            return True
        return "sc5xx" in str(event.fields.get("template_pattern", ""))

    def _cost_anomaly(self, event: Event, now: float) -> list[Signal]:
        """Tokens per unit of work, against that work's own history.

        Deliberately not a spend threshold: "$40 today" means nothing without
        knowing what it bought. A prompt that silently doubles, or a retry
        loop re-asking the model, shows up here as the same work costing more
        - and nowhere else, because nothing errors.
        """
        line = event.text_redacted.splitlines()[0]
        found = _extract_cost(line)
        if not found:
            return []
        label, amount = found
        history = self._cost.setdefault(label, deque(maxlen=200))

        finding = None
        if len(history) >= self.COST_MIN_SAMPLES:
            ordered = sorted(history)
            usual = ordered[len(ordered) // 2]
            last = self._cost_last_fire.get(label)
            if usual > 0 and amount >= usual * self.COST_RATIO \
                    and (last is None or now - last >= self.COST_COOLDOWN_S):
                self._cost_last_fire[label] = now
                finding = self._signal(
                    detector="CostAnomaly", metric=f"{label} tokens",
                    observed=round(amount, 1), baseline=round(usual, 1),
                    ratio=round(amount / usual, 1), severity="P3", event=event)
        # Compare BEFORE recording, so a spike is measured against prior
        # history rather than against a baseline it has already shifted.
        history.append(amount)
        return [finding] if finding else []

    def _cost_anomaly(self, event: Event, now: float) -> list[Signal]:
        """Tokens per unit of work, against that work's own history.

        Deliberately not a spend threshold: "$40 today" means nothing without
        knowing what it bought. A prompt that silently doubles, or a retry
        loop re-asking the model, shows up here as the same work costing more
        - and nowhere else, because nothing errors.
        """
        line = event.text_redacted.splitlines()[0]
        found = _extract_cost(line)
        if not found:
            return []
        label, amount = found
        history = self._cost.setdefault(label, deque(maxlen=200))

        finding = None
        if len(history) >= self.COST_MIN_SAMPLES:
            ordered = sorted(history)
            usual = ordered[len(ordered) // 2]
            last = self._cost_last_fire.get(label)
            if usual > 0 and amount >= usual * self.COST_RATIO \
                    and (last is None or now - last >= self.COST_COOLDOWN_S):
                self._cost_last_fire[label] = now
                finding = self._signal(
                    detector="CostAnomaly", metric=f"{label} tokens",
                    observed=round(amount, 1), baseline=round(usual, 1),
                    ratio=round(amount / usual, 1), severity="P3", event=event)
        # Compare BEFORE recording, so a spike is measured against prior
        # history rather than against a baseline it has already shifted.
        history.append(amount)
        return [finding] if finding else []

    def _cost_anomaly(self, event: Event, now: float) -> list[Signal]:
        """Tokens per unit of work, against that work's own history.

        Deliberately not a spend threshold: "$40 today" means nothing without
        knowing what it bought. A prompt that silently doubles, or a retry
        loop re-asking the model, shows up here as the same work costing more
        - and nowhere else, because nothing errors.
        """
        line = event.text_redacted.splitlines()[0]
        found = _extract_cost(line)
        if not found:
            return []
        label, amount = found
        history = self._cost.setdefault(label, deque(maxlen=200))

        finding = None
        if len(history) >= self.COST_MIN_SAMPLES:
            ordered = sorted(history)
            usual = ordered[len(ordered) // 2]
            last = self._cost_last_fire.get(label)
            if usual > 0 and amount >= usual * self.COST_RATIO \
                    and (last is None or now - last >= self.COST_COOLDOWN_S):
                self._cost_last_fire[label] = now
                finding = self._signal(
                    detector="CostAnomaly", metric=f"{label} tokens",
                    observed=round(amount, 1), baseline=round(usual, 1),
                    ratio=round(amount / usual, 1), severity="P3", event=event)
        # Compare BEFORE recording, so a spike is measured against prior
        # history rather than against a baseline it has already shifted.
        history.append(amount)
        return [finding] if finding else []

    def _cost_anomaly(self, event: Event, now: float) -> list[Signal]:
        """Tokens per unit of work, against that work's own history.

        Deliberately not a spend threshold: "$40 today" means nothing without
        knowing what it bought. A prompt that silently doubles, or a retry
        loop re-asking the model, shows up here as the same work costing more
        - and nowhere else, because nothing errors.
        """
        line = event.text_redacted.splitlines()[0]
        found = _extract_cost(line)
        if not found:
            return []
        label, amount = found
        history = self._cost.setdefault(label, deque(maxlen=200))

        finding = None
        if len(history) >= self.COST_MIN_SAMPLES:
            ordered = sorted(history)
            usual = ordered[len(ordered) // 2]
            last = self._cost_last_fire.get(label)
            if usual > 0 and amount >= usual * self.COST_RATIO \
                    and (last is None or now - last >= self.COST_COOLDOWN_S):
                self._cost_last_fire[label] = now
                finding = self._signal(
                    detector="CostAnomaly", metric=f"{label} tokens",
                    observed=round(amount, 1), baseline=round(usual, 1),
                    ratio=round(amount / usual, 1), severity="P3", event=event)
        # Compare BEFORE recording, so a spike is measured against prior
        # history rather than against a baseline it has already shifted.
        history.append(amount)
        return [finding] if finding else []

    def _error_ratio(self, event: Event, now: float) -> list[Signal]:
        """The doc's ErrorRatio, generalised: error-LIKE events (ERROR level,
        or the sc5xx status class - which is how an access log says "error"
        at INFO level) as a share of the stream, against its own history."""
        is_error = self._error_like(event)
        baseline_events, baseline_errors = self._err_totals
        self._err_totals[0] += 1
        self._err_totals[1] += 1 if is_error else 0
        if is_error:
            self._err_recent.append(event.text_redacted.splitlines()[0][:160])

        self._err_window.append((now, is_error))
        while self._err_window and now - self._err_window[0][0] > self.WINDOW_S:
            self._err_window.popleft()

        if baseline_events < self.ERR_MIN_HISTORY:
            return []
        window_total = len(self._err_window)
        window_errors = sum(1 for _, err in self._err_window if err)
        share = window_errors / max(window_total, 1)
        baseline_share = baseline_errors / max(baseline_events, 1)
        above = (window_total >= self.ERR_MIN_WINDOW
                 and share >= max(self.ERR_FLOOR,
                                  self.ERR_RATIO * baseline_share))
        below = share <= max(0.05, baseline_share * 1.5)
        if self._err_gate.update(now, above, below) != "fire":
            return []
        return [self._signal(
            detector="ErrorRatio", metric="error_share",
            observed=round(share, 2), baseline=round(baseline_share, 3),
            ratio=round(share / max(baseline_share, 0.001), 1),
            severity="P2", event=event,
            evidence=list(self._err_recent),
            sustained_s=self._err_gate.sustain_s,
        )]

    # -- detectors -----------------------------------------------------------

    def _novelty(self, event: Event) -> list[Signal]:
        if not event.is_novel or not event.template_id:
            return []
        is_error = event.level in {"ERROR", "WARN", "WARNING", "CRITICAL"}
        if not is_error and self._events_seen <= self.NOVELTY_WARMUP_EVENTS:
            return []
        return [self._signal(
            detector="NoveltyDetector",
            metric="first_occurrence",
            observed=1.0, baseline=0.0, ratio=0.0,
            severity="P2" if is_error else "P4",
            event=event,
        )]

    def _rates(self, event: Event, now: float) -> list[Signal]:
        emitted: list[Signal] = []
        track = self._tracks.get(event.template_id)
        if track is None:
            track = _TemplateTrack(first_seen=now, last_seen=now)
            self._tracks[event.template_id] = track
        track.total += 1
        track.last_seen = now
        track.dropped = False
        track.recent.append(event.text_redacted.splitlines()[0][:160])
        track.window.append(now)
        while track.window and now - track.window[0] > self.WINDOW_S:
            track.window.popleft()

        # --- RateSpike: this minute against the template's own history.
        history_s = now - track.first_seen
        if history_s >= self.MIN_HISTORY_S:
            completed = track.total - len(track.window)
            baseline_per_min = completed / max((history_s - self.WINDOW_S) / 60.0, 1.0)
            rate_now = float(len(track.window))
            above = (rate_now >= self.SPIKE_MIN_EVENTS
                     and rate_now >= self.SPIKE_RATIO * max(baseline_per_min, 0.1))
            below = rate_now <= self.SPIKE_CLEAR_RATIO * max(baseline_per_min, 0.1)
            change = track.gate.update(now, above, below)
            if change == "fire":
                emitted.append(self._signal(
                    detector="RateSpike",
                    metric="events_per_min",
                    observed=rate_now,
                    baseline=round(baseline_per_min, 2),
                    ratio=round(rate_now / max(baseline_per_min, 0.1), 1),
                    severity="P2",
                    event=event,
                    evidence=list(track.recent),
                    sustained_s=track.gate.sustain_s,
                ))

        # --- RateDrop: steady templates that have gone missing. Checked from
        # OTHER events' arrivals, because a stopped template emits nothing.
        for template_id, other in self._tracks.items():
            if template_id == event.template_id or other.dropped:
                continue
            active_min = (other.last_seen - other.first_seen) / 60.0
            if other.total < self.DROP_MIN_EVENTS or active_min < 5.0:
                continue
            rate = other.total / active_min
            if rate < self.DROP_MIN_RATE:
                continue
            expected_gap = 60.0 / rate
            gap = now - other.last_seen
            if gap >= max(self.DROP_FACTOR * expected_gap, 60.0):
                other.dropped = True
                emitted.append(self._signal(
                    detector="RateDrop",
                    metric="silence_gap_s",
                    observed=round(gap, 1),
                    baseline=round(expected_gap, 1),
                    ratio=round(gap / expected_gap, 1),
                    severity="P3",
                    event=event,
                    template_id=template_id,
                    evidence=list(other.recent),
                ))
        return emitted

    def _latency_shift(self, event: Event, now: float) -> list[Signal]:
        finding = self._latency.observe(
            event.service or self.service,
            event.text_redacted.splitlines()[0],
            event.ts,
        )
        if not finding:
            return []
        operation = finding["operation"]
        last = self._latency_last_fire.get(operation)
        if last is not None and now - last < self.LATENCY_COOLDOWN_S:
            return []
        self._latency_last_fire[operation] = now
        return [self._signal(
            detector="LatencyShift",
            metric=f"{operation} ms",
            observed=finding["value_ms"],
            baseline=finding["median_ms"],
            ratio=finding["ratio"],
            severity="P3",
            event=event,
        )]

    def _silence(self, event: Event, now: float) -> list[Signal]:
        emitted: list[Signal] = []
        if self._last_event_at is not None:
            gap = now - self._last_event_at
            if (len(self._gaps) >= self.SILENCE_MIN_GAPS
                    and gap >= max(self.SILENCE_FACTOR * median(self._gaps),
                                   self.SILENCE_MIN_S)):
                # Detected retrospectively, when the stream resumes: a silent
                # source emits nothing to detect WITH. The absence of logs is
                # a signal; naive systems read it as health.
                emitted.append(self._signal(
                    detector="SilenceDetector",
                    metric="silence_gap_s",
                    observed=round(gap, 1),
                    baseline=round(median(self._gaps), 2),
                    ratio=round(gap / max(median(self._gaps), 0.01), 1),
                    severity="P3",
                    event=event,
                ))
            if gap >= 0:
                self._gaps.append(gap)
        return emitted

    # -- plumbing ------------------------------------------------------------

    def _signal(self, *, detector: str, metric: str, observed: float,
                baseline: float, ratio: float, severity: str, event: Event,
                template_id: str = "", evidence: list[str] | None = None,
                sustained_s: float = 0.0) -> Signal:
        return Signal(
            id=f"S-{next(self._ids)}",
            detector=detector,
            service=event.service or self.service,
            metric=metric,
            observed=observed,
            baseline=baseline,
            ratio=ratio,
            started_at=event.ts,
            sustained_s=sustained_s,
            severity=severity,
            trace_id=event.trace_id,
            template_id=template_id or event.template_id,
            evidence=evidence if evidence is not None
                     else [event.text_redacted.splitlines()[0][:160]],
        )
