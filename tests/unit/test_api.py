"""End-to-end API loop against the real workbook: import -> seed -> accept ->
cancel -> worklist -> export."""
import io
import zipfile
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from buschain.engine import graph
from buschain.web import api
from buschain.web.app import create_app

# What every test in this file takes "today" to be. The reference workbook's
# season is fixed — its trips run late August to October 2026 — so against the
# real clock these tests were a measurement of where the calendar happened to
# stand: once the season had passed, three reclaim tests failed outright and
# six more skipped, on a checkout nobody had touched. The web layer reads the
# clock through api._today so that it can be pinned here, which is the same
# move that keeps the engine's own tests stable (they pass `as_of`).
#
# 2026-08-01 because it is the `as_of` the measurements in docs/engine.md §9
# were taken on: the whole season still lies ahead, most of it more than a
# month out and some of it inside one, so both sides of every date rule
# (21-day notice, 30-day horizon) have trips to bite on.
TODAY = date(2026, 8, 1)


@pytest.fixture
def client(tmp_path, workbook_bytes, monkeypatch):
    monkeypatch.setattr(api, "_today", lambda: TODAY)
    app = create_app(tmp_path / "api.db")
    with TestClient(app) as c:
        res = c.post("/api/import", files={
            "file": ("LDC_PLANNER__test.xlsx", workbook_bytes,
                     "application/octet-stream"),
        })
        assert res.status_code == 200, res.text
        assert res.json()["rows"] == 1670
        assert c.post("/api/registry/seed").status_code == 200
        # Plan ahead: all. These tests pin what the ENGINE does, and the
        # shipped default ("30", a month from today) would make almost every
        # one of them a measurement of where today happens to fall in a fixed
        # season — the reference workbook ends Oct 2026, so they would go from
        # asserting 113 covered trips to asserting 0, and then keep changing
        # answer as real time passes. The horizon has its own tests below,
        # which narrow it again against the pinned TODAY above.
        assert c.post("/api/settings", json={"horizon": "all"}).status_code == 200
        yield c


def test_state_shape(client):
    state = client.get("/api/state").json()
    assert state["seeded"] is True
    assert state["dataset"]["source_kind"] == "xlsx"
    assert len(state["trips"]) + len(state["non_operable"]) == 1670
    assert state["engine"]["suggestions_by_trip"]
    # The Fleets column seeds every charter, so a clean sheet seeds silently.
    assert state["seed_warnings"] == []
    assert state["sync_warnings"] == []


def test_state_serves_window_rules(client):
    """The board explains rule 1 rather than mirroring it, so /state has to
    carry the window — including the pair table, which is the only way the UI
    can say how many of a gap's free days an empty run spends driving. Losing
    this key does not degrade the board, it breaks it."""
    rules = client.get("/api/state").json()["rules"]
    assert rules == graph.window_rules()
    assert rules["min_gap_days"] == graph.MIN_GAP_DAYS
    assert rules["max_rest_days"] == graph.MAX_REST_DAYS
    # Unordered in the engine (a frozenset key), so it has to arrive in a fixed
    # order or the client's pair lookup depends on how the set happened to hash.
    assert rules["near_pairs"] == sorted(sorted(p) for p in rules["near_pairs"])
    assert graph.min_gap_days(*rules["near_pairs"][0]) == rules["near_gap_days"]


def test_state_carries_parked_windows(client):
    """The parked list must survive serialization with ids in the same
    'origin:key' form every other engine output uses — the panel dies
    silently without them."""
    parked = client.get("/api/state").json()["engine"]["parked"]
    assert isinstance(parked, list)
    for w in parked:
        assert {"company", "unit", "after", "before",
                "city", "days", "fits"} <= w.keys()
        assert w["after"].count(":") == 1 and w["days"] > 0
        for f in w["fits"]:
            assert {"demand", "gap_out", "er_out_days",
                    "er_back_days", "chains"} <= f.keys()


def test_direct_assign_without_supply(client):
    """The board's manual control assigns a numbered unit with no supply trip.

    Nothing about it is engine-gated — that is the point, since the engine
    proposes nothing at all on a fully-planned dataset — so the rules are
    enforced downstream instead: the assignment lands, and an overlap it
    creates is reported by the integrity check rather than refused here.
    """
    state = client.get("/api/state").json()
    busy = [t for t in state["trips"]
            if t["assignment"] and t["assignment"]["unit"] is not None]
    free = [t for t in state["trips"]
            if not t["assignment"] and t["status"] != "Canceled"]
    # Any assigned trip with an uncovered one overlapping it — which pair the
    # reference data offers is not the point, only that one exists.
    assigned, clash = next((a, c) for a in busy for c in free
                           if c["start"] <= a["end"] and c["end"] >= a["start"])
    unit = assigned["assignment"]

    before = len(state["engine"]["integrity"])
    res = client.post("/api/accept", json={
        "demand": clash["id"], "company": unit["company"], "unit": unit["unit"],
        "supply": None,
    })
    assert res.status_code == 200, res.text

    state = client.get("/api/state").json()
    now = next(t for t in state["trips"] if t["id"] == clash["id"])
    assert now["assignment"] == unit
    # supply None is the "no chain provenance" shape, so no connector is drawn
    # and broken_chains has nothing to hang off.
    assert {"supply": None, "demand": clash["id"]} in state["accepted_links"]
    assert len(state["engine"]["integrity"]) > before

    # Reversible: it went through repo.undoable like every other mutation.
    assert client.post("/api/undo", json={}).status_code == 200
    state = client.get("/api/state").json()
    assert next(t for t in state["trips"] if t["id"] == clash["id"])["assignment"] is None
    assert len(state["engine"]["integrity"]) == before


def test_accept_flow_and_broken_chain(client):
    state = client.get("/api/state").json()
    # find a chain suggestion whose supply is Pending so canceling breaks it
    chosen = None
    for suggestions in state["engine"]["suggestions_by_trip"].values():
        for s in suggestions:
            if s["kind"] == "chain" and s["at_risk"]:
                chosen = s
                break
        if chosen:
            break
    assert chosen, "real data should offer at least one at-risk chain suggestion"

    res = client.post("/api/accept", json={
        "demand": chosen["demand"], "company": chosen["company"],
        "unit": chosen["unit"], "supply": chosen["supply"],
    })
    assert res.status_code == 200, res.text

    # The link is what the board draws a chain connector from, so it has to
    # survive the round trip to the client, not just into the engine.
    state = client.get("/api/state").json()
    assert {"supply": chosen["supply"], "demand": chosen["demand"]} \
        in state["accepted_links"]

    pending_dep = chosen["at_risk"][0]
    res = client.post(f"/api/trips/{pending_dep}/status", json={"status": "Canceled"})
    assert res.status_code == 200

    state = client.get("/api/state").json()
    broken = state["engine"]["broken_chains"]
    assert any(b["assignment"] == chosen["demand"] for b in broken)

    res = client.post("/api/unassign", json={"trip": chosen["demand"]})
    assert res.status_code == 200
    state = client.get("/api/state").json()
    assert not any(b["assignment"] == chosen["demand"]
                   for b in state["engine"]["broken_chains"])


def test_manual_trip_validated(client):
    res = client.post("/api/trips", json={"values": {
        "A": "Pending", "E": "2026-09-01", "F": "2026-09-02",
        "G": "OSL", "H": "STO",
    }})
    assert res.status_code == 200
    tid = res.json()["id"]
    state = client.get("/api/state").json()
    assert any(t["id"] == tid for t in state["trips"])

    # junk city -> lands in non-operable with a reason, never rejected silently
    res = client.post("/api/trips", json={"values": {
        "A": "Pending", "E": "2026-09-01", "F": "2026-09-02",
        "G": "OSL", "H": "ferry Turku-STO",
    }})
    tid2 = res.json()["id"]
    state = client.get("/api/state").json()
    bad = next(r for r in state["non_operable"] if r["id"] == tid2)
    assert any("Arrival" in reason for reason in bad["reasons"])


