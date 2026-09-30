"""TestRunner - sandboxed execution of a reproducer, before and after a patch.

The sandbox is a throwaway COPY of the repo: the real project directory is
never patched, never written, never even cd-ed into. Tests run with a
stripped environment (PATH only - no credentials can leak into a generated
test), a hard timeout, and their exit code is the only thing believed.
"""

from __future__ import annotations

import ast
import re
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

TIMEOUT_S = 60
# A repo bigger than this is not copied; the run is skipped and SAID to be
# skipped - a silent skip would let an unexecuted reproducer look verified.
# A real multi-service repo's actual SOURCE routinely lands in the tens of
# megabytes once dependency trees, VCS metadata and embedded databases are
# excluded (see SKIP_DIR_NAMES/SKIP_DIR_SUFFIXES below) - incidental files
# that happen to sit in the repo (exported trace JSON, screenshots) can push
# a genuinely small codebase a few MB past a tighter limit for no reason
# related to whether the reproducer can run.
MAX_REPO_BYTES = 150 * 1024 * 1024

# Directory names never worth copying into a sandbox meant to run one Python
# (or similar) reproducer script - dependency trees, build output, VCS
# metadata, and embedded database storage. A real project's SurrealDB/
# RocksDB/LevelDB data directory is routinely gigabytes and irrelevant to
# whether a code fix makes a test pass; without this, a repo whose actual
# source is a few megabytes gets rejected outright by a size check that
# counted the database next to it. Both the size estimate below and the
# real copytree() call use this SAME list - previously they used different
# exclusions, so a repo that would have copied FINE (copytree already
# skipped .venv/node_modules) was rejected by a size check that did not.
SKIP_DIR_NAMES = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", ".codegraph",
    "dist", "build", ".tox", ".mypy_cache", ".pytest_cache",
}
# Directories identified by a suffix rather than an exact name - a database
# engine's own storage directory (SurrealDB, RocksDB and others commonly use
# a ".db" directory, not a single file) rather than a project convention.
SKIP_DIR_SUFFIXES = (".db",)


def _is_skippable_dir(path: Path) -> bool:
    return path.name in SKIP_DIR_NAMES or path.name.endswith(SKIP_DIR_SUFFIXES)
# How far from the diagnosed line a candidate match may sit and still be
# treated as "the one meant". Wide enough to survive a few lines of drift
# between when the incident was diagnosed and when the patch is written;
# narrow enough that two occurrences in DIFFERENT functions of a normal-
# sized file essentially never both fall inside it.
PROXIMITY_LINES = 15

_PATCHED_FILE = re.compile(r"^(?:---|\+\+\+)\s+(?:[ab]/)?(\S+)", re.M)


def _first_unparsable(work: Path, patch_text: str) -> tuple[str, str] | None:
    """The first patched Python file that no longer compiles, and why.

    Only files the patch touched, and only Python. A patch that corrupts a
    .py file is the tool's own bug and must never be reported as a test
    failure.
    """
    for name in _PATCHED_FILE.findall(patch_text or ""):
        if name == "/dev/null" or not name.endswith(".py"):
            continue
        target = work / name
        if not target.exists():
            continue
        try:
            ast.parse(target.read_text())
        except SyntaxError as exc:
            return name, f"{exc.__class__.__name__}: {exc.msg} (line {exc.lineno})"
        except (OSError, ValueError):
            continue
    return None




@dataclass
class RunResult:
    executed: bool
    exit_code: int
    output: str
    detail: str = ""


