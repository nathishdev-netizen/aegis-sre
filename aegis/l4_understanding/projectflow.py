"""The project as a picture, assembled from facts that already exist.

Everything here was already computed and already stored - entrypoints and
external calls from the AST, measured operations from the baselines,
observed dependencies from the logs, incidents from correlation. What was
missing was the assembly: the brief could say in prose that a service
"ingests events, classifies them with the OpenAI API, summarises and decides
next actions", while the screen called Flow graph drew one box.

So this builds nodes and edges, and - the part that matters - gives every
node the evidence behind it: which log lines ran through it, what is normal
for it, which incidents touched it, and which line of source writes it.
A node nobody can click into is a diagram, not an answer.

No model is called. If a fact is missing the node simply says less.
"""

from __future__ import annotations

import re
from typing import Any

# Operation names are identifiers; ordinary prose cannot collide with one.
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")


# A URL inside a test is a fixture, not a dependency. The distinction is
# the file it was found in, which the analysis already records - and it is
# the difference between "this service leans on 23 things" and the truth.
_NOT_PRODUCTION = ("/tests/", "/test/", "test_", "_test.", "/migrations/",
                   "/scripts/", "conftest", "/fixtures/", "/examples/")

# Placeholders a developer types when they mean "some address". They are
# never a real dependency and listing them makes the diagram untrustworthy.
_PLACEHOLDER = {"x", "run", "test", "...", "localhost", "example.com",
                "example.org", "example.net", "example.test", "foo", "bar",
                "your-domain.com", "mysite.example.com", "example.ac.uk"}


def _is_real_dependency(target: str, file: str) -> bool:
    """Whether a call site is production code talking to a real address."""
    low_file = (file or "").lower()
    if any(part in low_file for part in _NOT_PRODUCTION):
        return False
    authority = (target or "").split("://")[-1].split("/")[0].lower()
    host = authority.split(":")[0]
    if not host:
        return False
    # A bare "localhost" names nothing; "localhost:8080" names a service on
    # this machine, and those are the most useful dependencies there are.
    if host in _PLACEHOLDER and ":" not in authority:
        return False
    if host in _PLACEHOLDER and host not in ("localhost", "127.0.0.1"):
        return False
    # Schema and namespace URLs are declarations, not calls - nothing ever
    # fetches www.sitemaps.org at runtime.
    if host.startswith(("www.w3.org", "schemas.", "www.sitemaps.org",
                        "xmlns.")):
        return False
    return True


def _kind_of_target(target: str) -> str:
    """What sort of thing a service talks to, for grouping and for icons."""
    low = (target or "").lower()
    if any(w in low for w in ("openai", "anthropic", "groq", "comet", "opik")):
        return "llm"
    if any(w in low for w in ("postgres", "mysql", "redis", "mongo", "surreal",
                              "falkor", "sqlite", "clickhouse")):
        return "datastore"
    if low.startswith(("ws://", "wss://")):
        return "socket"
    if "localhost" in low or "127.0.0.1" in low:
        return "local-service"
    return "external"


