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

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aegis.l3_storage.store import AEGIS_HOME
from aegis.l8_action.gate import MAX_CHANGED_LINES, AutonomyGate
from aegis.l8_action.mapper import TraceToCodeMapper
from aegis.l8_action.runner import PROXIMITY_LINES, TestRunner

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
- assert the CORRECT behaviour, never the broken one. Do NOT catch the
  exception the bug raises and treat catching it as success - a script that
  does `try: buggy() / except TheError: print("SUCCESS")` exits 0 while the
  bug is present, which is backwards: it then passes before the patch and
  FAILS after it, and the fix can never be proven. Call the code under test
  plainly and let the bug's own exception fail the script; where the bug
  returns a wrong VALUE rather than raising, assert the value the diagnosis
  says it should have returned.
- never invent the exception type or message. Use the one the EVIDENCE shows,
  verbatim. A test written against NotImplementedError when the code raises
  ValueError fails before the patch AND after it, for a reason that has nothing
  to do with the bug - and it looks exactly like a patch that did not work.
- assert only what the EXCERPTS literally show. Never infer a return format
  by symmetry with a neighbouring line: if one branch returns a prefixed
  value like "lead:" + x and the branch you are testing returns x plainly,
  the expected value is x itself - NOT a "session:" + x you never saw
  written anywhere. Inventing a prefix, a
  wrapper or a type the source does not show makes the assertion fail even
  once the patch is correct, which blocks a good fix for a reason that has
  nothing to do with the bug. When the excerpt does not show what a call
  returns, assert something you CAN see - that it did not raise, or that a
  field the diagnosis names has the value the diagnosis names - rather than
  guessing the exact shape.
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
- the applier ONLY looks at "-" and "+" lines - it has no idea about @@ line
  numbers and it does not read unchanged/context lines in the hunk at all,
  so putting a "def name(...):" line in as context does nothing; it is
  invisible to the applier. Each "-" line is matched against the WHOLE file
  by its own text alone, so if a line like
  "sources = _resolve_sources(request.sources)" could appear in more than
  one function (this exact line appears in BOTH chat() and chat_sync() in
  api.py - check for this before writing a hunk that removes it), you
  cannot disambiguate by what surrounds it in the diff - only by what the
  "-" line ITSELF says. Pick a different, narrower target: remove a line
  that already contains something unique to the one occurrence you mean
  (a nearby variable name only used in that function, or the specific
  argument the diagnosis names), not a generic call shared across
  functions. If no single line in the function is unique on its own,
  rewrite the "-" line to include enough of the surrounding unique
  text on the SAME line (e.g. combine two adjacent statements into one
  "-"/"+" pair) rather than relying on diff context that will not be read.
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


# An except: block whose body only SUCCEEDS - printing, passing, asserting on
# the error's own text. Catching the bug's exception and calling that success
# is the one way a reproducer passes while the bug is present, which is
# exactly backwards: it would fail once the bug is fixed.
_SUCCESS_IN_EXCEPT = re.compile(
    r"except[^\n:]*:\s*\n"              # except SomeError as e:
    r"(?:[ \t]+(?:#[^\n]*)?\n)*"        # blank / comment lines
    r"[ \t]+(?:print\(|pass\b|assert\s)",
    re.M,
)


def _asserts_the_bug(reproducer: str) -> bool:
    """Whether the reproducer treats the bug's own exception as success.

    Deliberately narrow: only the shape that inverts the pass/fail contract.
    A reproducer may legitimately catch an exception to assert something
    about state afterwards - what makes this one wrong is that catching IS
    the success path, so the script exits 0 precisely because the bug fired.
    """
    return bool(_SUCCESS_IN_EXCEPT.search(reproducer or ""))


# How many propose() calls in a row are allowed to end in "blocked" before
# the agent stops trying on its own and hands the incident back to a human
# with everything it tried. Tonight's real case: five attempts at the same
# incident, each failing a different way, the last one quietly rewriting an
# error MESSAGE instead of the actual bug once it ran out of ideas - the
# shape of an agent papering over a symptom rather than admitting it is
# stuck. Two is deliberately tight: one retry to self-correct, then stop.
# The line that names a thing the reproducer can import: a top-level or
# nested def/class. Matched against the enclosing scope of a mapped line.
_DEF_LINE = re.compile(r"^\s*(?:async\s+def|def|class)\s+\w+")


MAX_ATTEMPTS = 2


