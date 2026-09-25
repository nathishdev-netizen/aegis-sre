"""C12 Direction B - Aegis as an MCP server.

    python3 -m aegis.mcp_server <logfile> [project] [--allow-model]

Lets any MCP client - Claude Code, an IDE assistant, another agent - query
what Aegis knows: incidents with evidence, run verdicts, the flow spec,
precedent history. This is what makes Aegis part of a workflow rather than
another dashboard to remember to open.

Doc rules kept:
  read-heavy    every tool is a read. The one spender - explain_incident -
                exists only when the operator started the server with
                --allow-model, and stays governed by the budget either way.
  no stubs      get_topology and simulate_scenario are in the doc's list but
                need C5/C14, which do not exist yet. A tool that answers
                "not implemented" trains callers to distrust the server, so
                they are simply not offered.

Transport: newline-delimited JSON-RPC 2.0 over stdio, per the MCP spec.
Register with:  claude mcp add aegis -- python3 -m aegis.mcp_server <logfile>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import load_env  # noqa: E402
load_env()

from aegis.server import AegisApp  # noqa: E402

PROTOCOL_VERSION = "2024-11-05"


def _tool(name: str, description: str, properties: dict | None = None,
          required: list | None = None) -> dict:
    return {
        "name": name,
        "description": description,
        "inputSchema": {"type": "object",
                        "properties": properties or {},
                        "required": required or []},
    }


class AegisMcp:
    """The protocol core: one request dict in, one response dict out.

    Separated from the stdio loop so every behaviour is testable in-process.
    """

    def __init__(self, app: AegisApp, allow_model: bool = False) -> None:
        self.app = app
        self.allow_model = allow_model

    # -- JSON-RPC ------------------------------------------------------------

    def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        method = request.get("method", "")
        request_id = request.get("id")
        if request_id is None:
            return None  # notification: nothing to say back
        try:
            if method == "initialize":
                result = self._initialize()
            elif method == "tools/list":
                result = {"tools": self._tools()}
            elif method == "tools/call":
                result = self._call(request.get("params") or {})
            elif method == "ping":
                result = {}
            else:
                return {"jsonrpc": "2.0", "id": request_id,
                        "error": {"code": -32601,
                                  "message": f"method not found: {method}"}}
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except Exception as exc:  # a bad request must not kill the server
            return {"jsonrpc": "2.0", "id": request_id,
                    "error": {"code": -32603, "message": f"{exc.__class__.__name__}: {exc}"}}

    def _initialize(self) -> dict[str, Any]:
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "aegis", "version": "0.1.0"},
        }

    # -- tools ---------------------------------------------------------------

    def _tools(self) -> list[dict[str, Any]]:
        tools = [
            _tool("aegis_overview",
                  "The watched project's funnel (lines -> events -> templates -> "
                  "signals -> incidents), verdict counts, and model status."),
            _tool("list_incidents",
                  "Open and recent incidents: id, severity, status, opened_at, "
                  "ranked-cause line.",
                  {"status": {"type": "string",
                              "description": "filter: open|resolved|forming"}}),
            _tool("get_incident",
                  "One incident in full: members, ranked cause and why, "
                  "timeline, evidence, precedents from memory.",
                  {"incident_id": {"type": "string"}}, ["incident_id"]),
            _tool("get_run_verdicts",
                  "Every completed run judged against the flow spec: achieved / "
                  "failed / hollow / degraded / unknown, each with its reason. "
                  "Hollow = completed cleanly but its purpose never ran - the "
                  "failure no error-based tool can see."),
            _tool("get_flow_spec",
                  "What a run of this project is supposed to do: steps with "
                  "presence, required and CRITICAL marks, and who set them."),
            _tool("query_incident_history",
                  "Search past archived incidents by words from their cause "
                  "lines and diagnoses. A match is a precedent, not a "
                  "conclusion - systems change.",
                  {"query": {"type": "string"}}, ["query"]),
        ]
        if self.allow_model:
            tools.append(_tool(
                "explain_incident",
                "Spend ONE governed model call explaining an incident, with "
                "citations checked against its evidence.",
                {"incident_id": {"type": "string"}}, ["incident_id"]))
        return tools

    def _call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name", "")
        arguments = params.get("arguments") or {}
        handlers = {
            "aegis_overview": self._overview,
            "list_incidents": self._list_incidents,
            "get_incident": self._get_incident,
            "get_run_verdicts": self._verdicts,
            "get_flow_spec": self._flow_spec,
            "query_incident_history": self._history,
            "explain_incident": self._explain,
        }
        handler = handlers.get(name)
        if handler is None:
            return self._text({"error": f"no such tool: {name}"}, is_error=True)
        return handler(arguments)

    @staticmethod
    def _text(payload: Any, is_error: bool = False) -> dict[str, Any]:
        return {"content": [{"type": "text",
                             "text": json.dumps(payload, indent=1)}],
                "isError": is_error}

    def _overview(self, _: dict) -> dict[str, Any]:
        state = self.app.state()
        verdict_counts: dict[str, int] = {}
        for verdict in state["verdicts"]:
            verdict_counts[verdict["verdict"]] = verdict_counts.get(verdict["verdict"], 0) + 1
        return self._text({
            "project": state["project"],
            "funnel": {label: count for label, count in state["funnel"]},
            "verdicts": verdict_counts,
            "incidents": state["stats"].get("incidents", 0),
            "quiet_notes": state["stats"].get("notes", 0),
            "model": state["model"],
        })

    def _list_incidents(self, arguments: dict) -> dict[str, Any]:
        wanted = arguments.get("status", "")
        rows = []
        for incident in self.app.state()["incidents"]:
            if wanted and incident["status"] != wanted:
                continue
            cause = ""
            for signal in incident.get("signals", []):
                if signal.get("id") == incident.get("ranked_cause"):
                    cause = (signal.get("evidence") or [""])[0][:120]
            rows.append({"id": incident["id"], "severity": incident["severity"],
                         "status": incident["status"],
                         "opened_at": incident["opened_at"],
                         "signals": len(incident.get("signals", [])),
                         "ranked_cause": cause})
        return self._text(rows)

    def _get_incident(self, arguments: dict) -> dict[str, Any]:
        wanted = str(arguments.get("incident_id", ""))
        for incident in self.app.state()["incidents"]:
            if incident["id"] == wanted:
                return self._text(incident)
        return self._text({"error": f"no incident {wanted}"}, is_error=True)

    def _verdicts(self, _: dict) -> dict[str, Any]:
        return self._text(self.app.state()["verdicts"])

    def _flow_spec(self, _: dict) -> dict[str, Any]:
        return self._text(self.app.state()["spec"])

    def _history(self, arguments: dict) -> dict[str, Any]:
        words = {w.lower() for w in str(arguments.get("query", "")).split()
                 if len(w) > 2}
        if not words:
            return self._text({"error": "empty query"}, is_error=True)
        rows = []
        for row in self.app.pipeline.store.archived_incidents():
            haystack = (row.get("cause", "") + " " + row.get("hypothesis", "")).lower()
            hits = sum(1 for word in words if word in haystack)
            if hits:
                rows.append({"id": row["id"], "score": hits,
                             "opened_at": row["opened_at"],
                             "severity": row["severity"],
                             "cause": row["cause"][:120],
                             "outcome": row["outcome"] or "not recorded",
                             "note": "precedent, not conclusion - systems change"})
        rows.sort(key=lambda r: -r["score"])
        return self._text(rows[:5])

    def _explain(self, arguments: dict) -> dict[str, Any]:
        if not self.allow_model:
            return self._text(
                {"error": "model calls are not permitted on this server - "
                          "restart with --allow-model to enable"}, is_error=True)
        result = self.app.explain(str(arguments.get("incident_id", "")))
        return self._text(result, is_error=not result.get("ok"))


def serve(app: AegisApp, allow_model: bool) -> None:
    core = AegisMcp(app, allow_model=allow_model)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            continue
        response = core.handle(request)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("--")]
    allow_model = "--allow-model" in argv
    if not args:
        print(__doc__, file=sys.stderr)
        return 1
    log_path = Path(args[0]).expanduser()
    if not log_path.exists():
        print(f"No such log file: {log_path}", file=sys.stderr)
        return 1
    project = args[1] if len(args) > 1 else log_path.stem
    app = AegisApp(project, str(log_path))
    try:
        serve(app, allow_model)
    finally:
        app.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
