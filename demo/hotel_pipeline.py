from __future__ import annotations

import json
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


HOST = "127.0.0.1"
PORT = 5055


class DemoState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.clients: set[BaseHTTPRequestHandler] = set()
        self.history: list[str] = []
        self.run_id = 0
        self.running = False
        self._timer: threading.Timer | None = None

    def log(self, line: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        entry = f"{timestamp} {line}"
        print(entry, flush=True)
        with self.lock:
            self.history.append(entry)
            self.history = self.history[-300:]
            payload = f"event: log\ndata: {json.dumps({'line': entry})}\n\n"
            clients = list(self.clients)
        for client in clients:
            try:
                client.wfile.write(payload.encode("utf-8"))
                client.wfile.flush()
            except Exception:
                self.unsubscribe(client)

    def subscribe(self, handler: BaseHTTPRequestHandler) -> None:
        with self.lock:
            self.clients.add(handler)

    def unsubscribe(self, handler: BaseHTTPRequestHandler) -> None:
        with self.lock:
            self.clients.discard(handler)

    def snapshot_lines(self) -> list[str]:
        with self.lock:
            return list(self.history)

    def emit_cycle(self) -> None:
        with self.lock:
            self.run_id += 1
            run_id = self.run_id
        hotel_mode = run_id % 2 == 1
        scenario = [
            "INFO Request received",
            "INFO Parsing booking payload",
            "INFO Extracting trip details",
        ]
        if hotel_mode:
            scenario += [
                "INFO Searching hotel inventory",
                "INFO Hotel details found for 12 options",
                "INFO Selected hotel: Sunset Residency",
                "INFO Hotel booking confirmed",
                "INFO Response sent",
            ]
        else:
            scenario += [
                "INFO Searching hotel inventory",
                "WARN Hotel service slow to respond",
                "ERROR Hotel lookup timeout",
                "INFO Retry path selected",
                "INFO Fallback to cached hotel results",
                "INFO Response sent",
            ]

        for line in scenario:
            self.log(line)
            time.sleep(0.8)

    def start_loop(self) -> None:
        def loop() -> None:
            while True:
                self.emit_cycle()
                time.sleep(6.0)

        if not self.running:
            # Keep the demo moving so the agent always has something live to watch.
            self.running = True
            thread = threading.Thread(target=loop, daemon=True)
            thread.start()


STATE = DemoState()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return

    def send_text(self, status: int, body: str, content_type: str = "text/plain; charset=utf-8") -> None:
        raw = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/":
            self.send_text(
                HTTPStatus.OK,
                "Hotel Pipeline Demo is running.\nOpen /logs for the current log stream.",
            )
            return

        if self.path == "/logs":
            body = "\n".join(STATE.snapshot_lines()) + ("\n" if STATE.snapshot_lines() else "")
            self.send_text(HTTPStatus.OK, body)
            return

        if self.path == "/events":
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            STATE.subscribe(self)
            try:
                for line in STATE.snapshot_lines():
                    self.wfile.write(f"event: log\ndata: {json.dumps({'line': line})}\n\n".encode("utf-8"))
                self.wfile.flush()
                while True:
                    time.sleep(15)
                    self.wfile.write(b"event: ping\ndata: {\"ok\":true}\n\n")
                    self.wfile.flush()
            except Exception:
                pass
            finally:
                STATE.unsubscribe(self)
            return

        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/run":
            thread = threading.Thread(target=STATE.emit_cycle, daemon=True)
            thread.start()
            self.send_text(HTTPStatus.OK, "started\n")
            return

        self.send_error(HTTPStatus.NOT_FOUND)


def main() -> None:
    STATE.start_loop()
    print(f"Hotel Pipeline Demo running at http://{HOST}:{PORT}", flush=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
