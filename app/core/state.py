from __future__ import annotations

import json
import os
import re
import time
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from queue import Empty, Full, Queue
from typing import Any
from urllib.error import URLError, HTTPError
from urllib.request import Request, urlopen

from app.config import settings
from app.core import learn as learning
from app.core import llm, sources
from app.core.discovery import discover_listening_ports
from app.core.parser import (
    detect_branch,
    is_continuation,
    strip_ansi,
    unwrap_payload,
    unwrap_payload_preserving_indent,
    infer_cause,
    infer_component,
    infer_skipped_component,
    infer_transition,
    now_iso,
    parse_log_line,
    pick_additional_causes,
)


# Sentinel that tells a client's writer thread to exit.
_SHUTDOWN = "__shutdown__"

OWN_PID = os.getpid()
OWN_PORT = settings.port


STAGE_ORDER = [
    {"key": "request", "label": "Request"},
    {"key": "auth", "label": "Auth"},
    {"key": "parse", "label": "Parse"},
    {"key": "retrieve", "label": "Retrieve"},
    {"key": "graph", "label": "Graph"},
    {"key": "embedding", "label": "Embedding"},
    {"key": "llm", "label": "LLM"},
    {"key": "response", "label": "Response"},
]

STAGE_INDEX = {stage["key"]: index for index, stage in enumerate(STAGE_ORDER)}


@dataclass
class StageState:
    key: str
    label: str
    status: str = "pending"
    seen: bool = False
    updated_at: str | None = None


@dataclass
class LogEntry:
    timestamp: str
    level: str
    message: str
    raw_line: str
    source: str = "manual"


@dataclass
class TimelineEntry:
    time: str
    label: str
    level: str
    status: str
    message: str


@dataclass
class Snapshot:
    running: bool
    current_stage: str
    current_label: str
    status: str
    confidence: int
    summary: str
    reason: str
    possible_causes: list[str]
    suggested_fixes: list[str]
    timeline: list[dict[str, Any]]
    log_lines: list[dict[str, Any]]
    stages: list[dict[str, Any]]
    ports: list[dict[str, Any]]
    graph: dict[str, Any]
    metrics: dict[str, int]
    source: dict[str, Any]
    updated_at: str
    # "patterns" = regex-derived observation, "llm" = model reasoning. The UI uses this
    # to avoid presenting a pattern match as if it were an explanation.
    interpretation: str = "patterns"
    evidence: list[str] = field(default_factory=list)
    backend: dict[str, Any] = field(default_factory=dict)
    # What has been learned about this source's own format and vocabulary.
    profile: dict[str, Any] = field(default_factory=dict)


def _stage_list() -> list[StageState]:
    return [StageState(**stage) for stage in STAGE_ORDER]


