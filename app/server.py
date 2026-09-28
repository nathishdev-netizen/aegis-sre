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



# -- the Aegis bridge --------------------------------------------------------
# v1 is frozen and must never import aegis at module level: the watcher has to
# keep working if the analysis layer is absent or broken. So the bridge is
# lazy, built once on first use, and every failure is reported as a JSON
# payload rather than a 500 that takes the page down with it.
_AEGIS = None
_AEGIS_TRIED = False


def _aegis():
    global _AEGIS, _AEGIS_TRIED
    if _AEGIS is not None or _AEGIS_TRIED:
        return _AEGIS
    _AEGIS_TRIED = True
    try:
        from aegis.server import AegisApp
        _AEGIS = AegisApp()
    except Exception:
        _AEGIS = None
    return _AEGIS


def _catch_up_with_v1(app) -> None:
    """Point Aegis at whatever v1 just attached to.

    The UI attaches through v1's /api/attach and Aegis only FOLLOWS, so a
    combine issued immediately afterwards used to answer "attach a source
    first" - the bridge had not yet heard about it.
    """
    try:
        source = getattr(runtime, "_snapshot", None)
        source = getattr(source, "source", None) or {}
        path = source.get("path")
        if (path and source.get("type") == "file"
                and path != app.log_path and Path(path).is_file()):
            app.attach(path)
    except Exception:
        pass


