"""Which uncovered trips a chain could ever reach — not just which have a
candidate today.

Accepting a link turns its demand trip into new supply, which can unlock
further links. The suggester only ever shows the first hop, so its candidate
list badly understates what is in play: a trip with no candidate right now may
open up once earlier links are accepted. Without this, the board cannot tell
"waiting on an earlier decision" apart from "no chain will ever reach this",
and every uncovered trip looks equally stuck.

Contention is deliberately ignored: this asks whether ANY chain path reaches
the trip, not whether the fleet has a spare bus to walk it. That makes the
negative answer the sound one — a trip left out is unreachable no matter what
order a person accepts things in, or how large the fleet gets. How much of the
reachable set the fleet can actually serve is a capacity question, answered by
the fleet warnings (rule 5), not here.

Repositioning is deliberately ignored too, and here that costs the superset
property: `plan` may cover trips this set leaves out, because the planner is
allowed to spend an empty run and this walk is not. Allowing them here would
make almost every trip reachable — an empty run answers "wrong city" by
definition — and a signal that is always true tells a planner nothing. So this
stays the stronger claim: reachable WITHOUT spending an empty run.

The walk is over (trip, company) pairs rather than trips, because a trip
reachable by two companies can satisfy different Y/N constraints downstream;
collapsing to trips alone would invent false dead ends.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from ..model import Trip, TripId
from .graph import MAX_GAP_DAYS, MIN_GAP_DAYS


def _company_ok(company: str, demand: Trip) -> bool:
    # Same rule as graph._constraint_ok, but keyed on a company rather than a
    # supply trip: here the carrier is hypothetical, not yet assigned.
    if demand.yn == "Y":
        return company == demand.constraint_company
    if demand.yn == "N":
        return company != demand.constraint_company
    return True


def reachable_demands(
    supply_trips: Iterable[Trip],
    demand_trips: Iterable[Trip],
) -> frozenset[TripId]:
    """Uncovered trips some sequence of accepts could chain into."""
    by_departure: dict[str, list[Trip]] = defaultdict(list)
    for d in demand_trips:
        by_departure[d.dep_city].append(d)

    # trip -> companies that could be operating it at its arrival
    served: dict[TripId, set[str]] = defaultdict(set)
    queue: list[tuple[Trip, str]] = []
    for t in supply_trips:
        a = t.assignment
        assert a is not None and a.unit is not None
        if a.company not in served[t.id]:
            served[t.id].add(a.company)
            queue.append((t, a.company))

    reached: set[TripId] = set()
    while queue:
        trip, company = queue.pop()
        for d in by_departure.get(trip.arr_city, ()):
            # Same-city walk by construction (keyed on trip.arr_city), so the
            # same-city window is the right one — the pair-dependent floors in
            # graph.min_gap_days only apply once an empty run is in play, which
            # this walk excludes on purpose.
            gap = (d.start - trip.end).days
            if not MIN_GAP_DAYS <= gap <= MAX_GAP_DAYS:
                continue
            if company in served[d.id] or not _company_ok(company, d):
                continue
            served[d.id].add(company)
            reached.add(d.id)
            queue.append((d, company))

    return frozenset(reached)
