"""Rule 1 window, city equality, and unit-timeline consistency (rule 6)."""
from hypothesis import given, settings

from buschain import engine
from buschain.engine import graph
from tests.helpers import mk_trip
from tests.strategies import reports


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


def test_trip_overlapping_the_supply_does_not_block():
    """The other half of _unit_timeline_ok's tolerance (graph.py): a trip that
    already sits on top of the supply is a pre-existing violation the integrity
    report enumerates, not a veto over every future link — otherwise one bad row
    freezes its whole unit.

    Covered here rather than by the reference data: the workbook's four overlap
    findings all came from typo'd-year rows, and the contract now refuses those
    (contract.MAX_TRIP_SPAN_DAYS), so no real row exercises this branch anymore.
    """
    s = mk_trip("STO", "CPH", 0, 3, company="ollex", unit=1)
    # Already sitting on top of s, and so already an integrity finding.
    squatter = mk_trip("OSL", "OSL", 1, 2, company="ollex", unit=1)
    d = mk_trip("CPH", "OSL", 5, 7)
    assert edges_for(s, d, unit_trips=[s, squatter])
    # The tolerance is only for overlaps with the SUPPLY: the same trip moved
    # clear of s still blocks the gap, as test_existing_trip_in_gap_blocks_edge
    # expects. Without this the test would pass on a graph.py that ignored
    # every existing trip.
    clear_of_supply = mk_trip("OSL", "OSL", 5, 6, company="ollex", unit=1)
    assert not edges_for(s, d, unit_trips=[s, clear_of_supply])


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


# ---------- insertion_ok: the same rulebook, asked from the timeline's side ----------
#
# The board's drag gesture gates on insertion_ok, the planner proposes from
# feasible_edges. Two rule surfaces are two places for the rules to be wrong,
# so the property below pins them together and the tests around it document
# the places they deliberately answer different questions.


def _runnable(trips):
    """Could one bus actually run this timeline as it stands?

    No two trips at once, and every consecutive pair leaves time to get from
    one arrival city to the next departure. NOT the same as "every pair is a
    valid chain": a bus with a fortnight between trips, or one driven to
    another city, is running a perfectly possible timeline that rule 1 would
    never have proposed. This is the precondition under which the planner's
    rulebook and the board's are required to agree; both apply tolerance
    outside it, and they do not have to tolerate identically.
    """
    ts = sorted(trips, key=lambda t: (t.start, t.end))
    return all(
        a.end < b.start
        and (b.start - a.end).days >= graph.min_gap_days(a.arr_city, b.dep_city)
        for a, b in zip(ts, ts[1:])
    )


@settings(max_examples=200)
@given(reports())
def test_every_feasible_edge_is_a_legal_insertion(report):
    """Whatever the planner proposes, the board must accept as a drop.

    The alternative is a board that refuses a link it is simultaneously
    suggesting — the single most confusing thing this pair of functions could
    do. Also pins the derived numbers: a drop records the same supply, gap and
    ER flag the accepted suggestion would have.

    Restricted to timelines a bus could run (see _runnable), which is where the
    two are required to agree. The two documented divergences on timelines that
    are already impossible are covered by name below.
    """
    # No date bounds: this is about whether the rules agree with each other,
    # and a window would only shrink the generated demand the property runs on.
    _, supply, demand, unit_assignments, _, _ = engine._split(
        report, frozenset(), None, None)
    for reposition in (False, True):
        for e in graph.feasible_edges(supply, demand, unit_assignments, reposition):
            key = (e.supply.assignment.company, e.supply.assignment.unit)
            unit_trips = unit_assignments[key]
            if not _runnable(unit_trips):
                continue
            spot = graph.insertion_ok(e.demand, unit_trips, key[0], reposition)
            assert spot is not None, f"edge {e.supply.id}->{e.demand.id} refused as a drop"
            assert spot.predecessor is not None
            assert spot.predecessor.id == e.supply.id
            assert spot.gap_days == e.gap_days
            assert spot.reposition == e.reposition
            # A proposed link dropped by hand must record the same link.
            assert spot.chains


def test_insertion_offers_an_idle_bus():
    """The reason this function exists. A unit with no trips generates no
    supply, so feasible_edges cannot propose it — and most of the fleet is
    parked most of the time. Nothing constrains an empty timeline, so the only
    possible refusal is rule 3."""
    d = mk_trip("CPH", "OSL", 4, 6)
    spot = graph.insertion_ok(d, [], "ollex")
    assert spot is not None and spot.predecessor is None and spot.gap_days is None
    assert not spot.chains, "nothing to chain from means no link to record"
    assert not graph.insertion_ok(
        mk_trip("CPH", "OSL", 4, 6, yn="Y", constraint="kautra"), [], "ollex")


