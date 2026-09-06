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
from aegis.l8_action.gate import AutonomyGate
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
- imports the code under test relative to the repo root shown in the paths
- exercises the diagnosed failure directly; no mocks of the code under test
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
- every '-' line must be copied EXACTLY from the source excerpts above,
  including comments and whitespace - an inexact line will not apply
- touch ONLY the mapped files; never tests, CI, dependencies, or config secrets
- change the fewest lines that fix the bug; no refactoring, no cleanup

Reply with ONLY a fenced diff code block."""


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
                 tier: str = "T1", project: str = "default") -> None:
        self.router = router
        self.repo = Path(repo_path).resolve()
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

        permit = self.gate.permits_draft()
        if not permit.allowed:
            return self._bundle(Proposal(
                incident_id, "advise",
                f"{permit.reason}. Recommendation: {diagnosis}"))

        evidence = list(incident.get("evidence") or [])
        locations = self.mapper.locate(evidence)
        if not locations:
            return self._bundle(Proposal(
                incident_id, "advise",
                "no evidence line maps to source in this repo - fix by hand, "
                "or add logging so the next incident maps (the gap report)"))

        excerpts = self._excerpts(locations)
        loc_text = "\n".join(f"  {l.file}:{l.line}  {l.source}" for l in locations)

        # 1. Reproducer - a NEW file, and it must FAIL.
        raw = self.router.chat("write_reproducer", [{"role": "user", "content":
            _REPRODUCER_PROMPT.format(diagnosis=diagnosis,
                                      evidence="\n".join(evidence[:6]),
                                      locations=loc_text, excerpts=excerpts)}],
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

        before = self.runner.run(self.TEST_FILENAME, reproducer)
        if not before.executed:
            return self._bundle(Proposal(
                incident_id, "blocked",
                f"sandbox could not run the reproducer: {before.detail}",
                reproducer=reproducer,
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
            _PATCH_PROMPT.format(diagnosis=diagnosis, locations=loc_text,
                                 excerpts=excerpts, reproducer=reproducer)}],
            purpose=f"patch for {incident_id}")
        if raw is None:
            return self._bundle(Proposal(incident_id, "unavailable",
                                         "model unavailable for the patch step",
                                         reproducer=reproducer,
                                         test_before=before.__dict__))
        patch = _code_block(raw)
        verdict = self.gate.validate_patch(patch, [l.file for l in locations])
        if not verdict.allowed:
            return self._bundle(Proposal(
                incident_id, "blocked", f"gate rejected the patch: {verdict.reason}",
                reproducer=reproducer, patch=patch,
                test_before=before.__dict__,
                locations=[l.__dict__ for l in locations]))

        after = self.runner.run(self.TEST_FILENAME, reproducer, patch)
        if not after.executed or after.exit_code != 0:
            return self._bundle(Proposal(
                incident_id, "blocked",
                "the patch did not make the reproducer pass - not proposing "
                "a fix that does not demonstrably fix",
                reproducer=reproducer, patch=patch,
                test_before=before.__dict__, test_after=after.__dict__,
                locations=[l.__dict__ for l in locations]))

        return self._bundle(Proposal(
            incident_id, "draft",
            "reproducer failed before the patch and passes after it",
            reproducer=reproducer, patch=patch,
            test_before=before.__dict__, test_after=after.__dict__,
            locations=[l.__dict__ for l in locations]))

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
        if proposal.patch:
            lines += ["", "DIFF:", "```diff", proposal.patch.rstrip(), "```"]
        lines += ["", "To apply (your decision, your keystrokes):",
                  f"  cd <repo> && patch -p1 < {directory / 'fix.patch'}",
                  "", "Aegis never merges and never writes to your repository."]
        (directory / "PROPOSAL.md").write_text("\n".join(lines) + "\n")
        proposal.bundle_path = str(directory / "PROPOSAL.md")
        return proposal
