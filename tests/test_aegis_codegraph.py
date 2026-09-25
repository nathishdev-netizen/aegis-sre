"""Regression tests - the CodeGraph bridge.

The failure modes of an optional external dependency: becoming mandatory,
lying when absent, spending a scarce investigation step to say "unavailable",
and - the one that would breach our own isolation rule - writing inside the
user's repository without being asked.
"""

from __future__ import annotations

import json as _json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aegis.l4_understanding.codegraph import (  # noqa: E402
    CodeGraph, index_present, status)


def _repo_with_index(has_index: bool) -> str:
    root = Path(tempfile.mkdtemp())
    if has_index:
        (root / ".codegraph").mkdir()
    return str(root)


def test_a_repo_without_an_index_is_reported_not_guessed():
    repo = _repo_with_index(False)
    assert index_present(repo) is False
    answer = CodeGraph(repo).explore("what calls process_event?")
    assert "no code graph" in answer
    assert "process_event" not in answer, "it answered a question it cannot answer"


def test_a_missing_cli_degrades_to_todays_behaviour():
    """An enhancement may never become a dependency. With no CLI the status
    says so plainly and nothing raises."""
    state = status(_repo_with_index(False))
    assert state["state"] in ("no_cli", "no_index")
    assert state["detail"]


def test_aegis_never_builds_the_index_itself():
    """`codegraph init` writes .codegraph/ INTO the project directory. Our
    isolation rule says Aegis writes only under ~/.aegis, so building the
    graph must be an explicit user action through one named method - never a
    side effect of attaching, analyzing, or exploring."""
    import inspect
    from aegis.l4_understanding import codegraph

    source = inspect.getsource(codegraph)
    assert "init" not in source.replace("_INDEX_DIR", ""), \
        "the bridge module can trigger an index build"

    from aegis.server import AegisApp
    build = inspect.getsource(AegisApp.build_code_graph)
    assert '"init"' in build, "the one build path does not call the CLI"
    for method in (AegisApp.analyze_project, AegisApp.attach):
        assert "build_code_graph" not in inspect.getsource(method), \
            "building the graph happens as a side effect"


def test_the_tool_is_offered_only_when_a_graph_exists():
    """A tool that always answers "not available" burns one of six
    investigation steps to say nothing."""
    import inspect
    from aegis.server import AegisApp
    source = inspect.getsource(AegisApp._investigation_tools)
    assert "index_present" in source
    assert 'tools["explore_code"] = explore_code' in source, \
        "explore_code is registered unconditionally"


def test_a_graph_reply_is_returned_and_capped():
    """The graph answers with source and call paths; a huge reply must not
    flood a prompt."""
    repo = _repo_with_index(True)
    graph = CodeGraph(repo)

    class FakeClient:
        def __init__(self, *a, **k): pass
        def list_tools(self): return [{"name": "codegraph_explore"}]
        def call_tool(self, name, args): return "x" * 9000
        def close(self): pass

    import aegis.l1_ingestion.providers as providers
    real = providers.McpClient
    providers.McpClient = FakeClient
    try:
        answer = graph.explore("anything")
    finally:
        providers.McpClient = real
    assert len(answer) <= 4000, f"reply not capped: {len(answer)}"


def test_a_build_without_the_cli_refuses_rather_than_half_running():
    from aegis.server import AegisApp
    app = AegisApp.__new__(AegisApp)
    app.code_analysis = {}
    app._load_code = lambda: {}
    assert app.build_code_graph()["ok"] is False


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
    print(f"\n{'FAILED' if failures else 'All codegraph tests passed'}")
    sys.exit(1 if failures else 0)
