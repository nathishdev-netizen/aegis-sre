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
import re
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
    FlowMiner, FlowSpec, synthesize_critical, _seconds)
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


def _v1_runtime():
    """v1's RuntimeState - the thing that writes the Live Brief.

    Imported lazily: aegis must still start when app/ is absent or partly
    broken, and a module-level import would make that fatal.
    """
    from app.server import runtime
    return runtime


def _in_time_order(events: list) -> list:
    """A run's events oldest-first, whatever shape their timestamps are.

    Sorted on parsed seconds rather than the raw string, because a bare clock
    ("10:12:04") and an ISO instant ("2026-09-30T05:43:38Z") do not sort
    against each other as text. Events with no usable timestamp keep their
    arrival order, after the stamped ones, so nothing is silently dropped.
    """
    stamped, bare = [], []
    for index, event in enumerate(events):
        seconds = _seconds(event.ts) if getattr(event, "ts", "") else None
        (bare if seconds is None else stamped).append(
            (seconds, index, event))
    stamped.sort(key=lambda row: (row[0], row[1]))
    return [row[2] for row in stamped] + [row[2] for row in bare]


_TRACE_FILE = re.compile(r'File "([^"]+)", line \d+')


def _aegis_version() -> str:
    try:
        import aegis

        return aegis.__version__
    except Exception:
        return "unknown"


