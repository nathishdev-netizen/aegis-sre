"""C2 - the provider connector layer: query external observability platforms
as if they were local, without their schemas leaking upward.

Principle P6 is the whole design: if anything above this layer contains the
word "SigNoz", the abstraction has failed. Every provider implements the same
narrow interface and returns the same canonical shapes; nothing above knows
which vendor answered.

Two transports, and they are different things:
  HTTP     SigNozProvider speaks SigNoz's own query API directly.
  MCP      McpProvider adapts ANY vendor that ships an MCP server (SigNoz,
           Opik, Grafana all do) - Direction A of C12: MCP as the transport
           UNDER this interface, never exposed to agents raw.

Honesty note, recorded where it belongs: the SigNoz payload/parse here is
built against the published v5 API shape, exercised only with fixture
responses - no live instance existed at build time. The adapter exists so
that when one does, corrections land in this one file and nowhere else.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from aegis.contracts.events import RawRecord


# Where configured providers live. One file for the install (providers are
# infrastructure, not per-project), never inside any project directory.


# Where configured providers live. One file for the install (providers are
# infrastructure, not per-project), never inside any project directory.


# Where configured providers live. One file for the install (providers are
# infrastructure, not per-project), never inside any project directory.


# Where configured providers live. One file for the install (providers are
# infrastructure, not per-project), never inside any project directory.


# Where configured providers live. One file for the install (providers are
# infrastructure, not per-project), never inside any project directory.
REGISTRY_PATH = Path(os.path.expanduser("~/.aegis/providers.json"))


@dataclass
class LogFilter:
    """The canonical query. Vendor DSLs are the adapters' problem."""
    service: str = ""
    since_minutes: int = 60
    limit: int = 200
    text: str = ""


class TelemetryProvider(Protocol):
    name: str

    def capabilities(self) -> set[str]: ...
    def query_logs(self, query: LogFilter) -> list[RawRecord]: ...


# -- QuotaGuard + cache: providers charge money and enforce quotas -----------

class QuotaGuard:
    def __init__(self, max_calls_per_minute: int = 30) -> None:
        self.max_calls = max_calls_per_minute
        self._window: list[float] = []

    def allow(self) -> bool:
        now = time.monotonic()
        self._window = [t for t in self._window if now - t < 60]
        if len(self._window) >= self.max_calls:
            return False
        self._window.append(now)
        return True


class QueryCache:
    def __init__(self, ttl_s: float = 30.0) -> None:
        self.ttl_s = ttl_s
        self._entries: dict[str, tuple[float, list[RawRecord]]] = {}

    def get(self, key: str) -> list[RawRecord] | None:
        entry = self._entries.get(key)
        if entry and time.monotonic() - entry[0] < self.ttl_s:
            return entry[1]
        return None

    def put(self, key: str, value: list[RawRecord]) -> None:
        self._entries[key] = (time.monotonic(), value)


# -- SigNoz over HTTP ---------------------------------------------------------

def _http_post(url: str, headers: dict[str, str], body: dict[str, Any],
               timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "User-Agent": "aegis-log-agent/0.1", **headers}, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


