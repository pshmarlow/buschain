"""Rule 8's second half: the same feasible-link model, solved exactly.

build_plan (plan.py) walks the edge graph greedily, and measurement says that
leaves coverage behind: on the reference data the greedy covers 113 of 190
demands where 124 is provably achievable (+30% with empty runs off). This
module formulates coverage over the SAME rules as an integer program and lets
CBC find the optimum — the brief's rule 8 anticipated exactly this, which is
why the model needed no change.

Three properties keep it trustworthy:

* Same rules, one home. Windows come from graph.min_gap_days, Y/N from
  reach._company_ok, and repositioning is tail-only — nothing here restates a
  rule, it only asks a better question over the same edges.
* Every link is CERTIFIED by the engine before it reaches the caller: the
  solution is replayed hop by hop through graph.feasible_edges exactly as
  build_plan applies its own links, and anything the engine would refuse is
  dropped (with the count reported). The returned plan is therefore a
  sequence of accepts the store will actually take.
* Deterministic: the model is built in canonical (sorted) order, so the LP
  handed to CBC is byte-stable and single-threaded CBC returns the same
  optimum for the same input — the property the greedy gives for free and a
  solver does not, unless it is made to.

Two deliberate conservatisms (both can only UNDER-count the optimum): at most
one demand is inserted into a committed mid-timeline gap, and interior
inserts are never extended into chains — the greedy CAN occasionally do the
latter, which is one more reason the caller compares the certified result
against the greedy and lets the better plan win; neither limit can make the
feature worse than what exists.

The objective prices what the model can honestly price: each covered trip is
worth 100_000_000, each empty run costs 100_000 and each gap day costs 1 — so
coverage strictly dominates (a trip is always worth taking), and among
equal-coverage optima the solver prefers fewer empty runs, then tighter
turnarounds. Real money stays out until the registry carries rates (brief:
out of scope).

pulp (which bundles CBC) is an optional dependency — `buschain[solver]`.
Without it the app runs unchanged and the API reports the solver as
unavailable.
"""
from __future__ import annotations

import dataclasses
import time
import warnings
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence  # noqa: F401 - Sequence in hints

from ..model import Assignment, Plan, PlannedLink, Trip, TripId
from . import graph
from .plan import _assemble
from .reach import _company_ok

try:
    import pulp
except ImportError:                     # pragma: no cover - exercised without extra
    pulp = None


class SolverUnavailable(RuntimeError):
    """pulp/CBC is not installed — install buschain[solver]."""


@dataclass(frozen=True)
class SolvedPlan:
    plan: Plan                          # certified links only, chain order
    status: str                         # CBC's verdict ('Optimal', 'Not Solved'…)
    seconds: float
    model_covered: int                  # what the ILP claimed before replay
    refused: int                        # links the engine refused during replay

    @property
    def covered(self) -> int:
        return len(self.plan.covered)


# Tier separation with real headroom: coverage dominates to ~999 links
# (10^8 / (10^5 + 6)), and the empty-run tier holds against accumulated
# gap-day differences (max 5 per link) to 20,000 links — the original 1,000
# separation silently inverted at 200 links, uncomfortably close to the
# reference data's 190 demands.
_TRIP_WORTH = 100_000_000
_EMPTY_RUN_COST = 100_000


def _window_ok(from_city: str, to_city: str, gap: int, reposition: bool) -> bool:
    if from_city != to_city and not reposition:
        return False
    lo = graph.min_gap_days(from_city, to_city)
    return lo <= gap <= lo + graph.MAX_REST_DAYS


def _edge_worth(from_city: str, to_city: str, gap: int) -> int:
    return (_TRIP_WORTH - gap
            - (_EMPTY_RUN_COST if from_city != to_city else 0))


def build_plan(
    demand_trips: Iterable[Trip],
    unit_assignments: Mapping[tuple[str, int], Sequence[Trip]],
    reposition: bool = True,
    time_limit: int = 60,
) -> SolvedPlan:
    """Optimal-coverage counterpart of plan.build_plan — same Plan shape out,
    so committing it is the ordinary accept path. No separate supply list:
    the unit timelines ARE the supply (numbered assigned trips, grouped)."""
    if pulp is None:
        raise SolverUnavailable(
            "The exact solver needs the 'pulp' package (buschain[solver])")
    t0 = time.time()
    with warnings.catch_warnings():
        # PuLP 3.3 deprecates LpVariable() and PULP_CBC_CMD in favor of a 4.0
        # API that does not exist yet in released form — one warning PER
        # VARIABLE (~15k per solve). pyproject pins pulp<4; migrate to
        # prob.add_variable + COIN_CMD (pulp[cbc]) when moving to 4.x.
        warnings.simplefilter("ignore", DeprecationWarning)
        return _solve(demand_trips, unit_assignments, reposition, time_limit, t0)


