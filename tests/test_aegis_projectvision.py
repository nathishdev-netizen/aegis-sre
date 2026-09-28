"""The three vision pieces: project analysis, cross-service dependency, and
static failure simulation. Fixture-verified against a deliberately imperfect
mini service (one call unguarded, one URL behind a constant, one DB touch)."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aegis.l4_understanding.codebase import analyze_repo  # noqa: E402
from aegis.l4_understanding.dependencies import DependencyMap  # noqa: E402
from aegis.l4_understanding.simulate import simulate_failure  # noqa: E402
from aegis.pipeline import Pipeline  # noqa: E402

MINI = Path(__file__).resolve().parent / "fixtures" / "mini_service"


def _analysis():
    return analyze_repo(MINI)


# --- 1. project analysis ------------------------------------------------------

def test_routes_are_found_with_their_functions():
    analysis = _analysis()
    routes = {(e.method, e.path): e.function for e in analysis.entrypoints
              if e.kind == "http"}
    assert routes[("POST", "/orders")] == "create_order"
    assert ("GET", "/health") in routes


def test_guard_and_timeout_status_is_per_call_and_correct():
    """The whole point: simulation later says degraded-vs-failed based on
    THIS. A wrong guard flag is a wrong prediction."""
    analysis = _analysis()
    by_target = {c.target: c for c in analysis.external_calls if c.kind == "http"}
    assert by_target["http://localhost:7002/reserve"].guarded is False
    assert by_target["http://localhost:7002/reserve"].has_timeout is False
    assert by_target["https://payments.example.com/charge"].guarded is True


def test_constant_urls_resolve_through_fstrings():
    """ORCH_URL = "http://localhost:6004" used in an f-string is still a
    dependency; a naive literal scan misses every configured URL."""
    analysis = _analysis()
    assert any(c.target.startswith("http://localhost:6004")
               for c in analysis.external_calls)


def test_reachability_crosses_files_and_ignores_builtins():
    analysis = _analysis()
    entry = next(e for e in analysis.entrypoints if e.path == "/orders")
    flow = analysis.flow_for(entry)
    assert set(flow["functions"]) == {"create_order", "reserve_stock", "charge_card"}
    assert any(u["target"] == "http://localhost:7002/reserve"
               for u in flow["unguarded"])


def test_log_statements_map_templates_to_code():
    analysis = _analysis()
    order_start = [l for l in analysis.log_statements if "ORDER START" in l.text]
    assert order_start and order_start[0].file == "app.py"


def test_a_broken_file_is_skipped_not_fatal():
    root = Path(tempfile.mkdtemp())
    (root / "good.py").write_text("def ok():\n    pass\n")
    (root / "bad.py").write_text("def broken(:\n")
    analysis = analyze_repo(root)
    assert analysis.files_scanned == 2
    assert any("bad.py" in s for s in analysis.skipped)


# --- 2. cross-service dependency ---------------------------------------------

def test_own_addresses_are_not_dependencies():
    """The real gateway's ngrok callback ranked as its own biggest SPOF until
    hosts a service declares as its own (public_host=…) were excluded."""
    mapper = DependencyMap(own_ports={8080})
    mapper.observe_log_line("Prompting via https://x1.ngrok-free.app/voice/gather")
    mapper.observe_log_line("Gateway starting public_host=x1.ngrok-free.app")
    mapper.observe_log_line("Calling http://localhost:6004/chat")
    mapper.observe_log_line("bound to http://0.0.0.0:8080")
    hosts = {r["host"] for r in mapper.report()}
    assert hosts == {"localhost"}, hosts


def test_running_here_matches_listening_ports_with_a_log():
    mapper = DependencyMap()
    mapper.observe_log_line("Calling http://localhost:6004/chat")
    rows = mapper.report(
        [{"port": 6004, "pid": 42, "process": "python"}],
        log_for_pid=lambda pid: f"/logs/{pid}.log")
    assert rows[0]["running_here"] is True
    assert rows[0]["combine_path"] == "/logs/42.log"


def test_code_analysis_brings_guard_status_logs_never_could():
    mapper = DependencyMap()
    mapper.absorb_code_analysis(_analysis().to_dict())
    row = next(r for r in mapper.report() if r["port"] == 7002)
    assert row["guarded_in_code"] is False


def test_one_trace_spans_both_services_when_combined():
    """The user's scenario verbatim: watch the gateway, combine the
    orchestrator, and one call id becomes one story - with the incident's
    cause landing on the DOWNSTREAM failure, not the gateway's symptom."""
    d = Path(tempfile.mkdtemp())
    (d / "gw.log").write_text(
        "2026-09-01 10:00:00 INFO gw: CALL START call=abc12345-9999\n"
        "2026-09-01 10:00:07 ERROR gw: /voice/process returned 500 call=abc12345-9999\n")
    (d / "orch.log").write_text(
        "2026-09-01 10:00:02 INFO orch: /chat start call=abc12345-9999\n"
        "2026-09-01 10:00:06 ERROR orch: tool FAILED upstream 503 call=abc12345-9999\n")
    pipeline = Pipeline("gateway", d / "gw.log", store_root=tempfile.mkdtemp())
    assert pipeline.add_secondary(d / "orch.log", "orchestrator")["ok"]
    assert not pipeline.add_secondary(d / "orch.log", "orchestrator")["ok"], \
        "the same file must not be watched twice"
    pipeline.run_once(); pipeline.drain()
    traces = pipeline.trace_index.traces()
    services = {e.service for events in traces.values() for e in events}
    assert services == {"gateway", "orchestrator"}
    incident = pipeline.incidents.incidents[0]
    cause = next(m for m in incident.members if m.id == incident.ranked_cause)
    assert cause.service == "orchestrator", \
        "the gateway's 500 symptom outranked the orchestrator's 503 cause"
    pipeline.close()


