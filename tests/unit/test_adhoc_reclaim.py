"""Rule 11: cancellable ad hoc hires can be moved onto the charter.

An ad hoc assignment is chain-invisible AND not demand, so before this it was a
dead end — a trip once given to a hired-in bus could never come back, however
much chartered capacity freed up later. This is the one action that undoes it,
and the properties that make it safe rather than merely useful are what this
file pins: the three-week notice, the pointlessness of releasing a non-49 trip,
and above all that a trip the replan cannot place keeps its hire.

The engine half is here against hand-built reports; the store half (what a
reclaim actually writes, and whether an imported company name comes back on
undo) lives in test_api.py, where a real database can answer it.
"""
from __future__ import annotations

import inspect
from datetime import timedelta

from buschain.engine import adhoc, rechain
from buschain.model import Status
from tests.helpers import BASE, mk_registry, mk_trip, report_of

REG = mk_registry(ollex=2, vilneda=1)

# Far enough out that rule 11 is satisfied by default, so a test that is not
# about the deadline never has to mention it.
FAR = 30


def _build(report, registry=REG, **kw):
    kw.setdefault("as_of", BASE)
    return adhoc.build(report, registry, (), **kw)


# -- what may be cancelled --------------------------------------------------

def test_notice_boundary_is_exactly_21_days():
    """Three weeks, and the boundary day itself still counts as cancellable."""
    assert adhoc.ADHOC_CANCEL_NOTICE_DAYS == 21
    on_time = mk_trip("OSL", "STO", 21, 22, company="ollex")
    too_late = mk_trip("OSL", "STO", 20, 21, company="ollex")

    result = _build(report_of(on_time, too_late))
    assert result.eligible == (on_time.id,)
    assert result.frozen_notice == 1


def test_only_bare_company_assignments_are_eligible():
    """A numbered unit is a statement about which vehicle runs the trip, not a
    purchase that can be called off."""
    numbered = mk_trip("OSL", "STO", FAR, FAR + 1, company="ollex", unit=1)
    uncovered = mk_trip("OSL", "STO", FAR, FAR + 1)
    result = _build(report_of(numbered, uncovered))
    assert result.eligible == ()
    assert (result.frozen_locked, result.frozen_pax, result.frozen_notice) == (0, 0, 0)


def test_locked_canceled_and_non_49_adhoc_are_counted_not_released():
    """The three refusals are mutually exclusive, so a panel can add them up —
    and a canceled hire is neither eligible nor 'untouched work'."""
    locked = mk_trip("OSL", "STO", FAR, FAR + 1, company="ollex")
    big_party = mk_trip("OSL", "STO", FAR, FAR + 1, company="ollex", pax=55)
    too_soon = mk_trip("OSL", "STO", 3, 4, company="ollex")
    canceled = mk_trip("OSL", "STO", FAR, FAR + 1, company="ollex",
                       status=Status.CANCELED)
    report = report_of(locked, big_party, too_soon, canceled)

    result = _build(report, locked={locked.id})
    assert result.eligible == ()
    assert (result.frozen_locked, result.frozen_pax, result.frozen_notice) == (1, 1, 1)


def test_a_blank_pax_hire_is_still_cancellable():
    """Rule 10 reads blank as unstated, so rule 11 must not read it as 'not 49'
    — paste and manual entry produce blank rows constantly."""
    hired = mk_trip("OSL", "STO", FAR, FAR + 1, company="ollex", pax=None)
    assert _build(report_of(hired)).eligible == (hired.id,)


def test_a_hire_past_the_horizon_is_out_of_scope_not_out_of_time():
    """The two date rules are counted apart because they mean opposite things:
    the notice is the supplier's deadline and cannot be argued with, the
    horizon is a window the dispatcher chose and can widen."""
    inside = mk_trip("OSL", "STO", 25, 26, company="ollex")
    outside = mk_trip("OSL", "STO", 60, 61, company="ollex")
    too_soon = mk_trip("OSL", "STO", 3, 4, company="ollex")
    report = report_of(inside, outside, too_soon)

    result = _build(report, horizon=BASE + timedelta(days=30))
    assert result.eligible == (inside.id,)
    assert (result.frozen_notice, result.frozen_horizon) == (1, 1)


def test_a_horizon_inside_the_notice_period_leaves_nothing_eligible():
    """Not a bug, but arithmetic worth pinning: with the window ending before
    the cancellation deadline, every hire is either too close to cancel or too
    far to plan. The counters have to keep saying which, or the panel can only
    show an empty list and no reason."""
    a = mk_trip("OSL", "STO", 5, 6, company="ollex")     # inside notice
    b = mk_trip("OSL", "STO", 40, 41, company="ollex")   # past the horizon
    result = _build(report_of(a, b), horizon=BASE + timedelta(days=14))

    assert result.eligible == ()
    assert (result.frozen_notice, result.frozen_horizon) == (1, 1)


def test_eligibility_does_not_ask_who_wrote_the_cell():
    """The carve-out from the source-data boundary, asserted rather than
    described: an imported hire is as cancellable as one this app booked, and
    the engine has no parameter with which to tell them apart."""
    assert "system_assigned" not in inspect.signature(adhoc.build).parameters
    assert "system_assigned" not in inspect.signature(adhoc.releasable).parameters


# -- what a reclaim does ----------------------------------------------------