def _solve(demand_trips, unit_assignments, reposition, time_limit, t0) -> SolvedPlan:

    # Canonical orders everywhere below: iteration order decides variable
    # order decides the LP file decides CBC's path — see module docstring.
    demand = sorted(demand_trips, key=lambda t: (t.start, t.end, t.id))
    units = {
        key: sorted(trips, key=lambda t: (t.start, t.end, t.id))
        for key, trips in sorted(unit_assignments.items())
    }

    prob = pulp.LpProblem("rule8_coverage", pulp.LpMaximize)
    covering: dict[TripId, list] = {d.id: [] for d in demand}
    objective = []

    def var(name: str, worth: int, covers: TripId):
        v = pulp.LpVariable(name, cat="Binary")
        covering[covers].append(v)
        objective.append(worth * v)
        return v

    # x[(unit, demand)]: demand starts the unit's tail extension. The tail is
    # the one place feasible_edges allows an empty run from.
    x: dict[tuple[tuple[str, int], TripId], object] = {}
    tails: dict[tuple[str, int], Trip] = {}
    for i, (key, trips) in enumerate(units.items()):
        company = key[0]
        tail = max(trips, key=lambda t: (t.end, t.start, t.id))
        tails[key] = tail
        mine = []
        for j, d in enumerate(demand):
            gap = (d.start - tail.end).days
            if gap < 1 or not _company_ok(company, d):
                continue
            if _window_ok(tail.arr_city, d.dep_city, gap, reposition):
                v = var(f"x_{i}_{j}", _edge_worth(tail.arr_city, d.dep_city, gap), d.id)
                x[(key, d.id)] = v
                mine.append(v)
        if mine:
            prob += pulp.lpSum(mine) <= 1        # one successor per tail

    # g[(unit, gap-after-A, demand)]: a single same-city insert into a
    # committed gap. Same-city on BOTH sides, mirroring _unit_timeline_ok:
    # A is not the tail, so no empty run, and the committed successor B must
    # still chain from the inserted demand.
    g: dict[tuple[tuple[str, int], TripId, TripId], object] = {}
    for i, (key, trips) in enumerate(units.items()):
        company = key[0]
        for a_idx, (A, B) in enumerate(zip(trips, trips[1:])):
            slot = []
            for j, d in enumerate(demand):
                if not _company_ok(company, d):
                    continue
                if (A.arr_city == d.dep_city and d.arr_city == B.dep_city
                        and graph.MIN_GAP_DAYS <= (d.start - A.end).days
                            <= graph.MAX_GAP_DAYS
                        and graph.MIN_GAP_DAYS <= (B.start - d.end).days
                            <= graph.MAX_GAP_DAYS):
                    gap = (d.start - A.end).days
                    v = var(f"g_{i}_{a_idx}_{j}",
                            _edge_worth(A.arr_city, d.dep_city, gap), d.id)
                    g[(key, A.id, d.id)] = v
                    slot.append(v)
            if slot:
                prob += pulp.lpSum(slot) <= 1    # one insert per gap

    # w[(company, d1, d2)]: chain extension — d2 after already-covered d1.
    # Company-keyed, not unit-keyed: the window depends only on cities and
    # dates, and the gate below ties the chain back to a unit of the company.
    # A chained demand is always the tail of its chain when extended, so the
    # full pair window (empty runs included) applies.
    companies = sorted({key[0] for key in units})
    w: dict[tuple[str, TripId, TripId], object] = {}
    w_out: dict[tuple[str, TripId], list] = {}
    w_in: dict[tuple[str, TripId], list] = {}
    for c_idx, c in enumerate(companies):
        for j1, d1 in enumerate(demand):
            if not _company_ok(c, d1):
                continue
            for j2, d2 in enumerate(demand):
                gap = (d2.start - d1.end).days
                if gap < 1 or not _company_ok(c, d2):
                    continue
                if _window_ok(d1.arr_city, d2.dep_city, gap, reposition):
                    v = var(f"w_{c_idx}_{j1}_{j2}",
                            _edge_worth(d1.arr_city, d2.dep_city, gap), d2.id)
                    w[(c, d1.id, d2.id)] = v
                    w_out.setdefault((c, d1.id), []).append(v)
                    w_in.setdefault((c, d2.id), []).append(v)

    # Each demand is covered at most once, across all three edge kinds.
    for d in demand:
        if covering[d.id]:
            prob += pulp.lpSum(covering[d.id]) <= 1

    # A chain may continue from d1 under company c only if d1 was covered
    # under c by a tail extension or an earlier chain hop — never by a gap
    # insert, whose committed successor pins the timeline behind it. Time
    # ordering makes these paths acyclic, so no subtour handling is needed.
    for (c, d1_id), outs in sorted(w_out.items(), key=lambda kv: str(kv[0])):
        gates = [v for ((cu, _unit), dd), v in x.items()
                 if cu == c and dd == d1_id]
        gates += w_in.get((c, d1_id), [])
        if gates:
            prob += pulp.lpSum(outs) <= pulp.lpSum(gates)
        else:
            for v in outs:
                prob += v == 0

    if not objective:
        return SolvedPlan(plan=_assemble([], "optimal"), status="Empty",
                          seconds=time.time() - t0, model_covered=0, refused=0)

    prob += pulp.lpSum(objective)
    status = prob.solve(pulp.PULP_CBC_CMD(msg=0, timeLimit=time_limit, threads=1))

    chosen = lambda v: (v.value() or 0) > 0.5  # noqa: E731 - local predicate
    model_covered = sum(
        1 for d in demand if any(chosen(v) for v in covering[d.id]))

    links, refused = _certify(
        demand,
        units,
        tails,
        [k for k, v in x.items() if chosen(v)],
        [k for k, v in g.items() if chosen(v)],
        [k for k, v in w.items() if chosen(v)],
        reposition,
    )
    return SolvedPlan(
        plan=_assemble(links, "optimal"),
        status=pulp.LpStatus[status],
        seconds=time.time() - t0,
        model_covered=model_covered,
        refused=refused,
    )


