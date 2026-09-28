"""T2: applying a patch to the working tree, and taking it back.

Every test here is about the rollback, not the write. Applying is easy;
what makes it safe to offer is that a change the project's own tests
disagree with leaves no trace.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aegis.l8_action.apply import (  # noqa: E402
    PatchApplier, dirty_files, find_suite)
from aegis.l8_action.gate import AutonomyGate  # noqa: E402


def _repo(with_suite: bool = True, passing: bool = True) -> Path:
    """A tiny git repo with one source file and its own test suite."""
    root = Path(tempfile.mkdtemp(prefix="aegis-apply-test-"))
    (root / "svc.py").write_text("def greet():\n    return 'hi'\n")
    if with_suite:
        (root / "tests").mkdir()
        # unittest.TestCase, not a bare def: `def test_x(): self.assertEqual(...)`
        # has no self, is collected by nothing, and the suite passes vacuously -
        # which silently stopped this file from testing the revert at all.
        (root / "tests" / "test_svc.py").write_text(
            "import sys, unittest; sys.path.insert(0, '.')\n"
            "from svc import greet\n"
            "class TestSvc(unittest.TestCase):\n"
            "    def test_greet(self):\n"
            "        self.assertEqual(greet(), %r)\n" % ("hi" if passing else "UNCHANGED"))
    subprocess.run(["git", "init", "-q"], cwd=root, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=root, capture_output=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "base"], cwd=root, capture_output=True)
    return root


_GOOD_PATCH = """--- a/svc.py
+++ b/svc.py
@@
-    return 'hi'
+    return 'hi'  # documented
"""

_BREAKING_PATCH = """--- a/svc.py
+++ b/svc.py
@@
-    return 'hi'
+    return 'BROKEN'
"""


def test_a_patch_that_breaks_your_tests_is_reverted_not_left_behind():
    """The whole point of T2. A patch proved it fixes what it claimed - in a
    sandbox, against its own reproducer. It proved nothing about everything
    else, and that is exactly what a user cannot eyeball."""
    repo = _repo()
    before = (repo / "svc.py").read_text()
    result = PatchApplier(repo).apply(_BREAKING_PATCH)
    assert result.applied is False, result.detail
    assert result.reverted is True, result.detail
    assert (repo / "svc.py").read_text() == before, "file was left modified"
    assert "REVERTED" in result.detail


def test_a_patch_your_tests_accept_stays_applied():
    repo = _repo()
    result = PatchApplier(repo).apply(_GOOD_PATCH)
    assert result.applied is True, result.detail
    assert result.reverted is False
    assert "documented" in (repo / "svc.py").read_text()
    assert result.suite, "no suite was run"


def test_revert_puts_the_file_back_after_a_successful_apply():
    """Applied and passing is still not permanent: the button that makes
    applying safe is the one that undoes it."""
    repo = _repo()
    original = (repo / "svc.py").read_text()
    applier = PatchApplier(repo)
    applied = applier.apply(_GOOD_PATCH)
    assert applied.applied is True
    back = applier.revert(applied.backup_path, applied.files)
    assert back.reverted is True, back.detail
    assert (repo / "svc.py").read_text() == original


def test_uncommitted_work_blocks_the_apply():
    """A user who cannot tell their own edits from ours can review neither,
    and `git checkout` would take both."""
    repo = _repo()
    (repo / "svc.py").write_text("def greet():\n    return 'my own edit'\n")
    result = PatchApplier(repo).apply(_GOOD_PATCH)
    assert result.applied is False
    assert "uncommitted" in result.detail
    assert "my own edit" in (repo / "svc.py").read_text()


def test_a_repo_with_no_suite_is_applied_but_says_nothing_checked_it():
    """Silence must not read as approval: no suite is not the same as a
    suite that passed."""
    repo = _repo(with_suite=False)
    result = PatchApplier(repo).apply(_GOOD_PATCH)
    assert result.applied is True, result.detail
    assert "no test suite" in result.detail
    assert result.suite == ""


def test_a_patch_naming_no_real_file_changes_nothing():
    repo = _repo()
    result = PatchApplier(repo).apply(
        "--- a/nope.py\n+++ b/nope.py\n@@\n-x\n+y\n")
    assert result.applied is False
    assert "names no file" in result.detail


def test_t0_and_t1_can_never_write_to_the_repo():
    """Autonomy is a tier, checked mechanically - not a model's confidence."""
    for tier in ("T0", "T1"):
        verdict = AutonomyGate(tier=tier).permits_apply()
        assert verdict.allowed is False, tier
        assert "never writes" in verdict.reason
    assert AutonomyGate(tier="T2").permits_apply().allowed is True


