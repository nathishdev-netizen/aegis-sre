"""Read-only HTTP tools: a watched system's own GET endpoints, offered to an
agent as tools without writing a line of code in that system.

Logs say what happened. They cannot say where a message is NOW - stuck in a
retry queue, quarantined, sitting as an unsent draft - and the services that
know already answer that over HTTP. This turns a declared list of those GET
endpoints into tools.

The file is the whole surface. A model can only call a tool it declares, and
every rule that keeps that surface read-only is enforced here, not trusted to
the file or the model:

  GET only      there is no method field. A file that declares one other than
                GET is refused, so a write cannot be configured by accident.
  fixed shape   path values are percent-encoded whole, so an argument cannot
                add a path segment or a query; only declared query parameters
                are sent; everything else the caller passes is dropped.
  fixed values  "fixed_query" is sent on every call and cannot be overridden -
                for limits that protect the system behind the endpoint (a
                listing that costs the upstream a call per row, a daily quota).
  secrets       header values like "${ADMIN_KEY}" are read from the
                environment at call time and never appear in any listing,
                result or error.
  bounded       a timeout, a response cap, and redaction of the body through
                the same redactor the log pipeline uses.

Configured by AEGIS_HTTP_TOOLS=<path to a JSON file>; unset, no tools exist.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from aegis.l2_normalization.redactor import redact

HTTP_TOOLS_ENV = "AEGIS_HTTP_TOOLS"
DEFAULT_TIMEOUT_S = 10.0
DEFAULT_MAX_BYTES = 20_000

_NAME = re.compile(r"[a-z][a-z0-9_]{1,47}")
_PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]*)\}")
_SECRET_REF = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")

# (status, body bytes). Injected in tests; urllib in production.
Transport = Callable[[str, dict[str, str], float, int], tuple[int, bytes]]


class HttpToolsConfigError(ValueError):
    """The file is refused as a whole: a half-loaded toolbox hides mistakes."""


@dataclass(frozen=True)
class HttpTool:
    name: str
    description: str
    service: str
    path: str
    path_params: dict[str, str] = field(default_factory=dict)
    query: dict[str, str] = field(default_factory=dict)
    fixed_query: dict[str, str] = field(default_factory=dict)
    redact: bool = True
    max_bytes: int = DEFAULT_MAX_BYTES

    def schema(self) -> dict[str, Any]:
        properties = {key: {"type": "string", "description": desc}
                      for key, desc in {**self.path_params, **self.query}.items()}
        return {"name": self.name, "description": self.description,
                "inputSchema": {"type": "object", "properties": properties,
                                "required": list(self.path_params)}}


@dataclass(frozen=True)
class _Service:
    base_url: str
    headers: dict[str, str]
    timeout_s: float


def _urllib_transport(url: str, headers: dict[str, str], timeout_s: float,
                      max_bytes: int) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310
            return response.status, response.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(max_bytes + 1)


class HttpToolbox:
    def __init__(self, services: dict[str, _Service], tools: list[HttpTool],
                 transport: Transport | None = None) -> None:
        self._services = services
        self._tools = {tool.name: tool for tool in tools}
        self._transport = transport or _urllib_transport

    # -- loading -------------------------------------------------------------

    @classmethod
    def from_env(cls, reserved: set[str] = frozenset(),
                 transport: Transport | None = None) -> "HttpToolbox | None":
        path = os.environ.get(HTTP_TOOLS_ENV, "").strip()
        if not path:
            return None
        return cls.load(Path(path).expanduser(), reserved, transport)

    @classmethod
    def load(cls, path: Path, reserved: set[str] = frozenset(),
             transport: Transport | None = None) -> "HttpToolbox":
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise HttpToolsConfigError(f"cannot read {path}: {exc}") from exc
        return cls.from_dict(data, reserved, transport)

    @classmethod
    def from_dict(cls, data: dict[str, Any], reserved: set[str] = frozenset(),
                  transport: Transport | None = None) -> "HttpToolbox":
        if not isinstance(data, dict):
            raise HttpToolsConfigError("the file must be a JSON object")
        services: dict[str, _Service] = {}
        for name, spec in (data.get("services") or {}).items():
            base = str((spec or {}).get("base_url", "")).rstrip("/")
            if not base.startswith(("http://", "https://")):
                raise HttpToolsConfigError(f"service {name!r}: base_url must be http(s)")
            headers = {str(k): str(v) for k, v in (spec.get("headers") or {}).items()}
            services[name] = _Service(base, headers,
                                      float(spec.get("timeout_s", DEFAULT_TIMEOUT_S)))

        tools: list[HttpTool] = []
        seen: set[str] = set()
        for raw in data.get("tools") or []:
            name = str(raw.get("name", ""))
            if not _NAME.fullmatch(name):
                raise HttpToolsConfigError(f"tool name {name!r}: use lowercase letters, digits, _")
            if name in seen or name in reserved:
                raise HttpToolsConfigError(f"tool name {name!r} is already taken")
            method = str(raw.get("method", "GET")).upper()
            if method != "GET":
                raise HttpToolsConfigError(f"tool {name!r}: only GET is allowed, not {method}")
            service = str(raw.get("service", ""))
            if service not in services:
                raise HttpToolsConfigError(f"tool {name!r}: unknown service {service!r}")
            path = str(raw.get("path", ""))
            if not path.startswith("/") or "?" in path or "#" in path:
                raise HttpToolsConfigError(f"tool {name!r}: path must start with / and carry no query")
            path_params = {str(k): str(v) for k, v in (raw.get("path_params") or {}).items()}
            if set(_PLACEHOLDER.findall(path)) != set(path_params):
                raise HttpToolsConfigError(
                    f"tool {name!r}: path placeholders and path_params must match")
            query = {str(k): str(v) for k, v in (raw.get("query") or {}).items()}
            fixed_query = {str(k): str(v) for k, v in (raw.get("fixed_query") or {}).items()}
            if set(query) & set(path_params) or set(fixed_query) & (set(query) | set(path_params)):
                raise HttpToolsConfigError(f"tool {name!r}: a parameter name is declared twice")
            description = str(raw.get("description", "")).strip()
            if not description:
                raise HttpToolsConfigError(f"tool {name!r}: a description is required - "
                                           "it is all the model knows about the tool")
            tools.append(HttpTool(name, description, service, path, path_params, query,
                                  fixed_query, bool(raw.get("redact", True)),
                                  int(raw.get("max_bytes", DEFAULT_MAX_BYTES))))
            seen.add(name)
        return cls(services, tools, transport)

    # -- use -----------------------------------------------------------------

    def names(self) -> set[str]:
        return set(self._tools)

    def catalogue(self) -> list[dict[str, Any]]:
        return [tool.schema() for tool in self._tools.values()]

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        tool = self._tools.get(name)
        if tool is None:
            return {"ok": False, "error": f"no such tool: {name}"}
        missing = [key for key in tool.path_params
                   if not str(arguments.get(key, "")).strip()]
        if missing:
            return {"ok": False, "error": f"missing required argument(s): {missing}"}
        service = self._services[tool.service]

        path = _PLACEHOLDER.sub(
            lambda m: urllib.parse.quote(str(arguments[m.group(1)]).strip(), safe=""),
            tool.path)
        params = {key: str(arguments[key]) for key in tool.query
                  if arguments.get(key) not in (None, "")}
        params.update(tool.fixed_query)
        url = service.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)

        headers = {"Accept": "application/json"}
        for key, value in service.headers.items():
            unresolved = [ref for ref in _SECRET_REF.findall(value) if not os.environ.get(ref)]
            if unresolved:
                return {"ok": False, "error": f"secret not configured: {unresolved}"}
            headers[key] = _SECRET_REF.sub(lambda m: os.environ[m.group(1)], value)

        shown = service.base_url + path  # never the query values' secrets: there are none
        try:
            status, body = self._transport(url, headers, service.timeout_s, tool.max_bytes)
        except Exception as exc:  # unreachable service is an answer, not a crash
            return {"ok": False, "request": f"GET {shown}",
                    "error": f"{exc.__class__.__name__}: {exc}"}

        truncated = len(body) > tool.max_bytes
        text = body[:tool.max_bytes].decode("utf-8", errors="replace")
        if not truncated:
            try:
                text = json.dumps(json.loads(text), separators=(",", ":"))
            except ValueError:
                pass
        if tool.redact:
            text = redact(text)
        for value in headers.values():  # a service that echoes a header back
            if len(value) >= 8:
                text = text.replace(value, "***")
        return {"ok": 200 <= status < 300, "status": status, "request": f"GET {shown}",
                "truncated": truncated, "body": text}