class SigNozProvider:
    """Logs via POST /api/v5/query_range. All SigNoz-isms live HERE."""

    name = "signoz"

    def __init__(self, base_url: str, api_key: str = "",
                 transport: Callable[..., dict[str, Any]] | None = None,
                 quota: QuotaGuard | None = None,
                 cache: QueryCache | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._transport = transport or _http_post
        self.quota = quota or QuotaGuard()
        self.cache = cache or QueryCache()
        self.last_error = ""

    def capabilities(self) -> set[str]:
        return {"logs"}

    def query_logs(self, query: LogFilter) -> list[RawRecord]:
        key = f"logs:{query.service}:{query.since_minutes}:{query.limit}:{query.text}"
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        if not self.quota.allow():
            return []  # degrade, never block - and never surprise-bill

        now_ms = int(time.time() * 1000)
        expressions = []
        if query.service:
            expressions.append(f"service.name = '{query.service}'")
        if query.text:
            expressions.append(f"body CONTAINS '{query.text}'")
        body = {
            "start": now_ms - query.since_minutes * 60_000,
            "end": now_ms,
            "requestType": "raw",
            "compositeQuery": {"queries": [{
                "type": "builder_query",
                "spec": {
                    "name": "A", "signal": "logs", "limit": query.limit,
                    "filter": {"expression": " AND ".join(expressions)},
                    "order": [{"key": {"name": "timestamp"}, "direction": "desc"}],
                },
            }]},
        }
        headers = {"SIGNOZ-API-KEY": self.api_key} if self.api_key else {}
        try:
            response = self._transport(f"{self.base_url}/api/v5/query_range",
                                       headers, body, 30.0)
            self.last_error = ""
        except (urllib.error.URLError, OSError, ValueError) as exc:
            # A dead provider must not stall detection - but it must not be
            # able to look healthy either. The reason is recorded so check()
            # can report the truth instead of "0 records".
            self.last_error = f"{exc.__class__.__name__}: {exc}"
            return []

        records = self._normalize(response, query)
        self.cache.put(key, records)
        return records

    def _normalize(self, response: dict[str, Any],
                   query: LogFilter) -> list[RawRecord]:
        """Vendor JSON -> canonical RawRecord. The boundary P6 talks about."""
        records: list[RawRecord] = []
        results = (((response.get("data") or {}).get("data") or {})
                   .get("results") or (response.get("data") or {}).get("results") or [])
        for result in results:
            for row in result.get("rows") or []:
                data = row.get("data") or {}
                text = str(data.get("body") or data.get("log") or "")
                if not text:
                    continue
                # Same rule as the MCP path: a row's other columns - status
                # code, duration, http route, span kind - are the numbers
                # detection works on, and dropping them left every SigNoz
                # record as bare prose.
                from aegis.l1_ingestion.tabular import flatten_fields
                extras = [pair for pair in flatten_fields(
                    {k: v for k, v in data.items()
                     if k not in ("body", "log")})
                    if pair.split("=", 1)[0] not in text]
                if extras:
                    text = text + " " + " ".join(extras)
                records.append(RawRecord(
                    source_id=f"signoz:{query.service or 'all'}",
                    payload=text,
                    service=str(data.get("service.name") or query.service),
                    collected_at=str(row.get("timestamp") or ""),
                ))
        return records


# -- Any vendor's MCP server as a transport ----------------------------------

class McpClient:
    """A minimal MCP client: spawn the server command, speak newline-delimited
    JSON-RPC over its stdio. The same wire our own server (Phase 11) talks -
    which is also how this client gets tested against something real."""

    # Where package managers put the CLIs an MCP server is launched with.
    # A GUI- or launcher-started Python inherits a minimal PATH that has none
    # of these, so "uvx" resolves on a developer's terminal and fails inside
    # the running app with a bare FileNotFoundError - the tool is installed,
    # just invisible.
    # The spec revision we implement; servers negotiate down if they must.

    # The spec revision we implement; servers negotiate down if they must.

    # The spec revision we implement; servers negotiate down if they must.
    PROTOCOL_VERSION = "2024-11-05"

    _EXTRA_PATH = ("/opt/homebrew/bin", "/usr/local/bin",
                   os.path.expanduser("~/.local/bin"),
                   os.path.expanduser("~/.cargo/bin"))

    @classmethod
    def resolve(cls, program: str) -> str | None:
        """The absolute path to an MCP server command, or None."""
        import shutil
        search = os.pathsep.join(
            [os.environ.get("PATH", "")] + list(cls._EXTRA_PATH))
        return shutil.which(program, path=search)

    def __init__(self, command: list[str], timeout_s: float = 60.0,
                 env: dict[str, str] | None = None,
                 cwd: str | None = None) -> None:
        import subprocess
        # Vendor MCP servers authenticate through the environment
        # (SIGNOZ_API_KEY, OPIK_API_KEY). The parent environment is inherited
        # so a key already exported keeps working, with explicit values
        # layered on top.
        # The child needs the same PATH we searched to FIND it: npx resolves
        # here and then fails with "env: node: No such file or directory"
        # because node lives in the same directory the parent could not see.
        environment = {**os.environ, **{k: str(v) for k, v in (env or {}).items()}}
        environment["PATH"] = os.pathsep.join(
            [environment.get("PATH", "")] + [d for d in self._EXTRA_PATH
                                             if os.path.isdir(d)])
        command = list(command)
        if command:
            found = self.resolve(command[0])
            if found is None:
                raise FileNotFoundError(
                    f"{command[0]} is not installed, or not on this process's"
                    f" PATH. Looked in: {os.environ.get('PATH', '')}"
                    f"{os.pathsep}{os.pathsep.join(self._EXTRA_PATH)}")
            command[0] = found
        # stderr is KEPT, not discarded: an MCP server that dies on startup
        # (a missing package, a bad credential, a runner still downloading)
        # explains itself there, and throwing that away left the user with
        # "mcp server closed the connection" and nothing to act on.
        # cwd matters for servers that serve "the project here" - a code
        # graph server started outside its repo would answer for the wrong
        # codebase, or none.
        self._proc = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1, env=environment,
            cwd=cwd)
        self.timeout_s = timeout_s
        self._next_id = 0
        # initialize REQUIRES params under the MCP spec: protocolVersion,
        # capabilities and clientInfo. Sending the bare method worked against
        # our own lenient server (Phase 11) and was rejected by the first real
        # vendor one with "Invalid request parameters" - a handshake bug that
        # only a third-party server could reveal.
        self._request("initialize", {
            "protocolVersion": self.PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "aegis", "version": "1.0.0"},
        })
        self._notify("notifications/initialized")

    def _notify(self, method: str) -> None:
        self._proc.stdin.write(json.dumps(
            {"jsonrpc": "2.0", "method": method}) + "\n")
        self._proc.stdin.flush()

    def _request(self, method: str, params: dict | None = None) -> dict[str, Any]:
        self._next_id += 1
        message = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            message["params"] = params
        self._proc.stdin.write(json.dumps(message) + "\n")
        self._proc.stdin.flush()
        line = self._proc.stdout.readline()
        response = json.loads(line)
        if "error" in response:
            raise RuntimeError(response["error"].get("message", "mcp error"))
        return response.get("result") or {}

    def list_tools(self) -> list[dict[str, Any]]:
        return self._request("tools/list").get("tools", [])

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        result = self._request("tools/call",
                               {"name": name, "arguments": arguments})
        texts = [c.get("text", "") for c in result.get("content", [])
                 if c.get("type") == "text"]
        joined = "\n".join(texts)
        try:
            return json.loads(joined)
        except ValueError:
            return joined



    def _why_it_died(self) -> str:
        """Whatever the server said on its way out, for the error message."""
        note = ""
        if self._proc.stderr is not None:
            try:
                note = (self._proc.stderr.read() or "").strip()
            except Exception:
                note = ""
        if not note:
            code = self._proc.poll()
            note = (f"it exited with status {code}" if code is not None
                    else "it stopped responding and said nothing")
        return note[-400:]

    def close(self) -> None:
        try:
            self._proc.stdin.close()
            self._proc.terminate()
        except OSError:
            pass


