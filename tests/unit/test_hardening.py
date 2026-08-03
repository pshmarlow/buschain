"""Pins for the structural-review fixes: silent-corruption, atomicity and
trust-boundary hardening. Each test names the failure it forbids."""
import io
import re
import zipfile
from xml.etree import ElementTree

import openpyxl
import pytest
from fastapi.testclient import TestClient

from buschain import contract
from buschain.codecs import csv_codec, xlsx_reader, xlsx_writer
from buschain.codecs.seed import SeedCompany, SeedResult
from buschain.codecs.types import (
    ALL_COLUMNS, COLUMN_ORDER, COLUMN_TITLES, PROJECT, RawTripRow,
)
from buschain.model import Company, Registry, TripId
from buschain.store import db
from buschain.store.repo import Repo, RepoError
from buschain.web.app import create_app


@pytest.fixture
def repo(tmp_path):
    return Repo(db.connect(tmp_path / "hardening.db"))


# -- xlsx writer: XML that Excel will actually open --------------------------

def test_control_characters_never_reach_the_xml():
    """XML 1.0 cannot represent \\x0b (Word's line break) even escaped;
    escape() passes it through and Excel then refuses the file — an export
    that 'succeeded' and corrupted the deliverable."""
    cell = xlsx_writer._cell_xml("A2", None, "bad\x0bvalue\x00x")
    ElementTree.fromstring(cell)                      # parses
    assert b"bad value x" in cell                     # words kept apart

    # And through the real splice path, not just the helper.
    sheet = (b'<worksheet><sheetData><row r="2"><c r="A2" t="inlineStr">'
             b"<is><t>old</t></is></c></row></sheetData></worksheet>")
    out = xlsx_writer._patch_sheet(
        sheet, (xlsx_writer.CellWrite(2, "A", "a\x0bb"),), ())
    ElementTree.fromstring(out)


def test_appended_rows_grow_the_autofilter_range():
    """The reference sheet ships with an ACTIVE filter; a manual trip appended
    outside its ref is invisible while the filter is on."""
    sheet = (b'<worksheet><dimension ref="A1:C3"/>'
             b'<autoFilter ref="A1:C3"><filterColumn colId="0"/></autoFilter>'
             b"<sheetData>"
             b'<row r="1"><c r="A1" t="inlineStr" s="1"><is><t>Status</t></is></c></row>'
             b'<row r="3"><c r="A3" t="inlineStr" s="1"><is><t>x</t></is></c></row>'
             b"</sheetData></worksheet>")
    out = xlsx_writer._append_rows(
        sheet, (xlsx_writer.NewRow(values={"A": "Confirmed"}),))
    assert b'<autoFilter ref="A1:C4">' in out
    # Grown only — a smaller target must leave it alone.
    assert xlsx_writer._grow_autofilter(sheet, 2) == sheet


def test_numeric_cell_with_superscript_digit_falls_back_to_text():
    """'²'.isdigit() is True but int('²') raises — the export crashed instead
    of writing the honest string."""
    cell = xlsx_writer._typed_cell_xml("B2", "num", None, "²")
    assert b"inlineStr" in cell


# -- reader: malformed input is an import error, not a 500 -------------------

def test_garbage_bytes_are_a_value_error():
    with pytest.raises(ValueError, match="Not a readable"):
        xlsx_reader.read(b"this is not a zip archive")


def test_shifted_header_is_rejected(workbook_bytes):
    """One inserted column shifts every value a slot left AND makes the export
    splice assignments into the wrong column — both silent. The header row is
    the tripwire."""
    wb = openpyxl.load_workbook(io.BytesIO(workbook_bytes))
    ws = wb[next(n for n in xlsx_reader.SHEET_NAMES if n in wb.sheetnames)]
    ws.insert_cols(1)
    buf = io.BytesIO()
    wb.save(buf)
    with pytest.raises(ValueError, match="not the contract layout"):
        xlsx_reader.read(buf.getvalue())


def test_superscript_project_is_a_reason_not_a_crash():
    registry = Registry(companies={}, cities={"sto": "STO", "cph": "CPH"})
    row = RawTripRow(sheet_row=2, values={
        **{c: None for c in ALL_COLUMNS},
        ALL_COLUMNS[0]: "Confirmed", PROJECT: "²",
        ALL_COLUMNS[4]: "2026-04-01", ALL_COLUMNS[5]: "2026-04-02",
        ALL_COLUMNS[6]: "STO", ALL_COLUMNS[7]: "CPH",
    })
    result = contract.validate_row(row, TripId("sheet", 2), registry)
    assert any("not numeric" in r for r in result.reasons)


def test_superscript_unit_number_does_not_resolve():
    registry = Registry(
        companies={"kautra": Company(name="Kautra", note=None, units=frozenset({1}))},
        cities={},
    )
    assert registry.resolve_company("Kautra ²") is None   # not a crash


def test_csv_header_case_and_spacing_variants_are_accepted():
    header = ",".join(t.upper() for t in COLUMN_ORDER)
    data = header + "\nConfirmed,1,,,2026-04-01,2026-04-02,STO,CPH,,,,,,,\n"
    assert len(csv_codec.read(data.encode()).rows) == 1


# -- store: atomicity and in-use guards --------------------------------------

