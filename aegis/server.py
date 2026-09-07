"""The Aegis dashboard server - everything the platform knows, in a browser.

    python3 -m aegis.server                      # pick a source in the browser
    python3 -m aegis.server <logfile> [project] [port]

Local-first like everything else: stdlib HTTP server, no build step, no
dependencies. Serves aegis/web/index.html and a JSON API over the same
pipeline the CLI demos use. v1's dashboard (app/server.py, port 8500) is a
separate program and is not touched by this one.

Model calls happen only when the user clicks for them - the page itself is
free to leave open forever.
"""

from __future__ import annotations

import json
import threading
import time
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import load_env  # noqa: E402
load_env()

from aegis.l3_storage.store import AEGIS_HOME  # noqa: E402
from aegis.l4_understanding.conformance import ConformanceEngine  # noqa: E402
from aegis.l4_understanding.flowspec import (  # noqa: E402
    FlowMiner, FlowSpec, synthesize_critical)
from aegis.l7_reasoning.explainer import Explainer  # noqa: E402
from aegis.l7_reasoning.gaps import GapReporter  # noqa: E402
from aegis.l7_reasoning.governance import Budget  # noqa: E402
from aegis.l7_reasoning.router import ModelRouter  # noqa: E402
from aegis.pipeline import Pipeline  # noqa: E402

WEB_DIR = Path(__file__).resolve().parent / "web"
DEFAULT_PORT = 8600
POLL_S = 1.0


RECENT_PATH = AEGIS_HOME / "recent.json"


