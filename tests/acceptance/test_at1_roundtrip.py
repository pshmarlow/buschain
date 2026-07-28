"""AT1: import -> export with no in-system changes reproduces every
passthrough cell and every non-operable row exactly.

We hold ourselves to the strongest form: with zero changes the export is
byte-for-byte the uploaded file; with changes, everything except the two
written cells is still value-and-entry-identical.

One deliberate exception, tested first below: a workbook whose Information
sheet has no pick-list columns yet gains them on its first export, because
the sheet is where those lists live. That happens once — the file is a fixed
point from then on, which is what every other test here works against via the
`workbook_with_lists` fixture.
"""
import io
import zipfile

import openpyxl

from buschain.codecs import xlsx_reader
from buschain.codecs.types import COMPANY_NAME, STATUS
from buschain.model import Status, TripId
from buschain.store import db
from buschain.store.repo import Repo


def make_repo(tmp_path, workbook_bytes, seed_result) -> Repo:
    repo = Repo(db.connect(tmp_path / "at1.db"))
    repo.seed_from(seed_result)
    repo.import_dataset(xlsx_reader.read(workbook_bytes), "LDC_PLANNER__test.xlsx")
    repo.sync_options(seed_result)                  # as the import endpoint does
    return repo


def test_first_export_adds_the_pick_list_columns_and_nothing_else(
        tmp_path, workbook_bytes, seed_result):
    """The one-time bootstrap: a workbook with no Nationality/OP columns gets
    them, written only into O and P of the Information sheet."""
    from buschain.codecs import xlsx_writer
    from buschain.codecs.seed import INFO_SHEET
    repo = make_repo(tmp_path, workbook_bytes, seed_result)
    exported = xlsx_writer.export(repo.build_export_plan())

    with zipfile.ZipFile(io.BytesIO(workbook_bytes)) as before, \
            zipfile.ZipFile(io.BytesIO(exported)) as after:
        assert before.namelist() == after.namelist()
        changed = [n for n in before.namelist() if before.read(n) != after.read(n)]
    assert len(changed) == 1                        # the Information part only

    ws_a = openpyxl.load_workbook(io.BytesIO(workbook_bytes), data_only=True)[INFO_SHEET]
    ws_b = openpyxl.load_workbook(io.BytesIO(exported), data_only=True)[INFO_SHEET]
    diffs = [
        ws_b.cell(r, c).coordinate
        for r in range(1, ws_b.max_row + 1)
        for c in range(1, ws_b.max_column + 1)
        if (ws_a.cell(r, c).value if r <= ws_a.max_row and c <= ws_a.max_column
            else None) != ws_b.cell(r, c).value
    ]
    assert diffs, "the lists have to land somewhere"
    assert {d.rstrip("0123456789") for d in diffs} == {"O", "P"}
    assert ws_b["O1"].value == "Nationality" and ws_b["P1"].value == "OP"
    options = repo.options()
    assert [ws_b.cell(r, 15).value for r in range(2, 2 + len(options["Nationality"]))] \
        == sorted(options["Nationality"])


def test_untouched_roundtrip_is_byte_identical(
        tmp_path, workbook_with_lists, seed_result_with_lists):
    """The steady state, and the fixed point the bootstrap converges to:
    re-importing an exported workbook and exporting it again changes nothing,
    so repeated cycles cannot ratchet the file."""
    from buschain.codecs import xlsx_writer
    repo = make_repo(tmp_path, workbook_with_lists, seed_result_with_lists)
    plan = repo.build_export_plan()
    assert plan.info_cell_writes == ()
    assert xlsx_writer.export(plan) == workbook_with_lists


def test_fleet_sync_then_export_is_byte_identical(
        tmp_path, workbook_with_lists, seed_result_with_lists):
    """The two fleet directions agree.

    Reading the Fleets column and writing it back must be a fixed point: if
    a sync could shift a value the export would then re-splice, the sheet and
    the registry would ratchet apart on every import/export cycle.
    """
    from buschain.codecs import xlsx_writer
    repo = make_repo(tmp_path, workbook_with_lists, seed_result_with_lists)
    assert repo.sync_fleets(seed_result_with_lists) == []
    plan = repo.build_export_plan()
    assert plan.info_writes == ()
    assert xlsx_writer.export(plan) == workbook_with_lists


