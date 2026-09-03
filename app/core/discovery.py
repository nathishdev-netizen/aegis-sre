from __future__ import annotations

import re
import subprocess


def discover_listening_ports() -> list[dict]:
    """Return listening TCP ports visible on the local machine."""
    try:
        result = subprocess.run(
            ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return []

    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if len(lines) <= 1:
        return []

    ports: list[dict] = []
    seen: set[tuple[int, int, str]] = set()
    for line in lines[1:]:
        # lsof columns are whitespace-separated, but command names may be truncated.
        match = re.search(r"(?P<cmd>\S+)\s+(?P<pid>\d+)\s+\S+\s+\S+\s+\S+\s+\S+\s+(?P<name>.+)$", line)
        if not match:
            continue
        name = match.group("name")
        port_match = re.search(r":(?P<port>\d+)(?:\s|\(|$)", name)
        if not port_match:
            continue
        pid = int(match.group("pid"))
        port = int(port_match.group("port"))
        key = (pid, port, match.group("cmd"))
        if key in seen:
            continue
        seen.add(key)
        ports.append(
            {
                "pid": pid,
                "process": match.group("cmd"),
                "port": port,
                "name": name.strip(),
                "line": line.strip(),
            }
        )

    # Keep the list stable and easy to scan.
    ports.sort(key=lambda item: (item["port"], item["process"]))
    return ports
