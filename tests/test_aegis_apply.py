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
        (root / "tests" / "test_svc.py").write_text(
            "import sys; sys.path.insert(0, '.')\n"
            "from svc import greet\n"
            "def test_greet():\n"
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
