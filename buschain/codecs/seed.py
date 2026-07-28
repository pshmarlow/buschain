"""Registry seed and fleet sync from the 'Information' sheet.

Company names and fleet counts come from the two adjacent header columns
'Bus Companies' and 'Fleets', paired positionally row by row. That pairing is
the whole point: the previous source was a free-text 'Expected charters'
block matched to the company list by name, and it silently failed — the line
read 'Nordi Bus - 2' against a company listed as 'Nordibuzz', so two real
vehicles went unregistered and 31 rows fell out of the operable set behind a
warning nobody read. A cell beside the name cannot mismatch.

Notes are still matched by name (display-only, so a miss costs nothing).
Free-text rate/notes parsing into rules stays out of scope by the brief.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field

import openpyxl
from openpyxl.utils import get_column_letter

from .. import normalize
from ..model import Company, Registry
from .types import INFO_FLEET_COL, INFO_OPTION_COLS, INFO_SHEET

COMPANY_HEADER = "Bus Companies"
FLEET_HEADER = "Fleets"
NOTE_HEADER = "Notes"                 # note text; the company label sits in column A

# Initial closed city registry: values observed as plain canonical codes in
# the reference data. Registry is CRUD-editable in-app; whether e.g. STV and
# Stavanger should alias to one code is a user registry decision, not ours.
DEFAULT_CITIES: tuple[str, ...] = (
    "STO", "OSL", "CPH", "BGO", "HEL", "STV",
    "Trondheim", "Stavanger", "Karlshamn", "Kristiansund", "Helsingborg",
    "Billund", "Rødbyhavn", "Skagen", "Malmo", "Odense", "Hamburg", "Flam",
)


@dataclass
class SeedCompany:
    name: str
    note: str | None = None
    units: tuple[int, ...] = ()

    @property
    def fleet_size(self) -> int:
        return len(self.units)


@dataclass(frozen=True)
class SheetOptionList:
    """A pick list as the Information sheet currently states it."""
    column: str                       # its column letter on that sheet
    values: tuple[str, ...]           # sheet order; blanks and repeats dropped
    last_row: int                     # last row holding anything in that column


@dataclass
class SeedResult:
    companies: list[SeedCompany]
    cities: list[str]
    warnings: list[str] = field(default_factory=list)
    # Pick lists keyed by column title, for the columns the sheet actually
    # heads. Carried here rather than read separately so an import parses the
    # workbook once; the store consumes them, the Registry model does not.
    options: dict[str, SheetOptionList] = field(default_factory=dict)

    def fleets(self) -> dict[str, int]:
        """canonical key -> chartered vehicle count, for Repo.sync_fleets."""
        return {normalize.casefold_key(c.name): len(c.units) for c in self.companies}

    def to_registry(self) -> Registry:
        return Registry(
            companies={
                normalize.casefold_key(c.name): Company(
                    name=c.name, note=c.note, units=frozenset(c.units),
                )
                for c in self.companies
            },
            cities={normalize.casefold_key(c): c for c in self.cities},
        )


def _match_company(label: str, companies: dict[str, SeedCompany]) -> str | None:
    """Match a free-text label to a seeded company key without guessing.

    Accepted evidence, strongest first: exact key; company name is a prefix
    of the label ('Ollex - Tatjana/Olga'); label tokens are a suffix of the
    company tokens ('VIP Coach Service' -> 'UAB VIP Coach Service').
    Anything weaker is a warning for the user to resolve in the Registry tab.
    """
    key = normalize.casefold_key(label)
    if key in companies:
        return key
    prefix_hits = [k for k in companies if key.startswith(k + " ")]
    if len(prefix_hits) == 1:
        return prefix_hits[0]
    label_tokens = key.split()
    suffix_hits = [
        k for k in companies
        if k.split()[-len(label_tokens):] == label_tokens
    ]
    if len(suffix_hits) == 1:
        return suffix_hits[0]
    return None


def read_info_grid(blob: bytes) -> list[list[object]]:
    wb = openpyxl.load_workbook(io.BytesIO(blob), read_only=True, data_only=True)
    if INFO_SHEET not in wb.sheetnames:
        raise ValueError(f"Workbook has no sheet named {INFO_SHEET!r}")
    grid: list[list[object]] = [
        list(row) for row in wb[INFO_SHEET].iter_rows(values_only=True)
    ]
    wb.close()
    return grid


def _header_index(grid: list[list[object]], title: str) -> int | None:
    """Locate a column by its row-1 header rather than a fixed letter, so
    inserting a column ahead of it does not silently repoint the reader."""
    if not grid:
        return None
    key = normalize.casefold_key(title)
    for i, cell in enumerate(grid[0]):
        if cell is not None and normalize.casefold_key(str(cell)) == key:
            return i
    return None


def _cell(row: list[object], index: int | None) -> object:
    return row[index] if index is not None and index < len(row) else None


def _fleet_count(value: object) -> int | None:
    """A Fleets cell -> vehicle count. None means 'not a usable number', which
    the caller reports; an empty cell is 0 (no chartered vehicles)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return 0
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value) if float(value).is_integer() and value >= 0 else None
    text = normalize.trim(str(value))
    # isdecimal, not isdigit: isdigit accepts superscripts int() refuses.
    return int(text) if text.isdecimal() else None


