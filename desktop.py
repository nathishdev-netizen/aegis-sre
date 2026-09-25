#!/usr/bin/env python3
"""Log Agent as a desktop application.

Runs the agent's HTTP server on a loopback port and shows its dashboard in a native
window - no browser, no address bar, no tab to lose. The server is an implementation
detail here, not something the user visits.

    python3 desktop.py

The window owns the server's lifetime: closing the window stops it.
"""

from __future__ import annotations

import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

WINDOW_TITLE = "Log Agent"
WINDOW_WIDTH = 1380
WINDOW_HEIGHT = 920
MIN_WIDTH = 940
MIN_HEIGHT = 640
STARTUP_TIMEOUT = 20.0


def free_port(preferred: int) -> int:
    """Return `preferred` if it is free, otherwise any open port.

    A desktop app must not fail to launch because something else already holds the
    configured port - it has no terminal in which to explain itself.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        if probe.connect_ex(("127.0.0.1", preferred)) != 0:
            return preferred
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_until_ready(url: str, timeout: float = STARTUP_TIMEOUT) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0):
                return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.15)
    return False


def main() -> int:
    try:
        import webview
    except ImportError:
        print("pywebview is not installed.\n"
              "  .venv/bin/pip install pywebview\n"
              "Or open the dashboard in a browser instead: python3 run.py",
              file=sys.stderr)
        return 1

    from app.config import settings
    from app.core.state import RuntimeState
    from app import server as agent_server

    port = free_port(settings.port)
    # state.py compares discovered ports against this to avoid attaching to itself,
    # so it has to agree with the port actually bound.
    agent_server.PORT = port
    import app.core.state as state_module
    state_module.OWN_PORT = port

    runtime: RuntimeState = agent_server.runtime
    runtime.refresh_ports()

    httpd = agent_server.ReusableHTTPServer(("127.0.0.1", port), agent_server.RequestHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    base = f"http://127.0.0.1:{port}"
    if not wait_until_ready(f"{base}/api/state"):
        print(f"The agent did not start on {base}", file=sys.stderr)
        httpd.server_close()
        return 1

    window = webview.create_window(
        WINDOW_TITLE,
        base,
        width=WINDOW_WIDTH,
        height=WINDOW_HEIGHT,
        min_size=(MIN_WIDTH, MIN_HEIGHT),
        confirm_close=False,
    )

    def on_closed() -> None:
        # The window owns the server: leaving it running would hold the port and
        # keep tailing files after the app is visibly gone.
        try:
            httpd.shutdown()
        except Exception:
            pass
        httpd.server_close()

    window.events.closed += on_closed

    # http.server is threaded but modest; this app is single-user and local, so the
    # default backend (WKWebView on macOS) is all the shell needs.
    webview.start()
    return 0


if __name__ == "__main__":
    sys.exit(main())