def _attempts_path(project: str, incident_id: str) -> Path:
    return (AEGIS_HOME / "projects" / project / "proposals"
            / incident_id / "attempts.json")


def _load_attempts(project: str, incident_id: str) -> dict[str, Any]:
    path = _attempts_path(project, incident_id)
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {"count": 0, "stopped": False, "stop_reason": "",
                "history": []}


def _record_attempt(project: str, incident_id: str, status: str,
                    detail: str) -> None:
    """Append one attempt's outcome. Never called for "draft" - a proposal
    that actually worked resets nothing to hide, but a run that succeeded
    is not a failure to count against the limit."""
    state = _load_attempts(project, incident_id)
    state["count"] = int(state.get("count", 0)) + 1
    state.setdefault("history", []).append(
        {"status": status, "detail": str(detail)[:300]})
    state["history"] = state["history"][-10:]
    path = _attempts_path(project, incident_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=1))


def stop_auto_fix(project: str, incident_id: str, reason: str) -> None:
    """Mark an incident as needing a human, not more AI patch attempts.

    Separate from the attempt-count ceiling: this is for a case a human
    has already looked at and judged - like INC-2, whose real cause is a
    missing database table four layers downstream of every line the agent
    kept patching - where letting the count merely run out would mean
    several more wasted, wrong attempts before the same conclusion.
    """
    state = _load_attempts(project, incident_id)
    state["stopped"] = True
    state["stop_reason"] = reason
    path = _attempts_path(project, incident_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=1))


