"""Multi-hop chain construction: the plan the one-hop suggester cannot state.

suggest.py ranks edges out of trips that are *already* assigned, so it can only
ever describe the next hop. A demand trip becomes supply the moment someone
accepts it, which unlocks further links — but nothing computes that ahead of
time, so a planner sees a handful of loose candidates instead of the chains
they belong to.

This module runs the same feasible-link graph forward: take edges, apply them
provisionally, let the newly covered trips act as supply, repeat. Nothing here
touches the store; the result is derived state recomputed on every evaluate(),
which is what keeps provisional links out of cell_overrides and therefore out
of the xlsx export.

Ceiling note: the fleet binds long before the algorithm does. reach.py reports
far more trips as "reachable", but it deliberately ignores contention — a trip
one chain could touch is not a trip a free bus exists for. Expect the plan to
cover a fraction of the reachable set.
"""
from __future__ import annotations

import dataclasses
from collections import defaultdict
from typing import Callable, Iterable, Literal, Sequence

from ..model import Plan, PlannedLink, ProposedChain, Trip, TripId
from .graph import Edge, feasible_edges, min_gap_days

Objective = Literal["coverage", "idle"]

# Ranking keys. Both end in the same total-order tiebreakers so a fixed input
# and objective always yield an identical plan — the store re-runs this on
# every mutation and a plan that reshuffled on its own would be unreadable.
#
# `reposition` leads both keys, so every link that needs no empty run is placed
# before any link that does. An empty run is a real cost the model cannot price
# (there are no distances in the registry), so the only defensible policy is to
# spend one solely on demand nothing else could serve. Ordering is only half of
# that; the deferral in build_plan is the other half — see the comment there for
# why the sort alone leaks empty runs onto demands that had a free option.
#
# Then shortest gap, then earliest finish — classic interval scheduling: among
# equally cheap turnarounds, take the trip that hands the bus back soonest,
# because that bus is the next link's supply.
#
# This replaced a most-constrained-demand-first key (scored by how many
# supplies could serve each demand that round). That heuristic was measurably
# better before empty runs existed and measurably worse after: repositioning
# gives almost every demand a candidate, so the scarcity signal flattens out
# while the cost of tying a bus up for a long gap does not. Measured over both
# datasets, constrained-first covered 46/131 and 100/190; this key covers
# 51/131 and 117/190.
#
# The gap term is raw gap_days, per the brief — a bus mid-empty-run is out of
# service just as surely as one standing in a yard, so the link that hands it
# back soonest wins whether the days went to driving or waiting. Note this is
# deliberately NOT the same accounting as Plan.idle_days below, which excludes
# travel: the metric answers "how much time was wasted", the ranking answers
# "which bus is free first". Both questions are real and they differ.
def _key_coverage(edge: Edge) -> tuple:
    return (edge.reposition, edge.gap_days, edge.demand.end,
            edge.demand.id, edge.supply.id)


def _key_idle(edge: Edge) -> tuple:
    # suggest.py's own ranking: shortest turnaround wins.
    return (edge.reposition, edge.gap_days, edge.demand.start,
            edge.demand.id, edge.supply.id)


_KEYS: dict[str, Callable[[Edge], tuple]] = {
    "coverage": _key_coverage,
    "idle": _key_idle,
}

DEFAULT_OBJECTIVE: Objective = "coverage"