def test_merging_does_not_exist_at_any_tier():
    """T3 is absent by construction, not by configuration."""
    for tier in ("T0", "T1", "T2"):
        assert AutonomyGate(tier=tier).can_merge() is False, tier


def test_the_projects_own_suite_is_discovered_not_guessed():
    repo = _repo()
    found = find_suite(repo)
    assert found is not None
    assert found[0] in ("pytest", "unittest")
    assert dirty_files(repo) == []


def _monorepo() -> Path:
    """A monorepo shaped like paideia: a stub suite at the root, the REAL
    service suite one level down with its own venv and its own interpreter."""
    root = Path(tempfile.mkdtemp(prefix="aegis-mono-test-"))
    (root / "tests").mkdir()
    (root / "tests" / "test_stub.py").write_text(
        "import unittest\n"
        "class T(unittest.TestCase):\n"
        "    def test_broken(self):\n"
        "        raise ImportError('this stub has nothing to do with the service')\n")
    svc = root / "services" / "chatbot"
    (svc / "agents").mkdir(parents=True)
    (svc / "agents" / "orchestrator.py").write_text("def route():\n    return 'hi'\n")
    (svc / "tests").mkdir()
    (svc / "tests" / "test_route.py").write_text(
        "import sys, unittest; sys.path.insert(0, '.')\n"
        "from agents.orchestrator import route\n"
        "class T(unittest.TestCase):\n"
        "    def test_route(self):\n"
        "        self.assertEqual(route(), 'hi')\n")
    # The service's own venv, as a real monorepo has. Symlinked to whatever
    # interpreter is running the test so it is genuinely executable.
    venv_bin = svc / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python3").symlink_to(sys.executable)
    (svc / "venv" / "pyvenv.cfg").write_text("home = /usr\n")
    subprocess.run(["git", "init", "-q"], cwd=root, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=root, capture_output=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "base"], cwd=root, capture_output=True)
    return root


def test_the_suite_beside_the_patched_file_wins_over_a_root_stub():
    """The real failure this prevents: a patch to services/chatbot ran the
    monorepo ROOT's unrelated tests/ under Aegis's OWN 3.9 interpreter. Both
    root tests failed to import, and the user was told their suite "was
    ALREADY failing" - about a suite their project never runs."""
    repo = _monorepo()
    found = find_suite(repo, ["services/chatbot/agents/orchestrator.py"])
    assert found is not None
    _name, command, directory, python = found
    assert directory == repo / "services" / "chatbot", directory
    assert "services/chatbot/venv" in python, python
    assert command[0] == python, command


def test_the_suite_runs_under_the_projects_interpreter_not_aegiss():
    """Aegis runs in its own venv. A hardcoded "python3" is Aegis's python,
    which has none of the project's dependencies - and the import errors that
    follow are indistinguishable from a genuinely failing suite."""
    repo = _monorepo()
    found = find_suite(repo, ["services/chatbot/agents/orchestrator.py"])
    assert found is not None
    assert found[3] != "python3", "fell back to whatever python is on PATH"
    assert Path(found[3]).is_file()


def test_a_monorepo_patch_is_verified_by_the_right_suite_end_to_end():
    """The whole chain, not just discovery: a good patch to a service stays
    applied and reports the SERVICE's suite, never the root stub's."""
    repo = _monorepo()
    result = PatchApplier(repo).apply(
        "--- a/services/chatbot/agents/orchestrator.py\n"
        "+++ b/services/chatbot/agents/orchestrator.py\n"
        "@@\n"
        "-    return 'hi'\n"
        "+    return 'hi'  # documented\n")
    assert result.applied is True, result.detail
    assert result.reverted is False, result.detail
    assert "ALREADY failing" not in result.detail, result.detail
    assert "services/chatbot" in result.suite, result.suite


