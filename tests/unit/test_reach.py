"""Reachability: which uncovered trips a chain could ever get to.

The suggester only shows the first hop, so these cover the distinction the
board depends on — "no candidate yet" versus "no chain will ever reach this".
"""
from buschain import engine
from buschain.model import Status
from tests.helpers import mk_registry, mk_trip, report_of

REG = mk_registry(ollex=2, vilneda=2)


def test_direct_candidate_is_reachable():
    d = mk_trip("CPH", "OSL", 10, 12)
    s = mk_trip("STO", "CPH", 0, 9, company="ollex", unit=1)
    result = engine.evaluate(report_of(d, s), REG)
    assert d.id in result.reachable
    assert d.id in result.suggestions_by_trip          # actionable right now


def test_second_hop_is_reachable_though_it_has_no_candidate_yet():
    """The trip the board must stop painting as hopeless."""
    s = mk_trip("STO", "CPH", 0, 9, company="ollex", unit=1)
    first = mk_trip("CPH", "OSL", 10, 12)              # chains off s
    # Chains off `first` once taken (OSL, gap 3), and out of reach of a direct
    # empty run from s: CPH -> OSL is a one-clear-day pair, so its reposition
    # window off day 9 closes at day 14.
    second = mk_trip("OSL", "BGO", 15, 17)
    result = engine.evaluate(report_of(s, first, second), REG)

    # Nothing reaches OSL today, so the suggester offers nothing for `second`...
    assert second.id not in result.suggestions_by_trip
    # ...but accepting `first` would put a bus there, so it is not hopeless.
    assert {first.id, second.id} <= result.reachable


def test_unreachable_city_is_never_reachable():
    d = mk_trip("HEL", "OSL", 10, 12)                  # nothing ever arrives in HEL
    s = mk_trip("STO", "CPH", 0, 9, company="ollex", unit=1)
    result = engine.evaluate(report_of(d, s), REG)
    assert d.id not in result.reachable


def test_gap_outside_window_is_never_reachable():
    d = mk_trip("CPH", "OSL", 30, 32)                  # 21 days after the arrival
    s = mk_trip("STO", "CPH", 0, 9, company="ollex", unit=1)
    result = engine.evaluate(report_of(d, s), REG)
    assert d.id not in result.reachable


def test_y_constraint_blocks_reachability_for_the_wrong_company():
    blocked = mk_trip("CPH", "OSL", 10, 12, yn="Y", constraint="vilneda")
    s = mk_trip("STO", "CPH", 0, 9, company="ollex", unit=1)
    result = engine.evaluate(report_of(blocked, s), REG)
    assert blocked.id not in result.reachable


def test_reachability_follows_the_company_that_can_serve_it():
    """A hop is only usable by a company the constraint actually permits."""
    s_ollex = mk_trip("STO", "CPH", 0, 9, company="ollex", unit=1)
    s_vilneda = mk_trip("BGO", "CPH", 0, 9, company="vilneda", unit=1)
    # Reachable from either arrival, but only Vilneda may serve it.
    hop = mk_trip("CPH", "OSL", 10, 12, yn="Y", constraint="vilneda")
    # Downstream of hop, and Ollex is excluded — still fine, Vilneda carries it.
    tail = mk_trip("OSL", "BGO", 14, 16, yn="N", constraint="ollex")
    result = engine.evaluate(report_of(s_ollex, s_vilneda, hop, tail), REG)
    assert {hop.id, tail.id} <= result.reachable


def test_canceled_trips_are_not_reachable_demand():
    d = mk_trip("CPH", "OSL", 10, 12, status=Status.CANCELED)
    s = mk_trip("STO", "CPH", 0, 9, company="ollex", unit=1)
    result = engine.evaluate(report_of(d, s), REG)
    assert d.id not in result.reachable


def test_adhoc_assignments_generate_no_reachability():
    """Ad hoc is chain-invisible, so it must not open any chain."""
    d = mk_trip("CPH", "OSL", 10, 12)
    adhoc = mk_trip("STO", "CPH", 0, 9, company="ollex")   # no unit number
    result = engine.evaluate(report_of(d, adhoc), REG)
    assert d.id not in result.reachable


def test_every_current_chain_candidate_is_reachable():
    """The invariant the board's three-state colouring rests on: a trip you
    can accept right now can never be painted as unreachable.

    Holds for candidates that need no empty run only. `reachable` excludes
    repositioning on purpose (see reach.py) — it answers "reachable without
    spending an empty run" — so a repositioning suggestion may well point at a
    trip outside the set. That gap has always been in the design; it was
    invisible here until the pair-dependent windows widened the reposition
    window enough for this fixture to hit it.
    """
    trips = [
        mk_trip("STO", "CPH", 0, 9, company="ollex", unit=1),
        mk_trip("BGO", "OSL", 0, 9, company="vilneda", unit=1),
        mk_trip("CPH", "OSL", 10, 12),
        mk_trip("OSL", "BGO", 11, 13),
        mk_trip("OSL", "STO", 14, 16),
        mk_trip("HEL", "STO", 14, 16),
    ]
    result = engine.evaluate(report_of(*trips), REG)
    has_chain = {
        tid for tid, ss in result.suggestions_by_trip.items()
        if any(s.kind == "chain" and not s.reposition for s in ss)
    }
    assert has_chain
    assert has_chain <= result.reachable