def _certify(demand, units, tails, x_sel, g_sel, w_sel, reposition):
    """Replay the solution through the real engine, link by link.

    Each hop is accepted provisionally exactly the way build_plan applies its
    own links, and must be emitted by graph.feasible_edges against the live
    timeline at that moment. A refused hop is dropped along with the rest of
    its chain (the suffix has no supply without it). This is what turns the
    ILP's claim into a plan the store is guaranteed to accept.
    """
    by_id = {d.id: d for d in demand}
    live = {key: list(trips) for key, trips in units.items()}
    links: list[PlannedLink] = []
    step: dict[tuple[str, int], int] = {}
    refused = 0

    def accept(supply: Trip, d: Trip, key: tuple[str, int]) -> Trip | None:
        nonlocal refused
        if graph.feasible_edges([supply], [d], live, reposition):
            planned = dataclasses.replace(
                d, assignment=Assignment(company=key[0], unit=key[1]))
            live[key].append(planned)
            gap = (d.start - supply.end).days
            links.append(PlannedLink(
                supply=supply.id, demand=d.id, company=key[0], unit=key[1],
                gap_days=gap, step=step.get(key, 0),
                reposition=supply.arr_city != d.dep_city,
                from_city=supply.arr_city, to_city=d.dep_city,
            ))
            step[key] = step.get(key, 0) + 1
            return planned
        refused += 1
        return None

    # Gap inserts first: they sit inside committed gaps, which tail chains
    # never touch, so this order cannot invalidate anything after it.
    for key, a_id, d_id in sorted(g_sel, key=str):
        A = next(t for t in live[key] if t.id == a_id)
        accept(A, by_id[d_id], key)

    # Tail chains: start from x, follow the company's chosen w successors.
    succ = {(c, d1): d2 for c, d1, d2 in w_sel}
    for key, d_id in sorted(x_sel, key=str):
        current = accept(tails[key], by_id[d_id], key)
        while current is not None:
            nxt = succ.get((key[0], current.id))
            if nxt is None:
                break
            current = accept(current, by_id[nxt], key)

    return links, refused
