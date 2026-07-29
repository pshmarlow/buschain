"""Neutral shapes at the codec boundary.

Codecs (xlsx, csv, manual entry) produce `ImportedDataset` and consume
`ExportPlan`. They never construct model.Trip — contract validation is core
logic with one home (buschain.contract), not codec logic. Swapping or adding
a codec touches nothing outside buschain.codecs.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping

# The sheet every codec reads and writes. One list because the name was
# previously spelled in four places (reader, writer, both csv paths) — the same
# drift hazard the column letters below already solved.
#
# Several names because the workbook is re-cut every season and the tab is
# renamed with it ('Capitals 2026' became 'CAPITALS'). The reader takes
# whichever of these the workbook actually has and the writer patches that same
# one, so a season rename is a one-line edit here rather than a failed import.
# The layout check below is what actually guarantees we read the right sheet;
# the name only has to find it.
SHEET_NAMES: tuple[str, ...] = ("Capitals 2026", "CAPITALS")
# The name a codec CREATES a sheet with (csv -> xlsx), where there is no
# existing workbook to take it from.
SHEET_NAME = SHEET_NAMES[0]

# The interchange layout of the trip sheet, header row 1, in order.
# Everything else here derives from this list, so a layout change (the sheet
# lost its formula-backed 'Y-N'/'B Comp' pair, shifting every later column two
# letters left) is a one-line edit. Letters were previously repeated in five
# places and silently drifted apart on that edit — a stale "P" (once 'Company
# name') would have spliced accepted assignments into the passthrough grid on
# export.
COLUMN_ORDER: tuple[str, ...] = (
    "Status", "Project", "Bus Preference", "P Notes", "Start date", "End date",
    "Departure", "Arrival", "Nationality", "OP", "Quoted rate", "Pax", "ER",
    "Company name", "C Notes",
)


def _letter(index: int) -> str:
    # Base-26 like Excel's own numbering (…, Z, AA, AB), so the "layout change
    # is a one-line edit" promise above does not quietly expire at 27 columns.
    index += 1
    letters = ""
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters


ALL_COLUMNS: tuple[str, ...] = tuple(_letter(i) for i in range(len(COLUMN_ORDER)))
COLUMN_TITLES: Mapping[str, str] = dict(zip(ALL_COLUMNS, COLUMN_ORDER))
# Title -> letter. Stored edits key on the title, not the letter: a title
# survives a column being inserted or deleted, a letter does not.
COLUMN_LETTERS: Mapping[str, str] = {t: c for c, t in COLUMN_TITLES.items()}
_COL = COLUMN_LETTERS

# Named handles so logic never spells a letter. Reference these, not literals.
STATUS = _COL["Status"]
PROJECT = _COL["Project"]
BUS_PREFERENCE = _COL["Bus Preference"]
START_DATE = _COL["Start date"]
END_DATE = _COL["End date"]
DEPARTURE = _COL["Departure"]
ARRIVAL = _COL["Arrival"]
PAX = _COL["Pax"]
ER = _COL["ER"]
COMPANY_NAME = _COL["Company name"]

# Every column above is carried and displayed; the named handles are the ones
# the contract actually validates. The rest ('P Notes', 'Nationality', 'OP',
# 'Quoted rate', 'C Notes') is free text — '650EUR', '（580*6=3480EUR）' — with
# no defensible rule to check, so validating it could only manufacture
# failures.
#
# Any contract column may be written back to an existing row: assignments and
# status changes are the common case, but repo.set_cell accepts every title
# and the writer types dates/numbers natively via _cell_kind. (An earlier
# WRITABLE_COLUMNS constant claimed only Status and Company name were written;
# it was enforced nowhere and the claim went stale — dropped rather than kept
# as documentation that lies.)

# Passthrough columns that get a registry-backed pick list, so their values can
# be normalized ('South Korea' / 'Korea' -> 'South Korean') instead of drifting
# as free text. The list only populates the dropdown: these columns stay
# passthrough and are never validated, or pruning the list would turn tidying
# a menu into knocking rows out of the operable set.
OPTION_COLUMNS: tuple[str, ...] = ("Nationality", "OP")
assert all(c in COLUMN_LETTERS for c in OPTION_COLUMNS)


@dataclass(frozen=True)
class RawTripRow:
    sheet_row: int | None               # 1-based source row; None for csv/manual origins
    values: Mapping[str, object]        # all contract columns, keyed by letter, raw typed values


@dataclass(frozen=True)
class ImportedDataset:
    source_kind: Literal["xlsx", "csv"]
    rows: tuple[RawTripRow, ...]
    blob: bytes | None                  # original xlsx bytes; None for csv (no byte round-trip)
    # csv only: every row's full value list (all columns incl. the legacy date
    # grid) so a csv-sourced dataset can re-emit passthrough values on export.
    csv_rows: tuple[tuple[str, ...], ...] | None = None
    csv_header: tuple[str, ...] | None = None


# The 'Information' sheet, header row 1. Two things are written there: the
# fleet count, back into the row the company was read from, and the pick lists
# below, each owning a whole column.
INFO_SHEET = "Information"
INFO_FLEET_COL = "N"

# Pick-list column title -> the column it occupies on the Information sheet.
# The sheet is the home of these lists (import replaces the stored list from
# here, export writes it back), so the letter is a fixed default rather than
# something discovered: a fleet count has its company name beside it to anchor
# on, a list has nothing. The reader still prefers the header's actual position
# when there is one, so inserting a column ahead of these does not repoint them.
INFO_OPTION_COLS: Mapping[str, str] = {"Nationality": "O", "OP": "P"}
assert set(INFO_OPTION_COLS) == set(OPTION_COLUMNS)

# The city registry lives on the same sheet, in the same shape as the pick
# lists above and located the same way (by header, falling back to this letter
# when the column has to be created). Deliberately NOT in INFO_OPTION_COLS:
# those are pick lists for passthrough columns of the TRIP sheet, which the
# contract never validates against, whereas Departure/Arrival are resolved
# against these codes — so editing this column changes which rows are operable,
# and Repo.sync_cities has to be more careful than sync_options is.
CITY_HEADER = "Cities"
INFO_CITY_COL = "Q"
assert CITY_HEADER not in INFO_OPTION_COLS


@dataclass(frozen=True)
class CellWrite:
    sheet_row: int
    column: str                         # contract-layout letter (ALL_COLUMNS)
    value: str


@dataclass(frozen=True)
class InfoWrite:
    """A chartered-vehicle count to write into Information!N.

    Its own type rather than a sheet-qualified CellWrite: there is exactly one
    such column, it is always numeric, and CellWrite's column letters are
    meaningful only against the Capitals layout (where N is 'Company name').
    """
    sheet_row: int
    value: int
    # Where the sheet actually keeps its Fleets column, discovered by header
    # at export time (seed.fleet_column) — the reader locates it that way, and
    # a writer pinned to the fixed letter would splice numbers over whatever
    # drifted underneath it. The default applies only when the sheet has no
    # Fleets column yet.
    column: str = INFO_FLEET_COL


@dataclass(frozen=True)
class InfoCellWrite:
    """A text cell anywhere on the Information sheet, for the pick lists.

    Carries its own column letter, unlike InfoWrite, because a list occupies a
    whole column rather than one cell per company row. Still not a CellWrite:
    these letters mean the Information layout, where 'O' is 'Nationality' and
    not the Capitals sheet's 'ER'.
    """
    sheet_row: int
    column: str
    value: str                          # '' empties the cell (a pruned entry)


@dataclass(frozen=True)
class NewRow:
    """A manually entered trip to append on export (operated values only —
    passthrough columns of new rows are legitimately empty)."""
    values: Mapping[str, object]


@dataclass(frozen=True)
class ExportPlan:
    blob: bytes | None                  # original workbook bytes (None for csv-sourced datasets)
    cell_writes: tuple[CellWrite, ...]
    new_rows: tuple[NewRow, ...]
    # Only rows whose fleet count actually differs from the registry, so an
    # untouched registry still exports byte-for-byte (AT1).
    info_writes: tuple[InfoWrite, ...] = ()
    # Pick-list columns, likewise only when the stored list and the sheet's
    # column disagree on their contents.
    info_cell_writes: tuple[InfoCellWrite, ...] = ()
    # Source rows deleted in-system, to be dropped from the exported sheet.
    # Row numbers below them are NOT renumbered: `r` attributes are absolute
    # and referenced by every cell in the row, so renumbering would mean
    # rewriting the whole sheet — exactly what byte-preservation forbids.
    deleted_rows: tuple[int, ...] = ()