def _repo_root_from_evidence(evidence: list) -> str:
    """The project directory a traceback's own frames point at, if they agree.

    Frames name absolute paths. The deepest frame inside the user's code is
    the best anchor; its package root (the highest directory still holding an
    __init__.py, else its own directory) is what a repo path should be.
    Interpreter and site-packages frames are ignored - every traceback has
    them and they say nothing about the project.
    """
    from pathlib import Path as _Path

    candidates: list[_Path] = []
    for line in evidence:
        for text in str(line).splitlines():
            found = _TRACE_FILE.search(text)
            if not found:
                continue
            raw = found.group(1)
            if any(mark in raw for mark in (
                    "/site-packages/", "/dist-packages/", "/lib/python",
                    "/Cellar/", "/.venv/", "/venv/", "/node_modules/",
                    "/Frameworks/Python.framework/")):
                continue
            path = _Path(raw)
            if path.exists():
                candidates.append(path)
    if not candidates:
        return ""
    # Deepest frame last in a traceback; it is the one that actually raised.
    # Climb to the nearest directory that looks like a PROJECT - one holding
    # a pytest.ini, requirements.txt, pyproject.toml, setup.py or .git. That
    # is what a repo path means, and it is what the runner needs to find the
    # right interpreter and import root. Walking by __init__.py alone was
    # wrong both ways: a package dir without one stopped the climb at
    # "agents/", and a deeply nested package overshot.
    markers = ("pytest.ini", "requirements.txt", "pyproject.toml",
               "setup.py", "setup.cfg", ".git", "Pipfile")
    target = candidates[-1].parent
    probe = target
    for _ in range(6):
        if any((probe / marker).exists() for marker in markers):
            return str(probe)
        if probe.parent == probe:
            break
        probe = probe.parent
    return str(target)



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
        self.provider_recent: list[dict] = []
        self._provider_seen: set[str] = set()
        self._lock = threading.Lock()
        self._spec: FlowSpec | None = None
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
            # Every five minutes, re-check the fixes that were marked as
            # working. The outcome label was the one thing in this whole
            # pipeline that required a human to come back later and tell us -
            # and nobody ever does, so every precedent in memory read
            # "outcome: not recorded" forever and similar() handed out
            # unverified diagnoses with a precedent's authority.
            if ticks % 300 == 0:
                try:
                    self.watch_fixes()
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
        # Removing the connector currently being streamed has to detach too,
        # for the same reason stopping does: a cleared provider_source beside a
        # live pipeline and a "provider://" log_path is a half-state the page
        # cannot describe.
        if self.provider_source == name:
            self.stream_provider("")
        return self.registry.remove(str(name))

    def check_provider(self, name: str) -> dict:
        return self.registry.check(str(name))

    def provider_services(self, name: str) -> dict:
        """Which services this connector can see - the choice the user makes
        before streaming, so one backend's many systems do not arrive as one
        undifferentiated stream."""
        return self.registry.services(str(name))

    def stream_provider(self, name: str, service: str = "") -> dict:
        """Attach the pipeline to a provider instead of a local file."""
        name = str(name).strip()
        if not name:
            # Stopping must tear the source DOWN, not just stop polling it.
            # Clearing provider_source alone left log_path "provider://signoz"
            # and the pipeline alive - a state that is neither attached nor
            # detached, so the page reported "not reading anything" while the
            # funnel still showed 502 ingested events, and panels gated on the
            # source vanished.
            with self._lock:
                if self.pipeline is not None:
                    try:
                        self.pipeline.close()
                    except Exception:
                        pass
                self.pipeline = None
                self.log_path = ""
                self.project = ""
                self.provider_recent = []
                self._provider_seen = set()
            self.provider_source = ""
            self.provider_service = ""
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
            self.provider_recent = []
        self.provider_source = name
        self.provider_service = service
        self.pull_provider()
        return {"ok": True, "streaming_from": name, "project": project}

    def _pull_every_service(self, provider, window: int, budget: int) -> list:
        """One query PER SERVICE, so a quiet service is not drowned out.

        A single unfiltered query returns the newest N records, and the busy
        services fill them: measured on a live backend, one 200-record poll
        carried 7 of 12 services, and payment-ms (42 records in a WEEK) would
        never have appeared. "Everything" that silently means "the loudest
        seven" is worse than not offering it.

        Each service gets an equal share of the budget instead. That costs one
        query per service per poll, which is why connectors are polled every
        twenty ticks rather than every one.
        """
        listed = self.registry.services(self.provider_source)
        names = [s["name"] for s in (listed.get("services") or []) if s.get("name")]
        if not names:
            # Cannot enumerate - fall back to the old single query rather than
            # return nothing.
            return provider.query_logs(LogFilter(
                service="", since_minutes=window, limit=budget))
        # A floor of 20: an equal split across many services can round down to
        # a handful of records each, which is too few to see a run in.
        share = max(20, budget // max(1, len(names)))
        records = []
        for name in names:
            try:
                records.extend(provider.query_logs(LogFilter(
                    service=name, since_minutes=window, limit=share)))
            except Exception:
                continue  # one unreachable service must not stop the rest
        return records

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
            service = getattr(self, "provider_service", "")
            window = 1440 if first else 30
            budget = 500 if first else 200
            if service:
                records = provider.query_logs(LogFilter(
                    service=service, since_minutes=window, limit=budget))
            else:
                records = self._pull_every_service(provider, window, budget)
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
                # The newest arrivals, kept verbatim. A connector has no Live
                # Brief (that is written by v1's runtime, which never sees these
                # records), so without this the page could say a connector was
                # streaming but never show a single thing it had read.
                self.provider_recent.insert(0, {
                    "service": record.service, "env": record.host,
                    "at": (record.collected_at or "")[:19],
                    "text": record.payload[:200],
                })
                del self.provider_recent[25:]
                # Keep the record's OWN service: a connector holds many, and
                # without this every one of them became the project name.
                event = self.pipeline.normalizer.feed(
                    record.payload, record.service, record.collected_at)
                if event is not None:
                    self.pipeline._commit(event)
                    fresh += 1
                # The Live Brief is written by v1's runtime, which reads a FILE
                # and so never saw a connector's records: a streaming connector
                # showed incidents and verdicts while the brief stayed "No logs
                # yet" forever. Feed it the same line, so a connector gets the
                # same one-sentence account of what is happening that a watched
                # file gets. Best effort - v1 failing must not stop ingestion.
                try:
                    _v1_runtime().ingest_line(record.payload, source="provider")
                except Exception:
                    pass
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
        """Where this project's flow spec lives.

        Named "-flow", not "-call": "call" is a voice service's word for a run,
        and an order pipeline or a job queue has no calls in it. An existing
        "-call.json" is still read, so a spec written before this rename and
        hand-edited since is not silently ignored.
        """
        flows = AEGIS_HOME / "projects" / self.project / "flows"
        current = flows / f"{self.project}-flow.json"
        if not current.exists():
            legacy = flows / f"{self.project}-call.json"
            if legacy.exists():
                return legacy
        return current

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

    def _source_descriptor(self) -> dict:
        """What is actually being read, right now.

        Three kinds, and they must never be confused: a local log FILE, a local
        PORT whose log file was resolved for you, or a CONNECTOR streamed from a
        vendor. The UI was inferring this from the shape of log_path, which
        cannot tell a live connector from a leftover one - provider://X with
        provider_source empty means nothing is being read at all, and that is
        exactly the state that kept showing a stale "watching chatbot.log".
        """
        path = str(self.log_path or "")
        if path.startswith("provider://"):
            name = self.provider_source or ""
            if not name:
                return {"kind": "none", "label": "not reading anything",
                        "detail": "a connector was selected but is not streaming",
                        "healthy": False}
            service = getattr(self, "provider_service", "") or ""
            return {"kind": "connector", "name": name, "service": service,
                    # Name the SERVICE when one was chosen: "signoz" alone does
                    # not say which of its eight systems is being read.
                    "label": ("streaming " + service + " via " + name) if service
                             else ("streaming all of " + name),
                    "detail": service, "healthy": True}
        if path:
            return {"kind": "file", "name": path,
                    "label": "reading " + path.rsplit("/", 1)[-1],
                    "detail": path, "healthy": True}
        return {"kind": "none", "label": "not reading anything",
                "detail": "", "healthy": False}

    def state(self) -> dict:
        if self.pipeline is None:
            return {"attached": False, "sources": self.sources(),
                    "source": {"kind": "none", "label": "not reading anything",
                               "detail": "", "healthy": False},
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
            # The traceback already names the checkout that produced these
            # logs. Offering it means the repo field starts correct instead
            # of holding whatever path the browser remembered - a stale one
            # sent two whole fix attempts at a different checkout of the same
            # project, where the frames resolved nowhere and the patch landed
            # on the `except` that logged the error.
            for incident in incidents:
                root = _repo_root_from_evidence(incident.get("evidence") or [])
                if root:
                    incident["repo_hint"] = root
            notes = [s.to_dict() for s in self.pipeline.incidents.notes[-15:]]
            traces = self.pipeline.trace_index.traces()
            templates = self.pipeline.store.templates(limit=10)
            # Newest last, as a stream reads. Bounded: the page shows a window,
            # not the history.
            recent_events = list(self.pipeline.hot.recent(60))
            patterns = self.pipeline.memory.patterns()[:5]

        spec = self.spec()
        verdicts = []
        if spec is not None:
            engine = ConformanceEngine(mode="shadow")
            judged = getattr(self.pipeline, "_judged", set())
            for trace_id, events in traces.items():
                # Chronological order, once, here - every consumer below
                # assumes it. A connector returns rows newest-first, which
                # made "when did this run open" report its LAST event and
                # every duration come out negative (so: 0.0s). Events with no
                # timestamp keep their arrival order, after the stamped ones.
                events = _in_time_order(events)
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
                    # Which service this run happened in. A connector holds
                    # many, and when no purpose is marked every run carries
                    # the same verdict and the same reason - the service is
                    # then the only thing telling two cards apart.
                    "service": next((e.service for e in events if e.service), ""),
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
            # WHAT is being watched, said plainly rather than left for the UI to
            # infer from a "provider://" prefix. A stale log_path and an empty
            # streaming_from looked identical to a live connector, so the header
            # kept claiming a source that was no longer being read.
            "source": self._source_descriptor(),
            # The diagram of this project, from the analysis plus the measured
            # telemetry. _project_flow existed and was never called, so the UI
            # that renders it could only ever show "Run Analyze below and this
            # becomes a diagram" - including right after a successful analyze.
            "project_flow": self._project_flow(incidents, notes),
            # The cached project brief. It was read for prompt context but never
            # sent to the page, and renderUnderstanding reads "understanding" -
            # a field nothing ever set - so the panel said "not generated yet"
            # even immediately after generating one.
            "understanding": self._project_brief(),
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
            # The live stream, in the ONLY form that may leave this process:
            # text_redacted. The browser showing raw lines would make the page
            # the leak, which is why a test guards this key's presence.
            "events": [
                {"ts": e.ts, "service": e.service, "level": e.level,
                 "text": e.text_redacted.splitlines()[0][:400]
                 if e.text_redacted else ""}
                for e in recent_events
            ],
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

        tools = {"search_logs": search_logs, "get_baseline": get_baseline,
                 "get_dependencies": get_dependencies,
                 "get_run_verdicts": get_run_verdicts,
                 "get_similar_past": get_similar_past}
        # A tool that can only answer "not available" still costs one of six
        # investigation steps to find that out. On a real incident the model
        # spent steps 2 AND 5 on get_code_for, both returning "this project
        # has not been analyzed", then had to rule out its own hypothesis for
        # lack of the facts it had just failed twice to fetch. Offer it only
        # when there is code to read.
        if self._load_code():
            tools["get_code_for"] = get_code_for
        return tools

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
        from aegis.l6_correlation.verify import load, recurrence, verify

        snapshot = load(self._fix_snapshot_for(incident_id))
        if snapshot is None:
            return {"ok": False,
                    "detail": "nothing was measured when this was marked - "
                              "mark an incident as worked and the numbers at "
                              "that moment are kept for comparison"}
        with self._lock:
            operations = [b for b in self.pipeline.detect._latency.summary()
                          if b["ready"]]
            templates = snapshot.get("templates") or []
            now_counts = (self.pipeline.store.template_activity(templates)
                          if templates else {})
        result = verify(snapshot, operations)
        # Latency is the right measure for a slow-flow incident and the wrong
        # one for every other kind. Whether the problem itself came back is
        # the measure that generalises, so both are reported and the headline
        # is whichever one can actually speak.
        recur = recurrence(templates, snapshot.get("template_counts") or {},
                           now_counts)
        headline = recur["verdict"]
        if headline in ("unknown", "too-early"):
            speaking = [r for r in (result.get("operations") or [])
                        if r.get("verdict") in ("held", "worse")]
            if speaking:
                headline = speaking[0]["verdict"]
        # verify() reports ok=False when it has no TIMINGS to compare, which
        # for an error or novelty incident is the normal case rather than a
        # failure. The answer is ok as long as either measure spoke.
        return {**result,
                "ok": bool(result.get("ok")) or headline not in
                      ("unknown", "too-early"),
                "recurrence": recur,
                "headline": headline,
                "measured_at": snapshot.get("at", "")}

    def watch_fixes(self) -> dict:
        """Re-measure every fix that was marked as working.

        A fix whose problem came back is downgraded from "worked" to
        "did_not_work" with the measurement that says so, so memory stops
        offering that diagnosis as a precedent that succeeded. This is the
        self-correcting half: Aegis grading its own past work against what
        the logs did afterwards, rather than against what anyone believed at
        the time.

        Only ever downgrades. A verdict of "held" is not promoted to proof,
        because a problem that has not recurred YET is not the same as a
        problem that is fixed - and overstating that is the failure mode this
        whole project exists to avoid.
        """
        if self.pipeline is None:
            return {"ok": False, "detail": "no source attached"}
        changed = []
        for row in self.pipeline.store.archived_incidents():
            if (row.get("outcome") or "") != "worked":
                continue
            if not row.get("fix_snapshot"):
                continue
            incident_id = str(row.get("id", "")).split("@")[0]
            result = self.verify_fix(incident_id)
            if not result.get("ok"):
                continue
            if result.get("headline") != "recurred":
                continue
            detail = (result.get("recurrence") or {}).get("detail", "")
            self.pipeline.memory.record_outcome(
                str(row["id"]), "did_not_work",
                f"auto-verified: {detail}")
            self.outcomes[incident_id] = "did_not_work"
            changed.append({"incident": incident_id, "detail": detail})
        return {"ok": True, "downgraded": changed}

    def _fix_snapshot_for(self, incident_id: str) -> str:
        """The snapshot for this incident, by archive key or bare id.

        Archive rows are keyed "INC-1@<opened_at>" so a new day's INC-1 does
        not overwrite yesterday's, but callers hold the bare id.
        """
        raw = self.pipeline.store.fix_snapshot(incident_id)
        if raw:
            return raw
        for row in self.pipeline.store.archived_incidents():
            if str(row.get("id", "")).split("@")[0] == incident_id:
                if row.get("fix_snapshot"):
                    return str(row["fix_snapshot"])
        return ""


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
            # Keep the numbers as they stand RIGHT NOW, so "did it hold?" can
            # be measured later instead of asked. Nothing wrote this before,
            # which made verify_fix unreachable in every case: it always
            # answered "nothing was measured when this was marked".
            if outcome == "worked":
                self._capture_fix_snapshot(incident_id, incident)
        self.outcomes[incident_id] = outcome
        return {"ok": True, "incident": incident_id, "outcome": outcome}

    def _capture_fix_snapshot(self, incident_id: str, incident: dict) -> None:
        """Baselines and signature counts at the moment a fix was marked.

        Two measures, because one does not generalise. Latency answers a
        slow-flow incident; for a novelty, an error spike or a run of hollow
        checkouts the timing never moves and the question that still works is
        whether the incident's own templates fired again.
        """
        import json

        from aegis.l6_correlation.verify import snapshot_operations

        try:
            operations = [b for b in self.pipeline.detect._latency.summary()
                          if b.get("ready")]
            snapshot = snapshot_operations(operations)
            templates = sorted({s.get("template_id", "")
                                for s in (incident.get("signals") or [])}
                               - {""})
            snapshot["templates"] = templates
            snapshot["template_counts"] = (
                self.pipeline.store.template_activity(templates)
                if templates else {})
            key = f"{incident_id}@{incident.get('opened_at', '')}"
            self.pipeline.store.set_fix_snapshot(key, json.dumps(snapshot))
        except Exception:
            # A missing snapshot degrades verification to "not measured yet".
            # It must never fail the outcome the user just recorded.
            pass

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
            # The analysed code map, which the agent's own docstring says it
            # needs: "it turns 'grep for this log line' into 'this line is
            # written at file:line'". It was never being handed over, so
            # _locate_via_code always fell through to text search and a gap
            # report could not name a single line.
            agent = RemediationAgent(self.router, repo, tier="T1",
                                     project=self.project,
                                     code=self._load_code())
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
            # Present only when Aegis could not explain the incident: the
            # lines whose absence is why.
            "gaps": proposal.gaps,
        }

    def apply_fix(self, incident_id: str, repo_path: str,
                  allow_dirty: bool = False, run_tests: bool = True,
                  fast_only: bool = False) -> dict:
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

        # The reproducer travels with the patch so apply() can re-prove the
        # fix for free when the project's own suite is skipped. A suite that
        # makes live LLM or database calls is not something to spend on the
        # user's behalf without them choosing it.
        result = PatchApplier(repo).apply(
            bundle["patch"], allow_dirty=bool(allow_dirty),
            run_tests=bool(run_tests) or bool(fast_only),
            reproducer=bundle.get("reproducer") or "",
            fast_only=bool(fast_only))
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



    def retry_fix(self, incident_id: str) -> dict:
        """Clear the attempt counter so Propose fix can run again.

        The circuit breaker stops after two failed attempts and nothing could
        reset it - resume_auto_fix() existed but was reachable from no API or
        button, so the only way back was deleting the proposal directory by
        hand. Two attempts spent on a wrong repo path left an incident
        permanently un-retryable.
        """
        from aegis.l8_action.remediate import resume_auto_fix

        project = self.project or ""
        if not project or not incident_id:
            return {"ok": False, "detail": "no project or incident"}
        try:
            resume_auto_fix(project, incident_id)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "detail": f"{exc.__class__.__name__}: {exc}"}
        return {"ok": True, "detail": f"{incident_id} can be retried"}

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
                # Aegis's own test, so apply() can re-prove the fix without
                # running (and paying for) the project's whole suite.
                "reproducer": _read("aegis_reproducer.py"),
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
            elif self.path == "/healthz":
                # Anything running this as a service needs a liveness probe,
                # and the version it answers with is how a deploy confirms
                # WHICH build is up.
                self._json({"ok": True, "service": "aegis",
                            "version": _aegis_version()})
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
