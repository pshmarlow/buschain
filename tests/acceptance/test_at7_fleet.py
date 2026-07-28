"""AT7: days where a company exceeds its fleet are reported.

The fleet is the company's numbered-unit count, and only numbered-unit trips
count against it — see engine.checks.fleet_warnings for why this narrows the
brief's wording.
"""
from collections import defaultdict
from datetime import timedelta

from hypothesis import given, settings

from buschain import contract, engine
from buschain.model import Status
from tests.strategies import registries, reports


def assert_fleet_reported(report, registry):
    result = engine.evaluate(report, registry)
    # Recompute the day counts independently and compare both directions.
    counts = defaultdict(lambda: defaultdict(int))
    for t in report.trips:
        if (t.status is Status.CANCELED or t.assignment is None
                or t.assignment.unit is None):
            continue
        day = t.start
        while day <= t.end:
            counts[t.assignment.company][day] += 1
            day += timedelta(days=1)
    expected = set()
    for company_key, days in counts.items():
        company = registry.companies.get(company_key)
        if company is None:
            continue
        for day, n in days.items():
            if n > company.fleet_size:
                expected.add((company_key, day, n, company.fleet_size))
    reported = {(w.company, w.day, w.count, w.fleet_size)
                for w in result.fleet_warnings}
    assert reported == expected


@given(report=reports(), registry=registries())
@settings(max_examples=150, deadline=None)
def test_property_fleet(report, registry):
    assert_fleet_reported(report, registry)


def test_real_data_fleet(dataset, registry):
    report = contract.validate(dataset, registry)
    assert_fleet_reported(report, registry)
