"""A tiny worker pool - the fixture target for remediation tests.

The bug is the doc's own worked example in miniature: POOL_MAXSIZE was
"tuned" down and now saturates under normal concurrency.
"""

POOL_MAXSIZE = 1  # was 8 before the bad tune

_active = 0


def acquire():
    global _active
    if _active >= POOL_MAXSIZE:
        raise RuntimeError(f"pool exhausted: {_active} connections active")
    _active += 1
    return _active


def release():
    global _active
    _active = max(0, _active - 1)
