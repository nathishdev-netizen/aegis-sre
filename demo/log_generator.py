"""Configurable log source for testing the agent.

Emits N log lines in a chosen scenario, so you can check the agent end to end:
does it attach, parse, track stages, and explain what happened?

Examples:
    # 50 lines of a healthy pipeline, streamed at 5/sec on port 5060
    python3 demo/log_generator.py --count 50 --scenario success

    # a run that fails on the embedding service
    python3 demo/log_generator.py --scenario failure

    # noisy/awkward input: JSON lines, tracebacks, ANSI colour, blank lines
    python3 demo/log_generator.py --scenario messy

    # write to a file instead of serving a port (tests file attachment)
    python3 demo/log_generator.py --count 100 --file /tmp/test.log

    # everything at once, forever
    python3 demo/log_generator.py --scenario mixed --count 0

Attach the agent by picking the printed port in the UI, or:
    curl -XPOST localhost:3000/api/trace-port -d '{"port":5060}'
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


# --- Scenarios ---------------------------------------------------------------
# Each yields (level, message). The agent should recover the stage sequence and,
# in the failing cases, explain what broke and what was skipped downstream.

SUCCESS = [
    ("INFO", "Request received POST /api/query"),
    ("INFO", "Authentication passed for user 4821"),
    ("INFO", "Parsing request payload"),
    ("INFO", "Retriever selected: hybrid-bm25"),
    ("INFO", "Calling embedding service"),
    ("INFO", "Embeddings generated in 142ms"),
    ("INFO", "Graph traversal complete: 18 nodes"),
    ("INFO", "LLM call started model=gpt-4o-mini"),
    ("INFO", "LLM responded in 890ms"),
    ("INFO", "Response sent status=200"),
]

FAILURE = [
    ("INFO", "Request received POST /api/query"),
    ("INFO", "Authentication passed for user 4821"),
    ("INFO", "Parsing request payload"),
    ("INFO", "Retriever selected: hybrid-bm25"),
    ("INFO", "Calling embedding service at http://embeddings:8080"),
    ("WARN", "Embedding service slow to respond (2400ms)"),
    ("ERROR", "Connection timeout after 5000ms calling embedding service"),
    ("INFO", "Retry path selected (attempt 2 of 3)"),
    ("ERROR", "Connection timeout after 5000ms calling embedding service"),
    ("WARN", "Skipping graph traversal - no embeddings available"),
    ("ERROR", "Pipeline aborted: required stage failed"),
]

DB_FAILURE = [
    ("INFO", "Request received GET /api/orders"),
    ("INFO", "Authentication passed"),
    ("INFO", "Opening database connection pool"),
    ("ERROR", "Connection refused: could not connect to postgres:5432"),
    ("WARN", "Falling back to read replica"),
    ("ERROR", "Connection refused: could not connect to replica:5432"),
    ("ERROR", "Request failed status=503"),
]

RATE_LIMIT = [
    ("INFO", "Request received POST /api/summarize"),
    ("INFO", "Parsing request payload"),
    ("INFO", "LLM call started model=gpt-4o-mini"),
    ("ERROR", "429 Too Many Requests - rate limit exceeded"),
    ("INFO", "Backing off for 2000ms"),
    ("INFO", "Retry path selected (attempt 2 of 3)"),
    ("INFO", "LLM responded in 1120ms"),
    ("INFO", "Response sent status=200"),
]

# Deliberately awkward input: the shapes that break naive line parsers.
MESSY = [
    ("INFO", '{"line": "10:00:01 INFO Structured JSON payload"}'),
    ("INFO", '{"message": "nested level field", "level": "warn"}'),
    ("ERROR", "Traceback (most recent call last):"),
    ("ERROR", '  File "/app/pipeline.py", line 88, in embed'),
    ("ERROR", "    raise TimeoutError('embedding service unreachable')"),
    ("ERROR", "TimeoutError: embedding service unreachable"),
    ("INFO", "\x1b[32mINFO\x1b[0m ANSI coloured line"),
    ("INFO", ""),
    ("INFO", "   "),
    ("INFO", "no-level line with no timestamp at all"),
    ("INFO", "Response sent status=500"),
]

# A real Python traceback: one failure that spans many physical lines.
TRACEBACK = [
    ("INFO", "Request received POST /api/embed"),
    ("INFO", "Authentication passed for user 4821"),
    ("INFO", "Calling embedding service at http://embeddings:8080"),
    ("ERROR", "Unhandled exception in request handler"),
    ("RAW", "Traceback (most recent call last):"),
    ("RAW", '  File "/app/pipeline.py", line 88, in embed'),
    ("RAW", "    return client.embed(payload)"),
    ("RAW", '  File "/app/client.py", line 42, in embed'),
    ("RAW", "    raise TimeoutError('embedding service unreachable')"),
    ("RAW", "TimeoutError: embedding service unreachable"),
    ("INFO", "Request finished status=500"),
]


SCENARIOS = {
    "success": SUCCESS,
    "failure": FAILURE,
    "db": DB_FAILURE,
    "ratelimit": RATE_LIMIT,
    "messy": MESSY,
    "traceback": TRACEBACK,
}


def build_lines(scenario: str, count: int) -> list[str]:
    """Produce `count` formatted lines (count=0 means one full cycle)."""
    if scenario == "mixed":
        pool = [s for name, s in SCENARIOS.items() if name != "messy"]
    else:
        pool = [SCENARIOS[scenario]]

    lines: list[str] = []
    cycle = 0
    while True:
        source = pool[cycle % len(pool)]
        for level, message in source:
            if level == "RAW":
                # Emitted verbatim: indentation is what marks a continuation line.
                lines.append(message)
            else:
                stamp = time.strftime("%H:%M:%S")
                lines.append(f"{stamp} {level} {message}" if message.strip() else message)
            if count and len(lines) >= count:
                return lines
        cycle += 1
        if not count:
            return lines


class GeneratorState:
    """Holds emitted lines and fans them out to connected SSE clients."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.clients: set[BaseHTTPRequestHandler] = set()
        self.history: list[str] = []

    def emit(self, line: str) -> None:
        print(line, flush=True)
        with self.lock:
            self.history.append(line)
            self.history = self.history[-500:]
            clients = list(self.clients)
        payload = f"event: log\ndata: {json.dumps({'line': line})}\n\n".encode("utf-8")
        for client in clients:
            try:
                client.wfile.write(payload)
                client.wfile.flush()
            except Exception:
                with self.lock:
                    self.clients.discard(client)

    def lines(self) -> list[str]:
        with self.lock:
            return list(self.history)


