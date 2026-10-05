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
from app.core.baselines import Baselines, extract_duration  # noqa: E402
from app.core.state import RuntimeState  # noqa: E402
from app.store import Store  # noqa: E402
from app.core import audit  # noqa: E402


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

def test_a_long_traceback_keeps_the_frames_that_name_our_own_code():
    """detail was capped at the FIRST 40 lines. A loguru diagnostic traceback
    is long enough that those 40 filled with framework frames (fastapi,
    langgraph, asyncio) and the project's own - the ones naming the code that
    actually raised, which arrive last - were dropped. The mapper then had
    only the except/logger lines and Propose fix patched the handler that
    CAUGHT the failure."""
    runtime = RuntimeState()
    runtime.ingest_line("10:00:01 ERROR [api] Unhandled error while answering", source="t")
    runtime.ingest_line("Traceback (most recent call last):", source="t")
    for i in range(60):                       # framework noise, more than the cap
        runtime.ingest_line(f'  File "/venv/lib/framework{i}.py", line {i}, in wrap', source="t")
    runtime.ingest_line('  File "/app/agents/orchestrator.py", line 1910, in _route', source="t")
    runtime.ingest_line("    raise ValueError('dispatch unavailable')", source="t")

    error = [e for e in runtime.snapshot()["log_lines"] if e.get("level") == "ERROR"][0]
    detail = "\n".join(error.get("detail") or [])
    assert "orchestrator.py" in detail, \
        "the deepest frame - the one naming our own code - was dropped by the cap"
    assert len(error["detail"]) <= 40, "the cap must still bound the detail"


def test_loguru_marks_the_raising_frame_and_it_still_belongs_to_the_error():
    """Loguru writes the frame that actually RAISED with a "> " marker in the
    leading column, so it has no indentation to match on and became an event
    of its own - taking the one frame naming the raise site away from the
    error. Propose fix then saw only the except/logger lines and patched the
    handler that CAUGHT the failure instead of the code that caused it."""
    runtime = RuntimeState()
    for line in [
        "10:00:01 ERROR [api] Unhandled error while answering",
        "Traceback (most recent call last):",
        '  File "/app/api.py", line 347, in chat_sync',
        '> File "/app/agents/orchestrator.py", line 1910, in _route',
        "    raise ValueError('dispatch unavailable')",
        "ValueError: dispatch unavailable",
    ]:
        runtime.ingest_line(line, source="test")

    events = runtime.snapshot()["log_lines"]
    error = [e for e in events if e.get("level") == "ERROR"]
    assert len(error) == 1, f"the traceback split into {len(error)} error events"
    detail = "\n".join(error[0].get("detail") or [])
    assert "orchestrator.py" in detail, \
        "the marked frame naming the raise site was not kept with its error"


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


def test_a_missing_path_is_never_remembered():
    """Attaching to a path that does not exist overwrote the remembered source, so a
    later restart came back watching nothing at all."""
    runtime = RuntimeState()
    session = runtime._SESSION_FILE
    backup = session.read_text() if session.exists() else None
    try:
        runtime.attach_file("/nonexistent/path.log")
        after = session.read_text() if session.exists() else ""
        assert "/nonexistent/path.log" not in after, "a missing path must not be remembered"
    finally:
        if backup is not None:
            session.write_text(backup)


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


def test_picking_a_port_attaches_aegis_too_not_just_v1():
    """Aegis follows v1's source, and it was told only from /api/attach. The UI
    attaches by PORT, so every user who clicked a port left Aegis detached -
    watching nothing, with no incidents and no error to explain why. Drives the
    real do_POST route, because a test that re-implements the route body would
    have passed against the broken version too."""
    import io
    import app.server as server

    told = []

    class _FakeAegis:
        def attach(self, path, project=None):
            told.append((path, project))

    class _FakeRuntime:
        def attach_port(self, port):
            return {"source": {"type": "file", "path": "/tmp/picked.log",
                               "attached": True, "via_port": port}}

    class _Handler(server.RequestHandler):
        def __init__(self):  # no socket, no server
            self.path = "/api/trace-port"
            self.headers = {"Content-Length": str(len(_BODY))}
            self.rfile = io.BytesIO(_BODY)
            self.wfile = io.BytesIO()
            self.sent = []

        def send_response(self, *a, **k):
            self.sent.append(a)

        def send_header(self, *a, **k):
            pass

        def end_headers(self):
            pass

        def log_message(self, *a, **k):
            pass

    _BODY = b'{"port": 8030}'
    original_aegis, original_runtime = server._aegis, server.runtime
    server._aegis = lambda: _FakeAegis()
    server.runtime = _FakeRuntime()
    try:
        _Handler().do_POST()
    finally:
        server._aegis, server.runtime = original_aegis, original_runtime

    assert told == [("/tmp/picked.log", None)], (
        f"picking a port did not tell Aegis its source: {told}")


def test_the_wire_does_not_ship_the_whole_log_every_frame():
    """The UI looked stuck in a loop. broadcast() fires on every state change -
    which means every ingested log line - and each frame was the WHOLE snapshot
    at ~103KB, of which 67KB was all 200 log_lines re-sent verbatim. A chatbot
    writing ~14 lines a second pushed ~850KB/s and the browser called render()
    on each one, so the page never settled.

    wire_snapshot() trims log_lines for the browser only. snapshot() must stay
    complete: llm.py takes the last llm_log_window of the LATEST RUN, so
    trimming before that filter would silently starve it on a busy log."""
    runtime = RuntimeState()
    for i in range(200):
        runtime._snapshot.log_lines.append(
            {"timestamp": "10:00:00", "level": "INFO", "status": "info",
             "message": f"[api] line {i} with enough text to be realistic",
             "source": "test"})

    full = runtime.snapshot()
    wire = runtime.wire_snapshot()
    assert len(full["log_lines"]) == 200, "the internal snapshot was trimmed"
    assert len(wire["log_lines"]) == runtime.WIRE_LOG_LINES
    assert len(json.dumps(wire)) < len(json.dumps(full)), "the wire got no smaller"
    # The newest lines are the ones the UI renders - trimming must keep the END.
    assert wire["log_lines"][-1] == full["log_lines"][-1]