def _tolerant_apply(work: Path, patch_text: str, *,
                    near_line: int | None = None,
                    near_file: str = "") -> tuple[bool, str]:
    """Apply simple line-replacement hunks by unique prefix match.

    Handles the shapes model patches actually take: N removed lines with N
    added lines (1:1 replacement), or FEWER removed lines than added - a
    model routinely writes a one-line fix preceded by an explanatory
    comment, which is one "-" and several "+". Rejecting that outright, as
    a strict 1:1 checker does, means a correct one-line fix never even gets
    tested because it arrived with a comment attached.

    The safety property is unchanged either way: every removed line must
    still resolve to exactly ONE target line before anything is written.
    Extra added lines beyond the matched count are inserted immediately
    after the matched line, in order - never guessed at a second location.

    near_line/near_file: the 1-indexed line the DIAGNOSIS mapped to (not
    written by the model, so it cannot be gamed or mistyped by it). Real
    incidents keep landing on a removed line that is genuinely ambiguous by
    text alone - the same helper call copy-pasted into a sibling function -
    and four rounds of stricter prompt wording did not stop the model from
    writing the plain, ambiguous line anyway. Asking a human to disambiguate
    every time defeats the point of proposing a fix at all, so: when a
    removed line has multiple textual matches AND exactly one of them falls
    within PROXIMITY_LINES of the diagnosed line in the diagnosed file,
    THAT one is used. If two or more candidate matches are both near the
    hint, or there is no hint, this still refuses exactly as before - the
    hint only ever narrows a genuine tie to one, it never overrides an
    already-unique match and never picks among several equally-close ones.
    """
    current_file: Path | None = None
    minus: list[str] = []
    plus: list[str] = []

    def flush() -> str | None:
        nonlocal minus, plus
        if current_file is None or not minus:
            minus, plus = [], []
            return None
        try:
            lines = current_file.read_text(errors="replace").splitlines(keepends=True)
        except OSError:
            return f"cannot read {current_file.name}"
        file_matches_hint = bool(
            near_line and near_file
            and current_file.name == Path(near_file).name)
        # A multi-line removal is almost always one contiguous block, and as a
        # BLOCK it is far more identifiable than its lines are separately: a
        # real hunk here removed a five-line raise whose closing ")" alone
        # matched 124 places in the file, so line-by-line matching refused a
        # patch whose block occurs exactly once. Try the block first; fall
        # back to per-line matching when it is not contiguous.
        targets: list[int] = []
        stripped = [m.strip() for m in minus]
        if len(minus) > 1:
            starts = [
                i for i in range(len(lines) - len(minus) + 1)
                if all(lines[i + off].strip() == stripped[off]
                       for off in range(len(minus)))
            ]
            if len(starts) != 1 and file_matches_hint:
                near = [i for i in starts
                        if abs((i + 1) - near_line) <= PROXIMITY_LINES]
                if len(near) == 1:
                    starts = near
            if len(starts) == 1:
                targets = list(range(starts[0], starts[0] + len(minus)))

        for old_line in (minus if not targets else []):
            wanted = old_line.strip()
            hits = [i for i, line in enumerate(lines)
                    if line.strip().startswith(wanted) or wanted in line]
            if len(hits) != 1 and file_matches_hint:
                near = [i for i in hits
                        if abs((i + 1) - near_line) <= PROXIMITY_LINES]
                if len(near) == 1:
                    hits = near
            if len(hits) != 1:
                return f"{len(hits)} matches for {wanted[:40]!r} - not unique"
            targets.append(hits[0])
        # Pair each removed line with its replacement 1:1; any extra "+"
        # lines beyond that are new lines inserted after the last match, and
        # any removed line left over is a DELETION - the shape of a fix that
        # takes a wrong guard out. Rejecting those outright meant the patch
        # was never applied at all, and the run then reported "the patch did
        # not make the reproducer pass" - true, but only because the
        # reproducer had been re-run against untouched code.
        replacements = list(zip(targets, plus[:len(minus)]))
        extra = plus[len(minus):]
        deletions = targets[len(plus):]
        # Reusing the TARGET's indentation is right for a one-line fix, where
        # the model often writes the line with no leading whitespace at all.
        # It is wrong for a multi-line block: each replacement line would
        # inherit the indent of whatever line it happened to land on, which
        # for a block spanning several nesting levels produces text that is
        # not valid Python - the reproducer then dies on a syntax error
        # (exit 2) and the run reports the patch as not fixing anything. When
        # the patch carries its own indentation, that indentation IS the fix.
        block_replace = len(replacements) > 1
        for index, new_line in replacements:
            if block_replace and new_line[:1] in (" ", "\t"):
                lines[index] = new_line.rstrip("\n") + "\n"
            else:
                indent = lines[index][: len(lines[index]) - len(lines[index].lstrip())]
                lines[index] = indent + new_line.strip() + "\n"
        if extra and replacements:
            last_index = replacements[-1][0]
            indent = lines[last_index][: len(lines[last_index]) - len(lines[last_index].lstrip())]
            insert_at = last_index + 1
            for offset, new_line in enumerate(extra):
                # Reusing the ANCHOR's indentation is right when the model
                # wrote the line flush left, and wrong whenever the patch
                # carries its own. Inserting a guard after `async def chat(...)`
                # anchored on a line at column 0 put the whole block at column
                # 0 too, inside a body that needs four spaces - IndentationError
                # before the reproducer could run, blamed on the patch.
                if new_line[:1] in (" ", "\t"):
                    lines.insert(insert_at + offset, new_line.rstrip("\n") + "\n")
                else:
                    lines.insert(insert_at + offset, indent + new_line.strip() + "\n")
        # Highest index first, so earlier deletions do not shift later ones.
        for index in sorted(deletions, reverse=True):
            del lines[index]
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


