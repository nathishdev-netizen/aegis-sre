"""Run one Aegis per service, and put them behind one page.

    python3 -m aegis.supervisor [services.yml] [--port 3000]

An AegisApp watches exactly one project: its pipeline, its spec, its store
are all singular. Making it hold several would mean threading a project id
through 48 methods and every API route, and would put every service in one
process - where a single bad log file can take the rest down with it.

So this does the other thing. One child process per service, each the Aegis
that already works, each on its own port and its own SQLite file. The
supervisor's own job is small and boring:

  - start a child per service in the config
  - restart one that dies, with a backoff so a service that cannot start
    does not become a fork bomb
  - poll every child's /healthz and serve one page that shows all of them
  - pass signals through, so `systemctl stop` stops the children too

Config is a plain list, parsed without PyYAML because this package has no
dependencies and never will:

    flights:  /var/log/tt/flights.log
    hotels:   /var/log/tt/hotels.log
    booking:  /var/log/tt/booking.log

Nothing here reaches into a child. The supervisor only knows what /healthz
tells it, which is the same thing a load balancer would know.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DEFAULT_PORT = 3000
# Children are numbered from the supervisor's own port + 1, not from a fixed
# 3001: a box where 3000 was taken got --port 3010 for the supervisor while
# its children still tried 3001, which another service already held. The
# children follow wherever the supervisor was moved to.
CHILD_PORT_OFFSET = 1

HEALTH_EVERY_S = 5.0
HEALTH_TIMEOUT_S = 3.0

# A child that dies is restarted, but a child that dies instantly and
# repeatedly is misconfigured, and restarting it in a tight loop buries the
# real error under thousands of lines. Back off, and say so.
RESTART_BACKOFF_S = (2, 5, 15, 30, 60)
# Long enough that a child which ran fine for a while starts from a clean
# slate rather than inheriting a backoff from last week.
BACKOFF_RESET_S = 300.0


@dataclass
class Service:
    """One watched service and the child process reading it."""

    name: str
    log_path: str
    port: int
    process: subprocess.Popen | None = None
    restarts: int = 0
    started_at: float = 0.0
    last_health: dict = field(default_factory=dict)
    last_error: str = ""

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def status(self) -> str:
        """What to show for this service: its own health, or why there is none."""
        if not self.alive:
            return "stopped"
        return str(self.last_health.get("status") or "starting")


def parse_config(text: str) -> list[tuple[str, str]]:
    """`name: /path/to.log` per line. Comments and blanks ignored.

    Deliberately not YAML: the format is a list of pairs, and a dependency
    for that would be the first this package has ever taken.
    """
    found: list[tuple[str, str]] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        name, _, path = line.partition(":")
        name, path = name.strip(), path.strip()
        # A Windows-style path keeps its drive letter: split on the FIRST
        # colon only, then put back what the partition took if it looks
        # like one.
        if len(path) == 1 and path.isalpha():
            continue
        if name and path:
            found.append((name, os.path.expanduser(path)))
    return found


class Supervisor:
    def __init__(self, services: list[tuple[str, str]], port: int = DEFAULT_PORT,
                 python: str | None = None) -> None:
        self.port = port
        self.python = python or sys.executable
        self.services = [
            Service(name=name, log_path=path,
                    port=port + CHILD_PORT_OFFSET + n)
            for n, (name, path) in enumerate(services)
        ]
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.started_at = time.time()

    # -- children ------------------------------------------------------------

    def _spawn(self, service: Service) -> None:
        """Start one child. Its stdout goes to ours, tagged, so a single
        `journalctl -u aegis` shows every service's output in one place."""
        cmd = [self.python, "-m", "aegis.server", service.log_path,
               service.name, "--port", str(service.port)]
        try:
            service.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
                cwd=str(Path(__file__).resolve().parents[1]),
            )
            service.started_at = time.time()
            service.last_error = ""
            threading.Thread(target=self._pump, args=(service,), daemon=True).start()
            print(f"[supervisor] {service.name} -> {service.url} "
                  f"(pid {service.process.pid})", flush=True)
        except Exception as exc:
            service.last_error = f"{exc.__class__.__name__}: {exc}"
            print(f"[supervisor] {service.name} FAILED to start: "
                  f"{service.last_error}", flush=True)

    def _pump(self, service: Service) -> None:
        """Tag a child's output with its name and forward it."""
        handle = service.process.stdout if service.process else None
        if handle is None:
            return
        for line in handle:
            if self._stop.is_set():
                return
            print(f"[{service.name}] {line.rstrip()}", flush=True)

    def _backoff(self, service: Service) -> float:
        n = min(service.restarts, len(RESTART_BACKOFF_S) - 1)
        return RESTART_BACKOFF_S[n]

    def _watch(self) -> None:
        """Restart children that die, and poll the health of those that live."""
        while not self._stop.is_set():
            for service in self.services:
                if self._stop.is_set():
                    return
                if not service.alive:
                    ran_for = time.time() - service.started_at
                    if service.started_at and ran_for > BACKOFF_RESET_S:
                        service.restarts = 0        # it was healthy for a while
                    wait = self._backoff(service)
                    code = service.process.poll() if service.process else None
                    print(f"[supervisor] {service.name} exited "
                          f"(code {code}); restarting in {wait}s", flush=True)
                    if self._stop.wait(wait):
                        return
                    service.restarts += 1
                    self._spawn(service)
                else:
                    self._poll_health(service)
            self._stop.wait(HEALTH_EVERY_S)

    def _poll_health(self, service: Service) -> None:
        try:
            with urllib.request.urlopen(f"{service.url}/healthz",
                                        timeout=HEALTH_TIMEOUT_S) as response:
                service.last_health = json.load(response)
                service.last_error = ""
        except urllib.error.URLError as exc:
            # Normal for the first second or two after a spawn; only
            # interesting once it persists, which the page shows as
            # "starting" rather than an error.
            service.last_health = {}
            service.last_error = str(getattr(exc, "reason", exc))[:120]
        except Exception as exc:
            service.last_health = {}
            service.last_error = f"{exc.__class__.__name__}: {exc}"[:120]

    # -- reporting -----------------------------------------------------------

    def overview(self) -> dict:
        rows = []
        for service in self.services:
            health = service.last_health
            rows.append({
                "name": service.name,
                "log_path": service.log_path,
                "url": service.url,
                "port": service.port,
                "status": service.status(),
                "pid": service.process.pid if service.alive and service.process else None,
                "restarts": service.restarts,
                "uptime_s": round(time.time() - service.started_at, 1)
                            if service.started_at and service.alive else 0,
                "events": health.get("events"),
                "incidents": health.get("incidents"),
                "version": health.get("version"),
                "error": service.last_error or None,
            })
        bad = [r for r in rows if r["status"] not in ("ok", "starting")]
        return {
            "ok": not bad,
            "service": "aegis-supervisor",
            "uptime_s": round(time.time() - self.started_at, 1),
            "watching": len(rows),
            "unhealthy": len(bad),
            "services": rows,
        }

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        for service in self.services:
            self._spawn(service)
        threading.Thread(target=self._watch, daemon=True).start()

    def shutdown(self, *_: object) -> None:
        """Stop every child, then ourselves. systemd sends one signal to the
        group, but a child that ignores it would otherwise be orphaned."""
        if self._stop.is_set():
            return
        self._stop.set()
        print("[supervisor] stopping children", flush=True)
        for service in self.services:
            if service.alive and service.process:
                try:
                    service.process.terminate()
                except Exception:
                    pass
        deadline = time.time() + 10
        for service in self.services:
            if service.process:
                try:
                    service.process.wait(timeout=max(0.1, deadline - time.time()))
                except Exception:
                    try:
                        service.process.kill()
                    except Exception:
                        pass
        print("[supervisor] stopped", flush=True)