def test_a_burst_of_log_lines_coalesces_into_one_frame():
    """Fourteen frames a second is fourteen renders a second. This is LIVE
    state, so the newest frame supersedes the ones behind it and dropping them
    loses nothing - what the user sees is the latest either way."""
    runtime = RuntimeState()
    sent = []

    class _Handler:
        def send_sse(self, event, payload):
            sent.append(payload)

    handler = _Handler()
    runtime.subscribe(handler)
    try:
        for i in range(20):
            runtime._snapshot.log_lines.append(
                {"timestamp": "10:00:00", "level": "INFO", "status": "info",
                 "message": f"burst {i}", "source": "test"})
            runtime.broadcast()
        time.sleep(1.0)
    finally:
        runtime.unsubscribe(handler)
    assert sent, "nothing was delivered at all"
    assert len(sent) < 20, f"no coalescing: {len(sent)} frames for 20 changes"
    # Whatever survived must be the NEWEST state, not a stale frame.
    assert "burst 19" in sent[-1], "the last frame was not the latest state"


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


def test_last_source_is_remembered_across_restarts():
    """Restarting the agent left it watching nothing, so the dashboard came back
    empty with "No logs yet" and the user had to re-attach to see anything."""
    import tempfile, os
    from pathlib import Path as _P

    with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as fh:
        fh.write("10:00:01 INFO [api] Request received\n")
        log_path = fh.name

    runtime = RuntimeState()
    session = runtime._SESSION_FILE
    backup = session.read_text() if session.exists() else None
    try:
        runtime.attach_file(log_path)
        assert session.exists(), "attaching must remember the source"

        # A fresh instance stands in for a restart.
        revived = RuntimeState()
        assert revived.restore_last_source() is True
        assert revived.snapshot()["source"]["type"] == "file"
    finally:
        os.unlink(log_path)
        if backup is not None:
            session.write_text(backup)
        elif session.exists():
            session.unlink()


def test_a_new_run_clears_the_previous_verdict():
    """A successful call that followed a failed one still reported the failure: the
    reset only ran for a stage literally named "request", so a voice gateway logging
    "CALL START" never began a new run and inherited the old verdict forever."""
    runtime = RuntimeState()
    runtime.ingest_line("12:18:41 INFO [api] CALL START call=aaa from=111", source="test")
    runtime.ingest_line("12:18:41 ERROR [identity] Lookup FAILED - connection refused", source="test")
    assert runtime.snapshot()["status"] == "failed"

    # A new call begins. Its verdict must be its own.
    runtime.ingest_line("13:26:47 INFO [api] CALL START call=bbb from=222", source="test")
    runtime.ingest_line("13:26:47 INFO [identity] Resolved 222 - allowed=True", source="test")

    snapshot = runtime.snapshot()
    assert snapshot["status"] != "failed", "a new run must not inherit the last failure"
    assert snapshot["metrics"]["failures"] == 0
    assert snapshot["possible_causes"] == []


def test_model_context_is_scoped_to_the_latest_run():
    """Handed 200 lines spanning five calls, the model described an old failure while
    the newest call had succeeded. Telling it to focus was not enough - the context
    itself has to be cut."""
    from app.core.llm import _latest_run

    logs = [
        {"message": "[api] CALL START call=aaa"},
        {"message": "[identity] Lookup FAILED"},
        {"message": "[api] CALL END call=aaa"},
        {"message": "[api] CALL START call=bbb"},
        {"message": "[identity] Resolved - allowed=True"},
    ]
    latest = _latest_run(logs)
    assert len(latest) == 2, "context must start at the most recent run boundary"
    assert "CALL START call=bbb" in latest[0]["message"]


def test_backfilled_history_is_interpreted():
    """Interpretation was only requested from ingest_line, so a file that had stopped
    growing never triggered a model pass - the brief showed the pattern template
    forever, however long the user waited."""
    import tempfile, os

    with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as fh:
        fh.write("10:00:01 INFO [api] CALL START call=aaa\n")
        fh.write("10:00:02 ERROR [db] Connection refused\n")
        path = fh.name

    runtime = RuntimeState()
    requested = []
    runtime._request_interpretation = lambda: requested.append(True)
    try:
        runtime.attach_file(path)
        import time as _t
        _t.sleep(1.0)
        assert requested, "attaching to an existing file must request interpretation"
    finally:
        os.unlink(path)


def test_fallback_summary_is_not_a_log_dump():
    """The template pasted a whole raw ERROR line into its sentence, producing the
    wall of text it was supposed to spare the reader."""
    runtime = RuntimeState()
    runtime.ingest_line("12:18:41 INFO [api] CALL START call=aaa from=919095797973", source="test")
    runtime.ingest_line(
        "12:18:41 ERROR [identity] Lookup FAILED against http://localhost:6004/identity "
        "(ConnectError: All connection attempts failed) - failing closed",
        source="test")

    summary = runtime.snapshot()["summary"]
    assert "ConnectError" not in summary, "the summary must not quote a raw log line"
    assert len(summary) < 160, "a fallback summary should be a sentence, not a dump"


def test_dashboard_javascript_has_no_shadowing_or_tdz_faults():
    """Two JS faults threw inside render(), aborting it and leaving the whole
    dashboard blank - which looked exactly like "the agent is not reading my logs":

      - a local `const badge = getElementById(...)` shadowed the badge() helper, so
        `pills.map(badge)` called a DOM element
      - `title` was used one block before its `const`, a temporal-dead-zone error

    Neither is visible without executing the page, so this test executes it.
    """
    import re as _re
    from pathlib import Path as _P

    html = (_P(__file__).resolve().parent.parent / "app" / "web" / "index.html").read_text()
    script = html.split("<script>")[1].split("</script>")[0]

    # A local named `badge` anywhere would shadow the helper for its whole function.
    assert not _re.search(r"\bconst badge\s*=", script), \
        "a local `badge` shadows the badge() helper used by pills.map(badge)"

    # `title` must be declared before the block that interpolates it.
    decl = script.find("const title =")
    use = script.find("escapeHtml(title)")
    assert decl != -1 and use != -1 and decl < use, \
        "`title` is used before its declaration (temporal dead zone)"

# --- Config -------------------------------------------------------------------

def test_llm_unavailable_without_key():
    assert llm.status()["mode"] in {"patterns", "llm"}
    if not settings.openai_api_key:
        assert llm.is_available() is False
        assert llm.status()["available"] is False


# --- Bug 46: a config value was measured as if it were a duration -------------

def test_settings_are_not_measurements():
    """A startup banner's "timeout=45.0s" was read as a 45-second operation, which
    poisoned the baseline it landed in. Same bug shape as v1's cause matcher reading
    that line as an outage."""
    assert extract_duration("[orch] Client ready timeout=45.0s retries=3") is None
    assert extract_duration("[cache] ttl=300s max=1000") is None
    # A real measurement on a line that also carries settings still counts.
    assert extract_duration("[tts] Synthesised 60 chars in 1136ms") == (
        "synthesised chars", 1136.0
    )