def test_changed_fleet_writes_only_that_information_cell(
        tmp_path, workbook_with_lists, seed_result_with_lists):
    """Adding a unit moves the number in the sheet, and nothing else."""
    from buschain.codecs import xlsx_writer
    from buschain.codecs.seed import FLEET_HEADER, INFO_SHEET
    repo = make_repo(tmp_path, workbook_with_lists, seed_result_with_lists)
    repo.add_unit("ollex", 3)                       # Ollex: 2 chartered -> 3

    exported = xlsx_writer.export(repo.build_export_plan())
    with zipfile.ZipFile(io.BytesIO(workbook_with_lists)) as before, \
            zipfile.ZipFile(io.BytesIO(exported)) as after:
        changed = [n for n in before.namelist() if before.read(n) != after.read(n)]
    assert len(changed) == 1                        # the Information part only

    wb_a = openpyxl.load_workbook(io.BytesIO(workbook_with_lists), data_only=True)
    wb_b = openpyxl.load_workbook(io.BytesIO(exported), data_only=True)
    diffs = [
        (ca.coordinate, ca.value, cb.value)
        for row_a, row_b in zip(wb_a[INFO_SHEET].iter_rows(),
                                wb_b[INFO_SHEET].iter_rows())
        for ca, cb in zip(row_a, row_b) if ca.value != cb.value
    ]
    assert [(c, a, b) for c, a, b in diffs] == [(diffs[0][0], 2, 3)]
    assert wb_b[INFO_SHEET][diffs[0][0]].value == 3, FLEET_HEADER
    # The trip sheet is untouched: a fleet edit is not a trip edit.
    assert [
        (ca.coordinate, ca.value, cb.value)
        for row_a, row_b in zip(wb_a["Capitals 2026"].iter_rows(),
                                wb_b["Capitals 2026"].iter_rows())
        for ca, cb in zip(row_a, row_b) if ca.value != cb.value
    ] == []


def test_edited_roundtrip_touches_only_written_cells(
        tmp_path, workbook_with_lists, seed_result_with_lists):
    from buschain.codecs import xlsx_writer
    workbook_bytes = workbook_with_lists
    repo = make_repo(tmp_path, workbook_bytes, seed_result_with_lists)

    # One in-system status change + one accepted ad hoc assignment on a
    # known demand row (sheet row 1321 profiled as unassigned Pending).
    repo.set_status(TripId("sheet", 3), Status.CONFIRMED)
    repo.accept(TripId("sheet", 1321), "kautra", None, None)

    exported = xlsx_writer.export(repo.build_export_plan())

    with zipfile.ZipFile(io.BytesIO(workbook_bytes)) as before, \
            zipfile.ZipFile(io.BytesIO(exported)) as after:
        assert before.namelist() == after.namelist()
        changed = [n for n in before.namelist() if before.read(n) != after.read(n)]
        assert changed == ["xl/worksheets/sheet1.xml"]

    wb_a = openpyxl.load_workbook(io.BytesIO(workbook_bytes), data_only=True)
    wb_b = openpyxl.load_workbook(io.BytesIO(exported), data_only=True)
    ws_a, ws_b = wb_a["Capitals 2026"], wb_b["Capitals 2026"]
    diffs = []
    for row_a, row_b in zip(ws_a.iter_rows(), ws_b.iter_rows()):
        for ca, cb in zip(row_a, row_b):
            if ca.value != cb.value:
                diffs.append((ca.coordinate, ca.value, cb.value))
    # Exactly the two written cells. Nothing else — in particular no
    # passthrough date-grid cell on row 1321, which an earlier splice bug
    # wiped when the target cell was an empty self-closing one.
    assert diffs == [
        (f"{STATUS}3", "Canceled", "Confirmed"),
        (f"{COMPANY_NAME}1321", None, "Kautra"),
    ]


