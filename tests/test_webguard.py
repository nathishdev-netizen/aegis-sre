"""Regression tests - the localhost request guard.

Binding to 127.0.0.1 does not stop a browser: any website a user has open
can POST to a localhost port (CSRF), and a domain that re-resolves to
127.0.0.1 can even read responses (DNS rebinding). Each case below is one
of those attacks, or one legitimate client that must keep working.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.webguard import request_allowed  # noqa: E402


def test_curl_and_scripts_stay_welcome():
    """Non-browser clients send no Origin; the threat model is the browser."""
    assert request_allowed("127.0.0.1:3000", "") is True
    assert request_allowed("localhost:8600", "") is True


def test_the_apps_own_ui_passes():
    assert request_allowed("localhost:3000", "http://localhost:3000") is True
    assert request_allowed("127.0.0.1:3000", "http://127.0.0.1:3000") is True
    assert request_allowed("[::1]:3000", "http://[::1]:3000") is True


def test_a_website_posting_at_localhost_is_refused():
    """The CSRF case: the browser stamps the true Origin and a page cannot
    forge it - this header is the entire defence."""
    assert request_allowed("127.0.0.1:3000", "https://evil.example") is False
    assert request_allowed("localhost:3000", "http://evil.example:3000") is False


def test_dns_rebinding_is_refused_by_host():
    """A hostile domain rebound to 127.0.0.1 arrives with its own name in
    Host - the one trace of where the browser thinks it is."""
    assert request_allowed("evil.example:3000", "") is False
    assert request_allowed("127.0.0.1.evil.example", "") is False
    assert request_allowed("", "") is False


def test_lookalike_origins_do_not_pass():
    assert request_allowed("127.0.0.1:3000", "http://localhost.evil.example") is False
    assert request_allowed("127.0.0.1:3000", "http://127.0.0.1.evil.example") is False


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
    print(f"\n{'FAILED' if failures else 'All webguard tests passed'}")
    sys.exit(1 if failures else 0)
