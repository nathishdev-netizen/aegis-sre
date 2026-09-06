"""TestRunner - sandboxed execution of a reproducer, before and after a patch.

The sandbox is a throwaway COPY of the repo: the real project directory is
never patched, never written, never even cd-ed into. Tests run with a
stripped environment (PATH only - no credentials can leak into a generated
test), a hard timeout, and their exit code is the only thing believed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

TIMEOUT_S = 60
# A repo bigger than this is not copied; the run is skipped and SAID to be
# skipped - a silent skip would let an unexecuted reproducer look verified.
MAX_REPO_BYTES = 50 * 1024 * 1024


@dataclass
class RunResult:
    executed: bool
    exit_code: int
    output: str
    detail: str = ""


def _tolerant_apply(work: Path, patch_text: str) -> tuple[bool, str]:
    """Apply simple line-replacement hunks by unique prefix match.

    Handles only the shape model patches usually take - N removed lines, N
    added lines - and requires each removed line to match exactly ONE line in
    the file (by either being a prefix of it or containing it). Anything
    ambiguous fails, because a fuzzy patch applied to the wrong line is worse
    than no patch.
    """
    current_file: Path | None = None
    minus: list[str] = []
    plus: list[str] = []

    def flush() -> str | None:
        nonlocal minus, plus
        if current_file is None or not minus:
            minus, plus = [], []
            return None
        if len(minus) != len(plus):
            return "hunk is not a 1:1 line replacement"
        try:
            lines = current_file.read_text(errors="replace").splitlines(keepends=True)
        except OSError:
            return f"cannot read {current_file.name}"
        for old_line, new_line in zip(minus, plus):
            wanted = old_line.strip()
            hits = [i for i, line in enumerate(lines)
                    if line.strip().startswith(wanted) or wanted in line]
            if len(hits) != 1:
                return f"{len(hits)} matches for {wanted[:40]!r} - not unique"
            indent = lines[hits[0]][: len(lines[hits[0]]) - len(lines[hits[0]].lstrip())]
            lines[hits[0]] = indent + new_line.strip() + "\n"
        current_file.write_text("".join(lines))
        minus, plus = [], []
        return None

    for raw in (patch_text or "").splitlines():
        if raw.startswith("+++ "):
            error = flush()
            if error:
                return False, error
            name = raw[4:].strip()
            name = name[2:] if name.startswith("b/") else name
            current_file = work / name
        elif raw.startswith("-") and not raw.startswith("---"):
            minus.append(raw[1:])
        elif raw.startswith("+") and not raw.startswith("+++"):
            plus.append(raw[1:])
        elif raw.startswith("@@"):
            error = flush()
            if error:
                return False, error
    error = flush()
    if error:
        return False, error
    return True, "applied tolerantly"


class TestRunner:
    def __init__(self, repo_path: str | Path) -> None:
        self.repo = Path(repo_path).resolve()

    def _repo_size(self) -> int:
        total = 0
        for path in self.repo.rglob("*"):
            if path.is_file() and ".git" not in path.parts:
                total += path.stat().st_size
                if total > MAX_REPO_BYTES:
                    return total
        return total

    def run(self, test_filename: str, test_content: str,
            patch_text: str = "") -> RunResult:
        if self._repo_size() > MAX_REPO_BYTES:
            return RunResult(False, -1, "", "repo exceeds sandbox copy limit - "
                                           "run the reproducer yourself")
        sandbox = Path(tempfile.mkdtemp(prefix="aegis-sandbox-"))
        try:
            work = sandbox / "repo"
            shutil.copytree(self.repo, work,
                            ignore=shutil.ignore_patterns(".git", "node_modules",
                                                          ".venv", "__pycache__"))
            if patch_text:
                patched = subprocess.run(
                    ["/usr/bin/patch", "-p1", "--forward", "--silent"],
                    input=patch_text, text=True, cwd=work,
                    capture_output=True, timeout=TIMEOUT_S)
                if patched.returncode != 0:
                    # Model diffs are notoriously loose with context: the
                    # first live run's hunk omitted a trailing comment on the
                    # '-' line and patch(1) rightly refused it. For simple
                    # line replacements, match tolerantly - and fail honestly
                    # when the match is not unique.
                    applied, detail = _tolerant_apply(work, patch_text)
                    if not applied:
                        return RunResult(False, patched.returncode,
                                         patched.stdout + patched.stderr,
                                         f"patch did not apply ({detail})")
            test_path = work / test_filename
            test_path.parent.mkdir(parents=True, exist_ok=True)
            test_path.write_text(test_content)
            completed = subprocess.run(
                ["python3", test_filename], cwd=work, text=True,
                capture_output=True, timeout=TIMEOUT_S,
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            )
            return RunResult(True, completed.returncode,
                             (completed.stdout + completed.stderr)[-2000:])
        except subprocess.TimeoutExpired:
            return RunResult(True, -1, "", f"timed out after {TIMEOUT_S}s")
        finally:
            shutil.rmtree(sandbox, ignore_errors=True)