def build_flow(code: dict[str, Any] | None,
               operations: list[dict[str, Any]] | None = None,
               dependencies: list[dict[str, Any]] | None = None,
               incidents: list[dict[str, Any]] | None = None,
               events: list[dict[str, Any]] | None = None,
               components: list[str] | None = None) -> dict[str, Any]:
    """One flow: entry points, the work they do, what they depend on.

    Three bands, because that is how a reader thinks about a service: what
    comes IN, what it DOES, and what it LEANS ON.
    """
    code = code or {}
    operations = operations or []
    dependencies = dependencies or []
    incidents = incidents or []
    events = events or []

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    # -- band 1: the ways in ----------------------------------------------
    for entry in (code.get("entrypoints") or [])[:24]:
        function = str(entry.get("function") or "")
        if not function:
            continue
        label = (f"{entry.get('method','')} {entry.get('path','')}".strip()
                 or function)
        nodes.append({
            "id": f"entry:{function}",
            "band": "entry",
            "label": label,
            "detail": f"{function}()",
            "source": f"{entry.get('file')}:{entry.get('line')}",
            "kind": entry.get("kind") or "http",
        })

    # -- band 2: the work, and what it costs -------------------------------
    # Measured operations are the only nodes with evidence of actually
    # running, so they carry the timings and the incidents.
    for row in operations[:14]:
        operation = str(row.get("operation") or "")
        name = operation.split()[0] if operation else ""
        if not name:
            continue
        node = {
            "id": f"op:{name}",
            "band": "work",
            "label": name,
            "kind": "operation",
            "median_ms": row.get("median_ms"),
            "p95_ms": row.get("p95_ms"),
            "samples": row.get("count"),
            "observed": True,
        }
        # p95 far above the median is the shape of an occasional hang, and
        # the reader should see it on the node rather than discover it in an
        # incident three screens away.
        try:
            if row.get("median_ms") and row.get("p95_ms"):
                node["spread"] = round(row["p95_ms"] / max(row["median_ms"], 1), 1)
        except (TypeError, ZeroDivisionError):
            pass
        nodes.append(node)

    # An entrypoint whose name matches an operation is that operation's way
    # in - the only link the facts actually support, so the only one drawn.
    operation_ids = {n["label"]: n["id"] for n in nodes if n["band"] == "work"}
    for node in [n for n in nodes if n["band"] == "entry"]:
        function = node["detail"].rstrip("()")
        target = operation_ids.get(function)
        if target:
            edges.append({"from": node["id"], "to": target, "why": "same name"})

    # -- band 3: what it leans on ------------------------------------------
    # Code says what CAN be called; logs say what WAS. Both are shown, and
    # which one a node rests on is stated, never blurred.
    seen_targets: dict[str, dict[str, Any]] = {}
    for call in (code.get("external_calls") or []):
        target = str(call.get("target") or "")
        if not target:
            continue
        if not _is_real_dependency(target, str(call.get("file") or "")):
            continue
        key = target.split("://")[-1].split("/")[0] or target
        # 127.0.0.1:8080 and localhost:8080 are one service. Two nodes for
        # one thing is the duplicate-port bug in a third costume.
        key = key.replace("127.0.0.1", "localhost")
        node = seen_targets.get(key)
        if node is None:
            node = {
                "id": f"dep:{key}",
                "band": "depends",
                "label": key,
                "kind": _kind_of_target(target),
                "in_code": True,
                "observed": False,
                "unguarded": 0,
                "call_sites": [],
            }
            seen_targets[key] = node
            nodes.append(node)
        node["call_sites"].append({
            "function": call.get("function"),
            "source": f"{call.get('file')}:{call.get('line')}",
            "guarded": bool(call.get("guarded")),
            "has_timeout": bool(call.get("has_timeout")),
        })
        if not call.get("guarded") or (call.get("kind") == "http"
                                       and not call.get("has_timeout")):
            node["unguarded"] += 1

    for row in dependencies:
        host = str(row.get("host") or "")
        if not host:
            continue
        # The observed path needs the same filter as the code path: these
        # hosts reach the logs because a test printed them, and a filter
        # applied to only one of two doors is not a filter.
        # Check the full address: this path had only the bare host, so a
        # real service on localhost:8080 was judged as if it were the word
        # "localhost" and thrown away.
        full = f"{host}:{row['port']}" if row.get("port") else host
        if not _is_real_dependency(full, ""):
            continue
        host = host.replace("127.0.0.1", "localhost")
        key = f"{host}:{row['port']}" if row.get("port") else host
        node = seen_targets.get(key) or seen_targets.get(host)
        if node is None:
            node = {
                "id": f"dep:{key}",
                "band": "depends",
                "label": key,
                "kind": _kind_of_target(key),
                "in_code": False,
                "unguarded": 0,
                "call_sites": [],
            }
            seen_targets[key] = node
            nodes.append(node)
        node["observed"] = True
        node["evidence"] = row.get("evidence")
        node["running_here"] = bool(row.get("running_here"))
        # A dependency running on this machine with a readable log is the
        # one suggestion worth making: the symptom lands in the watched
        # service's log and the CAUSE lands in this one, which nobody is
        # reading. "Two projects exist on your machine" is a much weaker
        # thing to say than "the database you depend on is right here".
        if row.get("combine_path"):
            node["combine_path"] = row["combine_path"]
        node["process"] = row.get("process") or ""

    # Work depends on everything the service depends on - the AST knows the
    # call site but not which operation reached it at runtime, and inventing
    # that link would be a guess presented as a fact.
    work_nodes = [n for n in nodes if n["band"] == "work"]
    for dependency in [n for n in nodes if n["band"] == "depends"]:
        for work in work_nodes:
            edges.append({"from": work["id"], "to": dependency["id"],
                          "why": "declared in code", "weak": True})

    # -- evidence: the reverse index the UI needs --------------------------
    # Clicking a node has to answer "show me". Every one of these facts was
    # already stored; none of it was addressable by node until now.
    for node in nodes:
        name = node.get("label", "").split()[0]
        if not name:
            continue
        lowered = name.lower()
        node["incidents"] = [
            incident.get("id") for incident in incidents
            if any(lowered in str(e).lower()
                   for e in (incident.get("evidence") or []))
        ][:6]
        node["log_lines"] = [
            event.get("text", "")[:150] for event in events
            if lowered in str(event.get("text", "")).lower()
        ][:5]

    # The blind spots: things this service leans on, running here, whose
    # logs nobody is reading.
    blind = []
    seen_logs: set[str] = set()
    for node in nodes:
        if node["band"] != "depends" or not node.get("running_here"):
            continue
        path = node.get("combine_path")
        # localhost:8080 and 127.0.0.1:8080 are the same service, and the
        # log file is what proves it - suggesting both would be the
        # duplicate-port bug wearing a different hat.
        if not path or path in seen_logs:
            continue
        seen_logs.add(path)
        blind.append(node)

    return {
        "nodes": nodes,
        "edges": edges,
        "blind_spots": blind,
        "bands": ["entry", "work", "depends"],
        "counts": {
            "entry": sum(1 for n in nodes if n["band"] == "entry"),
            "work": sum(1 for n in nodes if n["band"] == "work"),
            "depends": sum(1 for n in nodes if n["band"] == "depends"),
            "unguarded": sum(n.get("unguarded", 0) for n in nodes),
        },
        # What a reader must not have to guess: which nodes rest on observed
        # behaviour and which are only declared in source.
        "legend": {
            "observed": "seen running in the logs",
            "declared": "found in the code, never seen running",
            "unguarded": "no try/except, or an HTTP call with no timeout",
        },
    }
