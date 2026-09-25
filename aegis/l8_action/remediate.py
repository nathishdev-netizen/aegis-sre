"""C13 - the remediation agent: reproduce first, patch minimally, draft only.

The flow is the doc's, and the order is the safety mechanism:

  incident -> map to code -> FAILING test -> patch -> test passes -> bundle

If the reproducer passes before the patch, the diagnosis is wrong and the
agent STOPS - models are extremely good at producing confident, plausible
patches for problems they have diagnosed incorrectly, and the failing test is
the only defence.

Output is a proposal bundle under ~/.aegis/projects/<p>/proposals/, never a
write to the target repo: applying a fix is a human act. There is no merge
path in this codebase at any tier.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aegis.l3_storage.store import AEGIS_HOME
from aegis.l8_action.gate import MAX_CHANGED_LINES, AutonomyGate
from aegis.l8_action.mapper import TraceToCodeMapper
from aegis.l8_action.runner import TestRunner

_REPRODUCER_PROMPT = """You are writing a REPRODUCER for a diagnosed incident. It must FAIL now, for the right reason, and pass once the underlying bug is fixed.

DIAGNOSIS: {diagnosis}

EVIDENCE (redacted log lines):
{evidence}

MAPPED SOURCE (file:line and the line content):
{locations}

SOURCE EXCERPTS:
{excerpts}

Write ONE self-contained Python test script:
- plain script style: `python3 <file>` exits non-zero on failure (use assert), prints one line on success
- it RUNS FROM {import_root}, which is already the working directory, and
  imports are relative to THAT - not to the repo root. The mapped file
  {import_file} is therefore `{import_stmt}`. Do NOT add sys.path lines: the
  one that looks right (`parents[0]`) is the script's own directory, and it
  turns every run into ModuleNotFoundError - which fails before AND after
  the patch, so the fix can never be proven.
- exercises the diagnosed failure directly; no mocks of the code under test
- it must fail because of the BUG, not because of a missing import, a
  missing dependency or a wrong signature. If the diagnosed path needs
  arguments you cannot know, construct the smallest real object the excerpts
  show, and assert on the behaviour the diagnosis names.
- 30 lines maximum

Reply with ONLY a fenced python code block."""

_PATCH_PROMPT = """You are writing the MINIMAL fix for a diagnosed incident. A failing reproducer already exists; your patch must make it pass.

DIAGNOSIS: {diagnosis}

MAPPED SOURCE (the only files you may touch):
{locations}

SOURCE EXCERPTS:
{excerpts}

REPRODUCER (already failing, do not modify it):
{reproducer}

Rules:
- unified diff format (--- a/path, +++ b/path, @@ hunks), repo-relative paths
- touch ONLY the mapped files; never tests, CI, dependencies, or config secrets
- change the fewest lines that fix the bug; no refactoring, no cleanup.
  If the bug is one expression inside a multi-line statement (a logger
  call, a function call spanning several lines), remove and replace ONLY
  that one inner line - never the whole surrounding statement. Removing
  "logger.info(" to change one f-string argument three lines below it is
  the shape to avoid: it turns a one-line fix into a multi-line block that
  (a) is far more likely to be non-unique in the file and (b) is not
  minimal.
- the applier matches each removed "-" line against the file by its TEXT
  alone, not by position - it does not read the @@ line numbers, so do not
  rely on them to pick between occurrences. If a line like
  "sources = _resolve_sources(request.sources)" could appear more than once
  (e.g. the same call inside a sibling function), remove and replace an
  ADJACENT line instead - or include it - that only appears near the ONE
  occurrence you mean, so the removed text itself is unique in the file.
  Never write a line number into the content of a "-" or "+" line; that
  text becomes part of what the applier searches for and will not be found.
- HARD LIMIT: {max_lines} changed lines (+ and - together) across the whole
  patch. A patch over that is rejected unread, so a rewritten function is
  worth nothing however correct it is. Add a guard, change a condition,
  fix the call - do not restructure the code around it.

