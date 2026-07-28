"""Multi-hop chain construction (engine/plan.py).

The properties that matter are the ones a one-hop suggester got for free and a
planner does not: a chain applies several assignments to one bus, so it can
break the unit-integrity rule the suggester could never reach.
"""
from __future__ import annotations

import dataclasses
from collections import defaultdict

from buschain.engine import checks, evaluate
from buschain.engine.plan import DEFAULT_OBJECTIVE, build_plan
from buschain.model import AcceptedLink, Status
from tests.helpers import mk_registry, mk_trip, report_of


def _split(trips):
    """(supply, demand, units) the way evaluate() derives them."""
    live = [t for t in trips if t.status is not Status.CANCELED]
    supply = [t for t in live
              if t.assignment is not None and t.assignment.unit is not None]
    demand = [t for t in live if t.assignment is None]
    units = defaultdict(list)
    for t in supply:
        units[(t.assignment.company, t.assignment.unit)].append(t)
    return supply, demand, units


def build(trips, objective="coverage"):
    """Run the planner the way evaluate() does, minus repositioning — most of
    these tests are about the same-city rules and say so by excluding it."""
    return build_plan(*_split(trips), objective, reposition=False)


def links_of(plan):
    return [link for chain in plan.chains for link in chain.links]


def test_chains_beyond_the_first_hop():
    """The whole point: a trip only reachable via another uncovered trip."""
    seed = mk_trip("STO", "CPH", 0, 2, company="ollex", unit=1)
    hop1 = mk_trip("CPH", "OSL", 4, 6)
    hop2 = mk_trip("OSL", "BGO", 8, 10)          # needs hop1 accepted first
    plan = build([seed, hop1, hop2])

    assert plan.covered == {hop1.id, hop2.id}
    assert len(plan.chains) == 1
    assert [link.demand for link in plan.chains[0].links] == [hop1.id, hop2.id]
    assert [link.step for link in plan.chains[0].links] == [0, 1]
    # Each link hangs off its predecessor, which is what forces accept order.
    assert plan.chains[0].links[1].supply == hop1.id


def test_plan_adds_no_integrity_violations():
    """Two demands could each chain off the same arrival, but not both onto
    the same bus — they overlap. Taking both is exactly the failure the
    one-edge-per-unit-per-round rule exists to prevent."""
    seed = mk_trip("STO", "CPH", 0, 2, company="ollex", unit=1)
    a = mk_trip("CPH", "OSL", 4, 9)
    b = mk_trip("CPH", "BGO", 5, 10)             # overlaps a
    trips = [seed, a, b]
    plan = build(trips)

    assert len(links_of(plan)) == 1, "only one of the overlapping pair is plannable"

    by_id = {t.id: t for t in trips}
    units = defaultdict(list)
    units[("ollex", 1)].append(seed)
    for link in links_of(plan):
        units[(link.company, link.unit)].append(
            dataclasses.replace(by_id[link.demand], assignment=seed.assignment))
    assert checks.unit_integrity(units) == ()


def test_plan_never_violates_a_y_constraint():
    seed = mk_trip("STO", "CPH", 0, 2, company="ollex", unit=1)
    only_vilneda = mk_trip("CPH", "OSL", 4, 6, yn="Y", constraint="vilneda")
    assert build([seed, only_vilneda]).covered == frozenset()


def test_plan_never_violates_an_n_constraint():
    seed = mk_trip("STO", "CPH", 0, 2, company="ollex", unit=1)
    not_ollex = mk_trip("CPH", "OSL", 4, 6, yn="N", constraint="ollex")
    assert build([seed, not_ollex]).covered == frozenset()


def test_confirmed_assignments_never_appear_in_the_plan():
    """A committed trip is supply, not demand — it is what the plan routes
    around, and that is what makes a confirmed link immune to replanning."""
    seed = mk_trip("STO", "CPH", 0, 2, company="ollex", unit=1)
    taken = mk_trip("CPH", "OSL", 4, 6, company="ollex", unit=1)
    plan = build([seed, taken])
    assert taken.id not in plan.covered


def test_canceled_trips_are_never_planned():
    seed = mk_trip("STO", "CPH", 0, 2, company="ollex", unit=1)
    dead = mk_trip("CPH", "OSL", 4, 6, status=Status.CANCELED)
    assert build([seed, dead]).covered == frozenset()


def test_adhoc_assignments_generate_no_supply():
    """Rule: a bare company is 'some bus from X' — nobody knows where it ends
    up, so it cannot feed a chain."""
    adhoc = mk_trip("STO", "CPH", 0, 2, company="ollex")     # no unit
    d = mk_trip("CPH", "OSL", 4, 6)
    assert build([adhoc, d]).covered == frozenset()


def test_empty_plan_without_supply():
    assert build([mk_trip("CPH", "OSL", 4, 6)]).chains == ()


def test_plan_is_deterministic():
    trips = [
        mk_trip("STO", "CPH", 0, 2, company="ollex", unit=1),
        mk_trip("STO", "CPH", 0, 2, company="ollex", unit=2),
        mk_trip("CPH", "OSL", 4, 6),
        mk_trip("CPH", "BGO", 5, 7),
        mk_trip("OSL", "HEL", 9, 11),
    ]
    first = build(trips)
    for _ in range(5):
        again = build(trips)
        assert [(l.supply, l.demand, l.company, l.unit) for l in links_of(again)] \
            == [(l.supply, l.demand, l.company, l.unit) for l in links_of(first)]


