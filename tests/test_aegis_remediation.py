"""Phase 9 regression tests - C13 remediation at T0/T1.

The fake router returns canned model output, but the sandbox runs are REAL
subprocess executions against the fixture app - the reproduce-first protocol
is only worth testing if the tests actually execute code.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aegis.l8_action.gate import AutonomyGate  # noqa: E402
from aegis.l8_action.mapper import TraceToCodeMapper  # noqa: E402
from aegis.l8_action.remediate import RemediationAgent  # noqa: E402
from aegis.l8_action.runner import TestRunner, MAX_REPO_BYTES  # noqa: E402

SAMPLE_APP = Path(__file__).resolve().parent / "fixtures" / "sample_app"

FAILING_TEST = """```python
from app import acquire
acquire()
acquire()
print("pool handled normal concurrency")
```"""

PASSING_TEST = """```python
from app import acquire
acquire()
print("only one connection - passes even with the bug")
```"""

GOOD_PATCH = '''```diff
--- a/app.py
+++ b/app.py
@@ -6,7 +6,7 @@
 "tuned" down and now saturates under normal concurrency.
 """
 
-POOL_MAXSIZE = 1  # was 8 before the bad tune
+POOL_MAXSIZE = 8
 
 _active = 0
 
```'''

TEST_TOUCHING_PATCH = """```diff
--- a/tests/test_app.py
+++ b/tests/test_app.py
-assert x
+pass
```"""


class FakeRouter:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def chat(self, task, messages, purpose="", **kw):
        self.calls.append(task)
        return self.replies.pop(0) if self.replies else None


def _agent(replies, tier="T1"):
    import tempfile, aegis.l8_action.remediate as rem
    from aegis.l3_storage import store
    # proposals land in a temp AEGIS_HOME so tests never touch the real one
    rem.AEGIS_HOME = Path(tempfile.mkdtemp())
    return RemediationAgent(FakeRouter(replies), SAMPLE_APP, tier=tier,
                            project="svc-test")


INCIDENT = {"id": "INC-1", "evidence":
            ["ERROR worker: pool exhausted: 1 connections active"]}
HYPOTHESIS = {"statement": "POOL_MAXSIZE was tuned down and saturates"}


def _repo_digest() -> str:
    parts = []
    for path in sorted(SAMPLE_APP.rglob("*")):
        if path.is_file():
            parts.append(path.read_bytes())
    return hashlib.sha256(b"".join(parts)).hexdigest()


def test_the_full_loop_produces_a_draft():
    """incident -> failing test -> patch -> passing test -> DRAFT bundle."""
    agent = _agent([FAILING_TEST, GOOD_PATCH])
    proposal = agent.propose(INCIDENT, HYPOTHESIS)
    assert proposal.status == "draft", proposal.detail
    assert proposal.test_before["exit_code"] != 0
    assert proposal.test_after["exit_code"] == 0
    body = Path(proposal.bundle_path).read_text()
    assert "DRAFT - requires human review" in body
    assert "never merges" in body


def test_a_passing_reproducer_stops_everything():
    """The rule that separates useful from dangerous: if the reproducer
    passes before any fix, the diagnosis is wrong - no patch is attempted."""
    agent = _agent([PASSING_TEST, GOOD_PATCH])
    proposal = agent.propose(INCIDENT, HYPOTHESIS)
    assert proposal.status == "stopped"
    assert "diagnosis is wrong" in proposal.detail
    assert agent.router.calls == ["write_reproducer"], \
        "a patch was requested despite the failed protocol"


def test_the_target_repo_is_never_written():
    """The one promise: watching and fixing a project must leave its files
    byte-for-byte identical. Applying a fix is a human act."""
    before = _repo_digest()
    for replies in ([FAILING_TEST, GOOD_PATCH], [PASSING_TEST]):
        _agent(list(replies)).propose(INCIDENT, HYPOTHESIS)
    assert _repo_digest() == before, "remediation modified the target repo"


def test_t0_advises_and_calls_no_model_for_code():
    agent = _agent([FAILING_TEST, GOOD_PATCH], tier="T0")
    proposal = agent.propose(INCIDENT, HYPOTHESIS)
    assert proposal.status == "advise"
    assert agent.router.calls == [], "T0 must not spend code-writing calls"


def test_a_patch_touching_tests_is_blocked():
    agent = _agent([FAILING_TEST, TEST_TOUCHING_PATCH])
    proposal = agent.propose(INCIDENT, HYPOTHESIS)
    assert proposal.status == "blocked"
    assert "test" in proposal.detail


def test_a_patch_that_does_not_fix_is_not_proposed():
    useless = GOOD_PATCH.replace("POOL_MAXSIZE = 8", "POOL_MAXSIZE = 1  # still broken")
    agent = _agent([FAILING_TEST, useless])
    proposal = agent.propose(INCIDENT, HYPOTHESIS)
    assert proposal.status == "blocked"
    assert "did not make the reproducer pass" in proposal.detail


def test_unmappable_evidence_degrades_to_advice():
    agent = _agent([FAILING_TEST, GOOD_PATCH])
    proposal = agent.propose(
        {"id": "INC-2", "evidence": ["something with no source anywhere"]},
        HYPOTHESIS)
    assert proposal.status == "advise"
    assert agent.router.calls == [], "code calls spent with nothing mapped"


def test_merge_capability_does_not_exist():
    """T2 (apply to the working tree, revertibly) now exists. T3 - opening a
    PR, or merging one - still does not, at any tier and at any confidence.
    can_merge() is the documentation of that, and it is checked in code
    rather than asked of a prompt."""
    for tier in ("T0", "T1", "T2"):
        assert AutonomyGate(tier).can_merge() is False, tier
    try:
        AutonomyGate("T3")
        raise AssertionError("T3 was accepted - merging must not exist")
    except ValueError:
        pass


def test_only_t2_may_write_to_the_working_tree():
    """The tiers below T2 draft a patch and stop. Writing is not something a
    model can reach by being confident - it is a different tier, and the
    server only constructs T2 from an explicit human action."""
    for tier in ("T0", "T1"):
        assert AutonomyGate(tier).permits_apply().allowed is False, tier
    assert AutonomyGate("T2").permits_apply().allowed is True


def test_gate_blocks_infrastructure_and_dependencies():
    gate = AutonomyGate("T1")
    for name in ("requirements.txt", ".github/workflows/ci.yml", ".env",
                 "package.json", "Dockerfile"):
        diff = f"--- a/{name}\n+++ b/{name}\n-a\n+b\n"
        assert not gate.validate_patch(diff, [name]).allowed, name


def test_gate_blocks_oversize_patches():
    body = "\n".join("+new line" for _ in range(50))
    diff = f"--- a/app.py\n+++ b/app.py\n{body}\n"
    verdict = AutonomyGate("T1").validate_patch(diff, ["app.py"])
    assert not verdict.allowed and "minimal" in verdict.reason


def test_mapper_finds_the_raising_line():
    locations = TraceToCodeMapper(SAMPLE_APP).locate(
        ["ERROR worker: pool exhausted: 1 connections active"])
    assert any(l.file == "app.py" and "RuntimeError" in l.source
               for l in locations)


def test_mapper_finds_a_message_split_across_two_source_lines():
    """A real failure against paideia-chatbot: the logger call splits a long
    message into two adjacent string literals -
        logger.warning(
            "[orchestrator] OUT OF SCOPE REFUSAL - question graded out of scope, "
            "research pipeline skipped entirely"
        )
    - which Python concatenates with no separator, but which the evidence line
    (as actually logged) reports as one continuous sentence. The mapper used
    to check each source line alone and never found it."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        app = Path(tmpdir) / "orchestrator.py"
        app.write_text(
            'def out_of_scope():\n'
            '    logger.warning(\n'
            '        "[orchestrator] OUT OF SCOPE REFUSAL - question graded out of scope, "\n'
            '        "research pipeline skipped entirely"\n'
            '    )\n'
        )
        locations = TraceToCodeMapper(tmpdir).locate([
            "[orchestrator] OUT OF SCOPE REFUSAL - question graded out of scope, "
            "research pipeline skipped entirely"
        ])
        assert any(l.file == "orchestrator.py" and l.line == 3 for l in locations), \
            "a message split across two adjacent string literals was not found"


def test_mapper_ignores_an_interpolated_exception_repr():
    """A second real failure: logger.warning(f"connection lost ({exc!r}) --
    reconnecting") logs "connection lost (TimeoutError()) -- reconnecting" at
    runtime - "TimeoutError()" exists nowhere in the source, which says
    "{exc!r}". A fragment built from the logged text used to search for the
    runtime value itself and never find the real call site."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Path(tmpdir) / "db.py"
        db.write_text(
            'def reconnect(exc):\n'
            '    logger.warning(f"[chatbot_db] connection lost ({exc!r}) -- '
            'reconnecting and retrying once")\n'
        )
        locations = TraceToCodeMapper(tmpdir).locate([
            "[chatbot_db] connection lost (TimeoutError()) -- reconnecting and retrying once"
        ])
        assert any(l.file == "db.py" and l.line == 2 for l in locations), \
            "an interpolated exception repr blocked mapping to the real logger call"


def test_repo_size_excludes_an_embedded_database_directory():
    """A real project's SurrealDB/RocksDB-style storage directory (a *.db
    directory holding gigabytes of wal/manifest/sstables files, not source)
    must not count toward the sandbox copy limit - it was pushing a project
    whose actual code is a few megabytes past the limit entirely, and
    Propose fix never even reached the reproducer step."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir)
        (repo / "app.py").write_text("def handler():\n    return 1\n")
        db_dir = repo / "services" / "ingest" / "data" / "project.db"
        db_dir.mkdir(parents=True)
        # One file well past MAX_REPO_BYTES on its own - if the size check
        # does not exclude the .db directory, this alone fails the repo.
        (db_dir / "vlog").write_bytes(b"0" * (MAX_REPO_BYTES + 1024))

        size = TestRunner(repo)._repo_size()
        assert size < MAX_REPO_BYTES, \
            f"embedded database directory was counted toward the repo size ({size} bytes)"


def test_repo_size_and_copy_use_the_same_exclusions():
    """The size PRE-CHECK and the actual copytree() call used to have two
    separate, drifted exclusion lists - a repo that would have copied fine
    (copytree already skipped .venv/node_modules) was rejected by a size
    check that counted them anyway. Both must exclude the same things."""
    from aegis.l8_action.runner import SKIP_DIR_NAMES, SKIP_DIR_SUFFIXES
    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir)
        (repo / "app.py").write_text("def handler():\n    return 1\n")
        for name in SKIP_DIR_NAMES:
            bulky = repo / name
            bulky.mkdir(exist_ok=True)
            (bulky / "big.bin").write_bytes(b"0" * (MAX_REPO_BYTES // 4))
        for suffix in SKIP_DIR_SUFFIXES:
            bulky = repo / f"data{suffix}"
            bulky.mkdir(exist_ok=True)
            (bulky / "big.bin").write_bytes(b"0" * (MAX_REPO_BYTES // 4))

        size = TestRunner(repo)._repo_size()
        assert size < 1024, \
            f"a skip-listed directory was still counted toward the repo size ({size} bytes)"


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
    print(f"\n{'FAILED' if failures else 'All remediation tests passed'}"
          f"{f' ({failures} failing)' if failures else ''}")
    sys.exit(1 if failures else 0)