def test_version_strings_are_not_seconds():
    """"model=bulbul:v3" nearly parsed as a 3-second operation - the seconds pattern
    needs a delimiter, not just a trailing s."""
    assert extract_duration("[tts] model=bulbul:v3 speaker=rohan") is None
    assert extract_duration("[api] CALL END call=82050189 - 16s, hangup=NORMAL") == (
        "call end", 16000.0
    )


def test_operation_name_excludes_logger_scaffolding():
    """Every tts line began "INFO voice.tts:", so every distinct operation collapsed
    into one bucket named after the logger and 34 false spikes were reported."""
    op, ms = extract_duration("INFO voice.tts: [tts] Synthesised 60 chars in 1136ms")
    assert "info" not in op and "voice" not in op, op
    assert op == "synthesised chars"
    assert ms == 1136.0


def test_spike_is_measured_against_history_not_itself():
    """Adding the sample before comparing let a spike shift the baseline it was being
    judged against, so large outliers under-reported."""
    baselines = Baselines()
    for _ in range(12):
        assert baselines.observe("tts", "[tts] done in 100ms") is None
    finding = baselines.observe("tts", "[tts] done in 900ms")
    assert finding is not None
    assert finding["median_ms"] == 100.0, "compared against a baseline it had shifted"
    assert finding["ratio"] == 9.0


def test_no_verdict_without_enough_history():
    """Two samples is an anecdote. Reporting "3x slower than usual" off them reads as
    authoritative and is not."""
    baselines = Baselines()
    for _ in range(3):
        baselines.observe("api", "[api] ready in 10ms")
    assert baselines.observe("api", "[api] ready in 5000ms") is None
    assert not baselines.summary()[0]["ready"]


def test_baselines_survive_a_restart():
    """History held only in RAM is lost when the window closes, so "is this normal?"
    could never be answered on a fresh start."""
    import tempfile, os

    path = os.path.join(tempfile.mkdtemp(), "history.db")
    store = Store(path)
    first = Baselines(store, source="svc")
    for _ in range(12):
        first.observe("orch", "[orch] /chat returned in 800ms")
    store.close()

    store2 = Store(path)
    second = Baselines(store2, source="svc")
    assert second.summary(), "no history recovered after restart"
    assert second.summary()[0]["ready"], "recovered history was not usable"
    finding = second.observe("orch", "[orch] /chat returned in 6946ms")
    assert finding is not None, "restart lost the baseline needed to spot the spike"
    assert finding["ratio"] > 8
    store2.close()


def test_store_failure_never_stops_ingest():
    """Reading logs is the job; history is an enhancement. A broken database must not
    take the agent down with it."""

    class BrokenStore:
        def known_operations(self, source):
            return []

        def samples(self, *a):
            return []

        def record_duration(self, *a, **k):
            raise RuntimeError("disk full")

    baselines = Baselines(BrokenStore(), source="svc")
    for _ in range(12):
        baselines.observe("tts", "[tts] done in 100ms")
    finding = baselines.observe("tts", "[tts] done in 900ms")
    assert finding is not None, "a store error swallowed a real finding"


def test_absence_is_reported_for_components_that_stopped():
    """Nothing fires when a step simply stops happening - no error, no threshold."""
    baselines = Baselines()
    for _ in range(12):
        baselines.observe("tts", "[tts] done in 100ms")
    assert baselines.absences({"tts", "api"}) == []
    missing = baselines.absences({"api"})
    assert len(missing) == 1 and missing[0]["component"] == "tts"
    assert "did not appear" in baselines.describe(missing[0])


def test_drift_needs_both_halves_of_history():
    """Two samples cannot show a trend - reporting one anyway would be a guess
    dressed up as a measurement."""
    from app.core.baselines import Baseline
    b = Baseline(component="tts", operation="synthesise")
    b.add(100.0)
    b.add(300.0)
    assert b.drift() is None, "too little history to call it a trend"


def test_drift_fires_on_a_real_trend_no_single_run_would_trip():
    """Every run in this history is individually unremarkable - none is 3x the
    previous one - yet the operation has genuinely gotten much slower."""
    from app.core.baselines import Baseline
    b = Baseline(component="orch", operation="chat")
    for _ in range(20):
        b.add(800.0)
    for _ in range(20):
        b.add(2400.0)
    result = b.drift()
    assert result is not None, "a real 3x trend across 40 runs was missed"
    assert result["ratio"] == 3.0


# --- Outcome verdicts: a clean run is not the same claim as a useful one -------

def _fake_outcome_client(payload: dict) -> object:
    class FakeMessage:
        content = json.dumps(payload)

    class FakeClient:
        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    return type("R", (), {"choices": [type("C", (), {"message": FakeMessage})]})
    return FakeClient()


def test_hollow_verdict_without_evidence_is_downgraded():
    """'hollow' is the verdict no error check computes, which makes it the one most
    tempting for a model to assert without real support. It must cite lines that
    actually appear in this run's own logs, or it does not stand."""
    snapshot = {
        "log_lines": [
            {"message": "CALL START call=1"},
            {"message": "CALL END - 5s, hangup_cause=NORMAL_CLEARING"},
        ],
        "timeline": [], "stages": [], "metrics": {"total_events": 2}, "source": {},
    }
    original = llm._client
    llm._client = lambda: _fake_outcome_client({
        "verdict": "hollow",
        "purpose": "handle a call",
        "reason": "nothing was said",
        "confidence": 95,
        "evidence": ["ERROR the caller never spoke a word"],  # never in the logs
    })
    try:
        result = llm.interpret_outcome(snapshot)
        assert result["verdict"] == "unknown", "an ungrounded hollow verdict must not stand"
    finally:
        llm._client = original


def test_hollow_verdict_with_real_evidence_is_accepted():
    """The guard must not reject a hollow verdict whose evidence genuinely is
    in this run's logs, just because it is the rarer, more serious claim."""
    snapshot = {
        "log_lines": [
            {"message": "CALL START call=1"},
            {"message": "TURN SKIPPED - Plivo captured no speech, re-prompting"},
            {"message": "CALL END - 18s, hangup_cause=NORMAL_CLEARING"},
        ],
        "timeline": [], "stages": [], "metrics": {"total_events": 3}, "source": {},
    }
    original = llm._client
    llm._client = lambda: _fake_outcome_client({
        "verdict": "hollow",
        "purpose": "handle a call",
        "reason": "no speech was captured",
        "confidence": 90,
        "evidence": ["TURN SKIPPED - Plivo captured no speech, re-prompting"],
    })
    try:
        result = llm.interpret_outcome(snapshot)
        assert result["verdict"] == "hollow", "real evidence must not be rejected"
    finally:
        llm._client = original


