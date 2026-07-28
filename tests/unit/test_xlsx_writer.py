"""Splice correctness on synthetic sheet XML and real-file patching."""
import io
import zipfile

import pytest

from buschain.codecs import xlsx_writer
from buschain.codecs.types import (
    ARRIVAL, CellWrite, COMPANY_NAME, DEPARTURE, END_DATE, ExportPlan,
    NewRow, PAX, PROJECT, START_DATE, STATUS,
)
from buschain.codecs.xlsx_writer import _patch_sheet


SHEET = (
    b'<?xml version="1.0"?><worksheet>'
    b'<dimension ref="A1:C3"/><sheetData>'
    b'<row r="1"><c r="A1" t="inlineStr"><is><t>Status</t></is></c></row>'
    b'<row r="2"><c r="A2" s="5" t="inlineStr"><is><t>Confirmed</t></is></c>'
    b'<c r="C2"><v>7</v></c></row>'
    b'<row r="3"/>'
    b'</sheetData></worksheet>'
)


def test_replace_existing_cell_preserves_style():
    out = _patch_sheet(SHEET, (CellWrite(2, "A", "Canceled"),), ())
    assert b'<c r="A2" s="5" t="inlineStr"><is><t>Canceled</t></is></c>' in out
    assert b"Confirmed" not in out
    assert b'<c r="C2"><v>7</v></c>' in out          # neighbors untouched


def test_insert_missing_cell_in_column_order():
    out = _patch_sheet(SHEET, (CellWrite(2, "B", "Ollex 1"),), ())
    a, b, c = (out.index(ref) for ref in (b'r="A2"', b'r="B2"', b'r="C2"'))
    assert a < b < c


def test_insert_at_row_end():
    out = _patch_sheet(SHEET, (CellWrite(2, "P", "Ollex"),), ())
    assert out.index(b'r="C2"') < out.index(b'r="P2"')


def test_write_into_empty_cell_leaves_later_cells_intact():
    """Regression: writing to an existing self-closing (empty) cell must not
    swallow the passthrough cells that follow it.

    The target B2 is empty and self-closing; D2 and E2 sit after it and carry
    values the system must never touch. A regex that lets the '>.*?</c>'
    branch win for a self-closing tag matches through to D2's closing tag and
    deletes everything in between.
    """
    sheet = (
        b'<?xml version="1.0"?><worksheet><dimension ref="A1:E2"/><sheetData>'
        b'<row r="2"><c r="A2" s="5" t="inlineStr"><is><t>Confirmed</t></is></c>'
        b'<c r="B2" s="9"/>'
        b'<c r="D2" s="1" t="s"><v>25</v></c>'
        b'<c r="E2" s="1"><v>42</v></c></row>'
        b'</sheetData></worksheet>'
    )
    out = _patch_sheet(sheet, (CellWrite(2, "B", "Kautra"),), ())
    assert b'<c r="B2" s="9" t="inlineStr"><is><t>Kautra</t></is></c>' in out
    assert b'<c r="D2" s="1" t="s"><v>25</v></c>' in out
    assert b'<c r="E2" s="1"><v>42</v></c>' in out


def test_write_into_self_closing_row():
    out = _patch_sheet(SHEET, (CellWrite(3, "A", "Pending"),), ())
    assert b'<row r="3"><c r="A3" t="inlineStr"><is><t>Pending</t></is></c></row>' in out


def test_xml_escaping():
    out = _patch_sheet(SHEET, (CellWrite(2, "A", 'A<B>&"C'),), ())
    assert b"<t>A&lt;B&gt;&amp;\"C</t>" in out


def test_append_uses_the_first_row_with_no_values():
    """Row 3 exists but is empty, so a new trip goes there rather than to row 4.

    'Free' means holds no values, not past the last <row> element: the real
    sheet's data ends at 1671 while its row elements run to 3000, and appending
    past the last element put a new trip ~1330 rows below everything else.
    """
    out = _patch_sheet(SHEET, (), (NewRow({"A": "Pending", "I": "STO"}),))
    # column A style is copied from the last populated data row (s="5")
    assert b'<row r="3"><c r="A3" s="5" t="inlineStr"><is><t>Pending</t></is></c>' in out
    assert b'<row r="4"' not in out
    assert b'<dimension ref="A1:C3"/>' in out         # already covered it


