"""What IS this project? - a model-written brief, grounded in extracted facts.

The AST analysis (C6) knows structure exactly: routes, calls, log sites. What
it cannot know is intent - that this repo is a lead-qualification service, that
process_event is a background classifier, that the graph store is the memory
every answer depends on. Intent is what makes every downstream explanation
sharper: an investigator told "this is a background task" does not treat a long
duration like a user-facing hang.

So this is the one place structure is turned into a description, under the same
rules as every other model call here:

  - the model sees ONLY extracted facts (entrypoints, dependencies, observed
    operations, log volume), never raw source or raw logs
  - every identifier it names is checked against those facts; sentences naming
    things that do not exist are dropped, and the document says so
  - the result is cached against a hash of its inputs, so it regenerates when
    the code or the observed behaviour changes and never costs a call otherwise
  - with no model configured there is simply no brief - the fact sheet itself
    is still available, because it required no model to build

Generic by construction: the inputs are the analysis schema every repo shares.
Nothing here knows a project name, a vendor, or a framework.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

_PROMPT = """You are writing a short technical brief describing a software project to an engineer who has never seen it. You know ONLY the facts below, extracted from its code and its telemetry. Do not invent components, endpoints, or purposes that the facts do not support; if the facts are thin, say what is unknown.

FACTS:
{facts}

Write it as a FLOW an engineer can follow. Emit all SIX headings below,
each alone on its own line, in this order, even when a section is empty -
write the heading and then one line saying there is nothing. Never fold a
section's content into the previous section's last line, and never use a
markdown table.

WHAT IT IS
One or two sentences: what kind of system this appears to be and what it
exists to do.

HOW A REQUEST FLOWS
Trace one request end to end as numbered steps, in order: the entry point
it arrives at, what that calls, what those call in turn, and where the
answer comes back from. The entrypoints list gives the real routes with
their methods and handler functions - start from one of those (pick the
one the rest of the facts say most about), never from "an incoming
request reaches the application". One step per line, "1. " through however many
steps the facts support. Name the exact function or route at each step.
If the facts only support two or three steps, write two or three - do not
pad the chain with steps the facts do not show.

WHAT RUNS IN THE BACKGROUND
Operations that are not serving a request - schedulers, workers, startup.
If the facts show none, say so in one line.

WHAT IT LEANS ON
One line per external system in external_calls: its address, what calls
it, and whether the call is guarded (timeout/retry) or not. Mark unguarded
ones plainly - they are the ones whose failure the code does not handle.
Anything in test_only_urls appears ONLY in test files: do not list those
as dependencies - at most say in one line how many there are.

WHAT IS MEASURED
What observed_operations shows: these are MEASURED medians and p95s from
live telemetry, with sample counts. If the list is non-empty you must state
the numbers; claiming telemetry is unavailable while it is listed is an
error. A p95 far above the median is worth calling out. If the list IS
empty, say "nothing measured yet" and why that limits the rest.

WHAT IS NOT KNOWABLE
Plainly, what these facts cannot tell you.

