"""xlsx importer: workbook bytes -> ImportedDataset.

Reads cached values only (data_only=True) — the system consumes whatever Excel
last computed and never recomputes a formula. The sheet's only formula-backed
columns were the old 'Y-N'/'B Comp' pair, which had no cached values at all
(the file is written by a tool that does not evaluate formulas, so every one
read as empty); they are gone from the layout and the constraint is now read
straight from 'Bus Preference'.

The original bytes ride along untouched for the byte-preserving export path.
"""
from __future__ import annotations

import io
import zipfile

import openpyxl
from openpyxl.utils.exceptions import InvalidFileException

from .. import normalize
from .types import ALL_COLUMNS, COLUMN_ORDER, ImportedDataset, RawTripRow, SHEET_NAME

HEADER_ROW = 1
# A row "has content" iff any contract-layout cell is non-empty. The legacy
# date grid that follows is passthrough and deliberately ignored here: a row
# with only grid residue is not a trip. This bound must track the layout —
# the grid starts immediately after the last contract column.
_CONTENT_COLS = len(ALL_COLUMNS)
_COL_INDEX = {letter: i for i, letter in enumerate(ALL_COLUMNS)}


def read(blob: bytes) -> ImportedDataset:
    try:
        wb = openpyxl.load_workbook(io.BytesIO(blob), read_only=True, data_only=True)
    # Truncated download, renamed .xls, password-protected file: none of these
    # raise ValueError on their own, so without this they surface as a 500
    # instead of the import error they are.
    except (zipfile.BadZipFile, KeyError, InvalidFileException) as e:
        raise ValueError(f"Not a readable .xlsx workbook ({e})") from e
    if SHEET_NAME not in wb.sheetnames:
        raise ValueError(f"Workbook has no sheet named {SHEET_NAME!r}")
    ws = wb[SHEET_NAME]

    # The values below are read positionally, so row 1 must actually be the
    # contract layout. Without this check a workbook with one inserted column
    # imports every value one slot off — dates still look like dates, so it
    # can pass validation — and the export then splices assignments into
    # whatever column drifted under 'Company name'.
    header = next(ws.iter_rows(min_row=HEADER_ROW, max_row=HEADER_ROW,
                               max_col=_CONTENT_COLS, values_only=True), ())
    got = ["" if c is None else normalize.casefold_key(str(c)) for c in header]
    expected = [normalize.casefold_key(t) for t in COLUMN_ORDER]
    if got != expected:
        raise ValueError(
            f"Sheet {SHEET_NAME!r} row 1 is not the contract layout; expected "
            f"{list(COLUMN_ORDER)}, got {[str(c or '') for c in header]}"
        )

    rows: list[RawTripRow] = []
    for row_num, cells in enumerate(
        ws.iter_rows(min_row=HEADER_ROW + 1, max_col=_CONTENT_COLS, values_only=True),
        start=HEADER_ROW + 1,
    ):
        if not any(c is not None and str(c) != "" for c in cells):
            continue
        values = {letter: cells[idx] for letter, idx in _COL_INDEX.items()}
        rows.append(RawTripRow(sheet_row=row_num, values=values))
    wb.close()
    return ImportedDataset(source_kind="xlsx", rows=tuple(rows), blob=blob)
