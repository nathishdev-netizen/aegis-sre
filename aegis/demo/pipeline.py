"""Run the complete Phase 0 pipeline on one project's log.

    python3 -m aegis.demo.pipeline <logfile> [project-name] [--fresh]

Everything is scoped to the named project: its templates and counts land in
~/.aegis/projects/<project>/store.db and nowhere else. Run it on two different
projects and list that directory - two databases, nothing shared. The log file
itself is opened read-only and left byte-for-byte untouched.
"""

from __future__ import annotations

import hashlib
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from aegis.l3_storage.store import AEGIS_HOME  # noqa: E402
from aegis.pipeline import Pipeline  # noqa: E402


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if a != "--fresh"]
    fresh = "--fresh" in argv
    if not args:
        print(__doc__)
        return 1
    log = Path(args[0]).expanduser()
    if not log.exists():
        print(f"No such log file: {log}")
        return 1
    project = args[1] if len(args) > 1 else log.stem

    if fresh:
        target = AEGIS_HOME / "projects" / project
        if target.exists():
            shutil.rmtree(target)

    before = hashlib.sha256(log.read_bytes()).hexdigest()

    pipeline = Pipeline(project, log)
    pipeline.run_once()
    pipeline.drain()
    stats = pipeline.stats()

    print(f"\nproject     : {stats['project']}")
    print(f"store       : {stats['store']}")
    print(f"lines in    : {stats['lines_in']}")
    print(f"events      : {stats['events']}")
    print(f"templates   : {stats['templates']}"
          f"   (funnel {stats['lines_in'] / max(1, stats['templates']):.1f}x)")
    print(f"correlation : {stats['extracted']} extracted"
          f" / {stats['inferred']} inferred / {stats['unattributed']} none")

    print("\ntop templates in this project's store:")
    for template in pipeline.store.templates(limit=5):
        print(f"  {template['count']:5}x  {template['pattern'][:62]}")
    pipeline.close()

    after = hashlib.sha256(log.read_bytes()).hexdigest()
    print(f"\nwatched log untouched: {'YES' if before == after else 'NO - BUG'}"
          f"  (sha256 {'identical' if before == after else 'CHANGED'})")

    projects_dir = AEGIS_HOME / "projects"
    print(f"\nall projects under {projects_dir} - one directory each, nothing shared:")
    for entry in sorted(projects_dir.iterdir()):
        if entry.is_dir():
            size = sum(f.stat().st_size for f in entry.rglob("*") if f.is_file())
            print(f"  {entry.name:24} {size / 1024:7.1f} KB")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
