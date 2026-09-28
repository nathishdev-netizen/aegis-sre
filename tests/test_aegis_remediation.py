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
from aegis.l8_action.remediate import RemediationAgent, _asserts_the_bug  # noqa: E402
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


def test_a_traceback_frame_beats_searching_for_its_text():
    """A real failure against paideia-chatbot: the only evidence line was
    uvicorn's generic "Exception in ASGI application", which appears in no
    source file, so Propose fix reported "no evidence line maps to source"
    while the traceback naming api.py:276 sat one line below it. A frame
    states the file and line outright - believe it rather than searching."""
    evidence = (
        'ERROR:    Exception in ASGI application\n'
        f'  File "{SAMPLE_APP / "app.py"}", line 15, in acquire\n'
        '    raise RuntimeError(...)'
    )
    locations = TraceToCodeMapper(SAMPLE_APP).locate([evidence])
    assert any(l.file == "app.py" and l.line == 15 for l in locations), \
        "a traceback frame naming the repo's own file was not mapped"


def _apply(tmpdir, before: str, patch: str, **kw):
    """Write `before` into a repo copy, apply `patch`, return (ok, detail, text)."""
    from aegis.l8_action.runner import _tolerant_apply
    root = Path(tmpdir)
    target = root / "svc" / "mod.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(before)
    ok, detail = _tolerant_apply(root, patch, **kw)
    return ok, detail, target.read_text()