@dataclass
class McpToolMap:
    """How one vendor's MCP tools map onto the canonical interface. Explicit
    configuration, never guessed: inventing another vendor's tool names would
    be fabrication wearing an adapter's name."""
    logs_tool: str
    arguments: dict[str, Any] = field(default_factory=dict)
    # Dotted path to the list of log lines inside the tool's reply.
    lines_path: str = ""
    line_key: str = ""     # key holding the text when items are objects
    # Some vendors keep the numbers that matter OFF their list view: Opik's
    # trace table has duration and cost but not tokens, which live on the
    # individual trace. Naming a detail tool here lets a bounded number of
    # rows per poll be enriched with them.
    detail_tool: str = ""
    detail_arguments: dict[str, Any] = field(default_factory=dict)
    detail_fields: tuple[str, ...] = ()
    max_detail_calls: int = 8
    # How THIS vendor spells paging. Named here so the pull logic stays
    # vendor-neutral: a backend that calls them offset/count, or does not
    # page at all, is configuration rather than a code change.
    page_argument: str = ""
    size_argument: str = ""
    max_pages: int = 1
    # How THIS vendor spells paging. Named here so the pull logic stays
    # vendor-neutral: a backend that calls them offset/count, or does not
    # page at all, is configuration rather than a code change.
    page_argument: str = ""
    size_argument: str = ""
    max_pages: int = 1
    # Some vendors keep the numbers that matter OFF their list view: Opik's
    # trace table has duration and cost but not tokens, which live on the
    # individual trace. Naming a detail tool here lets a bounded number of
    # rows per poll be enriched with them.
    detail_tool: str = ""
    detail_arguments: dict[str, Any] = field(default_factory=dict)
    detail_fields: tuple[str, ...] = ()
    max_detail_calls: int = 8


