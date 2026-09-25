"""Phase 1 regression tests - correlation.

Same rule as every other suite here: each test is named for a real failure
observed against a real log file, or for the specific dishonesty it prevents.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aegis.contracts.events import Event  # noqa: E402
from aegis.l2_normalization.normalizer import Normalizer  # noqa: E402
from aegis.l2_normalization.tracelinker import TraceLinker  # noqa: E402


def _event(text: str, ts: str = "2026-09-01 19:00:00") -> Event:
    return Event(id="E", ts=ts, service="svc", level="INFO", text_redacted=text)


def test_named_key_is_extracted_as_evidence():
    linker = TraceLinker()
    event = linker.link(_event("[api] CALL START call=abc12345-def0 - resolving"))
    assert event.trace_id == "abc12345-def0"
    assert event.correlation_basis == "extracted"


def test_unkeyed_line_is_inferred_when_one_session_is_open():
    """89% of the reference log's lines never name their call. With exactly one
    session open they can only belong to it - but as an assumption, not fact."""
    linker = TraceLinker()
    linker.link(_event("CALL START call=abc12345-def0"))
    event = linker.link(_event("[tts] Synthesised 60 chars in 1136ms"))
    assert event.trace_id == "abc12345-def0"
    assert event.correlation_basis == "inferred"


def test_no_guess_when_two_sessions_are_open():
    """The reference service handles one call at a time, which makes inference
    look safe. Production overlaps - and attributing a line to the wrong
    customer's trace is worse than attributing it to none."""
    linker = TraceLinker()
    linker.link(_event("CALL START call=abc12345-def0"))
    linker.link(_event("CALL START call=fff99999-1111"))
    event = linker.link(_event("[tts] Synthesised 60 chars in 1136ms"))
    assert event.trace_id == ""
    assert event.correlation_basis == "none"


def test_no_guess_when_nothing_is_open():
    """Startup banners and warmup belong to no call, and must not be glued to
    whichever call happens to come first."""
    linker = TraceLinker()
    event = linker.link(_event("[api] Gateway ready in 8ms - accepting calls"))
    assert event.trace_id == ""
    assert event.correlation_basis == "none"


def test_call_end_closes_the_session():
    """Lines after a CALL END are between calls; inferring them into the call
    that just finished would stretch every trace to the next call's start."""
    linker = TraceLinker()
    linker.link(_event("CALL START call=abc12345-def0"))
    ended = linker.link(_event("CALL END call=abc12345-def0 - 16s"))
    # The closing line itself still belongs to the call...
    assert ended.trace_id == "abc12345-def0"
    # ...but nothing after it does.
    event = linker.link(_event("[filler] cache tidy"))
    assert event.correlation_basis == "none"


def test_crashed_session_expires_instead_of_living_forever():
    """A crash never logs its END. Without a timeout the session stays open
    forever and every later unkeyed line is inferred into a call that finished
    an hour ago."""
    linker = TraceLinker(idle_timeout_s=300)
    linker.link(_event("CALL START call=abc12345-def0", ts="2026-09-01 19:00:00"))
    event = linker.link(_event("[tts] warmup", ts="2026-09-01 19:20:00"))
    assert event.correlation_basis == "none"


def test_timestampless_line_inherits_instead_of_fabricating():
    """A raw dump title with no timestamp was stamped with the wall-clock time
    of the analysis run - 10:54:24 appeared inside a 19:02 call, a time that
    exists nowhere in the file. Evidence must never contain invented values."""
    normalizer = Normalizer(service="svc")
    events = list(normalizer.feed_all([
        "2026-09-01 19:03:29 INFO voice: hangup received",
        "┌─ PLIVO → /voice/hangup",
        "2026-09-01 19:03:30 INFO voice: goodbye",
    ]))
    dump = next(e for e in events if e.text_redacted.startswith("┌"))
    assert dump.ts == "19:03:29", f"fabricated or wrong ts: {dump.ts!r}"


def test_generic_trace_id_outranks_app_call_id():
    """When a service emits real trace ids, they are the join key - the app's
    own call id is the fallback, not the winner."""
    linker = TraceLinker()
    event = linker.link(_event("trace_id=11112222aaaa call=abc12345-def0 step ok"))
    assert event.trace_id == "11112222aaaa"


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
    print(f"\n{'FAILED' if failures else 'All Phase 1 tests passed'}"
          f"{f' ({failures} failing)' if failures else ''}")
    sys.exit(1 if failures else 0)
