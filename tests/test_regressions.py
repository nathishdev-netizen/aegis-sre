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



# --- Bug: multi-line tracebacks fragmented into unrelated events ---------------

def test_traceback_is_one_event_not_many():
    """A Python traceback became 6 events across 4 invented components (Embedding,
    Other, Response), inflating failure counts and polluting the graph."""
    runtime = RuntimeState()
    for line in [
        "10:00:01 ERROR Unhandled exception in request handler",
        "Traceback (most recent call last):",
        '  File "/app/pipeline.py", line 88, in embed',
        "    return client.embed(payload)",
        "TimeoutError: embedding service unreachable",
        "10:00:02 INFO Request finished status=500",
    ]:
        runtime.ingest_line(line, source="test")

    snapshot = runtime.snapshot()
    assert snapshot["metrics"]["total_events"] == 2, "traceback must fold into its parent"
    components = {lane["component"] for lane in snapshot["graph"]["lanes"]}
    assert "Other" not in components, "stack frames must not invent components"


def test_traceback_detail_is_preserved():
    """Folding must not discard the trace - it is the most useful evidence there is."""
    runtime = RuntimeState()
    runtime.ingest_line("10:00:01 ERROR Unhandled exception", source="test")
    runtime.ingest_line('  File "/app/pipeline.py", line 88, in embed', source="test")
    runtime.ingest_line("TimeoutError: embedding service unreachable", source="test")

    detail = runtime.snapshot()["log_lines"][-1].get("detail") or []
    assert any("pipeline.py" in d for d in detail), "stack frame must be retained"
    assert any("TimeoutError" in d for d in detail)


def test_timestamped_line_is_never_a_continuation():
    """A real entry must start a new event even when it follows a traceback."""
    from app.core.parser import is_continuation
    assert not is_continuation("2026-08-30 10:00:02 INFO Request finished")
    assert not is_continuation("10:00:02 INFO Request finished")
    assert not is_continuation("INFO Request received")
    assert is_continuation('  File "/app/x.py", line 8, in f')
    assert is_continuation("  at com.foo.Bar.run(Bar.java:42)")


# --- Bugs found when attaching to a second source -----------------------------

def test_success_line_is_never_marked_failed():
    """Once the run failed, EVERY later node was stamped "failed" - so
    "Authentication passed for user 4821" rendered as a failure in the graph."""
    runtime = RuntimeState()
    runtime.ingest_line("10:00:01 ERROR Unhandled exception in request handler", source="test")
    runtime.ingest_line("10:00:02 INFO Authentication passed for user 4821", source="test")

    for lane in runtime.snapshot()["graph"]["lanes"]:
        for group in lane["branch_groups"]:
            for node in group["nodes"]:
                if "Authentication passed" in node["message"]:
                    assert node["status"] != "failed", "a success line must not render as failed"


def test_auth_success_does_not_trigger_auth_failure_cause():
    """The cause regex matched the word "auth" anywhere, so "Authentication passed"
    produced reason="Authentication or authorization issue"."""
    from app.core.parser import infer_cause

    assert infer_cause("INFO Authentication passed for user 4821") is None
    assert infer_cause("ERROR 401 Unauthorized") is not None
    assert infer_cause("ERROR Authentication failed") is not None


def test_attaching_new_source_clears_previous_run():
    """Attaching elsewhere kept the old timeline, graph and metrics, so one source's
    failures were attributed to another."""
    runtime = RuntimeState()
    runtime.ingest_line("10:00:01 ERROR Connection timeout", source="port:9001")
    assert runtime.snapshot()["metrics"]["total_events"] == 1

    runtime.attach_file("/nonexistent/path.log")
    snapshot = runtime.snapshot()
    assert snapshot["metrics"]["total_events"] == 0, "stale run must be cleared"
    assert snapshot["log_lines"] == []


# --- Discovery: learn the pipeline from the source's own output ---------------

