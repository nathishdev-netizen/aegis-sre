"""Cross-service dependencies - "you watch the gateway, but the gateway calls
the orchestrator, and the orchestrator is running right here. Watch both?"

The method is the field-established one for building a dependency map WITHOUT
distributed tracing: mine interaction evidence from what already exists -
URL/host:port literals in the logs, plus the external calls found by code
analysis - into a directed graph weighted by how often each target appears.
Uninstrumented dependencies (databases, third-party APIs) show up the same
way. Then each localhost dependency is matched against the ports actually
listening on this machine: those are the ones the user can combine into one
correlated watch.

Two field rules applied: the most-depended-upon node is flagged as a single
point of failure, and a dependency that is never guarded in code is flagged
before it fails rather than after.
"""

from __future__ import annotations

import re
from typing import Any

_URL = re.compile(r"https?://([\w.-]+)(?::(\d+))?")
_HOSTPORT = re.compile(r"\b(localhost|127\.0\.0\.1|0\.0\.0\.0)[:](\d{2,5})\b")
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0"}


def _targets_in(text: str) -> list[dict[str, Any]]:
    found = []
    for match in _URL.finditer(text or ""):
        host, port = match.group(1), match.group(2)
        found.append({"host": host, "port": int(port) if port else None,
                      "url": match.group(0),
                      "local": host in _LOCAL_HOSTS})
    for match in _HOSTPORT.finditer(text or ""):
        if not any(f["port"] == int(match.group(2)) for f in found):
            found.append({"host": match.group(1), "port": int(match.group(2)),
                          "url": match.group(0), "local": True})
    return found


_OWN_DECL = re.compile(
    r"\b(?:public[_-]?host|public[_-]?url|callback[_-]?url|external[_-]?url|"
    r"base[_-]?url|listening|bound to|serving)\W{0,4}(?:https?://)?([\w.-]+)", re.I)
# Documentation links inside warnings are advice, not dependencies.
_DOC_HOSTS = {"developer.mozilla.org", "docs.python.org", "github.com",
              "stackoverflow.com", "learn.microsoft.com"}


class DependencyMap:
    """Evidence-weighted map of what this service talks to."""

    def __init__(self, own_ports: set[int] | None = None) -> None:
        # The service's own address appearing in its own logs is not a
        # dependency - a public callback URL or bind banner would otherwise
        # make every service depend on itself.
        self.own_ports = own_ports or set()
        self.own_hosts: set[str] = set()
        self._targets: dict[str, dict[str, Any]] = {}

    def observe_log_line(self, text: str) -> None:
        for match in _OWN_DECL.finditer(text or ""):
            host = match.group(1).lower()
            if "." in host:
                self.own_hosts.add(host)
                # retroactively drop it if earlier lines already counted it
                for key in [k for k in self._targets
                            if self._targets[k]["host"].lower() == host]:
                    del self._targets[key]
        for target in _targets_in(text):
            if target["local"] and target["port"] in self.own_ports:
                continue
            if target["host"].lower() in self.own_hosts \
                    or target["host"].lower() in _DOC_HOSTS:
                continue
            key = f"{target['host']}:{target['port'] or 80}"
            entry = self._targets.setdefault(key, {
                "host": target["host"], "port": target["port"],
                "url": target["url"], "local": target["local"],
                "seen_in_logs": 0, "seen_in_code": 0,
                "guarded_in_code": None})
            entry["seen_in_logs"] += 1

    def absorb_code_analysis(self, analysis: dict[str, Any]) -> None:
        """Code-declared dependencies join the same map, with guard status -
        the piece logs can never tell you."""
        for dep in analysis.get("dependencies", []):
            # "config" entries are declared service addresses; they carry a
            # URL exactly like an http call does.
            if dep.get("kind") not in ("http", "config"):
                key = f"{dep['target']}:0"
                entry = self._targets.setdefault(key, {
                    "host": dep["target"], "port": None, "url": dep["target"],
                    "local": True, "seen_in_logs": 0, "seen_in_code": 0,
                    "guarded_in_code": None})
                entry["seen_in_code"] += dep.get("calls", 1)
                entry["guarded_in_code"] = dep.get("always_guarded")
                continue
            for target in _targets_in(dep.get("target", "")):
                if target["local"] and target["port"] in self.own_ports:
                    continue
                key = f"{target['host']}:{target['port'] or 80}"
                entry = self._targets.setdefault(key, {
                    "host": target["host"], "port": target["port"],
                    "url": target["url"], "local": target["local"],
                    "seen_in_logs": 0, "seen_in_code": 0,
                    "guarded_in_code": None})
                entry["seen_in_code"] += dep.get("calls", 1)
                entry["guarded_in_code"] = dep.get("always_guarded")

    def report(self, listening: list[dict[str, Any]] | None = None,
               log_for_pid=None) -> list[dict[str, Any]]:
        """The map, joined against what is actually running here."""
        by_port = {}
        for item in (listening or []):
            try:
                by_port[int(item.get("port", 0) or 0)] = item
            except (TypeError, ValueError):
                continue
        rows = []
        for entry in self._targets.values():
            row = dict(entry)
            row["evidence"] = row["seen_in_logs"] + row["seen_in_code"]
            row["running_here"] = False
            row["combine_path"] = ""
            if entry["local"] and entry["port"] in by_port:
                row["running_here"] = True
                row["process"] = by_port[entry["port"]].get("process", "?")
                if log_for_pid is not None:
                    try:
                        pid = int(by_port[entry["port"]].get("pid", 0) or 0)
                        row["combine_path"] = log_for_pid(pid) or ""
                    except Exception:
                        row["combine_path"] = ""
            rows.append(row)
        rows.sort(key=lambda r: -r["evidence"])
        # The field rule: name the single point of failure explicitly.
        if rows:
            rows[0]["single_point_of_failure"] = len(rows) > 1 and \
                rows[0]["evidence"] >= 2 * max((r["evidence"] for r in rows[1:]),
                                               default=1)
        return rows