def test_manual_trip_carries_the_passthrough_columns(client):
    """Notes, rate and nationality typed on the form reach the exported row.

    They are free text the contract never validates, so nothing else in the
    system would notice them going missing — the sheet is the only place they
    are ever read again."""
    res = client.post("/api/trips", json={"values": {
        "A": "Pending", "D": "-Vilneda", "E": "2026-09-01", "F": "2026-09-02",
        "G": "OSL", "H": "STO", "I": "South Korean", "K": "650EUR",
        "O": "confirmed by phone",
    }})
    assert res.status_code == 200
    tid = res.json()["id"]
    trip = next(t for t in client.get("/api/state").json()["trips"]
                if t["id"] == tid)
    assert trip["values"]["P Notes"] == "-Vilneda"
    assert trip["values"]["C Notes"] == "confirmed by phone"
    assert trip["values"]["Quoted rate"] == "650EUR"

    import openpyxl
    exported = client.get("/api/export.xlsx").content
    ws = openpyxl.load_workbook(io.BytesIO(exported), data_only=True)["Capitals 2026"]
    row = next(r for r in ws.iter_rows(min_col=1, max_col=15)
               if r[3].value == "-Vilneda" and r[0].value == "Pending")
    assert [row[i].value for i in (8, 10, 14)] == [
        "South Korean", "650EUR", "confirmed by phone"]


def test_paste_adds_rows_and_undoes_as_one(client):
    """A pasted batch is one action. Forty undo steps would also flush every
    other action out of a 50-deep stack."""
    before = len(client.get("/api/state").json()["trips"])
    text = (
        "Confirmed\t22198\t\tnote one\t1-Sep-26\t2-Sep-26\tOSL\tSTO\n"
        "Pending\t22199\t\t\t3-Sep-26\t4-Sep-26\tSTO\tCPH\t\t\t\t45\t\t\tnote two\n"
    )
    res = client.post("/api/trips/paste", json={"text": text})
    assert res.status_code == 200, res.text
    ids = res.json()["ids"]
    assert len(ids) == 2

    state = client.get("/api/state").json()
    added = {t["id"]: t for t in state["trips"] if t["id"] in ids}
    assert len(added) == 2, "both rows should have passed the contract"
    first = added[ids[0]]
    assert (first["start"], first["end"]) == ("2026-09-01", "2026-09-02")
    assert first["values"]["P Notes"] == "note one"
    assert added[ids[1]]["values"]["C Notes"] == "note two"
    assert added[ids[1]]["pax"] == 45

    assert state["undo"]["label"] == "paste 2 trips"
    client.post("/api/undo")
    after = client.get("/api/state").json()
    assert len(after["trips"]) == before
    assert not any(t["id"] in ids for t in after["trips"])


def test_manual_row_is_numbered_for_the_row_it_will_export_to(client):
    """The label is a promise about the exported file, so it is checked against
    the exported file. Its own key is a SQLite rowid that counts every row of
    every dataset ever imported — 13361 on the ninth import.

    Bare number, no 'new' word: the Row column shows the number alone, and the
    noun belongs only to prose like the undo label below.
    """
    res = client.post("/api/trips", json={"values": {
        "A": "Pending", "E": "2026-09-01", "F": "2026-09-02",
        "G": "OSL", "H": "STO", "B": "99001",
    }})
    tid = res.json()["id"]
    label = client.get("/api/state").json()["labels"][tid]
    assert label == "1672"
    assert client.get("/api/state").json()["undo"]["label"] == "add trip"

    import openpyxl
    exported = client.get("/api/export.xlsx").content
    ws = openpyxl.load_workbook(io.BytesIO(exported), data_only=True)["Capitals 2026"]
    assert ws.cell(row=int(label), column=2).value == 99001

    # Undo labels come from the same call, or an entry would name a row the
    # table numbers differently.
    client.delete(f"/api/trips/{tid}")
    assert client.get("/api/state").json()["undo"]["label"] == "delete row 1672"


def test_paste_with_a_header_maps_columns_by_title(client):
    """A sheet with hidden columns puts fewer fields on the clipboard than the
    layout has; the header is what keeps the rest from shifting left."""
    text = ("Status\tProject\tStart date\tEnd date\tDeparture\tArrival\tPax\t"
            "Company name\n"
            "Confirmed\t99002\t1-Sep-26\t2-Sep-26\tOSL\tSTO\t49\tVilneda 1")
    res = client.post("/api/trips/paste", json={"text": text})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["by_header"] is True
    assert "Nationality" not in body["columns"]

    trip = next(t for t in client.get("/api/state").json()["trips"]
                if t["id"] == body["ids"][0])
    assert trip["pax"] == 49
    assert trip["assignment"] == {"company": "vilneda", "unit": 1}
    assert trip["values"].get("Nationality", "") == ""


def test_paste_without_a_header_is_read_by_position(client):
    res = client.post("/api/trips/paste", json={
        "text": "Confirmed\t99003\t\t\t1-Sep-26\t2-Sep-26\tOSL\tSTO"})
    body = res.json()
    assert body["by_header"] is False
    assert body["columns"] == ["Status", "Project", "Bus Preference", "P Notes",
                               "Start date", "End date", "Departure", "Arrival"]


def test_paste_with_declared_columns_maps_a_hidden_column_copy(client):
    """No header, data rows only — the picker's declaration does the mapping.
    This is the planner's actual workflow; the header path is the bonus."""
    res = client.post("/api/trips/paste", json={
        "text": "Confirmed\t99004\t1-Sep-26\t2-Sep-26\tOSL\tSTO\t49\tVilneda 1",
        "columns": ["Status", "Project", "Start date", "End date",
                    "Departure", "Arrival", "Pax", "Company name"]})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["by_header"] is False
    assert body["overflow_rows"] == 0

    trip = next(t for t in client.get("/api/state").json()["trips"]
                if t["id"] == body["ids"][0])
    assert trip["pax"] == 49
    assert trip["assignment"] == {"company": "vilneda", "unit": 1}
    assert trip["values"].get("Nationality", "") == ""


def test_paste_with_a_bad_declared_column_is_refused(client):
    res = client.post("/api/trips/paste", json={
        "text": "Confirmed\t1-Sep-26", "columns": ["Status", "Bogus"]})
    assert res.status_code == 422
    assert "Bogus" in res.json()["detail"]


def test_paste_keeps_rows_the_contract_rejects(client):
    """Same rule as import (AT2): a row is never silently dropped. A junk city
    lands in non-operable with its reason, where it can be fixed in place."""
    res = client.post("/api/trips/paste", json={
        "text": "Confirmed\t\t\t\t1-Sep-26\t2-Sep-26\tOSL\tferry Turku-STO"})
    tid = res.json()["ids"][0]
    bad = next(r for r in client.get("/api/state").json()["non_operable"]
               if r["id"] == tid)
    assert any("Arrival" in reason for reason in bad["reasons"])


def test_paste_leaves_an_ambiguous_date_for_the_contract_to_report(client):
    """Not guessed at in the codec: '04/05/2026' is 4 May or 5 April depending
    on the exporting locale, and a wrong date that validates is worse."""
    res = client.post("/api/trips/paste", json={
        "text": "Confirmed\t\t\t\t04/05/2026\t06/05/2026\tOSL\tSTO"})
    tid = res.json()["ids"][0]
    bad = next(r for r in client.get("/api/state").json()["non_operable"]
               if r["id"] == tid)
    assert any("'04/05/2026' is not a valid date" in r for r in bad["reasons"])


def test_paste_of_nothing_is_refused(client):
    res = client.post("/api/trips/paste", json={"text": "\n \n"})
    assert res.status_code == 422
    assert client.get("/api/state").json()["undo"] is None


def test_exports(client, workbook_bytes):
    """No trip changes, so the trip sheet comes back untouched. The workbook
    does gain its Nationality/OP columns on this first export — the sheet is
    where those lists live — and is a fixed point from then on: re-importing
    the export and exporting again returns the same bytes."""
    xlsx = client.get("/api/export.xlsx")
    assert xlsx.status_code == 200
    with zipfile.ZipFile(io.BytesIO(workbook_bytes)) as before, \
            zipfile.ZipFile(io.BytesIO(xlsx.content)) as after:
        assert [n for n in before.namelist()
                if before.read(n) != after.read(n)] == ["xl/worksheets/sheet2.xml"]

    res = client.post("/api/import", files={
        "file": ("round2.xlsx", xlsx.content, "application/octet-stream")})
    assert res.status_code == 200, res.text
    assert client.get("/api/export.xlsx").content == xlsx.content

    csv_res = client.get("/api/export.csv")
    assert csv_res.status_code == 200
    assert csv_res.text.startswith("Status,Project")


