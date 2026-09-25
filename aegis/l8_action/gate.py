"""C13's AutonomyGate - which change classes are permitted, enforced in code.

The doc's hard lines, each one a mechanical check rather than a prompt
instruction, because a model cannot be trusted to police itself:

  - Never auto-merge. Not a tier, not a setting: the capability does not
    exist in this codebase. can_merge() is the documentation of that fact.
  - Never modify tests to make them pass. A patch touching any test file is
    rejected before it is even considered.
  - Never touch infrastructure, secrets, CI, or dependency versions.
  - Minimal or nothing: an oversize patch is scope creep wearing a fix's
    name, and scope creep in a fix PR destroys reviewability.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# T2 applies a patch to the working tree, on one explicit human click, and
# reverts it the moment the project's own tests disagree. T3 - opening a PR
# or merging - does not exist, at any tier. can_merge() is that fact.
TIERS = ("T0", "T1", "T2")

MAX_CHANGED_LINES = 40

# Paths a fix patch may never touch, regardless of tier.
_FORBIDDEN_FRAGMENTS = (
    ".env", "secret", ".github/", ".gitlab-ci", ".circleci", "Dockerfile",
    "docker-compose", "requirements", "package.json", "package-lock",
    "poetry.lock", "Pipfile", "pyproject.toml", "Makefile", ".pre-commit",
)

_TEST_PATH = re.compile(r"(^|/)(tests?/|test_[^/]*$|[^/]*_test\.[a-z]+$)")

_DIFF_FILE = re.compile(r"^(?:---|\+\+\+)\s+(?:[ab]/)?(\S+)", re.M)


def changed_files(diff: str) -> list[str]:
    files = []
    for name in _DIFF_FILE.findall(diff or ""):
        if name not in ("/dev/null",) and name not in files:
            files.append(name)
    return files


def changed_line_count(diff: str) -> int:
    return sum(1 for line in (diff or "").splitlines()
               if (line.startswith("+") or line.startswith("-"))
               and not line.startswith(("+++", "---")))


@dataclass
class Verdict:
    allowed: bool
    reason: str


class AutonomyGate:
    def __init__(self, tier: str = "T0") -> None:
        if tier not in TIERS:
            raise ValueError(f"tier must be one of {TIERS} - T2/T3 do not exist yet")
        self.tier = tier

    def can_merge(self) -> bool:
        """Always False. Not configuration - the merge capability does not
        exist, at any tier, at any confidence level."""
        return False

    def permits_draft(self) -> Verdict:
        if self.tier == "T0":
            return Verdict(False, "tier T0 is advise-only: explanation and "
                                  "recommendation, no code")
        return Verdict(True, f"{self.tier} permits a draft proposal for "
                             "human review")

    def permits_apply(self) -> Verdict:
        """Writing to the user's working tree. Never reachable by a model
        deciding it is confident: the caller must be a human action, and the
        tier must have been raised deliberately."""
        if self.tier != "T2":
            return Verdict(False, f"tier {self.tier} never writes to your "
                                  "repo - a patch is a draft for you to apply")
        return Verdict(True, "T2 permits applying to the working tree, "
                             "revertible, after your tests agree")

    def validate_patch(self, diff: str, allowed_files: list[str]) -> Verdict:
        files = changed_files(diff)
        if not files:
            return Verdict(False, "patch touches no files")
        for name in files:
            if _TEST_PATH.search(name):
                return Verdict(False, f"patch touches test file {name} - "
                                      "modifying tests to make them pass is forbidden")
            lowered = name.lower()
            for fragment in _FORBIDDEN_FRAGMENTS:
                if fragment.lower() in lowered:
                    return Verdict(False, f"patch touches {name} - infrastructure, "
                                          "secrets, CI and dependencies need a "
                                          "separate approval scope")
            if allowed_files and name not in allowed_files:
                return Verdict(False, f"patch touches {name}, which the trace "
                                      "never mapped to - out of scope for this fix")
        lines = changed_line_count(diff)
        if lines > MAX_CHANGED_LINES:
            return Verdict(False, f"{lines} changed lines > {MAX_CHANGED_LINES} - "
                                  "not a minimal fix")
        return Verdict(True, f"{len(files)} file(s), {lines} line(s) - within scope")

    def validate_reproducer(self, filename: str, existing_files: set[str]) -> Verdict:
        if filename in existing_files:
            return Verdict(False, f"{filename} already exists - the reproducer "
                                  "must be a NEW test, never an edit")
        return Verdict(True, "new test file")