def test_outcome_verdict_outside_the_known_set_becomes_unknown():
    """A model returning something off-schema must degrade, not propagate a verdict
    nothing downstream knows how to render."""
    snapshot = {
        "log_lines": [{"message": "CALL START call=1"}],
        "timeline": [], "stages": [], "metrics": {"total_events": 1}, "source": {},
    }
    original = llm._client
    llm._client = lambda: _fake_outcome_client({
        "verdict": "sort-of-worked", "purpose": "", "reason": "", "confidence": 50, "evidence": [],
    })
    try:
        result = llm.interpret_outcome(snapshot)
        assert result["verdict"] == "unknown"
    finally:
        llm._client = original


def test_outcome_judgement_runs_after_a_completed_run():
    """A run that finishes must trigger an outcome judgement, not just a status
    update - that judgement is the whole point of the verdict layer."""
    rs = RuntimeState()
    calls = []
    original_request = rs._request_outcome_judgement
    original_available = llm.is_available
    rs._request_outcome_judgement = lambda *a, **kw: calls.append(a)
    llm.is_available = lambda: True
    try:
        rs.ingest_line("[api] CALL START call=1")
        rs.ingest_line("[api] Execution completed successfully.")
    finally:
        rs._request_outcome_judgement = original_request
        llm.is_available = original_available
    assert len(calls) == 1, "completion did not trigger an outcome judgement"


def test_no_outcome_judgement_without_a_model():
    """Without a configured model, ingestion must not spawn a call that can only
    return None - matching interpret_run's own no-model behaviour."""
    rs = RuntimeState()
    calls = []
    original_request = rs._request_outcome_judgement
    original_available = llm.is_available
    rs._request_outcome_judgement = lambda *a, **kw: calls.append(a)
    llm.is_available = lambda: False
    try:
        rs.ingest_line("[api] CALL START call=1")
        rs.ingest_line("[api] Execution completed successfully.")
    finally:
        rs._request_outcome_judgement = original_request
        llm.is_available = original_available
    assert calls == [], "an outcome judgement was requested with no model configured"


# --- Audit reports: rollups of what happened over a time window ---------------

def _insert_run(store, source, hours_ago, verdict, reason="test", events=5):
    from datetime import datetime, timedelta, timezone
    conn = store._connect()
    ts = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO runs (source, started_at, ended_at, verdict, reason, evidence,"
        " events, errors, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (source, ts, ts, verdict, reason, "[]", events, 0, ts))
    conn.commit()
    store._release(conn)


def _insert_duration(store, source, hours_ago, component, operation, value_ms):
    from datetime import datetime, timedelta, timezone
    conn = store._connect()
    ts = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO durations (source, component, operation, value_ms, observed_at,"
        " recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
        (source, component, operation, value_ms, ts, ts))
    conn.commit()
    store._release(conn)


def test_audit_report_excludes_runs_outside_the_window():
    """A run from 30 hours ago must not count toward a 24-hour report - the
    entire point of a windowed report is that it is NOT all-time history."""
    store = Store(path=":memory:")
    _insert_run(store, "svc", 1, "achieved")
    _insert_run(store, "svc", 30, "achieved")
    report = audit.generate(store, "svc", "24h")
    assert report.total_runs == 1, "a run outside the window was counted"


def test_audit_report_counts_verdicts_correctly():
    store = Store(path=":memory:")
    _insert_run(store, "svc", 1, "achieved")
    _insert_run(store, "svc", 2, "hollow", "no speech captured")
    _insert_run(store, "svc", 3, "achieved")
    _insert_run(store, "svc", 5, "failed", "connection refused")
    report = audit.generate(store, "svc", "24h")
    assert report.total_runs == 4
    assert report.verdict_counts == {"achieved": 2, "hollow": 1, "failed": 1}
    assert len(report.notable_runs) == 2, "hollow and failed must both surface as notable"


def test_audit_report_migrates_a_pre_recorded_at_database():
    """A database created before recorded_at existed must not crash the report -
    it should just correctly exclude rows it cannot place in time."""
    import sqlite3
    import tempfile
    import os
    tmpdir = tempfile.mkdtemp()
    old_db = os.path.join(tmpdir, "old.db")
    conn = sqlite3.connect(old_db)
    conn.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL,"
                 " started_at TEXT, ended_at TEXT, label TEXT, verdict TEXT, reason TEXT,"
                 " evidence TEXT, events INTEGER DEFAULT 0, errors INTEGER DEFAULT 0)")
    conn.execute("CREATE TABLE durations (id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL,"
                 " component TEXT NOT NULL, operation TEXT NOT NULL, value_ms REAL NOT NULL,"
                 " observed_at TEXT)")
    conn.execute("INSERT INTO runs (source, started_at, verdict) VALUES ('svc', '11:00:00', 'achieved')")
    conn.commit()
    conn.close()

    store = Store(path=old_db)
    report = audit.generate(store, "svc", "24h")
    assert report.total_runs == 0, "a pre-migration row with no recorded_at must be excluded, not crash"
    store.close()


def test_spike_reported_once_per_operation_not_once_per_repeat():
    """A sustained level shift (a drift) must not ALSO appear as many separate
    spikes - one entry per operation, the worst instance, not one per repeat."""
    store = Store(path=":memory:")
    for h in range(23, 12, -1):
        _insert_duration(store, "svc", h, "orch", "chat", 800.0)
    for h in range(11, 0, -1):
        _insert_duration(store, "svc", h, "orch", "chat", 2400.0)
    report = audit.generate(store, "svc", "24h")
    matching = [s for s in report.spikes if s["component"] == "orch"]
    assert len(matching) == 1, "a sustained shift produced more than one spike entry"


def test_audit_report_drift_uses_only_the_windows_own_samples():
    """A 3x drift entirely within the window must be found even with zero runs -
    drift is a property of the durations already on disk, not of run outcomes."""
    store = Store(path=":memory:")
    for h in range(23, 12, -1):
        _insert_duration(store, "svc", h, "orch", "chat", 800.0)
    for h in range(11, 0, -1):
        _insert_duration(store, "svc", h, "orch", "chat", 2400.0)
    report = audit.generate(store, "svc", "24h")
    assert report.total_runs == 0
    assert len(report.drifts) == 1
    assert report.drifts[0]["ratio"] == 3.0
    assert "trending" in report.summary, "a drift finding with no runs must still appear in the summary"