def test_reclaimed_trip_lands_on_a_numbered_bus():
    """The point of the feature: work comes back off a hired bus."""
    anchor = mk_trip("STO", "OSL", FAR, FAR + 2, company="ollex", unit=1)
    hired = mk_trip("OSL", "STO", FAR + 4, FAR + 6, company="ollex")

    result = _build(report_of(anchor, hired), reposition=False)
    assert result.eligible == (hired.id,)
    assert result.reclaimed == (hired.id,)
    assert result.stayed_adhoc == ()
    link = result.engine.plan.chains[0].links[0]
    assert (link.demand, link.company, link.unit) == (hired.id, "ollex", 1)


def test_uncoverable_eligible_trip_keeps_its_adhoc_bus():
    """The never-strand guarantee, asserted on the LINKS and not just the field.

    `stayed_adhoc` being right is not enough — what protects the trip is that
    nothing in commit_links names it, so the store never blanks its cell.
    """
    unreachable = mk_trip("HEL", "BGO", FAR, FAR + 1, company="ollex")
    result = _build(report_of(unreachable))
    assert result.eligible == (unreachable.id,)
    assert result.stayed_adhoc == (unreachable.id,)
    assert result.reclaimed == ()
    assert unreachable.id not in {d for d, _, _, _ in adhoc.commit_links(result)}


def test_reclaim_can_withdraw_a_proposed_link():
    """One pool: a reclaim competes with genuinely uncovered work and can win.

    That is the trade the objective cannot price, so it has to be visible —
    `regressed` is what a human overrules it with.
    """
    one_bus = mk_registry(ollex=1)
    anchor = mk_trip("STO", "OSL", FAR, FAR + 2, company="ollex", unit=1)
    # The hire departs sooner and ends earlier, so the objective prefers it;
    # taking it leaves the bus in STO and `uncovered` out of reach.
    uncovered = mk_trip("OSL", "STO", FAR + 5, FAR + 7)
    hired = mk_trip("OSL", "STO", FAR + 4, FAR + 6, company="ollex")
    report = report_of(anchor, uncovered, hired)

    result = _build(report, one_bus, reposition=False)
    assert result.reclaimed == (hired.id,)
    assert result.regressed == (uncovered.id,)


def test_commit_links_are_in_chain_order():
    """A reclaimed trip is often the supply of a later hop, so order is load
    bearing: the store refuses a link whose supply is not yet assigned."""
    anchor = mk_trip("STO", "OSL", FAR, FAR + 2, company="ollex", unit=1)
    hired = mk_trip("OSL", "STO", FAR + 4, FAR + 6, company="ollex")
    onward = mk_trip("STO", "OSL", FAR + 8, FAR + 10)

    result = _build(report_of(anchor, hired, onward), reposition=False)
    links = adhoc.commit_links(result)
    assert [d for d, _, _, _ in links] == [hired.id, onward.id]
    assert links[1][3] == hired.id


def test_adhoc_trips_are_still_never_supply_in_the_preview():
    """AT6's companion. A trip is either untouched — still bare-company, still
    excluded from supply — or stripped to unassigned, at which point it is
    demand like any other. Neither route makes a hire into supply."""
    anchor = mk_trip("STO", "OSL", FAR, FAR + 2, company="ollex", unit=1)
    kept = mk_trip("OSL", "STO", 2, 3, company="vilneda")     # inside the notice
    hired = mk_trip("OSL", "STO", FAR + 4, FAR + 6, company="ollex")
    result = _build(report_of(anchor, kept, hired), reposition=False)

    for suggestions in result.engine.suggestions_by_trip.values():
        for s in suggestions:
            assert s.supply != kept.id
    assert kept.id not in {t for ts in result.engine.timelines.values() for t in ts}


# -- the boundary with the master re-chain ----------------------------------

def test_rechain_leaves_adhoc_assignments_alone():
    """A re-chain released hires it could never re-place — plan.chains hold
    numbered units only — so every one of them was guaranteed to strand."""
    hired = mk_trip("OSL", "STO", FAR, FAR + 1, company="ollex")
    result = rechain.build(report_of(hired), REG, (),
                           system_assigned={hired.id}, as_of=BASE)
    assert result.released == ()
    assert result.stranded == ()
    # Not counted as frozen either: ad hoc is simply not this action's subject.
    assert (result.frozen_source, result.frozen_locked) == (0, 0)


def test_the_two_release_sets_are_disjoint():
    """One owner per assignment kind is what keeps the two actions from having
    contradictory ad hoc policies."""
    numbered = mk_trip("STO", "OSL", FAR, FAR + 2, company="ollex", unit=1)
    hired = mk_trip("OSL", "STO", FAR + 4, FAR + 6, company="ollex")
    report = report_of(numbered, hired)

    released = set(rechain.build(
        report, REG, (), system_assigned={numbered.id, hired.id},
        as_of=BASE).released)
    eligible = set(_build(report).eligible)
    assert released == {numbered.id}
    assert eligible == {hired.id}
    assert released & eligible == set()


def test_horizon_needs_no_repeating():
    """Rule 11's window subsumes the re-chain's 'has not started yet' boundary,
    which is why refusal() does not test it: 21 days out is in the future by
    construction, and a past trip fails the notice test first."""
    past = mk_trip("OSL", "STO", -10, -8, company="ollex")
    result = adhoc.build(report_of(past), REG, (), as_of=BASE + timedelta(days=1))
    assert result.eligible == ()
    assert result.frozen_notice == 1