def _repo_with_costly_suite() -> Path:
    """A repo whose suite, if run, writes SPENT - standing in for the live LLM
    and database calls a real project's tests make."""
    root = Path(tempfile.mkdtemp(prefix="aegis-cost-test-"))
    (root / "svc.py").write_text("def greet():\n    raise ValueError('boom')\n")
    (root / "tests").mkdir()
    (root / "tests" / "test_costly.py").write_text(
        "import unittest, pathlib\n"
        "class T(unittest.TestCase):\n"
        "    def test_costly(self):\n"
        "        pathlib.Path('SPENT').write_text('money')\n")
    subprocess.run(["git", "init", "-q"], cwd=root, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=root, capture_output=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "base"], cwd=root, capture_output=True)
    return root


_FIXING_PATCH = ("--- a/svc.py\n+++ b/svc.py\n@@\n"
                 "-    raise ValueError('boom')\n+    return 'hi'\n")
_REPRODUCER = ("import sys; sys.path.insert(0,'.')\n"
               "from svc import greet\n"
               "assert greet() == 'hi', 'still broken'\n")


def test_the_reproducer_verifies_the_fix_without_spending_on_your_suite():
    """Verifying cost real money. paideia's 136 tests are five files of live
    LLM and SurrealDB calls, and apply() runs the suite TWICE (baseline, then
    patched) - so one click was ~18 minutes and ~124 LLM-backed tests, spent
    without the user being told. Aegis's own reproducer proves the same patch
    landed, for free."""
    repo = _repo_with_costly_suite()
    result = PatchApplier(repo).apply(_FIXING_PATCH, run_tests=False,
                                      reproducer=_REPRODUCER)
    assert result.applied is True, result.detail
    assert result.reverted is False
    assert not (repo / "SPENT").exists(), "the costly suite was run anyway"
    assert result.suite == "reproducer"
    assert "return 'hi'" in (repo / "svc.py").read_text()


def test_a_patch_its_own_reproducer_rejects_is_reverted():
    """Skipping the suite must not mean skipping verification. A patch that
    applies cleanly but does not actually fix the bug has to come back out -
    otherwise "free" would just mean "unchecked"."""
    repo = _repo_with_costly_suite()
    before = (repo / "svc.py").read_text()
    result = PatchApplier(repo).apply(
        "--- a/svc.py\n+++ b/svc.py\n@@\n"
        "-    raise ValueError('boom')\n+    raise ValueError('still boom')\n",
        run_tests=False, reproducer=_REPRODUCER)
    assert result.applied is False, result.detail
    assert result.reverted is True
    assert (repo / "svc.py").read_text() == before, "file left modified"
    assert "REVERTED" in result.detail


def test_aegis_never_leaves_its_test_file_in_your_repo():
    """The reproducer is Aegis's, not the project's. Leaving aegis_reproducer.py
    behind would show up in the user's next `git status` as a file they did not
    write."""
    repo = _repo_with_costly_suite()
    PatchApplier(repo).apply(_FIXING_PATCH, run_tests=False,
                             reproducer=_REPRODUCER)
    assert not (repo / "aegis_reproducer.py").exists()


def test_skipping_the_suite_says_so_rather_than_implying_all_is_well():
    """Silence must not read as approval: a free check on the patch is not a
    check on everything else, and the user has to be told which they got."""
    repo = _repo_with_costly_suite()
    result = PatchApplier(repo).apply(_FIXING_PATCH, run_tests=False,
                                      reproducer=_REPRODUCER)
    assert "NOT run" in result.detail, result.detail


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except AssertionError as exc:
                failures += 1
                print(f"  FAIL  {name}: {exc}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"  ERROR {name}: {exc.__class__.__name__}: {exc}")
    print(f"\n{'FAILED' if failures else 'All apply tests passed'}"
          f"{f' ({failures} failing)' if failures else ''}")
    sys.exit(1 if failures else 0)
