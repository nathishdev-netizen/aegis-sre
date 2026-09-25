"""MCP server tests - the protocol core exercised in-process.

No stdio, no model calls: AegisMcp.handle() is the whole surface the stdio
loop delegates to.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aegis.mcp_server import AegisMcp  # noqa: E402
from aegis.server import AegisApp  # noqa: E402

CALLS = "".join(
    f"2026-09-01 10:{m:02d}:00 INFO voice: [api] CALL START call=aaaa000{m}-1111\n"
    f"2026-09-01 10:{m:02d}:01 ERROR voice: [pay] charge FAILED upstream 502\n"
    f"2026-09-01 10:{m:02d}:20 INFO voice: [api] CALL END call=aaaa000{m}-1111 - 20s\n"
    for m in range(3))


def _mcp(allow_model: bool = False) -> AegisMcp:
    log = Path(tempfile.mkdtemp()) / "svc.log"
    log.write_text(CALLS)
    app = AegisApp("mcp-test", str(log))
    app._stop.set(); app._thread.join(timeout=2)
    app.pipeline.run_once(); app.pipeline.drain()
    return AegisMcp(app, allow_model=allow_model)


def _call(mcp: AegisMcp, name: str, arguments: dict | None = None) -> tuple[dict, dict]:
    response = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": name, "arguments": arguments or {}}})
    result = response["result"]
    return result, json.loads(result["content"][0]["text"])


def test_initialize_handshake():
    response = _mcp().handle({"jsonrpc": "2.0", "id": 0, "method": "initialize"})
    result = response["result"]
    assert result["protocolVersion"]
    assert result["serverInfo"]["name"] == "aegis"
    assert "tools" in result["capabilities"]


def test_notifications_get_no_response():
    """A JSON-RPC notification has no id; answering one corrupts the stream."""
    assert _mcp().handle({"jsonrpc": "2.0",
                          "method": "notifications/initialized"}) is None


def test_unknown_method_is_a_proper_error():
    response = _mcp().handle({"jsonrpc": "2.0", "id": 9, "method": "resources/list"})
    assert response["error"]["code"] == -32601


def test_model_tool_is_absent_unless_permitted():
    """Read-heavy, writes gated: the spender does not even appear in the tool
    list unless the OPERATOR started the server with --allow-model - a client
    cannot ask for what is not offered."""
    names = [t["name"] for t in _mcp()._tools()]
    assert "explain_incident" not in names
    assert "explain_incident" in [t["name"] for t in _mcp(True)._tools()]


def test_explain_refuses_even_if_called_when_gated():
    result, payload = _call(_mcp(False), "explain_incident", {"incident_id": "INC-1"})
    assert result["isError"] is True
    assert "not permitted" in payload["error"]


def test_overview_answers_from_the_pipeline():
    result, payload = _call(_mcp(), "aegis_overview")
    assert result["isError"] is False
    assert payload["funnel"]["log lines"] == 9
    assert payload["incidents"] >= 1


def test_incident_roundtrip():
    mcp = _mcp()
    _, rows = _call(mcp, "list_incidents")
    assert rows and rows[0]["ranked_cause"]
    _, full = _call(mcp, "get_incident", {"incident_id": rows[0]["id"]})
    assert full["id"] == rows[0]["id"]
    assert full["signals"]


def test_missing_incident_is_an_error_result_not_a_crash():
    result, payload = _call(_mcp(), "get_incident", {"incident_id": "INC-999"})
    assert result["isError"] is True


def test_history_finds_precedents_by_words():
    mcp = _mcp()
    mcp.app.pipeline.memory.remember(
        {"id": "INC-OLD", "opened_at": "09:00:00", "severity": "P2",
         "evidence": ["ERROR voice: [pay] charge FAILED upstream 502"],
         "signals": []})
    _, rows = _call(mcp, "query_incident_history", {"query": "charge failed upstream"})
    assert rows and rows[0]["id"].startswith("INC-OLD")
    assert "not conclusion" in rows[0]["note"]


def test_a_malformed_call_cannot_kill_the_server():
    response = _mcp().handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                              "params": {"name": "get_incident",
                                         "arguments": {"incident_id": None}}})
    assert "result" in response or "error" in response


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
    print(f"\n{'FAILED' if failures else 'All MCP tests passed'}"
          f"{f' ({failures} failing)' if failures else ''}")
    sys.exit(1 if failures else 0)
