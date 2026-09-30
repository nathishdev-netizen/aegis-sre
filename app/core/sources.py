"""Source selection and probing - the layer that decides what to attach to.

This is the "connection layer" concern kept separate from state management: which
local processes are plausible log sources, which endpoint on a port actually serves
logs, and how to describe a source honestly in the UI.

The functions here are pure or I/O-only and carry no shared state, which makes the
attachment rules directly testable without spinning up threads or a server.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.config import settings


OWN_PID = os.getpid()

# Ports that are never useful to attach to, even though they are listening.
IGNORED_PORTS = frozenset({
    5432, 5433,   # postgres
    3306,         # mysql
    6379,         # redis
    27017,        # mongodb
    11211,        # memcached
    9200, 9300,   # elasticsearch
    2379,         # etcd
    4040,         # ngrok inspector
    7000, 5000,   # macOS AirPlay / ControlCenter
})

# Processes that are infrastructure, whatever port they hold. A database writes a log
# file, so a port click on one "succeeds" and floods the dashboard with noise.
IGNORED_PROCESSES = frozenset({
    "postgres", "postmaster", "mysqld", "redis-server", "mongod", "memcached",
    "rapportd", "controlce", "controlcenter", "ngrok",
})


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of probing one endpoint on a port."""
    ok: bool
    path: str | None = None
    is_stream: bool = False
    content_type: str = ""
    lines: tuple[str, ...] = ()
    error: str | None = None


def is_self(item: dict[str, Any], own_port: int | None = None) -> bool:
    """True when a discovered port belongs to this agent process.

    Attaching to ourselves makes the agent parse its own state JSON and report
    failures that never happened, so this check guards both discovery and attach.
    """
    port = own_port if own_port is not None else settings.port
    return int(item.get("pid", 0) or 0) == OWN_PID or int(item.get("port", 0) or 0) == port


def is_plausible_source(item: dict[str, Any]) -> bool:
    """Whether a listening port is worth probing for logs at all."""
    if is_self(item):
        return False
    if int(item.get("port", 0) or 0) in IGNORED_PORTS:
        return False
    name = str(item.get("process", "")).lower()
    return not any(name.startswith(p) for p in IGNORED_PROCESSES)


def rank_candidates(ports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order discovered ports by how likely they are to be an app emitting logs.

    The demo source ranks first so the out-of-box experience works; databases and
    infrastructure are filtered out entirely rather than merely deprioritised.
    """
    candidates = [item for item in ports if is_plausible_source(item)]

    def score(item: dict[str, Any]) -> tuple[int, int]:
        port = int(item.get("port", 0) or 0)
        process = str(item.get("process", "")).lower()
        if port == 5055:                      # bundled demo backend
            rank = 0
        elif "python" in process or "node" in process:
            rank = 1                          # likely an app server
        else:
            rank = 2
        return (rank, port)

    return sorted(candidates, key=score)


def pick_auto_port(ports: list[dict[str, Any]]) -> int | None:
    """Best guess at which local port to attach to, or None when nothing qualifies."""
    ranked = rank_candidates(ports)
    return int(ranked[0]["port"]) if ranked else None


def probe_endpoint(port: int, path: str, timeout: float = 2.5) -> ProbeResult:
    """Check whether one endpoint serves readable logs.

    A 200 alone is not enough: any web app returns 200 on some path. A source counts
    as readable only if it declares an event stream or actually returns text lines.
    """
    url = f"http://127.0.0.1:{port}{path}"
    try:
        request = Request(url, headers={"Accept": "text/event-stream, text/plain, application/json"})
        with urlopen(request, timeout=timeout) as response:
            content_type = response.headers.get("Content-Type", "")
            is_stream = (
                "text/event-stream" in content_type
                or path.endswith("events")
                or path.endswith("stream")
            )
            if is_stream:
                return ProbeResult(ok=True, path=path, is_stream=True, content_type=content_type)

            body = response.read().decode("utf-8", errors="replace")
            lines = tuple(line.strip() for line in body.splitlines() if line.strip())
            if not lines:
                return ProbeResult(ok=False, path=path, content_type=content_type, error="empty body")
            return ProbeResult(ok=True, path=path, is_stream=False, content_type=content_type, lines=lines)
    except (HTTPError, URLError, TimeoutError, ConnectionError, OSError) as exc:
        return ProbeResult(ok=False, path=path, error=exc.__class__.__name__)


def describe_source(
    port: int,
    *,
    path: str | None = None,
    attached: bool,
    lines_seen: int = 0,
    streaming: bool = False,
) -> dict[str, Any]:
    """Build the source card shown in the UI.

    Reports connection and data separately: "connected but no lines yet" is a real
    and common state, and calling it "streaming" would overstate what we know.
    """
    if not attached:
        label = f"No readable log stream found on port {port}"
    elif lines_seen == 0:
        label = f"Connected to port {port} via {path} - waiting for first line"
    elif streaming:
        label = f"Streaming from port {port} via {path}"
    else:
        label = f"Reading logs from port {port} via {path}"

    return {
        "type": "port",
        "label": label,
        "path": f"127.0.0.1:{port}{path or ''}",
        "attached": attached,
        "lines_seen": lines_seen,
    }


def log_files_for_pid(pid: int) -> list[str]:
    """Log files the process on this port already has open for writing.

    Far more reliable than guessing conventional paths: if the app writes a log file,
    the OS knows about it. Lets a click on a port attach to that app's real log even
    when it serves nothing over HTTP.
    """
    if not pid:
        return []
    try:
        result = subprocess.run(
            ["lsof", "-p", str(pid)],
            capture_output=True, text=True, check=False, timeout=5,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return []

    candidates: list[str] = []
    redirected: set[str] = set()
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 9:
            continue
        path = parts[-1]
        if not path.startswith("/") or not path.endswith(".log"):
            continue
        # Skip the OS and other apps' logs - we want this project's own output.
        if any(path.startswith(prefix) for prefix in ("/private/var/", "/var/", "/System/", "/Library/")):
            continue
        # FD 1 and 2 are stdout and stderr: a shell redirect, not a file the
        # app chose to open. It usually holds a few startup lines while the
        # app's real logger writes somewhere else entirely.
        fd = parts[3].rstrip("rwu") if len(parts) > 3 else ""
        if fd in ("1", "2"):
            redirected.add(path)
        if path not in candidates:
            candidates.append(path)
    # A file reached ONLY through stdout/stderr ranks below one the app opened
    # for itself. Both can sit in .logs/ with near-identical names.
    return ([p for p in candidates if p not in redirected]
            + [p for p in candidates if p in redirected])


def best_log_file_for_pid(pid: int) -> str | None:
    """Pick the most likely application log among the files a process has open."""
    files = log_files_for_pid(pid)
    if not files:
        return None
    # A file under a .logs/ or logs/ directory is almost certainly the app's own.
    # log_files_for_pid already puts app-opened files ahead of stdout/stderr
    # redirects, so the FIRST such match is the better one - "chatbot.log"
    # (uvicorn's stdout, two lines) and "chatbot.app.log" (the application's
    # own logger, thousands) both live in .logs/, and picking by directory
    # alone returned whichever the OS happened to list first.
    for path in files:
        if "/.logs/" in path or "/logs/" in path:
            return path
    return files[0]
