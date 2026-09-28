"""C6, the code half - read the repo and write down what it is SUPPOSED to do.

Everything here is deterministic: Python's own ast module for Python files,
narrow regexes for the JS/TS route idioms. No model reads the repo. The doc is
explicit about why - a "send the whole repo to an LLM" analysis is unverifiable
and exceeds any context window; the call graph is built by parsing, and models
only ever NAME things afterwards.

What it extracts, and what each part later feeds:

  entrypoints     HTTP routes, mains        -> flow specs get real starts
  call graph      who calls whom            -> reachable steps per entrypoint
  external calls  http/db/subprocess, with  -> cross-service dependencies,
                  guarded/timeout flags        simulation's failure predictions
  log statements  literal format strings    -> template -> file:line mapping,
                                               far stronger than grepping

Read-only, size-capped, and honest: name resolution is best-effort within the
repo, and every derived item carries its file:line so a human can check it.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MAX_FILES = 800
MAX_FILE_BYTES = 600_000
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist",
             "build", ".tox", "site-packages", ".mypy_cache",
             # Vendored runtimes are not this project's code. A bundled
             # CPython inside a .app contributed 86 of 95 "entrypoints" -
             # ast.py, base64.py, calendar.py - and 175 of 181 external
             # calls, so the fact sheet the brief is written from was the
             # standard library and the project's own code never fit.
             "Frameworks", "lib-dynload", "Resources", "Contents",
             "vendor", "third_party", "bower_components", ".gradle",
             ".next", ".nuxt", "target", "Pods", "DerivedData"}

# Directory NAMES are not enough: a bundled interpreter lives at
# "<app>.app/Contents/.../lib/python3.14/", whose every part is innocuous.
SKIP_PATTERNS = (".app/", ".framework/", "/lib/python3", "/lib/python2",
                 "/node_modules/", "/site-packages/", "/dist-packages/",
                 "/.git/", "/__pycache__/")

_HTTP_FUNCS = {"get", "post", "put", "delete", "patch", "request", "head", "options"}
_HTTP_LIBS = {"requests", "httpx", "urllib", "aiohttp", "http"}
_DB_HINTS = {"sqlite3", "psycopg2", "pymysql", "sqlalchemy", "redis", "pymongo",
             "asyncpg", "mysql"}
_ROUTE_DECORATORS = {"get", "post", "put", "delete", "patch", "route", "websocket"}

_JS_ROUTE = re.compile(
    r"\b(?:app|router)\.(get|post|put|delete|patch)\(\s*['\"]([^'\"]+)['\"]")
_URL_LITERAL = re.compile(r"https?://[\w.:\-]+[\w/\-{}]*")


@dataclass
class Entrypoint:
    kind: str            # http | main
    method: str
    path: str
    function: str
    file: str
    line: int

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class ExternalCall:
    kind: str            # http | db | subprocess
    target: str          # url literal, library, or command
    function: str        # enclosing function
    file: str
    line: int
    guarded: bool        # inside try/except
    has_timeout: bool

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class LogStatement:
    text: str            # the literal format string
    level: str
    function: str
    file: str
    line: int

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class CodeAnalysis:
    root: str
    files_scanned: int = 0
    entrypoints: list[Entrypoint] = field(default_factory=list)
    external_calls: list[ExternalCall] = field(default_factory=list)
    log_statements: list[LogStatement] = field(default_factory=list)
    # function -> called function names (best-effort, repo-local resolution)
    calls: dict[str, list[str]] = field(default_factory=dict)
    # functions DEFINED in this repo - the reachable set filters to these, so
    # len()/log.info() noise never pollutes a flow
    defined: set[str] = field(default_factory=set)
    # functions DEFINED in this repo - the reachable set filters to these, so
    # len()/log.info() noise never pollutes a flow
    defined: set[str] = field(default_factory=set)
    # functions DEFINED in this repo - the reachable set filters to these, so
    # len()/log.info() noise never pollutes a flow
    defined: set[str] = field(default_factory=set)
    # functions DEFINED in this repo - the reachable set filters to these, so
    # len()/log.info() noise never pollutes a flow
    defined: set[str] = field(default_factory=set)
    # functions DEFINED in this repo - the reachable set filters to these, so
    # len()/log.info() noise never pollutes a flow
    defined: set[str] = field(default_factory=set)
    skipped: list[str] = field(default_factory=list)

    # -- derived -------------------------------------------------------------

    def reachable_from(self, function: str, depth: int = 6) -> list[str]:
        seen: list[str] = []
        frontier = [function]
        for _ in range(depth):
            nxt = []
            for name in frontier:
                for callee in self.calls.get(name, []):
                    if callee in self.defined and callee not in seen \
                            and callee != function:
                        seen.append(callee)
                        nxt.append(callee)
            frontier = nxt
        return seen

    def flow_for(self, entrypoint: Entrypoint) -> dict[str, Any]:
        """One entrypoint's intended flow: the functions it reaches, the
        external calls those functions make, and how guarded each is."""
        reach = [entrypoint.function] + self.reachable_from(entrypoint.function)
        externals = [c for c in self.external_calls if c.function in reach]
        return {
            "entrypoint": entrypoint.to_dict(),
            "functions": reach,
            "external_calls": [c.to_dict() for c in externals],
            "unguarded": [c.to_dict() for c in externals
                          if not c.guarded or (c.kind == "http" and not c.has_timeout)],
        }

    def dependencies(self) -> list[dict[str, Any]]:
        """Distinct external targets, worst-case guard status per target."""
        by_target: dict[str, dict[str, Any]] = {}
        for call in self.external_calls:
            entry = by_target.setdefault(call.target, {
                "target": call.target, "kind": call.kind, "calls": 0,
                "always_guarded": True, "always_timed": True,
                "where": f"{call.file}:{call.line}"})
            entry["calls"] += 1
            entry["always_guarded"] &= call.guarded
            entry["always_timed"] &= call.has_timeout or call.kind != "http"
        return sorted(by_target.values(), key=lambda d: -d["calls"])

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root, "files_scanned": self.files_scanned,
            "entrypoints": [e.to_dict() for e in self.entrypoints],
            "external_calls": [c.to_dict() for c in self.external_calls],
            "log_statements": [l.to_dict() for l in self.log_statements],
            "calls": self.calls, "defined": sorted(self.defined),
            "skipped": self.skipped,
            "dependencies": self.dependencies(),
        }


class _PyVisitor(ast.NodeVisitor):
    def __init__(self, analysis: CodeAnalysis, rel: str) -> None:
        self.analysis = analysis
        self.rel = rel
        self.func_stack: list[str] = ["<module>"]
        self.try_depth = 0
        self.imports: set[str] = set()

    # -- context -------------------------------------------------------------

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.imports.add(alias.name.split(".")[0])
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module:
            self.imports.add(node.module.split(".")[0])
        self.generic_visit(node)

    def _visit_func(self, node) -> None:
        name = node.name
        self.analysis.defined.add(name)
        self.analysis.defined.add(name)
        self.analysis.defined.add(name)
        self.analysis.defined.add(name)
        self.analysis.defined.add(name)
        for deco in node.decorator_list:
            self._maybe_route(deco, name, node.lineno)
        self.func_stack.append(name)
        self.generic_visit(node)
        self.func_stack.pop()

    visit_FunctionDef = _visit_func
    visit_AsyncFunctionDef = _visit_func

    def visit_Try(self, node: ast.Try) -> None:
        self.try_depth += 1
        for child in node.body:
            self.visit(child)
        self.try_depth -= 1
        for handler in node.handlers:
            self.visit(handler)
        for child in node.finalbody + node.orelse:
            self.visit(child)

    # -- extraction ----------------------------------------------------------

    def _maybe_route(self, deco: ast.expr, function: str, line: int) -> None:
        target = deco.func if isinstance(deco, ast.Call) else deco
        if isinstance(target, ast.Attribute) and target.attr in _ROUTE_DECORATORS:
            path = ""
            if isinstance(deco, ast.Call) and deco.args:
                first = deco.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    path = first.value
            self.analysis.entrypoints.append(Entrypoint(
                kind="http", method=target.attr.upper(), path=path,
                function=function, file=self.rel, line=line))

    def visit_Call(self, node: ast.Call) -> None:
        current = self.func_stack[-1]
        callee = None
        root = None
        if isinstance(node.func, ast.Name):
            callee = node.func.id
        elif isinstance(node.func, ast.Attribute):
            callee = node.func.attr
            base = node.func.value
            while isinstance(base, ast.Attribute):
                base = base.value
            if isinstance(base, ast.Name):
                root = base.id

        if callee:
            self.analysis.calls.setdefault(current, []).append(callee)

        has_timeout = any(k.arg == "timeout" for k in node.keywords)
        url = self._url_in(node)

        if root in _HTTP_LIBS and callee in _HTTP_FUNCS:
            self.analysis.external_calls.append(ExternalCall(
                kind="http", target=url or f"{root}.{callee}(dynamic)",
                function=current, file=self.rel, line=node.lineno,
                guarded=self.try_depth > 0, has_timeout=has_timeout))
        elif root in _DB_HINTS or (callee == "connect" and root in _DB_HINTS):
            self.analysis.external_calls.append(ExternalCall(
                kind="db", target=root or callee, function=current,
                file=self.rel, line=node.lineno,
                guarded=self.try_depth > 0, has_timeout=has_timeout))
        elif root == "subprocess":
            self.analysis.external_calls.append(ExternalCall(
                kind="subprocess", target=callee or "run", function=current,
                file=self.rel, line=node.lineno,
                guarded=self.try_depth > 0, has_timeout=has_timeout))

        # logging calls with a literal first argument -> template mapping.
        # _literal_text, not isinstance(Constant): requiring a plain string
        # dropped every f-string log line silently, and modern code writes
        # almost all of them that way. paideia's vector_agent.py contributed
        # ZERO of 181 log statements for that reason, so nothing could map a
        # line it wrote back to source - which is exactly what a gap report
        # needs. The helper already knew how to take the fixed words out of a
        # JoinedStr; it just was not being asked.
        if callee in ("debug", "info", "warning", "error", "critical", "exception") \
                and node.args:
            text = self._literal_text(node.args[0])
            if text:
                self.analysis.log_statements.append(LogStatement(
                    text=text, level=callee.upper(), function=current,
                    file=self.rel, line=node.lineno))
        self.generic_visit(node)




    @staticmethod
    def _literal_text(node: ast.expr) -> str:
        """The FIXED words of a log call - the parts that appear verbatim in
        every line it writes. f-string slots become a space, so
        f"Step {n}: {node}" yields "Step :" and still matches the template."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr):
            return " ".join(part.value for part in node.values
                            if isinstance(part, ast.Constant)
                            and isinstance(part.value, str)).strip()
        # "a" "b" implicit concatenation, and "a" + var
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left = _PyVisitor._literal_text(node.left)
            right = _PyVisitor._literal_text(node.right)
            return f"{left} {right}".strip()
        return ""

    def _url_in(self, node: ast.Call) -> str:
        for arg in list(node.args) + [k.value for k in node.keywords]:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                match = _URL_LITERAL.search(arg.value)
                if match:
                    return match.group(0)
            if isinstance(arg, ast.JoinedStr):  # f-string: take literal pieces
                literal = "".join(p.value for p in arg.values
                                  if isinstance(p, ast.Constant))
                match = _URL_LITERAL.search(literal)
                if match:
                    return match.group(0)
        return ""








