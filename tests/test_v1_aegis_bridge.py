"""The v1-UI bridge: Aegis intelligence inside the Log Intelligence Agent.

The contract: v1's behaviour is untouched; the bridge is additive and
degrades to absence. If aegis breaks, v1 must not notice.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.server as v1_server  # noqa: E402


def test_bridge_initialises_lazily_and_attaches():
    app = v1_server._aegis()
    assert app is not None, v1_server._aegis_error
    result = app.attach("tests/fixtures/spring-boot.log")
    assert result["ok"] is True
    app.pipeline.run_once(); app.pipeline.drain()
    state = app.state()
    assert state["attached"] is True
    assert state["stats"]["events"] > 0


def test_page_carries_the_run_quality_section():
    page = (Path("app/web/index.html")).read_text()
    # The redesign replaced the single bolted-on "aegisPanel" with the
    # tabbed shell, so the panel id is gone by intent. What must still
    # be true is that every v2 capability has a home on the page.
    assert 'data-view="now"' in page and 'data-sub="flow"' in page
    assert "Run quality" in page
    assert "/api/aegis/state" in page
    # every v2 capability has a home on this page
    for element_id in ("aegisFunnel", "aegisVerdicts", "aegisIncidents",
                       "aegisGaps", "aegisSpec", "aegisPatterns", "aegisNotes",
                       "data-aegis-fix", "data-aegis-explain", "aegisMarkBtn"):
        assert element_id in page, f"missing from the v1 UI: {element_id}"
    # every v2 capability has a home on this page
    for element_id in ("aegisFunnel", "aegisVerdicts", "aegisIncidents",
                       "aegisGaps", "aegisSpec", "aegisPatterns", "aegisNotes",
                       "data-aegis-fix", "data-aegis-explain", "aegisMarkBtn"):
        assert element_id in page, f"missing from the v1 UI: {element_id}"
    # every v2 capability has a home on this page
    for element_id in ("aegisFunnel", "aegisVerdicts", "aegisIncidents",
                       "aegisGaps", "aegisSpec", "aegisPatterns", "aegisNotes",
                       "data-aegis-fix", "data-aegis-explain", "aegisMarkBtn"):
        assert element_id in page, f"missing from the v1 UI: {element_id}"
    # every v2 capability has a home on this page
    for element_id in ("aegisFunnel", "aegisVerdicts", "aegisIncidents",
                       "aegisGaps", "aegisSpec", "aegisPatterns", "aegisNotes",
                       "data-aegis-fix", "data-aegis-explain", "aegisMarkBtn"):
        assert element_id in page, f"missing from the v1 UI: {element_id}"
    # every v2 capability has a home on this page
    for element_id in ("aegisFunnel", "aegisVerdicts", "aegisIncidents",
                       "aegisGaps", "aegisSpec", "aegisPatterns", "aegisNotes",
                       "data-aegis-fix", "data-aegis-explain", "aegisMarkBtn"):
        assert element_id in page, f"missing from the v1 UI: {element_id}"
    # the section hides itself when the bridge is absent - v1 look preserved
    assert 'id="aegisPanel" hidden' in page


def test_v1_engine_never_imports_aegis():
    """The engine boundary holds: only the SERVER file bridges. app/core/*
    stays aegis-free, so v1's logic cannot be affected by aegis changes."""
    for path in Path("app/core").glob("*.py"):
        assert "aegis" not in path.read_text(), f"{path} imports aegis"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except AssertionError as exc:
                failures += 1
                print(f"  FAIL  {name}: {exc}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"  ERROR {name}: {exc.__class__.__name__}: {exc}")
    print(f"\n{'FAILED' if failures else 'All bridge tests passed'}"
          f"{f' ({failures} failing)' if failures else ''}")
    sys.exit(1 if failures else 0)
