"""Shared builders for engine tests: compact trip/registry construction."""
from __future__ import annotations

from datetime import date, timedelta

from buschain.model import (
    Assignment, Company, ContractReport, Registry, Status, Trip, TripId,
)

BASE = date(2026, 4, 1)


def mk_registry(**fleets: int | None) -> Registry:
    """mk_registry(ollex=2, vilneda=None) -> companies with units 1..fleet.

    fleet is the unit count; Company.fleet_size derives from it, so there is
    no way to express the two disagreeing (and no reason to).
    """
    companies = {}
    for name, fleet in fleets.items():
        units = frozenset(range(1, (fleet or 0) + 1))
        companies[name] = Company(name=name.capitalize(), note=None, units=units)
    cities = {c.casefold(): c for c in ("STO", "OSL", "CPH", "BGO", "HEL")}
    return Registry(companies=companies, cities=cities)


_seq = 0


def mk_trip(
    dep: str, arr: str, start: int, end: int, *,
    status: Status = Status.CONFIRMED,
    company: str | None = None, unit: int | None = None,
    yn: str | None = None, constraint: str | None = None,
    er: bool = False, row: int | None = None, pax: int | None = None,
) -> Trip:
    """Days are offsets from BASE so chains read as small integers.

    pax defaults to None — unstated, which rule 10 lets onto any bus — so a test
    that is not about capacity never has to mention it.
    """
    global _seq
    _seq += 1
    assignment = Assignment(company, unit) if company else None
    return Trip(
        id=TripId("sheet", row if row is not None else 1000 + _seq),
        status=status, project=None,
        yn=yn, constraint_company=constraint,  # type: ignore[arg-type]
        start=BASE + timedelta(days=start), end=BASE + timedelta(days=end),
        dep_city=dep, arr_city=arr, pax=pax,
        assignment=assignment, er_marked=er,
    )


def report_of(*trips: Trip) -> ContractReport:
    return ContractReport(trips=tuple(trips), non_operable=())