def read_file(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write_json(handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
    raw = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


def _query_window(path: str) -> str:
    _, _, query = path.partition("?")
    for pair in query.split("&"):
        key, _, value = pair.partition("=")
        if key == "window" and value:
            return value
    return "24h"


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
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
            return

        if self.path == "/healthz":
            write_json(self, HTTPStatus.OK,
                       {"ok": True, "service": "log-intelligence",
                        "version": "1.0.0"})
            return

        if self.path.startswith("/api/aegis/"):
            app = _aegis()
            if app is None:
                write_json(self, HTTPStatus.OK,
                           {"ok": False, "detail": "aegis unavailable"})
                return
            tail = self.path[len("/api/aegis/"):]
            name, _, rest = tail.partition("/")
            query = ""
            if "?" in name:
                name, _, query = name.partition("?")
            if "?" in rest:
                rest, _, query = rest.partition("?")
            force = "force=1" in query
            if name == "state":
                write_json(self, HTTPStatus.OK, app.state())
                return
            if name == "sources":
                write_json(self, HTTPStatus.OK, app.sources(force=force))
                return
            if name == "providers":
                write_json(self, HTTPStatus.OK, {"providers": app.providers()})
                return
            if name == "proposal" and rest:
                write_json(self, HTTPStatus.OK, app.proposal(rest))
                return
            write_json(self, HTTPStatus.NOT_FOUND,
                       {"ok": False, "detail": f"no such route: {self.path}"})
            return

        if self.path == "/api/state":
            write_json(self, HTTPStatus.OK, {"state": runtime.snapshot()})
            return

        if self.path == "/api/ports":
            write_json(self, HTTPStatus.OK, {"ports": runtime.refresh_ports()})
            return

        if self.path == "/api/audit" or self.path.startswith("/api/audit?"):
            write_json(self, HTTPStatus.OK, {"report": runtime.audit_report(_query_window(self.path))})
            return

        if self.path == "/api/audit/download" or self.path.startswith("/api/audit/download?"):
            from app.core.audit import render_html
            window = _query_window(self.path)
            report = runtime.audit_report_object(window)
            body = render_html(report, runtime.source_label()).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Disposition",
                             f'attachment; filename="audit-report-{window}.html"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/events":
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            runtime.subscribe(self)
            try:
                self.send_sse("state", json.dumps({"type": "state", "state": runtime.wire_snapshot()}))
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

        if self.path.startswith("/api/aegis/"):
            app = _aegis()
            if app is None:
                write_json(self, HTTPStatus.OK,
                           {"ok": False, "detail": "aegis unavailable"})
                return
            # The UI addresses some of these as /api/aegis/explain/INC-1 and
            # others with the id in the body. Both are real call sites, so
            # both are accepted rather than one silently matching nothing.
            tail = self.path[len("/api/aegis/"):]
            name, _, rest = tail.partition("/")
            if rest and not body.get("incident_id"):
                body = {**body, "incident_id": rest}
            if name == "combine":
                # v1 owns the attach; catch up before answering, or the first
                # combine after a fresh attach is rejected as "no source".
                _catch_up_with_v1(app)
                write_json(self, HTTPStatus.OK, app.combine(
                    str(body.get("path", "")), str(body.get("service", ""))))
                return
            if name == "explain":
                write_json(self, HTTPStatus.OK,
                           app.explain(str(body.get("incident_id", ""))))
                return
            if name == "investigate":
                write_json(self, HTTPStatus.OK,
                           app.investigate(str(body.get("incident_id", ""))))
                return
            if name == "remediate":
                write_json(self, HTTPStatus.OK, app.remediate(
                    str(body.get("incident_id", "")), str(body.get("repo_path", ""))))
                return
            if name == "simulate":
                write_json(self, HTTPStatus.OK,
                           app.simulate(str(body.get("target", ""))))
                return
            if name == "analyze":
                write_json(self, HTTPStatus.OK,
                           app.analyze_project(str(body.get("repo_path", ""))))
                return
            if name == "understand":
                write_json(self, HTTPStatus.OK,
                           app.understand(force=bool(body.get("force"))))
                return
            if name == "build-code-graph":
                write_json(self, HTTPStatus.OK,
                           app.build_code_graph(str(body.get("repo_path", ""))))
                return
            if name == "mark-purpose":
                write_json(self, HTTPStatus.OK, app.mark_purpose())
                return
            if name == "outcome":
                write_json(self, HTTPStatus.OK, app.record_outcome(
                    str(body.get("incident_id", "")), str(body.get("outcome", "")),
                    str(body.get("note", ""))))
                return
            if name == "verify-fix":
                write_json(self, HTTPStatus.OK,
                           app.verify_fix(str(body.get("incident_id", ""))))
                return
            if name == "suppress":
                write_json(self, HTTPStatus.OK, app.suppress(
                    str(body.get("template_id", "")), str(body.get("reason", "")),
                    bool(body.get("undo"))))
                return
            if name == "wipe-project":
                write_json(self, HTTPStatus.OK,
                           app.wipe_project(str(body.get("project", ""))))
                return
            if name == "provider":
                if rest:
                    write_json(self, HTTPStatus.OK, app.remove_provider(rest))
                    return
                write_json(self, HTTPStatus.OK, app.add_provider(body))
                return
            if name == "apply-fix":
                # run_tests defaults to TRUE only when the caller says so.
                # A project suite can make live LLM and database calls, and
                # apply() runs it twice (baseline, then patched), so it is the
                # user's spend to authorise - not a silent default.
                write_json(self, HTTPStatus.OK, app.apply_fix(
                    str(body.get("incident_id", "")),
                    str(body.get("repo_path", "")),
                    bool(body.get("allow_dirty")),
                    run_tests=bool(body.get("run_tests")),
                    fast_only=bool(body.get("fast_only"))))
                return
            if name == "revert-fix":
                write_json(self, HTTPStatus.OK, app.revert_fix(
                    str(body.get("incident_id", "")),
                    str(body.get("repo_path", "")),
                    str(body.get("backup_path", "")),
                    list(body.get("files") or [])))
                return
            if name == "proposal":
                write_json(self, HTTPStatus.OK,
                           app.proposal(str(body.get("incident_id", ""))))
                return
            write_json(self, HTTPStatus.NOT_FOUND,
                       {"ok": False, "detail": f"no such route: {self.path}"})
            return

        if self.path == "/api/log":
            line = body.get("line", "")
            snapshot = runtime.ingest_line(line, source="manual")
            write_json(self, HTTPStatus.OK, {"ok": True, "state": snapshot})
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
            # Aegis follows v1's source, and the port picker is how the UI
            # attaches - so telling Aegis only from /api/attach left it
            # silently detached for every user who picked a port instead of
            # typing a path. Same call, same place, as the file route.
            path = (((snapshot or {}).get("source") or {}).get("path")) or ""
            app = _aegis()
            if app is not None and path:
                try:
                    app.attach(path, body.get("project") or None)
                except Exception:
                    pass
            write_json(self, HTTPStatus.OK, {"ok": True, "state": snapshot})
            return

        if self.path == "/api/attach":
            path = body.get("path", "")
            if not path:
                write_json(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Missing path"})
                return
            snapshot = runtime.attach_file(path)
            # Aegis follows v1's source rather than owning one. Telling it
            # here - not lazily on the next call - is what makes the very
            # first combine or state read after an attach correct.
            app = _aegis()
            if app is not None:
                try:
                    app.attach(path, body.get("project") or None)
                except Exception:
                    pass
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