def test_two_appended_rows_take_consecutive_free_rows():
    out = _patch_sheet(SHEET, (), (NewRow({"A": "Pending"}), NewRow({"A": "Confirmed"})))
    assert b'<c r="A3" s="5" t="inlineStr"><is><t>Pending</t></is></c>' in out
    assert b'<c r="A4" s="5" t="inlineStr"><is><t>Confirmed</t></is></c>' in out
    assert out.index(b'<row r="3"') < out.index(b'<row r="4"') < out.index(b"</sheetData>")
    assert b'<dimension ref="A1:C4"/>' in out         # grown by the one new row


def test_appended_row_does_not_inherit_the_blank_rows_general_format():
    """The regression the style override exists for.

    The trailing empty rows carry a general number format. Inheriting it would
    render a start date as the day serial 46266 instead of a date, so the new
    cells take the style of the data rows above instead.
    """
    sheet = SHEET.replace(b'<row r="3"/>', b'<row r="3"><c r="A3" s="99"/></row>')
    out = _patch_sheet(sheet, (), (NewRow({"A": "Pending"}),))
    assert b'<c r="A3" s="5" t="inlineStr">' in out   # s=5 from row 2, not s=99
    assert b's="99"' not in out


def test_unmodified_export_returns_original_bytes(workbook_bytes):
    plan = ExportPlan(blob=workbook_bytes, cell_writes=(), new_rows=())
    assert xlsx_writer.export(plan) is workbook_bytes


def test_patched_export_changes_only_target_cells(workbook_bytes):
    plan = ExportPlan(
        blob=workbook_bytes,
        cell_writes=(CellWrite(3, STATUS, "Confirmed"),
                     CellWrite(2, COMPANY_NAME, "Kautra 1")),
        new_rows=(),
    )
    patched = xlsx_writer.export(plan)

    with zipfile.ZipFile(io.BytesIO(workbook_bytes)) as before, \
            zipfile.ZipFile(io.BytesIO(patched)) as after:
        assert before.namelist() == after.namelist()
        changed = [n for n in before.namelist()
                   if before.read(n) != after.read(n)]
    assert changed == ["xl/worksheets/sheet1.xml"]

    import openpyxl
    wb_a = openpyxl.load_workbook(io.BytesIO(workbook_bytes), data_only=True)
    wb_b = openpyxl.load_workbook(io.BytesIO(patched), data_only=True)
    ws_a, ws_b = wb_a["Capitals 2026"], wb_b["Capitals 2026"]
    assert ws_b[f"{STATUS}3"].value == "Confirmed"
    assert ws_b[f"{COMPANY_NAME}2"].value == "Kautra 1"
    diffs = []
    for row_a, row_b in zip(ws_a.iter_rows(max_row=50), ws_b.iter_rows(max_row=50)):
        for ca, cb in zip(row_a, row_b):
            if ca.value != cb.value:
                diffs.append(ca.coordinate)
    assert diffs == [f"{COMPANY_NAME}2", f"{STATUS}3"]   # row-major iteration order


def test_appended_rows_read_back_with_native_dates(workbook_bytes):
    from datetime import datetime
    plan = ExportPlan(
        blob=workbook_bytes,
        cell_writes=(),
        new_rows=(NewRow({STATUS: "Pending", PROJECT: "999",
                          START_DATE: "2026-09-01", END_DATE: "2026-09-03",
                          DEPARTURE: "OSL", ARRIVAL: "STO",
                          PAX: "49", COMPANY_NAME: ""}),),
    )
    patched = xlsx_writer.export(plan)
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(patched), data_only=True)
    ws = wb["Capitals 2026"]
    col = lambda letter: ord(letter) - ord("A") + 1     # 1-based for ws.cell
    # Not ws.max_row: the sheet's row elements run to 3000 while its data ends
    # at 1671, and the new trip lands just below the data at 1672.
    new_row = next(r for r in range(1, ws.max_row + 1)
                   if ws.cell(r, col(PROJECT)).value == 999)
    assert new_row == 1672
    assert ws.cell(new_row, col(STATUS)).value == "Pending"
    got = ws.cell(new_row, col(START_DATE)).value
    assert got == datetime(2026, 9, 1) or got == "2026-09-01"
    assert ws.cell(new_row, col(PROJECT)).value == 999