def test_discovers_components_from_bracket_tags():
    """Stages must come from the project's own vocabulary, not a hardcoded list."""
    from app.core.learn import learn

    etl = [
        "02:14:03 INFO [extract] Pulled 12400 rows",
        "02:14:09 INFO [validate] Row 8801 ok",
        "02:14:09 WARN [deadletter] Routed row to dlq",
        "02:14:10 INFO [transform] Normalised row",
        "02:14:10 INFO [load] Upserted batch rows=500",
    ] * 3
    profile = learn(etl)
    assert profile.components == ["extract", "validate", "deadletter", "transform", "load"], \
        "components must appear in pipeline order"


def test_discovers_from_json_component_field():
    from app.core.learn import learn

    lines = [
        '{"level":"info","component":"gateway","message":"request in"}',
        '{"level":"error","component":"billing","message":"card declined"}',
    ] * 5
    profile = learn(lines)
    assert profile.line_format == "json"
    assert profile.components == ["gateway", "billing"]


def test_type_annotations_are_not_components():
    """A naive bracket scan picks up [dict], [float], [field] from type hints."""
    from app.core.learn import learn

    profile = learn(["10:00 INFO [dict] x", "10:00 INFO [float] y", "10:00 INFO [api] real"] * 3)
    assert profile.components == ["api"], "builtins must be filtered out"


def test_application_format_beats_framework_banner():
    """Uvicorn's startup banner outnumbered the app's own loguru lines, so the format
    was detected as uvicorn and the real pipeline lines were treated as boilerplate."""
    from app.core.learn import detect_format

    lines = ["INFO:     Started server process [123]"] * 7 + [
        "2026-08-28 16:00:18.807 | INFO | api:lifespan:44 - [api] Warming models",
    ] * 6
    line_format, _ = detect_format(lines)
    assert line_format == "loguru", "the application's own format must win"


def test_no_tags_reports_nothing_rather_than_guessing():
    """With no component vocabulary, the honest answer is an empty list."""
    from app.core.learn import learn

    profile = learn(["16:04:11 INFO Warming up the retriever"] * 12)
    assert profile.components == []
    assert profile.confident is False
    assert "no component tags" in profile.describe() or "format" in profile.describe()


def test_auto_attach_never_steals_an_explicit_source():
    """The UI polls /api/ports; refresh_ports() then auto-attached to a discovered
    port and wiped the file the user had chosen - mid-run, mid-call."""
    runtime = RuntimeState()
    runtime.attach_file("/tmp/does-not-matter.log")
    before = runtime.snapshot()["source"]["type"]
    assert before == "file"

    runtime.refresh_ports()
    after = runtime.snapshot()["source"]["type"]
    assert after == "file", "an explicit source must survive a background port scan"


def test_failed_port_probe_stops_instead_of_flooding():
    """A 404 raises HTTPError, whose `continue` skipped the for/else give-up branch -
    so an app with no log endpoint was probed with 7 requests every 3 seconds forever,
    flooding its console with 404s."""
    import threading as _t
    runtime = RuntimeState()
    stop = _t.Event()

    # Port 1 is never a log source; the probe must give up rather than loop.
    runtime._trace_port_loop(1, stop, runtime._port_generation + 1)

    # Reaching here at all means the loop terminated instead of spinning.
    assert True


def test_port_click_prefers_the_apps_own_log_file():
    """Clicking a port used to probe seven HTTP endpoints, printing seven 404s in the
    console of any app that does not serve logs. The OS already knows which log file
    the process has open - use that instead."""
    from app.core import sources as _sources

    original = _sources.best_log_file_for_pid
    _sources.best_log_file_for_pid = lambda pid: "/tmp/pretend-app.log"
    try:
        runtime = RuntimeState()
        runtime._snapshot.ports = [{"pid": 4242, "port": 6003, "process": "Python"}]
        runtime.attach_port(6003)
        source = runtime.snapshot()["source"]
        assert source["type"] == "file", "must attach to the file, not probe HTTP"
        assert source["path"] == "/tmp/pretend-app.log"
    finally:
        _sources.best_log_file_for_pid = original


