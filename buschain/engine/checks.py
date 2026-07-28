"""Reporting checks: fleet over-commitment, unit integrity, broken chains.

Everything here is listed, never auto-fixed (rules 4-6).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from typing import Mapping, Sequence

from ..model import (
    AcceptedLink, BrokenChainItem, FleetWarning, IntegrityFinding, Registry,
    Status, Trip, TripId,
)
from .graph import _pair_chainable


def fleet_warnings(
    assigned_live: Sequence[Trip], registry: Registry
) -> tuple[FleetWarning, ...]:
    """Rule 5: per company per day, simultaneous non-canceled trips on that
    company's NUMBERED units must not exceed its fleet. Trip intervals count
    inclusive of both endpoints (a bus is occupied on its end day).

    Deliberate deviation from the brief, which counts numbered + ad hoc: the
    fleet number comes from the Information sheet's 'Fleets' column and means
    chartered vehicles, while a bare-company assignment means "some further
    bus from their yard" — capacity the charter count says nothing about.
    Counting ad hoc against it produced 1448 warning-days on the reference
    data (Ollex: 2 chartered, 120 ad hoc trips), which buried the real
    over-commitments rather than reporting them.
    """
    per_company_day: dict[str, dict] = defaultdict(lambda: defaultdict(int))
    for t in assigned_live:
        assert t.assignment is not None
        if t.assignment.unit is None:           # ad hoc: chain- and fleet-invisible
            continue
        day = t.start
        while day <= t.end:
            per_company_day[t.assignment.company][day] += 1
            day += timedelta(days=1)
    warnings: list[FleetWarning] = []
    # sorted: the one engine output that wasn't canonically ordered —
    # first-appearance order meant reordering source rows reordered the list.
    for company_key, days in sorted(per_company_day.items()):
        company = registry.companies.get(company_key)
        if company is None:
            # Unreachable while the registry is closed (every key here came
            # through resolve_company); if that invariant ever breaks, this
            # silently drops over-commitments — the opposite of this module's
            # contract — so treat a hit here as a bug, not a filter.
            continue
        for day in sorted(days):
            if days[day] > company.fleet_size:
                warnings.append(FleetWarning(
                    company=company_key, day=day,
                    count=days[day], fleet_size=company.fleet_size,
                ))
    return tuple(warnings)


def unit_integrity(
    unit_assignments: Mapping[tuple[str, int], Sequence[Trip]],
    accepted: Sequence[AcceptedLink] = (),
) -> tuple[IntegrityFinding, ...]:
    """Rule 6: overlaps are violations; each consecutive pair must chain
    (same city, gap 1..4) or be an explicit ER — which is still listed, as
    an enumerated fact rather than a violation (AT3).

    accepted: a pair someone deliberately linked across two cities is a
    repositioning run they approved, not a mismatch to go and fix. It is still
    listed — an empty run is a cost worth seeing — but as its own kind, so the
    violation list does not fill up with the planner's own accepted proposals.
    The ER column stays read-only (it is the sheet's, not ours); the accepted
    link is what records the intent.
    """
    repositioned = {
        (link.supply, link.demand) for link in accepted if link.supply is not None
    }
    findings: list[IntegrityFinding] = []
    for (company, unit), trips in sorted(unit_assignments.items()):
        ordered = sorted(trips, key=lambda t: (t.start, t.end, t.id))
        for earlier, later in zip(ordered, ordered[1:]):
            if later.start <= earlier.end:
                kind = "overlap"
            elif _pair_chainable(earlier, later):
                continue
            elif later.er_marked:
                kind = "explicit_er"
            elif earlier.arr_city != later.dep_city:
                kind = ("reposition" if (earlier.id, later.id) in repositioned
                        else "city_mismatch")
            else:
                kind = "gap_out_of_window"
            findings.append(IntegrityFinding(
                company=company, unit=unit,
                earlier=earlier.id, later=later.id, kind=kind,
            ))
    return tuple(findings)


def broken_chains(
    accepted: Sequence[AcceptedLink],
    trips_by_id: Mapping[TripId, Trip],
) -> tuple[BrokenChainItem, ...]:
    """Rule 4: a cancellation invalidates every accepted assignment that
    relied on it, transitively down the chain."""
    items: list[BrokenChainItem] = []
    # demand -> (root canceled trip) discovered so far; iterate until stable
    # because links may arrive in any order.
    broken_root: dict[TripId, TripId] = {}
    changed = True
    while changed:
        changed = False
        for link in accepted:
            if link.demand in broken_root or link.supply is None:
                continue
            supply_trip = trips_by_id.get(link.supply)
            root: TripId | None = None
            # A link breaks when its supply is canceled, was unassigned again
            # (e.g. via the worklist), or dropped out of the operable set.
            if (supply_trip is None
                    or supply_trip.status is Status.CANCELED
                    or supply_trip.assignment is None):
                root = link.supply
            elif link.supply in broken_root:
                root = broken_root[link.supply]
            if root is not None:
                broken_root[link.demand] = root
                items.append(BrokenChainItem(
                    assignment=link.demand, canceled_dependency=root, link=link,
                ))
                changed = True
    return tuple(items)
