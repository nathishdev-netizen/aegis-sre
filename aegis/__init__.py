"""Aegis - the layered platform described in aegis-architecture.md.

Package layout mirrors the document's layer map, so any component's home is
predictable from the layer it belongs to:

    l1_ingestion      L1  collectors and provider connectors      (C1, C2)
    l2_normalization  L2  parse, redact, fingerprint, enrich      (C3)
    l3_storage        L3  event store, metric series, indexes     (C4)
    l4_understanding  L4  topology, flow specs, conformance       (C5-C7)
    l5_detection      L5  statistical detectors                   (C8)
    l6_correlation    L6  incident manager                        (C9)
    contracts         the shared data schemas every layer agrees on

Data flows upward and shrinks at every step. If volume is not dropping by an
order of magnitude per layer, a layer is broken.

This package is additive. Nothing in `app/` imports from here, so v1 and v2
behaviour are unchanged by its presence.
"""

# Read from the installed package metadata so pyproject.toml is the single
# source of truth. Hardcoding it here meant `--version` printed 0.1.0 while
# pyproject said 1.0.0, and nothing caught the disagreement.
try:  # installed
    from importlib.metadata import version as _version

    __version__ = _version("log-intelligence-agent")
except Exception:  # running from a checkout, not installed
    import re as _re
    from pathlib import Path as _Path

    try:
        _toml = (_Path(__file__).resolve().parent.parent
                 / "pyproject.toml").read_text()
        _found = _re.search(r'^version\s*=\s*"([^"]+)"', _toml, _re.M)
        __version__ = _found.group(1) if _found else "0+unknown"
    except Exception:
        __version__ = "0+unknown"