Refer to functions, routes and operations by their exact names from the
facts. Do not invent a step, a caller or a dependency to complete a chain."""


def build_fact_sheet(code: dict[str, Any] | None,
                     operations: list[dict[str, Any]] | None = None,
                     templates: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """The model's entire world: structure from the analysis, behaviour from
    telemetry. Assembled without a model, so it exists even when no key does."""
    code = code or {}
    entrypoints = [{
        "kind": e.get("kind", ""), "method": e.get("method", ""),
        "path": e.get("path", ""), "function": e.get("function", ""),
    } for e in (code.get("entrypoints") or [])[:40]]
    external = [{
        # The analysis names this field "target"; reading "url" here fed the
        # model forty external calls with blank addresses, and its brief
        # honestly reported "empty URLs" - garbage in, garbage described.
        "url": c.get("target") or c.get("url", ""),
        "function": c.get("function", ""),
        "file": c.get("file", ""),
        "guarded": bool(c.get("guarded") or c.get("has_timeout")),
    } for c in (code.get("external_calls") or [])[:40]]
    observed = [{
        "operation": o.get("operation", ""),
        "median_ms": o.get("median_ms"), "p95_ms": o.get("p95_ms"),
        "samples": o.get("count"),
    } for o in (operations or [])[:20]]
    # De-duplicate external calls by address: forty rows saying the same
    # unguarded URL is bulk, not information, and bulk is what pushed the
    # measured telemetry past the prompt's size cap - the model then
    # truthfully reported "no telemetry supplied" three runs in a row while
    # the numbers sat in the full dict, cut off. Key order is meaning here:
    # dict order survives serialisation, so the measured facts go FIRST and
    # any truncation eats names, never numbers.
    unique: dict[str, dict[str, Any]] = {}
    for c in external:
        key = c["url"] or c["function"]
        if key and key not in unique:
            unique[key] = c
    # A URL that only ever appears in a test file is a fixture, not a
    # dependency. Nine rows of example.com from test_json_store.py filled
    # the prompt ahead of the project's own routes, and the model then
    # reported - truthfully, from what it was given - that the entry points
    # were "not identifiable".
    real = [c for c in unique.values() if not _is_test_path(c.get("file", ""))]
    fixtures = [c for c in unique.values() if _is_test_path(c.get("file", ""))]
    # Key order is meaning here: dict order survives serialisation, so what
    # the project IS goes first and any truncation eats the tail. Measured
    # numbers, then the routes that define the system, then what it calls -
    # test fixtures last, since they describe the tests, not the service.
    return {
        "observed_operations": observed,
        "files_scanned": code.get("files_scanned", 0),
        "log_statement_count": len(code.get("log_statements") or []),
        "template_count": len(templates or []),
        "entrypoints": entrypoints,
        "external_calls": real[:25],
        "dependencies": (code.get("dependencies") or [])[:20],
        "test_only_urls": [c.get("url", "") for c in fixtures][:10],
    }


def _is_test_path(path: str) -> bool:
    low = str(path or "").lower()
    return ("/test" in low or low.startswith("test")
            or "/tests/" in low or "_test." in low or "conftest" in low)


def _known_identifiers(facts: dict[str, Any]) -> set[str]:
    """Every name the model is allowed to use. Grounding = cite from this set."""
    known: set[str] = set()
    for e in facts.get("entrypoints", []):
        known.update(v for v in (e.get("function"), e.get("path")) if v)
    for c in facts.get("external_calls", []):
        if c.get("function"):
            known.add(c["function"])
        if c.get("url"):
            known.add(c["url"])
    for o in facts.get("observed_operations", []):
        # "summarize completed" -> both the phrase and its head word
        op = str(o.get("operation") or "")
        if op:
            known.add(op)
            known.add(op.split()[0])
    for d in facts.get("dependencies", []):
        known.add(str(d))
    return known


_IDENT = re.compile(r"`([^`]+)`|\b([a-z_][a-z0-9_]{3,}(?:_[a-z0-9_]+)+)\b")


def ground(text: str, facts: dict[str, Any]) -> tuple[str, list[str]]:
    """Drop sentences naming identifiers the facts do not contain.

    The check is deliberately narrow: only identifier-shaped tokens
    (snake_case or backtick-quoted) are challenged, so ordinary prose is
    never censored. A dropped sentence is a fabrication caught, and the
    caller reports it rather than hiding it.
    """
    known = _known_identifiers(facts)
    kept: list[str] = []
    dropped: list[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+", text.strip()):
        cited = {a or b for a, b in _IDENT.findall(sentence)}
        bad = {c for c in cited
               if c and not any(c in k or k in c for k in known)}
        if bad:
            dropped.append(f"{sentence[:80]} (named: {', '.join(sorted(bad))})")
        else:
            kept.append(sentence)
    return " ".join(kept), dropped


class ProjectComprehension:
    """Generate, cache and serve one project's brief."""

    def __init__(self, directory: Path) -> None:
        self.path = Path(directory) / "understanding.json"

    def load(self) -> dict[str, Any] | None:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return None

    def brief(self) -> str:
        """The text other layers prepend to their prompts. Empty when absent -
        absence must cost nothing."""
        doc = self.load()
        return (doc or {}).get("brief", "")

    def stale(self, facts: dict[str, Any]) -> bool:
        doc = self.load()
        return doc is None or doc.get("facts_hash") != self._hash(facts)

    @staticmethod
    def _hash(facts: dict[str, Any]) -> str:
        return hashlib.sha256(
            json.dumps(facts, sort_keys=True).encode()).hexdigest()[:16]

    def generate(self, facts: dict[str, Any], router: Any,
                 force: bool = False) -> dict[str, Any]:
        """One governed call, or the cache, or an honest absence."""
        if not force and not self.stale(facts):
            doc = self.load() or {}
            doc["from_cache"] = True
            return doc
        raw = router.chat(
            "describe_project",
            [{"role": "user",
              "content": _PROMPT.format(facts=json.dumps(facts, indent=1)[:14000])}],
            purpose="write the project brief")
        if raw is None:
            return {"ok": False, "brief": "",
                    "detail": "model unavailable - the fact sheet below is "
                              "complete without it", "facts": facts}
        brief, dropped = ground(raw.strip(), facts)
        doc = {
            "ok": True,
            "brief": brief,
            "dropped_as_ungrounded": dropped,
            "facts_hash": self._hash(facts),
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "facts": facts,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(doc, indent=1))
        except OSError:
            pass  # a read-only disk costs the cache, not the answer
        return doc