def company_rows(grid: list[list[object]]) -> list[tuple[int, str]]:
    """(1-based sheet row, company display name) for every named company.

    Shared with the exporter, which needs the same row->company mapping to
    write a fleet number back into the row it came from.
    """
    col = _header_index(grid, COMPANY_HEADER)
    if col is None:
        return []
    out: list[tuple[int, str]] = []
    for i, row in enumerate(grid[1:], start=2):
        raw = _cell(row, col)
        if raw is None:
            continue
        name = normalize.collapse_ws(normalize.trim(str(raw)))
        if name:
            out.append((i, name))
    return out


def fleet_column(grid: list[list[object]]) -> str:
    """The letter fleet counts should be WRITTEN to — discovered by header,
    like the reader locates it, so an inserted column cannot make the exporter
    splice numbers over whatever drifted under the fixed letter. The default
    only applies when the sheet has no Fleets column yet (first export
    populates it)."""
    col = _header_index(grid, FLEET_HEADER)
    return INFO_FLEET_COL if col is None else get_column_letter(col + 1)


def sheet_fleets(grid: list[list[object]]) -> list[tuple[int, str, int | None]]:
    """(sheet row, company name, fleet count as the sheet currently states it).

    Count is None when the cell is unreadable or the column is absent — the
    exporter treats that as 'differs from the registry' and writes the real
    number, which is how a missing Fleets column gets populated on first
    export rather than staying blank forever.
    """
    fleet_col = _header_index(grid, FLEET_HEADER)
    return [
        (row, name,
         None if fleet_col is None else _fleet_count(_cell(grid[row - 1], fleet_col)))
        for row, name in company_rows(grid)
    ]


def sheet_option_lists(grid: list[list[object]]) -> dict[str, SheetOptionList]:
    """Pick lists off their own columns on the Information sheet.

    A column the sheet does not head is absent from the result rather than
    present and empty. The distinction is the whole safety of the sync: the
    sheet having nothing to say about a list must not clear it, while a headed
    column the user emptied in Excel must.

    Located by header like 'Bus Companies' is, not by the fixed letter, so a
    column inserted to the left does not silently repoint the reader at the
    wrong values — and, worse on this path, get them written back there.
    """
    out: dict[str, SheetOptionList] = {}
    for title in INFO_OPTION_COLS:
        col = _header_index(grid, title)
        if col is None:
            continue
        values: list[str] = []
        seen: set[str] = set()
        last_row = 1                              # the header row itself
        for i, row in enumerate(grid[1:], start=2):
            raw = _cell(row, col)
            if raw is None:
                continue
            last_row = i                          # counts even if it trims away
            text = normalize.collapse_ws(normalize.trim(str(raw)))
            if text and text not in seen:
                seen.add(text)
                values.append(text)
        out[title] = SheetOptionList(
            column=get_column_letter(col + 1), values=tuple(values),
            last_row=last_row,
        )
    return out


def read_seed(blob: bytes) -> SeedResult:
    grid = read_info_grid(blob)
    result = SeedResult(companies=[], cities=list(DEFAULT_CITIES))
    companies: dict[str, SeedCompany] = {}

    fleet_col = _header_index(grid, FLEET_HEADER)
    if fleet_col is None:
        result.warnings.append(
            f"The {INFO_SHEET} sheet has no {FLEET_HEADER!r} column, so no chartered "
            "vehicles were registered and nothing can be chained. Add the column "
            "beside 'Bus Companies' and re-import, or add units in the Registry tab."
        )

    # 'Bus Companies' + 'Fleets', paired positionally: same row, no matching.
    for sheet_row, name in company_rows(grid):
        count = _fleet_count(_cell(grid[sheet_row - 1], fleet_col))
        if count is None:
            result.warnings.append(
                f"{name}: {FLEET_HEADER} cell on row {sheet_row} is not a whole "
                "number, so no units were registered for it"
            )
            count = 0
        companies[normalize.casefold_key(name)] = SeedCompany(
            # Chartered vehicles are the numbered, chainable units.
            name=name, units=tuple(range(1, count + 1)),
        )

    # Rate-table rows (label in column A + a 'Notes' column): attach the note
    # as a display note when the label matches a known company. Display-only,
    # so name matching is acceptable here in a way it was not for fleet counts.
    note_col = _header_index(grid, NOTE_HEADER)
    for row in grid[1:]:
        label = normalize.trim(str(row[0])) if row and row[0] else ""
        if not label:
            continue
        note_raw = _cell(row, note_col)
        note = normalize.trim(str(note_raw)) if note_raw else ""
        if not note:
            continue
        key = _match_company(label, companies)
        if key is not None and companies[key].note is None:
            companies[key].note = note

    result.companies = list(companies.values())
    result.options = sheet_option_lists(grid)
    return result
