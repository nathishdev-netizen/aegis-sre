"""Phase 7 regression tests - C6 flow specs + C7 conformance.

The moat's failure modes are both directions: calling a real conversation
hollow (which this code did on its first run) and calling a hollow call
achieved (which every error-based tool does always).
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aegis.contracts.events import Event  # noqa: E402
from aegis.l4_understanding.conformance import ConformanceEngine  # noqa: E402
from aegis.l4_understanding.flowspec import (  # noqa: E402
    FlowMiner, FlowSpec, FlowStep, synthesize_critical)


def _ev(text: str, ts: str = "10:00:00", level: str = "INFO",
        template: str = "") -> Event:
    return Event(id="E", ts=ts, service="svc", level=level,
                 text_redacted=text, template_id=template or f"T-{text[:12]}")


def _call(kind: str, minute: int) -> list[Event]:
    """A synthetic call in one of the real log's three observed shapes."""
    t = lambda s: f"10:{minute:02d}:{s:02d}"
    start = [_ev("CALL START", t(0), template="T-start"),
             _ev("greeting synthesised", t(1), template="T-greet")]
    end = [_ev("hangup", t(20), template="T-hangup"),
           _ev("CALL END", t(21), template="T-end")]
    if kind == "real":
        return start + [_ev("processing the caller's request", t(5), template="T-process"),
                        _ev("turn completed", t(9), template="T-turn")] + end
    if kind == "failed":
        return start + [_ev("Lookup FAILED", t(2), level="ERROR", template="T-fail")] + end
    return start + end  # hollow: greet then hangup, nothing between


def _spec_from(kinds: list[str]) -> tuple[FlowSpec, dict[str, list[Event]]]:
    traces = {f"t-{i}": _call(kind, i) for i, kind in enumerate(kinds)}
    spec = FlowMiner().mine(traces, name="call")
    step = spec.step("T-process")
    if step:
        step.critical = True
        step.critical_source = "human"
    return spec, traces


# --- Mining ------------------------------------------------------------------

def test_mining_marks_presence_and_required():
    spec, _ = _spec_from(["real", "real", "real", "hollow"])
    assert spec.step("T-start").required           # in every trace
    assert spec.step("T-process").presence == 0.75
    assert spec.step("T-process").required         # >= 0.7
    spec2, _ = _spec_from(["real", "hollow", "hollow", "hollow"])
    assert not spec2.step("T-process").required    # 0.25


def test_short_traces_do_not_pollute_the_spec():
    traces = {"t-1": _call("real", 1), "t-2": [_ev("stray", template="T-stray")]}
    spec = FlowMiner().mine(traces, name="call")
    assert spec.traces_mined == 1
    assert spec.step("T-stray") is None


def test_spec_survives_save_load_and_human_edits():
    """The spec is the human's document. An edit - marking a step critical -
    must survive the round trip, or editing it is a lie."""
    spec, _ = _spec_from(["real", "real"])
    directory = Path(tempfile.mkdtemp())
    path = spec.save(directory)
    loaded = FlowSpec.load(path)
    assert loaded.step("T-process").critical is True
    assert loaded.step("T-process").critical_source == "human"
    assert loaded.expected_duration_s == spec.expected_duration_s


# --- Verdicts ----------------------------------------------------------------

def test_a_clean_call_that_did_nothing_is_hollow():
    """The moat. Zero errors, normal teardown, green everywhere - and the
    purpose step never ran. Error-based monitoring cannot see this."""
    spec, _ = _spec_from(["real", "real", "real"])
    report, _ = ConformanceEngine().check("t-h", _call("hollow", 9), spec)
    assert report.verdict == "hollow"
    assert not any(e for e in _call("hollow", 9) if e.level == "ERROR")
    assert any(d["type"] == "purpose_never_ran" for d in report.deviations)


def test_a_real_conversation_is_achieved():
    spec, traces = _spec_from(["real", "real", "hollow"])
    report, _ = ConformanceEngine().check("t-0", traces["t-0"], spec)
    assert report.verdict == "achieved"