def test_csv_import_and_exports(tmp_path):
    app = create_app(tmp_path / "csv.db")
    with TestClient(app) as c:
        header = ("Status,Project,Bus Preference,P Notes,Start date,End date,"
                  "Departure,Arrival,Nationality,OP,Quoted rate,Pax,ER,"
                  "Company name,C Notes")
        body = "Confirmed,1,,,2026-04-01,2026-04-02,STO,CPH,,,,49,,,note"
        res = c.post("/api/import", files={
            "file": ("trips.csv", (header + "\n" + body).encode(), "text/csv"),
        })
        assert res.status_code == 200, res.text
        c.post("/api/registry/cities", json={"code": "STO"})
        c.post("/api/registry/cities", json={"code": "CPH"})

        csv_out = c.get("/api/export.csv")
        assert body.split(",")[:10] == csv_out.text.splitlines()[1].split(",")[:10]

        xlsx_out = c.get("/api/export.xlsx")
        assert xlsx_out.status_code == 200
        with zipfile.ZipFile(io.BytesIO(xlsx_out.content)) as zf:
            assert any(n.startswith("xl/worksheets/") for n in zf.namelist())


def test_edit_free_text_cell_roundtrips_to_export(client, workbook_bytes):
    """A passthrough column is editable and its edit reaches the xlsx, without
    disturbing any other cell."""
    state = client.get("/api/state").json()
    trip = state["trips"][0]
    original_note = trip["values"]["C Notes"]
    assert original_note != "reviewed 2026-07-25"

    res = client.post(f"/api/trips/{trip['id']}/cell",
                      json={"column": "C Notes", "value": "reviewed 2026-07-25"})
    assert res.status_code == 200, res.text

    state = client.get("/api/state").json()
    edited = next(t for t in state["trips"] if t["id"] == trip["id"])
    assert edited["values"]["C Notes"] == "reviewed 2026-07-25"
    # marked as an in-system edit so the UI can badge the cell
    assert state["overrides"][trip["id"]]["C Notes"] == "reviewed 2026-07-25"
    # a free-text edit changes nothing the contract reads
    assert len(state["trips"]) == len(client.get("/api/state").json()["trips"])

    exported = client.get("/api/export.xlsx").content
    import openpyxl
    ws_a = openpyxl.load_workbook(io.BytesIO(workbook_bytes), data_only=True)["Capitals 2026"]
    ws_b = openpyxl.load_workbook(io.BytesIO(exported), data_only=True)["Capitals 2026"]
    diffs = [(a.coordinate, a.value, b.value)
             for ra, rb in zip(ws_a.iter_rows(), ws_b.iter_rows())
             for a, b in zip(ra, rb) if a.value != b.value]
    assert diffs == [("O2", original_note, "reviewed 2026-07-25")]


def test_edit_can_repair_a_non_operable_row(client):
    """Editing the cell a reason points at moves the row into the operable set."""
    state = client.get("/api/state").json()
    bad = next(r for r in state["non_operable"]
               if any(x.startswith("Arrival") for x in r["reasons"]))
    before = len(state["trips"])

    res = client.post(f"/api/trips/{bad['id']}/cell",
                      json={"column": "Arrival", "value": "OSL"})
    assert res.status_code == 200, res.text

    state = client.get("/api/state").json()
    assert not any(r["id"] == bad["id"] for r in state["non_operable"])
    repaired = next(t for t in state["trips"] if t["id"] == bad["id"])
    assert repaired["arr"] == "OSL"
    assert len(state["trips"]) == before + 1


def test_edited_date_lands_as_a_real_date_not_text(client):
    """An edited date must be a native date cell, or the sheet's date grid and
    Excel's own formatting stop seeing it as one."""
    state = client.get("/api/state").json()
    trip = state["trips"][0]
    res = client.post(f"/api/trips/{trip['id']}/cell",
                      json={"column": "End date", "value": "2026-04-09"})
    assert res.status_code == 200, res.text

    exported = client.get("/api/export.xlsx").content
    import openpyxl
    from datetime import datetime
    ws = openpyxl.load_workbook(io.BytesIO(exported), data_only=True)["Capitals 2026"]
    assert ws["F2"].value == datetime(2026, 4, 9)


def test_editing_a_cell_back_to_its_imported_value_clears_the_edit(client):
    """No override left behind, so the export does not splice an unchanged cell."""
    state = client.get("/api/state").json()
    trip = state["trips"][0]
    original = trip["values"]["Nationality"]

    client.post(f"/api/trips/{trip['id']}/cell",
                json={"column": "Nationality", "value": "Swedish"})
    assert client.get("/api/state").json()["overrides"][trip["id"]]

    client.post(f"/api/trips/{trip['id']}/cell",
                json={"column": "Nationality", "value": original})
    assert trip["id"] not in client.get("/api/state").json()["overrides"]


def test_unknown_column_rejected(client):
    state = client.get("/api/state").json()
    trip = state["trips"][0]
    res = client.post(f"/api/trips/{trip['id']}/cell",
                      json={"column": "Y-N", "value": "Y"})
    # 422: a title outside the contract layout is a malformed request, not a
    # state conflict — the same mapping every other shape error gets.
    assert res.status_code == 422


def test_bus_preference_edit_redirects_routing(client):
    """The Bus Preference dropdown's two forms — 'X' and 'NO X' — must reach
    the suggester, not just the contract."""
    state = client.get("/api/state").json()
    by_trip = state["engine"]["suggestions_by_trip"]
    trip = next(t for t in state["trips"]
                if not t["yn"] and not t["assignment"] and by_trip.get(t["id"]))
    company_key = by_trip[trip["id"]][0]["company"]
    company_name = next(c["name"] for c in state["registry"]["companies"]
                        if c["key"] == company_key)

    def candidates():
        s = client.get("/api/state").json()
        return [c["company"] for c in s["engine"]["suggestions_by_trip"].get(trip["id"], [])]

    assert company_key in candidates()

    # 'NO X' — that company may never serve this trip
    client.post(f"/api/trips/{trip['id']}/cell",
                json={"column": "Bus Preference", "value": f"NO {company_name}"})
    s = client.get("/api/state").json()
    edited = next(t for t in s["trips"] if t["id"] == trip["id"])
    assert (edited["yn"], edited["constraint_company"]) == ("N", company_key)
    assert company_key not in candidates()

    # 'X' — only that company may serve it
    client.post(f"/api/trips/{trip['id']}/cell",
                json={"column": "Bus Preference", "value": company_name})
    s = client.get("/api/state").json()
    edited = next(t for t in s["trips"] if t["id"] == trip["id"])
    assert (edited["yn"], edited["constraint_company"]) == ("Y", company_key)
    assert set(candidates()) == {company_key}


def test_bus_preference_off_the_dropdown_is_reported_not_swallowed(client):
    """The dropdown only offers registry companies, but the API stays open so
    imported free text still surfaces as a reason rather than being refused."""
    state = client.get("/api/state").json()
    trip = state["trips"][0]
    res = client.post(f"/api/trips/{trip['id']}/cell",
                      json={"column": "Bus Preference", "value": "Some Other Firm"})
    assert res.status_code == 200

    state = client.get("/api/state").json()
    row = next(r for r in state["non_operable"] if r["id"] == trip["id"])
    assert any("Bus Preference" in reason for reason in row["reasons"])


def test_pick_lists_seed_from_the_data_and_are_prunable(client):
    """Nationality/OP dropdowns come from the registry, seeded on first import
    so they work immediately, and prunable so typos can be merged away."""
    state = client.get("/api/state").json()
    options = state["registry"]["options"]
    assert set(options) == {"Nationality", "OP"}
    # the reference data's near-duplicates are all present, which is the point:
    # they exist to be merged by hand
    assert {"South Korean", "South Korea", "Korea"} <= set(options["Nationality"])
    assert {"Leslie", "Lesile"} <= set(options["OP"])

    res = client.request("DELETE", "/api/registry/options/OP/Lesile")
    assert res.status_code == 200
    state = client.get("/api/state").json()
    assert "Lesile" not in state["registry"]["options"]["OP"]
    # pruning the list is not validation: no row moved out of the operable set
    assert len(state["trips"]) == len(client.get("/api/state").json()["trips"])

    assert client.post("/api/registry/options",
                       json={"column": "OP", "value": "Nadia"}).status_code == 200
    assert "Nadia" in client.get("/api/state").json()["registry"]["options"]["OP"]