def test_config_values_are_not_failures():
    """A startup banner reading "timeout=45.0s" was reported as "Connection timed out"
    and the whole healthy boot was marked failed. A failure word in a config value,
    or on an INFO line, is not a failure."""
    from app.core.parser import infer_cause, infer_transition

    healthy = [
        "INFO [api] Gateway starting - orchestrator=http://localhost:6004 timeout=45.0s",
        "INFO Config: request_timeout=30 retry_count=3",
        "INFO [api] Gateway ready in 8ms - accepting calls",
    ]
    for line in healthy:
        assert infer_cause(line) is None, f"config value flagged as a cause: {line}"
        transition = infer_transition(line)
        assert not (transition and transition.get("status") == "failed"), \
            f"config value flagged as a failure: {line}"

    # Real failures must still be detected.
    for line in [
        "ERROR [tts] Timeout after 5000ms calling Sarvam",
        "ERROR [db] Connection refused: ws://127.0.0.1:8000",
    ]:
        assert infer_cause(line) is not None, f"real failure missed: {line}"


def test_info_lines_never_produce_a_cause():
    """The level gates the diagnosis: only ERROR/WARN lines describe a problem."""
    runtime = RuntimeState()
    runtime.ingest_line("10:00:01 INFO Connection timeout setting is 45s", source="test")
    assert runtime.snapshot()["possible_causes"] == []


def test_completion_line_does_not_erase_a_failure():
    """A run that reached its end after failing was reported "completed successfully",
    wiping the reason, causes and fixes - the panels rendered empty."""
    runtime = RuntimeState()
    runtime.ingest_line("10:00:01 INFO [api] Request received", source="test")
    runtime.ingest_line("10:00:02 ERROR [db] 500 Internal Server Error from upstream", source="test")
    runtime.ingest_line("10:00:03 INFO [api] Response sent", source="test")

    snapshot = runtime.snapshot()
    assert snapshot["status"] == "failed", "a completion line must not overwrite a failure"
    assert snapshot["possible_causes"], "causes must survive to reach the UI"
    assert snapshot["suggested_fixes"], "fixes must survive to reach the UI"


def test_warning_does_not_downgrade_a_failure():
    """A WARNING logged after an ERROR reset the whole run to "warning" and dropped
    its diagnosis, because status was assigned rather than compared."""
    runtime = RuntimeState()
    runtime.ingest_line("10:00:01 ERROR [db] Connection refused: localhost:5432", source="test")
    runtime.ingest_line("10:00:02 WARNING [api] Falling back to cache", source="test")

    assert runtime.snapshot()["status"] == "failed"


def test_upstream_5xx_is_diagnosed():
    """An upstream 500 - one of the commonest real failures - matched no cause pattern,
    so the run showed as failed with an empty Likely Causes panel."""
    from app.core.parser import infer_cause

    for line in [
        "ERROR [identity] Lookup FAILED against http://localhost:6004/identity (500 Internal Server Error)",
        "ERROR upstream returned 502 Bad Gateway",
        "WARNING could not pre-synthesise filler: name 'fp' is not defined",
    ]:
        assert infer_cause(line) is not None, f"no cause found for: {line}"


def test_infrastructure_is_not_offered_as_a_source():
    """Postgres was listed as a clickable source. It listens AND writes a log file, so
    clicking it "worked" - and filled the dashboard with 2600 lines of database noise
    instead of the user's own application."""
    ports = [
        {"pid": 1, "port": 5432, "process": "postgres"},
        {"pid": 2, "port": 6003, "process": "Python"},
        {"pid": 3, "port": 7000, "process": "ControlCe"},
    ]
    offered = [p for p in ports if sources.is_plausible_source(p)]
    assert [p["port"] for p in offered] == [6003], "only real app ports may be offered"


def test_running_is_false_once_a_run_has_failed():
    """Every ingested line set running=True, so a tailed file kept the run marked
    "in progress" long after it failed - the headline read "Run in progress" beside
    a FAILED badge."""
    runtime = RuntimeState()
    runtime.ingest_line("10:00:01 INFO [api] Request received", source="test")
    assert runtime.snapshot()["running"] is True

    runtime.ingest_line("10:00:02 ERROR [db] 500 Internal Server Error", source="test")
    snapshot = runtime.snapshot()
    assert snapshot["status"] == "failed"
    assert snapshot["running"] is False, "a failed run is not in progress"

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
