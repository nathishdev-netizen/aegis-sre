"""Dashboard server tests - the API the page renders from.

No HTTP and no model calls: AegisApp is exercised directly, which is the
whole surface the handlers delegate to.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aegis.server import AegisApp, WEB_DIR  # noqa: E402


def _app(lines: str) -> AegisApp:
    log = Path(tempfile.mkdtemp()) / "svc.log"
    log.write_text(lines)
    app = AegisApp("svc-test", str(log))
    app._stop.set()          # no background polling in tests
    app._thread.join(timeout=2)
    app.pipeline.run_once()
    app.pipeline.drain()
    return app


CALLS = "".join(
    f"2026-09-01 10:{m:02d}:00 INFO voice: [api] CALL START call=aaaa000{m}-1111\n"
    f"2026-09-01 10:{m:02d}:01 INFO voice: greeting done in 900ms\n"
    f"2026-09-01 10:{m:02d}:05 INFO voice: processing request reply sent\n"
    f"2026-09-01 10:{m:02d}:20 INFO voice: [api] CALL END call=aaaa000{m}-1111 - 20s\n"
    for m in range(4))


def test_state_carries_everything_the_page_renders():
    app = _app(CALLS)
    state = app.state()
    # "gaps" belongs to a FIX RESULT, not to state - the page reads it from
    # the proposal reply. Asserting it here outlived the API it described.
    for key in ("project", "funnel", "verdicts", "incidents", "notes",
                "templates", "patterns", "spec", "model", "updated_at",
                "events"):
        assert key in state, f"page would render without {key!r}"
    assert state["funnel"][0][1] == 16     # log lines
    assert state["model"]["calls_made"] == 0, "state() must never spend a model call"
    app.close()


def test_verdicts_are_unknown_until_purpose_is_marked():
    """An unmarked spec must yield unknown - never a guess - and the page is
    told why, so it can point at the mark-purpose button."""
    app = _app(CALLS)
    state = app.state()
    assert state["spec"]["exists"]
    assert not state["spec"]["purpose_marked"]
    assert state["verdicts"], "no verdicts at all"
    assert all(v["verdict"] == "unknown" for v in state["verdicts"])
    app.close()


def test_explaining_a_missing_incident_is_an_error_not_a_crash():
    app = _app(CALLS)
    result = app.explain("INC-does-not-exist")
    assert result["ok"] is False
    app.close()


def test_the_page_ships_with_the_server():
    page = (WEB_DIR / "index.html").read_text()
    assert "api/state" in page
    assert "Run verdicts" in page


def test_stream_events_are_redacted():
    """The live-stream panel shows what the pipeline stored - which must be
    the redacted form, or the browser becomes the leak."""
    app = _app("2026-09-01 10:00:00 INFO voice: CALL from=916360722483\n" * 5)
    events = app.state()["events"]
    assert events
    assert all("916360722483" not in e["text"] for e in events)
    assert any("<PHONE>" in e["text"] for e in events)
    app.close()


def test_ask_rejects_empty_questions_without_spending():
    """`ask` is now `explain`; the guarantee it protects is unchanged - a
    blank input must not cost a model call."""
    app = _app(CALLS)
    before = app.router.budget.calls_made
    result = app.explain("   ")
    assert result["ok"] is False
    assert app.router.budget.calls_made == before, "an empty question cost a call"
    app.close()


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
    print(f"\n{'FAILED' if failures else 'All server tests passed'}"
          f"{f' ({failures} failing)' if failures else ''}")
    sys.exit(1 if failures else 0)
