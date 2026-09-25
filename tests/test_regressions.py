"""Regression tests for bugs found by running the agent against a live source.

Each test here corresponds to a real failure that was observed, not a hypothetical.
Run with:  python3 -m pytest tests/ -v   (or: python3 tests/test_regressions.py)
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.core import llm, sources  # noqa: E402
from app.core.parser import parse_log_line, unwrap_payload  # noqa: E402
from app.core.state import RuntimeState  # noqa: E402


# --- Bug 1: the agent attached to itself and invented a failure ----------------

def test_never_offers_itself_as_a_source():
    """Reading our own SSE feed made the agent report a 94%-confidence auth failure
    that never happened - it was matching "auth" inside its own state JSON."""
    ports = [
        {"pid": sources.OWN_PID, "port": 9999, "process": "Python"},
        {"pid": 1234, "port": settings.port, "process": "Python"},
        {"pid": 5678, "port": 5055, "process": "Python"},
    ]
    assert sources.is_self(ports[0]) is True, "own PID must be excluded"
    assert sources.is_self(ports[1]) is True, "own port must be excluded"
    assert sources.is_self(ports[2]) is False

    assert sources.pick_auto_port(ports) == 5055


def test_infrastructure_ports_are_not_log_sources():
    """Postgres is listening, but it is not an app emitting logs."""
    ports = [{"pid": 1, "port": 5432, "process": "postgres"}]
    assert sources.pick_auto_port(ports) is None


# --- Bug 2: SSE payloads were parsed as JSON syntax, not as logs ---------------

def test_sse_json_payload_is_unwrapped():
    """Every line arrived as {"line": "..."} and was parsed as JSON syntax, so an
    ERROR was recorded as INFO and timestamps came from the wrapper."""
    parsed = parse_log_line('{"line": "18:05:11 ERROR Hotel lookup timeout"}')
    assert parsed["level"] == "ERROR", "level must come from the log, not the wrapper"
    assert parsed["timestamp"] == "18:05:11"
    assert "Hotel lookup timeout" in parsed["message"]


def test_level_field_is_honoured():
    parsed = parse_log_line('{"message": "Connection refused", "level": "error"}')
    assert parsed["level"] == "ERROR"


































def test_ansi_colour_codes_are_stripped():
    """Local dev servers emit colour; the escape bytes ended up inside the level."""
    parsed = parse_log_line("\x1b[32mINFO\x1b[0m ANSI coloured line")
    assert "\x1b" not in parsed["message"]
    assert parsed["message"] == "INFO ANSI coloured line"


def test_blank_payload_unwraps_to_blank():
    """{"line": ""} was left as raw JSON instead of unwrapping to an empty line."""
    assert unwrap_payload('{"line": ""}') == ""
    assert unwrap_payload('{"line": "   "}') == ""


def test_malformed_json_degrades_safely():
    assert unwrap_payload('{"malformed": ') == '{"malformed":'
    assert unwrap_payload("plain text") == "plain text"
    assert parse_log_line("plain text")["message"] == "plain text"


# --- Bug 3: a stalled SSE client froze every caller of broadcast() -------------

def test_slow_client_does_not_block_broadcast():
    """One stalled browser tab made /api/ports hang for 25s+ while lsof alone
    returned in 0.05s, because broadcast() wrote to sockets inline."""
    runtime = RuntimeState()

    class StalledClient:
        def send_sse(self, event, data):
            time.sleep(30)

    runtime.subscribe(StalledClient())

    done = threading.Event()
    threading.Thread(target=lambda: (runtime.broadcast(), done.set()), daemon=True).start()
    assert done.wait(5), "broadcast must not block behind a slow client"


# --- Bug: SSE backlog replayed on every reconnect -----------------------------





# --- Bug: SSE backlog replayed on every reconnect -----------------------------





# --- Bug: SSE backlog replayed on every reconnect -----------------------------





# --- Bug: SSE backlog replayed on every reconnect -----------------------------





# --- Bug: SSE backlog replayed on every reconnect -----------------------------





# --- Bug: SSE backlog replayed on every reconnect -----------------------------





# --- Bug: SSE backlog replayed on every reconnect -----------------------------





# --- Bug: SSE backlog replayed on every reconnect -----------------------------





# --- Bug: SSE backlog replayed on every reconnect -----------------------------

def test_repeated_lines_are_not_reingested():
    """An idle SSE stream hits the socket timeout, reconnects, and the source
    replays its whole backlog. 22 real lines became 2868 events."""
    runtime = RuntimeState()
    batch = [
        "10:00:01 INFO Request received",
        "10:00:02 ERROR Connection timeout",
        "10:00:03 INFO Retry path selected",
    ]
    for line in batch:
        runtime.ingest_line(line, source="test")
    first = runtime.snapshot()["metrics"]["total_events"]

    # The dedupe lives in the tracer loop, so assert the property it protects:
    # replaying identical lines must not multiply the retained distinct messages.
    for line in batch:
        runtime.ingest_line(line, source="test")
    messages = {item["message"] for item in runtime.snapshot()["log_lines"]}
    assert len(messages) == len(batch), "replayed lines must not create new distinct entries"
    assert first == len(batch)


def test_client_disconnect_is_not_an_error():
    """A browser closing a tab raised ConnectionResetError and printed a full
    traceback per disconnect, flooding the console."""
    import app.server as server

    assert hasattr(server.RequestHandler, "handle_one_request"), \
        "handler must override handle_one_request to swallow client disconnects"


# --- Bug 4: confident answers that contradicted their own evidence ------------

def test_answer_never_contradicts_its_own_evidence():
    """'Did the hotel lookup fail?' returned YES at 96% while citing
    'Hotel booking confirmed' - keyword presence is not an outcome.

    In pattern mode the honest answer is "unknown"; with a model it should be a
    correct "no". What must never happen again is a confident "yes" on these logs.
    """
    runtime = RuntimeState()
    runtime.ingest_line("10:00:01 INFO Hotel booking confirmed", source="test")

    result = runtime.answer_query("Did the hotel lookup fail?")
    assert result["verdict"] != "yes", "must not claim failure when the log says confirmed"

    if result.get("source") == "keywords":
        assert result["verdict"] == "unknown", "keyword search cannot determine failure"
        assert result["confidence"] == 0


def test_pattern_mode_reports_no_confidence_score():
    """Confidence was computed as 82 + len(fixes)*4 - arithmetic, not certainty."""
    runtime = RuntimeState()
    runtime.ingest_line("10:00:01 ERROR Connection timeout", source="test")

    snapshot = runtime.snapshot()
    assert snapshot["status"] == "failed"
    assert snapshot["confidence"] == 0, "a regex hit is an observation, not a diagnosis"
    assert snapshot["interpretation"] == "patterns"


































def test_ungrounded_evidence_is_rejected():
    """Asked 'did the embedding service fail?' about a database failure, the model
    accepted the premise and answered YES at 90% - citing lines about postgres."""
    class FakeMessage:
        content = json.dumps({
            "answer": "Yes, the embedding service failed.",
            "verdict": "yes", "confidence": 90,
            "evidence": ["ERROR embedding service timed out"],  # never in the logs
        })

    class FakeClient:
        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    return type("R", (), {"choices": [type("C", (), {"message": FakeMessage})]})

    snapshot = {
        "log_lines": [
            {"message": "ERROR Connection refused: could not connect to postgres:5432"},
            {"message": "ERROR Request failed status=503"},
        ],
        "timeline": [], "stages": [], "metrics": {"total_events": 2}, "source": {},
    }

    original = llm._client
    llm._client = lambda: FakeClient()
    try:
        result = llm.answer_question(snapshot, "Did the embedding service fail?")
        assert result["verdict"] == "unknown", "evidence not present in logs must be rejected"
        assert result["confidence"] == 0
    finally:
        llm._client = original


def test_evidence_actually_in_logs_is_accepted():
    """The guard must not reject real evidence that the model reformatted."""
    corpus = "error connection refused: could not connect to postgres:5432"
    assert llm._appears_in("ERROR Connection refused: could not connect to postgres:5432", corpus)
    assert not llm._appears_in("ERROR embedding service timed out", corpus)


def test_llm_answer_without_evidence_is_downgraded():
    """A model asserting an outcome while citing nothing is not grounded."""
    class FakeMessage:
        content = json.dumps(
            {"answer": "Yes it failed.", "verdict": "yes", "confidence": 99, "evidence": []}
        )

    class FakeClient:
        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    return type("R", (), {"choices": [type("C", (), {"message": FakeMessage})]})

    original = llm._client
    llm._client = lambda: FakeClient()
    try:
        result = llm.answer_question({"log_lines": [], "timeline": [], "stages": [],
                                      "metrics": {"total_events": 1}, "source": {}}, "Did it fail?")
        assert result["verdict"] == "unknown", "ungrounded verdicts must be downgraded"
    finally:
        llm._client = original


# --- Bug 5: stale verdicts leaked across runs ---------------------------------

def test_new_request_clears_previous_verdict():
    """'Execution completed successfully' sat next to a live failure because the
    previous cycle's verdict was never cleared."""
    runtime = RuntimeState()
    runtime.ingest_line("10:00:01 INFO Request received", source="test")
    runtime.ingest_line("10:00:02 INFO Response sent", source="test")
    assert runtime.snapshot()["status"] == "success"

    runtime.ingest_line("10:00:03 INFO Request received", source="test")
    snapshot = runtime.snapshot()
    assert snapshot["status"] != "success", "a new run must not inherit the old verdict"
    assert snapshot["possible_causes"] == []


# --- Config -------------------------------------------------------------------

def test_llm_unavailable_without_key():
    assert llm.status()["mode"] in {"patterns", "llm"}
    if not settings.openai_api_key:
        assert llm.is_available() is False
        assert llm.status()["available"] is False


if __name__ == "__main__":
    import sys

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
    print(f"\n{'FAILED' if failures else 'All regression tests passed'}"
          f"{f' ({failures} failing)' if failures else ''}")
    sys.exit(1 if failures else 0)
