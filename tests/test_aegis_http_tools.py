"""Read-only HTTP tools: every rule that keeps the surface read-only, proven
against a fake transport so no test ever reaches a network."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aegis.l1_ingestion.http_tools import (  # noqa: E402
    HttpToolbox,
    HttpToolsConfigError,
)


def _config(**tool_overrides) -> dict:
    tool = {"name": "failed_deliveries", "service": "ms",
            "path": "/admin/queue/failed-delivery",
            "description": "Messages whose delivery to the agent failed.",
            "query": {"mailbox": "mailbox email", "limit": "max rows"}}
    tool.update(tool_overrides)
    return {"services": {"ms": {"base_url": "http://127.0.0.1:8090/gmail/",
                                "headers": {"X-Admin-Key": "${TEST_ADMIN_KEY}"}}},
            "tools": [tool]}


class _Recorder:
    def __init__(self, status: int = 200, body: bytes = b"[]") -> None:
        self.status, self.body, self.calls = status, body, []

    def __call__(self, url, headers, timeout_s, max_bytes):
        self.calls.append({"url": url, "headers": headers})
        return self.status, self.body


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv("TEST_ADMIN_KEY", "super-secret-admin-key")


def test_a_write_cannot_be_configured():
    with pytest.raises(HttpToolsConfigError, match="only GET"):
        HttpToolbox.from_dict(_config(method="POST"))


def test_a_path_argument_cannot_add_segments_or_a_query():
    config = _config(name="conversation", path="/threads/{thread_id}/conversation",
                     path_params={"thread_id": "gmail thread id"}, query={})
    transport = _Recorder()
    box = HttpToolbox.from_dict(config, transport=transport)
    box.call("conversation", {"thread_id": "../../admin/diag/agent?x=1#"})
    url = transport.calls[0]["url"]
    assert url == ("http://127.0.0.1:8090/gmail/threads/"
                   "..%2F..%2Fadmin%2Fdiag%2Fagent%3Fx%3D1%23/conversation")


def test_only_declared_query_parameters_are_sent():
    transport = _Recorder()
    box = HttpToolbox.from_dict(_config(), transport=transport)
    box.call("failed_deliveries", {"limit": "5", "status": "SENT", "mailbox": ""})
    assert transport.calls[0]["url"].endswith("/admin/queue/failed-delivery?limit=5")


def test_the_secret_is_sent_but_never_shown():
    transport = _Recorder(body=b'{"echo": "super-secret-admin-key"}')
    box = HttpToolbox.from_dict(_config(), transport=transport)
    result = box.call("failed_deliveries", {})
    assert transport.calls[0]["headers"]["X-Admin-Key"] == "super-secret-admin-key"
    assert "super-secret-admin-key" not in json.dumps(result)
    assert "super-secret" not in json.dumps(box.catalogue())


def test_a_missing_secret_fails_the_call_without_sending(monkeypatch):
    monkeypatch.delenv("TEST_ADMIN_KEY")
    transport = _Recorder()
    result = HttpToolbox.from_dict(_config(), transport=transport).call("failed_deliveries", {})
    assert result["ok"] is False and "TEST_ADMIN_KEY" in result["error"]
    assert transport.calls == []


def test_the_body_is_redacted_and_capped():
    body = json.dumps([{"from": "ravi@infosys.com", "pad": "x" * 100}]).encode()
    box = HttpToolbox.from_dict(_config(max_bytes=60), transport=_Recorder(body=body))
    result = box.call("failed_deliveries", {})
    assert result["truncated"] is True and len(result["body"]) <= 60
    assert "ravi@infosys.com" not in result["body"]


def test_an_error_status_is_reported_not_raised():
    box = HttpToolbox.from_dict(_config(), transport=_Recorder(status=401, body=b'{"detail":"no"}'))
    result = box.call("failed_deliveries", {})
    assert result["ok"] is False and result["status"] == 401


def test_an_unreachable_service_is_an_answer_not_a_crash():
    def refuse(*_):
        raise ConnectionRefusedError("refused")
    result = HttpToolbox.from_dict(_config(), transport=refuse).call("failed_deliveries", {})
    assert result["ok"] is False and "refused" in result["error"]


def test_a_required_path_argument_must_be_given():
    config = _config(name="conversation", path="/threads/{thread_id}/conversation",
                     path_params={"thread_id": "gmail thread id"}, query={})
    transport = _Recorder()
    result = HttpToolbox.from_dict(config, transport=transport).call("conversation", {})
    assert result["ok"] is False and transport.calls == []


@pytest.mark.parametrize("override, message", [
    ({"name": "Bad-Name"}, "tool name"),
    ({"service": "nope"}, "unknown service"),
    ({"path": "/x/{id}"}, "placeholders"),
    ({"path": "/x?y=1"}, "no query"),
    ({"description": ""}, "description"),
])
def test_a_bad_file_is_refused_whole(override, message):
    with pytest.raises(HttpToolsConfigError, match=message):
        HttpToolbox.from_dict(_config(**override))


def test_a_tool_cannot_shadow_a_built_in():
    with pytest.raises(HttpToolsConfigError, match="already taken"):
        HttpToolbox.from_dict(_config(name="get_incident"), reserved={"get_incident"})


def test_the_catalogue_is_what_a_model_sees():
    schema = HttpToolbox.from_dict(_config()).catalogue()[0]
    assert schema["name"] == "failed_deliveries"
    assert set(schema["inputSchema"]["properties"]) == {"mailbox", "limit"}
    assert schema["inputSchema"]["required"] == []


def test_a_fixed_value_is_always_sent_and_cannot_be_overridden():
    """A listing that costs its upstream a call per row must stay small, no
    matter what the model asks for."""
    transport = _Recorder()
    box = HttpToolbox.from_dict(_config(query={"status": "item status"},
                                        fixed_query={"page_size": "20"}), transport=transport)
    box.call("failed_deliveries", {"status": "PENDING", "page_size": "5000"})
    assert transport.calls[0]["url"].endswith("?status=PENDING&page_size=20")
    assert "page_size" not in box.catalogue()[0]["inputSchema"]["properties"]


def test_a_fixed_value_cannot_also_be_a_caller_parameter():
    with pytest.raises(HttpToolsConfigError, match="declared twice"):
        HttpToolbox.from_dict(_config(fixed_query={"limit": "5"}))
