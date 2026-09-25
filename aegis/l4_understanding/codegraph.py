"""A read-only bridge to a CodeGraph index, when one exists.

Our own AST analysis (codebase.py) knows Python structure exactly. CodeGraph
knows thirty languages and, more usefully, the CALL PATHS our flat analysis
does not: who calls whom, across files and across dynamic dispatch. It runs
as an MCP server over stdio - the same wire our Opik client already speaks -
started inside the repo it indexes.

Everything here is best-effort and read-only:

  - if the CLI is not installed, or the repo has no .codegraph index, this
    reports absent and every caller falls back to the AST facts. An
    enhancement may never become a dependency (the P8 rule).
  - Aegis NEVER builds the index. `codegraph init` writes .codegraph/ INTO
    the project directory, and the isolation rule says Aegis writes only
    under ~/.aegis. Building the graph is the user's explicit act, in the
    UI, performed by the CodeGraph CLI - not something this module does.

So this file only ever READS a graph someone else chose to build.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# Where the CLI keeps its index, and how its server is launched. Read off the
# project's own docs, not guessed: `codegraph serve --mcp` over stdio.
_INDEX_DIR = ".codegraph"
_SERVE_COMMAND = ["codegraph", "serve", "--mcp"]
_EXPLORE_TOOL = "codegraph_explore"


def index_present(repo_path: str | Path) -> bool:
    """Whether this repo has a graph to read. Cheap - just a directory check."""
    try:
        return (Path(repo_path).expanduser() / _INDEX_DIR).is_dir()
    except OSError:
        return False


def cli_available() -> bool:
    """Whether the CodeGraph CLI can be launched at all."""
    from aegis.l1_ingestion.providers import McpClient
    return McpClient.resolve("codegraph") is not None


def status(repo_path: str | Path) -> dict[str, Any]:
    """What the UI shows on the Analyze card, in three honest states."""
    if not cli_available():
        return {"state": "no_cli",
                "detail": "CodeGraph CLI not installed - code graph features off. "
                          "Install it to add cross-language call paths."}
    if not index_present(repo_path):
        return {"state": "no_index",
                "detail": "no code graph in this repo yet. Building writes a "
                          ".codegraph/ folder INTO the repo (one time)."}
    return {"state": "ready",
            "detail": "code graph connected - call paths available to "
                      "Investigate and Propose fix."}


class CodeGraph:
    """One short-lived connection to a repo's graph. Opened for a query,
    closed after - a long-lived server per project is not worth the process."""

    def __init__(self, repo_path: str | Path) -> None:
        self.repo = str(Path(repo_path).expanduser())

    def explore(self, question: str, timeout_s: float = 30.0) -> str:
        """Ask the graph one question. Returns its text, or a plain reason
        it could not answer - never a fabricated one."""
        if not index_present(self.repo):
            return ("no code graph in this repo - build it from the Analyze "
                    "card, or rely on the AST facts")
        from aegis.l1_ingestion.providers import McpClient
        client = None
        try:
            client = McpClient(_SERVE_COMMAND, timeout_s=timeout_s, cwd=self.repo)
            tools = {t.get("name") for t in client.list_tools()}
            if _EXPLORE_TOOL not in tools:
                return (f"this CodeGraph build does not expose {_EXPLORE_TOOL} "
                        f"(has: {', '.join(sorted(tools)) or 'nothing'})")
            reply = client.call_tool(_EXPLORE_TOOL, {"query": str(question)})
            text = reply if isinstance(reply, str) else str(reply)
            return text[:4000] if text.strip() else "the graph returned nothing"
        except (RuntimeError, OSError, ValueError) as exc:
            return f"code graph unavailable: {exc.__class__.__name__}: {exc}"
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
