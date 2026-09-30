"""TraceToCodeMapper - from an evidence line back to the code that wrote it.

Deterministic and read-only: the literal fragments of a log line (what is
left after stripping the variable parts) are searched for in the repo's
source. The repo is never written to - a mapper with write access to the
project it diagnoses would violate the one promise this product makes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

SOURCE_SUFFIXES = {".py", ".js", ".ts", ".go", ".java", ".rb", ".rs", ".php"}
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist",
             "build", ".tox", ".mypy_cache"}
MAX_FILES = 2000
# A Python traceback frame, which names the file and line directly rather than
# quoting a format string that has to be searched for.
_TRACEBACK_FRAME = re.compile(r'File "([^"]+)", line (\d+)')


# Frames from the interpreter, a virtualenv or an installed package. EVERY
# traceback contains these and they are never the user's own code, so they
# must not be read as "the repository path is wrong".
_LIBRARY_MARKERS = (
    "/site-packages/", "/dist-packages/", "/lib/python", "/Cellar/",
    "/.venv/", "/venv/", "/node_modules/", "/Frameworks/Python.framework/",
)


def _is_library_path(path: str) -> bool:
    return any(marker in path for marker in _LIBRARY_MARKERS)


@dataclass
class Location:
    file: str          # repo-relative
    line: int
    source: str
    fragment: str


def _normalize_for_match(text: str) -> str:
    """Strip quote characters and collapse whitespace, lowercased.

    Adjacent string literals in source ("a" "b") concatenate with no separator
    at all - the closing quote of one touches the opening quote of the next -
    so comparing raw text (even joined with a space) never lines up with a
    fragment that spans the boundary. Stripping quotes and whitespace from
    both the source and the fragment before comparing is what makes them equal.
    """
    return re.sub(r"\s+", "", text.replace('"', "").replace("'", "")).lower()


def _fragments(evidence_line: str) -> list[str]:
    """The parts of a log line likely to appear verbatim in source code.

    Values, ids and placeholders vary per run; the fixed words around them are
    what the format string contains. Fragments under three words match half
    the repo and are dropped.
    """
    text = evidence_line
    text = re.sub(r"<[A-Z]+>", " ", text)                       # redaction marks
    text = re.sub(r"\b[0-9a-f]{8}-[0-9a-f-]{8,}\b", " ", text, flags=re.I)
    text = re.sub(r"\b\w+=[^\s]+", " ", text)                    # key=value
    # A repr() of a runtime object - {exc!r} in an f-string becomes
    # "(TimeoutError())" or "(ValueError('bad id'))" in the logged line, and
    # that exact text exists nowhere in the source: the source says "{exc!r}",
    # not the exception's actual class name, so deleting it must not leave the
    # words on either side touching - "connection lost (X) -- retrying" and
    # "connection lost -- retrying" are different strings, and the source still
    # has "({exc!r})" sitting between "lost" and "--". Treated as a split point,
    # like the { } and [ ] below, so each side is searched on its own.
    text = re.sub(r"\([A-Z]\w*\([^()]*\)\)", "|", text)
    text = re.sub(r"\b\d+(?:\.\d+)?\b", " ", text)               # numbers
    text = re.sub(r"https?://\S+", " ", text)
    pieces = re.split(r"[|:\[\]{}\"']+", text)
    fragments = []
    for piece in pieces:
        words = piece.split()
        cleaned = " ".join(words)
        # Colons chop log messages into short pieces, and a strict three-word
        # minimum dropped "pool exhausted" entirely - the mapper found nothing
        # for the doc's own worked example. Two words qualify when they are
        # long enough to be distinctive rather than half the repo.
        if len(words) >= 3 or (len(words) == 2 and len(cleaned) >= 12):
            fragments.append(cleaned)
    fragments.sort(key=len, reverse=True)
    return fragments[:4]


class TraceToCodeMapper:
    def __init__(self, repo_path: str | Path) -> None:
        self.repo = Path(repo_path).resolve()
        # Traceback frames pointing at source OUTSIDE this repo, kept so the
        # caller can say "the path looks wrong" instead of silently mapping
        # by text search.
        self.foreign_frames: list[str] = []

    def _source_files(self) -> list[Path]:
        found = []
        for path in self.repo.rglob("*"):
            if len(found) >= MAX_FILES:
                break
            if not path.is_file() or path.suffix not in SOURCE_SUFFIXES:
                continue
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            found.append(path)
        return found

    def foreign_frame_warning(self) -> str:
        """Why the traceback was unusable, if it named another tree entirely.

        Empty when frames were used, or when there were none to begin with.
        """
        if not self.foreign_frames:
            return ""
        sample = self.foreign_frames[0]
        return (f"the traceback names {len(self.foreign_frames)} file(s) "
                f"outside the repository path given - e.g. {sample} - so none "
                "of it could be used and the fix was mapped by text search "
                "instead. Check the repository path points at the checkout "
                "that produced these logs.")

    def _frame_locations(self, evidence_lines: list[str]) -> list[Location]:
        """Locations read straight off Python traceback frames.

        A frame already states the file and the line - searching the repo for
        text that looks like one is strictly worse than believing it. Text
        matching stays for log lines that merely QUOTE a format string; this
        path is for lines that name the code outright.
        """
        found: list[Location] = []
        seen: set[tuple[str, int]] = set()
        for raw in evidence_lines:
            for text in str(raw).splitlines():
                match = _TRACEBACK_FRAME.search(text)
                if not match:
                    continue
                path = Path(match.group(1))
                try:
                    relative = path.resolve().relative_to(self.repo)
                except ValueError:
                    # A frame outside this repo cannot be patched here. Worth
                    # REMEMBERING though: when the traceback names a project
                    # tree and the repo path is a different checkout of it,
                    # every frame is dropped and the mapper falls back to
                    # text-matching the log line - which lands on whatever
                    # file happens to contain that string, usually the
                    # `except` that logged it. Two attempts were spent that
                    # way before anyone could see the path was wrong.
                    if (path.suffix in SOURCE_SUFFIXES
                            and not _is_library_path(str(path))):
                        self.foreign_frames.append(str(path))
                    continue
                number = int(match.group(2))
                key = (str(relative), number)
                if key in seen:
                    continue
                seen.add(key)
                try:
                    lines = path.read_text(errors="replace").splitlines()
                    source = lines[number - 1].strip()[:160] if 0 < number <= len(lines) else ""
                except OSError:
                    source = ""
                found.append(Location(file=str(relative), line=number,
                                      source=source, fragment=text.strip()[:160]))
        # Deepest frame FIRST. A Python traceback prints outermost to
        # innermost, and the LAST frame is where the exception was actually
        # raised - but returning them in that order put the entry point at the
        # top, so a patch was written for api.py's `except Exception:` (the
        # code that LOGS the error) while the raise sat in orchestrator.py.
        # No patch to the handler can make the reproducer pass, so the whole
        # attempt was spent on the wrong file.
        found.reverse()
        return found

    def locate(self, evidence_lines: list[str]) -> list[Location]:
        """Files and lines whose source contains an evidence fragment."""
        # A traceback names the file and line outright; prefer it over guessing.
        frames = self._frame_locations(evidence_lines)
        if frames:
            return frames[:20]
        wanted = []
        for line in evidence_lines:
            wanted.extend(_fragments(line))
        if not wanted:
            return []
        locations: list[Location] = []
        seen: set[tuple[str, int]] = set()
        for path in self._source_files():
            try:
                content = path.read_text(errors="replace")
            except OSError:
                continue
            lines = content.splitlines()
            for number, source_line in enumerate(lines, 1):
                # A long log message is often split across two physical source
                # lines as adjacent string literals ("...part one, "\n"part
                # two"), which Python concatenates with NO separator at all -
                # the closing quote of one butts directly against the opening
                # quote of the next. A fragment spanning both halves can never
                # match either line alone, or even the two lines joined with a
                # space: stripping quote characters and collapsing whitespace
                # from both sides is what makes "scope, " + "research" line up
                # with the fragment "scope, research" the same way Python's own
                # concatenation does.
                next_line = lines[number] if number < len(lines) else ""
                joined = _normalize_for_match(source_line + next_line)
                for fragment in wanted:
                    normalized_fragment = _normalize_for_match(fragment)
                    if fragment.lower() in source_line.lower() or \
                       normalized_fragment in joined:
                        key = (str(path), number)
                        if key not in seen:
                            seen.add(key)
                            locations.append(Location(
                                file=str(path.relative_to(self.repo)),
                                line=number,
                                source=source_line.strip()[:160],
                                fragment=fragment,
                            ))
        return locations[:20]