STATE = GeneratorState()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/logs":
            body = "\n".join(STATE.lines())
            raw = (body + "\n").encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return

        if self.path == "/events":
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            with STATE.lock:
                STATE.clients.add(self)
            try:
                for line in STATE.lines():
                    self.wfile.write(
                        f"event: log\ndata: {json.dumps({'line': line})}\n\n".encode("utf-8")
                    )
                self.wfile.flush()
                while True:
                    time.sleep(15)
                    self.wfile.write(b'event: ping\ndata: {"ok":true}\n\n')
                    self.wfile.flush()
            except Exception:
                pass
            finally:
                with STATE.lock:
                    STATE.clients.discard(self)
            return

        self.send_error(HTTPStatus.NOT_FOUND)


class ReusableServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Emit test logs so the agent has something real to read.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--count", type=int, default=40,
                        help="how many lines to emit; 0 = loop forever (default: 40)")
    parser.add_argument("--scenario", choices=[*SCENARIOS, "mixed"], default="mixed",
                        help="which pipeline to simulate (default: mixed)")
    parser.add_argument("--rate", type=float, default=3.0,
                        help="lines per second (default: 3)")
    parser.add_argument("--port", type=int, default=5060,
                        help="port to serve /events and /logs on (default: 5060)")
    parser.add_argument("--file", type=str, default=None,
                        help="also append lines to this file, to test file attachment")
    parser.add_argument("--seed", type=int, default=None,
                        help="seed for reproducible jitter")
    args = parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    target = None
    if args.file:
        target = Path(args.file).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")

    server = ReusableServer(("127.0.0.1", args.port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    total = "infinite" if not args.count else args.count
    print(f"Log generator on http://127.0.0.1:{args.port}  (/events, /logs)", flush=True)
    print(f"  scenario={args.scenario} count={total} rate={args.rate}/s", flush=True)
    if target:
        print(f"  also writing to {target}", flush=True)
    print(f"\nAttach the agent to port {args.port}, or:", flush=True)
    print(f"  curl -XPOST localhost:3000/api/trace-port "
          f"-H 'content-type: application/json' -d '{{\"port\":{args.port}}}'\n", flush=True)

    delay = 1.0 / args.rate if args.rate > 0 else 0.0
    emitted = 0
    try:
        while True:
            for line in build_lines(args.scenario, args.count or 0):
                STATE.emit(line)
                if target:
                    with target.open("a", encoding="utf-8") as handle:
                        handle.write(line + "\n")
                emitted += 1
                if args.count and emitted >= args.count:
                    print(f"\nEmitted {emitted} lines. Still serving on port {args.port} "
                          f"(Ctrl-C to stop).", flush=True)
                    while True:
                        time.sleep(3600)
                time.sleep(delay + random.uniform(0, delay * 0.3))
            if not args.count:
                time.sleep(2.0)
    except KeyboardInterrupt:
        print("\nStopped.", flush=True)
        sys.exit(0)


if __name__ == "__main__":
    main()