def test_csv_sourced_plan_refuses_patch():
    plan = ExportPlan(blob=None, cell_writes=(), new_rows=())
    with pytest.raises(xlsx_writer.XlsxPatchError):
        xlsx_writer.export(plan)


# -- Information sheet: pick-list columns --------------------------------------

INFO = (
    b'<?xml version="1.0"?><worksheet>'
    b'<dimension ref="A1:B4"/><sheetData>'
    b'<row r="1"><c r="A1" t="inlineStr"><is><t>Bus Companies</t></is></c>'
    b'<c r="B1" s="7"/></row>'
    b'<row r="2"><c r="A2" t="inlineStr"><is><t>Ollex</t></is></c>'
    b'<c r="B2" t="inlineStr"><is><t>Swedish</t></is></c></row>'
    b'<row r="4"><c r="B4" t="inlineStr"><is><t>Korean</t></is></c></row>'
    b'</sheetData></worksheet>'
)


def _info(*cells):
    from buschain.codecs.xlsx_writer import _patch_info
    from buschain.codecs.types import InfoCellWrite
    return _patch_info(INFO, (), tuple(InfoCellWrite(*c) for c in cells))


def test_pick_list_creates_rows_past_the_end_of_the_sheet():
    """A list can be longer than the sheet is tall — the reference workbook
    ends at row 56 and its OP list needs 58."""
    out = _info((5, "B", "Danish"), (6, "B", "Finnish"))
    assert b'<row r="5"><c r="B5" t="inlineStr"><is><t>Danish</t></is></c></row>' in out
    assert out.index(b'<row r="5">') < out.index(b'<row r="6">') < out.index(b"</sheetData>")
    assert b'<dimension ref="A1:B6"/>' in out


def test_pick_list_fills_a_gap_in_ascending_row_order():
    """Row 3 is missing from the sheet. Rows must stay ordered inside
    sheetData, so the new one goes before row 4, not at the end."""
    out = _info((3, "B", "Danish"))
    assert out.index(b'<row r="2">') < out.index(b'<row r="3">') < out.index(b'<row r="4">')


def test_pruned_entry_leaves_a_genuinely_empty_cell():
    """Not an empty inline string: a pruned value has to read back as blank,
    the same as a cell nobody ever filled in."""
    out = _info((4, "B", ""))
    assert b'<c r="B4"/>' in out
    assert b"Korean" not in out


def test_new_pick_list_column_widens_the_dimension():
    out = _info((1, "D", "OP"), (2, "D", "Andrea"))
    assert b'<dimension ref="A1:D4"/>' in out


def test_pick_list_header_inherits_the_style_of_the_cell_it_replaces():
    out = _info((1, "B", "Nationality"))
    assert b'<c r="B1" s="7" t="inlineStr"><is><t>Nationality</t></is></c>' in out


def test_fleet_and_pick_list_writes_share_a_row():
    """Both kinds can land on row 1; applied per row, because each patch
    re-locates the row's byte span."""
    from buschain.codecs.xlsx_writer import _patch_info
    from buschain.codecs.types import InfoCellWrite, InfoWrite
    out = _patch_info(INFO, (InfoWrite(2, 3),), (InfoCellWrite(2, "B", "Danish"),))
    assert b'<c r="N2"><v>3</v></c>' in out
    assert b'<c r="B2" t="inlineStr"><is><t>Danish</t></is></c>' in out