class AegisApp:
    """The product shell: starts empty, attaches to any project's log from
    the UI, and can switch sources without a restart. One pipeline at a time;
    every project's data stays in its own store regardless."""

    def __init__(self, project: str | None = None,
                 log_path: str | None = None) -> None:
        self.project = ""
        self.pipeline: Pipeline | None = None
        self.log_path = ""
        self.router = ModelRouter(budget=Budget(max_calls=10, min_interval_s=1.0))
        self.explainer = Explainer(self.router)
        self.hypotheses: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._spec: FlowSpec | None = None
        self._stop = threading.Event()
        if log_path:
            self.attach(log_path, project)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                with self._lock:
                    if self.pipeline is not None:
                        self.pipeline.run_once()
            except Exception:
                pass
            self._stop.wait(POLL_S)

    # -- sources -------------------------------------------------------------

    def attach(self, raw_path: str, project: str | None = None) -> dict:
        """Point the shell at a log file. Replaces the current source; the
        old project's data stays intact in its own store."""
        path = Path(str(raw_path)).expanduser()
        if not path.is_file():
            return {"ok": False, "detail": f"not a file: {path}"}
        name = (project or "").strip() or path.stem
        with self._lock:
            if self.pipeline is not None:
                try:
                    self.pipeline.close()
                except Exception:
                    pass
            self.project = name
            self.log_path = str(path)
            self.pipeline = Pipeline(name, path)
            self.hypotheses = {}
            self._spec = None
        self._remember_recent(str(path), name)
        return {"ok": True, "project": name, "path": str(path)}

    def _remember_recent(self, path: str, project: str) -> None:
        try:
            recent = json.loads(RECENT_PATH.read_text()) if RECENT_PATH.exists() else []
        except (OSError, ValueError):
            recent = []
        recent = [r for r in recent if r.get("path") != path]
        recent.insert(0, {"path": path, "project": project,
                          "at": time.strftime("%Y-%m-%d %H:%M")})
        try:
            RECENT_PATH.parent.mkdir(parents=True, exist_ok=True)
            RECENT_PATH.write_text(json.dumps(recent[:10], indent=1))
        except OSError:
            pass

    def sources(self) -> dict:
        """What the picker offers: known projects, recent paths, and log
        files of services currently running on this machine (reusing v1's
        proven discovery - and never offering Aegis itself)."""
        recent = []
        try:
            if RECENT_PATH.exists():
                recent = [r for r in json.loads(RECENT_PATH.read_text())
                          if Path(r.get("path", "")).is_file()]
        except (OSError, ValueError):
            recent = []
        projects = []
        projects_dir = AEGIS_HOME / "projects"
        if projects_dir.is_dir():
            for entry in sorted(projects_dir.iterdir()):
                if entry.is_dir():
                    size = sum(f.stat().st_size for f in entry.rglob("*") if f.is_file())
                    projects.append({"name": entry.name,
                                     "size_kb": round(size / 1024, 1)})
        discovered = []
        try:
            from app.core import sources as v1_sources
            from app.core.discovery import discover_listening_ports
            for item in discover_listening_ports():
                if not v1_sources.is_plausible_source(item):
                    continue
                log = v1_sources.best_log_file_for_pid(int(item.get("pid", 0) or 0))
                if log:
                    discovered.append({"process": item.get("process", "?"),
                                       "port": item.get("port"), "path": log})
        except Exception:
            pass
        return {"recent": recent, "projects": projects,
                "discovered": discovered[:8]}

    # -- flow spec -----------------------------------------------------------

    def _spec_path(self) -> Path:
        return AEGIS_HOME / "projects" / self.project / "flows" / f"{self.project}-call.json"

    def spec(self) -> FlowSpec | None:
        """The saved (possibly human-edited) spec wins over a fresh mine."""
        if self.pipeline is None:
            return None
        path = self._spec_path()
        if path.exists():
            try:
                return FlowSpec.load(path)
            except (ValueError, KeyError):
                pass
        traces = self.pipeline.trace_index.traces()
        if len(traces) < 2:
            return None
        if self._spec is None or self._spec.traces_mined < len(traces):
            self._spec = FlowMiner().mine(traces, name=f"{self.project}-call")
        return self._spec

    def mark_purpose(self) -> dict:
        """One governed model call; the result is saved human-editable."""
        spec = self.spec()
        if spec is None:
            return {"ok": False, "detail": "not enough traces to mine a spec yet"}
        summary = synthesize_critical(spec, self.router)
        spec.save(self._spec_path().parent)
        return {"ok": True, "detail": summary}

    # -- views ---------------------------------------------------------------

    def state(self) -> dict:
        if self.pipeline is None:
            return {"attached": False, "sources": self.sources(),
                    "model": {"available": self.router.available(),
                              "calls_made": self.router.budget.calls_made,
                              "budget": self.router.budget.max_calls},
                    "updated_at": time.strftime("%H:%M:%S")}
        with self._lock:
            stats = self.pipeline.stats()
            incidents = [i.to_dict() for i in self.pipeline.incidents.incidents]
            notes = [s.to_dict() for s in self.pipeline.incidents.notes[-15:]]
            traces = self.pipeline.trace_index.traces()
            templates = self.pipeline.store.templates(limit=10)
            patterns = self.pipeline.memory.patterns()[:5]
            recent = [{
                "ts": e.ts, "level": e.level,
                "text": e.text_redacted.splitlines()[0][:150],
                "trace": e.trace_id[:8] if e.trace_id else "",
            } for e in self.pipeline.hot.recent(30)]

        spec = self.spec()
        verdicts = []
        if spec is not None:
            engine = ConformanceEngine(mode="shadow")
            for trace_id, events in traces.items():
                if len(events) < 4:
                    continue
                report, _ = engine.check(trace_id, events, spec)
                verdicts.append({
                    "trace_id": trace_id, "verdict": report.verdict,
                    "reason": report.reason, "duration_s": report.duration_s,
                    "opened_at": events[0].ts, "events": len(events),
                })
        verdicts.sort(key=lambda v: v["opened_at"])

        for incident in incidents:
            incident["hypothesis"] = self.hypotheses.get(incident["id"])
            incident["precedents"] = self.pipeline.memory.similar(incident, top=2)

        purpose_marked = bool(spec and any(s.critical for s in spec.steps))
        spec_view = {
            "exists": spec is not None,
            "purpose_marked": purpose_marked,
        }
        gaps = GapReporter().report(
            stats=stats, verdicts=verdicts, spec=spec_view,
            incidents=incidents,
            timed_components={op["component"] for op in []},
        )
        return {
            "attached": True,
            "project": self.project,
            "log_path": self.log_path,
            "stats": stats,
            "funnel": [
                ("log lines", stats["lines_in"]),
                ("events", stats["events"]),
                ("templates", stats["templates"]),
                ("signals", stats["signals"]),
                ("incidents", stats["incidents"]),
            ],
            "verdicts": verdicts,
            "incidents": incidents,
            "notes": notes,
            "templates": templates,
            "patterns": patterns,
            "spec": {
                "exists": spec is not None,
                "purpose_marked": purpose_marked,
                "steps": [s.to_dict() for s in spec.steps] if spec else [],
                "path": str(self._spec_path()),
            },
            "gaps": gaps,
            "events": recent,
            "model": {
                "available": self.router.available(),
                "calls_made": self.router.budget.calls_made,
                "budget": self.router.budget.max_calls,
            },
            "updated_at": time.strftime("%H:%M:%S"),
        }

    def ask(self, question: str) -> dict:
        """One governed Q&A call about what the platform currently knows.
        The context is the platform's own derived state - redacted evidence,
        verdicts, incidents - never raw logs."""
        question = (question or "").strip()[:500]
        if not question:
            return {"ok": False, "detail": "empty question"}
        if self.pipeline is None:
            return {"ok": False, "detail": "no source attached"}
        state = self.state()
        context = {
            "funnel": dict(state["funnel"]),
            "verdicts": state["verdicts"],
            "incidents": [{
                "id": i["id"], "severity": i["severity"], "status": i["status"],
                "opened_at": i["opened_at"],
                "evidence": (i.get("evidence") or [])[:4],
            } for i in state["incidents"]],
            "recent_events": [e["text"] for e in state["events"][-15:]],
            "gaps": state["gaps"],
        }
        answer = self.router.chat(
            "answer_question",
            [{"role": "user", "content":
              "You are answering a question about one service's analysed logs. "
              "Answer plainly in a few sentences. Quote evidence lines verbatim "
              "when they support you. If the data below cannot answer the "
              "question, say exactly that - never guess.\n\nDATA:\n"
              + json.dumps(context, indent=1)
              + "\n\nQUESTION: " + question}],
            purpose=f"Q&A: {question[:60]}")
        if answer is None:
            return {"ok": False,
                    "detail": "model unavailable or budget spent - the panels "
                              "above still hold everything measured"}
        return {"ok": True, "answer": answer.strip()[:2500]}

    def explain(self, incident_id: str) -> dict:
        if self.pipeline is None:
            return {"ok": False, "detail": "no source attached"}
        incident = next((i.to_dict() for i in self.pipeline.incidents.incidents
                         if i.id == incident_id), None)
        if incident is None:
            return {"ok": False, "detail": f"no incident {incident_id}"}
        precedents = self.pipeline.memory.similar(incident, top=3)
        hypothesis = self.explainer.explain(incident, precedents=precedents)
        if hypothesis is None:
            return {"ok": False,
                    "detail": "model unavailable, budget spent, or call failed"}
        self.hypotheses[incident_id] = hypothesis
        return {"ok": True, "hypothesis": hypothesis}

    def close(self) -> None:
        self._stop.set()
        if self.pipeline is not None:
            self.pipeline.close()


