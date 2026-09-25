"""Start the Log Intelligence Agent.

A launcher, deliberately thin: everything it needs lives in app.server, so
the server can also be imported and driven by tests without starting a
listener.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.server import main  # noqa: E402

if __name__ == "__main__":
    main()
