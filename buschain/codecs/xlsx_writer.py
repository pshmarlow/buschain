"""Byte-preserving xlsx export via surgical zip/XML patching.

Why not openpyxl on the write path: saving through openpyxl regenerates the
whole workbook — it re-serializes 9 MB of sheet XML, converts the file's
inline strings to a shared-string table, and drops the VML comment part. Any
of those breaks acceptance test 1 (round-trip preservation).

Instead: every zip entry is copied byte-for-byte except the target sheet's
XML, which is patched by splicing only the cells the system wrote (Status and
Company name) and filling the first free rows below the data with manually
entered trips. With zero changes, the original bytes are returned untouched.
"""
from __future__ import annotations

import io
import re
import zipfile
from datetime import date
from typing import Literal
from xml.sax.saxutils import escape

from .types import (
    ALL_COLUMNS, CellWrite, END_DATE, ExportPlan, INFO_SHEET, InfoCellWrite,
    InfoWrite, NewRow, PAX, PROJECT, START_DATE,
)
# The reader owns where the header is; the writer must agree with it about
# which rows are data, so it takes the number rather than restating it.
from .xlsx_reader import HEADER_ROW

_CONTRACT_COLS = frozenset(ALL_COLUMNS)

_EPOCH = date(1899, 12, 30)          # Excel's day-serial epoch (1900 system)
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_INT_TEXT = re.compile(r"[+-]?\d+")
# Characters XML 1.0 cannot represent at all (not even escaped). escape()
# passes them through, the export succeeds, and Excel then refuses the file —
# silent corruption, the one outcome this module exists to prevent. Replaced
# with a space rather than dropped: the usual source is a paste out of
# Word/PDF, where \x0b is a line break and deleting it would glue words.
_XML_UNSAFE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")
# Columns whose new-row values must be written as native dates / numbers
# rather than inline strings, so Excel treats them like the rows above.
# These letters mean the Capitals layout ONLY — see _cell_kind.
_DATE_COLS = {START_DATE, END_DATE}
_NUM_COLS = {PROJECT, PAX}

Kind = Literal["str", "num", "date"]


class XlsxPatchError(RuntimeError):
    """The workbook's XML did not look like we verified it to look.

    Raised instead of guessing: a wrong splice could silently corrupt
    passthrough data, which is worse than a failed export.
    """


def export(plan: ExportPlan) -> bytes:
    if plan.blob is None:
        raise XlsxPatchError("No original workbook bytes (csv-sourced dataset)")
    if not (plan.cell_writes or plan.new_rows or plan.info_writes
            or plan.info_cell_writes or plan.deleted_rows):
        return plan.blob                      # AT1: byte-for-byte, trivially

    # The trip sheet is the first tab — the reader's rule, asked here of the
    # same bytes, so the export patches the sheet the data came from.
    sheet_path = _sheet_path(plan.blob, None)
    # The Information sheet is only resolved when there is something to write
    # there, so a workbook without one still exports its trip data.
    info_path = (_sheet_path(plan.blob, INFO_SHEET)
                 if plan.info_writes or plan.info_cell_writes else None)
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(plan.blob)) as src, \
            zipfile.ZipFile(out, "w") as dst:
        for path in (sheet_path, info_path):
            if path is not None and path not in src.namelist():
                raise XlsxPatchError(f"Resolved sheet part {path!r} not in archive")
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == sheet_path:
                data = _patch_sheet(data, plan.cell_writes, plan.new_rows,
                                    plan.deleted_rows)
            elif item.filename == info_path:
                data = _patch_info(data, plan.info_writes, plan.info_cell_writes)
            # compress_type copied so untouched entries stay byte-identical
            # after recompression of the archive container.
            dst.writestr(item, data)
    return out.getvalue()


# -- workbook plumbing -------------------------------------------------------