class RuntimeState:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._clients: set[Any] = set()
        self._client_queues: dict[Any, Queue[str]] = {}
        self._demo_timer: threading.Timer | None = None
        self._watcher_thread: threading.Thread | None = None
        self._watch_stop = threading.Event()
        self._watched_path: Path | None = None
        self._watched_offset: int = 0
        self._port_thread: threading.Thread | None = None
        self._port_stop = threading.Event()
        self._port_generation = 0
        self._selected_port: int | None = None
        self._event_counter = 0
        self._interpreting = False
        self._raw_for_learning: list[str] = []
        self._snapshot = self._fresh_snapshot()

    def _fresh_snapshot(self) -> Snapshot:
        return Snapshot(
            running=False,
            current_stage="idle",
            current_label="Waiting for logs",
            status="idle",
            confidence=0,
            summary="No logs yet.",
            reason="Awaiting execution events.",
            possible_causes=[],
            suggested_fixes=[],
            timeline=[],
            log_lines=[],
            stages=[asdict(stage) for stage in _stage_list()],
            ports=[],
            graph={
                "lanes": [],
                "edges": [],
                "transition_counts": {},
                "current_branch": "main",
                "current_branch_label": "Main path",
                "branch_counter": 0,
                "last_component": None,
                "next_likely": None,
            },
            metrics={"total_events": 0, "failures": 0, "retries": 0, "skipped": 0},
            source={"type": "idle", "label": "Scanning for a live local source", "path": None},
            updated_at=now_iso(),
            interpretation="patterns",
            evidence=[],
            backend=llm.status(),
            profile={},
        )

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(asdict(self._snapshot)))

    def reset(self) -> dict[str, Any]:
        with self._lock:
            if self._demo_timer is not None:
                self._demo_timer.cancel()
                self._demo_timer = None
            self._stop_watcher_locked()
            self._stop_port_trace_locked()
            self._snapshot = self._fresh_snapshot()
        self.broadcast()
        return self.snapshot()

    def _stage(self, stage_key: str) -> dict[str, Any] | None:
        for stage in self._snapshot.stages:
            if stage["key"] == stage_key:
                return stage
        return None

    def _lane(self, component: str) -> dict[str, Any]:
        for lane in self._snapshot.graph["lanes"]:
            if lane["component"] == component:
                return lane
        lane = {
            "component": component,
            "status": "idle",
            "seen": False,
            "branch_groups": [],
            "node_count": 0,
        }
        self._snapshot.graph["lanes"].append(lane)
        return lane

    def _branch_group(self, lane: dict[str, Any], branch_id: str, branch_label: str) -> dict[str, Any]:
        for group in lane["branch_groups"]:
            if group["id"] == branch_id:
                return group
        group = {
            "id": branch_id,
            "label": branch_label,
            "status": "idle",
            "nodes": [],
        }
        lane["branch_groups"].append(group)
        return group

    def _update_lane_status(self, lane: dict[str, Any]) -> None:
        statuses = [node["status"] for group in lane["branch_groups"] for node in group["nodes"]]
        if any(status == "failed" for status in statuses):
            lane["status"] = "failed"
        elif any(status == "running" for status in statuses):
            lane["status"] = "running"
        elif any(status == "retrying" for status in statuses):
            lane["status"] = "retrying"
        elif any(status == "skipped" for status in statuses):
            lane["status"] = "skipped"
        elif statuses:
            lane["status"] = "completed"
        lane["seen"] = bool(statuses)
        lane["node_count"] = len(statuses)

    def _add_graph_node(
        self,
        *,
        component: str,
        label: str,
        status: str,
        message: str,
        timestamp: str,
        branch_id: str,
        branch_label: str,
        source: str,
        synthetic: bool = False,
        skipped: bool = False,
        raw_line: str = "",
    ) -> dict[str, Any]:
        lane = self._lane(component)
        group = self._branch_group(lane, branch_id, branch_label)
        node = {
            "id": f"n{self._event_counter}",
            "component": component,
            "label": label,
            "status": "skipped" if skipped else status,
            "message": message,
            "timestamp": timestamp,
            "branch": branch_id,
            "branch_label": branch_label,
            "source": source,
            "synthetic": synthetic,
            "skipped": skipped,
            "raw_line": raw_line or message,
        }
        group["nodes"].append(node)
        self._update_lane_status(lane)
        return node

    def _record_transition(self, from_component: str | None, to_component: str) -> None:
        if not from_component or from_component == to_component:
            return
        counts = self._snapshot.graph["transition_counts"]
        counts.setdefault(from_component, {})
        counts[from_component][to_component] = counts[from_component].get(to_component, 0) + 1

    def _most_likely_successor(self, component: str | None) -> str | None:
        if not component:
            return None
        counts = self._snapshot.graph["transition_counts"].get(component, {})
        if not counts:
            return None
        return max(counts.items(), key=lambda item: item[1])[0]

    def _new_branch_id(self, kind: str) -> str:
        self._snapshot.graph["branch_counter"] += 1
        return f"{kind}-{self._snapshot.graph['branch_counter']}"

    def _update_stage(self, stage_key: str, status: str) -> None:
        stage = self._stage(stage_key)
        if stage:
            stage["status"] = status
            stage["seen"] = True
            stage["updated_at"] = now_iso()

    def _set_stage_flow(self, stage_key: str | None, status: str) -> None:
        if stage_key and status in {"running", "retrying", "failed", "completed"}:
            self._update_stage(stage_key, status)

    # How serious each status is. A later, milder event must not quietly downgrade a
    # failure the run has already recorded - a WARNING after an ERROR was resetting the
    # whole run to "warning" and dropping its causes and fixes.
    _STATUS_SEVERITY = {
        "idle": 0, "info": 0, "observed": 0, "running": 1, "completed": 1,
        "success": 1, "warning": 2, "retrying": 2, "skipped": 2, "failed": 3,
    }

    def _set_current(self, stage_key: str | None, label: str, status: str) -> None:
        if stage_key:
            self._snapshot.current_stage = stage_key
        self._snapshot.current_label = label

        current = self._STATUS_SEVERITY.get(self._snapshot.status, 0)
        incoming = self._STATUS_SEVERITY.get(status, 0)
        # "running" always applies: it says where the run IS, not how it is going.
        if incoming >= current or status == "running":
            self._snapshot.status = status if incoming >= current else self._snapshot.status
            if status == "running" and current < 3:
                self._snapshot.status = status

    def _append_timeline(self, entry: TimelineEntry) -> None:
        self._snapshot.timeline.append(asdict(entry))
        self._snapshot.timeline = self._snapshot.timeline[-settings.max_timeline:]

    def _summarize(self) -> str:
        timeline = list(reversed(self._snapshot.timeline))
        last_failure = next((item for item in timeline if item["status"] == "failed"), None)
        if self._snapshot.status == "failed":
            tail = f" Latest evidence: {last_failure['message']}" if last_failure else ""
            return f"The run failed at {self._snapshot.current_label}.{tail}".strip()
        if self._snapshot.status == "success":
            return f"The run completed successfully after {self._snapshot.metrics['total_events']} events."
        if self._snapshot.running:
            if last_failure:
                return f"The run is currently at {self._snapshot.current_label}, and the latest issue was {last_failure['message']}."
            return f"The run is currently at {self._snapshot.current_label}."
        return "Waiting for logs."

    def _looks_like_completion(self, message: str, transition: dict[str, Any] | None) -> bool:
        lowered = message.lower()
        if transition and transition.get("stage") == "response" and transition.get("status") == "completed":
            return True
        return bool(
            re.search(r"\b(response sent|pipeline completed|execution completed|completed successfully|finished successfully|all done)\b", lowered)
        )

    def _extract_terms(self, query: str) -> list[str]:
        stopwords = {
            "a", "an", "and", "are", "as", "at", "be", "by", "do", "does", "for", "from",
            "how", "i", "in", "is", "it", "log", "logs", "of", "on", "or", "run", "that",
            "the", "this", "to", "was", "what", "when", "where", "with", "you", "we", "will",
            "have", "has", "had", "there", "any", "current", "currently", "details",
        }
        words = re.findall(r"[a-z0-9_]+", query.lower())
        terms = [word for word in words if len(word) > 2 and word not in stopwords]
        return list(dict.fromkeys(terms))

    def _refresh_backend_status(self) -> None:
        """Re-read the interpretation backend so the badge reflects reality now."""
        with self._lock:
            self._snapshot.backend = llm.status()

    def answer_query(self, query: str) -> dict[str, Any]:
        query = (query or "").strip()
        self._refresh_backend_status()
        if not query:
            return {
                "answer": "Ask a question about the current run.",
                "verdict": "unknown",
                "confidence": 0,
                "evidence": [],
                "matches": [],
                "source": "none",
            }

        # Prefer real reasoning; fall back to keyword search only if no model answers.
        if llm.is_available() and self._snapshot.log_lines:
            answered = llm.answer_question(self.snapshot(), query)
            if answered:
                return {**answered, "matches": self._extract_terms(query)}

        terms = self._extract_terms(query)
        haystacks: list[dict[str, str]] = []
        for item in reversed(self._snapshot.log_lines):
            haystacks.append({
                "kind": "log",
                "text": f"{item.get('message', '')} {item.get('raw_line', '')}".strip(),
                "time": item.get("timestamp", ""),
            })
        for item in reversed(self._snapshot.timeline):
            haystacks.append({
                "kind": "timeline",
                "text": f"{item.get('label', '')} {item.get('message', '')}".strip(),
                "time": item.get("time", ""),
            })

        if not terms:
            latest = self._snapshot.log_lines[-1]["message"] if self._snapshot.log_lines else "No logs yet."
            return {
                "answer": f"I can only see the run itself right now. Latest log: {latest}",
                "verdict": "unknown",
                "confidence": 40 if self._snapshot.log_lines else 0,
                "evidence": [latest] if self._snapshot.log_lines else [],
                "matches": [],
            }

        matches: list[dict[str, str]] = []
        for item in haystacks:
            lowered = item["text"].lower()
            if any(term in lowered for term in terms):
                matches.append(item)

        seen_texts: set[str] = set()
        evidence: list[str] = []
        for item in matches:
            text = item["text"]
            if text and text not in seen_texts:
                seen_texts.add(text)
                evidence.append(f"{item['time']}: {text}" if item["time"] else text)
            if len(evidence) >= 4:
                break

        # Keyword search cannot answer the question that was actually asked - it only
        # reports which words appear. Say exactly that, and let the model answer when
        # one is configured.
        if matches:
            answer = (
                f"Found mentions of {', '.join(terms[:3])} in this run. "
                "Keyword search cannot tell you the outcome - set OPENAI_API_KEY for a real answer."
            )
        else:
            answer = f"No mention of {', '.join(terms[:3])} in this run's logs."

        return {
            "answer": answer,
            "verdict": "unknown",
            "confidence": 0,
            "evidence": evidence,
            "matches": terms,
            "source": "keywords",
        }

    def _stop_watcher_locked(self) -> None:
        self._watch_stop.set()
        self._watched_path = None
        self._watched_offset = 0
        self._snapshot.source = {"type": "idle", "label": "Scanning for a live local source", "path": None}
        if self._watcher_thread and self._watcher_thread.is_alive():
            # The watcher is a daemon thread and will exit when it notices the stop flag.
            pass
        self._watcher_thread = None

    def _stop_port_trace_locked(self) -> None:
        self._port_stop.set()
        # Bumping the generation retires any tracer that is mid-request and cannot
        # see the stop flag yet; it exits on its next check instead of lingering.
        self._port_generation += 1
        self._selected_port = None
        self._port_thread = None

    def _is_self(self, item: dict[str, Any]) -> bool:
        """True when a discovered port belongs to this agent process."""
        return sources.is_self(item, OWN_PORT)

    def refresh_ports(self) -> list[dict[str, Any]]:
        # Never offer or attach to ourselves: reading our own SSE feed makes the
        # agent parse its own state JSON and invent failures that never happened.
        ports = [item for item in discover_listening_ports() if not self._is_self(item)]
        auto_attach_port = self._pick_auto_port(ports) if settings.auto_attach else None
        with self._lock:
            self._snapshot.ports = ports
            self._snapshot.updated_at = now_iso()
        self.broadcast()
        # Auto-attach is a convenience for an idle agent, never an override. A source
        # the user chose - a file, a pipe, or a port they clicked - must survive a
        # background port scan; otherwise the run they are watching is wiped mid-call.
        with self._lock:
            already_attached = (
                self._selected_port is not None
                or self._watched_path is not None
                or self._snapshot.source.get("type") in {"file", "pipe"}
            )
        if auto_attach_port is not None and not already_attached:
            self.attach_port(auto_attach_port)
        return ports

    def _pick_auto_port(self, ports: list[dict[str, Any]]) -> int | None:
        return sources.pick_auto_port(ports)

    # How much existing history to read when first attaching to a file. Starting at the
    # very end shows an empty dashboard until the next line happens to arrive, which
    # reads as broken - and discards the run that is often the reason for attaching.
    BACKFILL_LINES = 200

    def _watch_file(self, path: Path) -> None:
        self._watch_stop.clear()
        self._watched_path = path
        backfill: list[str] = []
        try:
            size = path.stat().st_size
            # Read the tail of what is already there, then continue from the true end
            # so nothing is ingested twice.
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                existing = handle.read().splitlines()
            backfill = [line for line in existing[-self.BACKFILL_LINES:] if line.strip()]
            self._watched_offset = size
        except (FileNotFoundError, OSError):
            self._watched_offset = 0

        self._snapshot.source = {
            "type": "file",
            "label": "Watching local log file",
            "path": str(path),
        }
        self.broadcast()

        for line in backfill:
            self.ingest_line(line, source=f"file:{path.name}")

        def loop() -> None:
            while not self._watch_stop.is_set():
                if not path.exists():
                    self._snapshot.source = {
                        "type": "file",
                        "label": "Waiting for file to appear",
                        "path": str(path),
                    }
                    self.broadcast()
                    time.sleep(1.0)
                    continue

                try:
                    current_size = path.stat().st_size
                    if current_size < self._watched_offset:
                        self._watched_offset = 0
                    if current_size > self._watched_offset:
                        with path.open("r", encoding="utf-8", errors="replace") as handle:
                            handle.seek(self._watched_offset)
                            chunk = handle.read()
                            self._watched_offset = handle.tell()
                        for line in chunk.splitlines():
                            if line.strip():
                                self.ingest_line(line, source=f"file:{path.name}")
                except Exception as exc:  # noqa: BLE001
                    # Report WHAT failed, not just the exception class. "Watch error:
                    # PermissionError" gave no way to tell a real permission problem
                    # from a bug in this loop.
                    self._snapshot.source = {
                        "type": "file",
                        "label": f"Watch error: {exc.__class__.__name__}: {exc}"[:160],
                        "path": str(path),
                    }
                    self.broadcast()
                time.sleep(0.8)

        self._watcher_thread = threading.Thread(target=loop, daemon=True)
        self._watcher_thread.start()

    def attach_file(self, raw_path: str) -> dict[str, Any]:
        path = Path(raw_path).expanduser()
        with self._lock:
            self._stop_watcher_locked()
            self._stop_port_trace_locked()
            self._clear_run_locked()
            self._watch_stop = threading.Event()
            self._watch_file(path)
        return self.snapshot()

    def set_external_source(self, label: str) -> dict[str, Any]:
        """Adopt a source that pushes to us, rather than one we pull from.

        A piped stream is not discoverable by port scanning, so it announces itself;
        any existing watcher is stopped so two sources never interleave.
        """
        with self._lock:
            self._stop_watcher_locked()
            self._stop_port_trace_locked()
            self._clear_run_locked()
            self._snapshot.source = {
                "type": "pipe",
                "label": label,
                "path": "stdin",
                "attached": True,
                "lines_seen": 0,
            }
            self._snapshot.updated_at = now_iso()
        self.broadcast()
        return self.snapshot()

    def detach_source(self) -> dict[str, Any]:
        with self._lock:
            self._stop_watcher_locked()
            self._stop_port_trace_locked()
            self._snapshot.updated_at = now_iso()
        self.broadcast()
        return self.snapshot()

    def _clear_run_locked(self) -> None:
        """Drop the previous source's run so a new attachment starts clean.

        Attaching elsewhere while keeping the old timeline, graph and metrics makes
        the UI attribute one source's failures to another.
        """
        fresh = self._fresh_snapshot()
        self._raw_for_learning = []
        keep_ports = self._snapshot.ports
        self._snapshot = fresh
        self._snapshot.ports = keep_ports
        self._event_counter = 0

    def attach_port(self, port: int) -> dict[str, Any]:
        # Prefer the log FILE the process on this port already writes. Most real apps
        # serve no log endpoint, so probing them first means seven 404s in their console
        # before we conclude what the OS could have told us immediately.
        pid = next(
            (int(item.get("pid", 0) or 0) for item in self._snapshot.ports
             if int(item.get("port", 0) or 0) == port),
            0,
        )
        if not pid:
            pid = next(
                (int(item.get("pid", 0) or 0) for item in discover_listening_ports()
                 if int(item.get("port", 0) or 0) == port),
                0,
            )
        log_path = sources.best_log_file_for_pid(pid) if pid else None
        if log_path:
            snapshot = self.attach_file(log_path)
            with self._lock:
                self._snapshot.source = {
                    "type": "file",
                    "label": f"Reading port {port}'s log file",
                    "path": log_path,
                    "attached": True,
                    "lines_seen": 0,
                    "via_port": port,
                }
                self._snapshot.updated_at = now_iso()
            self.broadcast()
            return self.snapshot()

        with self._lock:
            self._stop_watcher_locked()
            self._stop_port_trace_locked()
            # Bind a fresh stop Event and generation to THIS attach. Both are passed
            # into the thread as locals, so replacing self._port_stop on a later
            # attach can never kill the new tracer or resurrect an orphaned one.
            self._clear_run_locked()
            stop_event = threading.Event()
            self._port_stop = stop_event
            self._port_generation += 1
            generation = self._port_generation
            self._selected_port = port
            self._snapshot.source = {
                "type": "port",
                "label": f"Tracing local port {port}",
                "path": f"127.0.0.1:{port}",
            }
            self._snapshot.updated_at = now_iso()
        self.broadcast()
        self._start_port_trace_thread(port, stop_event, generation)
        return self.snapshot()

    def _is_current_port_trace(self, generation: int) -> bool:
        with self._lock:
            return self._port_generation == generation

    def _start_port_trace_thread(self, port: int, stop_event: threading.Event, generation: int) -> None:
        def loop() -> None:
            self._trace_port_loop(port, stop_event, generation)

        self._port_thread = threading.Thread(target=loop, daemon=True)
        self._port_thread.start()

    def _set_source(self, generation: int, label: str, path: str, **extra: Any) -> None:
        """Publish source status, but only if this tracer is still the current one."""
        with self._lock:
            if self._port_generation != generation:
                return
            self._snapshot.source = {"type": "port", "label": label, "path": path, **extra}
            self._snapshot.updated_at = now_iso()
        self.broadcast()

    def _trace_port_loop(self, port: int, stop_event: threading.Event, generation: int) -> None:
        probe_paths = list(settings.probe_paths)
        # Lines already ingested, so re-reading a poll endpoint or reconnecting to a
        # stream that replays its backlog does not count the same line twice.
        seen_polled: set[str] = set()
        seen_streamed: set[str] = set()

        while not stop_event.is_set() and self._is_current_port_trace(generation):
            probed: list[str] = []
            found = False

            for path in probe_paths:
                if stop_event.is_set() or not self._is_current_port_trace(generation):
                    return
                url = f"http://127.0.0.1:{port}{path}"
                probed.append(path)
                try:
                    request = Request(url, headers={"Accept": "text/event-stream, text/plain, application/json"})
                    with urlopen(request, timeout=2.5) as response:
                        content_type = response.headers.get("Content-Type", "")
                        is_stream = "text/event-stream" in content_type or path.endswith("events") or path.endswith("stream")

                        if is_stream:
                            # Connected, but no log line has arrived yet. Say exactly that
                            # rather than implying we are already reading the run - unless
                            # we have read from this source before and are just reconnecting.
                            if seen_streamed:
                                self._set_source(
                                    generation,
                                    f"Streaming from port {port} via {path}",
                                    f"127.0.0.1:{port}{path}",
                                    attached=True,
                                    lines_seen=len(seen_streamed),
                                )
                            else:
                                self._set_source(
                                    generation,
                                    f"Connected to port {port} via {path} - waiting for first line",
                                    f"127.0.0.1:{port}{path}",
                                    attached=True,
                                    lines_seen=0,
                                )
                            count = 0
                            while not stop_event.is_set() and self._is_current_port_trace(generation):
                                try:
                                    raw = response.readline()
                                except (TimeoutError, OSError):
                                    # Idle stream hit the socket timeout. The connection is
                                    # still valid - reconnect and keep reading.
                                    break
                                if not raw:
                                    break
                                line = raw.decode("utf-8", errors="replace").strip()
                                if line.startswith("data:"):
                                    payload = line[5:].strip()
                                    # SSE sources replay their backlog on every reconnect, so
                                    # without this the same lines are ingested over and over -
                                    # 22 real lines became 2868 events.
                                    if payload and payload not in seen_streamed:
                                        seen_streamed.add(payload)
                                        self.ingest_line(payload, source=f"port:{port}")
                                        count += 1
                                        if count == 1:
                                            self._set_source(
                                                generation,
                                                f"Streaming from port {port} via {path}",
                                                f"127.0.0.1:{port}{path}",
                                                attached=True,
                                                lines_seen=count,
                                            )
                            # Stream ended; fall through and re-probe.
                            if count:
                                found = True
                                break
                        else:
                            body = response.read().decode("utf-8", errors="replace")
                            # rstrip only: leading whitespace identifies stack frames
                            # and must survive to the continuation check.
                            lines = [line.rstrip() for line in body.splitlines() if line.strip()]
                            fresh = [line for line in lines if line not in seen_polled]
                            if lines:
                                self._set_source(
                                    generation,
                                    f"Reading logs from port {port} via {path}",
                                    f"127.0.0.1:{port}{path}",
                                    attached=True,
                                    lines_seen=len(seen_polled) + len(fresh),
                                )
                                for line in fresh:
                                    if stop_event.is_set() or not self._is_current_port_trace(generation):
                                        return
                                    seen_polled.add(line)
                                    self.ingest_line(line, source=f"port:{port}")
                                # Keep polling this endpoint instead of returning, so
                                # later lines are picked up too.
                                found = True
                                break
                except (HTTPError, URLError, TimeoutError, ConnectionError, OSError):
                    # A 404 raises HTTPError. `continue` here skips the for/else, which
                    # is why an app with no log endpoint used to be probed forever.
                    continue

            if not found:
                # Every probe path failed. Report it and STOP: re-probing forever turns
                # the agent into a 404 flood against an app that simply does not serve
                # logs over HTTP. The user can re-attach explicitly if that changes.
                self._set_source(
                    generation,
                    f"No readable log stream found on port {port}",
                    f"127.0.0.1:{port}",
                    attached=False,
                    lines_seen=0,
                    probed=probed,
                )
                with self._lock:
                    if self._port_generation == generation:
                        self._selected_port = None
                return

            stop_event.wait(3.0)

    def _append_continuation(self, raw_line: str) -> dict[str, Any]:
        """Attach a stack-trace line to the entry it belongs to.

        A traceback is one failure, not five. Folding continuations into the parent
        keeps metrics honest, stops fake components appearing in the graph, and gives
        the model the whole trace as a single piece of evidence.
        """
        text = strip_ansi(raw_line or "").rstrip()
        with self._lock:
            if not self._snapshot.log_lines:
                return json.loads(json.dumps(asdict(self._snapshot)))
            entry = self._snapshot.log_lines[-1]
            detail = entry.get("detail") or []
            if len(detail) < 40:
                detail.append(text)
            entry["detail"] = detail
            self._snapshot.updated_at = now_iso()

            if self._snapshot.timeline:
                last = self._snapshot.timeline[-1]
                last_detail = last.get("detail") or []
                if len(last_detail) < 40:
                    last_detail.append(text)
                last["detail"] = last_detail
        self.broadcast()
        return self.snapshot()

    def _relabel_lanes_locked(self) -> None:
        """Re-assign every node once the project's vocabulary is known.

        The first lines of a source arrive before there is enough evidence to learn from
        (a framework banner, typically), so they are labelled with the built-in guesses.
        Relabel each NODE rather than each lane: lines grouped together under a guess
        often belong to different components, so a lane must be able to split apart and
        not merely be renamed.
        """
        discovered = self._snapshot.profile.get("components") or []
        if not discovered:
            return

        loose: list[tuple[str, dict[str, Any]]] = []
        for lane in self._snapshot.graph["lanes"]:
            for group in lane["branch_groups"]:
                for node in group["nodes"]:
                    raw = node.get("raw_line") or node.get("message", "")
                    renamed = self._component_for(node.get("message", ""), raw)
                    loose.append((renamed if renamed in discovered else "runtime", node))

        merged: dict[str, dict[str, Any]] = {}
        for name, node in loose:
            lane = merged.get(name)
            if lane is None:
                lane = {
                    "component": name,
                    "status": "idle",
                    "seen": True,
                    "branch_groups": [{"id": "main", "label": "Main path",
                                       "status": "idle", "nodes": []}],
                    "node_count": 0,
                }
                merged[name] = lane
            node["component"] = name
            lane["branch_groups"][0]["nodes"].append(node)

        for lane in merged.values():
            self._update_lane_status(lane)
        self._snapshot.graph["lanes"] = list(merged.values())

    def _component_for(self, message: str, raw_line: str) -> str:
        """Name the component for a line, preferring the project's own vocabulary.

        A discovered tag beats the built-in keyword rules: the rules were written for an
        imagined pipeline, while the tag is what this project actually calls the stage.
        """
        discovered = self._snapshot.profile.get("components") or []
        if discovered:
            text = strip_ansi(raw_line or "")
            # An explicit [tag] is definitive - but only the LAST one on the line.
            # Loguru writes "app.identity:lookup:44 - [intent] ..." so an earlier module
            # path would otherwise win over the tag the developer actually wrote.
            tags = re.findall(r"\[([a-zA-Z][\w.\-]{1,30})\]", text)
            for match in reversed(tags):
                name = match.rsplit(".", 1)[-1].lower()
                if name in discovered:
                    return name
            lowered = text.lower()
            for name in discovered:
                if re.search(rf"\b{re.escape(name)}\b", lowered):
                    return name
            # The project's vocabulary is known and this line is not part of it -
            # framework banners, warnings from libraries. Say so, rather than inventing
            # a pipeline stage the project does not have.
            return "runtime"

    # Debug scaffolding a developer switched on deliberately (payload/header dumps,
    # box-drawing frames). It is not execution - counting it swamps the real events.
    _NOISE = re.compile(
        # Box-drawing characters anywhere: loggers put them after their own prefix,
        # so anchoring to the line start missed every one of them.
        r"[\u2500-\u257F]"
        r"|\[(?:hdr|header|headers|body|payload|req|res|dump|raw)\]",
        re.I,
    )

    def _is_noise(self, raw_line: str, message: str) -> bool:
        text = strip_ansi(raw_line or "")
        if self._NOISE.search(text) or self._NOISE.search(message or ""):
            return True
        # A line whose entire content is a logger prefix carries no information.
        return not re.sub(r"^\s*[\w.]+:?\s*$", "", (message or "").strip())
        return infer_component(message)

    def ingest_line(self, raw_line: str, source: str = "manual") -> dict[str, Any]:
        # Continuation lines belong to the previous event; they are not events.
        # Unwrap first (SSE sends {"line": "  File ..."}) but keep the indentation,
        # since indentation is what identifies a stack frame.
        unwrapped = unwrap_payload_preserving_indent(raw_line)
        if is_continuation(unwrapped):
            return self._append_continuation(unwrapped)

        # Payload/header dumps are debug scaffolding, not execution. Fold them into the
        # preceding event so they stay readable without swamping the metrics - 170 of
        # 200 events were header lines from DUMP_PAYLOADS.
        _peek = parse_log_line(raw_line)
        if self._is_noise(raw_line, _peek["message"]) and self._snapshot.log_lines:
            return self._append_continuation(unwrapped)

        parsed = parse_log_line(raw_line)
        with self._lock:
            # Learn before naming: otherwise the first lines are labelled with the
            # built-in guesses and the diagram carries two vocabularies at once.
            self._raw_for_learning.append(raw_line)
            if len(self._raw_for_learning) > 400:
                self._raw_for_learning = self._raw_for_learning[-400:]
            if len(self._raw_for_learning) % 5 == 0 or not self._snapshot.profile:
                previous = self._snapshot.profile.get("components") or []
                self._snapshot.profile = learning.learn(self._raw_for_learning).as_dict()
                if (self._snapshot.profile.get("components") or []) != previous:
                    self._relabel_lanes_locked()
        component = self._component_for(parsed["message"], raw_line)
        transition = infer_transition(parsed["message"])
        branch = detect_branch(parsed["message"])
        skipped_component = infer_skipped_component(parsed["message"])
        with self._lock:
            self._event_counter += 1
            self._snapshot.running = True
            self._snapshot.metrics["total_events"] += 1
            self._snapshot.log_lines.append({**parsed, "source": source})
            self._snapshot.log_lines = self._snapshot.log_lines[-settings.max_log_lines:]

            branch_id = self._snapshot.graph["current_branch"]
            branch_label = self._snapshot.graph["current_branch_label"]
            if branch:
                branch_id = self._new_branch_id(branch["kind"])
                branch_label = branch["label"]
                self._snapshot.graph["current_branch"] = branch_id
                self._snapshot.graph["current_branch_label"] = branch_label

            prev_component = self._snapshot.graph["last_component"]
            self._record_transition(prev_component, component)

            if branch and prev_component:
                expected = self._most_likely_successor(prev_component)
                skip_target = expected if expected and expected != component else prev_component
                if skip_target:
                    self._add_graph_node(
                        component=skip_target,
                        label=f"{skip_target} skipped",
                        status="skipped",
                        message=f"Skipped because {branch_label.lower()} took a different path.",
                        timestamp=parsed["timestamp"],
                        branch_id=branch_id,
                        branch_label=branch_label,
                        source=source,
                        synthetic=True,
                        skipped=True,
                    )
                    self._snapshot.metrics.setdefault("skipped", 0)
                    self._snapshot.metrics["skipped"] += 1

            if skipped_component:
                self._add_graph_node(
                    component=skipped_component,
                    label=f"{skipped_component} skipped",
                    status="skipped",
                    message=parsed["message"],
                    timestamp=parsed["timestamp"],
                    branch_id=branch_id,
                    branch_label=branch_label,
                    source=source,
                    synthetic=True,
                    skipped=True,
                )
                self._snapshot.metrics.setdefault("skipped", 0)
                self._snapshot.metrics["skipped"] += 1

            if transition:
                # A new request starts a new run: clear the previous cycle's verdict so
                # a stale "completed successfully" cannot sit next to a fresh failure.
                if transition["stage"] == "request" and self._snapshot.status in {"failed", "success"}:
                    self._snapshot.status = "running"
                    self._snapshot.reason = "Run in progress."
                    self._snapshot.possible_causes = []
                    self._snapshot.suggested_fixes = []
                    self._snapshot.evidence = []
                    self._snapshot.confidence = 0
                if transition["stage"]:
                    self._set_stage_flow(transition["stage"], transition["status"])
                if transition["status"] == "retrying":
                    self._snapshot.metrics["retries"] += 1
                if transition["status"] == "failed":
                    self._snapshot.metrics["failures"] += 1
                self._set_current(transition["stage"], transition["label"], transition["status"])
                self._append_timeline(
                    TimelineEntry(
                        time=parsed["timestamp"],
                        label=transition["label"],
                        level=parsed["level"],
                        status=transition["status"],
                        message=parsed["message"],
                    )
                )
            else:
                self._append_timeline(
                    TimelineEntry(
                        time=parsed["timestamp"],
                        label=parsed["message"] or parsed["raw_line"],
                        level=parsed["level"],
                        status="info",
                        message=parsed["message"],
                    )
                )

            # A failure cause must come from a line that reports a failure. An INFO
            # line mentioning a failure word is describing config or history, not an
            # error - that is how "timeout=45.0s" became a reported outage.
            cause = infer_cause(parsed["message"]) if parsed["level"] in {"ERROR", "WARN"} else None
            if cause:
                # Pattern match only. Labelled as such, and given no confidence score -
                # a regex hit on "timeout" is an observation, not a diagnosis. The LLM
                # pass (when configured) replaces this with real reasoning.
                self._snapshot.reason = cause["cause"]
                self._snapshot.possible_causes = [cause["cause"], *pick_additional_causes(parsed["message"])]
                self._snapshot.suggested_fixes = cause["fixes"]
                self._snapshot.confidence = 0
                self._snapshot.interpretation = "patterns"
                self._snapshot.status = "failed"
                self._snapshot.running = True

            if self._looks_like_completion(parsed["message"], transition):
                self._snapshot.running = False
                self._snapshot.current_stage = "response"
                self._snapshot.current_label = "Completed"
                self._set_stage_flow("response", "completed")
                # A run that reached the end after failing did not succeed - it finished
                # degraded. Overwriting the failure here erased the reason and the fixes,
                # leaving a run with a red ERROR line reporting "completed successfully".
                if self._snapshot.status != "failed":
                    self._snapshot.status = "success"
                    self._snapshot.reason = "Execution completed successfully."
                    self._snapshot.confidence = 0

            # A node describes the line that produced it. Stamping every node with the
            # run's overall status marked "Authentication passed" as failed once any
            # later step failed - a success line must never render as a failure.
            if transition:
                status_for_node = transition["status"]
            elif parsed["level"] == "ERROR":
                status_for_node = "failed"
            else:
                status_for_node = "observed"
            node = self._add_graph_node(
                component=component,
                label=transition["label"] if transition else parsed["message"] or component,
                status=status_for_node,
                message=parsed["message"],
                timestamp=parsed["timestamp"],
                branch_id=branch_id,
                branch_label=branch_label,
                source=source,
                raw_line=raw_line,
            )
            self._snapshot.graph["last_component"] = component
            self._snapshot.graph["last_node_id"] = node["id"]
            self._snapshot.graph["next_likely"] = self._most_likely_successor(component)

            self._snapshot.summary = self._summarize()
            self._snapshot.updated_at = now_iso()

        self.broadcast()
        self._request_interpretation()
        return self.snapshot()

    def _request_interpretation(self) -> None:
        """Ask the model to explain the run, off the ingest path.

        Interpretation must never block ingestion or the SSE stream, and only one
        call is ever in flight - log lines arrive far faster than a model responds.
        """
        if not llm.is_available():
            return
        with self._lock:
            if self._interpreting:
                return
            self._interpreting = True

        def run() -> None:
            try:
                snapshot = self.snapshot()
                result = llm.interpret_run(snapshot)
                if not result:
                    return
                with self._lock:
                    # Discard a stale result if the run was reset while we were waiting.
                    if self._snapshot.metrics["total_events"] < snapshot["metrics"]["total_events"]:
                        return
                    self._snapshot.summary = result["summary"] or self._snapshot.summary
                    self._snapshot.reason = result["reason"] or self._snapshot.reason
                    if result["causes"]:
                        self._snapshot.possible_causes = result["causes"]
                    if result["fixes"]:
                        self._snapshot.suggested_fixes = result["fixes"]
                    self._snapshot.confidence = result["confidence"]
                    self._snapshot.evidence = result["evidence"]
                    self._snapshot.interpretation = "llm"
                    self._snapshot.updated_at = now_iso()
                self.broadcast()
            finally:
                with self._lock:
                    self._interpreting = False

        threading.Thread(target=run, daemon=True).start()

    def subscribe(self, handler: Any) -> None:
        queue: Queue[str] = Queue(maxsize=32)
        with self._lock:
            self._clients.add(handler)
            self._client_queues[handler] = queue

        def pump() -> None:
            while True:
                payload = queue.get()
                if payload is _SHUTDOWN:
                    return
                try:
                    handler.send_sse("state", payload)
                except Exception:
                    self.unsubscribe(handler)
                    return

        thread = threading.Thread(target=pump, daemon=True)
        thread.start()

    def unsubscribe(self, handler: Any) -> None:
        with self._lock:
            self._clients.discard(handler)
            queue = self._client_queues.pop(handler, None)
        if queue is not None:
            try:
                queue.put_nowait(_SHUTDOWN)
            except Full:
                pass

    def broadcast(self) -> None:
        """Hand the payload to each client's queue and return immediately.

        Writing to the sockets inline used to block every caller behind the slowest
        browser tab - that is what made /api/ports hang for 25s+ while lsof itself
        returned in 0.05s. Each client now drains its own queue on its own thread, so
        a stalled reader only ever costs that reader.
        """
        payload = json.dumps({"type": "state", "state": self.snapshot()})
        with self._lock:
            queues = list(self._client_queues.values())

        for queue in queues:
            try:
                queue.put_nowait(payload)
            except Full:
                # Reader cannot keep up. Drop the stale frame and keep the newest:
                # this is live state, so the latest snapshot supersedes the backlog.
                try:
                    queue.get_nowait()
                    queue.put_nowait(payload)
                except (Empty, Full):
                    pass

    def start_demo(self) -> None:
        demo_lines = [
            "10:31:01 INFO Request received",
            "10:31:01 INFO Authentication passed",
            "10:31:02 INFO Retriever selected",
            "10:31:03 ERROR Connection timeout",
            "10:31:03 INFO Retry path selected",
            "10:31:04 WARN Fallback to cache",
            "10:31:04 INFO Skipping embedding service",
            "10:31:05 INFO Response sent",
        ]

        self.reset()

        def emit(index: int = 0) -> None:
            if index >= len(demo_lines):
                return
            self.ingest_line(demo_lines[index], source="demo")
            timer = threading.Timer(0.9, emit, args=(index + 1,))
            timer.daemon = True
            with self._lock:
                self._demo_timer = timer
            timer.start()

        emit()