# --- 3. simulation ------------------------------------------------------------

def _dep_rows():
    mapper = DependencyMap()
    mapper.absorb_code_analysis(_analysis().to_dict())
    return mapper.report()


def test_guarded_dependency_predicts_degraded():
    result = simulate_failure("payments.example.com", dependencies=_dep_rows())
    assert result["ok"]
    assert result["predictions"][0]["outcome"] == "degraded"


def test_unguarded_dependency_predicts_failed():
    result = simulate_failure("localhost:7002", dependencies=_dep_rows())
    assert result["ok"]
    assert result["predictions"][0]["outcome"] == "failed"
    assert "try/except" in result["predictions"][0]["because"]


def test_unknown_target_is_refused_not_guessed():
    result = simulate_failure("never-heard-of-it", dependencies=_dep_rows())
    assert result["ok"] is False


def test_every_simulation_admits_it_is_static():
    result = simulate_failure("localhost:7002", dependencies=_dep_rows())
    assert any("static" in g.lower() for g in result["gaps"]), \
        "a prediction that does not state its limits is a claim"


def test_f_string_log_lines_are_not_invisible():
    """The analyser recorded a log statement only when its first argument was a
    plain string constant, so every f-string log line was dropped silently.
    paideia writes almost all of them that way: 181 statements were found where
    381 exist, and services/chatbot/agents/vector_agent.py contributed ZERO.

    That is not a cosmetic count. A line the map does not know about cannot be
    traced back to source, so neither incident mapping nor a gap report can
    name where it came from."""
    import ast
    import tempfile
    from pathlib import Path as _Path

    from aegis.l4_understanding.codebase import analyze_repo

    root = _Path(tempfile.mkdtemp(prefix="aegis-fstring-"))
    (root / "svc.py").write_text(
        "from loguru import logger\n"
        "def work(n):\n"
        "    logger.info('plain constant line')\n"
        "    logger.info(f'[svc] chose tool for n={n}')\n")
    found = analyze_repo(root).to_dict()["log_statements"]
    texts = [s["text"] for s in found]
    assert any("plain constant" in t for t in texts), texts
    assert any("chose tool for n=" in t for t in texts), (
        f"the f-string log line was dropped: {texts}")


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
    print(f"\n{'FAILED' if failures else 'All project-vision tests passed'}"
          f"{f' ({failures} failing)' if failures else ''}")
    sys.exit(1 if failures else 0)
