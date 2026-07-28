import pytest

from buschain import contract
from buschain.codecs import csv_codec
from buschain.codecs.types import (
    ALL_COLUMNS, COLUMN_TITLES, COMPANY_NAME, START_DATE, STATUS,
)

HEADER = ",".join(COLUMN_TITLES[c] for c in ALL_COLUMNS)


def make_csv(*rows: str) -> str:
    return HEADER + "\n" + "\n".join(rows) + "\n"


def test_read_maps_operated_columns():
    text = make_csv(
        'Confirmed,123,,,2026-04-01,2026-04-03,STO,CPH,Chinese,Mia,650EUR,49,N/A,Ollex,note'
    )
    ds = csv_codec.read(text)
    (row,) = ds.rows
    assert row.sheet_row == 2
    assert row.values[STATUS] == "Confirmed"
    assert row.values[START_DATE] == "2026-04-01"
    assert row.values[COMPANY_NAME] == "Ollex"
    assert ds.blob is None and ds.csv_header is not None


def test_wrong_header_rejected():
    with pytest.raises(csv_codec.CsvError):
        csv_codec.read("Foo,Bar\n1,2\n")


def test_blank_rows_skipped_extra_columns_kept():
    text = make_csv(
        'Confirmed,1,,,,,2026-04-01,2026-04-02,STO,CPH,,,,,,,,GRID1,GRID2',
        ',,,,,,,,,,,,,,,,',
    )
    ds = csv_codec.read(text)
    assert len(ds.rows) == 1
    assert ds.csv_rows is not None
    assert ds.csv_rows[0][-2:] == ("GRID1", "GRID2")


def test_csv_roundtrip_through_validate(registry):
    text = make_csv(
        'Confirmed,123,,,2026-04-01,2026-04-03,STO,CPH trf,,,,49,,Kautra 2,'
    )
    ds = csv_codec.read(text)
    report = contract.validate(ds, registry)
    (trip,) = report.trips
    assert trip.arr_city == "CPH"
    assert trip.assignment is not None
    assert (trip.assignment.company, trip.assignment.unit) == ("kautra", 2)


def test_write_rows_roundtrips_values():
    header = tuple(COLUMN_TITLES[c] for c in ALL_COLUMNS)
    rows = [["Confirmed", "1", "", "", "2026-04-01", "2026-04-02",
             "STO", "CPH", "", "", "", "49", "", "Ollex", ""]]
    text = csv_codec.write_rows_csv(header, rows)
    ds = csv_codec.read(text)
    assert ds.csv_rows == (tuple(rows[0]),)


def test_render_workbook_csv_matches_sheet(workbook_bytes):
    text = csv_codec.render_workbook_csv(workbook_bytes)
    lines = text.splitlines()
    assert lines[0].startswith("Status,Project,Bus Preference,P Notes")
    first = lines[1].split(",")
    assert first[0] == "Confirmed"
    assert first[4] == "2026-04-01"          # dates ISO-rendered


def test_fresh_workbook_reimports(registry):
    header = tuple(COLUMN_TITLES[c] for c in ALL_COLUMNS)
    rows = [["Confirmed", "1", "", "", "2026-04-01", "2026-04-02",
             "STO", "CPH", "", "", "", "49", "", "", ""]]
    blob = csv_codec.build_fresh_workbook(header, rows)
    from buschain.codecs import xlsx_reader
    ds = xlsx_reader.read(blob)
    report = contract.validate(ds, registry)
    assert len(report.trips) == 1
