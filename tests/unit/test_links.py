"""Rule 1 window, city equality, and unit-timeline consistency (rule 6)."""
from buschain.engine import graph
from tests.helpers import mk_trip


def edges_for(supply, demand, unit_trips=None, reposition=False):
    key = (supply.assignment.company, supply.assignment.unit)
    return graph.feasible_edges(
        [supply], [demand], {key: unit_trips if unit_trips is not None else [supply]},
        reposition=reposition,
    )


def test_gap_window_boundaries():
    s = mk_trip("STO", "CPH", 0, 3, company="ollex", unit=1)
    assert not edges_for(s, mk_trip("CPH", "OSL", 3, 5))   # same-day handover
    assert edges_for(s, mk_trip("CPH", "OSL", 4, 5))       # D+1
    assert edges_for(s, mk_trip("CPH", "OSL", 7, 9))       # D+4 (3 rest days)
    assert not edges_for(s, mk_trip("CPH", "OSL", 8, 9))   # D+5


def test_reposition_window_on_a_short_pair():
    """The rule as stated: a unit ending in STO on the 20th may depart CPH on
    the 22nd at the earliest — one clear day for the empty run."""
    s = mk_trip("OSL", "STO", 18, 20, company="ollex", unit=1)
    for start, allowed in ((21, False), (22, True), (25, True), (26, False)):
        got = bool(edges_for(s, mk_trip("CPH", "OSL", start, start + 1),
                             reposition=True))
        assert got is allowed, f"CPH departure on day {start}"


def test_reposition_window_on_a_far_pair():
    """Anything not on the short list needs two clear days, and the three
    rest days ride on top of that rather than being eaten by the drive."""
    s = mk_trip("OSL", "STO", 18, 20, company="ollex", unit=1)
    for start, allowed in ((22, False), (23, True), (26, True), (27, False)):
        got = bool(edges_for(s, mk_trip("Hamburg", "CPH", start, start + 1),
                             reposition=True))
        assert got is allowed, f"Hamburg departure on day {start}"


def test_short_pair_list_is_not_transitive():
    """CPH-OSL and OSL-BGO are both one-day hops; CPH-BGO is not one, so it
    takes the default. Two short legs do not add up to a short route."""
    s = mk_trip("OSL", "CPH", 18, 20, company="ollex", unit=1)
    d = mk_trip("BGO", "OSL", 22, 23)                     # one clear day only
    assert not edges_for(s, d, reposition=True)
    assert graph.min_gap_days("CPH", "BGO") == 3


def test_short_pairs_apply_in_both_directions():
    for a, b in (("CPH", "OSL"), ("CPH", "STO"), ("OSL", "STO"), ("OSL", "BGO")):
        assert graph.min_gap_days(a, b) == graph.min_gap_days(b, a) == 2


def test_city_must_match_canonically():
    s = mk_trip("STO", "CPH", 0, 3, company="ollex", unit=1)
    assert not edges_for(s, mk_trip("OSL", "STO", 4, 6))


def test_existing_trip_in_gap_blocks_edge():
    s = mk_trip("STO", "CPH", 0, 3, company="ollex", unit=1)
    inbetween = mk_trip("CPH", "OSL", 5, 6, company="ollex", unit=1)
    d = mk_trip("CPH", "OSL", 7, 9)
    assert not edges_for(s, d, unit_trips=[s, inbetween])


def test_overlap_with_unit_schedule_blocks_edge():
    s = mk_trip("STO", "CPH", 0, 3, company="ollex", unit=1)
    later = mk_trip("OSL", "STO", 5, 8, company="ollex", unit=1)
    d = mk_trip("CPH", "OSL", 4, 6)                        # overlaps 'later'
    assert not edges_for(s, d, unit_trips=[s, later])


def test_insert_may_not_break_a_valid_successor_pair():
    s = mk_trip("STO", "CPH", 0, 3, company="ollex", unit=1)
    # successor currently chains from s: CPH day 7, gap 4.
    successor = mk_trip("CPH", "STO", 7, 9, company="ollex", unit=1)
    # demand arrives in OSL -> would break the valid s -> successor pair.
    d = mk_trip("CPH", "OSL", 4, 5)
    assert not edges_for(s, d, unit_trips=[s, successor])
    # demand arriving back in CPH keeps the chain intact -> allowed.
    d_ok = mk_trip("CPH", "CPH", 4, 5)
    assert edges_for(s, d_ok, unit_trips=[s, successor])


def test_already_broken_successor_pair_does_not_block():
    s = mk_trip("STO", "CPH", 0, 3, company="ollex", unit=1)
    # successor is ALREADY out of window from s (gap 7, an enumerated
    # integrity finding) — inserting demand cannot make the timeline worse.
    successor = mk_trip("BGO", "STO", 10, 12, company="ollex", unit=1)
    d = mk_trip("CPH", "OSL", 5, 7)
    assert edges_for(s, d, unit_trips=[s, successor])


def test_er_marked_successor_never_blocks():
    s = mk_trip("STO", "CPH", 0, 3, company="ollex", unit=1)
    successor_er = mk_trip("CPH", "STO", 7, 9, company="ollex", unit=1, er=True)
    d = mk_trip("CPH", "OSL", 4, 5)
    assert edges_for(s, d, unit_trips=[s, successor_er])