def _config_urls(analysis: CodeAnalysis, tree: ast.AST, rel: str) -> None:
    """Service addresses declared as configuration, anywhere in a file.

    A settings class writes them as class attributes with a default -
    `surrealdb_url: str = Field("ws://localhost:8080/rpc")` - which is a real
    dependency that never appears inside any function call. Scanning only
    call sites missed every such declaration.
    """
    seen: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        match = _URL_LITERAL.search(node.value)
        if not match:
            continue
        url = match.group(0)
        if url in seen or any(c.target == url for c in analysis.external_calls):
            continue
        seen.add(url)
        analysis.external_calls.append(ExternalCall(
            kind="config", target=url, function="<declared>",
            file=rel, line=getattr(node, "lineno", 0),
            # A declaration says nothing about how calls to it are guarded.
            guarded=False, has_timeout=False))


def _resolve_module_urls(analysis: CodeAnalysis, tree: ast.AST, rel: str) -> None:
    """ORCH_URL = "http://localhost:6004" used in an f-string two lines later
    is still a dependency - resolve simple module-level constants."""
    constants: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            match = _URL_LITERAL.search(node.value.value)
            if match:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        constants[target.id] = match.group(0)
    if not constants:
        return
    for call in analysis.external_calls:
        if call.file == rel and "(dynamic)" in call.target and len(constants) == 1:
            call.target = next(iter(constants.values()))


