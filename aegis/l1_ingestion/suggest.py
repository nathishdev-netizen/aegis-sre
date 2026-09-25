"""Which of the machine's ports is worth watching, and why.

A flat list of every listening port is a wall, not a suggestion - a laptop
has thirty, and the interesting one is rarely first. This ranks them using
what the platform already knows:

  dependency   the watched project's own logs mention this port. Strongest
               signal there is: the app told us it talks to this thing.
  sibling      same repo/working directory as the watched process, so it is
               almost certainly another service of the same system.
  has a log    a process whose log file we can find is watchable NOW; one
               without is a dead end however interesting it looks.
  recognised   a process name that maps to a known kind of service.

Every suggestion carries the reason it was made, because a recommendation
without a reason is just a differently-ordered wall.
"""

from __future__ import annotations

from typing import Any

# Process names -> what they are. Only used to explain a suggestion, never to
# rank on its own: "python" says nothing about whether it matters to you.
_KINDS = {
    "surreal": "database", "postgres": "database", "psql": "database",
    "mysqld": "database", "mongod": "database", "redis-ser": "cache",
    "redis": "cache", "clickhouse": "database", "node": "node service",
    "python": "python service", "python3": "python service",
    "uvicorn": "python web service", "gunicorn": "python web service",
    "java": "jvm service", "ollama": "local model server",
    "docker": "container", "com.docke": "container",
}

# Ports the user is very unlikely to want as a log source.
_NOISE_PORTS = {22, 53, 123, 137, 138, 139, 445, 631, 5353, 7000, 7100}


def _kind_of(process: str) -> str:
    low = (process or "").lower()
    for name, kind in _KINDS.items():
        if low.startswith(name):
            return kind
    return ""


