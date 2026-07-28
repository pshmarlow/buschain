"""AT6: canceled trips never appear in matching output; ad hoc assignments
never appear as supply."""
from hypothesis import given, settings

from buschain import contract, engine
from buschain.model import Status
from tests.strategies import registries, reports


def assert_exclusions(report, registry):
    result = engine.evaluate(report, registry)
    by_id = {t.id: t for t in report.trips}
    canceled = {t.id for t in report.trips if t.status is Status.CANCELED}
    adhoc = {t.id for t in report.trips
             if t.assignment is not None and t.assignment.unit is None}

    for suggestions in (*result.suggestions_by_trip.values(),
                        *result.suggestions_by_unit.values()):
        for s in suggestions:
            assert s.demand not in canceled
            assert s.supply not in canceled
            assert s.supply not in adhoc, "ad hoc arrivals are not supply"
            if s.supply is not None:
                supply = by_id[s.supply]
                assert supply.assignment is not None
                assert supply.assignment.unit is not None

    for trip_ids in result.timelines.values():
        assert not (set(trip_ids) & canceled)
        assert not (set(trip_ids) & adhoc)


@given(report=reports(), registry=registries())
@settings(max_examples=200, deadline=None)
def test_property_exclusions(report, registry):
    assert_exclusions(report, registry)


def test_real_data_exclusions(dataset, registry):
    report = contract.validate(dataset, registry)
    assert_exclusions(report, registry)