def _sheet_path(blob: bytes, sheet_name: str | None) -> str:
    """Resolve a sheet -> worksheet part path via workbook.xml + rels.

    sheet_name None means the FIRST TAB, which is how the trip sheet is found;
    the Information sheet is still asked for by name. workbook.xml lists its
    <sheet> elements in tab order, so the first one in the document is tab 1 —
    hidden and chart tabs counted, the same count xlsx_reader.read makes. The
    two have to agree, or an edit would be spliced into a tab the trips were
    never read from; test_xlsx_reader.py pins them to each other.
    """
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        workbook = zf.read("xl/workbook.xml").decode("utf-8")
        rels = zf.read("xl/_rels/workbook.xml.rels").decode("utf-8")
    # \b in the first-tab form so it cannot settle on the enclosing <sheets>.
    pattern = (r"<sheet\b[^>]*/?>" if sheet_name is None else
               rf'<sheet[^>]*name="{re.escape(escape(sheet_name))}"[^>]*/?>')
    sheet = re.search(pattern, workbook)
    if sheet is None:
        raise XlsxPatchError(
            "Workbook has no sheets in workbook.xml" if sheet_name is None
            else f"Sheet {sheet_name!r} not found in workbook.xml")
    rid_match = re.search(r'r:id="(rId\d+)"', sheet.group(0))
    if rid_match is None:
        raise XlsxPatchError("Sheet element carries no relationship id")
    rel = re.search(
        rf'<Relationship[^>]*Id="{rid_match.group(1)}"[^>]*Target="([^"]+)"', rels
    ) or re.search(
        rf'<Relationship[^>]*Target="([^"]+)"[^>]*Id="{rid_match.group(1)}"', rels
    )
    if rel is None:
        raise XlsxPatchError("Sheet relationship not found in workbook rels")
    target = rel.group(1)
    if target.startswith("/"):                    # absolute part name
        return target.lstrip("/")
    return target if target.startswith("xl/") else f"xl/{target}"


# -- sheet XML patching ------------------------------------------------------

def _col_index(letter: str) -> int:
    n = 0
    for ch in letter:
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n


_CELL_REF = re.compile(rb'<c r="([A-Z]+)(\d+)"')


def _cell_kind(column: str) -> Kind:
    """Native type for a column of the Capitals layout.

    Only valid for that sheet: these are bare letters, and 'N' is 'Company
    name' here but the fleet count on the Information sheet. Every caller
    therefore passes a Kind explicitly rather than letting the writer look a
    letter up — a shared lookup across two layouts would eventually splice a
    number into a text column.
    """
    if column in _DATE_COLS:
        return "date"
    if column in _NUM_COLS:
        return "num"
    return "str"


def _patch_sheet(xml: bytes, writes: tuple[CellWrite, ...],
                 new_rows: tuple[NewRow, ...],
                 deleted_rows: tuple[int, ...] = ()) -> bytes:
    if b"<sheetData" not in xml:
        raise XlsxPatchError("sheetData element not found (unexpected namespace?)")

    # Deletions first, so appended rows are numbered against the final sheet.
    # A row is never both deleted and written: build_export_plan reads the
    # working set, which a deleted row has already left.
    if deleted_rows:
        xml = _delete_rows(xml, deleted_rows)

    by_row: dict[int, list[tuple[str, str, Kind]]] = {}
    for w in writes:
        by_row.setdefault(w.sheet_row, []).append(
            (w.column, w.value, _cell_kind(w.column)))

    for row_num, row_writes in sorted(by_row.items()):
        xml = _patch_row(xml, row_num, row_writes)

    if new_rows:
        xml = _append_rows(xml, new_rows)
    return xml


def _patch_info(xml: bytes, writes: tuple[InfoWrite, ...],
                cell_writes: tuple[InfoCellWrite, ...] = ()) -> bytes:
    """Fleet counts into Information!N as numbers, so the cell keeps behaving
    like the hand-entered ones beside it; pick lists as text down their own
    columns.

    Grouped by row and applied in one pass per row, because the two kinds can
    land on the same row and `_patch_row` re-locates the row's byte span each
    time it is called.
    """
    if b"<sheetData" not in xml:
        raise XlsxPatchError("sheetData element not found in Information sheet")
    by_row: dict[int, list[tuple[str, str, Kind]]] = {}
    for w in writes:
        by_row.setdefault(w.sheet_row, []).append(
            (w.column, str(w.value), "num"))
    for cw in cell_writes:
        by_row.setdefault(cw.sheet_row, []).append((cw.column, cw.value, "str"))

    for row_num, row_writes in sorted(by_row.items()):
        # A pick list can run past the last row the sheet has, and past gaps
        # in it — the reference workbook stops at 56 and is missing 45, 46
        # and 54, while its 'OP' list needs 58 rows.
        xml = _ensure_row(xml, row_num)
        xml = _patch_row(xml, row_num, row_writes)

    if by_row:
        widest = max((c for w in by_row.values() for c, _, _ in w),
                     key=_col_index)
        xml = _grow_dimension(xml, widest, max(by_row))
    return xml


