"""C5 - the topology mapper, at the granularity this world actually has.

The doc's C5 maps service-to-service dependencies. With one service there is
still a real graph: the COMPONENTS inside it (api -> tts -> orch -> identity),
mined from the order they appear within each assembled trace. That graph is
what the cause ranker needs - "report the earliest deviation" is P4, but the
doc's worked example ranks by upstream-ness FIRST, and until now our ranking
honestly admitted it was timing alone. When real multi-service traces arrive,
the same mapper works unchanged at service granularity.

Deterministic; derived only from observed transitions; every edge carries the
count that justifies it.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Iterable

_TAG = re.compile(r"\[([A-Za-z_][\w-]{1,24})\]")
_LOGGER = re.compile(r"\b[a-z_]{2,}\.([a-z_]{2,24}):")


def component_of(text: str, fallback: str = "app") -> str:
    """A line's component: its [tag] if it has one, else the logger's leaf
    ("voice.tts:" -> tts), else the fallback. Projects without either simply
    collapse to one node - a degenerate graph, honestly degenerate."""
    match = _TAG.search(text or "")
    if match:
        return match.group(1).lower()
    match = _LOGGER.search(text or "")
    if match:
        return match.group(1).lower()
    return fallback


# Two components whose typical positions differ by less than this are
# indistinguishable - the graph refuses to order them rather than coin-flip.
RANK_MARGIN = 0.08


class Topology:
    def __init__(self) -> None:
        self.nodes: dict[str, int] = {}
        self.edges: dict[tuple[str, str], int] = {}
        # median normalized first-appearance position per component (0 = run
        # start). Sequence EDGES turned out to be the wrong upstream signal:
        # conversational components alternate (api -> tts -> api...), making
        # the transition graph fully cyclic and every node reachable from
        # every other. WHERE a component first enters a run is stable.
        self.rank: dict[str, float] = {}
        self._cooccur: dict[str, set[str]] = {}

    # -- mining --------------------------------------------------------------

    @classmethod
    def mine(cls, traces: dict[str, list[Any]]) -> "Topology":
        topo = cls()
        first_positions: dict[str, list[float]] = defaultdict(list)
        for events in traces.values():
            if not events:
                continue
            previous = None
            seen_at: dict[str, float] = {}
            for index, event in enumerate(events):
                comp = component_of(event.text_redacted.splitlines()[0],
                                    fallback=event.service or "app")
                topo.nodes[comp] = topo.nodes.get(comp, 0) + 1
                if comp not in seen_at:
                    seen_at[comp] = index / max(len(events) - 1, 1)
                if previous is not None and previous != comp:
                    topo.edges[(previous, comp)] = topo.edges.get((previous, comp), 0) + 1
                previous = comp
            for comp, position in seen_at.items():
                first_positions[comp].append(position)
            trace_comps = set(seen_at)
            for comp in trace_comps:
                topo._cooccur.setdefault(comp, set()).update(trace_comps - {comp})
        for comp, positions in first_positions.items():
            ordered = sorted(positions)
            topo.rank[comp] = ordered[len(ordered) // 2]
        return topo

    # -- queries -------------------------------------------------------------

    def is_upstream(self, a: str, b: str) -> bool:
        """a consistently enters runs before b, by more than the margin."""
        if a not in self.rank or b not in self.rank:
            return False
        return self.rank[a] + RANK_MARGIN < self.rank[b]

    def most_upstream(self, components: Iterable[str]) -> str | None:
        """The doc's ranked-cause rule: the member every other member sits
        downstream of. None when the margins cannot separate a unique winner -
        an honest 'cannot say' rather than a coin flip."""
        candidates = [c for c in set(components) if c in self.rank]
        if len(candidates) < 2:
            return candidates[0] if candidates else None
        ordered = sorted(candidates, key=lambda c: self.rank[c])
        if self.rank[ordered[1]] - self.rank[ordered[0]] <= RANK_MARGIN:
            return None
        return ordered[0]

    def blast_radius(self, component: str) -> dict[str, Any]:
        """What runs after this component in the runs it takes part in - what
        is starved if it fails."""
        later = sorted(c for c in self._cooccur.get(component, set())
                       if self.is_upstream(component, c))
        earlier = sorted(c for c in self._cooccur.get(component, set())
                         if self.is_upstream(c, component))
        return {"component": component, "known": component in self.nodes,
                "downstream": later, "upstream": earlier}

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": [{"component": c, "events": n}
                      for c, n in sorted(self.nodes.items(), key=lambda kv: -kv[1])],
            "edges": [{"from": a, "to": b, "count": n}
                      for (a, b), n in sorted(self.edges.items(), key=lambda kv: -kv[1])],
        }

    def __len__(self) -> int:
        return len(self.nodes)
