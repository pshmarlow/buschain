"""Greedy ranking policy over feasible edges (rule 9: human-in-the-loop).

This module only orders and annotates candidates — it never assigns. Ranking
is deterministic: no-empty-run before empty-run, then gap ascending (brief),
then fewer at-risk dependencies, then earlier demand start, then TripIds, so
identical inputs always produce identical suggestion lists.
"""
from __future__ import annotations

from typing import Mapping, Sequence

from ..model import AcceptedLink, Suggestion, Trip, TripId
from . import deps
from .graph import Edge


def _sort_key(edge: Edge, at_risk: tuple[TripId, ...]):
    # Repositioning first, so a link needing an empty run can never outrank one
    # that does not — a person reading the list top-down sees the free moves
    # before they are offered a move that costs a bus a day of driving.
    return (edge.reposition, edge.gap_days, len(at_risk), edge.demand.start,
            edge.demand.id, edge.supply.id)


def build_suggestions(
    edges: Sequence[Edge],
    demand_trips: Sequence[Trip],
    trips_by_id: Mapping[TripId, Trip],
    accepted: Sequence[AcceptedLink],
) -> tuple[
    dict[tuple[str, int], tuple[Suggestion, ...]],
    dict[TripId, tuple[Suggestion, ...]],
]:
    supply_of = deps.supply_map(accepted)
    annotated = [
        (edge, deps.at_risk_chain(edge.supply, trips_by_id, supply_of))
        for edge in edges
    ]
    annotated.sort(key=lambda pair: _sort_key(*pair))

    by_unit: dict[tuple[str, int], list[Suggestion]] = {}
    by_trip: dict[TripId, list[Suggestion]] = {}
    for edge, at_risk in annotated:
        a = edge.supply.assignment
        assert a is not None and a.unit is not None
        unit_key = (a.company, a.unit)
        # The same link appears in both directions with its rank within
        # that grouping (per free unit / per uncovered trip).
        for rank_list in (by_unit.setdefault(unit_key, []),
                          by_trip.setdefault(edge.demand.id, [])):
            rank_list.append(Suggestion(
                kind="chain", supply=edge.supply.id, demand=edge.demand.id,
                company=a.company, unit=a.unit, gap_days=edge.gap_days,
                at_risk=at_risk, rank=len(rank_list),
                reposition=edge.reposition,
                from_city=edge.supply.arr_city, to_city=edge.demand.dep_city,
            ))

    # Rule 3 fallback: a Y-constrained trip with no feasible numbered chain
    # gets the ad hoc suggestion of its mandated company (rule 5 / AT5).
    #
    # "No feasible chain" counts only chains that need no empty run. A chain
    # that does is not a reason to withhold the fallback — it is precisely when
    # the planner most needs to see it, since another bus from the same
    # company's yard is the obvious alternative to driving one across the map.
    # Ranked last, so the free options still read first.
    for d in demand_trips:
        existing = by_trip.get(d.id, [])
        if d.yn == "Y" and not any(not s.reposition for s in existing):
            assert d.constraint_company is not None
            existing.append(Suggestion(
                kind="adhoc_fallback", supply=None, demand=d.id,
                company=d.constraint_company, unit=None, gap_days=None,
                at_risk=(), rank=len(existing),
            ))
            by_trip[d.id] = existing

    return (
        {k: tuple(v) for k, v in by_unit.items()},
        {k: tuple(v) for k, v in by_trip.items()},
    )