class McpProvider:
    name = "mcp"

    def __init__(self, client: McpClient, tool_map: McpToolMap,
                 provider_name: str = "mcp") -> None:
        self.client = client
        self.tool_map = tool_map
        self.name = provider_name
        self.last_error = ""
        # A detail read costs thousands of tokens of reply, so each id is
        # fetched at most once for the life of the connector.
        self._detail_cache: dict[str, str] = {}

    def capabilities(self) -> set[str]:
        return {"logs"}

    def query_logs(self, query: LogFilter) -> list[RawRecord]:
        """Ask the vendor for records, following pages while they last.

        A single page is whatever the backend felt like returning - 25 rows
        for one vendor, 100 for another - which made a baseline's depth an
        accident of the vendor's default rather than a choice. Paging is
        driven by the tool map, so nothing here knows which backend it is.
        """
        pages = max(1, self.tool_map.max_pages)
        if not self.tool_map.page_argument:
            pages = 1
        collected: list[RawRecord] = []
        for page in range(1, pages + 1):
            arguments = dict(self.tool_map.arguments)
            if query.service:
                arguments.setdefault("service", query.service)
            if self.tool_map.page_argument:
                arguments[self.tool_map.page_argument] = page
            if self.tool_map.size_argument:
                arguments.setdefault(self.tool_map.size_argument, 100)
            batch = self._one_page(arguments, query)
            if batch is None:
                break                      # the error is on last_error
            collected.extend(batch)
            if not batch or len(collected) >= query.limit:
                break                      # short page means no more rows
        return collected[: query.limit]

    def _one_page(self, arguments: dict[str, Any],
                  query: LogFilter) -> list[RawRecord] | None:
        try:
            reply = self.client.call_tool(self.tool_map.logs_tool, arguments)
            self.last_error = ""
        except (RuntimeError, OSError, ValueError) as exc:
            self.last_error = f"{exc.__class__.__name__}: {exc}"
            return None
        node: Any = reply
        for part in filter(None, self.tool_map.lines_path.split(".")):
            if not isinstance(node, dict):
                return []
            node = node.get(part)
        # An MCP tool answers with whatever text its vendor chose. Opik
        # returns a human-readable table ("id | name | duration_ms | ..."),
        # not a JSON list - and requiring a list here silently produced zero
        # records from 23 real traces while still reporting ok. Records are
        # lines of text either way; a string reply is split into them.
        if isinstance(node, str):
            # A vendor table becomes real log lines - timestamp, name,
            # duration, error, cost - so every detector, baseline and
            # incident downstream works on a connector exactly as it does
            # on a local file. Text that is not a table is kept as-is
            # rather than dropped.
            from aegis.l1_ingestion.tabular import to_log_lines, looks_tabular
            if looks_tabular(node):
                # A table with a header and no rows is an EMPTY page, not
                # prose. Falling back to raw lines here ingested the header
                # as data and made a spent page look full - so paging never
                # stopped, and a connector's last page became an event.
                node = to_log_lines(node)
            else:
                node = [line for line in node.splitlines() if line.strip()]
        if not isinstance(node, list):
            return []
        records = []
        for item in node[: query.limit]:
            if isinstance(item, str):
                text = item
            elif isinstance(item, dict) and self.tool_map.line_key:
                text = str(item.get(self.tool_map.line_key, ""))
            else:
                continue
            if text:
                text = self._with_detail(text)
                records.append(RawRecord(source_id=f"{self.name}:mcp",
                                         payload=text, service=query.service))
        return records

    _TRACE_ID = re.compile(r"trace_id=(\S+)")

    def _with_detail(self, line: str) -> str:
        """Append fields the list view omitted, for the newest rows only.

        Opik's trace table carries duration and cost but not tokens, so
        "how many tokens does this consume?" was unanswerable from data the
        platform had already collected. Reading a whole trace costs several
        thousand tokens of reply, so this is bounded per poll and cached per
        id: the newest rows get enriched, older ones keep what the table
        gave. A row whose detail cannot be read is returned unchanged rather
        than dropped.
        """
        if not self.tool_map.detail_tool or not self.tool_map.detail_fields:
            return line
        found = self._TRACE_ID.search(line)
        if not found:
            return line
        identifier = found.group(1)
        if identifier in self._detail_cache:
            return line + self._detail_cache[identifier]
        if len(self._detail_cache) >= self.tool_map.max_detail_calls:
            return line
        try:
            raw = self.client.call_tool(self.tool_map.detail_tool, {
                **self.tool_map.detail_arguments, "id": identifier})
        except (RuntimeError, OSError, ValueError):
            return line
        # call_tool returns a dict when the reply parsed as JSON, and str()
        # on a dict yields Python repr with SINGLE quotes - which the field
        # regex below never matches. Re-serialise so both shapes are read
        # the same way; this worked live only because Opik prefixes its
        # reply with a text banner that keeps it a string.
        reply = raw if isinstance(raw, str) else json.dumps(raw)
        # Keep EVERYTHING the vendor's record holds, not a list of field
        # names we thought to ask for. A backend knows things we have not
        # anticipated, and each hardcoded name is a future question that
        # cannot be answered from data already fetched.
        extra = ""
        body = reply[reply.find("{"):] if "{" in reply else ""
        record: dict[str, Any] = {}
        if body:
            try:
                record = json.loads(body[:body.rfind("}") + 1])
            except ValueError:
                record = {}
        # The payload may be wrapped ({"trace": {...}, "spans": [...]}).
        # Facts that live only on a child span - which MODEL answered, and
        # which provider - belong on the parent's line too, or "which model
        # is being used?" is unanswerable from a trace-level record.
        children = record.get("spans") if isinstance(
            record.get("spans"), list) else []
        if isinstance(record.get("trace"), dict):
            record = record["trace"]
        elif len(record) == 1:
            only = next(iter(record.values()))
            if isinstance(only, dict):
                record = only
        for child in children:
            if isinstance(child, dict) and child.get("model"):
                record = {**record, "model": child["model"]}
                if child.get("provider"):
                    record["provider"] = child["provider"]
                break
        if record:
            from aegis.l1_ingestion.tabular import flatten_fields
            known = set(re.findall(r"(\w+)=", line))
            # The row already states these; a second copy under the vendor's
            # own name is noise in a line a model will read.
            known |= {"name", "duration", "total_estimated_cost"}
            # The row already states these; a second copy under the vendor's
            # own name is noise in a line a model will read.
            known |= {"name", "duration", "total_estimated_cost"}
            pairs = [p for p in flatten_fields(record)
                     if p.split("=", 1)[0] not in known]
            extra = (" " + " ".join(pairs)) if pairs else ""
        elif self.tool_map.detail_fields:
            # Not JSON: fall back to pulling the named numbers out of prose.
            for field_name in self.tool_map.detail_fields:
                match = re.search(
                    r'"' + re.escape(field_name) + r'"\\s*:\\s*([0-9.]+)', reply)
                if match:
                    extra += f" {field_name}={match.group(1)}"
        self._detail_cache[identifier] = extra
        return line + extra