def make_handler(app: AegisApp):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep the terminal quiet
            pass

        def _json(self, payload, code=200):
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.startswith("/api/state"):
                self._json(app.state())
            elif self.path.startswith("/api/sources"):
                self._json(app.sources())
            elif self.path == "/" or self.path.startswith("/index"):
                page = (WEB_DIR / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)
            else:
                self._json({"ok": False, "detail": "not found"}, 404)

        def do_POST(self):
            if self.path == "/api/attach":
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    payload = json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    payload = {}
                self._json(app.attach(str(payload.get("path", "")),
                                      payload.get("project")))
            elif self.path == "/api/ask":
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    payload = json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    payload = {}
                self._json(app.ask(str(payload.get("question", ""))))
            elif self.path.startswith("/api/explain/"):
                self._json(app.explain(self.path.rsplit("/", 1)[-1]))
            elif self.path == "/api/mark-purpose":
                self._json(app.mark_purpose())
            else:
                self._json({"ok": False, "detail": "not found"}, 404)

    return Handler


def main(argv: list[str]) -> int:
    log_path = None
    project = None
    if len(argv) > 1:
        log_path = str(Path(argv[1]).expanduser())
        if not Path(log_path).exists():
            print(f"No such log file: {log_path}")
            return 1
        project = argv[2] if len(argv) > 2 else None
    port = int(argv[3]) if len(argv) > 3 else DEFAULT_PORT

    app = AegisApp(project, log_path)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    if log_path:
        print(f"Aegis watching '{app.project}' -> http://127.0.0.1:{port}")
    else:
        print(f"Aegis -> http://127.0.0.1:{port}  (pick a source in the browser)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
