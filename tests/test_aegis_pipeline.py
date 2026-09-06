"""Phase 0 completion tests - C1 collector, C4 store, and project isolation.

The isolation tests are the point: the user's requirement is that one project's
data can never affect another's, and that must hold structurally, not by
convention.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aegis.contracts.events import Event  # noqa: E402
from aegis.l1_ingestion.file_collector import FileCollector  # noqa: E402
from aegis.l3_storage.store import HotRing, ProjectStore  # noqa: E402
from aegis.pipeline import Pipeline  # noqa: E402


def _tmpdir() -> Path:
    return Path(tempfile.mkdtemp())


def _event(text: str, template: str, ts: str = "19:00:01") -> Event:
    return Event(id="E", ts=ts, service="svc", level="INFO",
                 text_redacted=text, template_id=template)


# --- C1 collector ------------------------------------------------------------

def test_collector_resumes_from_its_offset():
    """The only state a collector may hold is its read offset - and it must
    actually hold it, or every poll re-emits the whole file."""
    log = _tmpdir() / "a.log"
    log.write_text("one\ntwo\n")
    collector = FileCollector(log)
    assert len(list(collector.poll())) == 2
    with log.open("a") as handle:
        handle.write("three\n")
    assert [r.payload for r in collector.poll()] == ["three"]


def test_collector_survives_rotation():
    """A collector that dies the night logrotate truncates the file is not
    reliable. Truncation resets the offset; nothing raises, nothing is stuck."""
    log = _tmpdir() / "a.log"
    log.write_text("old line one\nold line two\n")
    collector = FileCollector(log)
    list(collector.poll())
    log.write_text("fresh\n")  # rotated: smaller than the old offset
    assert [r.payload for r in collector.poll()] == ["fresh"]


def test_collector_holds_back_half_written_lines():
    """Emitting a line the writer has not finished hands L2 a record that never
    existed. The fragment waits; the completed line arrives whole."""
    log = _tmpdir() / "a.log"
    log.write_text("complete\npart")
    collector = FileCollector(log)
    assert [r.payload for r in collector.poll()] == ["complete"]
    with log.open("a") as handle:
        handle.write("ial line\n")
    assert [r.payload for r in collector.poll()] == ["partial line"]


def test_collector_tags_every_record():
    """Read, tag, forward: downstream must never need to know where a record
    came from by any means other than the record itself."""
    log = _tmpdir() / "gateway.log"
    log.write_text("hello\n")
    record = next(iter(FileCollector(log, service="voice-gateway").poll()))
    assert record.service == "voice-gateway"
    assert record.source_id.endswith("gateway.log")


# --- C4 store: isolation is the requirement ----------------------------------

def test_projects_are_physically_isolated():
    """One project's data must not be able to affect another's - so each gets
    its own database file. There is no shared table for a bug to cross."""
    root = _tmpdir()
    store_a = ProjectStore("project-a", root=root)
    store_b = ProjectStore("project-b", root=root)
    assert store_a.path != store_b.path
    store_a.record_event(_event("hello", "T-1"))
    store_a.flush()
    assert store_a.template_count() == 1
    assert store_b.template_count() == 0, "project-b saw project-a's data"
    store_a.close(); store_b.close()


def test_counts_survive_a_restart():
    """Derived data is the point of the store; losing it on restart would mean
    re-learning every project from scratch, like v1 did."""
    root = _tmpdir()
    store = ProjectStore("svc", root=root)
    for _ in range(3):
        store.record_event(_event("hello", "T-1"))
    store.close()
    reopened = ProjectStore("svc", root=root)
    assert reopened.total_events() == 3
    reopened.close()


def test_hot_ring_is_bounded():
    """Hot means minutes, not history. An unbounded ring is a memory leak with
    a nicer name."""
    ring = HotRing(capacity=10)
    for i in range(100):
        ring.add(_event(f"line {i}", "T-1"))
    assert len(ring) == 10
    assert ring.recent(3)[-1].text_redacted == "line 99"


# --- The pipeline, end to end ------------------------------------------------

def test_pipeline_end_to_end_on_a_real_shaped_log():
    log = _tmpdir() / "svc.log"
    log.write_text(
        "2026-09-01 19:00:00 INFO voice: [api] CALL START call=abc12345-def0\n"
        "2026-09-01 19:00:01 INFO voice: [tts] Synthesised 60 chars in 1136ms\n"
        "2026-09-01 19:00:16 INFO voice: [api] CALL END call=abc12345-def0 - 16s\n"
    )
    pipeline = Pipeline("svc", log, store_root=_tmpdir())
    pipeline.run_once()
    pipeline.drain()
    stats = pipeline.stats()
    assert stats["events"] == 3
    assert stats["templates"] == 3
    assert stats["extracted"] == 2 and stats["inferred"] == 1
    pipeline.close()


def test_pipeline_never_writes_to_the_watched_project():
    """The monitored log is another project's property. Watching it must leave
    it byte-for-byte identical."""
    log = _tmpdir() / "svc.log"
    content = "2026-09-01 19:00:00 INFO voice: [api] CALL START call=abc12345-def0\n"
    log.write_text(content)
    before = log.read_bytes()
    pipeline = Pipeline("svc", log, store_root=_tmpdir())
    pipeline.run_once(); pipeline.drain(); pipeline.close()
    assert log.read_bytes() == before


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
    print(f"\n{'FAILED' if failures else 'All pipeline tests passed'}"
          f"{f' ({failures} failing)' if failures else ''}")
    sys.exit(1 if failures else 0)
