from __future__ import annotations

import json
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from app.config import settings
from app.core import llm
from app.core.state import RuntimeState


ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"
INDEX_HTML = WEB_ROOT / "index.html"
HOST = settings.host
PORT = settings.port

runtime = RuntimeState()


def read_file(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write_json(handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
    raw = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


class RequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return

    def handle_one_request(self) -> None:
        # A browser closing a tab or an SSE reader hanging up raises ConnectionReset /
        # BrokenPipe here. That is normal client behaviour, not a server fault, and the
        # default handler prints a full traceback for every one of them.
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError, TimeoutError):
            self.close_connection = True

    def send_sse(self, event: str, data: str) -> None:
        self.wfile.write(f"event: {event}\n".encode("utf-8"))
        self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
        self.wfile.flush()

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/":
            html = read_file(INDEX_HTML).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            # The dashboard is edited while the agent runs; without these a browser
            # serves a stale copy on refresh and the user sees fixes that never arrive.
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
            return

        if self.path == "/api/build":
            # The dashboard is edited while the agent runs. A page that never notices
            # leaves the user hard-refreshing to see fixes - so it checks this instead.
            write_json(self, HTTPStatus.OK, {"build": str(int(INDEX_HTML.stat().st_mtime))})
            return

        if self.path == "/api/state":
            write_json(self, HTTPStatus.OK, {"state": runtime.snapshot()})
            return

        if self.path == "/api/ports":
            write_json(self, HTTPStatus.OK, {"ports": runtime.refresh_ports()})
            return

        if self.path == "/events":
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            runtime.subscribe(self)
            try:
                self.send_sse("state", json.dumps({"type": "state", "state": runtime.snapshot()}))
                while True:
                    time.sleep(15)
                    self.send_sse("ping", json.dumps({"ok": True}))
            except Exception:
                pass
            finally:
                runtime.unsubscribe(self)
            return

        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        body = json.loads(raw or "{}")

        if self.path == "/api/log":
            line = body.get("line", "")
            snapshot = runtime.ingest_line(line, source="manual")
            write_json(self, HTTPStatus.OK, {"ok": True, "state": snapshot})
            return

        if self.path == "/api/logs":
            # Batch ingest for piped sources. One HTTP request per line would cap a
            # live service at a few hundred lines a second; a batch keeps up with any
            # log volume a local app produces.
            lines = body.get("lines") or []
            label = body.get("source") or "pipe"
            if not isinstance(lines, list):
                write_json(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": "lines must be a list"})
                return
            snapshot = None
            for line in lines:
                if isinstance(line, str) and line.strip():
                    snapshot = runtime.ingest_line(line, source=label)
            write_json(self, HTTPStatus.OK, {"ok": True, "ingested": len(lines),
                                             "state": snapshot or runtime.snapshot()})
            return

        if self.path == "/api/source":
            # A piped source announces itself so the UI can name it honestly.
            runtime.set_external_source(str(body.get("label") or "piped stream"))
            write_json(self, HTTPStatus.OK, {"ok": True})
            return

        if self.path == "/api/ask":
            query = body.get("query", "")
            write_json(self, HTTPStatus.OK, {"ok": True, "result": runtime.answer_query(query)})
            return

        if self.path == "/api/reset":
            write_json(self, HTTPStatus.OK, {"ok": True, "state": runtime.reset()})
            return

        if self.path == "/api/demo/start":
            runtime.start_demo()
            write_json(self, HTTPStatus.OK, {"ok": True})
            return

        if self.path == "/api/ports/refresh":
            write_json(self, HTTPStatus.OK, {"ok": True, "ports": runtime.refresh_ports()})
            return

        if self.path == "/api/trace-port":
            port = int(body.get("port", 0))
            if not port:
                write_json(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Missing port"})
                return
            snapshot = runtime.attach_port(port)
            write_json(self, HTTPStatus.OK, {"ok": True, "state": snapshot})
            return

        if self.path == "/api/attach":
            path = body.get("path", "")
            if not path:
                write_json(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Missing path"})
                return
            snapshot = runtime.attach_file(path)
            write_json(self, HTTPStatus.OK, {"ok": True, "state": snapshot})
            return

        if self.path == "/api/detach":
            write_json(self, HTTPStatus.OK, {"ok": True, "state": runtime.detach_source()})
            return

        self.send_error(HTTPStatus.NOT_FOUND)


class ReusableHTTPServer(ThreadingHTTPServer):
    # Without this a restart fails with "Address already in use" while the previous
    # socket sits in TIME_WAIT.
    allow_reuse_address = True
    daemon_threads = True


def main() -> None:
    runtime.refresh_ports()
    # Resume the source watched before the last shutdown: an agent that comes back
    # empty looks broken, and the user has to re-attach to see anything at all.
    if runtime.restore_last_source():
        print("  resumed watching the last source")
    server = ReusableHTTPServer((HOST, PORT), RequestHandler)
    backend = llm.status()
    print(f"Log Intelligence Agent running at http://{HOST}:{PORT}")
    print(f"  interpretation: {backend['mode']} - {backend['detail']}")
    print(f"  auto-attach:    {'on' if settings.auto_attach else 'off'}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
