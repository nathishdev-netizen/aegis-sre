"""What should have been logged, so the next incident can be explained.

The highest-leverage thing this system can do is not reason harder over the
evidence it has - it is notice what is missing. 26.9% of root-cause failures in
the published benchmarks were caused by evidence that was never captured at
all, and no amount of model quality recovers a line that was never written.

Today's case, exactly: a question needing both workers killed the process, and
the log's last line was the two workers choosing their tools. Nothing recorded
that _parallel_workers_node was entered, nothing recorded it returning. The
process died in between and left no trace, so there was nothing to map, nothing
to reproduce, and Propose fix correctly refused. A single log line either side
of that call would have turned "it hangs, no idea" into "it never returned".

A gap report is only useful if it is specific enough to act on, so every gap
names a file and a line from the analysed code map. "Add more logging" is not a
gap report; "add a line at orchestrator.py:1974, before the gather" is.

This runs when Aegis FAILS to explain something - not on a schedule. A gap is
evidence that would have answered a question that actually got asked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Gap:
    """One place a log line would have answered a question that was asked."""

    file: str
    line: int
    function: str
    why: str          # what could not be answered without it
    suggestion: str   # what to log there

    def to_dict(self) -> dict[str, Any]:
        return {"file": self.file, "line": self.line, "function": self.function,
                "why": self.why, "suggestion": self.suggestion}


@dataclass
class GapReport:
    question: str               # what Aegis could not answer
    gaps: list[Gap] = field(default_factory=list)
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"question": self.question, "detail": self.detail,
                "gaps": [g.to_dict() for g in self.gaps]}


def _last_logged(evidence: list[str], log_statements: list[dict]) -> dict | None:
    """The code site that wrote the LAST line before the trail went cold.

    Matched on the static part of the format string, because the logged line
    carries substituted values the source never contains: "Reranked 22
    candidates ... in 18076ms" comes from "Reranked %d candidates ... in %dms".
    Splitting on the placeholder and keeping the longest literal run is enough
    to identify the site without pretending to parse printf.
    """
    if not evidence or not log_statements:
        return None
    tail = str(evidence[-1])
    best, best_len = None, 0
    for statement in log_statements:
        text = str(statement.get("text") or "")
        if not text:
            continue
        for fragment in _literals(text):
            if len(fragment) > best_len and fragment in tail:
                best, best_len = statement, len(fragment)
    return best


def _literals(pattern: str, minimum: int = 8) -> list[str]:
    """The fixed runs of a format string, longest first."""
    out: list[str] = []
    for chunk in pattern.replace("{}", "%s").split("%"):
        piece = chunk[1:] if chunk[:1] in "sdrf" else chunk
        piece = piece.strip()
        if len(piece) >= minimum:
            out.append(piece)
    return sorted(out, key=len, reverse=True)


def _next_call_after(site: dict, calls: list[dict]) -> dict | None:
    """The first external call after that site, in the same file.

    This is where the trail most likely ends: something was logged, then a call
    was made that says nothing about itself, and the next thing anyone knows is
    that the run is over. That call is the gap.
    """
    if not site:
        return None
    file, line = site.get("file"), site.get("line") or 0
    after = [c for c in calls
             if c.get("file") == file and (c.get("line") or 0) > line]
    return min(after, key=lambda c: c.get("line") or 0) if after else None


def generate(question: str, evidence: list[str],
             code: dict[str, Any] | None) -> GapReport:
    """What logging would have answered `question`, given how this run ended.

    Needs the analysed code map: without it there is no file and no line, and a
    gap report that cannot name one is just an opinion. Says so rather than
    guessing.
    """
    if not code:
        return GapReport(question, detail=(
            "Analyze this project first - without the code map there is no "
            "file or line to point at, and 'add more logging' is not an answer."))

    statements = list(code.get("log_statements") or [])
    calls = list(code.get("external_calls") or [])
    report = GapReport(question)

    site = _last_logged(evidence, statements)
    if site is None:
        report.detail = (
            "The last line before the trail went cold does not match any log "
            "statement in this repo - it may come from a library, or from code "
            "that has not been analysed.")
        return report

    where = f"{site.get('file')}:{site.get('line')}"
    report.detail = (f"The trail ends at {where}, in "
                     f"{site.get('function') or 'this code'}.")

    # Is anything logged AFTER that point in the same function? A run that
    # stops here left no "done" line, and that absence is the gap - whether
    # what it called was an external service or this project's own helper.
    # Looking only at external calls missed today's case entirely: line 54
    # logs, line 55 awaits search_and_rerank(), the process dies inside it,
    # and nothing says the call was ever entered or left.
    same_function = [st for st in statements
                     if st.get("file") == site.get("file")
                     and st.get("function") == site.get("function")
                     and (st.get("line") or 0) > (site.get("line") or 0)]
    if not same_function:
        report.gaps.append(Gap(
            file=str(site.get("file")), line=int(site.get("line") or 0) + 1,
            function=str(site.get("function") or ""),
            why=("This is the last thing logged in this function. Whatever it "
                 "calls next says nothing about itself, so a run that stops "
                 "there cannot be told from one that never got there."),
            suggestion=("Log one line when this function finishes, with how "
                        "long it took. A run that goes quiet then says WHERE "
                        "it went quiet."),
        ))

    nxt = _next_call_after(site, calls)
    if nxt is not None:
        target = str(nxt.get("target") or "this call")
        report.gaps.append(Gap(
            file=str(nxt.get("file")), line=int(nxt.get("line") or 0),
            function=str(nxt.get("function") or ""),
            why=(f"Nothing is logged around this call, so when a run stops "
                 f"here there is no way to tell whether it was entered, "
                 f"returned, or died inside."),
            suggestion=(f"Log one line before and one after ({target}), "
                        f"including how long it took. Then a run that stops "
                        f"here says so instead of going silent."),
        ))
        if not nxt.get("has_timeout"):
            report.gaps.append(Gap(
                file=str(nxt.get("file")), line=int(nxt.get("line") or 0),
                function=str(nxt.get("function") or ""),
                why="This call has no timeout, so a hang here is indefinite.",
                suggestion=("Give it a timeout. A call that fails loudly after "
                            "N seconds is diagnosable; one that waits forever "
                            "is not."),
            ))

    if not report.gaps:
        report.detail += (" Every call after it is already logged - the "
                          "evidence for this one is somewhere else.")
    return report
