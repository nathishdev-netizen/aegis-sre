"""Connector registry: adding, listing, health, and the promise that a dead
provider can never look healthy."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aegis.l1_ingestion.providers import (  # noqa: E402
    MCP_PRESETS, ProviderRegistry, preset_choices)


def _registry() -> ProviderRegistry:
    return ProviderRegistry(Path(tempfile.mkdtemp()) / "providers.json")


def test_signoz_needs_a_url_and_mcp_needs_a_tool():
    registry = _registry()
    assert not registry.add({"name": "a", "kind": "signoz"})["ok"]
    assert not registry.add({"name": "b", "kind": "mcp", "command": ["x"]})["ok"]
    assert not registry.add({"name": "c", "kind": "nope"})["ok"]


def test_presets_carry_the_vendors_real_tool_names():
    """The tool names must match the vendor's real ones - a wrong name is a
    connector that silently returns nothing. signoz_search_logs was confirmed
    against github.com/SigNoz/signoz-mcp-server and the official docs, and a
    live v0.117 instance, in Sept 2026. The old value fetch_traces_or_logs was
    a guess and was wrong."""
    assert MCP_PRESETS["signoz-mcp"]["logs_tool"] == "signoz_search_logs"
    assert "opik-mcp" in MCP_PRESETS
    assert {p["id"] for p in preset_choices()} == set(MCP_PRESETS)


def test_a_preset_fills_in_the_tool_for_the_user():
    registry = _registry()
    assert registry.add({"name": "sig", "kind": "mcp", "preset": "signoz-mcp"})["ok"]
    import json
    stored = json.loads(registry.path.read_text())[0]
    assert stored["logs_tool"] == "signoz_search_logs"
    assert stored["command"] == MCP_PRESETS["signoz-mcp"]["command"]


def test_listing_never_leaks_the_api_key():
    registry = _registry()
    registry.add({"name": "sig", "kind": "signoz",
                  "base_url": "https://signoz.example.com", "api_key": "secret-key"})
    listed = registry.list()[0]
    assert listed["has_key"] is True
    assert "secret-key" not in str(listed)


def test_credentials_are_written_owner_only():
    registry = _registry()
    registry.add({"name": "sig", "kind": "signoz",
                  "base_url": "https://x.io", "api_key": "k"})
    mode = registry.path.stat().st_mode & 0o777
    assert mode == 0o600, oct(mode)


def test_an_unreachable_provider_reports_error_not_zero_records():
    """The first version returned [] on connection failure, so check() saw
    "0 records" and called it healthy. A connector that lies about its state
    is worse than no connector."""
    registry = _registry()
    registry.add({"name": "dead", "kind": "signoz",
                  "base_url": "https://signoz.invalid.nowhere", "api_key": "k"})
    result = registry.check("dead")
    assert result["ok"] is False
    assert result["detail"]
    assert registry.list()[0]["status"] == "error"


def test_removing_a_provider_forgets_it():
    registry = _registry()
    registry.add({"name": "sig", "kind": "signoz", "base_url": "https://x.io"})
    registry.remove("sig")
    assert registry.list() == []




# A real SigNoz v5 raw-logs response, captured from a live v0.117.1 instance on
# 2026-09-29. The parse must survive THIS shape, not the one we guessed:
# service.name lives in resources_string, severity in severity_text, and the
# row is {"timestamp", "data"} - not flat.
_LIVE_V5_RESPONSE = {
    "data": {"data": {"results": [{"rows": [
        {"timestamp": "2026-09-29T05:35:40Z", "data": {
            "body": "/api/method/ping | Outgoing Response | {\"http_status_code\": 200}",
            "severity_text": "INFO",
            "attributes_string": {"trace_id": "022a9e51d9b8318c85a00e115924f784",
                                  "api_method": "/api/method/ping"},
            "attributes_number": {"http_status_code": 200},
            "resources_string": {"service.name": "CRM-CBT-PROD",
                                 "deployment.environment": "UAT"},
        }},
    ]}]}},
}


def test_a_real_v5_row_parses_service_severity_and_numbers():
    """Guards the shape a live instance actually returns. service.name is nested
    in resources_string - reading it off the top of data (as the first version
    did) left every record with an empty service, and everything an empty
    service breaks: incident grouping, per-service baselines, the lot."""
    from aegis.l1_ingestion.providers import SigNozProvider, LogFilter

    p = SigNozProvider("http://signoz.test", api_key="k",
                       transport=lambda *a, **k: _LIVE_V5_RESPONSE)
    records = p.query_logs(LogFilter(since_minutes=60, limit=10))
    assert len(records) == 1, records
    rec = records[0]
    assert rec.service == "CRM-CBT-PROD", rec.service
    assert "INFO" in rec.payload, rec.payload
    assert "022a9e51" in rec.payload, "trace id was dropped"
    # A field already present in the body is not duplicated; one that is not
    # (api_method) gets appended so detection can see it.
    assert "http_status_code" in rec.payload, "status code lost entirely"
    assert "api_method=/api/method/ping" in rec.payload, "attribute was dropped"


def test_the_environment_is_read_however_the_service_declared_it():
    """Measured across five live services: most set deployment.environment,
    some set deployment.environment.name instead, and some set the literal
    "unknown". Reading one key left two of five unlabelled, and "reconfigure
    every app" is not an answer - the point is to work with the telemetry a
    project already emits."""
    from aegis.l1_ingestion.providers import _environment_of

    assert _environment_of({"deployment.environment": "PROD"}) == "PROD"
    # gateway-ms really does use this key, and was being dropped entirely
    assert _environment_of({"deployment.environment.name": "UAT"}) == "UAT"


def test_a_useless_environment_falls_back_to_the_service_name():
    """cbt-dev-crm declaring "unknown" is worse than no value: it occupies the
    slot with nothing. The name plainly says dev, so say so - marked with ? so
    an inferred environment can never be mistaken for a declared one."""
    from aegis.l1_ingestion.providers import _environment_of

    assert _environment_of({"service.name": "cbt-dev-crm",
                            "deployment.environment": "unknown"}) == "DEV?"
    assert _environment_of({"service.name": "cbt-uat-crm"}) == "UAT?"


def test_an_environment_that_cannot_be_known_is_left_blank():
    """destiin-cbt-v2 says nothing about where it runs. Guessing would be worse
    than the gap - a wrong PROD label is how someone debugs the wrong system."""
    from aegis.l1_ingestion.providers import _environment_of

    assert _environment_of({"service.name": "destiin-cbt-v2",
                            "deployment.environment": "unknown"}) == ""
    assert _environment_of({}) == ""


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
    print(f"\n{'FAILED' if failures else 'All connector tests passed'}"
          f"{f' ({failures} failing)' if failures else ''}")
    sys.exit(1 if failures else 0)
