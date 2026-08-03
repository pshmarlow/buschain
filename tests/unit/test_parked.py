"""Parked mid-chain windows: what is reported, what is excluded, and why.

The module under test never restates a rule — insertion_ok is the gate — so
these tests pin the two things parked.py itself decides: where a window
opens (strictly past the chain ceiling), and which legal placements are
worth listing (the ones the planner cannot propose).
"""
from buschain import engine
from buschain.engine import graph, parked
from tests.helpers import mk_registry, mk_trip, report_of


def unit(*trips) -> dict[tuple[str, int], list]:
    return {("ollex", 1): list(trips)}


def test_window_opens_only_past_the_chain_ceiling():
    """Same-city pair: floor 1 + 3 rest days means gap 4 is still chain
    territory; gap 5 is a parked window. The complement of feasible_edges'
    ceiling, so a window can never overlap what the planner already works."""
    a = mk_trip("STO", "CPH", 0, 3, company="ollex", unit=1)
    chainable = mk_trip("CPH", "OSL", 7, 9, company="ollex", unit=1)
    assert parked.windows([], unit(a, chainable)) == ()
    wide = mk_trip("CPH", "OSL", 8, 9, company="ollex", unit=1)
    (w,) = parked.windows([], unit(a, wide))
    assert w.days == 5 and w.city == "CPH" and w.fits == ()


def test_window_ceiling_rides_on_the_pair_floor():
    """A CPH->OSL closing pair has floor 2, so the window opens at gap 6 —
    the ceiling shifts with the pair exactly as it does in feasible_edges."""
    a = mk_trip("STO", "CPH", 0, 3, company="ollex", unit=1)
    b5 = mk_trip("OSL", "STO", 8, 10, company="ollex", unit=1)
    assert parked.windows([], unit(a, b5)) == ()
    b6 = mk_trip("OSL", "STO", 9, 10, company="ollex", unit=1)
    assert len(parked.windows([], unit(a, b6))) == 1


def test_a_tail_is_not_a_window():
    """No closing trip, no window: repositioning off a tail is the planner's
    own business (feasible_edges emits those edges itself)."""
    a = mk_trip("STO", "CPH", 0, 3, company="ollex", unit=1)
    assert parked.windows([], unit(a)) == ()


def test_cross_city_fit_reports_both_empty_run_floors():
    """The measured live case in miniature: bus parked in CPH, trip departs
    OSL (2-day drive out), lands in BGO with the closing trip leaving from
    STO (3-day drive back — BGO-STO is not on the short list)."""
    a = mk_trip("STO", "CPH", 0, 3, company="ollex", unit=1)
    b = mk_trip("STO", "OSL", 40, 42, company="ollex", unit=1)
    d = mk_trip("OSL", "BGO", 6, 8)
    (w,) = parked.windows([d], unit(a, b))
    (f,) = w.fits
    assert (f.er_out_days, f.er_back_days, f.gap_out) == (2, 3, 3)
    assert f.chains is True            # inside the reposition window


def test_planner_edges_are_not_repeated():
    """A same-city fit inside the chain window IS a feasible edge — the
    suggestion list already carries it, so the parked list must not."""
    a = mk_trip("STO", "CPH", 0, 3, company="ollex", unit=1)
    b = mk_trip("STO", "OSL", 40, 42, company="ollex", unit=1)
    d = mk_trip("CPH", "STO", 5, 7)
    assert graph.feasible_edges([a], [d], unit(a, b))
    (w,) = parked.windows([d], unit(a, b))
    assert w.fits == ()


def test_no_fit_duplicates_a_feasible_edge():
    """The exclusion pinned as a property (mirror of test_links.py's pinning):
    whatever the parked list reports, feasible_edges must not also emit."""
    a = mk_trip("STO", "CPH", 0, 3, company="ollex", unit=1)
    b = mk_trip("STO", "OSL", 40, 42, company="ollex", unit=1)
    ds = [mk_trip("CPH", "STO", 5, 7), mk_trip("OSL", "BGO", 6, 8),
          mk_trip("CPH", "STO", 15, 17), mk_trip("HEL", "HEL", 20, 21)]
    (w,) = parked.windows(ds, unit(a, b))
    edge_demands = {e.demand.id for e in graph.feasible_edges(
        [a, b], ds, unit(a, b), reposition=True)}
    assert w.fits
    assert not {f.demand for f in w.fits} & edge_demands


