"""At-risk dependency tracing (rule 4).

A suggested link is only as firm as the supply trip it hangs off and every
accepted link upstream of it: any Pending trip in that chain can cancel and
break the plan. Suggestions surface those trips so the human sees the risk
before accepting.
"""
from __future__ import annotations

from typing import Mapping, Sequence

from ..model import AcceptedLink, Status, Trip, TripId


def supply_map(accepted: Sequence[AcceptedLink]) -> dict[TripId, TripId | None]:
    """demand -> its accepted supply. Built once by the caller: at_risk_chain
    runs per edge, and rebuilding this dict there made annotation O(edges x
    accepted links) for a mapping that never changes within a call."""
    return {link.demand: link.supply for link in accepted}


def at_risk_chain(
    supply: Trip,
    trips_by_id: Mapping[TripId, Trip],
    supply_of: Mapping[TripId, TripId | None],
) -> tuple[TripId, ...]:
    risky: list[TripId] = []
    seen: set[TripId] = set()
    current: TripId | None = supply.id
    while current is not None and current not in seen:
        seen.add(current)
        trip = trips_by_id.get(current)
        if trip is not None and trip.status is Status.PENDING:
            risky.append(current)
        current = supply_of.get(current)
    return tuple(risky)
