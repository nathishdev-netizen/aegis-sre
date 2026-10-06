"""Console entry points - how the product installs and starts.

    pip install .           # then:
    aegis                   # the app (v1 UI + Aegis intelligence)
    aegis --version
    aegis-mcp <logfile> # the MCP server, for `claude mcp add`
"""

from __future__ import annotations

import sys


def _version() -> str:
    try:
        import aegis
        return aegis.__version__
    except Exception:
        return "unknown"


def main() -> int:
    if "--version" in sys.argv or "-V" in sys.argv:
        print(f"log-intelligence-agent {_version()}")
        return 0
    if "--help" in sys.argv or "-h" in sys.argv:
        print("aegis [--version]  - start the Log Intelligence app "
              "(LOG_AGENT_PORT to change the port, default 3000)")
        return 0
    from app.server import serve  # imported late: --version must not boot anything
    serve()
    return 0


def mcp_main() -> int:
    if "--version" in sys.argv or "-V" in sys.argv:
        print(f"log-intelligence-agent {_version()}")
        return 0
    from aegis.mcp_server import main as run
    return run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