def test_pick_list_rejects_a_column_without_one(client):
    res = client.post("/api/registry/options",
                      json={"column": "C Notes", "value": "x"})
    assert res.status_code == 422


def test_reimport_does_not_resurrect_pruned_options(client, workbook_bytes):
    # Read first, as the UI does: the list fills on read, and a delete only
    # means anything once there is a list to delete from.
    assert "Lesile" in client.get("/api/state").json()["registry"]["options"]["OP"]
    client.request("DELETE", "/api/registry/options/OP/Lesile")
    res = client.post("/api/import", files={
        "file": ("again.xlsx", workbook_bytes, "application/octet-stream")})
    assert res.status_code == 200
    assert "Lesile" not in client.get("/api/state").json()["registry"]["options"]["OP"]


def test_company_note_is_editable(client):
    state = client.get("/api/state").json()
    company = state["registry"]["companies"][0]
    res = client.post("/api/registry/companies", json={
        "name": company["name"], "fleet_size": company["fleet_size"],
        "note": "prefers 2-day turnarounds",
    })
    assert res.status_code == 200
    state = client.get("/api/state").json()
    updated = next(c for c in state["registry"]["companies"] if c["key"] == company["key"])
    assert updated["note"] == "prefers 2-day turnarounds"
    assert updated["fleet_size"] == company["fleet_size"]   # not clobbered
    assert updated["units"] == company["units"]


def _plan(client):
    return client.get("/api/state").json()["engine"]["plan"]


def _link_count(plan):
    return sum(len(c["links"]) for c in plan["chains"])


def test_plan_is_offered_and_settings_replan(client):
    state = client.get("/api/state").json()
    assert state["settings"] == {
        "mode": "manual", "objective": "coverage", "reposition": True,
        "horizon": "all"}          # pinned by the fixture, not the default

    coverage = _plan(client)
    assert _link_count(coverage) > len(state["engine"]["suggestions_by_trip"]), \
        "a multi-hop plan must beat the one-hop candidate list"

    assert client.post("/api/settings", json={"objective": "idle"}).status_code == 200
    idle = _plan(client)
    assert idle["objective"] == "idle"
    # Coverage covers more; idle spends less to do less. The two used to be told
    # apart by idle_days too, but once repositioning existed both keys had to
    # lead with gap ascending — which IS the idle-minimizing move — so that
    # number converged and the empty-run count became what separates them.
    assert _link_count(idle) < _link_count(coverage)
    assert idle["idle_days"] <= coverage["idle_days"]
    runs = lambda p: sum(l["reposition"] for c in p["chains"] for l in c["links"])
    assert runs(idle) <= runs(coverage)

    assert client.post("/api/settings", json={"objective": "nonsense"}).status_code == 422
    assert client.post("/api/settings", json={"mode": "nonsense"}).status_code == 422


def test_reposition_toggle_governs_the_whole_engine(client):
    """Off must reach the one-hop candidates too, not just the plan.

    A ghost slot the board offers while the planner refuses to chain it is the
    same setting disagreeing with itself, so both surfaces are asserted here.
    """
    before = client.get("/api/state").json()["engine"]
    assert any(l["reposition"] for c in before["plan"]["chains"] for l in c["links"])
    assert any(s["reposition"] for ss in before["suggestions_by_trip"].values() for s in ss)

    r = client.post("/api/settings", json={"reposition": False})
    assert r.status_code == 200 and r.json()["settings"]["reposition"] is False

    after = client.get("/api/state").json()["engine"]
    assert not any(l["reposition"] for c in after["plan"]["chains"] for l in c["links"])
    assert not any(s["reposition"] for ss in after["suggestions_by_trip"].values() for s in ss)
    # The point of the toggle: same-city work survives, empty runs do not.
    assert _link_count(after["plan"]) < _link_count(before["plan"])

    # Persisted, and each setting patches independently of the others.
    assert client.post("/api/settings", json={"mode": "auto"}).status_code == 200
    assert client.get("/api/state").json()["settings"] == {
        "mode": "auto", "objective": "coverage", "reposition": False,
        "horizon": "all"}          # pinned by the fixture, not the default
    assert client.post("/api/settings", json={"reposition": True}).status_code == 200
    assert _link_count(_plan(client)) == _link_count(before["plan"])


def test_provisional_links_never_reach_the_export(client):
    """The load-bearing guarantee: the plan is derived state, so proposing 50-odd
    assignments must not move a single byte of the workbook."""
    baseline = client.get("/api/export.xlsx").content
    assert _link_count(_plan(client)) > 0

    for patch in ({"mode": "auto"}, {"objective": "idle"}, {"objective": "coverage"},
                  {"reposition": False}, {"reposition": True}):
        assert client.post("/api/settings", json=patch).status_code == 200
        assert client.get("/api/export.xlsx").content == baseline, \
            f"planning under {patch} changed the export"


def test_accept_chain_is_atomic_and_in_order(client):
    before = client.get("/api/state").json()["engine"]
    chain = next(c for c in before["plan"]["chains"] if len(c["links"]) > 1)
    demands = [l["demand"] for l in chain["links"]]

    res = client.post("/api/accept-chain",
                      json={"company": chain["company"], "unit": chain["unit"]})
    assert res.status_code == 200, res.text
    assert res.json()["accepted"] == len(demands)

    state = client.get("/api/state").json()
    assigned = {t["id"]: t["assignment"] for t in state["trips"] if t["assignment"]}
    for demand in demands:
        assert assigned[demand] == {"company": chain["company"], "unit": chain["unit"]}
    # A chain writes several trips onto one bus, so it is the one thing that
    # could break unit integrity. It must not add a single VIOLATION. An
    # accepted empty run does add a finding, of kind "reposition" — that is the
    # record of a move someone approved, listed like explicit_er rather than
    # reported as damage, and counting it here would forbid the feature.
    violations = lambda e: [f for f in e["integrity"]
                            if f["kind"] not in ("explicit_er", "reposition")]
    assert len(violations(state["engine"])) == len(violations(before))
    accepted_runs = [f for f in state["engine"]["integrity"]
                     if f["kind"] == "reposition"]
    assert len(accepted_runs) == sum(l["reposition"] for l in chain["links"])

    # Re-posting must never re-commit what was just committed. It no longer
    # 409s on this unit: the endpoint takes (company, unit) and serves whatever
    # the freshly recomputed plan proposes, and since repositioning exists the
    # bus usually has a NEXT chain waiting off its new tail. So the property to
    # hold is disjointness, not refusal.
    again = client.post("/api/accept-chain",
                        json={"company": chain["company"], "unit": chain["unit"]})
    assert again.status_code in (200, 409)
    if again.status_code == 200:
        after = client.get("/api/state").json()
        still = {t["id"]: t["assignment"] for t in after["trips"] if t["assignment"]}
        for demand in demands:
            assert still[demand] == {"company": chain["company"], "unit": chain["unit"]}, \
                "the first chain's trips must not be rewritten by the second"

    # A unit the plan proposes nothing for is what actually 409s.
    assert client.post("/api/accept-chain",
                       json={"company": chain["company"], "unit": 999}
                       ).status_code == 409


def test_later_links_cannot_be_accepted_out_of_order(client):
    chain = next(c for c in _plan(client)["chains"] if len(c["links"]) > 1)
    second = chain["links"][1]
    res = client.post("/api/accept", json={
        "demand": second["demand"], "company": second["company"],
        "unit": second["unit"], "supply": second["supply"],
    })
    assert res.status_code == 409
    assert "earlier link" in res.json()["detail"]


def test_accept_plan_commits_everything_then_empties(client):
    plan = _plan(client)
    expected = _link_count(plan)
    before = len([t for t in client.get("/api/state").json()["trips"]
                  if t["status"] != "Canceled" and not t["assignment"]])

    res = client.post("/api/accept-plan", json={})
    assert res.status_code == 200, res.text
    assert res.json()["accepted"] == expected

    state = client.get("/api/state").json()
    after = len([t for t in state["trips"]
                 if t["status"] != "Canceled" and not t["assignment"]])
    assert before - after == expected
    # Committed trips are supply now, so the fleet has nothing left to re-plan.
    assert _link_count(state["engine"]["plan"]) == 0
    assert client.post("/api/accept-plan", json={}).status_code == 409


