"""The supervisor: one Aegis per service, one page over all of them.

What matters here is not that it starts processes - it is that it reports
honestly when one of them is wrong. A supervisor whose overview says "ok"
while a child is dead is worse than no supervisor, because it is the thing
a monitor trusts.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aegis.supervisor import (  # noqa: E402
    CHILD_PORT_OFFSET,
    RESTART_BACKOFF_S,
    Service,
    Supervisor,
    parse_config,
)


def test_config_is_one_line_per_service():
    parsed = parse_config(
        "# which logs to watch\n"
        "flights: /var/log/tt/flights.log\n"
        "\n"
        "hotels:  /var/log/tt/hotels.log   # trailing comment\n"
    )
    assert parsed == [("flights", "/var/log/tt/flights.log"),
                      ("hotels", "/var/log/tt/hotels.log")]


def test_a_malformed_line_is_skipped_not_fatal():
    """A typo in one line must not stop every other service from starting."""
    parsed = parse_config("flights: /a.log\nthis line has no colon\nhotels: /b.log\n")
    assert [name for name, _ in parsed] == ["flights", "hotels"]


def test_each_service_gets_its_own_port():
    """Two children on one port is the one failure that looks like a crash
    loop while actually being a config problem."""
    supervisor = Supervisor([("a", "/a.log"), ("b", "/b.log"), ("c", "/c.log")],
                            port=3000)
    ports = [s.port for s in supervisor.services]
    base = 3000 + CHILD_PORT_OFFSET
    assert ports == [base, base + 1, base + 2]
    assert len(set(ports)) == 3


def test_children_follow_the_supervisor_off_a_busy_port():
    """Child ports were a fixed 3001, independent of --port. On a box where
    3000 was already taken, moving the supervisor to 3010 left its children
    still reaching for 3001 - which another service held - so they bound,
    failed and restarted forever while the supervisor itself looked fine."""
    supervisor = Supervisor([("a", "/a.log"), ("b", "/b.log")], port=3010)
    ports = [s.port for s in supervisor.services]
    assert ports == [3011, 3012], (
        f"children did not follow the supervisor to 3010: {ports}")
    assert supervisor.port not in ports, "a child collides with the supervisor"


def test_overview_is_not_ok_when_a_service_is_down():
    """The whole point of the probe. 'ok' while a child is stopped would
    keep a half-dead deployment in service."""
    supervisor = Supervisor([("flights", "/a.log"), ("hotels", "/b.log")])
    supervisor.services[0].last_health = {"status": "ok", "events": 10}
    supervisor.services[0].process = _FakeProcess(alive=True)
    supervisor.services[1].process = None            # never started

    data = supervisor.overview()
    assert data["ok"] is False
    assert data["unhealthy"] == 1
    statuses = {s["name"]: s["status"] for s in data["services"]}
    assert statuses == {"flights": "ok", "hotels": "stopped"}


def test_a_starting_service_is_not_counted_as_unhealthy():
    """A child polled in the second before its HTTP server binds has no
    health yet. Calling that a failure would make every restart look like
    an outage."""
    supervisor = Supervisor([("flights", "/a.log")])
    supervisor.services[0].process = _FakeProcess(alive=True)
    supervisor.services[0].last_health = {}          # not answered yet

    data = supervisor.overview()
    assert data["services"][0]["status"] == "starting"
    assert data["ok"] is True


def test_restart_backoff_grows_and_is_bounded():
    """A child that cannot start must not be restarted in a tight loop:
    the real error gets buried under thousands of lines of its own retries."""
    supervisor = Supervisor([("flights", "/a.log")])
    service = supervisor.services[0]

    waits = []
    for n in range(len(RESTART_BACKOFF_S) + 3):
        service.restarts = n
        waits.append(supervisor._backoff(service))

    assert waits[0] < waits[-1], "backoff never grows"
    assert waits == sorted(waits), "backoff is not monotonic"
    assert max(waits) == RESTART_BACKOFF_S[-1], "backoff is unbounded"


def test_a_service_reports_the_url_a_reader_can_open():
    service = Service(name="flights", log_path="/a.log", port=3001)
    assert service.url == "http://127.0.0.1:3001"


class _FakeProcess:
    """Enough of Popen for the overview to be exercised without spawning."""

    def __init__(self, alive: bool) -> None:
        self._alive = alive
        self.pid = 4242

    def poll(self):
        return None if self._alive else -9


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
    print(f"\n{'FAILED' if failures else 'All supervisor tests passed'}"
          f"{f' ({failures} failing)' if failures else ''}")
    sys.exit(1 if failures else 0)
