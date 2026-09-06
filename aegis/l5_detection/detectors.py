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

Not implemented yet, honestly: ErrorRatio and CardinalityShift need structured
success/attempt fields; ConformanceRate needs C7; seasonal (hour-of-day)
baselines need dated timestamps, which this format family does not carry.
"""

from __future__ import annotations

import itertools
from collections import deque
from dataclasses import dataclass, field
from statistics import median

from app.core.baselines import Baselines

from aegis.contracts.events import Event, Signal


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
    SILENCE_FACTOR = 3.0
    SILENCE_MIN_S = 300.0
    LATENCY_COOLDOWN_S = 60.0

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

        emitted += self._novelty(event)
        if now is not None:
            if not clock_jumped:
                emitted += self._silence(event, now)
            emitted += self._rates(event, now)
            emitted += self._latency_shift(event, now)
            self._last_event_at = now

        self.signals.extend(emitted)
        return emitted

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
            # The bar is the largest quiet the service has ALREADY shown, not
            # the median gap. A call-based service is normally silent between
            # calls: judged against its median (about 1s, the within-call
            # rhythm), every routine idle period fired - ten alarms in one
            # file for a service doing exactly what it always does.
            longest_seen = max(self._gaps) if self._gaps else 0.0
            if (len(self._gaps) >= self.SILENCE_MIN_GAPS
                    and gap >= max(self.SILENCE_FACTOR * longest_seen,
                                   self.SILENCE_MIN_S)):
                # Detected retrospectively, when the stream resumes: a silent
                # source emits nothing to detect WITH. The absence of logs is
                # a signal; naive systems read it as health.
                emitted.append(self._signal(
                    detector="SilenceDetector",
                    metric="silence_gap_s",
                    observed=round(gap, 1),
                    baseline=round(longest_seen, 1),
                    ratio=round(gap / max(longest_seen, 0.01), 1),
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