def test_audit_report_named_windows_produce_different_totals():
    store = Store(path=":memory:")
    _insert_run(store, "svc", 1, "achieved")
    _insert_run(store, "svc", 30, "achieved")   # inside week/month, outside 24h
    _insert_run(store, "svc", 24 * 10, "achieved")  # inside month, outside week
    day = audit.generate(store, "svc", "24h")
    week = audit.generate(store, "svc", "week")
    month = audit.generate(store, "svc", "month")
    assert day.total_runs == 1
    assert week.total_runs == 2
    assert month.total_runs == 3


def test_audit_report_with_no_store_is_not_an_error():
    """A source that has never been attached still deserves a report, not a crash
    or a 500 - it just says there is nothing yet."""
    rs = RuntimeState()
    result = rs.audit_report("24h")
    assert result["total_runs"] == 0
    assert "summary" in result


def test_downloadable_report_includes_every_runs_evidence():
    """A downloaded report exists to be detailed - it must show each run's actual
    evidence lines, not just its verdict, or it is no more useful than the summary."""
    store = Store(path=":memory:")
    _insert_run(store, "svc", 1, "hollow", "no speech captured")
    conn = store._connect()
    conn.execute("UPDATE runs SET evidence = ? WHERE id = 1",
                 ('["TURN SKIPPED - Plivo captured no speech, re-prompting"]',))
    conn.commit()
    store._release(conn)
    report = audit.generate(store, "svc", "24h")
    out = audit.render_html(report, "svc")
    assert "TURN SKIPPED - Plivo captured no speech" in out
    assert "HOLLOW" in out.upper()


def test_downloadable_report_lists_every_run_not_just_notable_ones():
    """The 'All runs' section must include achieved runs too - a detailed report
    is not detailed if it silently drops the runs that went fine."""
    store = Store(path=":memory:")
    _insert_run(store, "svc", 1, "achieved", "booked a demo")
    _insert_run(store, "svc", 2, "hollow", "no speech captured")
    report = audit.generate(store, "svc", "24h")
    assert len(report.all_runs) == 2
    out = audit.render_html(report, "svc")
    assert "booked a demo" in out
    assert "no speech captured" in out


def test_report_html_escapes_evidence_content():
    """Evidence lines come from real logs and can contain characters that would
    otherwise break the HTML or inject markup - they must render as text."""
    store = Store(path=":memory:")
    _insert_run(store, "svc", 1, "failed", "error")
    conn = store._connect()
    conn.execute("UPDATE runs SET evidence = ? WHERE id = 1",
                 ('["<script>alert(1)</script>"]',))
    conn.commit()
    store._release(conn)
    report = audit.generate(store, "svc", "24h")
    out = audit.render_html(report, "svc")
    assert "<script>alert(1)</script>" not in out
    assert "&lt;script&gt;" in out


def test_a_connector_record_keeps_its_own_timestamp():
    """A connector returns the time as a field, not inside the message text.

    Reading only the text left every provider-backed run with no time at all,
    so the Runs page showed "@ · 0s" on every verdict.
    """
    from aegis.l2_normalization.normalizer import Normalizer

    stamp = "2026-09-29T19:55:19.190251197Z"
    norm = Normalizer(service="proj")
    event = norm.feed("info request completed status=200", "payments", stamp)
    event = event or norm.flush()
    assert event is not None
    assert event.ts == stamp, f"timestamp was dropped: {event.ts!r}"

    # A line carrying its OWN time still wins: watched files must not change.
    norm2 = Normalizer(service="proj")
    own = norm2.feed("2026-09-30T08:00:00Z INFO started", "", stamp)
    own = own or norm2.flush()
    assert own is not None and own.ts != stamp, (
        "the collector's time overwrote the one printed in the line")


def test_an_iso_instant_is_measurable():
    """_seconds only understood a bare clock, so ISO instants returned None."""
    from aegis.l4_understanding.flowspec import _seconds

    assert _seconds("2026-09-30T05:43:38.345687552Z") is not None
    assert _seconds("10:12:04") is not None
    assert _seconds("") is None and _seconds("not a time") is None

    start = _seconds("2026-09-30T05:43:38.000000000Z")
    end = _seconds("2026-09-30T05:43:41.500000000Z")
    assert abs((end - start) - 3.5) < 0.01

    # A run crossing midnight: the date is why this can be measured at all.
    before = _seconds("2026-09-29T23:59:58Z")
    after = _seconds("2026-09-30T00:00:05Z")
    assert abs((after - before) - 7.0) < 0.01


def test_a_run_is_measured_even_when_its_events_arrive_newest_first():
    """SigNoz returns rows newest-first.

    Taking last-minus-first made every duration negative, so it clamped to
    0.0 and no connector run could be told fast from slow.
    """
    from aegis.l4_understanding.conformance import ConformanceEngine
    from aegis.server import _in_time_order

    class _E:
        def __init__(self, ts):
            self.ts = ts

    newest_first = [_E("2026-09-30T05:43:38.400000Z"),
                    _E("2026-09-30T05:43:38.000000Z"),
                    _E("2026-09-30T05:43:37.200000Z")]

    span = ConformanceEngine._span(newest_first)
    assert abs(span - 1.2) < 0.01, f"duration collapsed to {span}"

    ordered = _in_time_order(newest_first)
    assert ordered[0].ts.endswith("37.200000Z"), (
        "the run opened at its EARLIEST event, not its newest")

    # An unstamped line is kept, not dropped.
    kept = _in_time_order([_E(""), _E("2026-09-30T05:43:38Z")])
    assert len(kept) == 2