# Known vendor MCP servers, with their REAL tool names - verified against the
# vendors' published docs (SigNoz MCP server, comet-ml/opik-mcp), not guessed.
# A preset means the user picks "SigNoz (MCP)" and types a URL; they never have
# to know that the tool is called fetch_traces_or_logs. Unknown vendors can
# still be added by naming the tool explicitly.
MCP_PRESETS: dict[str, dict[str, Any]] = {
    "signoz-mcp": {
        "label": "SigNoz (MCP server)",
        # github.com/SigNoz/signoz-mcp-server and signoz.io/docs/ai/signoz-mcp-server.
        # Verified against a live v0.117 instance and the official docs, Sept
        # 2026 - every field below was previously wrong: it is a Go binary, not
        # a `uvx` python package; the logs tool is signoz_search_logs, not
        # fetch_traces_or_logs; it reads SIGNOZ_URL, not SIGNOZ_HOST.
        "command": ["signoz-mcp-server"],  # the downloaded Go binary, on PATH
        "logs_tool": "signoz_search_logs",
        "arguments": {},
        "probe_tool": "signoz_search_logs",
        "env_hint": "SIGNOZ_URL / SIGNOZ_API_KEY (same key as the HTTP API)",
    },
    "opik-mcp": {
        "label": "Opik / Comet (MCP server)",
        # https://github.com/comet-ml/opik-mcp - python package, run via uvx
        "command": ["uvx", "opik-mcp"],
        "logs_tool": "get_trace_by_id",
        "arguments": {},
        "probe_tool": "list_projects",
        "env_hint": "OPIK_API_KEY / OPIK_WORKSPACE",
    },
}