def test_errors_mean_failed_regardless_of_steps():
    spec, _ = _spec_from(["real", "real"])
    report, _ = ConformanceEngine().check("t-f", _call("failed", 9), spec)
    assert report.verdict == "failed"
    assert report.evidence, "a failed verdict must carry the error lines"


def test_purpose_is_a_family_not_a_checklist():
    """First run of the real log condemned every real conversation: the
    process step mines into per-code-path variants, the model marked six of
    them critical, and demanding ALL of them made a call hollow for missing
    the OTHER calls' variants. One purpose step running is enough."""
    spec, traces = _spec_from(["real", "real"])
    variant = FlowStep(template_id="T-process-v2", label="process (other wording)",
                       presence=0.5, required=False, critical=True,
                       critical_source="model")
    spec.steps.append(variant)
    report, _ = ConformanceEngine().check("t-0", traces["t-0"], spec)
    assert report.verdict == "achieved", \
        "a run with ONE purpose variant present was condemned for lacking the other"


def test_no_critical_steps_means_unknown_not_a_guess():
    spec, traces = _spec_from(["real", "real"])
    for step in spec.steps:
        step.critical = False
    report, _ = ConformanceEngine().check("t-0", traces["t-0"], spec)
    assert report.verdict == "unknown"
    # The WORDING is user-facing and has been rewritten for the page; what
    # must hold is that it says nobody has defined the purpose, rather than
    # implying the run itself did something wrong.
    assert "FOR" in report.reason or "cannot be judged" in report.reason
    assert "fail" not in report.reason.lower()


def test_optional_steps_absent_produce_no_noise():
    spec, traces = _spec_from(["real", "hollow", "hollow", "hollow"])
    spec.step("T-process").critical = True
    report, _ = ConformanceEngine().check("t-0", traces["t-0"], spec)
    assert report.deviations == []


# --- Modes -------------------------------------------------------------------

def test_shadow_mode_records_but_never_signals():
    """Conformance alerting from day one is in the anti-pattern table."""
    spec, _ = _spec_from(["real", "real"])
    report, signals = ConformanceEngine(mode="shadow").check(
        "t-h", _call("hollow", 9), spec)
    assert report.verdict == "hollow"
    assert signals == []


def test_enforce_mode_signals_p2_with_the_trace_attached():
    spec, _ = _spec_from(["real", "real"])
    _, signals = ConformanceEngine(mode="enforce").check(
        "t-h", _call("hollow", 9), spec)
    assert len(signals) == 1
    assert signals[0].detector == "ConformanceRate"
    assert signals[0].trace_id == "t-h"


# --- The model step ----------------------------------------------------------

def test_model_may_only_flag_steps_that_exist():
    """The FlowSynthesizer invents nothing: an unknown step id in its reply
    is dropped, not created."""
    spec, _ = _spec_from(["real", "real"])
    for step in spec.steps:
        step.critical = False

    class FakeRouter:
        def chat(self, *a, **k):
            return ('{"critical_step_ids": ["T-process", "T-imaginary"],'
                    ' "flow_purpose": "x"}')

    summary = synthesize_critical(spec, FakeRouter())
    assert spec.step("T-process").critical is True
    assert spec.step("T-process").critical_source == "model"
    assert "1 critical step" in summary
    assert all(s.template_id != "T-imaginary" for s in spec.steps)


def test_model_unavailable_leaves_the_spec_for_humans():
    spec, _ = _spec_from(["real", "real"])
    for step in spec.steps:
        step.critical = False

    class DownRouter:
        def chat(self, *a, **k):
            return None

    summary = synthesize_critical(spec, DownRouter())
    assert "human" in summary
    assert not any(s.critical for s in spec.steps)


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
    print(f"\n{'FAILED' if failures else 'All conformance tests passed'}"
          f"{f' ({failures} failing)' if failures else ''}")
    sys.exit(1 if failures else 0)
