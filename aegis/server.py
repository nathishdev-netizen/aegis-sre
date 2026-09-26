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
import os
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
from aegis.l7_reasoning.governance import Budget  # noqa: E402
from aegis.l7_reasoning.router import ModelRouter  # noqa: E402
from aegis.pipeline import Pipeline  # noqa: E402
from aegis.l1_ingestion.suggest import (  # noqa: E402
    cwd_for_pid, probe_pids, suggest_ports)
from aegis.l4_understanding.codebase import analyze_repo  # noqa: E402
from aegis.l4_understanding.dependencies import DependencyMap  # noqa: E402
from aegis.l4_understanding.simulate import simulate_failure  # noqa: E402
from aegis.l1_ingestion.providers import ProviderRegistry, preset_choices  # noqa: E402
from aegis.l1_ingestion.providers import LogFilter  # noqa: E402
from aegis.l7_reasoning.router import active_profile, _route_for  # noqa: E402

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
        # The one label no code can infer: did the proposed fix work?
        # Without it every precedent reads "outcome: not recorded".
        self.outcomes: dict[str, str] = {}
        self.registry = ProviderRegistry()
        # Provider polling is opt-in per source: pulling history costs money
        # and quota, so it never starts on its own.
        self.provider_source = ""
        self._provider_seen: set[str] = set()
        self._lock = threading.Lock()
        self._spec: FlowSpec | None = None
        self.code_analysis: dict | None = None
        self.code_analysis: dict | None = None
        self.code_analysis: dict | None = None
        self.code_analysis: dict | None = None
        self.code_analysis: dict | None = None
        self._stop = threading.Event()
        if log_path:
            self.attach(log_path, project)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        ticks = 0
        while not self._stop.is_set():
            try:
                with self._lock:
                    if self.pipeline is not None:
                        moved = self.pipeline.run_once()
                        # A quiet stream leaves its last event buffered (the
                        # next line may continue it), so log time stops
                        # advancing and a run that ended is never judged
                        # complete. When nothing new has arrived for a few
                        # ticks, release it: the stream has moved on.
                        if moved:
                            self._idle_ticks = 0
                        else:
                            self._idle_ticks = getattr(self, "_idle_ticks", 0) + 1
                            if self._idle_ticks == 3:
                                self.pipeline.drain()
            except Exception:
                pass
            ticks += 1
            # Providers are polled far more slowly than a local file: they
            # are metered, rate-limited, and their data is minutes old by
            # nature. Detection runs on local speed; connectors add context.
            if self.provider_source and ticks % 20 == 0:
                try:
                    self.pull_provider()
                except Exception:
                    pass
            self._stop.wait(POLL_S)

    # -- connectors ----------------------------------------------------------

    def providers(self) -> dict:
        return {"providers": self.registry.list(),
                "presets": preset_choices(),
                "streaming_from": self.provider_source}

    def add_provider(self, entry: dict) -> dict:
        return self.registry.add(entry)

    def remove_provider(self, name: str) -> dict:
        if self.provider_source == name:
            self.provider_source = ""
        return self.registry.remove(str(name))

    def check_provider(self, name: str) -> dict:
        return self.registry.check(str(name))

    def stream_provider(self, name: str, service: str = "") -> dict:
        """Attach the pipeline to a provider instead of a local file."""
        name = str(name).strip()
        if not name:
            self.provider_source = ""
            return {"ok": True, "streaming_from": ""}
        check = self.registry.check(name)
        if not check.get("ok"):
            return {"ok": False, "detail": check.get("detail", "provider unreachable")}
        project = f"{name}:{service}" if service else name
        with self._lock:
            if self.pipeline is not None:
                try:
                    self.pipeline.close()
                except Exception:
                    pass
            self.project = project
            self.log_path = f"provider://{project}"
            # A provider-backed pipeline reads no file; the collector is left
            # pointed at a path that does not exist, which polls to nothing.
            self.pipeline = Pipeline(project, AEGIS_HOME / "projects" / "_provider.none")
            self.pipeline.spec_provider = self._spec_for_conformance
            self.hypotheses = {}
            self._spec = None
            self._provider_seen = set()
        self.provider_source = name
        self.provider_service = service
        self.pull_provider()
        return {"ok": True, "streaming_from": name, "project": project}

    def pull_provider(self) -> dict:
        """One query; new records only. De-duplicated, because a provider
        returns the same window each time it is asked."""
        if not self.provider_source or self.pipeline is None:
            return {"ok": False, "detail": "no provider stream"}
        provider = self.registry.build(self.provider_source)
        if provider is None:
            return {"ok": False, "detail": "provider could not be built"}
        try:
            # The FIRST pull of a connector reaches back for history, so
            # baselines start from evidence rather than from whatever one
            # page the vendor happened to return; later polls only need the
            # new arrivals. A backend with no history simply returns less.
            first = not self._provider_seen
            records = provider.query_logs(LogFilter(
                service=getattr(self, "provider_service", ""),
                since_minutes=1440 if first else 30,
                limit=500 if first else 200))
        finally:
            close = getattr(getattr(provider, "client", None), "close", None)
            if close:
                close()
        fresh = 0
        with self._lock:
            for record in records:
                key = f"{record.collected_at}|{record.payload}"
                if key in self._provider_seen:
                    continue
                self._provider_seen.add(key)
                event = self.pipeline.normalizer.feed(record.payload)
                if event is not None:
                    self.pipeline._commit(event)
                    fresh += 1
            # The normalizer holds the last line pending, waiting to see
            # whether the next one continues it (a traceback, a folded dump).
            # A file keeps arriving so that resolves itself; a provider poll
            # ENDS, so without this the newest record of every poll sat
            # invisible until the following poll twenty ticks later - and a
            # single-record answer never appeared at all.
            final = self.pipeline.normalizer.flush()
            if final is not None:
                self.pipeline._commit(final)
                fresh += 1
            # The normalizer holds the last line pending, waiting to see
            # whether the next one continues it (a traceback, a folded dump).
            # A file keeps arriving so that resolves itself; a provider poll
            # ENDS, so without this the newest record of every poll sat
            # invisible until the following poll twenty ticks later - and a
            # single-record answer never appeared at all.
            final = self.pipeline.normalizer.flush()
            if final is not None:
                self.pipeline._commit(final)
                fresh += 1
            # The normalizer holds the last line pending, waiting to see
            # whether the next one continues it (a traceback, a folded dump).
            # A file keeps arriving so that resolves itself; a provider poll
            # ENDS, so without this the newest record of every poll sat
            # invisible until the following poll twenty ticks later - and a
            # single-record answer never appeared at all.
            final = self.pipeline.normalizer.flush()
            if final is not None:
                self.pipeline._commit(final)
                fresh += 1
            if len(self._provider_seen) > 5000:
                self._provider_seen = set(list(self._provider_seen)[-2500:])
        return {"ok": True, "new_records": fresh, "returned": len(records)}

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
            # The live path judges each finished run against the SAME spec
            # the Flow & spec page shows - saved, human-editable, and only
            # meaningful once purpose steps are marked.
            self.pipeline.spec_provider = self._spec_for_conformance
            self.hypotheses = {}
            self._spec = None
        self._remember_recent(str(path), name)
        return {"ok": True, "project": name, "path": str(path)}



    def wipe_project(self, name: str) -> dict:
        """Delete everything Aegis learned about one project.

        The isolation promise cuts both ways: data lives only under
        ~/.aegis/projects/<name>, so deleting a project must be one action
        there - never a scan of anything else. The path is resolved and
        checked to still be inside the projects directory, so a name like
        "../../Desktop" cannot escape it. The watched log file itself is the
        user's and is never touched.
        """
        clean = (name or "").strip()
        if not clean:
            return {"ok": False, "detail": "no project named"}
        projects_dir = (AEGIS_HOME / "projects").resolve()
        target = (projects_dir / clean).resolve()
        if target.parent != projects_dir or not target.is_dir():
            return {"ok": False, "detail": f"no such project: {clean}"}
        with self._lock:
            if self.pipeline is not None and self.project == clean:
                try:
                    self.pipeline.close()
                except Exception:
                    pass
                self.pipeline = None
                self.project = ""
                self.log_path = ""
                self.hypotheses = {}
                self.outcomes = {}
                self._spec = None
        import shutil
        try:
            shutil.rmtree(target)
        except OSError as exc:
            return {"ok": False, "detail": f"could not delete: {exc}"}
        # Drop it from the recent list too, or the picker offers a ghost.
        try:
            recent = json.loads(RECENT_PATH.read_text()) if RECENT_PATH.exists() else []
            RECENT_PATH.write_text(json.dumps(
                [r for r in recent if r.get("project") != clean], indent=1))
        except (OSError, ValueError):
            pass
        return {"ok": True, "deleted": clean}

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

    # Scanning every listening process for its log file costs ~50ms per
    # process, and state() calls sources() on every poll - which made the
    # dashboard's own refresh take nine seconds on a machine with 42 ports
    # open. Ports do not appear and vanish second to second, so a short
    # cache is honest here; the Refresh button bypasses it.
    _SOURCES_TTL_S = 20.0

    def sources(self, force: bool = False) -> dict:
        """What the picker offers: known projects, recent paths, and log
        files of services currently running on this machine (reusing v1's
        proven discovery - and never offering Aegis itself)."""
        cached = getattr(self, "_sources_cache", None)
        if cached is not None and not force:
            made_at, payload = cached
            if time.monotonic() - made_at < self._SOURCES_TTL_S:
                return payload
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
            listening = discover_listening_ports()
            # ONE lsof for every process, reused by the list and the ranking
            # below. Per-pid probes were 64 subprocesses and up to 44s; the
            # Sources page showed nothing for all of it.
            cwds_by_pid, logs_by_pid = probe_pids(
                [int(item.get("pid", 0) or 0) for item in listening])
            for item in listening:
                if not v1_sources.is_plausible_source(item):
                    continue
                log = logs_by_pid.get(int(item.get("pid", 0) or 0))
                if log:
                    discovered.append({"process": item.get("process", "?"),
                                       "port": item.get("port"), "path": log})
        except Exception:
            pass
        # Ranked suggestions: 34 flat ports is a wall, not a recommendation.
        suggestions: list[dict[str, Any]] = []
        watched_project = ""
        try:
            watched_pid = 0
            watched_cwd = ""
            dependency_ports = {row["port"] for row in self.dependency_report()
                                if row.get("port") and row.get("local")}
            for item in listening:
                pid = int(item.get("pid", 0) or 0)
                if self.log_path and logs_by_pid.get(pid) == self.log_path:
                    watched_pid = pid
                    watched_cwd = cwds_by_pid.get(pid) or cwd_for_pid(pid)
                    break
            # A connector has no working directory, so the project is whatever
            # the analyzed repo is called - the only thing that ties a streamed
            # source to a place on disk.
            if not watched_cwd:
                repo = self._analyzed_repo()
                if repo:
                    watched_project = os.path.basename(repo.rstrip("/"))
            if watched_cwd:
                for part in reversed(watched_cwd.split("/")):
                    if part and part not in ("services", "apps", "src", "packages"):
                        watched_project = part
                        break
            suggestions = suggest_ports(
                listening,
                dependency_ports=dependency_ports,
                watched_pid=watched_pid, watched_cwd=watched_cwd,
                watched_project_root=self._analyzed_repo() or "",
                watched_log=self.log_path or "",
                watched_logs={str(c.path) for c, _n in
                              (self.pipeline.secondaries if self.pipeline else [])},
                own_pids={os.getpid()},
                log_for_pid=lambda pid: logs_by_pid.get(int(pid or 0)),
                cwd_for_pid=lambda pid: cwds_by_pid.get(int(pid or 0))
                or cwd_for_pid(pid))
            # The project's NAME comes from the cluster the ranking built, not
            # from a guess at the watched directory: services/crawl guessed
            # "crawl" while every row said "paideia-platform-explore", so
            # nothing matched and the list hid the services just attached.
            for row in suggestions:
                if row.get("is_watched") and row.get("project"):
                    watched_project = row["project"]
                    break
        except Exception:
            suggestions = []

        payload = {
            "recent": recent, "projects": projects,
            "discovered": discovered[:8],
            # "Suggested" must mean RELATED, not merely readable. Splitting on
            # watchable alone put every unrelated app on the machine under a
            # heading that claims relevance. With nothing attached there is no
            # "that" to be related to, and the right answer is every project on
            # this machine - the cold start a user actually begins from.
            "watching_something": bool(watched_project),
            # What is being watched is always listed - a row cannot say
            # WATCHING from under a heading it was dropped from.
            "suggested": [
                r for r in suggestions
                if r.get("is_watched")
                or (r["watchable"] and r["score"] >= 60
                    and (not watched_project
                         or r["score"] >= 100
                         or r.get("project") == watched_project))][:8],
            "other_ports": [r for r in suggestions
                            if not (r["watchable"] and r["score"] >= 60)][:20],
        }
        self._sources_cache = (time.monotonic(), payload)
        return payload

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

    def _spec_for_conformance(self):
        """The spec the live path judges against - only once a human or the
        model has marked what the flow exists to do. Before that every run
        would be judged "unknown", which is noise, not an incident."""
        try:
            spec = self.spec()
        except Exception:
            return None
        if spec is None or not any(s.critical for s in spec.steps):
            return None
        return spec

    def mark_purpose(self) -> dict:
        """One governed model call; the result is saved human-editable."""
        spec = self.spec()
        if spec is None:
            return {"ok": False, "detail": "not enough traces to mine a spec yet"}
        # The brief knows which operations this project exists to run.
        # Presence cannot tell purpose from setup until dozens of runs have
        # accumulated; the brief answers it from the code today.
        summary = synthesize_critical(spec, self.router, self._project_brief())
        spec.save(self._spec_path().parent)
        return {"ok": True, "detail": summary}

    # -- views ---------------------------------------------------------------

    def state(self) -> dict:
        if self.pipeline is None:
            return {"attached": False, "sources": self.sources(),
                    "model": {"profile": active_profile(),
                              "route": ":".join(_route_for("explain_incident")),
                              "available": self.router.available(),
                              "calls_made": self.router.budget.calls_made,
                              "budget": self.router.budget.max_calls},
                    "updated_at": time.strftime("%H:%M:%S")}
        with self._lock:
            stats = self.pipeline.stats()
            # Let the clock run before reading: an incident that opened and
            # then saw no further signal was frozen mid-lifecycle forever.
            self.pipeline.incidents.settle(self.pipeline.detect.last_now)
            incidents = [i.to_dict() for i in self.pipeline.incidents.incidents]
            notes = [s.to_dict() for s in self.pipeline.incidents.notes[-15:]]
            traces = self.pipeline.trace_index.traces()
            templates = self.pipeline.store.templates(limit=10)
            patterns = self.pipeline.memory.patterns()[:5]

        spec = self.spec()
        verdicts = []
        if spec is not None:
            engine = ConformanceEngine(mode="shadow")
            judged = getattr(self.pipeline, "_judged", set())
            for trace_id, events in traces.items():
                # A short trace is only skipped while it may still be
                # running. Once the live path has judged it (it went quiet),
                # its verdict is shown however few events it has - a run
                # that died after two steps is the one worth seeing.
                if len(events) < 4 and trace_id not in judged:
                    continue
                report, _ = engine.check(trace_id, events, spec)
                # What the run actually did, and what the spec expected of
                # it. A verdict card that says "hollow" and stops leaves the
                # reader with the one question it raised: which run, and
                # where did it stop? Both are already computed here.
                present = {e.template_id for e in events if e.template_id}
                verdicts.append({
                    "trace_id": trace_id, "verdict": report.verdict,
                    "reason": report.reason, "duration_s": report.duration_s,
                    "opened_at": events[0].ts, "events": len(events),
                    "deviations": report.deviations,
                    "evidence": report.evidence,
                    "steps": [{
                        "ts": e.ts,
                        "text": e.text_redacted.splitlines()[0][:150],
                        "service": e.service,
                    } for e in events[:25]],
                    # The purpose steps, and whether this run reached them:
                    # "expected X, never got there" is the whole finding.
                    "expected": [{
                        "label": step.label[:70],
                        "ran": step.template_id in present,
                        "critical": bool(step.critical),
                    } for step in spec.steps if step.critical][:6],
                })
        verdicts.sort(key=lambda v: v["opened_at"])

        for incident in incidents:
            incident["hypothesis"] = self.hypotheses.get(incident["id"])
            incident["precedents"] = self.pipeline.memory.similar(incident, top=2)
            incident["outcome"] = self.outcomes.get(incident["id"], "")

        purpose_marked = bool(spec and any(s.critical for s in spec.steps))
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
            "model": {
                "profile": active_profile(),
                "route": ":".join(_route_for("explain_incident")),
                "available": self.router.available(),
                "calls_made": self.router.budget.calls_made,
                "budget": self.router.budget.max_calls,
            },
            "updated_at": time.strftime("%H:%M:%S"),
        }





    def _investigation_tools(self) -> dict:
        """What the investigator may look at. Each returns observed data or
        says plainly that there is none - never a guess."""
        pipeline = self.pipeline

        def search_logs(args: dict) -> str:
            """search_logs(text, limit) - lines containing this text."""
            needle = str(args.get("text", "")).lower()
            limit = min(int(args.get("limit", 15) or 15), 40)
            if not needle:
                return "give a 'text' to search for"
            hits = [e.text_redacted.splitlines()[0]
                    for e in pipeline.hot.recent(4000)
                    if needle in e.text_redacted.lower()]
            if not hits:
                return f"no line contains {needle!r}"
            return f"{len(hits)} line(s) match; showing {min(limit, len(hits))}:\n" + \
                "\n".join(hits[-limit:])

        def get_baseline(args: dict) -> str:
            """get_baseline(operation) - what is normal for an operation, measured."""
            wanted = str(args.get("operation", "")).lower()
            rows = pipeline.detect._latency.summary()
            matches = [b for b in rows
                       if wanted in f"{b['component']} {b['operation']}".lower()] or rows
            if not matches:
                return "no timings have been measured yet"
            return "\n".join(
                f"{b['component']} {b['operation']}: usually {b['median_ms']}ms "
                f"(p95 {b['p95_ms']}ms, {b['count']} samples, ready={b['ready']})"
                for b in matches[:8])

        def get_code_for(args: dict) -> str:
            """get_code_for(log_text) - the source line that writes a log line,
            its function, and whether its external calls are guarded."""
            code = self._load_code()
            if not code:
                return ("this project has not been analyzed - no code facts "
                        "available. Run Analyze to enable this tool.")
            from aegis.l4_understanding.flowspec import _overlap
            wanted = str(args.get("log_text", ""))
            best, score = None, 0.0
            for statement in code.get("log_statements", []):
                overlap = _overlap(wanted, statement.get("text", ""))
                if overlap > score:
                    best, score = statement, overlap
            if best is None or score < 0.5:
                return f"no logging call in the code matches {wanted[:60]!r}"
            function = best.get("function", "")
            out = [f"written at {best.get('file')}:{best.get('line')} in {function}()"]
            for call in code.get("external_calls", []):
                if call.get("function") == function:
                    out.append(
                        f"  {function}() calls {call.get('target')} - "
                        f"{'guarded by try/except' if call.get('guarded') else 'NOT guarded'}, "
                        f"{'has timeout' if call.get('has_timeout') else 'NO timeout'} "
                        f"({call.get('file')}:{call.get('line')})")
            for entry in code.get("entrypoints", []):
                if function in code.get("calls", {}).get(entry.get("function", ""), []):
                    out.append(f"  reachable from {entry.get('method','')} "
                               f"{entry.get('path','')}")
            return "\n".join(out)

        def get_dependencies(args: dict) -> str:
            """get_dependencies() - what this service talks to, and guard status."""
            rows = self.dependency_report()
            if not rows:
                return "no dependencies observed"
            return "\n".join(
                f"{r['host']}:{r.get('port') or ''} seen {r['evidence']}x"
                f"{' RUNNING HERE' if r.get('running_here') else ''}"
                f"{' UNGUARDED in code' if r.get('guarded_in_code') is False else ''}"
                for r in rows[:8])

        def get_run_verdicts(args: dict) -> str:
            """get_run_verdicts() - how recent runs turned out, and why."""
            spec = self.spec()
            if spec is None:
                return "no flow spec yet - runs cannot be judged"
            from aegis.l4_understanding.conformance import ConformanceEngine
            engine = ConformanceEngine(mode="shadow")
            out = []
            for trace_id, events in list(pipeline.trace_index.traces().items())[-8:]:
                if len(events) < 4:
                    continue
                report, _ = engine.check(trace_id, events, spec)
                out.append(f"{report.verdict} {trace_id[:12]} "
                           f"({report.duration_s}s): {report.reason[:80]}")
            return "\n".join(out) or "no completed runs to judge"

        def get_similar_past(args: dict) -> str:
            """get_similar_past(incident_id) - past incidents like this one."""
            incident = next((i.to_dict() for i in pipeline.incidents.incidents
                             if i.id == str(args.get("incident_id", ""))), None)
            if incident is None:
                return "unknown incident id"
            matches = pipeline.memory.similar(incident, top=3)
            if not matches:
                return "no similar past incident on record"
            return "\n".join(
                f"{m['id']} (similarity {m['similarity']}) cause then: "
                f"{m['cause'][:80]} - outcome: {m['outcome']}" for m in matches)

        return {"search_logs": search_logs, "get_baseline": get_baseline,
                "get_code_for": get_code_for, "get_dependencies": get_dependencies,
                "get_run_verdicts": get_run_verdicts,
                "get_similar_past": get_similar_past}

    def investigate(self, incident_id: str) -> dict:
        """Run the full investigation loop on one incident."""
        if self.pipeline is None:
            return {"ok": False, "detail": "no source attached"}
        incident = next((i.to_dict() for i in self.pipeline.incidents.incidents
                         if i.id == incident_id), None)
        if incident is None:
            return {"ok": False, "detail": f"no incident {incident_id}"}
        from aegis.l7_reasoning.governance import Budget
        from aegis.l7_reasoning.investigator import Investigation
        from aegis.l7_reasoning.router import ModelRouter
        # Its own budget: an investigation is several calls by design, and
        # sharing the shell's budget meant the live brief could exhaust it
        # before the user ever clicked Investigate.
        router = ModelRouter(budget=Budget(max_calls=12, min_interval_s=0.5),
                             audit=self.router.audit)
        result = Investigation(router, self._investigation_tools()).run(incident)
        if result is None:
            return {"ok": False,
                    "detail": "model unavailable, budget spent, or no usable reply"}
        self.hypotheses[incident_id] = result
        return {"ok": True, "hypothesis": result}


    def verify_fix(self, incident_id: str) -> dict:
        """Did the fix hold? Measured against the same operations' own
        history, not asked of anyone."""
        if self.pipeline is None:
            return {"ok": False, "detail": "no source attached"}
        from aegis.l6_correlation.verify import load, verify
        snapshot = load(self.pipeline.store.fix_snapshot(incident_id))
        if snapshot is None:
            return {"ok": False,
                    "detail": "nothing was measured when this was marked - "
                              "mark an incident as worked and the numbers at "
                              "that moment are kept for comparison"}
        with self._lock:
            operations = [b for b in self.pipeline.detect._latency.summary()
                          if b["ready"]]
        return verify(snapshot, operations)


    def record_outcome(self, incident_id: str, outcome: str,
                       note: str = "") -> dict:
        """Close the learning loop: did the proposed fix actually work?

        This is the one label no amount of code can infer - only the person
        who applied the fix knows. Without it every precedent in memory reads
        "outcome: not recorded" forever and similar() hands out unverified
        diagnoses with a precedent's authority. wrong_diagnosis is worth as
        much as worked: it stops the same bad hypothesis being repeated.
        """
        allowed = ("worked", "did_not_work", "wrong_diagnosis")
        if outcome not in allowed:
            return {"ok": False,
                    "detail": f"outcome must be one of {', '.join(allowed)}"}
        if self.pipeline is None:
            return {"ok": False, "detail": "no source attached"}
        with self._lock:
            incident = next((i.to_dict() for i in
                             self.pipeline.incidents.incidents
                             if i.id == incident_id), None)
            if incident is None:
                return {"ok": False, "detail": f"no incident {incident_id}"}
            # Archive first: outcomes live on archived rows, and the live
            # pipeline never archives on its own. remember() is idempotent
            # (INSERT OR REPLACE on id@opened_at), so clicking twice is safe.
            self.pipeline.memory.remember(incident,
                                          self.hypotheses.get(incident_id))
            self.pipeline.memory.record_outcome(incident_id, outcome, note)
        self.outcomes[incident_id] = outcome
        return {"ok": True, "incident": incident_id, "outcome": outcome}

    def explain(self, incident_id: str) -> dict:
        if self.pipeline is None:
            return {"ok": False, "detail": "no source attached"}
        incident = next((i.to_dict() for i in self.pipeline.incidents.incidents
                         if i.id == incident_id), None)
        if incident is None:
            return {"ok": False, "detail": f"no incident {incident_id}"}
        incident["project_brief"] = self._project_brief()
        precedents = self.pipeline.memory.similar(incident, top=3)
        hypothesis = self.explainer.explain(incident, precedents=precedents)
        if hypothesis is None:
            return {"ok": False,
                    "detail": "model unavailable, budget spent, or call failed"}
        self.hypotheses[incident_id] = hypothesis
        return {"ok": True, "hypothesis": hypothesis}





    def suppress(self, template_id: str, reason: str = "",
                 undo: bool = False) -> dict:
        if self.pipeline is None:
            return {"ok": False, "detail": "no source attached"}
        template_id = (template_id or "").strip()
        if not template_id:
            return {"ok": False, "detail": "missing template_id"}
        if undo:
            self.pipeline.unsuppress_template(template_id)
        else:
            self.pipeline.suppress_template(template_id, reason)
        return {"ok": True, "suppressed": not undo, "template_id": template_id}

    # -- project analysis / dependencies / simulation ------------------------




    def _project_flow(self, incidents: list | None = None,
                      recent: list | None = None) -> dict | None:
        """Nodes, edges and the evidence under each. None when nothing has
        been analyzed - a flow drawn from nothing would be a fiction."""
        code = self._load_code()
        if not code:
            return None
        from aegis.l4_understanding.projectflow import build_flow
        try:
            operations = [b for b in self.pipeline.detect._latency.summary()
                          if b["ready"]] if self.pipeline else []
        except Exception:
            operations = []
        try:
            dependencies = self.dependency_report()
        except Exception:
            dependencies = []
        return build_flow(code, operations, dependencies,
                          incidents or [], recent or [])


    def understand(self, force: bool = False) -> dict:
        """Generate or refresh the project brief - the one model-written
        document describing what this project IS, grounded in the analysis
        and the observed telemetry. Cached by input hash: it costs a call
        only when the code or the behaviour has actually changed."""
        if self.pipeline is None:
            return {"ok": False, "detail": "no source attached"}
        code = self._load_code()
        if not code:
            # Without a code analysis, build_fact_sheet has nothing to describe -
            # the brief that comes back says "there is nothing" for every section,
            # which reads exactly like a broken feature rather than a missing step.
            # A brief this ungrounded is worse than none, so refuse to spend the
            # model call and say what is actually needed instead.
            return {"ok": False,
                    "detail": "no code analysis yet - click Analyze on Flow & spec "
                              "first, then Generate brief"}
        from aegis.l4_understanding.comprehension import (
            ProjectComprehension, build_fact_sheet)
        with self._lock:
            operations = [b for b in
                          self.pipeline.detect._latency.summary() if b["ready"]]
            templates = self.pipeline.store.templates(limit=20)
        facts = build_fact_sheet(code, operations, templates)
        comprehension = ProjectComprehension(
            AEGIS_HOME / "projects" / self.project)
        return comprehension.generate(facts, self.router, force=force)

    def _project_brief(self) -> str:
        """The cached brief, for prompt context. Empty when none exists -
        every caller must work identically without it."""
        if not self.project:
            return ""
        from aegis.l4_understanding.comprehension import ProjectComprehension
        return ProjectComprehension(
            AEGIS_HOME / "projects" / self.project).brief()

    def build_code_graph(self) -> dict:
        """Run `codegraph init` in the analyzed repo - ONLY on explicit
        request from the UI.

        This is the one action in Aegis that writes inside a project
        directory, and it is not Aegis writing: it is the user asking the
        CodeGraph CLI to build its own index there. It is a button, never a
        side effect of attaching or analyzing, and the UI says plainly what
        it creates before it is pressed.
        """
        repo = self._analyzed_repo()
        if not repo:
            return {"ok": False, "detail": "analyze a repo first"}
        from aegis.l4_understanding.codegraph import cli_available, status
        if not cli_available():
            return {"ok": False,
                    "detail": "CodeGraph CLI not installed on this machine"}
        from aegis.l1_ingestion.providers import McpClient
        import subprocess
        binary = McpClient.resolve("codegraph")
        # The child needs the PATH we searched to FIND it - the CLI is an npm
        # shim that then looks for node beside itself, and without this it
        # dies with "env: node: No such file or directory". Exactly the bug
        # already fixed for uvx in McpClient; the fix has to travel with
        # every process we start, not just the ones that speak MCP.
        environment = {**os.environ}
        environment["PATH"] = os.pathsep.join(
            [environment.get("PATH", "")]
            + [d for d in McpClient._EXTRA_PATH if os.path.isdir(d)])
        try:
            done = subprocess.run([binary, "init"], cwd=repo, timeout=900,
                                  capture_output=True, text=True,
                                  env=environment)
        except (OSError, subprocess.SubprocessError) as exc:
            return {"ok": False, "detail": f"{exc.__class__.__name__}: {exc}"}
        if done.returncode != 0:
            return {"ok": False,
                    "detail": (done.stderr or done.stdout or "init failed")[-400:]}
        return {"ok": True, "repo": repo, "code_graph": status(repo)}

    def _code_graph_status(self) -> dict | None:
        """Graph state for the UI, or None when no repo has been analyzed."""
        repo = self._analyzed_repo()
        if not repo:
            return None
        from aegis.l4_understanding.codegraph import status as graph_status
        return graph_status(repo)

    def _analyzed_repo(self) -> str:
        """The repo path the last Analyze used, from the stored facts."""
        code = self._load_code() or {}
        return str(code.get("root") or "")





    # -- project analysis / dependencies / simulation ------------------------







    # -- project analysis / dependencies / simulation ------------------------







    # -- project analysis / dependencies / simulation ------------------------







    # -- project analysis / dependencies / simulation ------------------------

    def _code_path(self):
        return AEGIS_HOME / "projects" / self.project / "code.json"

    def analyze_project(self, repo_path: str) -> dict:
        """Read the repo (read-only) and persist what it is supposed to do."""
        if self.pipeline is None:
            return {"ok": False, "detail": "attach a source first"}
        repo = Path(str(repo_path or "")).expanduser()
        if not repo.is_dir():
            return {"ok": False, "detail": f"not a directory: {repo}"}
        try:
            analysis = analyze_repo(repo)
        except Exception as exc:
            return {"ok": False, "detail": f"{exc.__class__.__name__}: {exc}"}
        self.code_analysis = analysis.to_dict()
        try:
            self._code_path().parent.mkdir(parents=True, exist_ok=True)
            self._code_path().write_text(json.dumps(self.code_analysis, indent=1))
        except OSError:
            pass
        summary = {
            "ok": True, "repo": str(repo),
            "files": analysis.files_scanned,
            "entrypoints": len(analysis.entrypoints),
            "external_calls": len(analysis.external_calls),
            "log_statements": len(analysis.log_statements),
            "unguarded": sum(1 for c in analysis.external_calls
                             if not c.guarded or
                             (c.kind == "http" and not c.has_timeout)),
        }
        return summary

    def _load_code(self) -> dict | None:
        if self.code_analysis is None and self._code_path().exists():
            try:
                self.code_analysis = json.loads(self._code_path().read_text())
            except (OSError, ValueError):
                self.code_analysis = None
        return self.code_analysis

    def dependency_report(self) -> list[dict]:
        if self.pipeline is None:
            return []
        mapper = DependencyMap()
        for event in self.pipeline.hot.recent(3000):
            mapper.observe_log_line(event.text_redacted.splitlines()[0])
        code = self._load_code()
        if code:
            mapper.absorb_code_analysis(code)
        listening = []
        log_for_pid = None
        try:
            from app.core import sources as v1_sources
            from app.core.discovery import discover_listening_ports
            listening = discover_listening_ports()
            log_for_pid = v1_sources.best_log_file_for_pid
        except Exception:
            pass
        return mapper.report(listening, log_for_pid=log_for_pid)

    def combine(self, path: str, service: str) -> dict:
        """Watch a dependency's log together with the primary source."""
        if self.pipeline is None:
            return {"ok": False, "detail": "attach a source first"}
        service = (service or "").strip() or Path(str(path)).stem
        with self._lock:
            return self.pipeline.add_secondary(str(path), service)

    def simulate(self, target: str) -> dict:
        if self.pipeline is None:
            return {"ok": False, "detail": "attach a source first"}
        return simulate_failure(
            target,
            topology=self.pipeline.topology,
            spec=self.spec(),
            dependencies=self.dependency_report(),
            code=self._load_code(),
        )





    def remediate(self, incident_id: str, repo_path: str) -> dict:
        """UI entry to Phase 9: reproduce-first, draft-only, repo never
        written. Two governed model calls at most, from the shared budget."""
        if self.pipeline is None:
            return {"ok": False, "detail": "no source attached"}
        incident = next((i for i in self.pipeline.incidents.incidents
                         if i.id == incident_id), None)
        if incident is None:
            return {"ok": False, "detail": f"no incident {incident_id}"}
        repo = Path(str(repo_path or "")).expanduser()
        if not repo.is_dir():
            return {"ok": False, "detail": f"not a directory: {repo}"}
        try:
            from aegis.l8_action.remediate import RemediationAgent
            agent = RemediationAgent(self.router, repo, tier="T1",
                                     project=self.project)
            proposal = agent.propose(incident.to_dict(),
                                     self.hypotheses.get(incident_id))
        except Exception as exc:
            return {"ok": False, "detail": f"{exc.__class__.__name__}: {exc}"}
        return {
            "ok": proposal.status == "draft",
            "status": proposal.status,
            "detail": proposal.detail,
            "bundle": proposal.bundle_path,
            "patch": (proposal.patch or "")[:800],
            "warnings": proposal.warnings,
        }

    def apply_fix(self, incident_id: str, repo_path: str,
                  allow_dirty: bool = False) -> dict:
        """T2: write a proven patch to the working tree, revertibly.

        Reachable only from an explicit human click. The patch has already
        proven it fixes what it claimed (its reproducer failed before and
        passed after, in a sandbox); this step checks the thing that proof
        does NOT cover - whether everything else still works - and reverts
        itself if the project's own suite disagrees.
        """
        from aegis.l8_action.apply import PatchApplier
        from aegis.l8_action.gate import AutonomyGate

        permit = AutonomyGate(tier="T2").permits_apply()
        if not permit.allowed:
            return {"ok": False, "detail": permit.reason}

        bundle = self.proposal(incident_id)
        if not bundle.get("ok") or not bundle.get("patch"):
            return {"ok": False,
                    "detail": "no proposed patch for this incident - propose "
                              "a fix first"}
        repo = Path(str(repo_path or "")).expanduser()
        if not repo.is_dir():
            return {"ok": False, "detail": f"not a directory: {repo}"}

        result = PatchApplier(repo).apply(bundle["patch"],
                                          allow_dirty=bool(allow_dirty))
        payload = result.to_dict()
        payload["ok"] = result.applied
        if result.applied:
            # Applying is a claim that the fix works. Snapshot the same
            # operations the verify loop measures, so "did it hold?" has a
            # before to compare against - without asking the user to press
            # a second button to record what they just did.
            try:
                self.record_outcome(incident_id, "worked",
                                    "applied by Aegis (T2), "
                                    f"{result.suite or 'no suite'} checked")
                payload["outcome_recorded"] = True
            except Exception:
                payload["outcome_recorded"] = False
        return payload

    def revert_fix(self, incident_id: str, repo_path: str,
                   backup_path: str, files: list[str] | None = None) -> dict:
        """Undo an applied patch. The button that makes applying safe."""
        from aegis.l8_action.apply import PatchApplier
        repo = Path(str(repo_path or "")).expanduser()
        if not repo.is_dir():
            return {"ok": False, "detail": f"not a directory: {repo}"}
        result = PatchApplier(repo).revert(str(backup_path or ""),
                                           list(files or []))
        payload = result.to_dict()
        payload["ok"] = result.reverted
        if result.reverted:
            try:
                self.record_outcome(incident_id, "did_not_work",
                                    "applied then reverted by hand")
            except Exception:
                pass
        return payload



    def proposal(self, incident_id: str) -> dict:
        """Read back a proposal bundle so the UI can show all of it.

        The fix agent writes PROPOSAL.md and fix.patch under the project's
        own proposals/ directory; showing 800 characters of patch and a file
        path was asking the user to go find the rest in a hidden dot-folder.
        The id is one path segment, resolved and checked, so a crafted
        incident id cannot read outside the proposals directory.
        """
        clean = (incident_id or "").strip()
        if not self.project or not clean:
            return {"ok": False, "detail": "no project or incident"}
        proposals = (AEGIS_HOME / "projects" / self.project
                     / "proposals").resolve()
        directory = (proposals / clean).resolve()
        if directory.parent != proposals or not directory.is_dir():
            return {"ok": False, "detail": f"no proposal for {clean}"}
        def _read(name: str) -> str:
            try:
                return (directory / name).read_text()
            except OSError:
                return ""
        return {"ok": True, "incident": clean,
                "proposal": _read("PROPOSAL.md"),
                "patch": _read("fix.patch"),
                "path": str(directory)}

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
            elif self.path.startswith("/api/providers"):
                self._json(app.providers())
            elif self.path.startswith("/api/providers"):
                self._json(app.providers())
            elif self.path.startswith("/api/providers"):
                self._json(app.providers())
            elif self.path.startswith("/api/providers"):
                self._json(app.providers())
            elif self.path.startswith("/api/providers"):
                self._json(app.providers())
            elif self.path.startswith("/api/sources"):
                self._json(app.sources())
            elif self.path.startswith("/api/sources"):
                self._json(app.sources())
            elif self.path.startswith("/api/sources"):
                self._json(app.sources())
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
            if self.path.startswith("/api/explain/"):
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