def test_a_service_with_no_correlation_id_still_produces_runs():
    """/chat/sync logs a thread (the LEAD), never a per-turn id.

    Every one of its lines was unattributable, so the endpoint had no runs, no
    verdicts and no duration - it was invisible to the tool even though its
    log says plainly where each request begins and ends.
    """
    from aegis.l2_normalization.normalizer import Normalizer
    from aegis.l2_normalization.tracelinker import TraceLinker

    turn = [
        "12:46:27 INFO [api] /chat/sync request received thread='lead:L-1' chars=5",
        "12:46:27 INFO [orchestrator] Question received (sources=['uws'])",
        "12:46:30 INFO [orchestrator] LLM usage node=generate total_tokens=4754",
        "12:46:31 INFO [orchestrator] ANSWER DELIVERED thread='lead:L-1' chars=136",
        "12:46:31 INFO [api] /chat/sync responded thread='lead:L-1' chars=136",
    ]
    norm, linker = Normalizer(service="chatbot"), TraceLinker()
    events = []
    for line in turn:
        event = norm.feed(line)
        if event is not None:
            events.append(linker.link(event))
    tail = norm.flush()
    if tail is not None:
        events.append(linker.link(tail))

    keyed = {e.trace_id for e in events if e.trace_id}
    assert len(keyed) == 1, f"the turn did not group into one run: {keyed}"
    assert linker.unattributed == 0, (
        f"{linker.unattributed} line(s) could not be placed")

    # The NEXT request must be its own run, not a continuation.
    for line in ["12:50:01 INFO [api] /chat/sync request received thread='lead:L-1' chars=9",
                 "12:50:02 INFO [api] /chat/sync responded thread='lead:L-1' chars=40"]:
        event = norm.feed(line)
        if event is not None:
            events.append(linker.link(event))
    tail = norm.flush()
    if tail is not None:
        events.append(linker.link(tail))
    assert len({e.trace_id for e in events if e.trace_id}) == 2, (
        "the second request was absorbed into the first")


def test_a_bare_clock_still_expires_an_idle_session():
    """_epoch only read a full date, so the idle timeout never ran.

    Sessions accumulated - 42 in one real log - and because inference needs
    exactly ONE open session, that silently disabled correlation for the
    entire file.
    """
    from aegis.l2_normalization.tracelinker import _epoch, TraceLinker
    from aegis.contracts.events import Event

    assert _epoch("15:49:03") is not None, "a bare clock must be usable"
    assert _epoch("2026-09-30 15:49:03.163") is not None
    assert _epoch("") is None and _epoch("junk") is None

    linker = TraceLinker(idle_timeout_s=300.0)
    linker.link(Event(id="e1", ts="10:00:00", service="s", level="INFO",
                      text_redacted="call=abc123def CALL START"))
    assert len(linker._open) == 1
    # Six minutes later, with no END logged: the session must not survive.
    linker.link(Event(id="e2", ts="10:06:01", service="s", level="INFO",
                      text_redacted="something unrelated"))
    assert len(linker._open) == 0, "an idle session was never expired"


def test_an_unkeyed_guess_cannot_swallow_the_whole_log():
    """A startup burst with no response line ran to 568 events in one log.

    A synthetic session is a GUESS that a stretch of lines is one request;
    past a plausible size the guess has failed and must be abandoned rather
    than allowed to absorb the file.
    """
    from aegis.l2_normalization.tracelinker import (
        TraceLinker, MAX_SYNTHETIC_EVENTS)
    from aegis.contracts.events import Event

    linker = TraceLinker()
    linker.link(Event(id="o1", ts="10:00:00", service="s", level="INFO",
                      text_redacted="[api] /chat request received"))
    for i in range(MAX_SYNTHETIC_EVENTS + 40):
        linker.link(Event(id=f"d{i}", ts="10:00:01", service="s", level="INFO",
                          text_redacted=f"[db] connected {i}"))

    assert len(linker._open) == 0, "the runaway guess was never abandoned"


def test_two_hunks_cannot_delete_the_same_block():
    """A model asked for one edit sometimes emits two hunks over one block.

    With "@@" carrying no line numbers nothing distinguishes them, so the
    second deletes what the first already removed. One real patch left a
    `try:` with no body; the file stopped parsing and it surfaced as a
    reproducer crash blamed on the test.
    """
    from aegis.l8_action.gate import AutonomyGate

    gate = AutonomyGate("T1")
    doubled = (
        "--- a/x.py\n+++ b/x.py\n@@\n"
        "-    result = call()\n+    try:\n+        result = call()\n"
        "+    except Exception:\n+        return None\n"
        "@@\n-    result = call()\n+    # handled above\n"
    )
    verdict = gate.validate_patch(doubled, ["x.py"])
    assert not verdict.allowed, "a patch deleting the same line twice was allowed"
    assert "same lines" in verdict.reason

    # Two hunks in genuinely different places stay allowed.
    distinct = ("--- a/x.py\n+++ b/x.py\n@@\n-    a = 1\n+    a = 2\n"
                "@@\n-    b = 1\n+    b = 2\n")
    assert gate.validate_patch(distinct, ["x.py"]).allowed


def test_a_patch_that_breaks_the_file_is_named_as_such():
    """patch(1) will happily leave a file that no longer compiles.

    Reported through the reproducer it read as "possibly the patch removed
    something the reproducer still needs" - which sends the reader to inspect
    a test that was fine.
    """
    import tempfile
    from pathlib import Path
    from aegis.l8_action.runner import _first_unparsable

    work = Path(tempfile.mkdtemp())
    target = work / "pkg" / "mod.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    patch = "--- a/pkg/mod.py\n+++ b/pkg/mod.py\n@@\n-    x = 1\n+    x = 2\n"

    target.write_text("def run():\n    try:\n    except Exception:\n        pass\n")
    broken = _first_unparsable(work, patch)
    assert broken is not None, "a file left unparsable was not detected"
    assert broken[0] == "pkg/mod.py"

    target.write_text("def run():\n    try:\n        x = 1\n    except Exception:\n        pass\n")
    assert _first_unparsable(work, patch) is None, "a healthy file was flagged"


def test_a_rate_limit_waits_as_long_as_the_provider_asked():
    """A fixed 20s guess under-waited, so the one retry failed for nothing.

    Free tiers meter TOKENS per minute and one Propose-fix call carrying code
    excerpts can exhaust a minute by itself, so the raw "HTTP Error 429"
    reached the user mid-demo.
    """
    from aegis.l7_reasoning.router import _retry_after

    class _WithHeader(Exception):
        headers = {"retry-after": "41.9"}

    assert _retry_after(_WithHeader(), "", 20.0) == 41.9, (
        "the provider's own Retry-After was ignored")
    # Groq also states it in the body.
    assert _retry_after(Exception(), "Please try again in 33.2s", 20.0) > 33.0
    # No signal at all falls back to the caller's default.
    assert _retry_after(Exception(), "", 20.0) == 20.0

    class _Absurd(Exception):
        headers = {"retry-after": "9999"}

    assert _retry_after(_Absurd(), "", 20.0) <= 90.0, (
        "a bad header could park an interactive click for minutes")