def test_seed_from_failure_leaves_nothing_behind(repo):
    """The inner mutators used to commit inside seed_from's transaction, so a
    failure part-way left a written registry with no 'seeded' marker — and the
    next attempt re-seeded on top of it."""
    bad = SeedResult(companies=[
        SeedCompany(name="Ollex", units=(1,)),
        SeedCompany(name="   ", units=(1,)),          # empty key -> RepoError
    ], cities=["STO"])
    with pytest.raises(RepoError):
        repo.seed_from(bad)
    assert not repo.is_seeded()
    assert repo.registry().companies == {}


def _one_trip_dataset(repo):
    from buschain.codecs.types import ImportedDataset
    repo.upsert_company("Ollex", None)
    repo.add_unit("ollex", 1)
    repo.add_city("STO")
    repo.add_city("CPH")
    row = RawTripRow(sheet_row=2, values={
        **{c: None for c in ALL_COLUMNS},
        ALL_COLUMNS[0]: "Confirmed",
        ALL_COLUMNS[4]: "2026-04-01", ALL_COLUMNS[5]: "2026-04-02",
        ALL_COLUMNS[6]: "STO", ALL_COLUMNS[7]: "CPH",
        ALL_COLUMNS[13]: "Ollex 1",
    })
    repo.import_dataset(
        ImportedDataset(source_kind="xlsx", rows=(row,), blob=b"fake"), "t.xlsx")


def test_deleting_an_in_use_company_is_refused(repo):
    _one_trip_dataset(repo)
    with pytest.raises(RepoError, match="live trip"):
        repo.delete_company("ollex")
    assert "ollex" in repo.registry().companies


def test_removing_an_in_use_city_is_refused(repo):
    _one_trip_dataset(repo)
    with pytest.raises(RepoError, match="live trip"):
        repo.remove_city("CPH")
    repo.add_city("OSL")
    repo.remove_city("OSL")                            # unused: fine
    with pytest.raises(RepoError, match="Unknown city"):
        repo.remove_city("OSL")                        # gone: no silent no-op


def test_manual_batch_failure_writes_no_rows(repo):
    """add_manual_trips used a bare trailing commit: a failure mid-batch left
    the earlier inserts pending, to be committed by whatever came next."""
    _one_trip_dataset(repo)
    rows = [{"A": "Confirmed"}, {"A": object()}]       # second is unserializable
    with pytest.raises(TypeError):
        repo.add_manual_trips(rows)
    report, _, _ = repo.load_state()
    assert len(report.trips) + len(report.non_operable) == 1


# -- API trust boundary ------------------------------------------------------

@pytest.fixture
def client(tmp_path, workbook_bytes):
    app = create_app(tmp_path / "hardening-api.db")
    with TestClient(app) as c:
        res = c.post("/api/import", files={
            "file": ("t.xlsx", workbook_bytes, "application/octet-stream")})
        assert res.status_code == 200, res.text
        assert c.post("/api/registry/seed").status_code == 200
        # As in test_api.py's fixture: these assert trust-boundary behaviour,
        # not where today falls in the reference season.
        assert c.post("/api/settings", json={"horizon": "all"}).status_code == 200
        yield c


def test_corrupt_upload_is_422(client):
    res = client.post("/api/import", files={
        "file": ("t.xlsx", b"garbage bytes", "application/octet-stream")})
    assert res.status_code == 422
    assert "Not a readable" in res.json()["detail"]


def test_manual_trip_with_unknown_column_is_422(client):
    res = client.post("/api/trips", json={"values": {"A": "Confirmed", "Z": "x"}})
    assert res.status_code == 422
    assert "Z" in res.json()["detail"]


def test_bad_trip_id_with_superscript_is_422(client):
    assert client.post("/api/trips/sheet:²/status",
                       json={"status": "Canceled"}).status_code == 422


def test_csv_import_clears_stale_sync_warnings(client):
    """Fleet-sync warnings describe the active dataset; a csv import replaces
    it, and the previous workbook's warnings must not survive on screen."""
    # Manufacture a warning: a unit with live trips that the sheet then drops
    # is over-engineering here — write one directly and import a csv over it.
    csv_text = ",".join(COLUMN_TITLES[c] for c in ALL_COLUMNS) + "\n"
    app_repo = client.app.state.repo
    app_repo.set_sync_warnings(["stale warning from the old workbook"])
    res = client.post("/api/import", files={
        "file": ("t.csv", csv_text.encode(), "text/csv")})
    assert res.status_code == 200, res.text
    assert client.get("/api/state").json()["sync_warnings"] == []


def test_export_grows_autofilter_over_manual_rows(client):
    """End-to-end: a manually added trip lands inside the sheet's active
    autoFilter range, not invisibly below it."""
    res = client.post("/api/trips", json={"values": {
        "A": "Confirmed", "E": "2026-04-01", "F": "2026-04-02",
        "G": "STO", "H": "CPH"}})
    assert res.status_code == 200, res.text
    out = client.get("/api/export.xlsx").content
    with zipfile.ZipFile(io.BytesIO(out)) as zf:
        sheet = zf.read("xl/worksheets/sheet1.xml")
    m = re.search(rb'<autoFilter ref="A1:[A-Z]+(\d+)"', sheet)
    assert m is not None
    # The reference sheet's data (and its filter) end at row 1671; the manual
    # trip appends at 1672, and the filter must now cover it.
    assert int(m.group(1)) >= 1672
