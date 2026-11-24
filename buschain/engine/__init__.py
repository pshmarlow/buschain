"""The routing engine: one pure entry point.

evaluate() is a pure function of (contract report, registry, accepted links)
-> EngineResult. It never touches files, databases, or the network, so every
mutation elsewhere (accept, cancel, manual entry) is simply: change state,
re-run evaluate. Full recompute is milliseconds at this data size — no
incremental invalidation logic anywhere.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import AbstractSet, Sequence

from ..model import (
    AcceptedLink, ContractReport, EngineResult, Registry, Scope, Status, Trip,
    TripId,
)
from . import checks, graph, parked, plan as plan_mod, reach, solve, suggest


def evaluate(
    report: ContractReport,
    registry: Registry,
    accepted: Sequence[AcceptedLink] = (),
    objective: plan_mod.Objective = plan_mod.DEFAULT_OBJECTIVE,
    reposition: bool = True,
    locked: AbstractSet[TripId] = frozenset(),
    as_of: date | None = None,
    horizon: date | None = None,
) -> EngineResult:
    """locked: trips the planner must not decide anything about.

    as_of: when given, only trips starting strictly after this date are treated
    as demand. Passed in rather than read from the clock, because a planner that
    consulted date.today() would stop being a pure function of its inputs — the
    same state would produce different plans on different days and no test could
    pin either. The web layer supplies today's date; the engine never asks.

    horizon: the far end of the same window — when given, only trips starting on
    or before this date are demand. It is an absolute DATE, never a "plan ahead
    N days" count, for the reason adhoc.py gives for keeping the cancellation
    notice out of graph.py: a now-relative number loose in a module of
    trip-to-trip predicates is one refactor away from being read as a gap.
    Resolving today + N is the web layer's job, and doing it there is also what
    keeps this function pinnable by a test that names both ends.

    Both default to None (unbounded), which is what the engine did before either
    was settable — so a caller that plans the whole dataset still can, and every
    test that predates the horizon keeps its meaning.
    """
    trips_by_id: dict[TripId, Trip] = {t.id: t for t in report.trips}
    (assigned_live, supply_trips, demand_trips,
     unit_assignments, adhoc_by_company, out_of_scope) = _split(
        report, locked, as_of, horizon)

    # Repositioning defaults on: without it a parked bus offers nothing, which
    # is exactly the state most of the fleet ends up in. The ranking in
    # suggest.py and plan.py puts every same-city link ahead of every empty run,
    # so leaving it on widens what a person CAN do without changing what they
    # are advised to do first.
    #
    # The flag reaches BOTH the one-hop edges and the plan below, deliberately:
    # a ghost slot the board offers but the planner refuses to chain is the same
    # setting disagreeing with itself. One switch, one answer everywhere.
    edges = graph.feasible_edges(
        supply_trips, demand_trips, unit_assignments, reposition)
    by_unit, by_trip = suggest.build_suggestions(edges, demand_trips, trips_by_id, accepted)

    timelines = {
        key: tuple(t.id for t in sorted(ts, key=lambda t: (t.start, t.end, t.id)))
        for key, ts in sorted(unit_assignments.items())
    }

    return EngineResult(
        suggestions_by_unit=by_unit,
        suggestions_by_trip=by_trip,
        integrity=checks.unit_integrity(unit_assignments, accepted),
        fleet_warnings=checks.fleet_warnings(assigned_live, registry),
        broken_chains=checks.broken_chains(accepted, trips_by_id),
        timelines=timelines,
        adhoc_by_company={
            c: tuple(t.id for t in sorted(ts, key=lambda t: (t.start, t.id)))
            for c, ts in sorted(adhoc_by_company.items())
        },
        reachable=reach.reachable_demands(supply_trips, demand_trips),
        # Built from committed state only, so every confirmed assignment acts
        # as immovable supply the chains route around — that is what makes a
        # confirmed link immune to replanning, with no lock flag anywhere.
        plan=plan_mod.build_plan(
            supply_trips, demand_trips, unit_assignments, objective, reposition),
        # Reported, never planned: the idle blocks the tail-only rule hides
        # from the planner. Same reposition switch as everything above.
        parked=parked.windows(demand_trips, unit_assignments, reposition),
        out_of_scope=out_of_scope,
        # The same fact as a number, for the callers that only ever needed
        # one. Derived here from the map so the two cannot disagree.
        beyond_horizon=sum(1 for why in out_of_scope.values() if why == "horizon"),
    )


@dataclass(frozen=True)
class DropTarget:
    """A bus that may take one particular trip, and what that would mean.

    supply is set only when the placement is a rule-1 handover: a link is a
    routing decision under the rules, and a trip dropped on a bus that has been
    parked a fortnight is an assignment, not a chain. gap_days and
    successor_gap are carried either way so the board can say what the drop
    costs before it happens.
    """
    company: str
    unit: int | None                    # None: the company's ad hoc lane
    supply: TripId | None
    gap_days: int | None
    successor_gap: int | None
    reposition: bool                    # the bus has to change city to get there
    chains: bool


@dataclass(frozen=True)
class DropRefusal:
    """A bus that may NOT take a trip, and which rule says so."""
    company: str
    unit: int
    reason: str


def drop_targets(
    report: ContractReport,
    registry: Registry,
    trip_id: TripId,
    reposition: bool = True,
) -> tuple[list[DropTarget], list[DropRefusal]]:
    """Every bus that could take `trip_id`, whether or not it is assigned now.

    Answers the board's drag gesture: a trip is picked up and every legal
    landing place has to light up before it is dropped. Computed here rather
    than in the browser for the usual reason — the rules live in graph.py, and
    a second copy in JS would be a second place for them to be wrong.

    The trip's CURRENT unit is offered like any other: a drag that ends where
    it started is a no-op the client drops, not an error the engine invents.
    Its own assignment is not subtracted from the timelines either, because
    insertion_ok excludes the trip being placed — a bus does not conflict with
    itself.
    """
    trip = next((t for t in report.trips if t.id == trip_id), None)
    if trip is None or trip.status is Status.CANCELED:
        return [], []
    # No date bounds, deliberately — both of them. The board's drag is a manual
    # escape hatch like the assign pickers: the horizon says what the PLANNER
    # decides on its own, and refusing a drop a dispatcher aimed by hand because
    # the trip is five weeks out would be the setting overruling its owner.
    _, _, _, unit_assignments, _, _ = _split(report, frozenset(), None, None)

    out: list[DropTarget] = []
    refused: list[DropRefusal] = []
    for key, company in sorted(registry.companies.items()):
        # An ad hoc lane is chain-invisible: it generates no supply and holds
        # no timeline, so rule 3 is the only thing that can refuse it. That is
        # also why it records no link.
        if graph.company_ok(key, trip):
            out.append(DropTarget(company=key, unit=None, supply=None,
                                  gap_days=None, successor_gap=None,
                                  reposition=False, chains=False))
        for unit in sorted(company.units):
            spot, why = graph.insertion_verdict(
                trip, unit_assignments.get((key, unit), ()), key, reposition)
            if spot is None:
                refused.append(DropRefusal(company=key, unit=unit, reason=why or ""))
                continue
            out.append(DropTarget(
                company=key, unit=unit,
                supply=(spot.predecessor.id
                        if spot.chains and spot.predecessor else None),
                gap_days=spot.gap_days, successor_gap=spot.successor_gap,
                reposition=spot.reposition, chains=spot.chains))
    return out, refused


def _split(report: ContractReport, locked: AbstractSet[TripId],
           as_of: date | None, horizon: date | None):
    """The one reading of supply vs demand, shared by evaluate and the solver
    so the two can never disagree about what is in play.

    Both date bounds cut DEMAND only. Supply is every assigned trip whatever its
    date: a bus that finishes the day before the window opens is standing there
    ready, and dropping it would make the first trips in the window unreachable
    for no reason anybody asked for."""
    # Canceled trips are excluded from matching but retained for audit and
    # as broken-chain roots (rule 7).
    live = [t for t in report.trips if t.status is not Status.CANCELED]

    assigned_live = [t for t in live if t.assignment is not None]
    # Supply = arrivals of numbered-unit assigned trips. Ad hoc (bare
    # company) assignments are chain-invisible: they never generate supply.
    supply_trips = [t for t in assigned_live if t.assignment.unit is not None]  # type: ignore[union-attr]
    # A locked trip leaves the demand set entirely: no suggestion, no plan link,
    # nothing to accept. It keeps its place on its unit's timeline and, when
    # assigned, still feeds the next hop — locking says "do not re-decide this
    # trip", not "pretend this bus is not there".
    #
    # The two date bounds come after the lock, and every trip they turn away is
    # RECORDED with the end of the window that did it. "No bus can reach this
    # trip", "too late to plan it" and "not looking that far yet" are three
    # different answers, and only this loop knows which one a trip got. A
    # count was enough while the horizon was the only bound a caller used; it
    # is not once a board has to label each bar, because every out-of-scope
    # trip otherwise reads as "no chain will ever reach this" — a verdict about
    # the fleet, for a trip nobody asked the fleet about.
    #
    # Started is tested first: a trip that is both under way and past the
    # horizon (a horizon behind as_of) cannot be planned whatever the window is
    # widened to, so that is the reason worth giving. Note the comparisons —
    # `<=` as_of because a trip beginning today is already today's problem, and
    # `>` horizon because a bound of today+30 includes the trip on day 30: the
    # preset says "a month ahead", not "a month minus one".
    out_of_scope: dict[TripId, Scope] = {}
    demand_trips: list[Trip] = []
    for t in live:
        if t.assignment is not None or t.id in locked:
            continue
        if as_of is not None and t.start <= as_of:
            out_of_scope[t.id] = "started"
        elif horizon is not None and t.start > horizon:
            out_of_scope[t.id] = "horizon"
        else:
            demand_trips.append(t)

    unit_assignments: dict[tuple[str, int], list[Trip]] = defaultdict(list)
    adhoc_by_company: dict[str, list[Trip]] = defaultdict(list)
    for t in assigned_live:
        assert t.assignment is not None
        if t.assignment.unit is not None:
            unit_assignments[(t.assignment.company, t.assignment.unit)].append(t)
        else:
            adhoc_by_company[t.assignment.company].append(t)
    return (assigned_live, supply_trips, demand_trips, unit_assignments,
            adhoc_by_company, out_of_scope)


def optimal_plan(
    report: ContractReport,
    reposition: bool = True,
    locked: AbstractSet[TripId] = frozenset(),
    as_of: date | None = None,
    horizon: date | None = None,
    time_limit: int = 60,
    objective: plan_mod.Objective = plan_mod.DEFAULT_OBJECTIVE,
) -> tuple[solve.SolvedPlan, int]:
    """Rule 8's exact solve over the same edges evaluate plans greedily.

    Returns (solved, greedy_covered) — the greedy count rides along because
    the comparison is the whole point of asking, and because the solver must
    never hand back less than the greedy would: on a timeout or a certify
    refusal that drops it below, the greedy plan is returned (labelled), so
    this endpoint can only ever match or improve what the board already shows.

    Deliberately NOT part of evaluate(): a CBC solve is seconds, evaluate runs
    on every mutation, and a 7-second tax on every cell edit would be paid
    mostly for plans nobody asked for. This is a preview-and-commit action,
    like the master re-chain.
    """
    # Same _split call as evaluate's, horizon included: the solver is only
    # meaningful as a comparison against the plan the board shows, and two
    # different demand sets would make "124 vs 113" a comparison of nothing.
    _, supply_trips, demand_trips, unit_assignments, _, _ = _split(
        report, locked, as_of, horizon)
    # The baseline is the plan the BOARD shows — under the idle objective,
    # comparing against the coverage plan would report a gap against numbers
    # the user never chose, and a fallback would carry the wrong label.
    greedy = plan_mod.build_plan(
        supply_trips, demand_trips, unit_assignments, objective, reposition)
    solved = solve.build_plan(demand_trips, unit_assignments, reposition, time_limit)
    if solved.covered < len(greedy.covered):
        solved = solve.SolvedPlan(
            plan=greedy, status=f"greedy-fallback ({solved.status})",
            seconds=solved.seconds, model_covered=solved.model_covered,
            refused=solved.refused)
    return solved, len(greedy.covered)