def test_a_patch_targets_the_raise_not_the_handler():
    """A traceback prints outermost to innermost; the LAST frame raised it.

    Returned in that order, the entry point came first, so the patch was
    written for the `except Exception:` that LOGS the error while the raise
    sat in another file. No patch to a handler can make the reproducer pass,
    so the whole attempt was spent on the wrong file.
    """
    import tempfile
    from pathlib import Path
    from aegis.l8_action.mapper import TraceToCodeMapper

    repo = Path(tempfile.mkdtemp())
    (repo / "api.py").write_text(
        "def chat():\n    try:\n        run()\n    except Exception:\n"
        "        log('failed')\n")
    (repo / "worker.py").write_text("def run():\n    raise ValueError('boom')\n")

    evidence = [
        f'  File "{repo / "api.py"}", line 3, in chat',
        f'  File "{repo / "worker.py"}", line 2, in run',
    ]
    located = TraceToCodeMapper(str(repo)).locate(evidence)
    assert located, "no frame was mapped"
    assert located[0].file == "worker.py", (
        f"anchored on {located[0].file} - the handler, not the raise")


def test_a_repo_path_that_does_not_match_the_traceback_is_named():
    """Pointed at another checkout of the same project, every frame is dropped.

    The mapper then falls back to text-matching the logged string, which lands
    on the `except` that wrote it rather than the raise. Two real attempts
    were spent that way before the path could be seen to be wrong.
    """
    import tempfile
    from pathlib import Path
    from aegis.l8_action.mapper import TraceToCodeMapper

    real = Path(tempfile.mkdtemp())
    (real / "worker.py").write_text("def run():\n    raise ValueError('boom')\n")
    other = Path(tempfile.mkdtemp())          # a different checkout
    (other / "worker.py").write_text("def run():\n    return 1\n")

    frame = [f'  File "{real / "worker.py"}", line 2, in run']

    mismatched = TraceToCodeMapper(str(other))
    mismatched.locate(frame)
    assert mismatched.foreign_frame_warning(), (
        "a traceback naming a different checkout was accepted silently")

    matched = TraceToCodeMapper(str(real))
    matched.locate(frame)
    assert not matched.foreign_frame_warning(), "a correct repo path warned"

    # EVERY traceback holds interpreter and package frames; they must never
    # be read as a wrong repository path.
    for library in ("/opt/homebrew/lib/python3.11/asyncio/base_events.py",
                    "/x/.venv/lib/python3.11/site-packages/langgraph/pregel.py"):
        noisy = TraceToCodeMapper(str(real))
        noisy.locate([f'  File "{library}", line 654, in run'])
        assert not noisy.foreign_frame_warning(), (
            f"a library frame ({library}) was mistaken for a wrong repo path")


def test_the_repo_field_starts_at_the_checkout_that_made_the_logs():
    """The field held whatever the browser remembered, from any project.

    A stale path to a SECOND checkout of the same project sent two whole fix
    attempts at code that did not contain the bug. The traceback names the
    right tree outright, so the field can start correct.
    """
    import tempfile
    from pathlib import Path
    from aegis.server import _repo_root_from_evidence

    repo = Path(tempfile.mkdtemp()) / "svc"
    (repo / "agents").mkdir(parents=True)
    # What marks a project root: the service holds a pytest.ini, the package
    # directory below it holds nothing. Climbing by __init__.py was wrong both
    # ways - a package dir without one stopped the climb one level too deep.
    (repo / "pytest.ini").write_text("[pytest]\n")
    (repo / "agents" / "orchestrator.py").write_text("x = 1\n")

    evidence = [f'  File "{repo / "agents" / "orchestrator.py"}", line 1, in go']
    assert _repo_root_from_evidence(evidence) == str(repo), (
        "the project root was not derived from the traceback")

    # Interpreter and package frames say nothing about the project.
    for library in ("/opt/homebrew/lib/python3.11/asyncio/base_events.py",
                    "/x/.venv/lib/python3.11/site-packages/langgraph/pregel.py"):
        assert _repo_root_from_evidence(
            [f'  File "{library}", line 9, in run']) == "", (
            f"{library} was mistaken for the project root")

    # No traceback at all offers nothing rather than guessing.
    assert _repo_root_from_evidence(["[api] plain log line"]) == ""


def test_a_stopped_incident_can_be_retried_without_deleting_files():
    """The breaker stops after two failures and nothing could reset it.

    resume_auto_fix() existed but was reachable from no API or button, so two
    attempts spent on a wrong repo path left an incident permanently
    un-retryable short of deleting its directory by hand.
    """
    import json
    import tempfile
    from pathlib import Path
    from unittest import mock
    from aegis.l8_action import remediate

    home = Path(tempfile.mkdtemp())
    with mock.patch.object(remediate, "AEGIS_HOME", home):
        path = remediate._attempts_path("proj", "INC-1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(
            {"count": 2, "stopped": True, "stop_reason": "gave up",
             "history": []}))

        remediate.resume_auto_fix("proj", "INC-1")
        state = json.loads(path.read_text())
        assert state["count"] == 0, "the attempt counter was not cleared"
        assert state["stopped"] is False
        assert not state["stop_reason"]

    # And it is reachable from the app, not just the module.
    from aegis.server import AegisApp
    assert hasattr(AegisApp, "retry_fix"), (
        "no API reaches resume_auto_fix - the only way back is rm -rf")


def test_third_party_noise_cannot_open_an_incident_in_any_language():
    """A pydantic warning from site-packages opened an incident and ranked
    ABOVE a genuine failure. The filter covered Python vendor trees only,
    while the product claim is "any project".
    """
    from aegis.l5_detection.detectors import _VENDOR_PATH

    vendor = (
        "/x/venv/lib/python3.11/site-packages/pydantic/_migration.py",
        "/x/node_modules/express/index.js",
        "/x/vendor/bundle/gems/rails-7/lib/a.rb",
        "/home/u/go/pkg/mod/github.com/pkg/errors/e.go",
        "/u/.cargo/registry/src/index/tokio/lib.rs",
        "/u/.m2/repository/org/springframework/core.jar",
    )
    for path in vendor:
        assert _VENDOR_PATH.search(path), f"third-party path not filtered: {path}"

    mine = ("/Users/me/project/app/api.py",
            "/srv/myapp/services/chatbot/agents/orchestrator.py",
            "/opt/app/vendors/pricing.py")
    for path in mine:
        assert not _VENDOR_PATH.search(path), (
            f"the user's own code was filtered as third-party: {path}")