Reply with ONLY a fenced diff code block."""


# Tiered, read-only, advisory - never drops a candidate or changes a verdict
# on its own. Ported from SWE-bench's infra_failure.py: separates "the
# environment broke" from "the patch was wrong" so a broken sandbox is never
# silently counted as a bad fix. AMBIGUOUS outranks ENVIRONMENT (checked
# first) because a missing module after a patch could be the patch's own
# doing - removing an import it still needs - not the sandbox.
_ENV_FAILURE = (
    "Cannot allocate memory", "OutOfMemoryError", "MemoryError",
    "Could not resolve host", "Temporary failure in name resolution",
    "Connection refused", "Errno 102",
)
_AMBIGUOUS_FAILURE = (
    "ModuleNotFoundError", "ImportError", "No module named",
    "collected 0 items", "no tests ran", "Ran 0 tests",
    "SyntaxError", "IndentationError",
)


def _classify_failure(output: str) -> str:
    """"environment" | "ambiguous" | "" (a real test failure)."""
    text = output or ""
    if any(marker in text for marker in _ENV_FAILURE):
        return "environment"
    if any(marker in text for marker in _AMBIGUOUS_FAILURE):
        return "ambiguous"
    return ""


def _code_block(text: str) -> str:
    match = re.search(r"```(?:python|diff)?\s*\n(.*?)```", text or "", re.S)
    return match.group(1) if match else (text or "").strip()


@dataclass
class Proposal:
    incident_id: str
    status: str                 # draft | advise | stopped | blocked | unavailable
    detail: str
    reproducer: str = ""
    patch: str = ""
    test_before: dict[str, Any] = field(default_factory=dict)
    test_after: dict[str, Any] = field(default_factory=dict)
    locations: list[dict[str, Any]] = field(default_factory=list)
    bundle_path: str = ""


class RemediationAgent:
    TEST_FILENAME = "aegis_reproducer.py"

    def __init__(self, router: Any, repo_path: str | Path,
                 tier: str = "T1", project: str = "default",
                 code: dict[str, Any] | None = None) -> None:
        self.router = router
        self.repo = Path(repo_path).resolve()
        # C6's analysis, when the project has been analyzed. It turns "grep
        # for this log line" into "this line is written at file:line, in a
        # function reachable from these entrypoints, which also calls these
        # externals" - context a reproducer cannot be written well without.
        self.code = code or {}
        self.gate = AutonomyGate(tier)
        self.mapper = TraceToCodeMapper(self.repo)
        self.runner = TestRunner(self.repo)
        self.project = project

    # -- the flow ------------------------------------------------------------

    def propose(self, incident: dict[str, Any],
                hypothesis: dict[str, Any] | None) -> Proposal:
        incident_id = str(incident.get("id", "?"))
        diagnosis = (hypothesis or {}).get("statement") or incident.get(
            "cause_why", ["no diagnosis available"])[0]
        # The brief tells the reproducer what kind of thing it is testing -
        # the weak reproducer this agent once wrote for a background task
        # came from a prompt that had no idea it WAS a background task.
        project_brief = str(incident.get("project_brief") or "")[:900]
        if project_brief:
            diagnosis = f"[project context: {project_brief}]\\n{diagnosis}"
        # The brief tells the reproducer what kind of thing it is testing -
        # the weak reproducer this agent once wrote for a background task
        # came from a prompt that had no idea it WAS a background task.
        project_brief = str(incident.get("project_brief") or "")[:900]
        if project_brief:
            diagnosis = f"[project context: {project_brief}]\\n{diagnosis}"

        permit = self.gate.permits_draft()
        if not permit.allowed:
            return self._bundle(Proposal(
                incident_id, "advise",
                f"{permit.reason}. Recommendation: {diagnosis}"))

        evidence = list(incident.get("evidence") or [])
        locations = self._locate_via_code(evidence) or self.mapper.locate(evidence)
        # How far a change here reaches. A patch to a leaf helper and a patch
        # to something nine callers depend on are different risks, and the
        # bundle should say which one the reviewer is holding.
        self.blast_radius = self._blast_radius(locations)
        if not locations:
            return self._bundle(Proposal(
                incident_id, "advise",
                "no evidence line maps to source in this repo - fix by hand, "
                "or add logging so the next incident maps (the gap report)"))

        excerpts = self._excerpts(locations)
        loc_text = "\n".join(f"  {l.file}:{l.line}  {l.source}" for l in locations)

        # 0. Smoke-test the environment BEFORE spending a model call on a
        # reproducer written against it. A stale venv guess or wrong import
        # root produces the exact ModuleNotFoundError a bad reproducer
        # would - discovering that only after writing one wastes a call and
        # looks like the model's fault. Self-healing, not just detecting:
        # if the discovered venv/root is wrong, retry from the bare repo
        # root before giving up, since that is where a single-package
        # project's dependencies usually live anyway.
        self.runner.hint = locations[0].file if locations else ""
        smoke = self.runner.smoke_test()
        if _classify_failure(smoke.output) == "ambiguous" and self.runner.hint:
            self.runner.hint = ""
            smoke = self.runner.smoke_test()
        if smoke.executed and smoke.exit_code != 0:
            kind = _classify_failure(smoke.output) or "environment"
            last_line = next((line for line in
                              (smoke.output or "").splitlines()[::-1]
                              if line.strip()), smoke.detail or "")
            return self._bundle(Proposal(
                incident_id, "blocked",
                f"the sandbox cannot even import this project's code yet "
                f"({kind}): {last_line.strip()[:160]}. No model call was "
                "spent - fix the environment (install the project's "
                "dependencies, or point Aegis at the right service "
                "directory) and try again.",
                locations=[l.__dict__ for l in locations]))

        # 1. Reproducer - a NEW file, and it must FAIL.
        raw = self.router.chat("write_reproducer", [{"role": "user", "content":
            _REPRODUCER_PROMPT.format(
                diagnosis=diagnosis,
                evidence="\n".join(evidence[:6]),
                locations=loc_text, excerpts=excerpts,
                **self._import_help(locations))}],
            purpose=f"reproducer for {incident_id}")
        if raw is None:
            return self._bundle(Proposal(incident_id, "unavailable",
                                         "model unavailable or budget spent"))
        reproducer = _code_block(raw)
        existing = {str(p.relative_to(self.repo)) for p in self.repo.rglob("*")
                    if p.is_file()}
        check = self.gate.validate_reproducer(self.TEST_FILENAME, existing)
        if not check.allowed:
            return self._bundle(Proposal(incident_id, "blocked", check.reason))

        # self.runner.hint was already set (and self-healed if the first
        # guess could not even import) by the smoke test above - resetting
        # it here would silently throw that fallback away.
        before = self.runner.run(self.TEST_FILENAME, reproducer)
        if not before.executed:
            return self._bundle(Proposal(
                incident_id, "blocked",
                f"sandbox could not run the reproducer: {before.detail}",
                reproducer=reproducer,
                locations=[l.__dict__ for l in locations]))
        # A reproducer that dies on an import never tested anything: it
        # fails identically before and after the patch, so "before failed,
        # after passed" can never be satisfied and the whole proposal is
        # blocked for a reason that has nothing to do with the bug.
        if _classify_failure(before.output) == "ambiguous":
            first = next(line for line in (before.output or "").splitlines()[::-1]
                         if line.strip())
            missing = ""
            match = re.search(r"No module named ['\"]([\w.]+)", before.output or "")
            if match:
                name = match.group(1).split(".")[0]
                own = (self.repo / name).exists() or (
                    self.repo / f"{name}.py").exists()
                missing = (
                    f" '{name}' is one of this project's own packages, so the "
                    "reproducer imported it by the wrong name."
                    if own else
                    f" '{name}' is a dependency this project needs and this "
                    "machine does not have - create the project's virtualenv "
                    "(.venv) and install into it, and the sandbox will use it.")
            return self._bundle(Proposal(
                incident_id, "blocked",
                "the reproducer could not import this project, so it never "
                f"reached the bug: {first.strip()[:160]}.{missing} Nothing was "
                "proven either way - the reproducer is in the bundle if you "
                "want to run it yourself.",
                reproducer=reproducer, test_before=before.__dict__,
                locations=[l.__dict__ for l in locations]))
        if before.exit_code == 0:
            # The one rule that separates useful from dangerous.
            return self._bundle(Proposal(
                incident_id, "stopped",
                "the reproducer PASSED before any fix - the diagnosis is "
                "wrong, and a patch built on it would be a confident guess. "
                "Stopping, as the protocol requires.",
                reproducer=reproducer, test_before=before.__dict__,
                locations=[l.__dict__ for l in locations]))

        # 2. Patch - minimal, gated, and it must make the reproducer pass.
        raw = self.router.chat("write_patch", [{"role": "user", "content":
            _PATCH_PROMPT.format(diagnosis=diagnosis,
                                 locations=loc_text + self._code_context(locations),
                                 excerpts=excerpts, reproducer=reproducer,
                                 max_lines=MAX_CHANGED_LINES)}],
            purpose=f"patch for {incident_id}")
        if raw is None:
            return self._bundle(Proposal(incident_id, "unavailable",
                                         "model unavailable for the patch step",
                                         reproducer=reproducer,
                                         test_before=before.__dict__))
        patch = _code_block(raw)
        verdict = self.gate.validate_patch(patch, [l.file for l in locations])
        if not verdict.allowed and "not a minimal fix" in verdict.reason:
            # Size is the one objection worth a second attempt: the model
            # rewrote a function when a guard would have done, and it does
            # not know the limit until it is told. Every other rejection -
            # tests, infrastructure, out of scope - is a boundary, not a
            # miss, and retrying it would just be asking twice.
            retry = self.router.chat("write_patch", [{"role": "user", "content":
                _PATCH_PROMPT.format(
                    diagnosis=diagnosis,
                    locations=loc_text + self._code_context(locations),
                    excerpts=excerpts, reproducer=reproducer,
                    max_lines=MAX_CHANGED_LINES)
                + f"\n\nYour previous attempt changed too much: "
                  f"{verdict.reason}. Make the SAME fix in fewer lines - "
                  f"the smallest edit that makes the reproducer pass."}],
                purpose=f"smaller patch for {incident_id}")
            if retry:
                smaller = _code_block(retry)
                second = self.gate.validate_patch(
                    smaller, [l.file for l in locations])
                if second.allowed:
                    patch, verdict = smaller, second
        if not verdict.allowed:
            # A rejection has to leave the reader somewhere. The patch is
            # still in the bundle, still readable, and still the model's
            # reading of the fix - it is just not something Aegis will
            # stand behind, and saying which is the whole difference.
            return self._bundle(Proposal(
                incident_id, "blocked",
                f"gate rejected the patch: {verdict.reason}. The diff is in "
                "the bundle below - read it as a suggestion, apply it by "
                "hand if it is right. Aegis proposes only what it has "
                "proven, and it could not prove this one.",
                reproducer=reproducer, patch=patch,
                test_before=before.__dict__,
                locations=[l.__dict__ for l in locations]))

        after = self.runner.run(self.TEST_FILENAME, reproducer, patch)
        if not after.executed or after.exit_code != 0:
            # "The patch is wrong" and "the sandbox broke" produce the same
            # exit code and used to produce the same message. They are not
            # the same finding: one says the diagnosis or fix needs work,
            # the other says nothing was proven either way.
            kind = _classify_failure(after.output)
            if kind == "environment":
                detail = ("the sandbox itself failed after applying the "
                          f"patch ({(after.output or after.detail or '')[:120]}) "
                          "- this is not evidence the patch is wrong. Nothing "
                          "was proven either way.")
            elif kind == "ambiguous":
                detail = ("the reproducer could not run after the patch "
                          f"({(after.output or '').splitlines()[-1][:120] if after.output else after.detail}) "
                          "- possibly the patch removed something the "
                          "reproducer still needs. Not proposing a fix that "
                          "cannot be shown to work, but this may be a broken "
                          "test rather than a broken patch.")
            else:
                detail = ("the patch did not make the reproducer pass - not "
                          "proposing a fix that does not demonstrably fix")
            return self._bundle(Proposal(
                incident_id, "blocked", detail,
                reproducer=reproducer, patch=patch,
                test_before=before.__dict__, test_after=after.__dict__,
                locations=[l.__dict__ for l in locations]))

        return self._bundle(Proposal(
            incident_id, "draft",
            "reproducer failed before the patch and passes after it",
            reproducer=reproducer, patch=patch,
            test_before=before.__dict__, test_after=after.__dict__,
            locations=[l.__dict__ for l in locations]))


    def _blast_radius(self, locations) -> str:
        """What else depends on the code we are about to patch, per the graph."""
        if not locations:
            return ""
        from aegis.l4_understanding.codegraph import CodeGraph, index_present
        repo = str(self.repo)
        if not index_present(repo):
            return ""
        target = locations[0].fragment or locations[0].source
        answer = CodeGraph(repo).explore(
            f"What calls {target}, directly and indirectly? "
            f"Summarise the blast radius of changing it.")
        if answer.startswith(("no code graph", "code graph unavailable")):
            return ""
        return answer[:1200]


    def _locate_via_operation(self, evidence: list[str]) -> list:
        """Map an evidence line to source by the OPERATION it names.

        Matching log text only works when the line was written by a logger in
        this repo. A connector-sourced line was not: a backend reports
        "process_event completed in 72551ms", which no logging call in the
        source ever wrote, so nothing mapped and remediation stopped even
        though the function is right there in the analysis under that exact
        name. Any telemetry that names its operations - OpenTelemetry spans,
        vendor traces - benefits from this, not one vendor.
        """
        from aegis.l8_action.mapper import Location

        named = {}
        for entry in (self.code.get("entrypoints") or []):
            function = str(entry.get("function") or "")
            if function and entry.get("file"):
                named.setdefault(function, entry)

        found: list[Location] = []
        seen: set[str] = set()
        for line in evidence[:6]:
            # Operation names are identifiers, so only identifier-shaped
            # words can match one; ordinary prose cannot collide with them.
            for word in re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", line):
                entry = named.get(word)
                if entry is None or word in seen:
                    continue
                seen.add(word)
                found.append(Location(
                    file=str(entry.get("file", "")),
                    line=int(entry.get("line", 0) or 0),
                    source=f"{entry.get('kind','')} {entry.get('method','')} "
                           f"{entry.get('path','')} -> {word}()".strip(),
                    fragment=word))
                break
        return found






    def _import_help(self, locations) -> dict:
        """How the reproducer must spell its imports.

        A monorepo service is its own import root, so the same file is
        `services.chatbot.api` from the repo and `api` from the service.
        Telling the model which one applies is the difference between a
        reproducer that tests the bug and one that dies on an import.
        """
        from aegis.l8_action.runner import _import_root
        rel = locations[0].file if locations else ""
        root = _import_root(self.repo, rel)
        try:
            inside = str((self.repo / rel).relative_to(root))
        except (ValueError, OSError):
            inside = rel
        module = inside[:-3].replace("/", ".") if inside.endswith(".py") else inside
        shown = str(root.relative_to(self.repo)) if root != self.repo else "the repo root"
        return {
            "import_root": shown,
            "import_file": inside or rel,
            "import_stmt": f"from {module} import ..." if module else "a direct import",
        }

    def _locate_via_code(self, evidence: list[str]) -> list:
        """Find the source line from the CODE ANALYSIS rather than by grepping.

        Grepping matches whatever text happens to appear; the analysis knows
        which logging call writes a line, in which function, so the patch
        prompt gets the right file, the enclosing function, and its callers.
        """
        statements = self.code.get("log_statements") or []
        if not statements:
            return []
        from aegis.l4_understanding.flowspec import _overlap
        from aegis.l8_action.mapper import Location

        found: list[Location] = []
        for line in evidence[:6]:
            best, score = None, 0.0
            for statement in statements:
                overlap = _overlap(line, statement.get("text", ""))
                if overlap > score:
                    best, score = statement, overlap
            if best is not None and score >= 0.6:
                found.append(Location(
                    file=best.get("file", ""), line=int(best.get("line", 0) or 0),
                    source=f"{best.get('level','')} in {best.get('function')}(): "
                           f"{best.get('text','')[:110]}",
                    fragment=best.get("text", "")[:60]))
        return found

    def _code_context(self, locations) -> str:
        """What else the analysis knows about the functions involved."""
        if not self.code:
            return ""
        functions = {l.source.split(" in ")[-1].split("(")[0]
                     for l in locations if " in " in l.source}
        lines = []
        for entry in self.code.get("entrypoints", []):
            reach = set(self.code.get("calls", {}).get(entry.get("function", ""), []))
            if functions & reach:
                lines.append(f"  reachable from {entry.get('method','')} "
                             f"{entry.get('path','')} -> {entry.get('function')}()")
        for call in self.code.get("external_calls", []):
            if call.get("function") in functions:
                guard = "guarded" if call.get("guarded") else "NOT guarded"
                timeout = "timeout" if call.get("has_timeout") else "NO timeout"
                lines.append(f"  {call.get('function')}() calls {call.get('target')} "
                             f"({guard}, {timeout}) at {call.get('file')}:{call.get('line')}")
        return ("\\n\\nWHAT THE CODE ANALYSIS KNOWS:\\n" + "\\n".join(lines[:8])) if lines else ""

    # -- the bundle ----------------------------------------------------------

    def _excerpts(self, locations, context: int = 6) -> str:
        chunks = []
        for location in locations[:4]:
            path = self.repo / location.file
            try:
                lines = path.read_text(errors="replace").splitlines()
            except OSError:
                continue
            start = max(0, location.line - 1 - context)
            end = min(len(lines), location.line + context)
            body = "\n".join(f"{n + 1:4} {lines[n]}" for n in range(start, end))
            chunks.append(f"# {location.file}\n{body}")
        return "\n\n".join(chunks)

    def _bundle(self, proposal: Proposal) -> Proposal:
        directory = (AEGIS_HOME / "projects" / self.project / "proposals"
                     / proposal.incident_id)
        directory.mkdir(parents=True, exist_ok=True)
        if proposal.reproducer:
            (directory / self.TEST_FILENAME).write_text(proposal.reproducer)
        if proposal.patch:
            (directory / "fix.patch").write_text(proposal.patch)
        lines = [
            f"# [aegis] proposal for {proposal.incident_id}",
            "",
            "STATUS: " + ("DRAFT - requires human review and application"
                          if proposal.status == "draft"
                          else proposal.status.upper()),
            "",
            f"DETAIL: {proposal.detail}",
        ]
        if proposal.locations:
            lines += ["", "MAPPED CODE:"] + [
                f"  {l['file']}:{l['line']}  {l['source']}"
                for l in proposal.locations[:6]]
        if proposal.test_before:
            lines += ["", f"REPRODUCER before patch: exit "
                          f"{proposal.test_before.get('exit_code')} (must be non-zero)"]
        if proposal.test_after:
            lines += [f"REPRODUCER after patch:  exit "
                      f"{proposal.test_after.get('exit_code')} (must be zero)"]
        radius = getattr(self, "blast_radius", "")
        if radius:
            lines += ["", "BLAST RADIUS (from the code graph - what else "
                          "depends on this):", radius]
        if proposal.patch:
            lines += ["", "DIFF:", "```diff", proposal.patch.rstrip(), "```"]
        lines += ["", "To apply (your decision, your keystrokes):",
                  f"  cd <repo> && patch -p1 < {directory / 'fix.patch'}",
                  "", "Aegis never merges and never writes to your repository."]
        (directory / "PROPOSAL.md").write_text("\n".join(lines) + "\n")
        proposal.bundle_path = str(directory / "PROPOSAL.md")
        return proposal
