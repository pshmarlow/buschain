"""Contract validation: RawTripRow -> Trip | NonOperableRow(reasons).

One home for the data contract regardless of source (xlsx, csv, manual
entry). Rows that fail collect *all* their reasons — a row is never silently
dropped (acceptance test 2) and never repaired: unresolvable values are
reported verbatim.
"""
from __future__ import annotations

from datetime import date, datetime

from . import normalize
from .codecs.types import (
    ALL_COLUMNS, ARRIVAL, BUS_PREFERENCE, COLUMN_TITLES, COMPANY_NAME,
    DEPARTURE, END_DATE, ER, PAX, PROJECT, RawTripRow, START_DATE, STATUS,
    ImportedDataset,
)
from .model import (
    Assignment, ContractReport, NonOperableRow, Registry, Status, Trip, TripId,
)

# ER values meaning "no ER" — anything else non-empty marks the trip as
# an explicit ER for integrity labeling only (never matching logic).
_NOT_ER = {"n/a", "na", "no", "n", "-"}

# The one judgment call in this module: every other reason here is objective
# ("not a date", "not a registry city"), while this one asks whether a parsed
# pair of dates is *plausible*. It earns its place by measurement. On the
# reference workbook's 1670 rows the 1594 sound ones span 0-11 days and 76 with
# a typo'd start year span 366-376, with nothing whatsoever in between — so any
# threshold inside that 355-day gap separates the two populations exactly. 60
# sits far above the longest real trip and far below a full year, which also
# catches a typo landing short of one.
#
# Refusing them is what makes them visible: they pass every other check, so
# before this they were fully operable and invisible to every report while
# sorting to the top of any date order. They also poisoned the reports they did
# reach — a 370-day trip on a numbered unit occupies that bus for a year, which
# was the sole source of all 20 fleet over-commitment warnings and all 4 overlap
# findings on the reference data. This is refusal, never repair: the date is
# left exactly as the sheet has it, the row is listed with the reason below, and
# editing the cell re-validates it straight back into the operable set.
MAX_TRIP_SPAN_DAYS = 60


def _as_str(value: object) -> str:
    if value is None:
        return ""
    # xlsx date cells arrive as datetime; render the date so display and any
    # failure reason quote what the sheet shows, not '2026-04-01 00:00:00'.
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return normalize.trim(str(value))