def test_undo_spans_screens_and_reverses_one_action_at_a_time(client):
    """The Data tab's edit and the Suggestions tab's accept share one history,
    so undo walks back through both in the order they happened."""
    state = client.get("/api/state").json()
    assert state["undo"] is None

    trip = state["trips"][0]
    assert client.post(f"/api/trips/{trip['id']}/cell",
                       json={"column": "C Notes", "value": "check with Tatjana"}
                       ).status_code == 200
    state = client.get("/api/state").json()
    assert state["undo"]["label"].startswith("edit C Notes on row ")
    assert state["undo"]["depth"] == 1

    chosen = next(s for ss in state["engine"]["suggestions_by_trip"].values()
                  for s in ss if s["kind"] == "chain")
    assert client.post("/api/accept", json={
        "demand": chosen["demand"], "company": chosen["company"],
        "unit": chosen["unit"], "supply": chosen["supply"],
    }).status_code == 200
    state = client.get("/api/state").json()
    assert state["undo"]["label"].startswith("assign row ")
    assert state["undo"]["depth"] == 2

    # Back one: the assignment goes, the earlier edit stays.
    assert client.post("/api/undo").json()["label"].startswith("assign row ")
    state = client.get("/api/state").json()
    assert state["accepted_links"] == []
    assert next(t for t in state["trips"]
                if t["id"] == chosen["demand"])["assignment"] is None
    edited = next(t for t in state["trips"] if t["id"] == trip["id"])
    assert edited["values"]["C Notes"] == "check with Tatjana"

    # Back again: the edit goes too, and the history is empty.
    client.post("/api/undo")
    state = client.get("/api/state").json()
    assert state["undo"] is None
    assert next(t for t in state["trips"]
                if t["id"] == trip["id"])["values"]["C Notes"] == \
        trip["values"]["C Notes"]
    assert client.post("/api/undo").status_code == 409


def test_delete_row_and_undo(client):
    state = client.get("/api/state").json()
    before = len(state["trips"]) + len(state["non_operable"])
    trip = state["trips"][0]

    assert client.delete(f"/api/trips/{trip['id']}").status_code == 200
    state = client.get("/api/state").json()
    assert len(state["trips"]) + len(state["non_operable"]) == before - 1
    assert not any(t["id"] == trip["id"] for t in state["trips"])
    assert state["undo"]["label"] == f"delete row {trip['id'].split(':')[1]}"
    # Gone from the working set means gone from the API surface too.
    assert client.post(f"/api/trips/{trip['id']}/status",
                       json={"status": "Canceled"}).status_code == 409

    client.post("/api/undo")
    state = client.get("/api/state").json()
    assert len(state["trips"]) + len(state["non_operable"]) == before
    assert any(t["id"] == trip["id"] for t in state["trips"])


def test_bulk_delete_spans_both_tables_and_is_one_undo_step(client):
    """Ticked rows from the operable and non-operable tables delete through
    the same endpoint — a row id is a row id — and the batch reverses as one
    undo entry, like paste and for the same stack-depth reason."""
    state = client.get("/api/state").json()
    ids = [t["id"] for t in state["trips"][:2]] + [state["non_operable"][0]["id"]]
    op_before, non_before = len(state["trips"]), len(state["non_operable"])

    res = client.post("/api/trips/delete", json={"ids": ids})
    assert res.status_code == 200, res.text
    state = client.get("/api/state").json()
    assert len(state["trips"]) == op_before - 2
    assert len(state["non_operable"]) == non_before - 1
    assert state["undo"]["label"] == "delete 3 rows"

    # Deleted rows leave the exported sheet too — the batch flows into the
    # same deleted_rows path as single deletes.
    sheet_rows = {int(i.split(":")[1]) for i in ids}
    import openpyxl
    ws = openpyxl.load_workbook(
        io.BytesIO(client.get("/api/export.xlsx").content))["Capitals 2026"]
    for r in sheet_rows:
        assert all(c.value is None for c in ws[r][:15]), f"row {r} still exported"

    client.post("/api/undo")
    state = client.get("/api/state").json()
    assert len(state["trips"]) == op_before
    assert len(state["non_operable"]) == non_before


def test_bulk_delete_is_all_or_nothing(client):
    """One stale id refuses the whole batch — a half-applied delete would
    leave the user diffing the table against what they ticked — and a refused
    action leaves no undo entry to trip over."""
    state = client.get("/api/state").json()
    ids = [state["trips"][0]["id"], "sheet:999999"]
    res = client.post("/api/trips/delete", json={"ids": ids})
    assert res.status_code == 409
    after = client.get("/api/state").json()
    assert len(after["trips"]) == len(state["trips"])
    assert after["undo"] is None


def test_bulk_delete_of_a_single_row_uses_its_label(client):
    trip = client.get("/api/state").json()["trips"][0]
    assert client.post("/api/trips/delete",
                       json={"ids": [trip["id"]]}).status_code == 200
    label = client.get("/api/state").json()["undo"]["label"]
    assert label == f"delete row {trip['id'].split(':')[1]}"
    client.post("/api/undo")


def test_bulk_delete_of_nothing_is_refused(client):
    assert client.post("/api/trips/delete", json={"ids": []}).status_code == 422


def test_pick_list_edits_reach_the_information_sheet(client):
    """The full loop the columns exist for: maintain the list in-app, export,
    re-import, and the sheet is what the list is restored from."""
    import openpyxl

    assert client.post("/api/registry/options",
                       json={"column": "Nationality", "value": "Estonian"}).status_code == 200
    assert client.request(
        "DELETE", "/api/registry/options/Nationality/Korea").status_code == 200

    data = client.get("/api/export.xlsx").content
    ws = openpyxl.load_workbook(io.BytesIO(data), data_only=True)["Information"]
    assert ws["O1"].value == "Nationality" and ws["P1"].value == "OP"
    written = [ws.cell(r, 15).value for r in range(2, ws.max_row + 1)]
    assert "Estonian" in written
    assert "Korea" not in written

    # Re-importing restores the list from the sheet, not from the trip data —
    # where 'Korea' still very much appears.
    res = client.post("/api/import", files={
        "file": ("round2.xlsx", data, "application/octet-stream")})
    assert res.status_code == 200, res.text
    options = client.get("/api/state").json()["registry"]["options"]["Nationality"]
    assert "Estonian" in options and "Korea" not in options


# -- the planning horizon ----------------------------------------------------
# Everything from here down is measured from today's date, which enters the
# system in the web layer — and which the `client` fixture pins to TODAY. These
# tests used to run against the real clock and skip once the reference season
# had passed, which kept the suite green by going dark: from that day on none
# of them ran again. So what were skips are now assertions. With the date
# pinned and the workbook fixed, "no future trip" can only mean something
# broke, and that has to fail loudly rather than be skipped past.

def _uncovered_after(client, iso):
    """Live uncovered trips starting after a date, straight off /state."""
    state = client.get("/api/state").json()
    return [t for t in state["trips"]
            if not t["assignment"] and t["status"] != "Canceled" and t["start"] > iso]


def test_horizon_bounds_everything_the_planner_derives(client):
    """One switch, one answer: the window has to reach the single-hop
    candidates and reachability too, not only the chains. A trip the board
    offers while the planner refuses to consider it is the same setting
    disagreeing with itself."""
    # The fixture pins "all", so this is the unbounded engine.
    wide = client.get("/api/state").json()
    assert wide["horizon_through"] is None
    assert wide["engine"]["beyond_horizon"] == 0
    assert wide["engine"]["plan"]["covered"], "the unbounded plan covers trips"

    cutoff = (TODAY + timedelta(days=30)).isoformat()
    outside = {t["id"] for t in _uncovered_after(client, cutoff)}
    assert outside, "the reference season runs more than a month past TODAY"

    assert client.post("/api/settings", json={"horizon": "30"}).status_code == 200
    narrow = client.get("/api/state").json()
    assert narrow["horizon_through"] == cutoff
    assert narrow["engine"]["beyond_horizon"] == len(outside)

    e = narrow["engine"]
    assert not (outside & set(e["plan"]["covered"]))
    assert not (outside & set(e["suggestions_by_trip"]))
    assert not (outside & set(e["reachable"]))
    # Nothing DISAPPEARS, though — the horizon bounds what the planner decides,
    # not what the dispatcher can see or reach by hand.
    assert outside <= {t["id"] for t in narrow["trips"]}


