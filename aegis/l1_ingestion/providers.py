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
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from aegis.contracts.events import RawRecord


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
        except (urllib.error.URLError, OSError, ValueError):
            return []  # a dead provider must not stall anything

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

    def __init__(self, command: list[str], timeout_s: float = 60.0) -> None:
        import subprocess
        self._proc = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1)
        self.timeout_s = timeout_s
        self._next_id = 0
        self._request("initialize")
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


class McpProvider:
    name = "mcp"

    def __init__(self, client: McpClient, tool_map: McpToolMap,
                 provider_name: str = "mcp") -> None:
        self.client = client
        self.tool_map = tool_map
        self.name = provider_name

    def capabilities(self) -> set[str]:
        return {"logs"}

    def query_logs(self, query: LogFilter) -> list[RawRecord]:
        arguments = dict(self.tool_map.arguments)
        if query.service:
            arguments.setdefault("service", query.service)
        try:
            reply = self.client.call_tool(self.tool_map.logs_tool, arguments)
        except (RuntimeError, OSError, ValueError):
            return []
        node: Any = reply
        for part in filter(None, self.tool_map.lines_path.split(".")):
            if not isinstance(node, dict):
                return []
            node = node.get(part)
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
                records.append(RawRecord(source_id=f"{self.name}:mcp",
                                         payload=text, service=query.service))
        return records
