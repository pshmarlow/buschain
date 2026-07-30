"""Rule 10: a trip whose Pax is not 49 may only go to an ad hoc bus.

The chartered coaches are 49-seaters, so a booking for any other party size is
work the fleet cannot take. Every gate that hands a trip to a NUMBERED unit has
to say so — the suggester, the reachability walk, the multi-hop plan, the exact
solver, and the board's drop — or one of them would offer what another refuses.
"""
import pytest

from buschain import engine
from buschain.engine import graph, solve
from tests.helpers import mk_registry, mk_trip, report_of

REG = mk_registry(ollex=2, kautra=1)


def _supply():
    return mk_trip("STO", "CPH", 0, 3, company="ollex", unit=1)


def test_non_49_demand_gets_no_chain_suggestion():
    # Identical trips but for the Pax: the 49 one chains off the same arrival,
    # which is what makes the other one's absence about capacity and nothing
    # else.
    s = _supply()
    standard = mk_trip("CPH", "OSL", 5, 7, pax=49)
    oversize = mk_trip("CPH", "OSL", 5, 7, pax=55)
    result = engine.evaluate(report_of(s, standard, oversize), REG)
    assert result.suggestions_by_trip[standard.id]
    assert oversize.id not in result.suggestions_by_trip


@pytest.mark.parametrize("pax", [5, 48, 50, 72])
def test_any_other_party_size_is_barred_in_both_directions(pax):
    # The rule is "different from 49", not "larger than 49": a minibus job is
    # no more the charter's work than a coachload of 72.
    s = _supply()
    d = mk_trip("CPH", "OSL", 5, 7, pax=pax)
    result = engine.evaluate(report_of(s, d), REG)
    assert d.id not in result.suggestions_by_trip
    assert d.id not in result.reachable
    assert d.id not in result.plan.covered


def test_blank_pax_still_chains():
    # The decision the rule rests on: unstated is not "not 49". Paste and manual
    # entry routinely produce rows with no Pax, and dropping those out of the
    # fleet's reach would be a silent loss of capacity.
    s = _supply()
    d = mk_trip("CPH", "OSL", 5, 7, pax=None)
    result = engine.evaluate(report_of(s, d), REG)
    assert result.suggestions_by_trip[d.id]
    assert d.id in result.reachable


def test_non_49_is_unreachable_rather_than_waiting():
    # reach.py answers "could any sequence of accepts ever get a bus here". For
    # a non-49 trip the answer is no at any fleet size, which is precisely how
    # the board tells an ad hoc job apart from a trip waiting its turn.
    s = _supply()
    hop = mk_trip("CPH", "OSL", 5, 7, pax=49)
    beyond = mk_trip("OSL", "BGO", 9, 11, pax=19)
    result = engine.evaluate(report_of(s, hop, beyond), REG)
    assert hop.id in result.reachable          # two hops out, still reachable
    assert beyond.id not in result.reachable


def test_multi_hop_plan_never_covers_a_non_49_trip():
    s = _supply()
    first = mk_trip("CPH", "OSL", 5, 7, pax=49)
    second = mk_trip("OSL", "BGO", 9, 11, pax=72)
    result = engine.evaluate(report_of(s, first, second), REG)
    assert first.id in result.plan.covered
    assert second.id not in result.plan.covered


def test_solver_leaves_non_49_trips_uncovered():
    pytest.importorskip("pulp", reason="solver extra not installed")
    s = _supply()
    standard = mk_trip("CPH", "OSL", 5, 7, pax=49)
    oversize = mk_trip("CPH", "OSL", 5, 7, pax=55)
    solved = solve.build_plan(
        [standard, oversize], {("ollex", 1): [s]})
    assert solved.plan.covered == frozenset({standard.id})
    # Certified replay refuses nothing: the ILP never proposed the illegal link
    # in the first place, which is the property that keeps the two in step.
    assert solved.refused == 0


def test_drop_refuses_every_numbered_unit_but_keeps_the_adhoc_lanes():
    # The escape hatch has to survive: a person dragging the bar still needs
    # somewhere to put it, and ad hoc IS the answer the rule gives.
    d = mk_trip("CPH", "OSL", 5, 7, pax=19)
    targets, refused = engine.drop_targets(report_of(_supply(), d), REG, d.id)
    assert all(t.unit is None for t in targets)
    assert {t.company for t in targets} == {"ollex", "kautra"}
    assert {(r.company, r.unit) for r in refused} == {
        ("ollex", 1), ("ollex", 2), ("kautra", 1)}
    # A refusal that cannot name its rule is the one thing this board avoids.
    assert all("19 pax" in r.reason and "rule 10" in r.reason for r in refused)


def test_imported_non_49_on_a_numbered_bus_is_listed_not_hidden():
    # Nothing in-system can create this; an imported sheet can, and the sheet
    # says what it says. Same stance as every other rule-6 finding.
    t = mk_trip("STO", "CPH", 0, 3, company="ollex", unit=1, pax=55)
    result = engine.evaluate(report_of(t), REG)
    finding, = result.integrity
    assert finding.kind == "pax_mismatch"
    assert (finding.company, finding.unit) == ("ollex", 1)
    # One trip, not a pair — both ids name it (see checks.unit_integrity).
    assert finding.earlier == finding.later == t.id


def test_adhoc_non_49_is_not_a_finding():
    t = mk_trip("STO", "CPH", 0, 3, company="ollex", pax=55)
    result = engine.evaluate(report_of(t), REG)
    assert result.integrity == ()


def test_rule_10_does_not_touch_supply():
    # The rule governs what a bus may PICK UP. A numbered bus already carrying
    # an odd-sized party (an imported row) still arrives somewhere and still
    # frees up — refusing its arrival as supply would strand the next trip over
    # a row the planner cannot fix.
    odd = mk_trip("STO", "CPH", 0, 3, company="ollex", unit=1, pax=55)
    d = mk_trip("CPH", "OSL", 5, 7, pax=49)
    result = engine.evaluate(report_of(odd, d), REG)
    assert [s.supply for s in result.suggestions_by_trip[d.id]] == [odd.id]


def test_predicate_boundary():
    assert graph.CHARTERED_PAX == 49
    assert graph.numbered_unit_ok(mk_trip("STO", "CPH", 0, 1, pax=49))
    assert graph.numbered_unit_ok(mk_trip("STO", "CPH", 0, 1, pax=None))
    assert not graph.numbered_unit_ok(mk_trip("STO", "CPH", 0, 1, pax=0))
