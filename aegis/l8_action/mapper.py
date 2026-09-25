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


@dataclass
class Location:
    file: str          # repo-relative
    line: int
    source: str
    fragment: str


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
    text = re.sub(r"\b\d+(?:\.\d+)?\b", " ", text)               # numbers
    text = re.sub(r"https?://\S+", " ", text)
    pieces = re.split(r"[|:\[\]{}\"']+", text)
    fragments = []
    for piece in pieces:
        words = piece.split()
        if len(words) >= 3:
            fragments.append(" ".join(words))
    fragments.sort(key=len, reverse=True)
    return fragments[:4]


class TraceToCodeMapper:
    def __init__(self, repo_path: str | Path) -> None:
        self.repo = Path(repo_path).resolve()

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

    def locate(self, evidence_lines: list[str]) -> list[Location]:
        """Files and lines whose source contains an evidence fragment."""
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
            for number, source_line in enumerate(content.splitlines(), 1):
                for fragment in wanted:
                    if fragment.lower() in source_line.lower():
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