def analyze_repo(root: str | Path) -> CodeAnalysis:
    root = Path(root).resolve()
    analysis = CodeAnalysis(root=str(root))
    count = 0
    for path in sorted(root.rglob("*")):
        if count >= MAX_FILES:
            analysis.skipped.append("file cap reached")
            break
        if not path.is_file() or any(p in SKIP_DIRS for p in path.parts):
            continue
        if any(pat in f"/{path.as_posix()}/" for pat in SKIP_PATTERNS):
            continue
        rel = str(path.relative_to(root))
        if path.suffix == ".py":
            count += 1
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    analysis.skipped.append(rel)
                    continue
                tree = ast.parse(path.read_text(errors="replace"))
            except (SyntaxError, OSError) as exc:
                analysis.skipped.append(f"{rel} ({exc.__class__.__name__})")
                continue
            visitor = _PyVisitor(analysis, rel)
            visitor.visit(tree)
            _resolve_module_urls(analysis, tree, rel)
            _config_urls(analysis, tree, rel)
            _config_urls(analysis, tree, rel)
            _config_urls(analysis, tree, rel)
            _config_urls(analysis, tree, rel)
            if any(isinstance(n, ast.FunctionDef) and n.name == "main"
                   for n in ast.walk(tree)):
                analysis.entrypoints.append(Entrypoint(
                    kind="main", method="", path="", function="main",
                    file=rel, line=1))
        elif path.suffix in (".js", ".ts"):
            count += 1
            try:
                text = path.read_text(errors="replace")
            except OSError:
                continue
            for match in _JS_ROUTE.finditer(text):
                line = text[:match.start()].count("\n") + 1
                analysis.entrypoints.append(Entrypoint(
                    kind="http", method=match.group(1).upper(),
                    path=match.group(2), function="", file=rel, line=line))
    analysis.files_scanned = count
    return analysis