def make_handler(supervisor: Supervisor):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):            # quiet; children log their own
            return

        def _json(self, payload: dict, code: int = 200) -> None:
            body = json.dumps(payload, indent=1).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path in ("/healthz", "/api/overview"):
                data = supervisor.overview()
                # A probe on the supervisor should fail when a service it is
                # responsible for is down, not only when the supervisor
                # itself has crashed.
                self._json(data, 200 if data["ok"] else 503)
            elif self.path == "/" or self.path.startswith("/index"):
                page = _page(supervisor.overview()).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)
            else:
                self._json({"ok": False, "detail": "not found"}, 404)

    return Handler


def _page(data: dict) -> str:
    """One page listing every service. Deliberately plain: this is a
    directory, not a dashboard - the dashboards are one click away."""
    colour = {"ok": "#3fb950", "degraded": "#d29922",
              "down": "#f85149", "stopped": "#f85149", "starting": "#8b949e"}
    rows = []
    for s in data["services"]:
        dot = colour.get(s["status"], "#8b949e")
        detail = s["error"] or (
            f"{s['events']} events · {s['incidents']} incidents"
            if s["events"] is not None else "waiting for first read")
        restarts = (f" · restarted {s['restarts']}x" if s["restarts"] else "")
        rows.append(f"""
      <a class="row" href="{s['url']}">
        <span class="dot" style="background:{dot}"></span>
        <span class="name">{s['name']}</span>
        <span class="status">{s['status']}</span>
        <span class="detail">{detail}{restarts}</span>
        <span class="port">:{s['port']}</span>
      </a>""")
    return f"""<!doctype html><meta charset="utf-8">
<meta http-equiv="refresh" content="10">
<title>Aegis — {data['watching']} services</title>
<style>
  :root {{ color-scheme: dark }}
  body {{ margin:0; background:#0d1117; color:#e6edf3;
         font:15px/1.5 ui-sans-serif,system-ui,-apple-system,sans-serif }}
  .wrap {{ max-width:900px; margin:0 auto; padding:48px 20px }}
  h1 {{ font-size:22px; margin:0 0 4px; font-weight:600 }}
  .sub {{ color:#8b949e; font-size:13px; margin:0 0 28px }}
  .row {{ display:grid; grid-template-columns:14px 1fr auto 2fr auto;
          gap:14px; align-items:center; padding:14px 16px; text-decoration:none;
          color:inherit; border:1px solid #21262d; border-radius:10px;
          margin-bottom:8px; background:#0e1320 }}
  .row:hover {{ border-color:#2a7f63 }}
  .dot {{ width:10px; height:10px; border-radius:50% }}
  .name {{ font-weight:600 }}
  .status {{ font-size:12px; color:#8b949e; text-transform:uppercase;
             letter-spacing:.08em }}
  .detail, .port {{ font-size:13px; color:#8b949e;
                    font-family:ui-monospace,SFMono-Regular,Menlo,monospace }}
  @media (max-width:640px) {{
    .row {{ grid-template-columns:14px 1fr auto }}
    .detail, .port {{ display:none }}
  }}
</style>
<div class="wrap">
  <h1>Aegis</h1>
  <p class="sub">{data['watching']} service(s) ·
     {data['unhealthy']} needing attention · refreshes every 10s</p>
  {''.join(rows)}
</div>"""


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("--")]
    port = DEFAULT_PORT
    for n, a in enumerate(argv):
        if a == "--port" and n + 1 < len(argv):
            port = int(argv[n + 1])

    config = Path(args[0]) if args else Path("services.yml")
    if not config.is_file():
        print(f"No config at {config}.\n\n"
              "Write one line per service:\n\n"
              "  flights: /var/log/tt/flights.log\n"
              "  hotels:  /var/log/tt/hotels.log\n")
        return 1

    services = parse_config(config.read_text())
    if not services:
        print(f"{config} lists no services.")
        return 1

    supervisor = Supervisor(services, port=port)
    signal.signal(signal.SIGTERM, supervisor.shutdown)
    signal.signal(signal.SIGINT, supervisor.shutdown)
    supervisor.start()

    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(supervisor))
    print(f"[supervisor] {len(services)} service(s) -> http://0.0.0.0:{port}",
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        supervisor.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