def test_horizon_round_trips_and_rejects_unknown_values(client):
    assert client.post("/api/settings", json={"horizon": "90"}).status_code == 200
    assert client.get("/api/state").json()["settings"]["horizon"] == "90"
    # Patches independently: setting one thing must not reset the rest.
    assert client.post("/api/settings", json={"objective": "idle"}).status_code == 200
    settings = client.get("/api/state").json()["settings"]
    assert (settings["horizon"], settings["objective"]) == ("90", "idle")
    # A free day count is not accepted — the presets are a closed set, so a
    # typo cannot quietly become a window nobody chose.
    assert client.post("/api/settings", json={"horizon": "45"}).status_code == 422
    assert client.post("/api/settings", json={"horizon": "nonsense"}).status_code == 422
    assert client.get("/api/state").json()["settings"]["horizon"] == "90"


def test_a_fresh_database_plans_a_month_ahead(tmp_path, workbook_bytes, monkeypatch):
    """The shipped default, asserted where no fixture has overridden it — the
    setting, that is. The date is pinned here as everywhere else in this file,
    so the cutoff is a fixed day and not one that can tick over mid-test."""
    monkeypatch.setattr(api, "_today", lambda: TODAY)
    app = create_app(tmp_path / "default.db")
    with TestClient(app) as c:
        c.post("/api/import", files={
            "file": ("t.xlsx", workbook_bytes, "application/octet-stream")})
        state = c.get("/api/state").json()
        assert state["settings"]["horizon"] == "30"
        assert state["horizon_through"] == (
            TODAY + timedelta(days=30)).isoformat()


def test_rechain_never_releases_a_trip_the_horizon_hides(client):
    """The correctness rule behind the fifth release boundary: an assignment
    past the cutoff is invisible to the replan, so releasing it would strand it
    every time. Asserted through the API because that is where the horizon is
    resolved from today."""
    cutoff = (TODAY + timedelta(days=30)).isoformat()
    assert _uncovered_after(client, cutoff), \
        "the reference season runs more than a month past TODAY"

    assert client.post("/api/settings", json={"horizon": "30"}).status_code == 200
    preview = client.get("/api/rechain").json()
    assert preview["horizon"] == cutoff

    by_id = {t["id"]: t for t in client.get("/api/state").json()["trips"]}
    assert all(by_id[i]["start"] <= cutoff for i in preview["released"])
    # And the counters still partition the future assignments.
    assert preview["frozen_horizon"] >= 0
    assert not set(preview["stranded"]) - set(preview["released"])


def test_reclaim_separates_the_notice_deadline_from_the_horizon(client):
    """A window shorter than the 21-day notice can leave nothing eligible, and
    the two counters are what let the panel say which rule did it."""
    wide = client.get("/api/reclaim-adhoc").json()
    assert wide["horizon"] is None and wide["frozen_horizon"] == 0
    assert wide["eligible"], "the reference data has cancellable hires at TODAY"

    assert client.post("/api/settings", json={"horizon": "14"}).status_code == 200
    narrow = client.get("/api/reclaim-adhoc").json()
    # 14 days out ends before the 21-day notice date, so the two rules cross
    # and every hire is frozen by one or the other.
    assert narrow["horizon"] < narrow["cutoff"]
    assert narrow["eligible"] == []
    assert narrow["frozen_horizon"] == len(wide["eligible"])
    # Mutually exclusive, so the panel's arithmetic adds up.
    assert narrow["frozen_notice"] == wide["frozen_notice"]


# -- locks and the master re-chain -------------------------------------------

def _future_suggestion(client):
    """An engine-proposed link whose demand has not started yet, or None."""
    state = client.get("/api/state").json()
    today = TODAY.isoformat()
    for trip in state["trips"]:
        if trip["start"] <= today:
            continue
        for s in state["engine"]["suggestions_by_trip"].get(trip["id"], []):
            if s["unit"] is not None:
                return s
    return None


def test_lock_round_trips_and_removes_the_trip_from_demand(client):
    state = client.get("/api/state").json()
    demand = next(t["id"] for t in state["trips"]
                  if not t["assignment"] and t["status"] != "Canceled"
                  and state["engine"]["suggestions_by_trip"].get(t["id"]))

    assert client.post(f"/api/trips/{demand}/lock",
                       json={"locked": True}).status_code == 200
    state = client.get("/api/state").json()
    assert demand in state["locked"]
    # The whole point: no suggestion, no plan link, nothing to accept.
    assert demand not in state["engine"]["suggestions_by_trip"]
    assert demand not in state["engine"]["plan"]["covered"]

    assert client.post(f"/api/trips/{demand}/lock",
                       json={"locked": False}).status_code == 200
    state = client.get("/api/state").json()
    assert state["locked"] == []
    assert state["engine"]["suggestions_by_trip"].get(demand)


def test_lock_is_undoable(client):
    trip = client.get("/api/state").json()["trips"][0]["id"]
    client.post(f"/api/trips/{trip}/lock", json={"locked": True})
    assert client.get("/api/state").json()["undo"]["label"].startswith("lock ")
    client.post("/api/undo")
    assert client.get("/api/state").json()["locked"] == []


def test_rechain_releases_an_app_made_future_assignment(client):
    s = _future_suggestion(client)
    assert s is not None, "the engine proposes a link on a trip after TODAY"
    assert client.post("/api/accept", json={
        "demand": s["demand"], "company": s["company"], "unit": s["unit"],
        "supply": s["supply"]}).status_code == 200

    preview = client.get("/api/rechain").json()
    assert s["demand"] in preview["released"]
    # Every source-file assignment in the future is counted, never released.
    assert preview["frozen_source"] > 0
    assert set(preview["stranded"]) <= set(preview["released"])


def test_rechain_will_not_release_a_locked_assignment(client):
    s = _future_suggestion(client)
    assert s is not None, "the engine proposes a link on a trip after TODAY"
    client.post("/api/accept", json={
        "demand": s["demand"], "company": s["company"], "unit": s["unit"],
        "supply": s["supply"]})
    assert s["demand"] in client.get("/api/rechain").json()["released"]

    client.post(f"/api/trips/{s['demand']}/lock", json={"locked": True})
    preview = client.get("/api/rechain").json()
    assert s["demand"] not in preview["released"]
    assert preview["frozen_locked"] >= 1


def test_rechain_apply_is_one_undo_step(client):
    """Applied as a unit and reversed as a unit — a half-applied rearrangement
    would leave assignments taken back with nothing put in their place."""
    s = _future_suggestion(client)
    assert s is not None, "the engine proposes a link on a trip after TODAY"
    client.post("/api/accept", json={
        "demand": s["demand"], "company": s["company"], "unit": s["unit"],
        "supply": s["supply"]})

    before = {t["id"]: t["assignment"] for t in client.get("/api/state").json()["trips"]}
    res = client.post("/api/rechain", json={})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["released"] >= 1

    after = {t["id"]: t["assignment"] for t in client.get("/api/state").json()["trips"]}
    assert after != before

    client.post("/api/undo")
    restored = {t["id"]: t["assignment"]
                for t in client.get("/api/state").json()["trips"]}
    assert restored == before


def test_rechain_never_touches_a_source_file_assignment(client):
    """The boundary the whole feature rests on: a company name that arrived on
    the spreadsheet is source data, and re-chain is not a back door around it."""
    state = client.get("/api/state").json()
    edited = set(state["overrides"])
    from_sheet = {t["id"] for t in state["trips"]
                  if t["assignment"] and t["id"] not in edited}
    assert from_sheet, "the reference workbook assigns buses in the sheet"

    client.post("/api/rechain", json={})
    after = {t["id"]: t["assignment"] for t in client.get("/api/state").json()["trips"]}
    for tid in from_sheet:
        assert after[tid] is not None


def test_rename_company_propagates_and_undoes(client):
    """The route's whole contract in one pass: the registry moves, every cell
    naming the company moves with it, nothing leaves the operable set, and one
    undo step reverses both halves."""
    before = client.get("/api/state").json()
    operable = len(before["trips"])
    named = [t for t in before["trips"]
             if (t["values"].get("Company name") or "").casefold().startswith("ollex")]
    assert named, "reference data should assign Ollex somewhere"

    res = client.post("/api/registry/companies/ollex/rename", json={"name": "Ollez"})
    assert res.status_code == 200 and res.json()["cells"] >= len(named)

    after = client.get("/api/state").json()
    assert len(after["trips"]) == operable          # nothing fell out
    assert not any((t["values"].get("Company name") or "").casefold().startswith("ollex")
                   for t in after["trips"])
    keys = {c["key"] for c in after["registry"]["companies"]}
    assert "ollez" in keys and "ollex" not in keys
    assert after["undo"]["label"] == "rename ollex to Ollez"

    client.post("/api/undo")
    back = client.get("/api/state").json()
    assert len(back["trips"]) == operable
    assert {c["key"] for c in back["registry"]["companies"]} >= {"ollex"}
    assert [t["values"].get("Company name") for t in back["trips"]] == \
           [t["values"].get("Company name") for t in before["trips"]]


