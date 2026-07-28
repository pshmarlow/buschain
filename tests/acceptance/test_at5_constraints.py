"""AT5: no suggestion ever violates a Y/N rule; a Y-row with no feasible
numbered chain receives the ad hoc fallback of that company."""
from hypothesis import given, settings

from buschain import contract, engine
from tests.strategies import registries, reports


def assert_constraints_respected(report, registry):
    result = engine.evaluate(report, registry)
    by_id = {t.id: t for t in report.trips}
    chain_covered = set()
    for suggestions in result.suggestions_by_trip.values():
        for s in suggestions:
            demand = by_id[s.demand]
            if demand.yn == "Y":
                assert s.company == demand.constraint_company
            elif demand.yn == "N":
                assert s.company != demand.constraint_company
            if s.kind == "chain":
                chain_covered.add(s.demand)
            else:
                assert s.kind == "adhoc_fallback" and s.unit is None
    for t in report.trips:
        if t.yn == "Y" and t.assignment is None and t.status.value != "Canceled":
            suggestions = result.suggestions_by_trip.get(t.id, ())
            assert suggestions, "Y-row must get a chain or the ad hoc fallback"


@given(report=reports(), registry=registries())
@settings(max_examples=200, deadline=None)
def test_property_constraints(report, registry):
    assert_constraints_respected(report, registry)


def test_real_data_constraints(dataset, registry):
    report = contract.validate(dataset, registry)
    assert_constraints_respected(report, registry)