def suggest_ports(ports: list[dict[str, Any]], *,
                  dependency_ports: set[int] | None = None,
                  watched_pid: int = 0,
                  watched_cwd: str = "",
                  watched_project_root: str = "",
                  watched_log: str = "",
                  watched_logs: "set[str] | None" = None,
                  own_pids: set[int] | None = None,
                  log_for_pid=None,
                  cwd_for_pid=None) -> list[dict[str, Any]]:
    """Rank listening ports by how likely they are to be worth watching."""
    dependency_ports = dependency_ports or set()
    own_pids = own_pids or set()
    ranked: list[dict[str, Any]] = []

    for item in ports:
        try:
            port = int(item.get("port", 0) or 0)
            pid = int(item.get("pid", 0) or 0)
        except (TypeError, ValueError):
            continue
        if not port or pid in own_pids or port in _NOISE_PORTS:
            continue
        # Tunnels, agents and editors run beside a project without being
        # part of it. Watching cloudflared tells you nothing about the
        # service it fronts.
        if any(word in str(item.get("process", "")).lower() for word in
               ("cloudflar", "ngrok", "tailscale", "dbeaver", "devin",
                "language_", "code helper", "chrome", "google")):
            continue

        process = str(item.get("process", "") or "")
        reasons: list[str] = []
        score = 0
        dependency_claim = False

        if port in dependency_ports:
            # A port NUMBER is not a service. :8099 appeared in this
            # project's migration scripts and the process actually holding
            # :8099 was an unrelated app's dev server - matching on the
            # number alone claimed the watched service calls something it
            # has never touched. The claim only stands when the process
            # holding the port also lives in the watched project's tree.
            score += 100
            reasons.append("your watched service calls this port")
            dependency_claim = True

        cwd = ""
        if cwd_for_pid is not None and pid:
            try:
                cwd = cwd_for_pid(pid) or ""
            except Exception:
                cwd = ""
        if watched_cwd and cwd and pid != watched_pid:
            # Siblings usually live BESIDE each other (services/chatbot and
            # services/worker), not inside each other - comparing the paths
            # directly only matched a parent/child pair and missed every
            # actual sibling.
            import os as _os
            shared = _os.path.commonpath([cwd, watched_cwd]) if \
                cwd.startswith("/") and watched_cwd.startswith("/") else ""
            watched_root = (watched_cwd or "").rstrip("/")
            # A genuine sibling shares its IMMEDIATE parent - services/a
            # and services/b meet at services/. A stranger shares something
            # further up: two projects in one workspace folder meet three
            # levels above, which is how shappers came to claim "same
            # project tree" while the user watched paideia. Distance from
            # the watched directory is the whole distinction.
            # Distance alone cannot separate them: services/chatbot and
            # unrelated-project are BOTH one level below their shared path.
            # What differs is WHAT that shared folder is. Siblings meet at a
            # grouping directory a repo deliberately creates; strangers meet
            # at a workspace folder that merely holds unrelated checkouts.
            # Distance alone cannot separate them: services/chatbot and an
            # unrelated checkout are BOTH one level below their shared path.
            # What differs is whether that shared folder is a PROJECT or a
            # drawer holding several. A project root is one that also
            # contains the watched service; a drawer is one whose other
            # child happens to be a different repository. The honest test
            # available here: the shared folder must be the watched
            # service's own parent, not its grandparent or higher.
            # Path shape alone cannot settle this: work/svc + work/other
            # and workspace/projectA + workspace/projectB are structurally
            # identical, and three attempts at a distance or naming rule
            # each got one of them right and the other wrong. The clustering
            # pass below already decides which project each port belongs to
            # by grouping them - so the honest test is whether the two
            # services land in the SAME cluster, not how their paths look.
            # That check happens after scoring, so the bonus is recorded as
            # provisional here and confirmed there.
            # Measured from the wrong side for three attempts. A sibling
            # sits ONE level below the shared path - project/svc beside
            # project/other. A stranger sits deeper: workspace/shappers/
            # apps/mobile is three below the folder it shares with
            # workspace/paideia. Both services being immediate children of
            # the shared path is what "same project tree" actually means.
            both_immediate = (
                cwd.count("/") - shared.count("/") <= 1
                and watched_root.count("/") - shared.count("/") <= 1)
            # Measured from the wrong side for three attempts. A sibling
            # sits ONE level below the shared path - project/svc beside
            # project/other. A stranger sits deeper: workspace/shappers/
            # apps/mobile is three below the folder it shares with
            # workspace/paideia. Both services being immediate children of
            # the shared path is what "same project tree" actually means.
            both_immediate = (
                cwd.count("/") - shared.count("/") <= 1
                and watched_root.count("/") - shared.count("/") <= 1)
            if shared and watched_root and (
                    shared == watched_root
                    or shared.startswith(watched_root + "/")
                    or both_immediate):
                score += 40
                reasons.append("same project tree as what you are watching")

        log_path = ""
        if log_for_pid is not None and pid:
            try:
                log_path = log_for_pid(pid) or ""
            except Exception:
                log_path = ""
        if log_path:
            score += 25
            reasons.append("writes a log file we can read")
        else:
            reasons.append("no log file found - nothing to read yet")

        kind = _kind_of(process)
        if kind:
            score += 5
            reasons.append(kind)

        # The dependency claim is only honest when the process holding the
        # port belongs to the project being watched. Withdraw it otherwise,
        # rather than leave a sentence the user can check and disprove.
        # The check must also run when there is no watched_cwd - a
        # connector source has none, and that was exactly the case where an
        # unrelated project's dev server kept its +100 for sharing a port
        # number. Fall back to the clustered project name.
        reference = watched_cwd or watched_project_root
        if dependency_claim and reference and cwd:
            import os as _os2
            try:
                shared = _os2.path.commonpath([cwd, reference])
            except ValueError:
                shared = ""
            # Depth is not kinship. Two unrelated projects under one
            # workspace folder share a five-segment path and looked like
            # siblings - the shared path has to BE the project root, or
            # inside it, not merely deep.
            inside = bool(shared) and (
                shared == reference.rstrip("/")
                or shared.startswith(reference.rstrip("/") + "/"))
            if not inside:
                score -= 100
                reasons = [r for r in reasons
                           if r != "your watched service calls this port"]
                reasons.append("shares a port number with a dependency, but "
                               "this process is from another project")

        ranked.append({
            # The one already being watched must say so. Without it every
            # row offered "Watch" including the row the user had just
            # clicked, so the screen never acknowledged the action.
            # ...and "watched" means ANY log Aegis is reading - the primary
            # and every service added with Watch together. Checking only
            # the primary left three of four freshly attached rows still
            # offering "Watch", which is the opposite of acknowledging.
            "is_watched": bool(
                (watched_pid and pid == watched_pid)
                or (watched_log and log_path and log_path == watched_log)
                or (log_path and log_path in (watched_logs or ()))),
            "port": port, "pid": pid, "process": process,
            "kind": kind, "log_path": log_path, "cwd": cwd,
            "score": score, "reasons": reasons,
            "watchable": bool(log_path),
            "recommended": score >= 100 or (score >= 60 and bool(log_path)),
        })

    # Relatedness has been measured only against a source already being
    # watched, so from a cold start nothing can be related to anything and
    # every port scores the same. But services that share a project
    # directory are visibly one system - three ports under
    # <root>/services/* is a platform, not three strangers - and that is
    # knowable before anything is attached. Group by shared root and treat
    # a cluster as the recommendation.
    import os as _os
    from collections import defaultdict
    clusters: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in ranked:
        cwd = row.get("cwd") or ""
        if not cwd:
            continue
        # Walk up out of a services/<name> or apps/<name> layout so siblings
        # meet at the project they belong to. A service living AT the project
        # root must stay there: walking it up anyway put the API one level
        # above its own workers, and the two never met.
        root = cwd
        for _ in range(3):
            parent = _os.path.dirname(root)
            if not parent or parent.count("/") < 3:
                break
            if _os.path.basename(parent) in ("services", "apps", "src",
                                             "packages"):
                root = _os.path.dirname(parent)   # skip the grouping folder
                break
            if _os.path.basename(root) in ("services", "apps", "src",
                                           "packages"):
                root = parent
                break
            break
        clusters[root].append(row)

    # Infrastructure a developer did not write is not their project. Docker,
    # databases and editors all share a directory too, and grouping them
    # produces a confident recommendation to watch the container runtime.
    # Infrastructure and scratch space are not projects. A temp directory
    # full of helper processes clusters exactly like a real codebase does,
    # and gets recommended with the same confidence.
    _INFRA = ("docker", "orbstack", "colima", "podman", "postgres", "mysql",
              "redis", "mongod", "library", "applications",
              "/tmp/", "/var/folders/", "/private/tmp", "scratchpad",
              "caches", "/.cache/", "appdata")
    # Infrastructure a developer did not write is not their project. Docker,
    # databases and editors all share a directory too, and grouping them
    # produces a confident recommendation to watch the container runtime.
    _INFRA = ("docker", "orbstack", "colima", "podman", "postgres", "mysql",
              "redis", "mongod", "library", "applications")
    # Revoke the provisional sibling bonus for anything whose cluster is
    # not the watched service's cluster. This is the check path shape could
    # not make: two projects in one workspace folder cluster separately.
    watched_cluster = ""
    for root, members in clusters.items():
        if any(m.get("cwd") == watched_cwd for m in members):
            watched_cluster = root
    if watched_cluster:
        for root, members in clusters.items():
            if root == watched_cluster:
                continue
            for row in members:
                if "same project tree as what you are watching" in row["reasons"]:
                    row["score"] -= 40
                    row["reasons"].remove(
                        "same project tree as what you are watching")

    for root, members in clusters.items():
        if len(members) < 2:
            continue
        label = _os.path.basename(root) or root
        # Match the whole path, not just its last segment: walking up from
        # .../com.docker.docker/data lands on "data", which names nothing.
        low = root.lower()
        if any(word in low for word in _INFRA) or root.count("/") < 4:
            continue
        # Match the whole path, not just its last segment: walking up from
        # .../com.docker.docker/data lands on "data", which names nothing.
        low = root.lower()
        if any(word in low for word in _INFRA) or root.count("/") < 4:
            continue
        for row in members:
            row["score"] += 45
            row["project"] = label
            # The count is deliberately NOT written into the reason here:
            # duplicate ports and excluded processes are removed after this
            # point, so a number frozen now disagrees with the list the user
            # can see - the header said 4 services and every row said 6.
            row["reasons"].append(f"part of the {label} project")

    # A service forked into workers listens once but appears per process.
    # Two identical rows for :8020 is a listing bug, not two services.
    seen_ports: set[int] = set()
    unique: list[dict[str, Any]] = []
    for row in sorted(ranked, key=lambda r: (-r["score"], r["port"])):
        if row["port"] in seen_ports:
            continue
        seen_ports.add(row["port"])
        unique.append(row)
    ranked = unique

    # A service forked into workers listens once but appears per process.
    # Two identical rows for :8020 is a listing bug, not two services.
    seen_ports: set[int] = set()
    unique: list[dict[str, Any]] = []
    for row in sorted(ranked, key=lambda r: (-r["score"], r["port"])):
        if row["port"] in seen_ports:
            continue
        seen_ports.add(row["port"])
        unique.append(row)
    ranked = unique

    ranked.sort(key=lambda r: (-r["score"], r["port"]))
    for row in ranked:
        # RECOMMENDED has to mean "this one, specifically". Eight rows all
        # tagged recommended is the same as none of them being tagged. Only
        # a verified dependency of the watched service earns it; merely
        # sharing a project folder does not, and is said in the reasons.
        # ...and never something already being watched: RECOMMENDED next to
        # WATCHING on the same row asks the user to do what they just did.
        row["recommended"] = (bool(row["log_path"]) and row["score"] >= 100
                              and not row.get("is_watched"))
    return ranked
