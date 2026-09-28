"""T2's applier: write a proven patch to the working tree, or leave no trace.

The whole value of this step is the rollback, not the write. A patch that
passed its reproducer in a sandbox has proven one thing - it fixes what it
claimed to fix. It has NOT proven that it leaves everything else working,
and that is precisely what a user cannot check quickly by eye.

So the order is: back up every file first, apply, run the project's OWN
test suite, and revert automatically unless that suite agrees. A fix that
breaks two other tests is reverted with the failures quoted, not left in
the tree for the user to discover later.

What this module refuses to do, structurally rather than by prompt:
  - write anything without a backup it can restore from
  - keep a change the project's tests disagree with
  - touch git (no commit, no stage, no branch) - the change sits in the
    working tree where `git diff` shows it and `git checkout` undoes it
  - run when the repo has uncommitted changes it would be mixed into
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aegis.l8_action.runner import (
    _import_root, _interpreter_for, _tolerant_apply)

TEST_TIMEOUT_S = 900

# How the project's own suite is run, in order of preference. The first one
# whose marker file exists is used; nothing is installed and nothing is
# guessed beyond this list.
_SUITES: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("pytest", ("pytest.ini", "setup.cfg", "pyproject.toml", "tests", "test"),
     ("python3", "-m", "pytest", "-q")),
    # "-s tests -p test_*.py" matters: plain `unittest discover` searches
    # the top level only, finds nothing in tests/, and exits non-zero - which
    # read as "your patch broke the suite" on every repo without an
    # __init__.py. Keeping the top level at "tests" avoids requiring one.
    ("unittest", ("tests",),
     ("python3", "-m", "unittest", "discover", "-q", "-s", "tests")),
    ("unittest", ("test",),
     ("python3", "-m", "unittest", "discover", "-q", "-s", "test")),
    ("npm test", ("package.json",), ("npm", "test", "--silent")),
)


@dataclass
class ApplyResult:
    applied: bool
    reverted: bool
    detail: str
    files: list[str] = field(default_factory=list)
    suite: str = ""
    suite_output: str = ""
    backup_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "applied": self.applied, "reverted": self.reverted,
            "detail": self.detail, "files": self.files,
            "suite": self.suite, "suite_output": self.suite_output[-3000:],
            "backup_path": self.backup_path,
        }


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(["git", *args], cwd=repo, text=True,
                              capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None


def dirty_files(repo: Path) -> list[str]:
    """Uncommitted changes the apply would be mixed into. A user who cannot
    tell their own edits from ours cannot review - or revert - either."""
    result = _git(repo, "status", "--porcelain")
    if result is None or result.returncode != 0:
        return []
    names = []
    for line in result.stdout.splitlines():
        name = line[3:].strip().strip('"')
        if not name or _is_build_output(name):
            continue
        names.append(name)
    return names


# Running the suite creates these, so checking for a clean tree AFTER a test
# run would otherwise report the user's repo as dirty because we ran it.
_IGNORED_DIRT = (".aegis", "__pycache__/", ".pytest_cache", ".mypy_cache",
                 ".ruff_cache", ".coverage", "node_modules/", ".DS_Store",
                 "aegis_reproducer.py")


def _is_build_output(name: str) -> bool:
    return (any(frag in name for frag in _IGNORED_DIRT)
            or name.endswith((".pyc", ".pyo", ".orig", ".rej")))


# Which interpreter runs the suite is not a detail: Aegis runs under its OWN
# venv, so a hardcoded "python3" is whatever interpreter Aegis was started
# with. A 3.11 project tested by Aegis's 3.9 fails to import and reads,
# wrongly, as "your suite was already failing". T2's sandbox already solved
# this - nearest-first venv walk from the patched file - so reuse it rather
# than keep a second, weaker copy that can drift.


def _upwards(start: Path, repo: Path) -> list[Path]:
    """start (or its directory), then each parent up to and including repo."""
    here = start if start.is_dir() else start.parent
    chain: list[Path] = []
    while True:
        chain.append(here)
        if here == repo or here.parent == here:
            break
        parent = here.parent
        if parent != repo and repo not in parent.parents:
            break
        here = parent
    return chain


def _with_python(command: tuple[str, ...], python: str) -> tuple[str, ...]:
    return (python,) + command[1:] if command[0] == "python3" else command


def _search_roots(repo: Path, near: list[str]) -> list[Path]:
    """Where to look for a suite: nearest the patch first, repo root last."""
    roots: list[Path] = []
    for rel in near:
        for directory in _upwards(repo / rel, repo):
            if directory not in roots:
                roots.append(directory)
    if repo not in roots:
        roots.append(repo)
    return roots


def find_suite(repo: Path, near: list[str] | None = None,
               ) -> tuple[str, tuple[str, ...], Path, str] | None:
    """The project's own tests, as the project already runs them.

    Returns (name, command, working directory, interpreter).

    Searched from the patched files upwards, not at the repo root only: a
    monorepo root often holds a stub tests/ directory with nothing to do with
    the service being changed, and running that instead of the real one is
    worse than running nothing - it reports a red suite the project never uses.
    """
    for directory in _search_roots(repo, near or []):
        python = _interpreter_for(repo, str(directory.relative_to(repo) / "x.py")
                                 if directory != repo else "")
        for name, markers, command in _SUITES:
            if not any((directory / marker).exists() for marker in markers):
                continue
            command = _with_python(command, python)
            if shutil.which(command[0]) is None and not Path(command[0]).is_file():
                continue
            # which(python3) says nothing about whether pytest is importable.
            # Treating "No module named pytest" as a failing suite would
            # revert good patches and tell the user their fix broke them.
            if command[1:3] == ("-m", "pytest"):
                try:
                    probe = subprocess.run(
                        [command[0], "-c", "import pytest"], cwd=directory,
                        capture_output=True, timeout=30)
                except (OSError, subprocess.SubprocessError):
                    continue
                if probe.returncode != 0:
                    continue
            return name, command, directory, python
    return None


def verify_with_reproducer(repo: Path, reproducer: str,
                          hint: str = "") -> tuple[bool, str]:
    """Run Aegis's OWN reproducer against the real repo.

    This is the cheap verification, and for a paid suite it is the only honest
    one. The project's suite is the broader check, but on a real project it can
    cost real money: paideia's 136 tests are five files of live LLM and
    SurrealDB calls, and apply() runs the suite TWICE (baseline, then patched),
    so one click was ~18 minutes and ~124 LLM-backed tests. A tool that quietly
    spends the user's API budget to verify its own work is not one they can
    leave on.

    The reproducer costs nothing: Aegis already wrote it, it already failed
    before the patch and passed after IN THE SANDBOX, and re-running it here
    proves the patch landed correctly in the real tree - which is the specific
    thing a sandbox proof does not cover.

    Runs it exactly the way T2's sandbox does, via _import_root and
    _interpreter_for. Writing it to the repo root instead failed with
    "ModuleNotFoundError: No module named 'agents.orchestrator'": a monorepo
    service is its own import root, so the reproducer has to run from
    services/chatbot, not the repo. Reusing those two functions rather than
    re-deriving the paths is the point - they already carry the reasoning,
    including that PYTHONPATH must name ONLY the import root because paideia's
    top-level agents/ shadows the service's.

    What it does NOT prove is that everything else still works. Callers must
    say so plainly rather than let silence read as approval.
    """
    if not reproducer.strip():
        return False, "no reproducer to run"
    root = _import_root(repo, hint)
    target = root / "aegis_reproducer.py"
    existed = target.exists()
    previous = target.read_text() if existed else ""
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(reproducer)
        try:
            completed = subprocess.run(
                [_interpreter_for(repo, hint), target.name],
                cwd=root, text=True, capture_output=True, timeout=120,
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                     "HOME": os.environ.get("HOME", str(repo)),
                     "PYTHONPATH": str(root),
                     "PYTHONDONTWRITEBYTECODE": "1"},
            )
        except subprocess.TimeoutExpired:
            return False, "the reproducer did not finish within 120s"
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"could not run the reproducer: {exc}"
        output = (completed.stdout + completed.stderr)[-2000:]
        return completed.returncode == 0, output
    finally:
        # Never leave our own test file behind in the user's repo.
        try:
            if existed:
                target.write_text(previous)
            else:
                target.unlink()
        except OSError:
            pass


class PatchApplier:
    """Applies one proven patch to the working tree, revertibly."""

    def __init__(self, repo_path: str | Path) -> None:
        self.repo = Path(repo_path).resolve()

    def apply(self, patch_text: str, *, allow_dirty: bool = False,
              run_tests: bool = True, reproducer: str = "") -> ApplyResult:
        if not patch_text.strip():
            return ApplyResult(False, False, "no patch to apply")
        if not self.repo.is_dir():
            return ApplyResult(False, False, f"not a directory: {self.repo}")

        dirty = [] if allow_dirty else dirty_files(self.repo)
        if dirty:
            return ApplyResult(
                False, False,
                "your repo has uncommitted changes (" +
                ", ".join(dirty[:4]) +
                (f" and {len(dirty) - 4} more" if len(dirty) > 4 else "") +
                ") - commit or stash them first, so what Aegis changed stays "
                "separable from what you changed")

        targets = _patch_targets(self.repo, patch_text)
        if not targets:
            return ApplyResult(False, False,
                               "patch names no file that exists in this repo")

        backup = Path(tempfile.mkdtemp(prefix="aegis-backup-"))
        for rel in targets:
            source = self.repo / rel
            destination = backup / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)

        applied, detail = _tolerant_apply(self.repo, patch_text)
        if not applied:
            _restore(backup, self.repo, targets)
            shutil.rmtree(backup, ignore_errors=True)
            return ApplyResult(False, False, f"patch did not apply: {detail}")

        if not run_tests:
            # Not "unchecked" - checked by the reproducer, which is free.
            # Running the project's own suite is what can cost money; proving
            # the patch does what it claimed does not.
            if reproducer.strip():
                # The patched file IS the hint: it tells _import_root which
                # directory this code expects to be imported from, which for a
                # monorepo service is the service, not the repo.
                passed, output = verify_with_reproducer(
                    self.repo, reproducer, targets[0] if targets else "")
                if not passed:
                    _restore(backup, self.repo, targets)
                    shutil.rmtree(backup, ignore_errors=True)
                    return ApplyResult(
                        False, True,
                        "REVERTED - the patch applied but its own reproducer "
                        "still fails against your repo, so it did not actually "
                        "fix this. Your files are exactly as they were.",
                        files=targets, suite="reproducer", suite_output=output)
                return ApplyResult(
                    True, False,
                    "applied - Aegis re-ran its own reproducer against your "
                    "repo and it passes. Your project's suite was NOT run, so "
                    "nothing has checked the REST of your code.",
                    files=targets, suite="reproducer", suite_output=output,
                    backup_path=str(backup))
            return ApplyResult(True, False,
                               "applied without running your tests, as asked - "
                               "verify before you commit",
                               files=targets, backup_path=str(backup))

        found = find_suite(self.repo, targets)
        if found is None:
            # No suite is not permission to keep an unverified change: say so
            # plainly and leave it applied only because there is nothing that
            # could have disagreed.
            return ApplyResult(
                True, False,
                "applied, but this repo has no test suite Aegis knows how to "
                "run - nothing has checked it beyond its own reproducer",
                files=targets, backup_path=str(backup))

        name, command, suite_dir, _python = found
        if suite_dir != self.repo:
            name = f"{name} ({suite_dir.relative_to(self.repo)})"
        # Was the suite green BEFORE the patch? A project whose tests were
        # already failing would otherwise have every fix reverted and be
        # told, wrongly, that the fix broke them. Measured, not assumed:
        # the backup is restored first so the baseline is the real one.
        _restore(backup, self.repo, targets)
        baseline_passed, baseline_output = _run_suite(suite_dir, command)
        applied_again, _ = _tolerant_apply(self.repo, patch_text)
        if not applied_again:
            _restore(backup, self.repo, targets)
            shutil.rmtree(backup, ignore_errors=True)
            return ApplyResult(False, True,
                               "patch applied once but not twice - reverted")

        passed, output = _run_suite(suite_dir, command)
        if not baseline_passed and not _suite_ran_nothing(baseline_output):
            # Already red before us. Keeping the patch and saying so is
            # honest; reverting it and blaming the patch is not.
            return ApplyResult(
                True, False,
                f"applied - but your {name} suite was ALREADY failing before "
                "this patch, so it cannot tell us whether this change is "
                "safe. Fix the suite, then re-check.",
                files=targets, suite=name, suite_output=output,
                backup_path=str(backup))
        if not passed and _suite_ran_nothing(output):
            # "NO TESTS RAN" is not a failing suite - it is an absent one,
            # and reverting a good patch while telling the user their fix
            # broke their tests is the worst thing this module could do.
            return ApplyResult(
                True, False,
                f"applied, but your {name} suite collected no tests - "
                "nothing has checked it beyond its own reproducer",
                files=targets, suite=name, suite_output=output,
                backup_path=str(backup))
        if passed:
            return ApplyResult(
                True, False,
                f"applied - your {name} suite still passes",
                files=targets, suite=name, suite_output=output,
                backup_path=str(backup))

        _restore(backup, self.repo, targets)
        shutil.rmtree(backup, ignore_errors=True)
        return ApplyResult(
            False, True,
            f"REVERTED - the patch broke your {name} suite. Your files are "
            "exactly as they were.",
            files=targets, suite=name, suite_output=output)

    def revert(self, backup_path: str, files: list[str]) -> ApplyResult:
        """Undo an applied patch from its backup."""
        backup = Path(backup_path)
        if not backup.is_dir():
            return ApplyResult(False, False,
                               "backup is gone - use `git checkout` on the "
                               "files listed in the proposal")
        _restore(backup, self.repo, files)
        shutil.rmtree(backup, ignore_errors=True)
        return ApplyResult(False, True, "reverted - your files are as they were",
                           files=files)


def _patch_targets(repo: Path, patch_text: str) -> list[str]:
    from aegis.l8_action.gate import changed_files
    targets = []
    for name in changed_files(patch_text):
        if (repo / name).is_file() and name not in targets:
            targets.append(name)
    return targets


def _restore(backup: Path, repo: Path, files: list[str]) -> None:
    for rel in files:
        source = backup / rel
        if source.is_file():
            shutil.copy2(source, repo / rel)


def _suite_ran_nothing(output: str) -> bool:
    low = (output or "").lower()
    return ("no tests ran" in low or "ran 0 tests" in low
            or "no tests collected" in low or "collected 0 items" in low)


def _run_suite(repo: Path, command: tuple[str, ...]) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            list(command), cwd=repo, text=True, capture_output=True,
            timeout=TEST_TIMEOUT_S,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                 "HOME": os.environ.get("HOME", str(repo))},
        )
    except subprocess.TimeoutExpired:
        return False, f"your test suite did not finish within {TEST_TIMEOUT_S}s"
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"could not run your test suite: {exc}"
    return completed.returncode == 0, (completed.stdout + completed.stderr)[-4000:]
