#!/usr/bin/env python3
"""Stream a program's output into the log agent, live.

No log file, no HTTP endpoint in your app, no restart changes beyond adding a pipe:

    your-start-command 2>&1 | python3 pipe.py

Everything read on stdin is echoed straight back to your terminal, so the pipe is
transparent - you still see your own output exactly as before. Lines are forwarded to
the agent in small batches in a background thread, so a slow or absent agent can never
block, delay or crash the program being watched.

Options:
    --label NAME     how the source appears in the UI (default: the command, or "piped stream")
    --agent URL      agent base URL (default: http://127.0.0.1:3000)
    --quiet          do not echo to stdout (only if something else is already showing it)
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import sys
import threading
import time
import urllib.error
import urllib.request


# Batch window: large enough that a chatty service costs few requests, short enough
# that a quiet one still appears live.
FLUSH_SECONDS = 0.4
MAX_BATCH = 200
QUEUE_LIMIT = 10000


def post(url: str, payload: dict, timeout: float = 5.0) -> bool:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout):
            return True
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Pipe a program's output into the log agent, live.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--agent", default=os.environ.get("LOG_AGENT_URL", "http://127.0.0.1:3000"))
    parser.add_argument("--label", default=None, help="name shown in the UI")
    parser.add_argument("--quiet", action="store_true", help="do not echo stdin to stdout")
    args = parser.parse_args()

    agent = args.agent.rstrip("/")
    label = args.label or os.environ.get("LOG_AGENT_LABEL") or "piped stream"

    pending: queue.Queue = queue.Queue(maxsize=QUEUE_LIMIT)
    stop = threading.Event()
    state = {"sent": 0, "dropped": 0, "connected": False, "warned": False}

    def sender() -> None:
        """Forward batches to the agent. Never raises into the reading thread."""
        batch: list[str] = []
        last = time.time()
        while not (stop.is_set() and pending.empty() and not batch):
            try:
                batch.append(pending.get(timeout=0.2))
            except queue.Empty:
                pass

            due = (time.time() - last) >= FLUSH_SECONDS
            if batch and (len(batch) >= MAX_BATCH or due or stop.is_set()):
                ok = post(f"{agent}/api/logs", {"lines": batch, "source": label})
                if ok:
                    state["sent"] += len(batch)
                    state["connected"] = True
                elif not state["warned"]:
                    # Say it once. A pipe that spams the terminal is worse than useless.
                    print(f"[pipe] cannot reach log agent at {agent} - still echoing output",
                          file=sys.stderr, flush=True)
                    state["warned"] = True
                batch = []
                last = time.time()

    thread = threading.Thread(target=sender, daemon=True)
    thread.start()

    if post(f"{agent}/api/source", {"label": label}):
        print(f"[pipe] streaming to {agent} as \"{label}\"", file=sys.stderr, flush=True)
    else:
        print(f"[pipe] log agent not reachable at {agent} - will keep trying",
              file=sys.stderr, flush=True)

    try:
        for raw in sys.stdin:
            line = raw.rstrip("\n")
            if not args.quiet:
                # Echo first: the watched program's own output must never be delayed
                # or lost because of anything this tool does.
                sys.stdout.write(raw)
                sys.stdout.flush()
            try:
                pending.put_nowait(line)
            except queue.Full:
                state["dropped"] += 1
    except KeyboardInterrupt:
        pass
    except Exception as exc:  # noqa: BLE001 - never take the watched program down
        print(f"[pipe] read error: {exc.__class__.__name__}: {exc}", file=sys.stderr, flush=True)
    finally:
        stop.set()
        thread.join(timeout=3.0)
        note = f"[pipe] forwarded {state['sent']} lines"
        if state["dropped"]:
            note += f" ({state['dropped']} dropped - agent could not keep up)"
        print(note, file=sys.stderr, flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