def _ensure_row(xml: bytes, row_num: int) -> bytes:
    """Add an empty <row r="N"/> when the sheet has none, so `_patch_row` has
    something to splice into.

    Rows must stay in ascending order inside sheetData, and a sheet simply
    omits rows nobody ever touched — so the new row goes before the first
    higher-numbered one, or at the end when there is none.
    """
    if re.search(rf'<row r="{row_num}"[ />]'.encode(), xml):
        return xml
    insert_at = None
    for m in re.finditer(rb'<row r="(\d+)"', xml):
        if int(m.group(1)) > row_num:
            insert_at = m.start()
            break
    if insert_at is None:
        insert_at = xml.rindex(b"</sheetData>")
    return xml[:insert_at] + f'<row r="{row_num}"/>'.encode() + xml[insert_at:]


def _grow_dimension(xml: bytes, column: str | None, row: int) -> bytes:
    """Widen <dimension> to cover cells a patch added beyond it.

    Metadata Excel tolerates being stale, but only ever grown here: shrinking
    it would hide real cells from readers that trust it.
    """
    dim = re.search(rb'<dimension ref="([A-Z]+\d+):([A-Z]+)(\d+)"/>', xml)
    if dim is None:
        return xml
    end_col, end_row = dim.group(2).decode(), int(dim.group(3))
    new_col = (column if column and _col_index(column) > _col_index(end_col)
               else end_col)
    new_row = max(end_row, row)
    if (new_col, new_row) == (end_col, end_row):
        return xml
    ref = dim.group(1) + b":" + new_col.encode() + str(new_row).encode()
    return xml[:dim.start()] + b'<dimension ref="' + ref + b'"/>' + xml[dim.end():]


def _grow_autofilter(xml: bytes, row: int) -> bytes:
    """Widen <autoFilter ref> to cover appended rows, like _grow_dimension.

    The reference sheet ships with an active filter; a manual trip appended
    outside its range is invisible while the filter is on — the same
    had-to-hunt-for-it problem _append_rows' row placement already solves.
    Grown only, and left alone when absent or not in the expected form.
    """
    m = re.search(rb'(<autoFilter ref="[A-Z]+\d+:[A-Z]+)(\d+)"', xml)
    if m is None or int(m.group(2)) >= row:
        return xml
    return (xml[:m.start()] + m.group(1) + str(row).encode() + b'"'
            + xml[m.end():])


def _row_span(xml: bytes, row_num: int) -> tuple[int, int, bytes]:
    """Locate the <row r="N" ...>...</row> (or self-closing) byte span."""
    open_match = re.search(rf'<row r="{row_num}"[ />]'.encode(), xml)
    if open_match is None:
        raise XlsxPatchError(f"Row {row_num} not found in sheet XML")
    start = open_match.start()
    head_end = xml.index(b">", start)
    if xml[head_end - 1:head_end] == b"/":        # self-closing, no cells
        return start, head_end + 1, xml[start:head_end + 1]
    end = xml.index(b"</row>", start) + len(b"</row>")
    return start, end, xml[start:end]


def _cell_xml(ref: str, style: bytes | None, value: str) -> bytes:
    value = _XML_UNSAFE.sub(" ", value)
    s_attr = b' s="' + style + b'"' if style else b""
    if not value:
        # Genuinely empty, not an empty inline string: a cleared cell and a
        # pruned pick-list entry must read back as blank, the same as a cell
        # nobody ever filled in. Style is kept so the column still looks like
        # itself.
        return f'<c r="{ref}"'.encode() + s_attr + b"/>"
    # Edge whitespace is significant only under xml:space="preserve" — Excel
    # emits it itself for such strings, and without it the reader trims on
    # import: a silent round-trip mutation.
    space = ' xml:space="preserve"' if value != value.strip() else ""
    return (f'<c r="{ref}"'.encode() + s_attr
            + f' t="inlineStr"><is><t{space}>{escape(value)}</t></is></c>'.encode())


