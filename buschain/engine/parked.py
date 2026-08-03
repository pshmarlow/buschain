"""Parked mid-chain windows: idle blocks the planner cannot see into.

feasible_edges allows a cross-city move only off the TAIL of a unit's chain
(graph.py says why: mid-chain it could strand the successor). The cost of that
rule is invisibility: a unit whose last committed trip is months out never has
a tail, so a weeks-long idle block in the middle of its timeline offers the
planner nothing. Measured on live data, one unit sat parked 56 days behind a
single far-future source-file assignment while six uncovered trips fit inside
the block — every one already a legal manual drop (insertion_ok allows them);
only the automatic planner abstains.

This module makes those blocks visible rather than plannable. Widening the
planner instead was considered and measured: ~1 extra covered trip, at the
price of auto-proposing DOUBLE empty runs (out and back) the model cannot
price — against the stated policy that an unpriced empty run is spent only on
demand nothing else could serve (plan.py). Listing the options keeps rule 6's
intent (approving an empty run is a person's call) and touches no planner
rule: insertion_ok stays the ONE gate that says whether a placement is legal,
and nothing here restates it.
"""
from __future__ import annotations

from typing import Iterable, Mapping, Sequence

from ..model import ParkedFit, ParkedWindow, Trip
from .graph import MAX_REST_DAYS, insertion_ok, min_gap_days


def windows(
    demand_trips: Iterable[Trip],
    unit_assignments: Mapping[tuple[str, int], Sequence[Trip]],
    reposition: bool = True,
) -> tuple[ParkedWindow, ...]:
    """Consecutive committed pairs wider than any chain window, each with the
    uncovered trips that would fit inside it.

    Wider than min_gap_days(pair) + MAX_REST_DAYS is the exact complement of
    the ceiling feasible_edges applies, so a window never overlaps territory
    the planner already works. Tails are excluded by construction (no closing
    trip): repositioning off a tail is the planner's own business. Zero-fit
    windows are still reported — a bus parked for weeks is worth seeing.

    A fit must pass insertion_ok with the window's opening trip as immediate
    predecessor. Fits the planner already emits as edges — same-city AND
    inside the chain window — are not repeated here; what remains is exactly
    what it cannot propose (a cross-city entry, or a same-city one deeper in
    the window). `reposition` is the same switch evaluate passes everywhere
    else and, as in insertion_ok, it gates only the `chains` flag (whether a
    drop would record a link), never whether the fit is listed.
    """
    demands = sorted(demand_trips, key=lambda t: (t.start, t.end, t.id))
    out: list[ParkedWindow] = []
    for (company, unit), trips in sorted(unit_assignments.items()):
        ordered = sorted(trips, key=lambda t: (t.start, t.end, t.id))
        for opener, closer in zip(ordered, ordered[1:]):
            days = (closer.start - opener.end).days
            if days <= min_gap_days(opener.arr_city, closer.dep_city) + MAX_REST_DAYS:
                continue
            fits: list[ParkedFit] = []
            for d in demands:
                if not (opener.end < d.start and d.end < closer.start):
                    continue
                moved = opener.arr_city != d.dep_city
                lo = min_gap_days(opener.arr_city, d.dep_city)
                gap_out = (d.start - opener.end).days
                if not moved and lo <= gap_out <= lo + MAX_REST_DAYS:
                    continue            # a feasible edge — the planner has it
                spot = insertion_ok(d, ordered, company, reposition)
                # The predecessor check matters on messy source data: a trip
                # overlapping the opener can end later and become the real
                # predecessor, in which case d is not in THIS window at all.
                if (spot is None or spot.predecessor is None
                        or spot.predecessor.id != opener.id):
                    continue
                fits.append(ParkedFit(
                    demand=d.id, gap_out=gap_out,
                    er_out_days=lo if moved else 0,
                    er_back_days=(0 if closer.er_marked
                                  or d.arr_city == closer.dep_city
                                  else min_gap_days(d.arr_city, closer.dep_city)),
                    chains=spot.chains,
                ))
            fits.sort(key=lambda f: (f.er_out_days + f.er_back_days,
                                     f.gap_out, f.demand))
            out.append(ParkedWindow(
                company=company, unit=unit, after=opener.id, before=closer.id,
                city=opener.arr_city, days=days, fits=tuple(fits)))
    return tuple(out)