def _parse_date(value: object) -> date | None:
    """Accept real date/datetime cells (xlsx) or ISO strings (csv/manual).

    Anything else is a contract failure — date repair is forbidden.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _as_str(value)
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _parse_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    text = _as_str(value)
    # Text parses exactly like the typed branches above: the overlay path
    # re-validates everything as text, so "-5" and "3.0" (the very rendering
    # _as_str gives a float cell) must not change meaning on an edit that
    # changed nothing. try/except rather than a digit-class check — isdigit
    # accepted superscripts int() refuses, which was a crash on load.
    try:
        return int(text)
    except ValueError:
        try:
            f = float(text)
        except ValueError:
            return None
        return int(f) if f.is_integer() else None


def validate_row(row: RawTripRow, trip_id: TripId, registry: Registry) -> Trip | NonOperableRow:
    v = {c: row.values.get(c) for c in ALL_COLUMNS}
    # Text renderings converted once up front: the checks below quote them in
    # reasons and reuse them as inputs, and this runs per row on every
    # working-set load. v keeps the raw typed values for the date/int parsers.
    s = {c: _as_str(v[c]) for c in ALL_COLUMNS}
    reasons: list[str] = []

    status = Status.parse(s[STATUS])
    if status is None:
        reasons.append(f"Status {s[STATUS]!r} is not Confirmed/Pending/Canceled")

    project_raw = s[PROJECT]
    project = _parse_int(v[PROJECT]) if project_raw else None
    if project_raw and project is None:
        reasons.append(f"Project {project_raw!r} is not numeric")

    # Bus Preference carries the company constraint as free text: a bare name
    # ('Vilneda') is a preference — use this company — and a 'NO ' prefix
    # ('NO Nordic Alliance') is a restriction. This is the rule the sheet's
    # retired Y-N/B Comp formulas encoded; reading C directly keeps one source
    # of truth and removes the dependency on Excel's cached formula values.
    pref_raw = s[BUS_PREFERENCE]
    yn: str | None = None
    constraint_company: str | None = None
    if pref_raw:
        negated = pref_raw[:3].casefold() == "no "
        name = pref_raw[3:].strip() if negated else pref_raw
        # A company-level constraint, so a numbered unit does not satisfy it.
        resolved = registry.resolve_company(name) if name else None
        if resolved is None or resolved[1] is not None:
            reasons.append(
                f"Bus Preference {pref_raw!r} does not resolve to a registry company"
            )
        else:
            yn = "N" if negated else "Y"
            constraint_company = resolved[0]

    start = _parse_date(v[START_DATE])
    end = _parse_date(v[END_DATE])
    if start is None:
        reasons.append(f"Start date {s[START_DATE]!r} is not a valid date")
    if end is None:
        reasons.append(f"End date {s[END_DATE]!r} is not a valid date")
    if start is not None and end is not None and start > end:
        reasons.append(f"Start date {start.isoformat()} is after end date {end.isoformat()}")
    # Only when the pair is otherwise sound: an inverted or unparseable date
    # already has its reason, and a second one about the span would describe a
    # consequence rather than a thing to fix.
    elif start is not None and end is not None and (end - start).days > MAX_TRIP_SPAN_DAYS:
        reasons.append(
            f"Start date {start.isoformat()} to end date {end.isoformat()} spans "
            f"{(end - start).days} days, over the {MAX_TRIP_SPAN_DAYS}-day limit "
            f"— check for a typo'd year"
        )

    dep = registry.resolve_city(s[DEPARTURE]) if s[DEPARTURE] else None
    if dep is None:
        reasons.append(f"Departure {s[DEPARTURE]!r} is not a registry city code")
    arr = registry.resolve_city(s[ARRIVAL]) if s[ARRIVAL] else None
    if arr is None:
        reasons.append(f"Arrival {s[ARRIVAL]!r} is not a registry city code")

    # Pax never fails the row: the brief gives it no format, and blank means
    # unstated. Rule 10 reads the parsed number downstream
    # (graph.numbered_unit_ok) — the contract only parses it.
    pax = _parse_int(v[PAX])

    er_marked = normalize.casefold_key(s[ER]) not in _NOT_ER and bool(s[ER])

    assignment: Assignment | None = None
    company_raw = s[COMPANY_NAME]
    if company_raw:
        resolved = registry.resolve_company(company_raw)
        if resolved is None:
            reasons.append(f"Company name {company_raw!r} does not resolve to a registry company or unit")
        else:
            assignment = Assignment(company=resolved[0], unit=resolved[1])

    display = {COLUMN_TITLES[c]: s[c] for c in ALL_COLUMNS if s[c]}
    if reasons:
        return NonOperableRow(id=trip_id, reasons=tuple(reasons), display=display)

    assert status is not None and start is not None and end is not None
    assert dep is not None and arr is not None
    return Trip(
        id=trip_id, status=status, project=project, yn=yn,  # type: ignore[arg-type]
        constraint_company=constraint_company, start=start, end=end,
        dep_city=dep, arr_city=arr, pax=pax, assignment=assignment,
        er_marked=er_marked, display=display,
    )


def validate(dataset: ImportedDataset, registry: Registry) -> ContractReport:
    """Whole-dataset convenience used by the acceptance tests.

    Production loads validate row-by-row (repo.load_state), which is where
    manual rows get their store-assigned ids per model.TripId's contract —
    the positional TripId("manual", i) fallback below exists only for
    codec-less test datasets and must not become a production identity.
    """
    trips: list[Trip] = []
    non_operable: list[NonOperableRow] = []
    for i, row in enumerate(dataset.rows):
        trip_id = (TripId("sheet", row.sheet_row) if row.sheet_row is not None
                   else TripId("manual", i))
        result = validate_row(row, trip_id, registry)
        (trips if isinstance(result, Trip) else non_operable).append(result)  # type: ignore[arg-type]
    return ContractReport(trips=tuple(trips), non_operable=tuple(non_operable))