def _typed_cell_xml(ref: str, kind: Kind, style: bytes | None, value: str) -> bytes:
    """A cell in its native type where the caller says the layout calls for one.

    An edited Start date written as an inline string would sit left-aligned in
    a date-formatted column and read back as text, so date and number cells
    emit a bare <v>. A date needs the column's date style to render as a date
    at all; with no style to copy, the string is the honest fallback.
    """
    if kind == "date" and _ISO_DATE.match(value) and style:
        try:
            serial = (date.fromisoformat(value) - _EPOCH).days
        except ValueError:
            # ISO-shaped but not a real date ("2026-13-45"): the honest
            # string, not a crashed export — the contract already flags the
            # row with the value quoted verbatim.
            return _cell_xml(ref, style, value)
        return f'<c r="{ref}" s="'.encode() + style + f'"><v>{serial}</v></c>'.encode()
    # Exactly int()'s own grammar (optional sign + digits): the guard must be
    # the precondition of the int() below — lstrip("-") let "--5" through and
    # isdigit let superscripts through; either crashed the whole export.
    if kind == "num" and _INT_TEXT.fullmatch(value):
        s_attr = b' s="' + style + b'"' if style else b""
        return f'<c r="{ref}"'.encode() + s_attr + f"><v>{int(value)}</v></c>".encode()
    return _cell_xml(ref, style, value)


def _patch_row(xml: bytes, row_num: int,
               writes: list[tuple[str, str, Kind]],
               force_styles: dict[str, bytes] | None = None) -> bytes:
    """Splice cells into a row, keeping each target cell's own style.

    `force_styles` overrides that per column, for the one caller that must not
    inherit: a manually entered trip lands in a trailing empty row whose cells
    carry a general format, and inheriting it would render its dates as day
    serials. It takes the style of the data rows above instead.
    """
    start, end, row = _row_span(xml, row_num)
    if row.endswith(b"/>"):                       # open up a self-closing row
        row = row[:-2] + b"></row>"

    for column, value, kind in writes:
        ref = f"{column}{row_num}"
        forced = (force_styles or {}).get(column)
        # Self-closing form FIRST, as its own alternative. Folded into one
        # branch as [^>]*(?:/>|>.*?</c>), the greedy [^>]* eats the trailing
        # '/' of an empty cell, the '/>' branch then fails, and '>.*?</c>'
        # runs on to the NEXT cell's closing tag — so writing to an empty cell
        # would delete every passthrough cell up to the next populated one.
        existing = re.search(
            rf'<c r="{ref}"[^>]*?/>|<c r="{ref}"[^>]*?>.*?</c>'.encode(),
            row, re.DOTALL,
        )
        if existing is not None:
            style_m = re.search(rb' s="(\d+)"', existing.group(0))
            style = forced or (style_m.group(1) if style_m else None)
            row = row[:existing.start()] \
                + _typed_cell_xml(ref, kind, style, value) \
                + row[existing.end():]
        else:
            # Insert keeping cells in column order (required by the format).
            target_idx = _col_index(column)
            insert_at = None
            for m in _CELL_REF.finditer(row):
                if int(m.group(2)) == row_num and _col_index(m.group(1).decode()) > target_idx:
                    insert_at = m.start()
                    break
            new_cell = _typed_cell_xml(ref, kind, forced, value)
            if insert_at is None:
                close = row.rindex(b"</row>")
                row = row[:close] + new_cell + row[close:]
            else:
                row = row[:insert_at] + new_cell + row[insert_at:]

    return xml[:start] + row + xml[end:]


def _delete_rows(xml: bytes, rows: tuple[int, ...]) -> bytes:
    """Cut whole <row> elements out of the sheet.

    The row's own cells, styles and formulas go with it; everything around it
    is untouched bytes. Numbering is left alone — `r` attributes are absolute
    and every cell reference repeats its row number, so renumbering would mean
    rewriting the entire sheet, which is the one thing this module exists to
    avoid. Excel reads the gap as an empty row, which is the honest picture:
    the trip is gone, the rows below it did not move.
    """
    # Each span is located against the current bytes, so order is a matter of
    # determinism rather than correctness.
    for row_num in sorted(rows, reverse=True):
        start, end, _ = _row_span(xml, row_num)
        xml = xml[:start] + xml[end:]
    return xml