def build_plan(
    supply_trips: Iterable[Trip],
    demand_trips: Iterable[Trip],
    unit_assignments: dict[tuple[str, int], list[Trip]],
    objective: Objective = DEFAULT_OBJECTIVE,
    reposition: bool = True,
) -> Plan:
    """Chains the fleet could run, given what is already assigned.

    Callers pass the same three things feasible_edges takes; this owns the
    iteration. Nothing is written — every assignment made here is a local
    dataclasses.replace on a frozen Trip, discarded when the call returns.
    """
    rank = _KEYS[objective]

    supply = list(supply_trips)
    remaining = {t.id: t for t in demand_trips}
    # Local mutable copy: the caller's mapping describes committed state and
    # must not gain provisional trips.
    units: dict[tuple[str, int], list[Trip]] = {
        key: list(trips) for key, trips in unit_assignments.items()
    }

    links: list[PlannedLink] = []
    step: dict[tuple[str, int], int] = defaultdict(int)

    while remaining:
        edges = feasible_edges(supply, list(remaining.values()), units, reposition)
        if not edges:
            break
        edges.sort(key=rank)

        # Empty runs are a LAST RESORT, and the ranking alone does not deliver
        # that. Sorting puts every same-city edge first, but the one-per-unit
        # rule below can consume a demand's same-city supply on an earlier,
        # better-ranked demand — and the loop then reaches that demand with only
        # its repositioning edge still usable and spends one. Measured on the
        # reference data: 10 of 26 empty runs went to demands that had a
        # same-city option in that very round.
        #
        # So defer instead. The demand waits a round rather than paying for a
        # drive, and the bus the empty run would have tied up stays free for
        # work only it can do. That is why deferring COVERS MORE rather than
        # less: coverage 108 -> 113 links on 26 -> 25 empty runs, idle
        # 101 -> 109 on 25 -> 25.
        #
        # A demand whose same-city supply is taken every round is never covered
        # here, where before it would have been given an empty run. That is the
        # trade, and the measurements above are it being worth taking. The
        # `progressed` guard below still terminates: a round in which every
        # placeable edge is deferred places nothing and breaks.
        deferrable = {e.demand.id for e in edges if not e.reposition}

        # At most one edge per unit per round. Every edge was validated against
        # its unit's timeline as it stood at round start, so a second edge for
        # the same unit is checked against stale state and can land two
        # overlapping trips on one bus. Deferring it costs a round; the next
        # pass re-validates it against the timeline it will actually face.
        used: set[tuple[str, int]] = set()
        progressed = False
        for edge in edges:
            if edge.demand.id not in remaining:
                continue
            if edge.reposition and edge.demand.id in deferrable:
                continue
            assignment = edge.supply.assignment
            assert assignment is not None and assignment.unit is not None
            key = (assignment.company, assignment.unit)
            if key in used:
                continue

            planned = dataclasses.replace(edge.demand, assignment=assignment)
            supply.append(planned)
            units.setdefault(key, []).append(planned)
            del remaining[edge.demand.id]
            used.add(key)
            progressed = True

            links.append(PlannedLink(
                supply=edge.supply.id, demand=edge.demand.id,
                company=assignment.company, unit=assignment.unit,
                gap_days=edge.gap_days, step=step[key],
                reposition=edge.reposition,
                from_city=edge.supply.arr_city, to_city=edge.demand.dep_city,
            ))
            step[key] += 1

        # Belt-and-braces: with the current rules this cannot fire — the
        # first edge in sorted order is always placeable (`used` is fresh,
        # and if it is a reposition edge then nothing was deferrable), so
        # every round with edges shrinks `remaining` by at least one, which
        # is the real termination argument. The guard stays so a future rule
        # that CAN block a whole round degrades to a break, not a spin.
        if not progressed:
            break

    return _assemble(links, objective)


def _assemble(links: Sequence[PlannedLink], objective: str) -> Plan:
    by_unit: dict[tuple[str, int], list[PlannedLink]] = defaultdict(list)
    for link in links:
        by_unit[(link.company, link.unit)].append(link)

    chains = tuple(
        ProposedChain(
            company=company, unit=unit,
            links=tuple(sorted(unit_links, key=lambda link: link.step)),
        )
        for (company, unit), unit_links in sorted(by_unit.items())
    )
    return Plan(
        objective=objective,
        chains=chains,
        covered=frozenset(link.demand for link in links),
        # Rest days, not gap: the days a pair's move requires are worked, not
        # idled, so a next-day same-city pickup and a minimum-gap reposition
        # both cost zero. Charging the empty run's travel as idleness would
        # make the idle objective avoid distance rather than avoid waiting.
        idle_days=sum(link.gap_days - min_gap_days(link.from_city, link.to_city)
                      for link in links),
    )


def chain_order(chain: ProposedChain) -> tuple[TripId, ...]:
    """Demand trips in the order they must be accepted.

    Each link's supply has to be assigned before the link itself, so a caller
    committing a whole chain walks it in this order or the second accept has
    no supply to point at.
    """
    return tuple(link.demand for link in chain.links)
