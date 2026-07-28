"""Locks and the master re-chain.

The engine half is unit-tested here against hand-built reports; the store half
(what a release actually writes) lives in test_api.py, where a real database
can answer whether the cell came back.
"""
from __future__ import annotations

from datetime import timedelta

from buschain import engine
from buschain.engine import rechain
from buschain.model import Status
from tests.helpers import BASE, mk_registry, mk_trip, report_of

REG = mk_registry(ollex=2, vilneda=1)


def _ids(trips):
    return {t.id for t in trips}


# -- locks ------------------------------------------------------------------

def test_locked_demand_draws_nothing():
    """A locked uncovered trip leaves the demand set entirely."""
    supply = mk_trip("STO", "OSL", 0, 2, company="ollex", unit=1)
    demand = mk_trip("OSL", "STO", 4, 6)
    report = report_of(supply, demand)

    free = engine.evaluate(report, REG)
    assert demand.id in free.plan.covered
    assert free.suggestions_by_trip[demand.id]

    held = engine.evaluate(report, REG, locked={demand.id})
    assert held.plan.covered == frozenset()
    assert demand.id not in held.suggestions_by_trip
    # Not merely unsuggested: nothing may reach it by any route.
    assert demand.id not in held.reachable


def test_locked_assignment_still_feeds_the_next_hop():
    """The chosen semantics: a lock pins a trip, it does not park its bus.

    The alternative reading ("no link either side") would dead-end the unit
    here and leave `later` uncovered, which is a materially different feature.
    """
    first = mk_trip("STO", "OSL", 0, 2, company="ollex", unit=1)
    later = mk_trip("OSL", "STO", 4, 6)
    result = engine.evaluate(report_of(first, later), REG, locked={first.id})
    assert later.id in result.plan.covered
    assert result.plan.chains[0].links[0].supply == first.id


# -- the horizon ------------------------------------------------------------

def test_as_of_excludes_trips_already_started():
    supply = mk_trip("STO", "OSL", 0, 2, company="ollex", unit=1)
    started = mk_trip("OSL", "STO", 4, 6)
    future = mk_trip("OSL", "STO", 10, 12)
    report = report_of(supply, started, future)

    # Day 4 is `started`'s own start date, and "has not started yet" is strict:
    # a trip beginning today is already someone's problem for today.
    result = engine.evaluate(report, REG, as_of=BASE + timedelta(days=4))
    assert started.id not in result.plan.covered


# -- what a re-chain may release --------------------------------------------

def _build(report, **kw):
    kw.setdefault("system_assigned", _ids(report.trips))
    kw.setdefault("as_of", BASE)
    return rechain.build(report, REG, (), **kw)


def test_releases_only_app_made_future_unlocked_assignments():
    from_sheet = mk_trip("STO", "OSL", 5, 6, company="ollex", unit=1)
    already_started = mk_trip("STO", "OSL", -2, 1, company="ollex", unit=2)
    locked = mk_trip("STO", "OSL", 8, 9, company="vilneda", unit=1)
    releasable = mk_trip("OSL", "STO", 12, 13, company="ollex", unit=1)
    report = report_of(from_sheet, already_started, locked, releasable)

    result = rechain.build(
        report, REG, (),
        # from_sheet's company name came in on the spreadsheet, so it is absent
        # here — the same boundary repo.unassign enforces one row at a time.
        system_assigned={already_started.id, locked.id, releasable.id},
        locked={locked.id},
        as_of=BASE,
    )
    assert result.released == (releasable.id,)
    assert result.frozen_source == 1        # from_sheet
    assert result.frozen_locked == 1        # locked
    # already_started is neither: it is out on the date, before either rule.


def test_canceled_assignments_are_never_released():
    canceled = mk_trip("STO", "OSL", 5, 6, company="ollex", unit=1,
                       status=Status.CANCELED)
    assert _build(report_of(canceled)).released == ()


def test_released_trip_is_replanned_onto_a_bus():
    """The point of the feature: an assignment goes back in the pot."""
    # Gaps stay inside rule 1's window (1..4 days same city) throughout: this
    # is a test about who owns the trip, not about the window refusing it.
    anchor = mk_trip("STO", "OSL", -5, -3, company="ollex", unit=1)
    moved = mk_trip("OSL", "STO", 1, 3, company="vilneda", unit=1)
    report = report_of(anchor, moved)

    result = rechain.build(report, REG, (), system_assigned={moved.id},
                           as_of=BASE, reposition=False)
    assert result.released == (moved.id,)
    # Vilneda 1 was never anywhere, so it generates no supply; Ollex 1 sits in
    # OSL and the replan hands the trip to it.
    assert result.stranded == ()
    link = result.engine.plan.chains[0].links[0]
    assert (link.demand, link.company, link.unit) == (moved.id, "ollex", 1)


def test_stranded_names_what_the_replan_could_not_re_cover():
    """Releasing is not monotone, and the caller has to be able to see that."""
    # Two trips share one bus's only usable slot: whichever the planner takes,
    # the other comes back with nothing.
    a = mk_trip("OSL", "STO", 1, 3, company="ollex", unit=1)
    b = mk_trip("OSL", "STO", 1, 3, company="ollex", unit=1)
    anchor = mk_trip("STO", "OSL", -5, -3, company="ollex", unit=1)
    report = report_of(anchor, a, b)

    result = rechain.build(report, REG, (), system_assigned={a.id, b.id},
                           as_of=BASE, reposition=False)
    assert set(result.released) == {a.id, b.id}
    assert len(result.stranded) == 1
    assert set(result.stranded) < set(result.released)


def test_commit_links_are_in_chain_order():
    """A released trip is often the supply of a later hop, so order is load
    bearing: the store refuses a link whose supply is not yet assigned."""
    anchor = mk_trip("STO", "OSL", -5, -3, company="ollex", unit=1)
    hop1 = mk_trip("OSL", "STO", 0, 2)
    hop2 = mk_trip("STO", "OSL", 4, 6)
    result = _build(report_of(anchor, hop1, hop2), system_assigned=set(),
                    as_of=BASE - timedelta(days=1))
    links = rechain.commit_links(result)
    assert [d for d, _, _, _ in links] == [hop1.id, hop2.id]
    assert links[1][3] == hop1.id           # hop2's supply is hop1


def test_locked_trip_keeps_its_assignment_through_a_rechain():
    keep = mk_trip("STO", "OSL", 5, 6, company="ollex", unit=1)
    result = _build(report_of(keep), locked={keep.id})
    assert result.released == ()
    assert result.frozen_locked == 1