_CELL_TAG = re.compile(rb'<c r="([A-Z]+)(\d+)"([^>]*)>')
_STYLE_ATTR = re.compile(rb' s="(\d+)"')


def _scan_populated(xml: bytes, bound: int,
                    columns: set[str]) -> tuple[int, dict[str, bytes]]:
    """One pass over the cells (up to `bound`) for the two things appending
    needs.

    Returns the last row carrying a value, and a style index per column copied
    from the last populated DATA row that had one.

    Only cells that actually carry a value count, and both answers depend on
    that. The sheet trails ~1300 rows of empty-but-styled cells below the data:
    they carry a general format, so taking the literal last cell would style an
    appended date as a plain number (Excel showing 46266 instead of
    2026-09-01), and taking the literal last row would put a manually entered
    trip ~1300 rows below everything else.

    The two answers treat the header row differently, on purpose. It counts
    for `last_row` — on a sheet with no trips yet it is the only populated
    row, and the first trip belongs directly under it. It never counts for
    `styles`: a header is not a data row, and its cells are general-format in
    every column. A new season's workbook is exactly that sheet (header, then
    2,987 formatted but empty rows), and copying the header there sent the
    first trips out with dates as bare serials — 46366 in Excel, 46366 in the
    csv export, and refused by this app's own import as "not a valid date".
    A column this leaves without an entry simply keeps the style of the cell
    the value lands in (see `_patch_row`), which in those workbooks is the
    blank row's own d-mmm-yy.

    One pass rather than two because the trip sheet is 14 MB and both walks
    want the same cell tags — and the caller's bound is passed to finditer
    rather than applied as a slice, which would copy those 14 MB first.
    """
    last_row = 0
    styles: dict[str, bytes] = {}
    for m in _CELL_TAG.finditer(xml, 0, bound):
        if m.group(3).endswith(b"/"):             # self-closing = empty
            continue
        col = m.group(1).decode()
        row = int(m.group(2))
        # Contract columns only: the reader's own rule is that a row with
        # nothing but date-grid residue (P+) is not a trip, and row_labels
        # promises the same numbering — counting residue here made the
        # export contradict both.
        if col in _CONTRACT_COLS:
            last_row = max(last_row, row)
        if col in columns and row > HEADER_ROW:
            s = _STYLE_ATTR.search(m.group(3))
            if s:
                styles[col] = s.group(1)          # later rows overwrite: last wins
    return last_row, styles


def _append_rows(xml: bytes, new_rows: tuple[NewRow, ...]) -> bytes:
    """Write manually entered trips into the first free rows below the data.

    'Free' means 'holds no values', not 'past the last <row> element'. The
    reference sheet's data ends at row 1671 but its row elements run out to
    3000, so appending after the last element put a new trip ~1330 rows below
    everything else — outside the conditional formatting and outside the
    'Bus Companies' data validation, both of which stop at row 3000, and far
    enough away that you had to hunt for a trip you had just added.

    Deletions are applied before this in _patch_sheet, so a new trip can take
    a row number a deletion just freed. That is consistent within the exported
    file, and self-correcting: undoing the deletion makes the row populated
    again, so the next export places the trip below it instead.
    """
    close = xml.rindex(b"</sheetData>")
    all_cols = {c for nr in new_rows for c in nr.values if nr.values.get(c) not in (None, "")}
    last_row, styles = _scan_populated(xml, close, all_cols)
    if last_row == 0:
        raise XlsxPatchError("Sheet has no populated rows to append after")

    for i, nr in enumerate(new_rows):
        row_num = last_row + 1 + i
        writes = [
            (col, str(nr.values[col]), _cell_kind(col))
            for col in sorted(nr.values, key=_col_index)
            if nr.values.get(col) not in (None, "")
        ]
        # The target row usually already exists as an empty styled one; when it
        # does not (a sheet with no trailing rows) _ensure_row creates it in
        # place. Either way the new cells take the style of the data rows
        # above, never the general format of the blank row they land in —
        # unless there is no data row above at all, when the blank row's own
        # style is the only statement the sheet makes (see _scan_populated).
        xml = _ensure_row(xml, row_num)
        xml = _patch_row(xml, row_num, writes, force_styles=styles)

    xml = _grow_dimension(xml, None, last_row + len(new_rows))
    return _grow_autofilter(xml, last_row + len(new_rows))
