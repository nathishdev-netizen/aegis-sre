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
    """The tool names were verified against the vendors' published docs, not
    guessed - a wrong tool name means a connector that silently returns
    nothing."""
    assert MCP_PRESETS["signoz-mcp"]["logs_tool"] == "fetch_traces_or_logs"
    assert "opik-mcp" in MCP_PRESETS
    assert {p["id"] for p in preset_choices()} == set(MCP_PRESETS)


def test_a_preset_fills_in_the_tool_for_the_user():
    registry = _registry()
    assert registry.add({"name": "sig", "kind": "mcp", "preset": "signoz-mcp"})["ok"]
    import json
    stored = json.loads(registry.path.read_text())[0]
    assert stored["logs_tool"] == "fetch_traces_or_logs"
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