def test_deep_same_city_fit_costs_no_drive():
    """Beyond the chain window the planner is blind even same-city; the fit
    is listed with zero drive on both sides and no link to record."""
    a = mk_trip("STO", "CPH", 0, 3, company="ollex", unit=1)
    b = mk_trip("STO", "OSL", 40, 42, company="ollex", unit=1)
    d = mk_trip("CPH", "STO", 15, 17)
    (w,) = parked.windows([d], unit(a, b))
    (f,) = w.fits
    assert (f.er_out_days, f.er_back_days) == (0, 0)   # arrives where B departs
    assert f.chains is False


def test_er_marked_closer_charges_no_return_leg():
    """An ER-marked closing trip carries its own approved empty run (rule 6),
    so the fit is not billed for reaching its departure city."""
    a = mk_trip("STO", "CPH", 0, 3, company="ollex", unit=1)
    b = mk_trip("STO", "OSL", 40, 42, company="ollex", unit=1, er=True)
    d = mk_trip("OSL", "BGO", 6, 8)
    (w,) = parked.windows([d], unit(a, b))
    assert w.fits[0].er_back_days == 0


def test_a_fit_may_not_strand_the_closing_trip():
    """The return floor is enforced (by insertion_ok, not re-stated here):
    landing in BGO two days before the closing trip departs STO — a 3-day
    move — is refused. Exactly the stranding the tail-only rule fears, tested
    instead of assumed."""
    a = mk_trip("STO", "CPH", 0, 3, company="ollex", unit=1)
    b = mk_trip("STO", "OSL", 40, 42, company="ollex", unit=1)
    strands = mk_trip("OSL", "BGO", 6, 38)
    (w,) = parked.windows([strands], unit(a, b))
    assert w.fits == ()


def test_hard_constraints_keep_a_demand_out():
    """Rule 3 via the one gate: a Y-constrained trip naming another company
    never appears in this company's window."""
    a = mk_trip("STO", "CPH", 0, 3, company="ollex", unit=1)
    b = mk_trip("STO", "OSL", 40, 42, company="ollex", unit=1)
    wrong = mk_trip("OSL", "BGO", 6, 8, yn="Y", constraint="vilneda")
    (w,) = parked.windows([wrong], unit(a, b))
    assert w.fits == ()


def test_fits_rank_cheapest_drive_first():
    a = mk_trip("STO", "CPH", 0, 3, company="ollex", unit=1)
    b = mk_trip("STO", "OSL", 40, 42, company="ollex", unit=1)
    deep_free = mk_trip("CPH", "STO", 15, 17)          # no drive at all
    crossing = mk_trip("OSL", "BGO", 6, 8)             # 2 out + 3 back
    (w,) = parked.windows([crossing, deep_free], unit(a, b))
    assert [f.er_out_days + f.er_back_days for f in w.fits] == [0, 5]
    assert w.fits[0].demand == deep_free.id


def test_evaluate_carries_parked_and_locks_empty_it():
    """Wired through evaluate: the window rides EngineResult, and a locked
    demand leaves the fit list the same way it leaves every other proposal
    surface — via the demand set, not a special case here."""
    a = mk_trip("STO", "CPH", 0, 3, company="ollex", unit=1)
    b = mk_trip("STO", "OSL", 40, 42, company="ollex", unit=1)
    d = mk_trip("OSL", "BGO", 6, 8)
    report = report_of(a, b, d)
    res = engine.evaluate(report, mk_registry(ollex=1))
    assert res.parked and res.parked[0].fits[0].demand == d.id
    res = engine.evaluate(report, mk_registry(ollex=1), locked=frozenset({d.id}))
    assert res.parked[0].fits == ()
