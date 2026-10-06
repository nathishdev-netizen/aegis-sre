"""Production-grade guards, each one traceable to field guidance on how this
class of tool fails: unbounded backfills freeze attach, rotation without
inode tracking reads mid-file garbage, pathological lines wedge parsers,
corrupt stores must cost history and never the product, and the package must
install and identify itself."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aegis.l1_ingestion import file_collector as fc  # noqa: E402
from aegis.l1_ingestion.file_collector import FileCollector  # noqa: E402
from aegis.l3_storage.store import ProjectStore  # noqa: E402


def test_backfill_is_bounded_on_huge_files(monkeypatch=None):
    """A 2GB log froze attach when the whole file was read for its tail.
    Backfill reads at most the cap, and says so."""
    log = Path(tempfile.mkdtemp()) / "big.log"
    old_cap = fc.BACKFILL_MAX_BYTES
    fc.BACKFILL_MAX_BYTES = 4096
    try:
        log.write_text("".join(f"line number {i}\n" for i in range(2000)))
        collector = FileCollector(log)
        records = list(collector.poll())
        assert collector.backfill_note, "a capped backfill must announce itself"
        assert len(records) < 400, f"cap ignored: {len(records)} records"
        # and the first record is a WHOLE line, not a mid-line fragment
        assert records[0].payload.startswith("line number ")
    finally:
        fc.BACKFILL_MAX_BYTES = old_cap


def test_rotation_is_detected_by_inode_not_size():
    """logrotate renames the old file and recreates the name. If the new file
    outgrows the old offset before the next poll, a size-only check reads
    from the middle of unrelated content."""
    directory = Path(tempfile.mkdtemp())
    log = directory / "svc.log"
    log.write_text("old-1\nold-2\n")
    collector = FileCollector(log)
    list(collector.poll())
    # rotate: move aside, recreate LARGER than the old offset
    log.rename(directory / "svc.log.1")
    log.write_text("new-1\nnew-2\nnew-3\nnew-4\n")
    payloads = [r.payload for r in collector.poll()]
    assert payloads == ["new-1", "new-2", "new-3", "new-4"], payloads


def test_pathological_lines_are_truncated():
    log = Path(tempfile.mkdtemp()) / "svc.log"
    log.write_text("ok\n" + "x" * (fc.MAX_LINE_BYTES + 5000) + "\nafter\n")
    payloads = [r.payload for r in FileCollector(log).poll()]
    assert len(payloads) == 3
    assert len(payloads[1]) <= fc.MAX_LINE_BYTES + 20
    assert payloads[1].endswith("…[truncated]")
    assert payloads[2] == "after"


def test_a_corrupt_store_is_quarantined_not_fatal():
    root = Path(tempfile.mkdtemp())
    target = root / "svc" / "store.db"
    target.parent.mkdir(parents=True)
    target.write_text("this is not a sqlite database at all")
    store = ProjectStore("svc", root=root)      # must not raise
    assert store.template_count() == 0
    quarantined = list(target.parent.glob("store.corrupt-*"))
    assert quarantined, "the corrupt file was not set aside for recovery"
    store.close()


def test_healthz_answers_with_a_version():
    import threading
    from http.server import ThreadingHTTPServer
    from aegis.server import AegisApp, make_handler
    app = AegisApp()
    app._stop.set(); app._thread.join(timeout=2)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as r:
            payload = json.loads(r.read())
        assert payload["ok"] is True
        assert payload["version"] == "1.0.0"
    finally:
        server.shutdown()
        app.close()


def test_the_package_installs_its_entry_points():
    pyproject = Path("pyproject.toml").read_text()
    assert 'aegis = "app.cli:main"' in pyproject
    assert 'aegis-mcp = "app.cli:mcp_main"' in pyproject
    assert "dependencies = []" in pyproject, "stdlib-only is a product promise"
    from app import cli
    import io, contextlib
    buffer = io.StringIO()
    argv = sys.argv
    sys.argv = ["aegis", "--version"]
    try:
        with contextlib.redirect_stdout(buffer):
            code = cli.main()
    finally:
        sys.argv = argv
    assert code == 0 and "1.0.0" in buffer.getvalue()


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
    print(f"\n{'FAILED' if failures else 'All production-grade tests passed'}"
          f"{f' ({failures} failing)' if failures else ''}")
    sys.exit(1 if failures else 0)