def preset_choices() -> list[dict[str, str]]:
    return [{"id": key, "label": spec["label"], "env_hint": spec["env_hint"]}
            for key, spec in MCP_PRESETS.items()]


class ProviderRegistry:
    """Configured providers and how to build them.

    Credentials live in this file with 0600 permissions - never in a project
    directory, never in the repo, and never sent anywhere except the provider
    they belong to. A provider that fails to build is reported as broken
    rather than silently missing: an observability tool that quietly stops
    querying a backend is worse than one that says it cannot.
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else REGISTRY_PATH
        self._live: dict[str, Any] = {}

    # -- persistence ---------------------------------------------------------

    def _load(self) -> list[dict[str, Any]]:
        try:
            if self.path.exists():
                data = json.loads(self.path.read_text())
                return data if isinstance(data, list) else []
        except (OSError, ValueError):
            pass
        return []

    def _save(self, entries: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(entries, indent=1))
        try:
            os.chmod(self.path, 0o600)   # it holds API keys
        except OSError:
            pass

    # -- api -----------------------------------------------------------------

    def list(self) -> list[dict[str, Any]]:
        """Configured providers, WITHOUT their secrets."""
        out = []
        for entry in self._load():
            out.append({
                "name": entry.get("name", ""),
                "kind": entry.get("kind", ""),
                "target": entry.get("base_url") or " ".join(entry.get("command", [])),
                "has_key": bool(entry.get("api_key")),
                "status": self._live.get(entry.get("name", ""), {}).get("status", "unchecked"),
                "detail": self._live.get(entry.get("name", ""), {}).get("detail", ""),
            })
        return out

    def add(self, entry: dict[str, Any]) -> dict[str, Any]:
        name = str(entry.get("name", "")).strip()
        kind = str(entry.get("kind", "")).strip().lower()
        if not name:
            return {"ok": False, "detail": "a provider needs a name"}
        if kind not in ("signoz", "mcp"):
            return {"ok": False, "detail": "kind must be 'signoz' or 'mcp'"}
        if kind == "signoz" and not str(entry.get("base_url", "")).startswith("http"):
            return {"ok": False, "detail": "signoz needs a base_url (http…)"}
        # A preset is the whole point of presets: the user picks "SigNoz (MCP
        # server)" and does not have to know that its logs tool is called
        # fetch_traces_or_logs. Filling it in here means a wrong tool name
        # cannot be typed at all, and the vendor's real names live in exactly
        # one place. Anything the user set explicitly wins.
        preset = MCP_PRESETS.get(str(entry.get("preset", "")).strip())
        if kind == "mcp" and preset:
            entry = {**{k: v for k, v in preset.items() if k != "label"}, **entry}
        if kind == "mcp" and not entry.get("command"):
            return {"ok": False, "detail": "an mcp provider needs a command to run"}
        if kind == "mcp" and not entry.get("logs_tool"):
            # Without it there is nothing to call, and the connector would
            # register happily and then return zero records forever - the
            # failure mode this whole layer exists to avoid.
            return {"ok": False, "detail":
                    "an mcp provider needs a logs_tool (the tool that returns "
                    "log records) - or pick a preset, which fills it in"}
        entries = [e for e in self._load() if e.get("name") != name]
        entries.append({k: v for k, v in entry.items() if v not in (None, "")})
        self._save(entries)
        self._live.pop(name, None)
        return {"ok": True, "name": name}

    def remove(self, name: str) -> dict[str, Any]:
        entries = [e for e in self._load() if e.get("name") != name]
        self._save(entries)
        self._live.pop(name, None)
        return {"ok": True, "name": name}

    def build(self, name: str):
        """Instantiate one provider, or None with the reason recorded."""
        entry = next((e for e in self._load() if e.get("name") == name), None)
        if entry is None:
            return None
        try:
            if entry.get("kind") == "signoz":
                return SigNozProvider(entry["base_url"], entry.get("api_key", ""))
            client = McpClient(list(entry["command"]))
            return McpProvider(client, McpToolMap(
                logs_tool=entry.get("logs_tool", "query_logs"),
                arguments=entry.get("arguments") or {},
                lines_path=entry.get("lines_path", ""),
                line_key=entry.get("line_key", "body"),
            ), provider_name=name)
        except Exception as exc:
            self._live[name] = {"status": "error",
                                "detail": f"{exc.__class__.__name__}: {exc}"}
            return None

    def check(self, name: str) -> dict[str, Any]:
        """Try one small query. Providers charge money and go down; the user
        must be able to see which state a connector is actually in."""
        provider = self.build(name)
        if provider is None:
            return {"ok": False, "name": name,
                    "detail": self._live.get(name, {}).get("detail", "unknown provider")}
        try:
            records = provider.query_logs(LogFilter(since_minutes=15, limit=5))
            # query_logs SWALLOWS transport failures by design - a dead provider
            # must not stall detection - and records the reason instead of
            # raising. So an empty list means either "nothing matched" or "could
            # not reach it", and only last_error tells them apart. Reading it is
            # the difference between a connector that reports it is down and one
            # that reports "0 records" and looks healthy.
            failure = getattr(provider, "last_error", "")
            if failure:
                self._live[name] = {"status": "error", "detail": failure}
                return {"ok": False, "name": name, "detail": failure}
            self._live[name] = {"status": "ok",
                                "detail": f"{len(records)} record(s) in the last 15m"}
            return {"ok": True, "name": name, "records": len(records),
                    "sample": [r.payload[:120] for r in records[:3]]}
        except Exception as exc:
            self._live[name] = {"status": "error",
                                "detail": f"{exc.__class__.__name__}: {exc}"}
            return {"ok": False, "name": name, "detail": str(exc)}
        finally:
            close = getattr(getattr(provider, "client", None), "close", None)
            if close:
                close()