_VENV_NAMES = (".venv", "venv", "env", ".virtualenv")
# Directories the last venv search walked, nearest-first.
_SEARCH: list = []


def _interpreter_for(repo: Path, hint: str = "") -> str:
    """The python that can actually import this project.

    A reproducer runs against the project's own code, so it needs the
    project's own dependencies. A bare "python3" has none of them, and the
    import error that follows looks exactly like a badly written
    reproducer - it is not, it is the wrong interpreter.

    A monorepo keeps its venv beside the SERVICE, not at the root:
    paideia's chatbot has services/chatbot/venv with fastapi in it while
    the root has nothing. Looking only at the top level told the user to
    create a virtualenv they already had. The hint is the file the
    evidence mapped to, so the search starts where the code being fixed
    actually lives and walks up from there.
    """
    starts = []
    if hint:
        here = (repo / hint).parent if not hint.startswith("/") else Path(hint).parent
        while here != here.parent and str(here).startswith(str(repo)):
            starts.append(here)
            here = here.parent
    starts.append(repo)
    _SEARCH.clear()
    _SEARCH.extend(starts)
    for base in starts:
        for name in _VENV_NAMES:
            for exe in ("python", "python3"):
                path = base / name / "bin" / exe
                if path.is_file():
                    return str(path)
    # Nothing beside the code: try any service venv in the repo, nearest
    # first, rather than giving up on a dependency that is installed.
    for cfg in sorted(repo.glob("*/*/*/pyvenv.cfg")) + sorted(repo.glob("*/*/pyvenv.cfg")):
        for exe in ("python", "python3"):
            path = cfg.parent / "bin" / exe
            if path.is_file():
                return str(path)
    return "python3"


def _import_root(work: Path, hint: str) -> Path:
    """Where this code expects to be imported from.

    The nearest ancestor of the mapped file that looks like a project root
    of its own - a pytest.ini, a pyproject.toml, or a venv beside it. For a
    single-package repo that is the repo; for a monorepo service it is the
    service directory, which is the whole difference between
    "ModuleNotFoundError: agents" and a reproducer that runs.
    """
    if not hint:
        return work
    here = (work / hint).parent
    while here != work.parent and str(here).startswith(str(work)):
        markers = ("pytest.ini", "pyproject.toml", "setup.py", "tox.ini")
        if any((here / m).is_file() for m in markers):
            return here
        if any((here / v / "bin").is_dir() for v in _VENV_NAMES):
            return here
        here = here.parent
    return work


def _dotted_module(repo: Path, hint: str) -> str:
    """The import statement the mapped file actually needs.

    A monorepo service is its own import root, so the same file is
    `services.chatbot.api` from the repo and `api` from the service. One
    formula, used both to tell the model how to spell its imports and to
    smoke-test that the import resolves before spending a call writing a
    reproducer against it.
    """
    if not hint:
        return ""
    root = _import_root(repo, hint)
    try:
        inside = str((repo / hint).relative_to(root))
    except (ValueError, OSError):
        inside = hint
    return inside[:-3].replace("/", ".") if inside.endswith(".py") else ""


