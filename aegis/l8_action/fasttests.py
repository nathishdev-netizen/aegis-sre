"""Which of a project's tests can verify a patch for free.

A project suite is the broader check, but paideia's is 136 tests across six
files, five of which make live LLM or SurrealDB calls, and apply() runs it
TWICE (baseline, then patched) - one click was ~18 minutes and ~124 LLM-backed
tests. That is the user's money, spent to check Aegis's homework.

Most of those tests are not expensive. 29 of them run in 2.4 seconds against
stub models. The problem is telling which, and READING the code cannot do it:
scanning for ChatOpenAI/Surreal/CrossEncoder flagged 34 of 35 test classes as
costly, including one that runs 12 tests in 1.77s, because an import says
nothing about whether the thing is mocked.

Duration can. A test that finishes in 20ms made no network call - that is
physics, not inference. So: run the full suite ONCE, record every test's time
from pytest's own --durations output, and afterwards run only the fast ones.

The timing is a measurement, so it has to be re-measured when the tests change.
The cache is keyed on the fingerprint of the test files themselves; edit or add
a test and its timing is unknown again, which counts as NOT fast. A new test is
never silently skipped.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

# Above this, a test is assumed to have gone to the network, loaded a model, or
# hit a database. Deliberately generous: the point is to exclude what costs
# money, not to chase milliseconds, and a slow pure-CPU test costs only time.
FAST_S = 1.0

# pytest --durations lines: "0.02s call     tests/test_x.py::TestY::test_z"
_DURATION = re.compile(r"^\s*([\d.]+)s\s+(call|setup|teardown)\s+(\S+)\s*$", re.M)


def fingerprint(suite_dir: Path) -> str:
    """Identity of the test files, so timings are re-measured when they change.

    Name, size and mtime of every test file. A test that was edited, or one that
    did not exist when the timings were taken, has no timing - and no timing
    means not fast, so it falls back to the full suite rather than being
    quietly dropped from the check.
    """
    parts = []
    for path in sorted(suite_dir.rglob("test_*.py")):
        try:
            stat = path.stat()
        except OSError:
            continue
        parts.append(f"{path.relative_to(suite_dir)}:{stat.st_size}:{int(stat.st_mtime)}")
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


def parse_durations(output: str) -> dict[str, float]:
    """Per-test seconds from pytest's --durations output.

    Sums call+setup+teardown: a test whose fixture opens a database is expensive
    even when the call itself is instant, and charging it only for the call
    would let exactly the costly ones through.
    """
    totals: dict[str, float] = {}
    for seconds, _phase, node in _DURATION.findall(output or ""):
        try:
            totals[node] = totals.get(node, 0.0) + float(seconds)
        except ValueError:
            continue
    return totals


def fast_nodes(durations: dict[str, float], limit: float = FAST_S) -> list[str]:
    return sorted(node for node, seconds in durations.items() if seconds < limit)


def cache_path(repo: Path, suite_dir: Path) -> Path:
    """Under ~/.aegis, never in the user's repo.

    Aegis writes only under its own home - a timings file appearing in someone's
    `git status` is a file they did not write.
    """
    key = hashlib.sha256(str(suite_dir.resolve()).encode()).hexdigest()[:12]
    return Path.home() / ".aegis" / "timings" / f"{key}.json"


def load(repo: Path, suite_dir: Path) -> dict[str, float] | None:
    """Timings for THESE test files, or None if they have changed since."""
    path = cache_path(repo, suite_dir)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if data.get("fingerprint") != fingerprint(suite_dir):
        return None
    durations = data.get("durations")
    return durations if isinstance(durations, dict) else None


def save(repo: Path, suite_dir: Path, durations: dict[str, float]) -> None:
    path = cache_path(repo, suite_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "fingerprint": fingerprint(suite_dir),
            "durations": durations,
        }, indent=1))
    except OSError:
        pass  # a cache that cannot be written is a slow path, not a failure


def timing_command(command: tuple[str, ...]) -> tuple[str, ...]:
    """The suite command, asking pytest to report every test's duration."""
    return command + ("--durations=0", "--durations-min=0")


def fast_command(command: tuple[str, ...], nodes: list[str]) -> tuple[str, ...]:
    """Run exactly these test ids - no -k expression to mis-parse."""
    return command + tuple(nodes)


# -- relevance ---------------------------------------------------------------
#
# Cheapness is not the point; RELEVANCE is. Selecting purely by duration
# happily includes test_graph_tools and test_observability when the patch
# touched the orchestrator - 45 tests that pass whatever that patch did, which
# is false comfort, not a check. The question worth answering is "which tests
# would catch this if the patch were wrong", and only then "can we afford
# them".
#
# Module-level imports answer it without adding anything to the user's repo.
# pytest-cov would give line-level precision, but it has to RUN the full suite
# under coverage to build that map - the exact cost this exists to avoid - and
# it would mean installing a plugin into a project for Aegis's benefit.


def _module_names(repo: Path, changed: list[str]) -> set[str]:
    """Import names a changed file could be reached by.

    services/chatbot/agents/orchestrator.py yields {"orchestrator",
    "agents.orchestrator", "agents"} - the spellings a test would actually
    write, since a monorepo service is its own import root.
    """
    names: set[str] = set()
    for rel in changed:
        parts = Path(rel).with_suffix("").parts
        if not parts:
            continue
        names.add(parts[-1])
        for i in range(len(parts) - 1, -1, -1):
            tail = ".".join(parts[i:])
            if tail:
                names.add(tail)
            if len(parts) - i > 3:
                break
        if len(parts) > 1:
            names.add(parts[-2])
    return {n for n in names if n not in ("__init__", "tests", "test")}


def relevant_files(suite_dir: Path, repo: Path, changed: list[str]) -> list[str]:
    """Test files that import something the patch touched.

    Read from the test source, not guessed. A file that never mentions the
    changed module cannot fail because of it, so running it proves nothing
    about this patch.
    """
    if not changed:
        return []
    names = _module_names(repo, changed)
    if not names:
        return []
    hits = []
    for path in sorted(suite_dir.rglob("test_*.py")):
        try:
            text = path.read_text()
        except OSError:
            continue
        if any(re.search(rf"\b{re.escape(n)}\b", text) for n in names):
            # Relative to the suite dir, because that is how pytest spells node
            # ids and therefore how the timings are keyed: "tests/test_x.py::..."
            hits.append(str(path.relative_to(suite_dir)))
    return hits


def select(durations: dict[str, float], suite_dir: Path, repo: Path,
           changed: list[str], limit: float = FAST_S) -> tuple[list[str], str]:
    """The tests to run for THIS patch, and one line saying how they were chosen.

    Relevance first, cost second - in that order, because a cheap irrelevant
    test is worse than useless: it passes whatever the patch did and reads as
    verification.
    """
    files = relevant_files(suite_dir, repo, changed)
    if not files:
        return [], "no test file mentions the code this patch changed"
    chosen = sorted(
        node for node, seconds in durations.items()
        if seconds < limit and any(node.startswith(f) for f in files))
    if not chosen:
        return [], (f"{len(files)} test file(s) exercise this code, but none of "
                    "their tests are quick enough to run for free")
    return chosen, (f"{len(chosen)} test(s) from {len(files)} file(s) that "
                    "exercise the changed code")