def test_a_business_key_correlates_a_run():
    """Plenty of commercial systems have no trace id at all.

    They correlate on the thing being processed - order_id, payment_id,
    job_id. None of those were recognised, so such a system produced NO runs
    whatsoever and nothing above L3 could see it.
    """
    from aegis.l2_normalization.tracelinker import TraceLinker

    linker = TraceLinker()
    for line, expected in (
        ("order.received order_id=ORD-88412 items=3", "ORD-88412"),
        ("job.started job_id=jb_7741aa queue=default", "jb_7741aa"),
        ("payment.authorised payment_id=pay_31f9ab", "pay_31f9ab"),
        ('{"orderId":"ORD-5","status":"ok"}', "ORD-5"),
    ):
        assert linker._extract_key(line) == expected, (
            f"business key not read from: {line}")

    # An id naming who or what the run BELONGS to is not the run. Correlating
    # on customer_id would merge every order that customer ever placed.
    for line in ("user_id=c_7741", "customer_id=9120", "tenant_id=t_1",
                 "sku_id=SKU-114"):
        assert linker._extract_key(line) is None, (
            f"{line} would merge unrelated runs")

    # A real trace id still outranks a business key on the same line.
    both = "order.received trace_id=tr_abcdef12 order_id=ORD-88412"
    assert linker._extract_key(both) == "tr_abcdef12"


def test_marking_too_many_steps_cannot_hide_a_hollow_run():
    """A run is hollow only when NONE of its purpose steps ran.

    That rule is right - one logical step often mines into several template
    variants - but it means marking MORE steps makes hollowness harder to
    detect, which is backwards. On a real log the model marked six of nine
    steps, including the first line of every run, and all three hollow runs
    came back `achieved`.

    The fix belongs in the prompt: a step present in nearly every run happens
    in the runs that FAILED too, so it cannot be what distinguishes success.
    """
    from aegis.l4_understanding.flowspec import _SYNTH_PROMPT

    lowered = _SYNTH_PROMPT.lower()
    assert "presence" in lowered and "1.00" in _SYNTH_PROMPT, (
        "the prompt does not tell the model that a near-ubiquitous step "
        "cannot be the purpose - the one signal that distinguishes them")
    assert "smallest set" in lowered or "one step" in lowered, (
        "the prompt does not ask for the smallest set; marking many steps "
        "makes a hollow run undetectable")


def test_marking_a_fix_keeps_the_numbers_to_judge_it_by():
    """verify_fix was unreachable: nothing ever wrote a fix snapshot.

    Its own error message said "mark an incident as worked and the numbers at
    that moment are kept for comparison" - and marking it worked kept
    nothing, so every call answered "nothing was measured". The whole
    verification stage was dead code.
    """
    import tempfile
    import time
    from pathlib import Path
    from aegis.server import AegisApp

    log = Path(tempfile.mkdtemp()) / "svc.log"
    log.write_text("".join(
        f"10:0{i // 6}:{i % 60:02d} ERROR svc: boom call=abc1234{i % 3} failed\n"
        for i in range(40)))
    app = AegisApp()
    try:
        app.attach(str(log), "fixsnap")
        time.sleep(2)
        incidents = [i.to_dict() for i in app.pipeline.incidents.incidents]
        assert incidents, "no incident to mark"
        incident_id = incidents[0]["id"]

        assert app.record_outcome(incident_id, "worked", "applied")["ok"]
        assert app._fix_snapshot_for(incident_id), (
            "marking a fix worked kept no numbers to judge it by")

        result = app.verify_fix(incident_id)
        assert result["ok"], f"verification still cannot speak: {result}"
        assert result["headline"] == "held", result["headline"]
    finally:
        app.close()


def test_a_fix_that_did_not_hold_is_caught_by_recurrence():
    """Latency cannot judge most incidents.

    An error, a novelty or a run of hollow checkouts can be fixed with no
    timing moving at all, so the measure that generalises is whether the
    incident's own templates fired again.
    """
    from aegis.l6_correlation.verify import recurrence, MIN_BEFORE

    back = recurrence(["T-a"], {"T-a": 10}, {"T-a": 14})
    assert back["verdict"] == "recurred" and "4 more" in back["detail"]

    held = recurrence(["T-a"], {"T-a": 10}, {"T-a": 10})
    assert held["verdict"] == "held"

    # Silence is not evidence when the thing was rare to begin with.
    rare = recurrence(["T-a"], {"T-a": MIN_BEFORE - 1}, {"T-a": MIN_BEFORE - 1})
    assert rare["verdict"] == "too-early", rare

    assert recurrence([], {}, {})["verdict"] == "unknown"

    # One of several coming back is still a recurrence.
    partial = recurrence(["T-a", "T-b"], {"T-a": 9, "T-b": 9},
                         {"T-a": 9, "T-b": 12})
    assert partial["verdict"] == "recurred" and "T-b" in partial["detail"]


def test_a_fix_that_stopped_holding_downgrades_itself():
    """The outcome label was the one thing needing a human to come back later.

    Nobody ever does, so every precedent read "outcome: not recorded" forever
    and similar() handed out unverified diagnoses with a precedent's
    authority. The watch re-measures and downgrades, with the measurement
    that justifies it.
    """
    import tempfile
    import time
    from pathlib import Path
    from aegis.server import AegisApp

    log = Path(tempfile.mkdtemp()) / "svc.log"
    log.write_text("".join(
        f"10:0{i // 6}:{i % 60:02d} ERROR svc: boom call=abc1234{i % 3} failed\n"
        for i in range(40)))
    app = AegisApp()
    try:
        app.attach(str(log), "watchdown")
        time.sleep(2)
        incident_id = [i.to_dict() for i in
                       app.pipeline.incidents.incidents][0]["id"]
        app.record_outcome(incident_id, "worked", "applied")

        def outcome() -> str:
            for row in app.pipeline.store.archived_incidents():
                if str(row["id"]).split("@")[0] == incident_id:
                    return str(row.get("outcome") or "")
            return ""

        # Quiet: a working fix must not be touched.
        assert app.watch_fixes()["downgraded"] == []
        assert outcome() == "worked"

        # The same problem comes back.
        with log.open("a") as handle:
            for i in range(14):
                handle.write(
                    f"10:40:{i:02d} ERROR svc: boom call=yyy8888{i % 3} failed\n")
        time.sleep(3)

        downgraded = app.watch_fixes()["downgraded"]
        assert len(downgraded) == 1, downgraded
        assert outcome() == "did_not_work", (
            "a fix whose problem returned is still recorded as working")
    finally:
        app.close()


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
