"""CSV codec: full contract layout, values only, both directions.

CSV cannot carry formatting/formulas, so "byte-preservation" applies to xlsx
only (user decision). Dates are ISO YYYY-MM-DD. A csv-sourced dataset keeps
every source column verbatim so csv -> csv round-trips values exactly.
"""
from __future__ import annotations

import csv
import io
from datetime import date, datetime

import openpyxl

from .. import normalize
from .types import ALL_COLUMNS, COLUMN_TITLES, ImportedDataset, RawTripRow, SHEET_NAME


class CsvError(ValueError):
    pass


def read(data: bytes | str) -> ImportedDataset:
    text = data.decode("utf-8-sig") if isinstance(data, bytes) else data
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise CsvError("CSV file is empty") from None

    # The header must be the contract layout (same titles, same order) —
    # anything else is a different data contract, not ours to reinterpret.
    # Compared casefolded/whitespace-collapsed: 'Start Date' from an Excel
    # round-trip is the same contract, only the spelling of it, and rejecting
    # the whole file over casing protects no data.
    expected = [COLUMN_TITLES[c] for c in ALL_COLUMNS]
    got = [h.strip() for h in header[:len(expected)]]
    if ([normalize.casefold_key(h) for h in got]
            != [normalize.casefold_key(e) for e in expected]):
        raise CsvError(
            f"CSV header does not match the contract layout; expected it to "
            f"start with {expected}, got {got}"
        )

    rows: list[RawTripRow] = []
    full_rows: list[tuple[str, ...]] = []
    # The header is record 1; data rows take their RECORD number as their
    # stable row identity — the csv analog of the xlsx sheet row. (Record,
    # not file line: a quoted multi-line cell spans several physical lines
    # but is one record, and the identity only has to be stable and unique.)
    for line_num, row in enumerate(reader, start=2):
        if not any(cell.strip() for cell in row[:len(ALL_COLUMNS)]):
            continue
        values = {
            c: (row[i].strip() if i < len(row) else "")
            for i, c in enumerate(ALL_COLUMNS)
        }
        rows.append(RawTripRow(sheet_row=line_num, values=values))
        full_rows.append(tuple(row))
    return ImportedDataset(
        source_kind="csv", rows=tuple(rows), blob=None,
        csv_rows=tuple(full_rows), csv_header=tuple(header),
    )


def _cell_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def render_workbook_csv(xlsx_bytes: bytes, sheet_name: str = SHEET_NAME) -> str:
    """CSV export for xlsx-sourced datasets: rendered FROM the (already
    patched) workbook so the csv always equals what the xlsx export says —
    one rendering path, no second source of truth."""
    wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), read_only=True, data_only=True)
    ws = wb[sheet_name]
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    for row in ws.iter_rows(values_only=True):
        writer.writerow([_cell_text(v) for v in row])
    wb.close()
    return out.getvalue()


def write_rows_csv(header: tuple[str, ...], rows: list[list[object]]) -> str:
    """CSV export for csv-sourced datasets: stored source rows + overlays."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow([_cell_text(v) for v in row])
    return out.getvalue()


def build_fresh_workbook(header: tuple[str, ...], rows: list[list[object]]) -> bytes:
    """xlsx export for csv-sourced datasets: a values-only workbook in the
    contract layout. Nothing to preserve here (openpyxl write is fine) —
    the UI labels this 'no byte-preserving export available'."""
    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = SHEET_NAME
    ws.append(list(header))
    for row in rows:
        ws.append([_cell_text(v) for v in row])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
