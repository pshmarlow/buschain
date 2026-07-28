"""Rule 9: ranked candidates, gap ascending, deterministic."""
from buschain import engine
from buschain.model import Status
from tests.helpers import mk_registry, mk_trip, report_of

REG = mk_registry(ollex=3)


def test_gap_ascending_within_group():
    d = mk_trip("CPH", "OSL", 10, 12)
    s_far = mk_trip("STO", "CPH", 0, 6, company="ollex", unit=1)    # gap 4
    s_near = mk_trip("STO", "CPH", 0, 9, company="ollex", unit=2)   # gap 1
    s_mid = mk_trip("STO", "CPH", 0, 8, company="ollex", unit=3)    # gap 2
    result = engine.evaluate(report_of(d, s_far, s_near, s_mid), REG)
    gaps = [s.gap_days for s in result.suggestions_by_trip[d.id]]
    assert gaps == [1, 2, 4]
    ranks = [s.rank for s in result.suggestions_by_trip[d.id]]
    assert ranks == [0, 1, 2]


def test_empty_runs_rank_on_calendar_gap_not_slack():
    """Among repositioning options the bus that is back in service soonest
    wins, even though both moves leave it zero spare days: driving is time out
    of service too. Plan.idle_days deliberately accounts differently — it
    measures waste, this measures availability."""
    s = mk_trip("OSL", "STO", 18, 20, company="ollex", unit=1)
    near = mk_trip("CPH", "OSL", 22, 24)                  # gap 2, one-day pair
    far = mk_trip("Hamburg", "CPH", 23, 25)               # gap 3, default pair
    result = engine.evaluate(report_of(s, near, far), REG)
    order = [(sg.demand, sg.gap_days)
             for sg in result.suggestions_by_unit[("ollex", 1)]]
    assert order == [(near.id, 2), (far.id, 3)]


def test_idle_days_ignores_the_travel_a_move_requires():
    """The other half of the split: a minimum-gap empty run wasted nothing,
    however far it went."""
    s = mk_trip("OSL", "STO", 18, 20, company="ollex", unit=1)
    far = mk_trip("Hamburg", "CPH", 23, 25)               # gap 3, all of it travel
    assert engine.evaluate(report_of(s, far), REG).plan.idle_days == 0


def test_fewer_at_risk_wins_on_equal_gap():
    d = mk_trip("CPH", "OSL", 10, 12)
    s_pending = mk_trip("STO", "CPH", 0, 9, company="ollex", unit=1,
                        status=Status.PENDING)
    s_firm = mk_trip("STO", "CPH", 0, 9, company="ollex", unit=2)
    result = engine.evaluate(report_of(d, s_pending, s_firm), REG)
    first, second = result.suggestions_by_trip[d.id]
    assert first.supply == s_firm.id and first.at_risk == ()
    assert second.at_risk == (s_pending.id,)


def test_both_directions_carry_same_links():
    d = mk_trip("CPH", "OSL", 10, 12)
    s = mk_trip("STO", "CPH", 0, 9, company="ollex", unit=1)
    result = engine.evaluate(report_of(d, s), REG)
    (by_trip,) = result.suggestions_by_trip[d.id]
    (by_unit,) = result.suggestions_by_unit[("ollex", 1)]
    assert (by_trip.supply, by_trip.demand) == (by_unit.supply, by_unit.demand)


def test_evaluate_is_deterministic():
    trips = [
        mk_trip("STO", "CPH", 0, 6, company="ollex", unit=1),
        mk_trip("OSL", "CPH", 1, 7, company="ollex", unit=2),
        mk_trip("CPH", "OSL", 8, 12),
        mk_trip("CPH", "BGO", 9, 14),
    ]
    r1 = engine.evaluate(report_of(*trips), REG)
    r2 = engine.evaluate(report_of(*trips), REG)
    assert r1 == r2
