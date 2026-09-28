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
