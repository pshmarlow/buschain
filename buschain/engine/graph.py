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


def company_ok(company: str, demand: Trip) -> bool:
    """Rule 3, company-level: Y + X -> only X may serve; N + X -> X never.

    Takes the company rather than the supply trip because the same rule governs
    a bus with no supply to chain from — an idle unit, or an ad hoc lane. One
    reading of rule 3 for every way a trip can be given to a company.
    """
    if demand.yn == "Y":
        return company == demand.constraint_company
    if demand.yn == "N":
        return company != demand.constraint_company
    return True


def _constraint_ok(supply: Trip, demand: Trip) -> bool:
    return company_ok(supply.assignment.company, demand)  # type: ignore[union-attr]


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
    # violations. They are enumerated by the integrity check for a human to
    # resolve — letting them veto every future link would freeze whole units on
    # account of bad rows the planner cannot fix.
    #
    # The case this was written for, a typo'd start year spanning twelve
    # months, is now caught upstream (contract.MAX_TRIP_SPAN_DAYS), which took
    # the reference data's overlap findings from 4 to 0. The branch stays for
    # the overlaps that reach here by other routes, but nothing in the fixture
    # exercises it any more — test_links.py covers it directly instead.
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


@dataclass(frozen=True)
class Insertion:
    """A place a trip may physically go on one unit's timeline.

    chains says whether the predecessor pair is a rule-1 handover, and so
    whether the acceptance should record a link: a dragged trip that lands in
    the window keeps exactly the provenance an accepted suggestion would, and
    one that lands a fortnight later records the assignment alone rather than
    manufacturing a chain nobody could run.

    gap_days and successor_gap are the raw day counts on either side, for a
    caller that has to explain the placement. Both are None when there is no
    trip on that side.
    """
    predecessor: Trip | None
    gap_days: int | None
    successor_gap: int | None
    reposition: bool = False
    chains: bool = False


def insertion_ok(
    demand: Trip,
    unit_trips: Sequence[Trip],
    company: str,
    reposition: bool = False,
) -> Insertion | None:
    """May this unit take this trip? The insert form of feasible_edges.

    feasible_edges answers the planner's question — "which trip can this bus
    pick up NEXT" — by walking supply trips, so a unit with nothing to hand off
    (idle, or the trip would land ahead of its first) produces no edge at all.
    That is the right answer for a chain proposal and the wrong one for a
    person dropping a trip onto a bus.

    The line drawn here is IMPOSSIBLE vs MERELY SUBOPTIMAL, which is the line
    the brief draws too: acceptance criterion 3 asks that every consecutive
    pair on a unit satisfy the rules **or be enumerated**. So this refuses only
    what no bus could do —

      * running two trips at once (overlap), and
      * departing before it could have arrived, empty run included
        (min_gap_days), and
      * a company the trip's Y/N constraint forbids, which rule 3 makes hard.

    — and permits the rest, leaving rule 1's three-rest-day ceiling to
    checks.unit_integrity, which enumerates the result exactly as it already
    does for the source data. That is not a loosening invented for the UI: on
    the reference workbook 34 of 183 consecutive pairs are already wider than
    the window, so a drop that refused them would refuse roughly a fifth of
    what the sheet itself records, on rows a person is looking straight at.

    The narrower question — is this pair a chain the planner would propose? —
    is answered too, as `chains`, and that is what decides whether a link gets
    recorded. test_links.py pins the two functions together: every edge
    feasible_edges emits must be an insertion this allows, with the same
    supply, gap and ER flag.

    reposition governs link recording only, never legality: a bus with a week
    to spare can be driven to another city without anyone approving an empty
    run, whereas turning empty runs off must stop this from writing one.

    Returns None when the unit may not take the trip; insertion_verdict gives
    the same answer with the reason attached.
    """
    return insertion_verdict(demand, unit_trips, company, reposition)[0]


def insertion_verdict(
    demand: Trip,
    unit_trips: Sequence[Trip],
    company: str,
    reposition: bool = False,
) -> tuple[Insertion | None, str | None]:
    """insertion_ok, plus why not — exactly one of the two is set.

    The reason travels because the board refuses drops, and a refusal that
    cannot say which rule it is applying is the kind every other control here
    avoids: the manual pickers name the rule they are breaking in the button
    itself. Phrased for a person looking at two bars, so it says which trip is
    in the way rather than which predicate returned false.
    """
    if not company_ok(company, demand):
        return None, (
            f"the trip names {'only' if demand.yn == 'Y' else 'anyone but'} "
            f"{demand.constraint_company} — a hard constraint (rule 3)")

    others = [t for t in unit_trips if t.id != demand.id]
    for t in others:
        if t.start <= demand.end and t.end >= demand.start:
            return None, (f"this bus is already running {t.dep_city}→{t.arr_city} "
                          f"({t.start} → {t.end}) over those days")
    before = [t for t in others if t.end < demand.start]
    after = [t for t in others if t.start > demand.end]
    pred = max(before, key=lambda t: t.end) if before else None
    succ = min(after, key=lambda t: t.start) if after else None

    gap = succ_gap = None
    moved = chains = False
    if pred is not None:
        moved = pred.arr_city != demand.dep_city
        lo = min_gap_days(pred.arr_city, demand.dep_city)
        gap = (demand.start - pred.end).days
        if gap < lo:
            return None, (
                f"the bus lands in {pred.arr_city} on {pred.end} and cannot be in "
                f"{demand.dep_city} by {demand.start} — that move needs {lo} day"
                f"{'s' if lo != 1 else ''}, and there {'is' if gap == 1 else 'are'} "
                f"{gap}")
        # The window's ceiling decides whether this is a chain, not whether it
        # is allowed. An empty run inside the window is one someone has to
        # approve (rule 6), so the setting gates the link.
        chains = gap <= lo + MAX_REST_DAYS and (reposition or not moved)
    if succ is not None:
        succ_gap = (succ.start - demand.end).days
        need = min_gap_days(demand.arr_city, succ.dep_city)
        # Refuse only if the insert is what STRANDS the successor. If the bus
        # already could not reach it — a pre-existing integrity finding, and
        # the source data has them — then blocking here would freeze the unit
        # over a row nobody dragging a bar can fix. Same tolerance
        # _unit_timeline_ok extends, measured against the physical floor rather
        # than the whole window. An ER-marked successor carries its own
        # approved empty run and is exempt either way (rule 6).
        was_reachable = succ.er_marked or pred is None or (
            (succ.start - pred.end).days >= min_gap_days(pred.arr_city, succ.dep_city))
        if was_reachable and not succ.er_marked and succ_gap < need:
            return None, (
                f"the bus is due in {succ.dep_city} on {succ.start} and this trip "
                f"leaves it in {demand.arr_city} on {demand.end} — that move needs "
                f"{need} day{'s' if need != 1 else ''}, and there "
                f"{'is' if succ_gap == 1 else 'are'} {succ_gap}")

    return Insertion(predecessor=pred, gap_days=gap, successor_gap=succ_gap,
                     reposition=moved, chains=chains), None


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