def test_a_patch_that_only_deletes_lines_is_applied():
    """The applier refused any hunk removing more lines than it added, so a
    fix whose whole point is taking a wrong guard OUT never got applied - and
    the run then reported "the patch did not make the reproducer pass", which
    was true only because the reproducer had been re-run against untouched
    code. Seen live on paideia-chatbot: an 11-remove / 9-add hunk."""
    import tempfile
    before = (
        "def f(req):\n"
        "    if not req.lead:\n"
        "        raise ValueError('nope')\n"
        "    return req.lead\n"
    )
    patch = (
        "--- a/svc/mod.py\n+++ b/svc/mod.py\n@@\n"
        "-    if not req.lead:\n"
        "-        raise ValueError('nope')\n"
        "     return req.lead\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        ok, detail, text = _apply(tmp, before, patch)
    assert ok, f"a deletion-only hunk was refused: {detail}"
    assert "raise ValueError" not in text, "the guard was not removed"
    assert "return req.lead" in text, "the surviving line was lost"


def test_a_replaced_block_keeps_the_patchs_own_indentation():
    """Reusing each TARGET line's indent is right for a one-line fix but wrong
    for a block: every line inherits the indent of whatever it landed on, and
    a block spanning nesting levels comes out as invalid Python. The
    reproducer then dies on a SyntaxError (exit 2) and the patch is blamed."""
    import tempfile
    before = (
        "def f(req):\n"
        "    if not req.lead:\n"
        "        raise ValueError('nope')\n"
        "    if req.lead:\n"
        "        return req.lead\n"
        "    return req.session\n"
    )
    patch = (
        "--- a/svc/mod.py\n+++ b/svc/mod.py\n@@\n"
        "-    if not req.lead:\n"
        "-        raise ValueError('nope')\n"
        "-    if req.lead:\n"
        "-        return req.lead\n"
        "+    if req.lead:\n"
        "+        return req.lead\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        ok, detail, text = _apply(tmp, before, patch)
    assert ok, f"block replacement refused: {detail}"
    compile(text, "mod.py", "exec")  # raises SyntaxError if indentation broke
    assert "raise ValueError" not in text


def test_inserted_lines_keep_the_patchs_own_indentation():
    """Seen live: a guard inserted after `async def chat(...)` anchored on a
    line at column 0, so every inserted line landed at column 0 too - inside
    a body needing four spaces. The run reported "the reproducer could not
    run after the patch (IndentationError)" and blamed the patch."""
    import tempfile
    before = (
        "def handler(request):\n"
        "    return request.value\n"
    )
    patch = (
        "--- a/svc/mod.py\n+++ b/svc/mod.py\n@@\n"
        "-def handler(request):\n"
        "+def handler(request):\n"
        "+    if not hasattr(request, 'value'):\n"
        "+        request.value = None\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        ok, detail, text = _apply(tmp, before, patch)
    assert ok, f"insertion refused: {detail}"
    compile(text, "mod.py", "exec")  # raises IndentationError if it regressed
    assert "    if not hasattr" in text, "the inserted guard lost its indentation"


def test_a_removed_block_is_matched_whole_not_line_by_line():
    """A five-line raise was refused because its closing ")" alone matched 124
    places in the file. As a contiguous BLOCK it occurs exactly once."""
    import tempfile
    before = (
        "def a():\n"
        "    g(\n"
        "        1\n"
        "    )\n"
        "def b():\n"
        "    h(\n"
        "        2\n"
        "    )\n"
    )
    patch = (
        "--- a/svc/mod.py\n+++ b/svc/mod.py\n@@\n"
        "-    h(\n"
        "-        2\n"
        "-    )\n"
        "+    pass\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        ok, detail, text = _apply(tmp, before, patch)
    assert ok, f"an unambiguous block was refused: {detail}"
    assert "h(" not in text and "g(" in text, "the wrong block was replaced"


def test_the_prompts_carry_no_stray_format_placeholders():
    """Both prompts go through str.format(), so any brace in their PROSE is a
    placeholder. Writing an f-string example into the guidance - `f"lead:{x}"`
    - turned every real propose() call into KeyError: 'x'. The prompts are only
    formatted at call time, so nothing catches this until a live run fails."""
    from aegis.l8_action.remediate import _PATCH_PROMPT, _REPRODUCER_PROMPT

    _REPRODUCER_PROMPT.format(
        diagnosis="d", evidence="e", locations="l", excerpts="x",
        import_root="r", import_file="f", import_stmt="s")
    _PATCH_PROMPT.format(
        diagnosis="d", locations="l", excerpts="e", reproducer="r", max_lines=12)


def test_overlap_walks_a_logged_line_back_to_its_format_string():
    """_overlap went missing in the repo recovery while two callers still
    imported it (server.get_code_for, remediate._code_context). Live, that
    surfaced as `tool failed: ImportError: cannot import name '_overlap'`
    inside an Investigate run, which the model then reported as the
    INCIDENT's cause - a broken tool presented as a diagnosis."""
    from aegis.l4_understanding.flowspec import _overlap

    logged = "[orchestrator] Step 4: generate request_id=abc123"
    statement = 'f"[orchestrator] Step {n}: {name} request_id={rid}"'
    assert _overlap(logged, statement) >= 0.6, "the real call site was not matched"

    unrelated = 'f"[vector_tools] Hybrid search returned {n} candidates"'
    assert _overlap(logged, unrelated) < 0.5, "an unrelated statement matched"

    assert _overlap("anything", "") == 0.0, "an empty statement must not match"


def test_a_reproducer_that_asserts_the_bug_is_named_as_such():
    """The model wrote this against paideia-chatbot: it catches the ValueError
    the bug raises and prints SUCCESS, so it exits 0 WHILE the bug is present
    and would fail once fixed - backwards. It still stops (the reproducer
    passed), but the reader must be told the test is inverted, not that their
    diagnosis was wrong."""
    inverted = (
        "from api import _memory_key\n"
        "try:\n"
        "    _memory_key(DummyRequest())\n"
        "except ValueError as e:\n"
        '    assert "memory key unavailable" in str(e)\n'
        '    print("SUCCESS")\n'
    )
    assert _asserts_the_bug(inverted)


def test_a_correct_reproducer_is_not_flagged_as_inverted():
    """Asserting the FIXED behaviour plainly must never trip the check."""
    correct = (
        "from api import _memory_key\n"
        "class R:\n"
        "    lead_id = None\n"
        "    session_id = 'sess'\n"
        "assert _memory_key(R()) == 'sess'\n"
        "print('ok')\n"
    )
    assert not _asserts_the_bug(correct)


def test_catching_to_clean_up_is_not_asserting_the_bug():
    """A reproducer may legitimately catch something and then assert on state
    afterwards - what makes the bad shape bad is that catching IS the success
    path, not that an except block exists at all."""
    legitimate = (
        "try:\n"
        "    risky()\n"
        "except ValueError:\n"
        "    cleanup()\n"
        "assert state.is_consistent()\n"
    )
    assert not _asserts_the_bug(legitimate)


def test_a_vendor_frame_is_never_offered_as_a_fix_site():
    """Frames inside site-packages belong to somebody else's code - a patch
    there would be proposed against a dependency, not this project."""
    evidence = (
        'ERROR\n'
        '  File "/x/venv/lib/python3.11/site-packages/fastapi/routing.py", line 604, in _producer'
    )
    assert TraceToCodeMapper(SAMPLE_APP).locate([evidence]) == []


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