def test_rename_onto_an_existing_company_is_a_conflict(client):
    res = client.post("/api/registry/companies/ollex/rename", json={"name": "Vilneda"})
    assert res.status_code == 409
    assert "already a registered company" in res.json()["detail"]


def test_rename_reaches_the_information_sheet_and_the_export(client):
    """The renamed company keeps its row in Bus Companies — which is matched by
    NAME to decide where a fleet count is written back, so leaving it stale
    would quietly drop the company out of that round-trip."""
    import io
    import openpyxl
    from buschain.codecs.seed import INFO_SHEET
    client.post("/api/registry/companies/ollex/rename", json={"name": "Ollez"})
    assert client.get("/api/state").json()["unwritable_companies"] == []

    exported = client.get("/api/export.xlsx").content
    ws = openpyxl.load_workbook(io.BytesIO(exported), data_only=True)[INFO_SHEET]
    names = [ws.cell(r, 13).value for r in range(2, ws.max_row + 1)]
    assert "Ollez" in names and "Ollex" not in names

    trips = openpyxl.load_workbook(io.BytesIO(exported), data_only=True)["Capitals 2026"]
    company_cells = [trips.cell(r, 14).value for r in range(2, 400)]
    assert any((c or "").startswith("Ollez") for c in company_cells)
    assert not any((c or "").startswith("Ollex") for c in company_cells)


# ---------- the board's drag gesture ----------


def _assigned_to_a_unit(state):
    return [t for t in state["trips"]
            if t["assignment"] and t["assignment"]["unit"] is not None
            and t["status"] != "Canceled"]


def _app_assigned(client):
    """Accept one suggestion, so there is an assignment the app owns.

    A freshly imported workbook has none: every bus on it came from the sheet,
    and those are source data no drag may move. The tests below need the other
    kind.
    """
    state = client.get("/api/state").json()
    s = next(s for by_trip in [state["engine"]["suggestions_by_trip"]]
             for group in by_trip.values() for s in group if s["kind"] == "chain")
    res = client.post("/api/accept", json={
        "demand": s["demand"], "company": s["company"], "unit": s["unit"],
        "supply": s["supply"]})
    assert res.status_code == 200, res.text
    return s["demand"]


def test_drop_targets_offer_buses_no_suggestion_ever_could(client):
    """Why this endpoint exists rather than reusing suggestions_by_trip.

    A suggestion hangs off a supply trip, so it can only ever name a bus that
    already has work to hand over — the drag has to light up the parked ones
    too. Two properties, checked over every uncovered trip the planner has an
    opinion about: the drop never refuses a link the planner proposes (the
    board contradicting itself), and somewhere on this dataset it offers more.
    """
    state = client.get("/api/state").json()
    by_trip = state["engine"]["suggestions_by_trip"]
    free = [t for t in state["trips"]
            if not t["assignment"] and t["status"] != "Canceled" and by_trip.get(t["id"])]
    assert free, "real data should leave uncovered trips with options"

    widened = 0
    for trip in free[:40]:                      # a sample: each is a full evaluate
        suggested = {(s["company"], s["unit"]) for s in by_trip[trip["id"]]}
        targets = client.get(f"/api/drop-targets/{trip['id']}").json()["targets"]
        units = {(t["company"], t["unit"]) for t in targets if t["unit"] is not None}
        assert suggested <= units, \
            f"{trip['id']}: a drop refused a link the planner proposes"
        widened += bool(units - suggested)
        # Every suggested bus keeps the link the suggestion would have recorded,
        # so a dragged trip and an accepted one leave the same provenance.
        by_key = {(t["company"], t["unit"]): t for t in targets}
        for s in by_trip[trip["id"]]:
            if s["kind"] == "chain":
                assert by_key[(s["company"], s["unit"])]["supply"] == s["supply"]
    assert widened, "no trip gained a bus — insertion_ok is not widening anything"


def test_reassign_moves_a_trip_between_units_and_undoes(client):
    """The gesture the feature is for: one call, one undo step, and the new bus
    visible everywhere — the Data tab reads the same Company cell the export
    splices, so writing it once is what makes the move propagate."""
    tid = _app_assigned(client)
    state = client.get("/api/state").json()
    trip = next(t for t in state["trips"] if t["id"] == tid)
    here = (trip["assignment"]["company"], trip["assignment"]["unit"])
    targets = client.get(f"/api/drop-targets/{tid}").json()["targets"]
    target = next((t for t in targets
                   if t["unit"] is not None and (t["company"], t["unit"]) != here), None)
    assert target, "an app-assigned trip should be movable to some other bus"

    res = client.post("/api/reassign", json={
        "trip": tid, "company": target["company"], "unit": target["unit"]})
    assert res.status_code == 200, res.text

    after = client.get("/api/state").json()
    now = next(t for t in after["trips"] if t["id"] == tid)
    assert now["assignment"] == {"company": target["company"], "unit": target["unit"]}
    # Written as a cell override, which is what the Data tab renders and the
    # xlsx export splices — one write, and the decision is everywhere.
    assert after["overrides"][tid], "the move must leave an edited cell behind"
    assert tid in after["engine"]["timelines"][f"{target['company']}|{target['unit']}"]
    assert tid not in after["engine"]["timelines"].get(f"{here[0]}|{here[1]}", [])

    assert client.post("/api/undo").status_code == 200
    back = client.get("/api/state").json()
    assert next(t for t in back["trips"] if t["id"] == tid)["assignment"] \
        == trip["assignment"]


def test_reassign_refuses_a_bus_the_engine_did_not_offer(client):
    """The client sends where the bar landed, never what it means. A drop the
    engine does not sanction is refused server-side even if the browser drew it
    as legal — the same rule the plan commits follow."""
    tid = _app_assigned(client)
    state = client.get("/api/state").json()
    targets = client.get(f"/api/drop-targets/{tid}").json()["targets"]
    legal = {(t["company"], t["unit"]) for t in targets}
    company, illegal = next(
        (c["key"], u) for c in state["registry"]["companies"]
        for u in c["units"] if (c["key"], u) not in legal)

    res = client.post("/api/reassign", json={
        "trip": tid, "company": company, "unit": illegal})
    assert res.status_code == 409
    assert "cannot take" in res.json()["detail"]


def test_reassign_moves_an_imported_assignment_like_the_data_tab_does(client):
    """A bus that arrived on the spreadsheet can be dragged to another bus.

    Typing over the Company cell in Data has always been able to do this
    (set_cell has no provenance check), so refusing it on the board made one
    screen able to express what the other could not. Both now write the same
    override, and the export carries the new bus.
    """
    state = client.get("/api/state").json()
    from_sheet = next(t for t in _assigned_to_a_unit(state)
                      if t["id"] not in state["overrides"])
    was = (from_sheet["assignment"]["company"], from_sheet["assignment"]["unit"])
    targets = client.get(f"/api/drop-targets/{from_sheet['id']}").json()["targets"]
    other = next(t for t in targets
                 if t["unit"] is not None and (t["company"], t["unit"]) != was)

    res = client.post("/api/reassign", json={
        "trip": from_sheet["id"], "company": other["company"], "unit": other["unit"]})
    assert res.status_code == 200, res.text
    after = client.get("/api/state").json()
    assert next(t for t in after["trips"] if t["id"] == from_sheet["id"])["assignment"] \
        == {"company": other["company"], "unit": other["unit"]}

    assert client.post("/api/undo").status_code == 200
    back = client.get("/api/state").json()
    assert next(t for t in back["trips"]
                if t["id"] == from_sheet["id"])["assignment"] \
        == from_sheet["assignment"]
    assert from_sheet["id"] not in back["overrides"], \
        "undo must leave the row as imported, not as an override that agrees"


