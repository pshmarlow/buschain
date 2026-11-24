import csv
import io
from datetime import datetime

import openpyxl
import pytest

from buschain.codecs import csv_codec, xlsx_reader, xlsx_writer
from buschain.codecs.types import (
    ARRIVAL, COLUMN_ORDER, COMPANY_NAME, CellWrite, ExportPlan, START_DATE, STATUS,
)


def test_row_count_matches_profile(dataset):
    # 1670 rows with content in the contract columns — profiled from
    # the reference file. Rows past these carry only date-grid residue.
    assert len(dataset.rows) == 1670


def test_sheet_rows_are_stable_ids(dataset):
    assert dataset.rows[0].sheet_row == 2
    assert all(r.sheet_row is not None for r in dataset.rows)


def test_values_are_raw_and_typed(dataset):
    first = dataset.rows[0]
    assert first.values[STATUS] == "Confirmed"
    assert isinstance(first.values[START_DATE], datetime)
    assert first.values[COMPANY_NAME] == "Ollex"


def test_blob_rides_along(dataset, workbook_bytes):
    assert dataset.blob == workbook_bytes


# -- which tab is the trip sheet ---------------------------------------------
# The first one, whatever it is called. The tab is renamed every season
# ('Capitals 2026', then 'CAPITALS'), and finding it by name made each rename a
# refused import until the name was added to a list in the code. Position is
# what stays put; the row-1 layout check is what proves the tab found is the
# right one.

_HEADER = list(COLUMN_ORDER)
_TRIP = ["Confirmed", 1, None, None, "2026-12-01", "2026-12-03", "STO", "CPH",
         None, None, None, 49, None, None, None]
# The same trip arriving somewhere else, so a test can tell which tab a row
# came from. Built by title rather than by index, like everything else here.
_OTHER_TRIP = ["OSL" if title == "Arrival" else value
               for title, value in zip(COLUMN_ORDER, _TRIP)]


def _book(*tabs) -> bytes:
    """A small workbook built in memory: each tab is (name, rows), in tab
    order. A trip tab is [_HEADER, <a trip>]; a decoy is anything else."""
    wb = openpyxl.Workbook(write_only=True)     # starts with no tab at all
    for name, rows in tabs:
        ws = wb.create_sheet(name)
        for row in rows:
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_trips_are_read_from_the_first_tab_whatever_it_is_called():
    blob = _book(("Summer 27", [_HEADER, _TRIP]), ("Information", [["x"]]))
    assert [r.values[ARRIVAL] for r in xlsx_reader.read(blob).rows] == ["CPH"]


def test_a_later_tab_is_ignored_even_under_a_name_that_used_to_win():
    """'CAPITALS' was on the accepted list, and under the old rule this
    workbook's trips would have come from tab 2. Now it is just a later tab."""
    blob = _book(("Summer 27", [_HEADER, _TRIP]),
                 ("CAPITALS", [_HEADER, _OTHER_TRIP]))
    assert [r.values[ARRIVAL] for r in xlsx_reader.read(blob).rows] == ["CPH"]


def test_reader_writer_and_csv_agree_on_the_trip_tab():
    """Three codecs find the tab three different ways — openpyxl's sheet list
    here and in the csv export, a regex over workbook.xml in the xlsx writer —
    and nothing but this test holds them to one answer. If they ever disagree,
    an accepted assignment is spliced into a tab the trips were never read
    from: silent corruption of somebody's workbook."""
    blob = _book(("Summer 27", [_HEADER, _TRIP]),
                 ("CAPITALS", [_HEADER, _OTHER_TRIP]))
    exported = xlsx_writer.export(ExportPlan(
        blob=blob, new_rows=(),
        cell_writes=(CellWrite(2, COMPANY_NAME, "Ollex 1"),)))

    first, later = openpyxl.load_workbook(io.BytesIO(exported)).worksheets
    assert first[f"{COMPANY_NAME}2"].value == "Ollex 1"
    assert later[f"{COMPANY_NAME}2"].value is None        # not one cell moved

    row = list(csv.reader(io.StringIO(csv_codec.render_workbook_csv(exported))))[1]
    by_title = dict(zip(COLUMN_ORDER, row))
    assert (by_title["Arrival"], by_title["Company name"]) == ("CPH", "Ollex 1")


def test_a_first_tab_that_is_not_the_trip_sheet_is_refused_by_name():
    """The trips are right there on tab 2, and that is exactly the case that
    must not be rescued by guessing: the rule is the first tab, and the error
    says which tab that was and what the others are, so it can be fixed in
    Excel by dragging one tab."""
    blob = _book(("Notes", [["something else"]]), ("CAPITALS", [_HEADER, _TRIP]))
    with pytest.raises(ValueError, match="first tab, 'Notes'") as refusal:
        xlsx_reader.read(blob)
    assert "not the contract layout" in str(refusal.value)
    assert "'CAPITALS'" in str(refusal.value)


def test_a_chart_as_the_first_tab_is_refused_rather_than_crashing():
    """A chart tab has no cells: reading rows off one is an AttributeError,
    which would surface as a 500 for what is simply the wrong tab in front."""
    from openpyxl.chart import BarChart, Reference
    wb = openpyxl.Workbook()
    trips = wb.worksheets[0]
    trips.title = "Trips"
    trips.append(_HEADER)
    trips.append(_TRIP)
    chart = BarChart()
    chart.add_data(Reference(trips, min_col=2, min_row=1, max_row=2),
                   titles_from_data=True)
    wb.create_chartsheet("Overview", 0).add_chart(chart)     # in front
    buf = io.BytesIO()
    wb.save(buf)
    with pytest.raises(ValueError, match="first tab, 'Overview', is a chart"):
        xlsx_reader.read(buf.getvalue())