class TestRunner:
    def __init__(self, repo_path: str | Path, hint: str = "",
                hint_line: int | None = None) -> None:
        self.repo = Path(repo_path).resolve()
        # Where the code under test lives, so the venv search starts there.
        self.hint = hint
        # The 1-indexed line the DIAGNOSIS mapped to in that file - from
        # facts, never from the patch the model writes. Lets the applier
        # break a textual tie the model itself cannot reliably avoid
        # creating (see PROXIMITY_LINES).
        self.hint_line = hint_line

    def _repo_size(self) -> int:
        # os.walk (not Path.rglob) because dirnames can be pruned IN PLACE -
        # rglob still descends into a skipped 3GB database directory just to
        # discard each file one at a time, which alone took several seconds
        # against a real project's SurrealDB storage.
        total = 0
        for dirpath, dirnames, filenames in os.walk(self.repo):
            dirnames[:] = [d for d in dirnames if not _is_skippable_dir(Path(d))]
            for name in filenames:
                try:
                    total += (Path(dirpath) / name).stat().st_size
                except OSError:
                    continue
                if total > MAX_REPO_BYTES:
                    return total
        return total

    def smoke_test(self) -> RunResult:
        """Can the discovered interpreter even import the target code?

        SWE-smith and Repo2Run both run this before spending a model call on
        a reproducer: a stale venv, a missing dependency, or a wrong import
        root produces the exact same ModuleNotFoundError as a badly written
        reproducer, and paying for an LLM call to discover that is a waste
        found too late. This finds it in under a second, in the same
        sandbox copy the real run will use, so a broken environment is
        caught here rather than blamed on the model afterwards.
        """
        module = _dotted_module(self.repo, self.hint)
        probe = (f"import {module}\nprint('ok')\n" if module
                 else "print('ok')\n")
        return self.run("aegis_smoke.py", probe)

    def run(self, test_filename: str, test_content: str,
            patch_text: str = "") -> RunResult:
        if self._repo_size() > MAX_REPO_BYTES:
            return RunResult(False, -1, "", "repo exceeds sandbox copy limit - "
                                           "run the reproducer yourself")
        sandbox = Path(tempfile.mkdtemp(prefix="aegis-sandbox-"))
        try:
            work = sandbox / "repo"
            # Sockets, fifos and device files cannot be copied - copytree
            # raises on the first one and the whole proposal dies with an
            # errno nobody can act on. CodeGraph leaves a daemon.sock in the
            # repo it indexes, so this is the common case, not an edge one.
            shutil.copytree(self.repo, work,
                            ignore=shutil.ignore_patterns(
                                *SKIP_DIR_NAMES,
                                *("*" + suffix for suffix in SKIP_DIR_SUFFIXES),
                                "*.sock", "*.pid", ".DS_Store"),
                            ignore_dangling_symlinks=True)
            if patch_text:
                patch_bin = shutil.which("patch")
                if patch_bin is None:
                    applied, detail = _tolerant_apply(
                        work, patch_text,
                        near_line=self.hint_line, near_file=self.hint)
                    if not applied:
                        return RunResult(False, -1, "",
                                         f"no patch tool and {detail}")
                    patch_text = ""  # applied; skip the subprocess path
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
                    applied, detail = _tolerant_apply(
                        work, patch_text,
                        near_line=self.hint_line, near_file=self.hint)
                    if not applied:
                        return RunResult(False, patched.returncode,
                                         patched.stdout + patched.stderr,
                                         f"patch did not apply ({detail})")
            # Does every file the patch touched still PARSE? patch(1) is happy
            # to leave a file that no longer compiles - one real patch removed
            # the only statement under a `try:` and re-added it nested a level
            # deeper, leaving the try with no body. Without this check the
            # breakage surfaced as the reproducer crashing, reported as
            # "possibly the patch removed something the reproducer still
            # needs" - which sends the reader to inspect a test that was fine.
            if patch_text:
                broken = _first_unparsable(work, patch_text)
                if broken:
                    return RunResult(
                        False, 1, broken[1],
                        f"the patch left {broken[0]} unparsable: {broken[1]}")
            # A monorepo service is usually its own import root: paideia's
            # chatbot holds agents/ and config/ beside api.py and a
            # pytest.ini of its own, so `from agents.orchestrator import x`
            # only resolves with services/chatbot on the path, never from
            # the repo root. Run the reproducer where its imports resolve.
            root = _import_root(work, self.hint)
            test_path = root / test_filename
            test_path.parent.mkdir(parents=True, exist_ok=True)
            test_path.write_text(test_content)
            # The interpreter is the real project's venv - the sandbox is a
            # copy, so that venv's sys.path still points at the ORIGINAL
            # tree. Without PYTHONPATH the reproducer either imports the
            # unpatched code (proving nothing) or fails to find it at all.
            # Pointing it at the copy is what makes before/after mean
            # anything.
            completed = subprocess.run(
                [_interpreter_for(self.repo, self.hint), test_filename],
                cwd=root, text=True,
                capture_output=True, timeout=TIMEOUT_S,
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                     # ONLY the import root. Adding the repo root as well
                     # looks harmless and is not: paideia has a top-level
                     # agents/ that shadows services/chatbot/agents/, so
                     # the reproducer resolved the wrong package and died
                     # on ModuleNotFoundError - with the right files sitting
                     # in the copy. Python already puts the script's own
                     # directory first; one path is the whole requirement.
                     "PYTHONPATH": str(root),
                     "PYTHONDONTWRITEBYTECODE": "1"},
            )
            return RunResult(True, completed.returncode,
                             (completed.stdout + completed.stderr)[-2000:])
        except subprocess.TimeoutExpired:
            return RunResult(True, -1, "", f"timed out after {TIMEOUT_S}s")
        finally:
            shutil.rmtree(sandbox, ignore_errors=True)
