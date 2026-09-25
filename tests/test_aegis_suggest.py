"""Port suggestions: a laptop has thirty listening ports and the interesting
one is rarely first. Ranking must be explainable and must never recommend
something the user cannot actually watch."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aegis.l1_ingestion.suggest import suggest_ports  # noqa: E402

PORTS = [
    {"port": 8080, "pid": 100, "process": "surreal"},
    {"port": 5173, "pid": 200, "process": "node"},
    {"port": 8020, "pid": 300, "process": "Python"},
    {"port": 5432, "pid": 400, "process": "postgres"},
    {"port": 22, "pid": 1, "process": "sshd"},
]


def _logs(pid):
    return f"/logs/{pid}.log" if pid in (100, 300, 400) else ""


def test_a_dependency_outranks_everything_else():
    """The strongest signal there is: the watched app's own config or logs
    say it talks to this port."""
    ranked = suggest_ports(PORTS, dependency_ports={8080}, log_for_pid=_logs)
    assert ranked[0]["port"] == 8080
    assert ranked[0]["recommended"] is True
    assert any("calls this port" in r for r in ranked[0]["reasons"])


def test_a_sibling_service_is_recognised_by_its_directory():
    ranked = suggest_ports(
        PORTS, watched_pid=300, watched_cwd="/repo/services/chatbot",
        log_for_pid=_logs,
        cwd_for_pid=lambda pid: "/repo/services/worker" if pid == 400 else "")
    postgres = next(r for r in ranked if r["port"] == 5432)
    assert any("same project tree" in x for x in postgres["reasons"])


def test_nothing_unwatchable_is_ever_recommended():
    """A port with no readable log is a dead end however interesting it looks
    - recommending it would send the user somewhere with nothing to read."""
    ranked = suggest_ports(PORTS, dependency_ports={5173}, log_for_pid=_logs)
    node = next(r for r in ranked if r["port"] == 5173)
    assert node["watchable"] is False
    assert any("no log file" in r for r in node["reasons"])


def test_noise_ports_and_our_own_process_are_dropped():
    ranked = suggest_ports(PORTS, own_pids={300}, log_for_pid=_logs)
    ports = {r["port"] for r in ranked}
    assert 22 not in ports, "ssh is never a log source"
    assert 8020 not in ports, "the agent must not offer itself"


def test_every_suggestion_carries_its_reason():
    """A recommendation without a reason is just a differently-ordered wall."""
    for row in suggest_ports(PORTS, dependency_ports={8080}, log_for_pid=_logs):
        assert row["reasons"], row


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
    print(f"\n{'FAILED' if failures else 'All suggestion tests passed'}"
          f"{f' ({failures} failing)' if failures else ''}")
    sys.exit(1 if failures else 0)
