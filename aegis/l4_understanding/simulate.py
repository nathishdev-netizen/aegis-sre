"""C14 rung 1 - static what-if: "will errors come if this scenario happens?"

The lightweight-gateway approach from the chaos-engineering literature:
synthesize a model from artifacts that already exist (the mined flow spec,
the component topology, code analysis when a repo was analyzed) and answer on
the graph - no fault is injected anywhere. Published results show graph
simulation of a discovered model closely tracks live fault injection, which
is exactly the standard this aims at: a useful PREDICTION with its basis
attached, never a claim of certainty.

The output vocabulary is the field's: an impact zone (what is affected), a
predicted outcome per flow (failed / degraded / hollow), and the gaps - what
the model cannot know, said plainly.
"""

from __future__ import annotations

import re

from typing import Any


def simulate_failure(target: str, *,
                     topology=None,
                     spec=None,
                     dependencies: list[dict[str, Any]] | None = None,
                     code: dict[str, Any] | None = None,
                     flow: dict[str, Any] | None = None,
                     aliases: dict[str, str] | None = None) -> dict[str, Any]:
    """Predict what happens if `target` (a component, or a dependency like
    localhost:6004) goes down."""
    raw_target = (target or "").strip()
    target = raw_target.lower()
    if not target:
        return {"ok": False, "detail": "name a component or dependency"}

    known: list[str] = []
    if topology is not None:
        known += sorted(getattr(topology, "nodes", {}))
    for row in dependencies or []:
        host = str(row.get("host", ""))
        port = row.get("port")
        known.append(f"{host}:{port}" if port else host)
    # The project flow knows every measured operation and every dependency
    # with its call sites. Those are the things a user actually asks about,
    # and simulate was answering UNKNOWN for all of them while the facts sat
    # one screen away.
    for node in (flow or {}).get("nodes", []):
        if node.get("band") in ("work", "depends") and node.get("label"):
            known.append(str(node["label"]))

    # A user types a question, not an identifier ("what if surreal goes down?").
    # Match on the words they used rather than demanding an exact id - and when
    # nothing matches, SAY what the valid names are instead of just refusing.
    if target not in [k.lower() for k in known]:
        words = {w for w in re.split(r"[^a-z0-9_]+", target) if len(w) > 2}
        # People name a technology, not a component id: "surreal" when the
        # component is chatbot_db. Aliases come from the words each component
        # actually uses in its own log lines.
        for word in list(words):
            if not aliases:
                break
            hit = aliases.get(word)
            if hit is None:
                # People type the product name, not the package name:
                # "surreal" for surrealdb, "postgres" for postgresql.
                matches = {alias: comp for alias, comp in aliases.items()
                           if alias.startswith(word) or word.startswith(alias)}
                if len({c for c in matches.values()}) == 1:
                    hit = next(iter(matches.values()))
            if hit:
                target = hit
                words = set()
                break
        # People name a technology, not a component id: "surreal" when the
        # component is chatbot_db. Aliases come from the words each component
        # actually uses in its own log lines.
        for word in list(words):
            if not aliases:
                break
            hit = aliases.get(word)
            if hit is None:
                # People type the product name, not the package name:
                # "surreal" for surrealdb, "postgres" for postgresql.
                matches = {alias: comp for alias, comp in aliases.items()
                           if alias.startswith(word) or word.startswith(alias)}
                if len({c for c in matches.values()}) == 1:
                    hit = next(iter(matches.values()))
            if hit:
                target = hit
                words = set()
                break
        # People name a technology, not a component id: "surreal" when the
        # component is chatbot_db. Aliases come from the words each component
        # actually uses in its own log lines.
        for word in list(words):
            if not aliases:
                break
            hit = aliases.get(word)
            if hit is None:
                # People type the product name, not the package name:
                # "surreal" for surrealdb, "postgres" for postgresql.
                matches = {alias: comp for alias, comp in aliases.items()
                           if alias.startswith(word) or word.startswith(alias)}
                if len({c for c in matches.values()}) == 1:
                    hit = next(iter(matches.values()))
            if hit:
                target = hit
                words = set()
                break
        # People name a technology, not a component id: "surreal" when the
        # component is chatbot_db. Aliases come from the words each component
        # actually uses in its own log lines.
        for word in list(words):
            if not aliases:
                break
            hit = aliases.get(word)
            if hit is None:
                # People type the product name, not the package name:
                # "surreal" for surrealdb, "postgres" for postgresql.
                matches = {alias: comp for alias, comp in aliases.items()
                           if alias.startswith(word) or word.startswith(alias)}
                if len({c for c in matches.values()}) == 1:
                    hit = next(iter(matches.values()))
            if hit:
                target = hit
                words = set()
                break
        # People name a technology, not a component id: "surreal" when the
        # component is chatbot_db. Aliases come from the words each component
        # actually uses in its own log lines.
        for word in list(words):
            if not aliases:
                break
            hit = aliases.get(word)
            if hit is None:
                # People type the product name, not the package name:
                # "surreal" for surrealdb, "postgres" for postgresql.
                matches = {alias: comp for alias, comp in aliases.items()
                           if alias.startswith(word) or word.startswith(alias)}
                if len({c for c in matches.values()}) == 1:
                    hit = next(iter(matches.values()))
            if hit:
                target = hit
                words = set()
                break
        # An alias resolved the target - skip scoring entirely. Falling through
        # with an empty word set scored nothing and overwrote the resolution.
        resolved_by_alias = not words
        scored = []
        for name in known:
            low = name.lower()
            hit = sum(1 for w in words if w in low or low.split(":")[0] in w
                      or any(part and part in w for part in low.split("_")))
            if hit:
                scored.append((hit, name))
        if resolved_by_alias:
            pass
        elif len(scored) == 1 or (scored and scored[0][0] >
                                  max([s0 for s0, _ in scored[1:]], default=0)):
            scored.sort(reverse=True)
            target = scored[0][1].lower()
        elif scored:
            return {"ok": False,
                    "detail": f"did you mean: {', '.join(n for _s, n in scored[:4])}?",
                    "known": known}
        else:
            return {"ok": False,
                    "detail": f"'{raw_target}' is not something this project has "
                              "shown. Known targets: " + ", ".join(known[:10]),
                    "known": known}

    impact_zone: list[str] = []
    predictions: list[dict[str, str]] = []
    gaps: list[str] = []
    basis: list[str] = []

    # --- component in the mined topology -----------------------------------
    known_component = topology is not None and target in getattr(topology, "nodes", {})
    if known_component:
        radius = topology.blast_radius(target)
        impact_zone = radius["downstream"]
        basis.append(f"topology mined from observed runs: {target} runs before "
                     f"{', '.join(impact_zone) if impact_zone else 'nothing observed'}")

    # --- steps in the flow spec ---------------------------------------------
    hit_steps = []
    if spec is not None:
        for step in getattr(spec, "steps", []):
            from aegis.l4_understanding.topology import component_of
            step_component = component_of(step.label, fallback="")
            if step_component == target:
                hit_steps.append(step)
        if hit_steps:
            criticals = [s for s in hit_steps if s.critical]
            requireds = [s for s in hit_steps if s.required and not s.critical]
            if criticals:
                predictions.append({
                    "outcome": "hollow-or-failed",
                    "because": f"{len(criticals)} purpose step(s) run in {target} - "
                               "runs would complete without achieving anything, or "
                               "abort, depending on error handling"})
            elif requireds:
                predictions.append({
                    "outcome": "degraded",
                    "because": f"{len(requireds)} required (non-purpose) step(s) "
                               f"run in {target}"})
            basis.append(f"flow spec: {len(hit_steps)} step(s) belong to {target}")

    # --- the project flow: call sites, guards, and what measures it --------
    # This is the half simulate never looked at. The flow knows which call
    # site reaches a dependency and whether THAT site has a timeout, which
    # is the difference between "callers fail fast" and "callers hang" - the
    # single most useful thing to say about a service going down.
    flow_nodes = {str(n.get("label", "")).lower(): n
                  for n in (flow or {}).get("nodes", [])}
    node = flow_nodes.get(target)
    if node is not None:
        band = node.get("band")
        if band == "depends":
            sites = node.get("call_sites") or []
            unguarded = [c for c in sites
                         if not c.get("guarded") or not c.get("has_timeout")]
            workers = [n["label"] for n in (flow or {}).get("nodes", [])
                       if n.get("band") == "work"]
            if unguarded:
                no_timeout = [c for c in unguarded if not c.get("has_timeout")]
                predictions.append({
                    "outcome": "failed-or-hung",
                    "because": (
                        f"{len(unguarded)} call site(s) reach it without a "
                        f"guard" + (f", and {len(no_timeout)} without a timeout - "
                        "those callers HANG rather than fail, holding the "
                        "request open" if no_timeout else "") +
                        (f". Every measured operation ({', '.join(workers)}) "
                         "runs through this service" if workers else ""))})
                for site in unguarded[:4]:
                    impact_zone.append(
                        f"{site.get('function')}() at {site.get('source')}")
            else:
                predictions.append({
                    "outcome": "degraded",
                    "because": "every call site reaching it is guarded and "
                               "timed - callers survive and skip its work"})
            basis.append(
                f"project flow: {len(sites)} call site(s) reach {target}"
                + (", observed running in the logs" if node.get("observed")
                   else ", declared in code but never observed running"))
            if node.get("running_here"):
                basis.append("this dependency is running on this machine - "
                             "its own log would show the cause")
        elif band == "work":
            # An operation going down is judged by what it measures and by
            # what it leans on, both of which the flow holds.
            deps = [n for n in (flow or {}).get("nodes", [])
                    if n.get("band") == "depends" and n.get("unguarded")]
            predictions.append({
                "outcome": "failed",
                "because": (
                    f"{target} is a measured operation" +
                    (f" (normally {int(node['median_ms'])}ms, p95 "
                     f"{int(node.get('p95_ms') or 0)}ms)"
                     if node.get("median_ms") else "") +
                    ". Requests that reach it produce nothing" +
                    (f". It runs through {len(deps)} unguarded dependency "
                     "call(s), so a failure there surfaces here first"
                     if deps else ""))})
            if node.get("spread", 0) >= 20:
                gaps.append(
                    f"{target} already shows a p95 {node['spread']}x its "
                    "median - it does not need to go down to hurt you")
            basis.append(
                f"project flow: measured from {node.get('samples')} runs")

    # --- dependency guard status from code ----------------------------------
    dep_row = None
    for row in dependencies or []:
        key = f"{row.get('host','')}:{row.get('port','')}".lower()
        if target in (str(row.get("host", "")).lower(), key,
                      str(row.get("url", "")).lower()):
            dep_row = row
            break
    if dep_row is not None:
        guarded = dep_row.get("guarded_in_code")
        if guarded is True:
            predictions.append({
                "outcome": "degraded",
                "because": "every call to it in the analyzed code is wrapped in "
                           "try/except - callers survive, the work it did is skipped"})
            basis.append("code analysis: calls are guarded")
        elif guarded is False:
            predictions.append({
                "outcome": "failed",
                "because": "at least one call to it in the analyzed code has no "
                           "try/except (and/or no timeout) - its failure propagates "
                           "as an unhandled error"})
            basis.append("code analysis: at least one unguarded call")
        else:
            gaps.append("no code analysis for this dependency - whether callers "
                        "survive its failure is unknown; run Analyze project")
        if dep_row.get("single_point_of_failure"):
            basis.append("this is the most-depended-upon target in the map "
                         "(single point of failure)")

    # The flow is a fourth source of truth, and the guard predated it: a
    # target matched only by the flow produced predictions and was then
    # thrown away by a check that had never heard of it.
    if not known_component and not hit_steps and dep_row is None and node is None:
        return {"ok": False,
                "detail": f"'{target}' is not a known component, flow step or "
                          "dependency - nothing to simulate against"}

    if not predictions:
        if impact_zone:
            predictions.append({
                "outcome": "degraded",
                "because": f"no purpose step is marked in {target}, so runs would "
                           f"likely continue - but {', '.join(impact_zone[:4])} run "
                           "after it and would lose whatever it provides"})
        else:
            predictions.append({
                "outcome": "unknown",
                "because": "it appears in runs, but nothing downstream depends on "
                           "it and no marked step belongs to it - mark purpose "
                           "steps, or analyze the repo, to predict more"})
    if spec is None or not getattr(spec, "steps", []):
        gaps.append("no flow spec yet - per-run outcome prediction is limited")
    gaps.append("static prediction from observed structure - a run under real "
                "failure can differ; treat this as where to LOOK, not what will "
                "certainly happen")

    return {
        "ok": True,
        "target": target,
        "impact_zone": impact_zone,
        "predictions": predictions,
        "basis": basis,
        "gaps": gaps,
    }
