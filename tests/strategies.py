"""Hypothesis strategies for engine property tests.

Generation is deliberately constrained (small pools of cities/companies,
short date window) so that chains, constraint hits, and fleet collisions
actually occur instead of being vanishingly rare.
"""
from __future__ import annotations

from datetime import timedelta

from hypothesis import strategies as st

from buschain.model import Assignment, ContractReport, Status, Trip, TripId
from tests.helpers import BASE, mk_registry

CITIES = ("STO", "OSL", "CPH", "BGO")
COMPANIES = {"ollex": 2, "kautra": 3, "vilneda": 1}


def registries():
    return st.just(mk_registry(**COMPANIES))


@st.composite
def trips(draw, row: int):
    start_off = draw(st.integers(min_value=0, max_value=30))
    duration = draw(st.integers(min_value=0, max_value=8))
    status = draw(st.sampled_from([Status.CONFIRMED, Status.CONFIRMED,
                                   Status.PENDING, Status.CANCELED]))
    company = draw(st.sampled_from([None, *COMPANIES]))
    assignment = None
    if company is not None:
        max_unit = COMPANIES[company]
        unit = draw(st.sampled_from([None, *range(1, max_unit + 1)]))
        assignment = Assignment(company=company, unit=unit)
    yn = constraint = None
    if assignment is None and draw(st.booleans()):
        yn = draw(st.sampled_from(["Y", "N"]))
        constraint = draw(st.sampled_from(sorted(COMPANIES)))
    return Trip(
        id=TripId("sheet", row),
        status=status, project=None, yn=yn, constraint_company=constraint,
        start=BASE + timedelta(days=start_off),
        end=BASE + timedelta(days=start_off + duration),
        dep_city=draw(st.sampled_from(CITIES)),
        arr_city=draw(st.sampled_from(CITIES)),
        pax=None, assignment=assignment,
        er_marked=draw(st.booleans()),
    )


@st.composite
def reports(draw, min_trips: int = 1, max_trips: int = 25):
    n = draw(st.integers(min_value=min_trips, max_value=max_trips))
    all_trips = tuple(draw(trips(row=i + 2)) for i in range(n))
    return ContractReport(trips=all_trips, non_operable=())
