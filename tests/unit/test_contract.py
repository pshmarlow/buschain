from datetime import date, timedelta

from buschain import contract
from buschain.codecs.types import (
    ARRIVAL, BUS_PREFERENCE, COLUMN_LETTERS, COMPANY_NAME, DEPARTURE, END_DATE,
    ER, PAX, PROJECT, RawTripRow, START_DATE, STATUS,
)
from buschain.model import NonOperableRow, Registry, Status, Trip, TripId, Company


def make_registry() -> Registry:
    return Registry(
        companies={
            "ollex": Company("Ollex", None, frozenset({1, 2})),
            "vilneda": Company("Vilneda", None, frozenset()),
        },
        cities={"sto": "STO", "cph": "CPH", "osl": "OSL"},
    )


# Semantic names, not column letters: these tests then say nothing about
# where a field sits in the sheet, and survive the next layout change.
_FIELDS = {
    "status": STATUS, "project": PROJECT, "pref": BUS_PREFERENCE,
    "start": START_DATE, "end": END_DATE, "dep": DEPARTURE, "arr": ARRIVAL,
    "pax": PAX, "er": ER, "company": COMPANY_NAME,
}


def row(**overrides) -> RawTripRow:
    values = {
        STATUS: "Confirmed", PROJECT: "12345", BUS_PREFERENCE: None,
        START_DATE: date(2026, 4, 1), END_DATE: date(2026, 4, 3),
        DEPARTURE: "STO", ARRIVAL: "CPH", PAX: 49, ER: "N/A", COMPANY_NAME: None,
    }
    values.update({_FIELDS[k]: v for k, v in overrides.items()})
    return RawTripRow(sheet_row=2, values=values)


def check(r: RawTripRow):
    return contract.validate_row(r, TripId("sheet", 2), make_registry())


def test_valid_demand_row():
    t = check(row())
    assert isinstance(t, Trip)
    assert t.status is Status.CONFIRMED
    assert t.assignment is None and t.project == 12345


def test_whole_number_stored_as_float_displays_without_decimal():
    """A non-Excel writer stores 25735 as '<v>25735.0</v>' and openpyxl reads
    that as a float; the Data tab showed '25735.0' for what Excel shows as 25735."""
    t = check(row(project=25735.0, pax=49.0))
    assert isinstance(t, Trip)
    assert t.project == 25735 and t.pax == 49
    assert t.display["Project"] == "25735" and t.display["Pax"] == "49"


def test_fractional_float_keeps_its_decimals_in_free_text():
    quoted = COLUMN_LETTERS["Quoted rate"]
    t = check(RawTripRow(sheet_row=2, values={**row().values, quoted: 12.5}))
    assert isinstance(t, Trip)
    assert t.display["Quoted rate"] == "12.5"


def test_fractional_project_is_not_a_whole_number():
    r = check(row(project=25735.5))
    assert isinstance(r, NonOperableRow)
    assert "Project '25735.5' is not a whole number" in r.reasons


def test_city_normalization_applies():
    t = check(row(dep="CPH trf", arr="osl (TRF)"))
    assert isinstance(t, Trip)
    assert (t.dep_city, t.arr_city) == ("CPH", "OSL")


def test_free_text_arrival_is_non_operable():
    r = check(row(arr="E.R to OSL add e.r fee to NHBT25039"))
    assert isinstance(r, NonOperableRow)
    assert any("Arrival" in reason for reason in r.reasons)


def test_reasons_accumulate():
    r = check(row(status="???", arr="nowhere", start="junk"))
    assert isinstance(r, NonOperableRow)
    assert len(r.reasons) == 3


def test_bare_preference_is_a_must_use_constraint():
    t = check(row(pref="Ollex"))
    assert isinstance(t, Trip)
    assert t.yn == "Y" and t.constraint_company == "ollex"


def test_no_prefix_is_a_must_avoid_constraint():
    t = check(row(pref="NO Vilneda"))
    assert isinstance(t, Trip)
    assert t.yn == "N" and t.constraint_company == "vilneda"


def test_no_prefix_is_case_insensitive():
    t = check(row(pref="no Ollex"))
    assert isinstance(t, Trip)
    assert t.yn == "N" and t.constraint_company == "ollex"


def test_unknown_preference_is_non_operable():
    r = check(row(pref="Some Other Firm"))
    assert isinstance(r, NonOperableRow)
    assert any("Bus Preference" in reason for reason in r.reasons)


def test_preference_must_be_bare_company_not_a_unit():
    # A company-level constraint: 'Ollex 1' names a vehicle, not a company.
    r = check(row(pref="Ollex 1"))
    assert isinstance(r, NonOperableRow)


def test_empty_preference_leaves_trip_unconstrained():
    t = check(row(pref=""))
    assert isinstance(t, Trip)
    assert t.yn is None and t.constraint_company is None


def test_numbered_unit_assignment_with_whitespace():
    t = check(row(company="Ollex  2"))
    assert isinstance(t, Trip)
    assert t.assignment is not None
    assert (t.assignment.company, t.assignment.unit) == ("ollex", 2)


def test_unknown_unit_is_non_operable():
    r = check(row(company="Ollex 9"))
    assert isinstance(r, NonOperableRow)


def test_adhoc_assignment():
    t = check(row(company="Vilneda"))
    assert isinstance(t, Trip)
    assert t.assignment is not None and t.assignment.unit is None


def test_start_after_end_rejected():
    r = check(row(start=date(2026, 5, 1), end=date(2026, 4, 1)))
    assert isinstance(r, NonOperableRow)


def test_implausible_span_rejected_with_the_span_in_the_reason():
    """A typo'd year: parses fine, ordered fine, but no trip here runs a year.

    The reason has to carry the measured span and the limit — the whole point
    is that the row looks ordinary until someone is told what is wrong with it.
    """
    r = check(row(start=date(2025, 4, 7), end=date(2026, 4, 16)))
    assert isinstance(r, NonOperableRow)
    assert any("374 days" in x and "60-day limit" in x for x in r.reasons), r.reasons


def test_span_boundary_is_inclusive():
    """Exactly at the limit is sound; one day past it is not. Pinned on both
    sides so a later `>=`/`>` slip cannot pass unnoticed."""
    start = date(2026, 4, 1)
    ok = check(row(start=start, end=start + timedelta(days=contract.MAX_TRIP_SPAN_DAYS)))
    assert isinstance(ok, Trip)
    over = check(row(start=start,
                     end=start + timedelta(days=contract.MAX_TRIP_SPAN_DAYS + 1)))
    assert isinstance(over, NonOperableRow)


def test_inverted_dates_do_not_also_report_a_span():
    """One reason per thing to fix: an inverted pair has a negative span, and a
    second complaint about its length would describe a consequence, not a cause."""
    r = check(row(start=date(2026, 5, 1), end=date(2024, 4, 1)))
    assert isinstance(r, NonOperableRow)
    assert not any("limit" in x for x in r.reasons), r.reasons


def test_iso_string_dates_accepted_for_csv():
    t = check(row(start="2026-04-01", end="2026-04-03"))
    assert isinstance(t, Trip)
    assert t.start == date(2026, 4, 1)


def test_er_marking_label_only():
    assert check(row(er="YES")).er_marked        # type: ignore[union-attr]
    assert not check(row(er="N/A")).er_marked    # type: ignore[union-attr]
    assert not check(row(er="")).er_marked       # type: ignore[union-attr]