def test_deleted_row_leaves_the_sheet_and_nothing_else_moves(
        tmp_path, workbook_with_lists, seed_result_with_lists):
    """A deleted row is cut out of the sheet, not blanked and not shifted.

    Row numbers below it deliberately do not close up: `r` attributes are
    absolute and repeated on every cell, so renumbering would mean rewriting
    the whole sheet — the one thing byte-preserving export exists to avoid.
    Excel therefore shows a gap, which is the honest picture.
    """
    from buschain.codecs import xlsx_writer
    workbook_bytes = workbook_with_lists
    repo = make_repo(tmp_path, workbook_bytes, seed_result_with_lists)
    repo.delete_trip(TripId("sheet", 4))

    exported = xlsx_writer.export(repo.build_export_plan())
    with zipfile.ZipFile(io.BytesIO(workbook_bytes)) as before, \
            zipfile.ZipFile(io.BytesIO(exported)) as after:
        assert before.namelist() == after.namelist()
        assert [n for n in before.namelist()
                if before.read(n) != after.read(n)] == ["xl/worksheets/sheet1.xml"]

    wb_a = openpyxl.load_workbook(io.BytesIO(workbook_bytes), data_only=True)
    wb_b = openpyxl.load_workbook(io.BytesIO(exported), data_only=True)
    ws_a, ws_b = wb_a["Capitals 2026"], wb_b["Capitals 2026"]
    assert ws_a.max_row == ws_b.max_row          # rows below stayed put
    diffs = [
        (ca.row, ca.coordinate, ca.value, cb.value)
        for row_a, row_b in zip(ws_a.iter_rows(), ws_b.iter_rows())
        for ca, cb in zip(row_a, row_b) if ca.value != cb.value
    ]
    assert diffs, "row 4 carried values, so removing it must change something"
    assert {d[0] for d in diffs} == {4}          # only that row
    assert all(after_value is None for *_, after_value in diffs)


def test_deleting_a_row_is_undoable_back_to_byte_identical(
        tmp_path, workbook_with_lists, seed_result_with_lists):
    """Undo has to reach all the way through the export, not just the UI."""
    from buschain.codecs import xlsx_writer
    repo = make_repo(tmp_path, workbook_with_lists, seed_result_with_lists)
    with repo.undoable("delete row 4"):
        repo.delete_trip(TripId("sheet", 4))
    repo.undo()
    assert xlsx_writer.export(repo.build_export_plan()) == workbook_with_lists


def test_manual_trip_lands_directly_below_the_data(
        tmp_path, workbook_with_lists, seed_result_with_lists):
    """A manually entered trip goes to row 1672, right under the data.

    The sheet's row elements run to 3000 while its data ends at 1671, so
    appending after the last row *element* put the trip at 3001 — ~1330 rows
    below everything else, and outside the 'Bus Companies' data validation on
    C2:C3000 that every real row gets.
    """
    from buschain.codecs import xlsx_writer
    from buschain.codecs.types import (
        ARRIVAL, DEPARTURE, END_DATE, PAX, PROJECT, START_DATE, STATUS)
    repo = make_repo(tmp_path, workbook_with_lists, seed_result_with_lists)
    repo.add_manual_trip({
        STATUS: "Pending", PROJECT: "999",
        START_DATE: "2026-09-01", END_DATE: "2026-09-03",
        DEPARTURE: "OSL", ARRIVAL: "STO", PAX: "49",
    })
    exported = xlsx_writer.export(repo.build_export_plan())

    wb_a = openpyxl.load_workbook(io.BytesIO(workbook_with_lists), data_only=True)
    wb_b = openpyxl.load_workbook(io.BytesIO(exported), data_only=True)
    ws_a, ws_b = wb_a["Capitals 2026"], wb_b["Capitals 2026"]
    assert ws_b["A1672"].value == "Pending"
    assert ws_b["B1672"].value == 999
    assert ws_a["A1672"].value is None               # was free before
    assert ws_b.max_row == ws_a.max_row              # no row added past the tail

    # Only that row changed — the data above it is untouched.
    changed_rows = {
        ca.row
        for row_a, row_b in zip(ws_a.iter_rows(), ws_b.iter_rows())
        for ca, cb in zip(row_a, row_b) if ca.value != cb.value
    }
    assert changed_rows == {1672}
