"""Operator-named run keys (AEGIS_RUN_KEYS).

Lines are shaped after a real two-service mail pipeline: a JSON-logging
intake service that gives every HTTP call its own trace_id, and a
console-logging agent that binds gmail_message_id to every line it writes
while handling one email.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aegis.contracts.events import Event  # noqa: E402
from aegis.l2_normalization.tracelinker import (  # noqa: E402
    RUN_KEYS_ENV,
    TraceLinker,
    run_key_patterns,
)

MS_DELIVERED = ('{"event": "agent.delivery_succeeded", "gmail_message_id": "19ab3f2c4d5e6f70", '
                '"trace_id": "4aa5312e4b0ba3d28b0d3da868d22282", "service": "destiin-gmail"}')
AGENT_INBOUND = ("2026-11-25T13:31:53Z [info     ] inbound_processed  gmail_message_id=19ab3f2c4d5e6f70 "
                 "gmail_thread_id=1a1eab0503cb817b outcome=replied request_id=015f3291485e4ebf "
                 "[app.api.v1.routes.webhook]")
MS_DRAFT_CALL = ('{"event": "http.request_completed", "gmail_thread_id": "1a1eab0503cb817b", '
                 '"trace_id": "b06fefde586496cd8f50cea6c46af066"}')


def _event(text: str, ts: str = "2026-11-25 13:31:53") -> Event:
    return Event(id="E", ts=ts, service="svc", level="INFO", text_redacted=text)


def test_one_email_is_one_run_across_both_services():
    """The intake's own trace_id differs from anything the agent logs, so
    without a run key the delivery and the agent's handling of the same email
    landed in different runs."""
    linker = TraceLinker(run_keys=("gmail_message_id",))
    delivered = linker.link(_event(MS_DELIVERED))
    handled = linker.link(_event(AGENT_INBOUND))
    assert delivered.trace_id == handled.trace_id == "19ab3f2c4d5e6f70"
    assert delivered.correlation_basis == "extracted"


def test_keys_are_tried_in_the_order_named():
    """A line carrying both ids belongs to the email, not the whole thread."""
    linker = TraceLinker(run_keys=("gmail_message_id", "gmail_thread_id"))
    assert linker.link(_event(AGENT_INBOUND)).trace_id == "19ab3f2c4d5e6f70"


def test_a_later_key_catches_lines_the_first_cannot():
    """Calls made back into the intake name only the thread; they still get a
    run instead of one per HTTP call."""
    linker = TraceLinker(run_keys=("gmail_message_id", "gmail_thread_id"))
    assert linker.link(_event(MS_DRAFT_CALL)).trace_id == "1a1eab0503cb817b"


def test_a_null_id_is_not_a_run_key():
    """Before the id is known loggers print null/None; taking that literally
    joined every such line into one run called "null"."""
    linker = TraceLinker(run_keys=("gmail_thread_id",))
    json_null = linker.link(_event('{"gmail_thread_id": null, "trace_id": "4aa5312e4b0ba3d2"}'))
    console_none = linker.link(_event("x gmail_thread_id=None trace_id=77778888aaaabbbb"))
    assert json_null.trace_id == "4aa5312e4b0ba3d2"
    assert console_none.trace_id == "77778888aaaabbbb"


def test_no_setting_changes_nothing(monkeypatch):
    """Other projects never set it, and must see the built-in order unchanged."""
    monkeypatch.delenv(RUN_KEYS_ENV, raising=False)
    assert TraceLinker().link(_event(MS_DELIVERED)).trace_id == "4aa5312e4b0ba3d28b0d3da868d22282"


def test_the_setting_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv(RUN_KEYS_ENV, " gmail_message_id , gmail_thread_id ")
    linker = TraceLinker()
    assert linker.run_keys == ("gmail_message_id", "gmail_thread_id")
    assert linker.link(_event(MS_DELIVERED)).trace_id == "19ab3f2c4d5e6f70"


def test_a_malformed_name_is_skipped_not_compiled():
    """A typo must not become a pattern that matches every line."""
    assert run_key_patterns((".*", "a b", "gmail_message_id")) == run_key_patterns(("gmail_message_id",))


def test_a_name_only_matches_as_a_whole_field():
    """gmail_message_id must not fire inside original_gmail_message_id."""
    linker = TraceLinker(run_keys=("gmail_message_id",))
    event = linker.link(_event("original_gmail_message_id=aaaabbbbcccc trace_id=11112222aaaa"))
    assert event.trace_id == "11112222aaaa"
