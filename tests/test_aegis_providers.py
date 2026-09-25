"""C2 provider connector tests.

SigNoz is exercised against fixture responses (no live instance existed at
build time - the adapter isolates the day that changes). The MCP client is
exercised against a REAL subprocess: our own Phase 11 server, speaking the
same wire any vendor MCP server speaks.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aegis.l1_ingestion.providers import (  # noqa: E402
    LogFilter, McpClient, McpProvider, McpToolMap, QueryCache, QuotaGuard,
    SigNozProvider)
from aegis.l2_normalization.normalizer import Normalizer  # noqa: E402

SIGNOZ_RESPONSE = {
    "data": {"data": {"results": [{"rows": [
        {"timestamp": "2026-09-05T10:00:16Z",
         "data": {"body": "ERROR payment webhook failed for order 42",
                  "service.name": "orders-api"}},
        {"timestamp": "2026-09-05T10:00:04Z",
         "data": {"body": "request completed in 45ms",
                  "service.name": "orders-api"}},
    ]}]}}
}


def _provider(response=None, quota=None):
    calls = []
    def transport(url, headers, body, timeout):
        calls.append({"url": url, "headers": headers, "body": body})
        return response if response is not None else SIGNOZ_RESPONSE
    return calls, SigNozProvider("https://signoz.example.com", api_key="k",
                                 transport=transport, quota=quota,
                                 cache=QueryCache(ttl_s=30))


def test_signoz_request_carries_the_query_and_the_key():
    calls, provider = _provider()
    provider.query_logs(LogFilter(service="orders-api", since_minutes=30))
    request = calls[0]
    assert request["url"].endswith("/api/v5/query_range")
    assert request["headers"]["SIGNOZ-API-KEY"] == "k"
    spec = request["body"]["compositeQuery"]["queries"][0]["spec"]
    assert spec["signal"] == "logs"
    assert "orders-api" in spec["filter"]["expression"]


def test_vendor_schema_never_leaks_upward():
    """P6: what comes back is canonical RawRecords - payload, service,
    timestamp. If a caller can see 'compositeQuery' or 'rows', the
    abstraction has failed."""
    _, provider = _provider()
    records = provider.query_logs(LogFilter(service="orders-api"))
    assert len(records) == 2
    for record in records:
        fields = set(record.to_dict())
        assert fields == {"source_id", "payload", "host", "service", "collected_at"}
    assert records[0].payload.startswith("ERROR payment webhook")


def test_a_dead_provider_degrades_never_raises():
    def broken(url, headers, body, timeout):
        raise OSError("connection refused")
    provider = SigNozProvider("https://x", transport=broken)
    assert provider.query_logs(LogFilter()) == []


def test_quota_guard_stops_runaway_spend():
    """Providers charge money. Over budget means degraded result, not a
    surprise bill during an outage - the exact P7 failure mode."""
    calls, provider = _provider(quota=QuotaGuard(max_calls_per_minute=2))
    for index in range(5):
        provider.query_logs(LogFilter(text=f"q{index}"))  # distinct cache keys
    assert len(calls) == 2


def test_repeat_queries_hit_the_cache_not_the_vendor():
    calls, provider = _provider()
    provider.query_logs(LogFilter(service="orders-api"))
    provider.query_logs(LogFilter(service="orders-api"))
    assert len(calls) == 1


def test_provider_records_flow_through_the_normal_pipeline():
    """A provider's logs get the same redaction and fingerprinting as a local
    file's - there is one pipeline, not a trusted side door."""
    fixture = {"data": {"data": {"results": [{"rows": [
        {"timestamp": "t",
         "data": {"body": "user jane.doe@example.com charged card"
                          " 4111 1111 1111 1111"}}]}]}}}
    _, provider = _provider(response=fixture)
    records = provider.query_logs(LogFilter())
    normalizer = Normalizer(service="remote")
    events = list(normalizer.feed_all([r.payload for r in records]))
    assert "jane.doe@example.com" not in events[0].text_redacted
    assert "<EMAIL>" in events[0].text_redacted


# -- The MCP client, against a real server ------------------------------------

def _own_server() -> McpClient:
    log = Path(tempfile.mkdtemp()) / "svc.log"
    log.write_text(
        "2026-09-01 10:00:00 INFO voice: [api] CALL START call=aaaa0001-1111\n"
        "2026-09-01 10:00:01 ERROR voice: [pay] charge FAILED upstream 502\n"
        "2026-09-01 10:00:20 INFO voice: [api] CALL END call=aaaa0001-1111 - 20s\n")
    return McpClient([sys.executable, "-m", "aegis.mcp_server", str(log), "c2-test"])


def test_mcp_client_speaks_to_a_real_server():
    client = _own_server()
    try:
        tools = client.list_tools()
        assert "aegis_overview" in [t["name"] for t in tools]
        overview = client.call_tool("aegis_overview", {})
        assert overview["funnel"]["log lines"] == 3
    finally:
        client.close()


def test_mcp_provider_maps_a_tool_reply_to_canonical_records():
    client = _own_server()
    try:
        # list_incidents definitely has a row on this log (its ERROR opened
        # one); get_run_verdicts was legitimately empty - one trace cannot
        # mine a spec, so there is nothing to judge against.
        provider = McpProvider(
            client,
            McpToolMap(logs_tool="list_incidents", lines_path="",
                       line_key="ranked_cause"),
            provider_name="aegis-remote")
        records = provider.query_logs(LogFilter(limit=10))
        assert records, "no records mapped from the tool reply"
        assert all(r.source_id == "aegis-remote:mcp" for r in records)
    finally:
        client.close()


def test_mcp_provider_survives_a_wrong_tool_name():
    client = _own_server()
    try:
        provider = McpProvider(client, McpToolMap(logs_tool="no_such_tool"))
        assert provider.query_logs(LogFilter()) == []
    finally:
        client.close()


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
    print(f"\n{'FAILED' if failures else 'All provider tests passed'}"
          f"{f' ({failures} failing)' if failures else ''}")
    sys.exit(1 if failures else 0)