def resume_auto_fix(project: str, incident_id: str) -> None:
    """Undo stop_auto_fix - the human decided it is worth trying again."""
    state = _load_attempts(project, incident_id)
    state["stopped"] = False
    state["stop_reason"] = ""
    state["count"] = 0
    path = _attempts_path(project, incident_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=1))


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
    # What logging would have let this be answered. Present only when Aegis
    # FAILED to explain something - a gap is evidence that would have answered
    # a question somebody actually asked, not a lint of the whole repo.
    gaps: dict[str, Any] = field(default_factory=dict)
    bundle_path: str = ""
    # Shown, never acted on: things worth a human's attention that did not
    # rise to blocking the proposal outright. A diagnosis/traceback mismatch
    # is the first of these - see _diagnosis_mismatch.
    warnings: list[str] = field(default_factory=list)


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

        # A human already told Aegis this incident needs them, not more
        # attempts - or the agent hit the same wall MAX_ATTEMPTS times in a
        # row already. Checked before ANY work: no smoke test, no model
        # call. Tonight's real case for the human-stop: five attempts on
        # the same incident, the last one silently rewriting the error
        # MESSAGE the user sees instead of the real bug once it ran out of
        # honest ideas - the shape of an agent hiding that it is stuck.
        attempts = _load_attempts(self.project, incident_id)
        if attempts.get("stopped"):
            return self._bundle(Proposal(
                incident_id, "advise",
                "a human marked this incident as needing manual attention: "
                f"{attempts.get('stop_reason') or 'no reason recorded'}. "
                "No further automatic attempts will run."))
        if attempts.get("count", 0) >= MAX_ATTEMPTS:
            history = attempts.get("history", [])
            tried = "; ".join(
                f"{h.get('status')}: {h.get('detail', '')[:120]}"
                for h in history[-MAX_ATTEMPTS:])
            return self._bundle(Proposal(
                incident_id, "advise",
                f"{attempts['count']} automatic attempts on this incident "
                f"all failed - stopping rather than keep guessing. What was "
                f"tried: {tried or 'no detail recorded'}. This needs a "
                "person to look at it."))

        permit = self.gate.permits_draft()
        if not permit.allowed:
            return self._bundle(Proposal(
                incident_id, "advise",
                f"{permit.reason}. Recommendation: {diagnosis}"))

        evidence = list(incident.get("evidence") or [])
        locations = self._locate_via_code(evidence) or self.mapper.locate(evidence)
        # The traceback named a source tree, and NONE of it is under the repo
        # path given - so every frame was discarded and whatever mapped did so
        # by text search, which lands on the file that happens to contain the
        # logged string (usually the `except` that wrote it, not the raise).
        # Stop here: a patch built on that is guesswork, and two real attempts
        # were spent this way before the path could be seen to be wrong.
        mismatch = self.mapper.foreign_frame_warning()
        if mismatch:
            return self._bundle(Proposal(
                incident_id, "advise",
                mismatch + " Nothing was patched."))
        # How far a change here reaches. A patch to a leaf helper and a patch
        # to something nine callers depend on are different risks, and the
        # bundle should say which one the reviewer is holding.
        self.blast_radius = self._blast_radius(locations)
        if not locations:
            # The promise this message has been making. Nothing maps, so the
            # useful answer is not "fix by hand" - it is WHICH line would have
            # made the next one mappable.
            from app.core import gaps

            report = gaps.generate(
                "which line of this project does this incident come from?",
                evidence, self.code)
            return self._bundle(Proposal(
                incident_id, "advise",
                "no evidence line maps to source in this repo. " + (
                    report.detail or "Analyze this project so the next "
                    "incident can be traced to a file and a line."),
                gaps=report.to_dict()))

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
        # The line the DIAGNOSIS mapped to - lets the patch applier break a
        # tie when the same call appears in more than one function (see
        # PROXIMITY_LINES in runner.py). Facts, not model output.
        self.runner.hint_line = locations[0].line if locations else None
        smoke = self.runner.smoke_test()
        if _classify_failure(smoke.output) == "ambiguous" and self.runner.hint:
            self.runner.hint = ""
            self.runner.hint_line = None
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
        # Before running it: an inverted reproducer is wrong by its SHAPE, and
        # the exit code cannot tell you. Checked only when it passed before the
        # fix, this was missed whenever the inversion also failed - here, a test
        # asserting the bug raises NotImplementedError against a bug that raises
        # ValueError fails before AND after, so it took the normal path, got a
        # patch, and surfaced as the generic "the patch did not make the
        # reproducer pass". That sends the reader looking at a patch when the
        # test was backwards.
        if _asserts_the_bug(reproducer):
            return self._bundle(Proposal(
                incident_id, "stopped",
                "the reproducer asserts the BUG instead of the fixed "
                "behaviour - it catches the error and treats catching it as "
                "success, so it can only pass while the bug is present. The "
                "diagnosis may well be right; the test is backwards. Not "
                "patching on it.",
                reproducer=reproducer,
                locations=[l.__dict__ for l in locations]))

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
            # The one rule that separates useful from dangerous. But WHY it
            # passed decides what the reader should do next, and the two
            # causes need opposite actions: a wrong diagnosis means look
            # again at the incident, while an inverted reproducer means the
            # diagnosis may be perfectly right and only the test is backwards.
            if _asserts_the_bug(reproducer):
                detail = (
                    "the reproducer PASSED before any fix, because it asserts "
                    "the BUG instead of the fixed behaviour - it catches the "
                    "error and treats catching it as success, so it would fail "
                    "once the bug is fixed. The diagnosis may well be right; "
                    "the test is backwards. Not patching on it.")
            else:
                detail = (
                    "the reproducer PASSED before any fix - the diagnosis is "
                    "wrong, and a patch built on it would be a confident guess. "
                    "Stopping, as the protocol requires.")
            return self._bundle(Proposal(
                incident_id, "stopped", detail,
                reproducer=reproducer, test_before=before.__dict__,
                locations=[l.__dict__ for l in locations]))

        # Flag, never block: does the reproducer's OWN traceback agree with
        # where the diagnosis pointed? A mismatch does not stop the patch
        # attempt - the diagnosis can be close enough even when not exact -
        # but it is worth showing a human before they trust the mapped file.
        mismatch = self._diagnosis_mismatch(before.output, locations)
        warnings = [mismatch] if mismatch else []

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
                locations=[l.__dict__ for l in locations],
                warnings=warnings))

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
                locations=[l.__dict__ for l in locations],
                warnings=warnings))

        return self._bundle(Proposal(
            incident_id, "draft",
            "reproducer failed before the patch and passes after it",
            reproducer=reproducer, patch=patch,
            test_before=before.__dict__, test_after=after.__dict__,
            locations=[l.__dict__ for l in locations],
            warnings=warnings))


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

    # Six lines each way cut the excerpt off one line above the branch the
    # reproducer had to assert on: it showed `if request.session_id:` but not
    # the `return request.session_id` below it, so the model inferred a
    # "session:" prefix by symmetry with the `lead:` branch it COULD see. The
    # patch was then correct and the assertion wrong, which blocks a good fix
    # for a reason unrelated to the bug. Enough lines to carry a whole small
    # function, which is the unit the assertion is actually about.
    def _excerpts(self, locations, context: int = 14) -> str:
        chunks = []
        for location in locations[:4]:
            path = self.repo / location.file
            try:
                lines = path.read_text(errors="replace").splitlines()
            except OSError:
                continue
            start = max(0, location.line - 1 - context)
            end = min(len(lines), location.line + context)
            # Always reach back to the enclosing def/class, however far it is.
            # The reproducer has to IMPORT this thing by name, and a fixed
            # window can cut the signature off: a mapped line 16 below its own
            # `def` left the model with the docstring and body but no name, so
            # it invented `_routing_grade` for `_route_after_in_scope_check`
            # and the run died on ImportError before reaching the bug.
            for n in range(location.line - 1, max(-1, location.line - 401), -1):
                if _DEF_LINE.match(lines[n]):
                    start = min(start, n)
                    break
            body = "\n".join(f"{n + 1:4} {lines[n]}" for n in range(start, end))
            chunks.append(f"# {location.file}\n{body}")
        return "\n\n".join(chunks)

    def _diagnosis_mismatch(self, output: str, locations) -> str:
        """Where the diagnosis pointed vs. where the reproducer actually
        crashed, in the project's own code. Returns a one-line warning to
        SHOW alongside a proposal - never used to block it.

        The diagnosis is the starting point for the whole flow, and until
        tonight nothing checked whether the reproducer's own failure
        actually agreed with it. Real case: the diagnosis for INC-2 named
        api.py:348 (a logger.exception call); the reproducer's real
        traceback bottoms out, in this project's own code, one line away
        at api.py:349 - close enough to look right - while the ACTUAL
        raise is several frames deeper in a third-party library
        (surrealdb), for a missing database table no patch to api.py can
        fix. A human reading both numbers side by side would have caught
        that in seconds; the pipeline did five patch attempts first.
        """
        if not locations or not output:
            return ""
        frames = re.findall(r'File "([^"]+)", line (\d+)', output)
        own = [(f, int(n)) for f, n in frames
               if "/venv/" not in f and "/site-packages/" not in f
               and not f.endswith(self.TEST_FILENAME)]
        if not own:
            return ""
        crash_file, crash_line = own[-1]
        crash_name = Path(crash_file).name
        target = locations[0]
        target_name = Path(target.file).name
        if crash_name != target_name:
            return (f"the diagnosis points at {target.file}, but the "
                    f"reproducer's own traceback bottoms out (in this "
                    f"project's code) in a DIFFERENT file: {crash_name}:"
                    f"{crash_line} - worth checking the diagnosis before "
                    "trusting a patch to the mapped file.")
        gap = abs(crash_line - target.line)
        if gap > PROXIMITY_LINES:
            return (f"the diagnosis points at {target.file}:{target.line}, "
                    f"but the reproducer's own traceback bottoms out (in "
                    f"this project's code) at line {crash_line} - {gap} "
                    "lines away. May be the same function seen from a "
                    "different angle, or the diagnosis may be pointing at "
                    "where the failure was LOGGED rather than where it was "
                    "actually RAISED.")
        return ""

    def _bundle(self, proposal: Proposal) -> Proposal:
        directory = (AEGIS_HOME / "projects" / self.project / "proposals"
                     / proposal.incident_id)
        directory.mkdir(parents=True, exist_ok=True)
        # Only a genuine attempt that BLOCKED counts against the limit -
        # not "draft" (it worked), and not "advise" (which is what the
        # limit-reached message itself returns; counting that would make
        # the warning increment its own counter and never let a human's
        # fix - or stop_auto_fix - actually clear it).
        if proposal.status == "blocked":
            _record_attempt(self.project, proposal.incident_id,
                            proposal.status, proposal.detail)
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
        if proposal.warnings:
            lines += ["", "WARNINGS:"] + [f"  - {w}" for w in proposal.warnings]
        if proposal.locations:
            lines += ["", "MAPPED CODE:"] + [
                f"  {l['file']}:{l['line']}  {l['source']}"
                for l in proposal.locations[:6]]
        if proposal.gaps and proposal.gaps.get("gaps"):
            lines += ["", "WHAT WOULD HAVE LET ME ANSWER THIS:",
                      f"  {proposal.gaps.get('detail', '')}"]
            for gap in proposal.gaps["gaps"]:
                lines += [f"  {gap['file']}:{gap['line']}  ({gap['function']})",
                          f"      {gap['why']}",
                          f"      -> {gap['suggestion']}"]
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