def test_unassigning_an_imported_bus_exports_a_blank(client):
    """Clearing an imported assignment writes an EMPTY Company cell rather than
    dropping an override that was never there — the old implementation cleared
    by deleting the override, which is a no-op on a source value and is why
    this used to be refused. The blank is the visible consequence, so it is
    pinned here rather than left to be discovered in Excel.
    """
    state = client.get("/api/state").json()
    from_sheet = next(t for t in _assigned_to_a_unit(state)
                      if t["id"] not in state["overrides"])
    assert client.post("/api/unassign", json={"trip": from_sheet["id"]}).status_code == 200

    after = client.get("/api/state").json()
    assert next(t for t in after["trips"]
                if t["id"] == from_sheet["id"])["assignment"] is None
    assert after["overrides"][from_sheet["id"]]["Company name"] == ""

    import io
    import openpyxl
    ws = openpyxl.load_workbook(
        io.BytesIO(client.get("/api/export.xlsx").content))["Capitals 2026"]
    col = list(after["columns"]).index("Company name") + 1
    assert not (ws.cell(int(from_sheet["id"].split(":")[1]), col).value or "").strip()

    # The planner's boundary is untouched: a re-chain still releases only what
    # the app assigned, so automation cannot do what this deliberate act did.
    assert client.post("/api/undo").status_code == 200
    restored = client.get("/api/state").json()
    assert next(t for t in restored["trips"]
                if t["id"] == from_sheet["id"])["assignment"] == from_sheet["assignment"]


def test_unassign_still_refuses_a_trip_with_no_bus(client):
    state = client.get("/api/state").json()
    free = next(t for t in state["trips"] if not t["assignment"])
    res = client.post("/api/unassign", json={"trip": free["id"]})
    assert res.status_code == 409
    assert "no assignment" in res.json()["detail"]


def test_date_drag_writes_both_ends_as_one_action(client):
    """A move preserves the span, so it can never create the inverted or
    over-long row the contract refuses — but only if both dates land together.
    One undo step has to take the whole gesture back."""
    state = client.get("/api/state").json()
    trip = next(t for t in state["trips"] if t["status"] != "Canceled")
    start, end = trip["start"], trip["end"]
    shifted = {"Start date": _plus(start, 3), "End date": _plus(end, 3)}

    res = client.post(f"/api/trips/{trip['id']}/cells", json={"values": shifted})
    assert res.status_code == 200, res.text
    after = client.get("/api/state").json()
    now = next(t for t in after["trips"] if t["id"] == trip["id"])
    assert (now["start"], now["end"]) == (shifted["Start date"], shifted["End date"])
    # Still operable: the span never changed, so no contract reason can appear.
    assert not any(r["id"] == trip["id"] for r in after["non_operable"])

    # The drag has to reach the workbook, and as a DATE — a date column spliced
    # as text would still look right in the app and be wrong in Excel, where
    # the sheet's own d-mmm-yy format has nothing to format.
    import io
    import openpyxl
    ws = openpyxl.load_workbook(
        io.BytesIO(client.get("/api/export.xlsx").content))["Capitals 2026"]
    cols = {t: i + 1 for i, t in enumerate(after["columns"])}
    row = int(trip["id"].split(":")[1])
    for title in ("Start date", "End date"):
        cell = ws.cell(row, cols[title])
        assert cell.value.date().isoformat() == shifted[title]
        assert cell.number_format == "d-mmm-yy"

    assert client.post("/api/undo").status_code == 200
    back = next(t for t in client.get("/api/state").json()["trips"]
                if t["id"] == trip["id"])
    assert (back["start"], back["end"]) == (start, end)


def test_multi_cell_edit_refuses_an_unknown_column_before_writing(client):
    state = client.get("/api/state").json()
    trip = next(t for t in state["trips"] if t["status"] != "Canceled")
    res = client.post(f"/api/trips/{trip['id']}/cells", json={"values": {
        "Start date": _plus(trip["start"], 1), "Nonesuch": "x"}})
    assert res.status_code == 422
    now = next(t for t in client.get("/api/state").json()["trips"]
               if t["id"] == trip["id"])
    assert now["start"] == trip["start"], "a refused batch must write nothing"


def _plus(iso, days):
    from datetime import date, timedelta
    return (date.fromisoformat(iso) + timedelta(days=days)).isoformat()


# -- rule 11: reclaiming ad hoc work -----------------------------------------

def test_reclaim_preview_accounts_for_every_live_adhoc_trip(client):
    """eligible + the three frozen counts is the whole live ad hoc population,
    which is what lets a panel show the split without inventing a fourth
    category. The counts are mutually exclusive by refusal()'s test order."""
    preview = client.get("/api/reclaim-adhoc").json()
    state = client.get("/api/state").json()
    live_adhoc = [t for t in state["trips"]
                  if t["assignment"] and t["assignment"]["unit"] is None
                  and t["status"] != "Canceled"]
    assert len(preview["eligible"]) + preview["frozen_locked"] \
        + preview["frozen_pax"] + preview["frozen_notice"] == len(live_adhoc)
    # The workbook's 16 non-49 rows are all ad hoc, and rule 10 means no
    # numbered bus could take them — so releasing them could never help.
    assert preview["frozen_pax"] == 16
    assert preview["cutoff"] == _plus(preview["as_of"], preview["notice_days"])


def test_reclaim_never_offers_a_hire_inside_the_notice_window(client):
    """Rule 11 is the whole point: a booking three weeks out or less stands."""
    preview = client.get("/api/reclaim-adhoc").json()
    by_id = {t["id"]: t for t in client.get("/api/state").json()["trips"]}
    for tid in preview["eligible"]:
        assert by_id[tid]["start"] >= preview["cutoff"]
    assert preview["frozen_notice"] > 0, "the reference data has near-term hires"


def test_reclaim_moves_an_imported_hire_onto_a_chartered_bus(client):
    """The carve-out, exercised: every eligible hire in this workbook came in on
    the spreadsheet, so if source-written names were off limits the feature
    would do nothing at all."""
    preview = client.get("/api/reclaim-adhoc").json()
    assert preview["reclaimed"], "the reference data should reclaim something"
    assert set(preview["from_source"]) == set(preview["eligible"])

    before = {t["id"]: t["assignment"]
              for t in client.get("/api/state").json()["trips"]}
    for tid in preview["reclaimed"]:
        assert before[tid]["unit"] is None          # a hire before

    res = client.post("/api/reclaim-adhoc", json={})
    assert res.status_code == 200, res.text
    assert res.json()["reclaimed"] == len(preview["reclaimed"])

    after = {t["id"]: t["assignment"]
             for t in client.get("/api/state").json()["trips"]}
    for tid in preview["reclaimed"]:
        assert after[tid]["unit"] is not None       # a chartered bus after


def test_reclaim_leaves_the_hires_it_cannot_replace(client):
    """The never-strand guarantee end to end. A trip the replan could not cover
    keeps the bus it already had — this action can make nothing worse."""
    preview = client.get("/api/reclaim-adhoc").json()
    assert preview["stayed_adhoc"]
    client.post("/api/reclaim-adhoc", json={})

    after = {t["id"]: t["assignment"]
             for t in client.get("/api/state").json()["trips"]}
    for tid in preview["stayed_adhoc"]:
        assert after[tid] is not None and after[tid]["unit"] is None


def test_reclaim_apply_is_one_undo_step(client):
    """Reversed as a unit, including the imported company names it blanked —
    the safety net the source-data carve-out leans on."""
    before = {t["id"]: t["assignment"]
              for t in client.get("/api/state").json()["trips"]}
    assert client.post("/api/reclaim-adhoc", json={}).status_code == 200
    assert {t["id"]: t["assignment"]
            for t in client.get("/api/state").json()["trips"]} != before

    client.post("/api/undo")
    restored = {t["id"]: t["assignment"]
                for t in client.get("/api/state").json()["trips"]}
    assert restored == before


def test_reclaim_refuses_to_cancel_a_numbered_assignment(client):
    """_require_adhoc is a gate on SHAPE, not on the caller's honesty: the store
    re-asks the question against the row it is about to blank."""
    from buschain.store.repo import RepoError
    repo = client.app.state.repo
    numbered = next(t for t in client.get("/api/state").json()["trips"]
                    if t["assignment"] and t["assignment"]["unit"] is not None)
    tid = repo.row_labels() and next(
        t.id for t in repo.load_state()[0].trips
        if f"{t.id.origin}:{t.id.key}" == numbered["id"])
    with pytest.raises(RepoError, match="bare-company"):
        repo.reclaim_adhoc({tid}, [(tid, "ollex", 1, None)])
