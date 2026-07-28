"""Feasible-link generation: the trips-and-links graph.

The edge list produced here IS the optimization model: v1 ranks it greedily
(suggest.py), and a future min-cost-flow/ILP solver consumes the identical
edges plus a cost function — no model change required (brief rule 8).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from ..model import Trip

# Rule 1: a unit arriving in C on day D can take a trip departing C on
# D+1..D+4 (max 3 rest days). Same-day handover (gap 0) is invalid.
MIN_GAP_DAYS = 1                                # same city
MAX_REST_DAYS = 3
MAX_GAP_DAYS = MIN_GAP_DAYS + MAX_REST_DAYS     # same city

# Driving the bus empty to a DIFFERENT city eats days before it can depart, on
# top of the turnaround. Two clear days is the norm; the pairs below are close
# enough to manage on one. Both directions — a frozenset key makes the pair
# unordered so the table cannot disagree with itself.
#
# NOT transitive: CPH-BGO is two short hops via OSL but is not itself short, so
# it takes the default. Adding a pair here is a claim about the map, and the
# source sheet carries no distances to derive it from — hence a literal here
# rather than a registry field for a user to tune.
_ONE_CLEAR_DAY_PAIRS = frozenset({
    frozenset({"CPH", "OSL"}),
    frozenset({"CPH", "STO"}),
    frozenset({"OSL", "STO"}),
    frozenset({"OSL", "BGO"}),
})
_NEAR_GAP_DAYS = 2                              # one clear day in between
_FAR_GAP_DAYS = 3                               # two clear days in between


def min_gap_days(from_city: str, to_city: str) -> int:
    """Earliest departure offset for a unit whose last trip ended in from_city.

    A unit ending in STO on the 20th may depart CPH on the 22nd (gap 2, one
    clear day) but Hamburg only on the 23rd (gap 3, two clear days).
    """
    if from_city == to_city:
        return MIN_GAP_DAYS
    if frozenset({from_city, to_city}) in _ONE_CLEAR_DAY_PAIRS:
        return _NEAR_GAP_DAYS
    return _FAR_GAP_DAYS


def window_rules() -> dict[str, object]:
    """Rule 1's window as plain data, for a client that has to explain it.

    The UI labels a gap in *free days* and has to name how many of them an
    empty run spends driving rather than parked — both answers come from
    min_gap_days, so the pair table has to reach the browser. Serving it keeps
    this module the single source of truth; a copy in JS would be a second
    place for the map to be wrong, and an explanation quoting a window the
    engine did not apply is worse than no explanation.
    """
    return {
        "min_gap_days": MIN_GAP_DAYS,
        "max_rest_days": MAX_REST_DAYS,
        "near_gap_days": _NEAR_GAP_DAYS,
        "far_gap_days": _FAR_GAP_DAYS,
        "near_pairs": sorted(sorted(pair) for pair in _ONE_CLEAR_DAY_PAIRS),
    }


@dataclass(frozen=True)
class Edge:
    supply: Trip                # numbered-unit assigned trip whose arrival frees the bus
    demand: Trip                # uncovered trip the unit could take next
    gap_days: int
    # The bus drives empty from supply.arr_city to demand.dep_city first. Rule 6
    # already recognizes this move as an explicit ER; what is new is proposing
    # one rather than only honoring the ones a human wrote down.
    reposition: bool = False


def _constraint_ok(supply: Trip, demand: Trip) -> bool:
    # Rule 3, company-level: Y + X -> only X may serve; N + X -> X never.
    if demand.yn == "Y":
        return supply.assignment.company == demand.constraint_company  # type: ignore[union-attr]
    if demand.yn == "N":
        return supply.assignment.company != demand.constraint_company  # type: ignore[union-attr]
    return True


def _pair_chainable(earlier: Trip, later: Trip) -> bool:
    gap = (later.start - earlier.end).days
    return (MIN_GAP_DAYS <= gap <= MAX_GAP_DAYS
            and earlier.arr_city == later.dep_city)


def _unit_timeline_ok(demand: Trip, supply: Trip, unit_trips: Sequence[Trip]) -> bool:
    """Accepting (supply -> demand) must keep the unit's whole timeline valid
    (rule 6): no overlap, supply stays the immediate predecessor, and any
    existing successor still chains from the demand trip (an ER-marked
    successor is exempt — its explicit repositioning covers the move).
    """
    # Trips already overlapping the supply trip are pre-existing integrity
    # violations (e.g. a typo'd start year spanning twelve months in the
    # reference data). They are enumerated by the integrity check for a human
    # to resolve — letting them veto every future link would freeze the whole
    # unit on account of one bad row.
    coherent = [
        t for t in unit_trips
        if t.id != supply.id
        and not (t.start <= supply.end and t.end >= supply.start)
    ]
    for t in coherent:
        if t.start <= demand.end and t.end >= demand.start:
            return False                            # overlap
        if supply.end < t.start < demand.start:
            return False                            # a trip already sits in the gap
    # An existing successor may only block the insert if it currently chains
    # validly from the supply trip and the insert would break that. If the
    # pair was already non-chaining (an enumerated integrity gap in the
    # source data), inserting the demand trip cannot make the timeline worse
    # — it typically shrinks the existing gap.
    successors = [t for t in coherent if t.start > demand.end]
    if successors:
        nxt = min(successors, key=lambda t: t.start)
        was_valid = nxt.er_marked or _pair_chainable(supply, nxt)
        still_valid = nxt.er_marked or _pair_chainable(demand, nxt)
        if was_valid and not still_valid:
            return False
    return True


def feasible_edges(
    supply_trips: Iterable[Trip],
    demand_trips: Iterable[Trip],
    unit_assignments: Mapping[tuple[str, int], Sequence[Trip]],
    reposition: bool = False,
) -> list[Edge]:
    """All (supply, demand) links satisfying rules 1-3 and 6.

    unit_assignments: non-canceled assigned trips per numbered unit — the
    timeline the accepted link must remain consistent with.

    reposition: also emit links where the bus drives empty to the departure
    city, allowed ONLY off the tail of a unit's chain. Mid-chain it would strand
    whatever comes next in the wrong city — _unit_timeline_ok would reject most
    of those anyway, but the tail restriction says why rather than relying on a
    downstream check to catch it. Off by default: rules 1-3 and 6 are the
    contract every other caller reads, and adding empty runs to them silently
    would change what `reachable` and the integrity report mean.
    """
    demands = list(demand_trips)
    edges: list[Edge] = []
    for s in supply_trips:
        assert s.assignment is not None and s.assignment.unit is not None
        unit_key = (s.assignment.company, s.assignment.unit)
        unit_trips = unit_assignments.get(unit_key, ())
        # A bus is free to be sent somewhere else only once nothing follows it.
        is_tail = all(t.end <= s.end for t in unit_trips)
        for d in demands:
            moved = s.arr_city != d.dep_city
            if moved and not (reposition and is_tail):
                continue
            # Window floor depends on the pair: the empty run has to fit inside
            # the gap. The ceiling rides on the floor so MAX_REST_DAYS keeps
            # meaning the same thing — three days parked — whether or not the
            # bus had to drive to get there.
            lo = min_gap_days(s.arr_city, d.dep_city)
            gap = (d.start - s.end).days
            if not lo <= gap <= lo + MAX_REST_DAYS:
                continue
            if not _constraint_ok(s, d):
                continue
            if not _unit_timeline_ok(d, s, unit_trips):
                continue
            edges.append(Edge(supply=s, demand=d, gap_days=gap, reposition=moved))
    return edges
