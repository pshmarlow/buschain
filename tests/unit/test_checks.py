"""Fleet check (rule 5), unit integrity (rule 6), broken chains (rule 4)."""

from buschain import engine
from buschain.model import AcceptedLink, Status
from tests.helpers import BASE, mk_registry, mk_trip, report_of

REG = mk_registry(ollex=2, vilneda=1)


def test_fleet_overcommit_days_reported_exactly():
    # fleet 2, three simultaneous numbered trips on days 2..3 (unit 1 twice).
    t1 = mk_trip("STO", "CPH", 0, 3, company="ollex", unit=1)
    t2 = mk_trip("OSL", "CPH", 1, 5, company="ollex", unit=2)
    t3 = mk_trip("CPH", "OSL", 2, 3, company="ollex", unit=1)
    result = engine.evaluate(report_of(t1, t2, t3), REG)
    days = [(w.day - BASE).days for w in result.fleet_warnings]
    assert days == [2, 3]
    assert all(w.count == 3 and w.fleet_size == 2 for w in result.fleet_warnings)


def test_adhoc_trips_do_not_count_against_the_charter():
    """A bare-company assignment is a bus beyond the chartered fleet, so the
    charter count says nothing about it — counting them produced 1448
    warning-days of noise on the reference data."""
    t1 = mk_trip("STO", "CPH", 0, 3, company="ollex", unit=1)
    t2 = mk_trip("OSL", "CPH", 0, 3, company="ollex", unit=2)
    adhoc = [mk_trip("CPH", "OSL", 0, 3, company="ollex") for _ in range(5)]
    result = engine.evaluate(report_of(t1, t2, *adhoc), REG)
    assert result.fleet_warnings == ()


def test_canceled_trips_do_not_count_against_fleet():
    t1 = mk_trip("STO", "CPH", 0, 3, company="ollex", unit=1)
    t2 = mk_trip("OSL", "CPH", 0, 3, company="ollex", unit=2)
    t3 = mk_trip("CPH", "OSL", 0, 3, company="ollex", unit=1,
                 status=Status.CANCELED)
    result = engine.evaluate(report_of(t1, t2, t3), REG)
    assert result.fleet_warnings == ()


def test_company_with_no_units_cannot_overcommit():
    reg = mk_registry(ollex=None)
    t1 = mk_trip("STO", "CPH", 0, 3, company="ollex")
    t2 = mk_trip("OSL", "CPH", 0, 3, company="ollex")
    result = engine.evaluate(report_of(t1, t2), reg)
    assert result.fleet_warnings == ()


def test_integrity_kinds():
    a = mk_trip("STO", "CPH", 0, 3, company="ollex", unit=1)
    overlap = mk_trip("CPH", "OSL", 2, 4, company="ollex", unit=1)
    b = mk_trip("OSL", "STO", 3, 6, company="vilneda", unit=1)
    far = mk_trip("STO", "OSL", 20, 22, company="vilneda", unit=1)   # gap 14
    result = engine.evaluate(report_of(a, overlap, b, far), REG)
    kinds = {(f.company, f.unit): f.kind for f in result.integrity}
    assert kinds[("ollex", 1)] == "overlap"
    assert kinds[("vilneda", 1)] == "gap_out_of_window"


def test_er_marked_pair_listed_as_explicit_er():
    a = mk_trip("STO", "CPH", 0, 3, company="ollex", unit=1)
    b = mk_trip("OSL", "STO", 10, 12, company="ollex", unit=1, er=True)
    result = engine.evaluate(report_of(a, b), REG)
    (finding,) = result.integrity
    assert finding.kind == "explicit_er"


def test_city_mismatch_within_window():
    a = mk_trip("STO", "CPH", 0, 3, company="ollex", unit=1)
    b = mk_trip("BGO", "STO", 5, 7, company="ollex", unit=1)
    result = engine.evaluate(report_of(a, b), REG)
    (finding,) = result.integrity
    assert finding.kind == "city_mismatch"


def test_broken_chain_cascades():
    root = mk_trip("STO", "CPH", 0, 3, company="ollex", unit=1,
                   status=Status.CANCELED)          # was Pending, now canceled
    mid = mk_trip("CPH", "OSL", 5, 7, company="ollex", unit=1)
    leaf = mk_trip("OSL", "BGO", 9, 11, company="ollex", unit=1)
    accepted = [
        AcceptedLink(supply=root.id, demand=mid.id),
        AcceptedLink(supply=mid.id, demand=leaf.id),
    ]
    result = engine.evaluate(report_of(root, mid, leaf), REG, accepted)
    broken = {b.assignment: b.canceled_dependency for b in result.broken_chains}
    assert broken == {mid.id: root.id, leaf.id: root.id}


def test_adhoc_listed_chain_invisible():
    adhoc = mk_trip("STO", "CPH", 0, 3, company="vilneda")
    d = mk_trip("CPH", "OSL", 5, 7)
    result = engine.evaluate(report_of(adhoc, d), REG)
    assert result.adhoc_by_company == {"vilneda": (adhoc.id,)}
    assert d.id not in result.suggestions_by_trip     # no supply generated
    assert result.timelines == {}