def test_objectives_are_both_valid_and_can_differ():
    """Shortest-turnaround and most-covered are genuinely different policies;
    both must still produce only legal links."""
    trips = [
        mk_trip("STO", "CPH", 0, 2, company="ollex", unit=1),
        mk_trip("CPH", "OSL", 3, 4),        # gap 1 — the idle objective's pick
        mk_trip("CPH", "BGO", 6, 7),        # gap 4
        mk_trip("BGO", "HEL", 9, 10),       # only reachable via the BGO trip
    ]
    for objective in ("coverage", "idle"):
        plan = build(trips, objective)
        assert plan.objective == objective
        for link in links_of(plan):
            assert 1 <= link.gap_days <= 4


def test_idle_days_counts_rest_not_gap():
    """Rule 1 defines rest = gap - 1, so a next-day pickup costs nothing."""
    seed = mk_trip("STO", "CPH", 0, 2, company="ollex", unit=1)
    next_day = mk_trip("CPH", "OSL", 3, 5)
    assert build([seed, next_day]).idle_days == 0


def test_reposition_unlocks_a_bus_parked_in_the_wrong_city():
    """The case the whole feature exists for: a free bus, in the right week,
    in the wrong city. Without an empty run it can never work again."""
    parked = mk_trip("OSL", "STO", 0, 2, company="ollex", unit=1)
    d = mk_trip("CPH", "OSL", 4, 6)

    assert build([parked, d], "coverage").covered == set()  # same-city only
    plan = build_plan(*_split([parked, d]), "coverage", reposition=True)
    (link,) = links_of(plan)
    assert link.demand == d.id and link.reposition
    assert (link.from_city, link.to_city) == ("STO", "CPH")


def test_reposition_loses_to_any_same_city_link():
    """An empty run costs money whether or not the trip earns any, so it may
    only ever be spent on demand nothing else could serve."""
    ollex = mk_trip("STO", "CPH", 0, 2, company="ollex", unit=1)
    vilneda = mk_trip("STO", "BGO", 0, 2, company="vilneda", unit=1)
    d = mk_trip("CPH", "OSL", 4, 6)          # CPH: free for ollex, empty run for vilneda
    plan = build_plan(*_split([ollex, vilneda, d]), "coverage", reposition=True)

    (link,) = links_of(plan)
    assert link.company == "ollex" and not link.reposition


def test_reposition_waits_for_a_same_city_bus_the_round_took():
    """The gap the sort order alone leaves open.

    Ranking places every same-city edge first, but the one-edge-per-unit rule
    can spend a demand's same-city bus on an earlier demand — and the loop then
    reaches that demand with only its empty run left and takes it. Here ollex 1
    is the free bus for BOTH demands and can only serve one per round, so
    without the deferral vilneda 1 gets driven STO->CPH for the second. Waiting
    a round costs nothing: ollex 1 lands back in CPH and takes it for free.
    """
    ollex = mk_trip("STO", "CPH", 0, 2, company="ollex", unit=1)
    vilneda = mk_trip("CPH", "STO", 0, 2, company="vilneda", unit=1)
    near = mk_trip("CPH", "CPH", 3, 3)       # ollex takes this first — shortest gap
    far = mk_trip("CPH", "OSL", 5, 6)        # free for ollex next round, empty run for vilneda

    plan = build_plan(*_split([ollex, vilneda, near, far]), "coverage", reposition=True)

    assert plan.covered == {near.id, far.id}
    assert not any(link.reposition for link in links_of(plan))
    # And the empty run was avoided by waiting, not by refusing the work: the
    # second link is ollex's, one round later.
    assert {link.company for link in links_of(plan)} == {"ollex"}


def test_reposition_is_offered_only_off_the_chain_tail():
    """Mid-chain it would strand whatever comes next in the wrong city."""
    first = mk_trip("STO", "CPH", 0, 2, company="ollex", unit=1)
    later = mk_trip("CPH", "OSL", 6, 8, company="ollex", unit=1)   # already booked
    d = mk_trip("BGO", "STO", 4, 5)          # would need CPH -> BGO between them
    plan = build_plan(*_split([first, later, d]), "coverage", reposition=True)
    assert links_of(plan) == []


def test_accepted_reposition_is_recorded_not_reported_as_damage():
    """Rule 6 calls a cross-city pair a mismatch. Once someone has accepted the
    link, it is an approved empty run — still listed, but not as a violation
    the reader is expected to go and fix."""
    parked = mk_trip("OSL", "STO", 0, 2, company="ollex", unit=1)
    moved = mk_trip("CPH", "OSL", 4, 6, company="ollex", unit=1)
    units = {("ollex", 1): [parked, moved]}

    (unlinked,) = checks.unit_integrity(units)
    assert unlinked.kind == "city_mismatch"

    (linked,) = checks.unit_integrity(
        units, [AcceptedLink(supply=parked.id, demand=moved.id)])
    assert linked.kind == "reposition"


def test_evaluate_exposes_the_plan():
    registry = mk_registry(ollex=1)
    report = report_of(
        mk_trip("STO", "CPH", 0, 2, company="ollex", unit=1),
        mk_trip("CPH", "OSL", 4, 6),
    )
    result = evaluate(report, registry)
    assert len(result.plan.chains) == 1
    assert result.plan.objective == DEFAULT_OBJECTIVE
    # Same-city chain, so reachability sees it too. That containment holds only
    # because no empty run is involved — reach.py does not model repositioning,
    # so in general the plan can cover trips outside `reachable`.
    assert result.plan.covered <= result.reachable