def test_insertion_ahead_of_a_units_first_trip():
    """The other case with no edge behind it: the trip lands first, so there is
    no supply to chain from and no link to record. It still has to leave the
    bus time to reach what was previously first."""
    first = mk_trip("CPH", "STO", 8, 10, company="ollex", unit=1)
    ok = mk_trip("OSL", "CPH", 4, 6)                   # arrives CPH day 6, gap 2
    spot = graph.insertion_ok(ok, [first], "ollex")
    assert spot.predecessor is None and spot.successor_gap == 2
    # Same slot, arriving in STO: the bus would have two days to reach CPH,
    # and STO-CPH is a one-clear-day pair, so it still makes it.
    assert graph.insertion_ok(mk_trip("OSL", "STO", 4, 6), [first], "ollex")
    # Arriving in BGO it does not: BGO-CPH needs two clear days, and the
    # successor would depart before the bus could be there.
    assert graph.insertion_ok(mk_trip("OSL", "BGO", 4, 6), [first], "ollex") is None


def test_insertion_allows_a_park_the_planner_would_never_propose():
    """The line: impossible is refused, merely idle is enumerated.

    A bus finishing in CPH and departing CPH eleven days later breaks nothing —
    it is 34 of the 183 consecutive pairs the reference workbook already
    records, and checks.unit_integrity lists every one of them. Refusing it
    here would make the board refuse what the sheet in front of the planner
    plainly does. No link is recorded, because eleven days is not a handover.
    """
    tail = mk_trip("STO", "CPH", 0, 3, company="ollex", unit=1)
    parked = mk_trip("CPH", "OSL", 14, 16)
    spot = graph.insertion_ok(parked, [tail], "ollex")
    assert spot is not None and spot.gap_days == 11
    assert not spot.chains
    assert not edges_for(tail, parked), "and the planner still will not propose it"

    # The floor is the part that is physical, and it still refuses.
    too_soon = mk_trip("CPH", "OSL", 3, 5)              # same-day handover
    assert graph.insertion_ok(too_soon, [tail], "ollex") is None


def test_insertion_refuses_a_drive_that_does_not_fit():
    """The other impossible case: the gap exists but the empty run does not fit
    inside it. Hamburg is on no short-pair list, so it needs two clear days."""
    tail = mk_trip("OSL", "STO", 0, 3, company="ollex", unit=1)
    assert graph.insertion_ok(mk_trip("Hamburg", "OSL", 5, 7), [tail], "ollex") is None
    assert graph.insertion_ok(mk_trip("Hamburg", "OSL", 6, 8), [tail], "ollex")


def test_insertion_records_an_empty_run_only_when_they_are_enabled():
    """reposition governs the link, not the legality. A bus with a fortnight to
    spare can be driven anywhere without anyone approving an ER; one crossing
    inside rule 1's window is the ER rule 6 wants recorded, so it is only
    written when empty runs are on."""
    tail = mk_trip("OSL", "STO", 0, 3, company="ollex", unit=1)
    near = mk_trip("CPH", "OSL", 5, 7)                 # STO->CPH, gap 2: an ER
    assert graph.insertion_ok(near, [tail], "ollex", reposition=True).chains
    off = graph.insertion_ok(near, [tail], "ollex", reposition=False)
    assert off is not None and not off.chains and off.reposition


def test_insertion_refuses_an_overlap_feasible_edges_tolerates():
    """First deliberate divergence, and the direction it runs in.

    feasible_edges ignores trips that overlap the SUPPLY (see
    test_trip_overlapping_the_supply_does_not_block) so one bad source row
    cannot freeze a unit. insertion_ok refuses any trip overlapping the trip
    being placed, because that is a person putting a bus in two places at once
    right now — a decision to refuse, not a data condition to tolerate.
    """
    s = mk_trip("STO", "CPH", 0, 3, company="ollex", unit=1)
    squatter = mk_trip("OSL", "OSL", 1, 5, company="ollex", unit=1)   # overlaps s
    d = mk_trip("CPH", "OSL", 5, 7)                                   # overlaps squatter
    assert edges_for(s, d, unit_trips=[s, squatter])
    assert graph.insertion_ok(d, [s, squatter], "ollex") is None


def test_insertion_refuses_stranding_a_successor_the_planner_would_strand():
    """Second divergence, found by the property test above.

    The unit runs STO->STO on day 0 and OSL->STO on day 2. Those two are not a
    valid CHAIN (different cities, no ER), so _unit_timeline_ok treats the pair
    as already broken and lets the planner insert between them. But the bus
    could physically make that move — two days is enough for STO->OSL — and
    inserting a day-1 trip ending in STO leaves one day, which is not.

    So the drop refuses what the planner offers, on a timeline no bus could run
    after the insert. Fixing feasible_edges instead would change what the
    planner proposes, which is not this feature's business; the board simply
    declines to carry out the worse of the two answers.
    """
    a = mk_trip("STO", "STO", 0, 0, company="vilneda", unit=1)
    c = mk_trip("OSL", "STO", 2, 2, company="vilneda", unit=1)
    b = mk_trip("STO", "STO", 1, 1)
    assert edges_for(a, b, unit_trips=[a, c])
    spot, why = graph.insertion_verdict(b, [a, c], "vilneda")
    assert spot is None and "due in OSL" in why

    # And the tolerance still applies when the successor was ALREADY out of
    # reach: moving c's departure a day earlier makes a->c impossible on its
    # own, so the insert cannot be blamed for it.
    unreachable = mk_trip("OSL", "STO", 1, 1, company="vilneda", unit=1)
    assert graph.insertion_ok(mk_trip("STO", "STO", 3, 3), [a, unreachable], "vilneda")
