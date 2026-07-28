"""Store: registry CRUD, dataset lifecycle, mutations, export-plan flags."""
import pytest

from buschain import engine
from buschain.codecs.types import (
    ARRIVAL, BUS_PREFERENCE, COMPANY_NAME, DEPARTURE, END_DATE, ER,
    ImportedDataset, PAX, PROJECT, RawTripRow, START_DATE, STATUS,
)
from buschain.model import AcceptedLink, Status, TripId
from buschain.store import db
from buschain.store.repo import Repo, RepoError


@pytest.fixture
def repo(tmp_path):
    return Repo(db.connect(tmp_path / "test.db"))


def seeded(repo: Repo) -> Repo:
    repo.upsert_company("Ollex", None)
    repo.add_unit("ollex", 1)
    repo.add_unit("ollex", 2)
    repo.upsert_company("Vilneda", None)
    for city in ("STO", "CPH", "OSL"):
        repo.add_city(city)
    return repo


# Semantic names, not column letters, so these tests say nothing about where
# a field sits in the sheet.
_FIELDS = {
    "status": STATUS, "project": PROJECT, "pref": BUS_PREFERENCE,
    "start": START_DATE, "end": END_DATE, "dep": DEPARTURE, "arr": ARRIVAL,
    "pax": PAX, "er": ER, "company": COMPANY_NAME,
}


def trip_values(**overrides):
    values = {
        STATUS: "Confirmed", PROJECT: "1", BUS_PREFERENCE: None,
        START_DATE: "2026-04-01", END_DATE: "2026-04-03",
        DEPARTURE: "STO", ARRIVAL: "CPH", PAX: 49, ER: None, COMPANY_NAME: None,
    }
    values.update({_FIELDS[k]: v for k, v in overrides.items()})
    return values


def import_rows(repo: Repo, *rows: RawTripRow) -> int:
    ds = ImportedDataset(source_kind="xlsx", rows=tuple(rows), blob=b"fake-xlsx-bytes")
    return repo.import_dataset(ds, "test.xlsx")


def test_registry_roundtrip(repo):
    seeded(repo)
    reg = repo.registry()
    assert reg.resolve_company("ollex  2") == ("ollex", 2)
    assert reg.resolve_city("CPH trf") == "CPH"
    repo.remove_unit("ollex", 2)
    assert repo.registry().resolve_company("Ollex 2") is None


def test_fleet_size_is_the_unit_count(repo):
    seeded(repo)
    assert repo.registry().companies["ollex"].fleet_size == 2
    repo.add_unit("ollex", 3)
    assert repo.registry().companies["ollex"].fleet_size == 3
    repo.remove_unit("ollex", 3)
    assert repo.registry().companies["ollex"].fleet_size == 2


def test_removing_a_unit_in_use_is_refused(repo):
    """Removal is not a shrink, it is a silent deletion: every row naming the
    unit stops resolving and leaves the operable set with its supply."""
    seeded(repo)
    import_rows(repo, RawTripRow(2, trip_values(company="Ollex 2")))
    with pytest.raises(RepoError) as e:
        repo.remove_unit("ollex", 2)
    assert "sheet:2" in str(e.value)
    assert repo.registry().resolve_company("Ollex 2") == ("ollex", 2)
    # Canceled trips do not pin a unit — they are excluded from matching.
    import_rows(repo, RawTripRow(2, trip_values(company="Ollex 2", status="Canceled")))
    repo.remove_unit("ollex", 2)
    assert repo.registry().resolve_company("Ollex 2") is None


def test_sync_fleets_applies_the_sheet_and_keeps_units_in_use(repo):
    from buschain.codecs.seed import SeedCompany, SeedResult

    seeded(repo)
    import_rows(repo, RawTripRow(2, trip_values(company="Ollex 2")))
    result = SeedResult(
        companies=[SeedCompany(name="Ollex", units=(1,)),
                   SeedCompany(name="Nordibuzz", units=(1, 2))],
        cities=[],
    )
    warnings = repo.sync_fleets(result)

    # Grown: a company absent from the registry arrives with its charter.
    assert repo.registry().companies["nordibuzz"].units == frozenset({1, 2})
    # Shrunk, but not below a unit that live trips still name — reported
    # instead, because failing the import outright would be worse.
    assert repo.registry().companies["ollex"].units == frozenset({1, 2})
    assert len(warnings) == 1 and "Ollex 2" in warnings[0]


def test_sync_fleets_keeps_notes_edited_in_app(repo):
    from buschain.codecs.seed import SeedCompany, SeedResult

    seeded(repo)
    repo.upsert_company("Ollex", "call Tatjana first")
    repo.sync_fleets(SeedResult(
        companies=[SeedCompany(name="Ollex", units=(1, 2), note="from the sheet")],
        cities=[],
    ))
    assert repo.registry().companies["ollex"].note == "call Tatjana first"


def test_import_replaces_active_dataset(repo):
    seeded(repo)
    first = import_rows(repo, RawTripRow(2, trip_values()))
    second = import_rows(repo, RawTripRow(2, trip_values(status="Pending")))
    assert repo.active_dataset_id() == second != first
    report, _, _ = repo.load_state()
    assert report.trips[0].status is Status.PENDING


def test_accept_writes_assignment_and_link(repo):
    seeded(repo)
    import_rows(repo,
                RawTripRow(2, trip_values(company="Ollex 1", arr="CPH")),
                RawTripRow(3, trip_values(status="Confirmed", start="2026-04-05",
                                          end="2026-04-07", dep="CPH", arr="OSL")))
    repo.accept(TripId("sheet", 3), "ollex", 1, TripId("sheet", 2))
    report, registry, accepted = repo.load_state()
    demand = next(t for t in report.trips if t.id.key == 3)
    assert demand.assignment is not None
    assert (demand.assignment.company, demand.assignment.unit) == ("ollex", 1)
    assert accepted == [
        AcceptedLink(supply=TripId("sheet", 2), demand=TripId("sheet", 3))
    ]
    # export plan carries exactly the accepted write, nothing else
    plan = repo.build_export_plan()
    assert [(w.sheet_row, w.column, w.value) for w in plan.cell_writes] == \
        [(3, COMPANY_NAME, "Ollex 1")]


def test_accept_rejects_double_assignment(repo):
    seeded(repo)
    import_rows(repo, RawTripRow(2, trip_values(company="Ollex 1")))
    with pytest.raises(RepoError):
        repo.accept(TripId("sheet", 2), "ollex", 2, None)


def test_accept_rejects_unknown_unit(repo):
    seeded(repo)
    import_rows(repo, RawTripRow(2, trip_values()))
    with pytest.raises(RepoError):
        repo.accept(TripId("sheet", 2), "ollex", 9, None)


def test_status_change_flows_to_engine_and_broken_chains(repo):
    seeded(repo)
    import_rows(repo,
                RawTripRow(2, trip_values(status="Pending", company="Ollex 1", arr="CPH")),
                RawTripRow(3, trip_values(start="2026-04-05", end="2026-04-07",
                                          dep="CPH", arr="OSL")))
    repo.accept(TripId("sheet", 3), "ollex", 1, TripId("sheet", 2))
    # the Pending supply cancels -> downstream assignment lands on the worklist
    repo.set_status(TripId("sheet", 2), Status.CANCELED)
    report, registry, accepted = repo.load_state()
    result = engine.evaluate(report, registry, accepted)
    (item,) = result.broken_chains
    assert item.assignment == TripId("sheet", 3)
    assert item.canceled_dependency == TripId("sheet", 2)
    # and the export plan reflects both in-system changes
    plan = repo.build_export_plan()
    writes = {(w.sheet_row, w.column): w.value for w in plan.cell_writes}
    assert writes == {(2, STATUS): "Canceled", (3, COMPANY_NAME): "Ollex 1"}


def test_unassign_only_for_system_written_assignments(repo):
    seeded(repo)
    import_rows(repo, RawTripRow(2, trip_values(company="Ollex 1")))
    with pytest.raises(RepoError):
        repo.unassign(TripId("sheet", 2))


def test_manual_trip_appears_and_exports_as_new_row(repo):
    seeded(repo)
    import_rows(repo, RawTripRow(2, trip_values()))
    tid = repo.add_manual_trip(trip_values(status="Pending", dep="OSL", arr="STO"))
    report, _, _ = repo.load_state()
    manual = next(t for t in report.trips if t.id == tid)
    assert manual.status is Status.PENDING
    plan = repo.build_export_plan()
    (new_row,) = plan.new_rows
    assert new_row.values[DEPARTURE] == "OSL"


def test_seed_only_once(repo, seed_result):
    repo.seed_from(seed_result)
    assert repo.is_seeded()
    assert repo.registry().resolve_company("Kautra 4") == ("kautra", 4)
    # Every charter comes from the Fleets column, including the two the old
    # name-matched charter block could not reach.
    assert repo.registry().resolve_company("Nordibuzz 2") == ("nordibuzz", 2)
    assert repo.registry().resolve_company("Vilneda 1") == ("vilneda", 1)
    assert repo.seed_warnings() == []
    with pytest.raises(RepoError):
        repo.seed_from(seed_result)


# -- row deletion ------------------------------------------------------------

def test_delete_removes_the_row_from_every_reader(repo):
    """One gate (_trip_rows), so a deleted row is gone from validation, the
    engine's inputs, the export plan and the pick lists at the same moment."""
    seeded(repo)
    import_rows(repo,
                RawTripRow(2, trip_values()),
                RawTripRow(3, trip_values(dep="CPH", arr="OSL")))
    repo.delete_trip(TripId("sheet", 3))

    report, _, _ = repo.load_state()
    assert [t.id.key for t in report.trips] == [2]
    # And out of the sheet on export, rather than merely blanked.
    assert repo.build_export_plan().deleted_rows == (3,)
    # It is no longer addressable: every mutation must miss it exactly as if
    # it had never been imported.
    with pytest.raises(RepoError):
        repo.set_status(TripId("sheet", 3), Status.CANCELED)


def test_delete_drops_links_that_named_the_row(repo):
    """Half a link is not a link: the bus can no longer run both trips."""
    seeded(repo)
    import_rows(repo,
                RawTripRow(2, trip_values(company="Ollex 1", arr="CPH")),
                RawTripRow(3, trip_values(start="2026-04-05", end="2026-04-07",
                                          dep="CPH", arr="OSL")))
    repo.accept(TripId("sheet", 3), "ollex", 1, TripId("sheet", 2))
    repo.delete_trip(TripId("sheet", 2))

    report, _, accepted = repo.load_state()
    assert accepted == []
    # The downstream assignment stays: it was written, and deleting its supply
    # removes the provenance, not the decision.
    demand = next(t for t in report.trips if t.id.key == 3)
    assert demand.assignment is not None


# -- undo --------------------------------------------------------------------

def test_undo_reverses_a_cell_edit(repo):
    seeded(repo)
    import_rows(repo, RawTripRow(2, trip_values()))
    with repo.undoable("edit Status on row 2"):
        repo.set_status(TripId("sheet", 2), Status.CANCELED)
    assert repo.undo_head() == {"label": "edit Status on row 2", "depth": 1}

    assert repo.undo() == "edit Status on row 2"
    report, _, _ = repo.load_state()
    assert report.trips[0].status is Status.CONFIRMED
    # The override is gone, not merely overwritten: an untouched cell must not
    # be spliced on export.
    assert repo.build_export_plan().cell_writes == ()
    assert repo.undo_head() is None


def test_undo_reverses_a_whole_chain_in_one_step(repo):
    """The reason an entry is a snapshot and not an inverse operation: this
    commits two links atomically and comes back the same way."""
    seeded(repo)
    import_rows(repo,
                RawTripRow(2, trip_values(company="Ollex 1", arr="CPH")),
                RawTripRow(3, trip_values(start="2026-04-05", end="2026-04-07",
                                          dep="CPH", arr="OSL")),
                RawTripRow(4, trip_values(start="2026-04-09", end="2026-04-11",
                                          dep="OSL", arr="STO")))
    with repo.undoable("accept chain on Ollex 1 (2 links)"):
        repo.accept_chain([
            (TripId("sheet", 3), "ollex", 1, TripId("sheet", 2)),
            (TripId("sheet", 4), "ollex", 1, TripId("sheet", 3)),
        ])
    report, _, accepted = repo.load_state()
    assert len(accepted) == 2

    repo.undo()
    report, _, accepted = repo.load_state()
    assert accepted == []
    assert [t.id.key for t in report.trips if t.assignment] == [2]


def test_undo_restores_a_deleted_row(repo):
    seeded(repo)
    import_rows(repo, RawTripRow(2, trip_values()), RawTripRow(3, trip_values()))
    with repo.undoable("delete row 3"):
        repo.delete_trip(TripId("sheet", 3))
    assert repo.undo() == "delete row 3"

    report, _, _ = repo.load_state()
    assert [t.id.key for t in report.trips] == [2, 3]
    assert repo.build_export_plan().deleted_rows == ()


def test_undo_removes_a_manually_added_trip(repo):
    """The one case the snapshot has to reverse by removing a row rather than
    by restoring a flag."""
    seeded(repo)
    import_rows(repo, RawTripRow(2, trip_values()))
    with repo.undoable("add trip"):
        tid = repo.add_manual_trip(trip_values(dep="OSL", arr="STO"))
    repo.undo()

    report, _, _ = repo.load_state()
    assert [t.id for t in report.trips] == [TripId("sheet", 2)]
    assert repo.build_export_plan().new_rows == ()
    with pytest.raises(RepoError):
        repo.set_status(tid, Status.CANCELED)


def test_undo_steps_back_one_action_at_a_time(repo):
    seeded(repo)
    import_rows(repo, RawTripRow(2, trip_values()))
    for status in (Status.PENDING, Status.CANCELED):
        with repo.undoable(f"set {status.value} on row 2"):
            repo.set_status(TripId("sheet", 2), status)

    assert repo.undo() == "set Canceled on row 2"
    report, _, _ = repo.load_state()
    assert report.trips[0].status is Status.PENDING
    assert repo.undo() == "set Pending on row 2"
    report, _, _ = repo.load_state()
    assert report.trips[0].status is Status.CONFIRMED
    with pytest.raises(RepoError):
        repo.undo()


def test_a_refused_action_leaves_no_undo_entry(repo):
    """The snapshot is taken before the action but pushed only after it
    succeeds, or the button would offer to undo something that never ran."""
    seeded(repo)
    import_rows(repo, RawTripRow(2, trip_values(company="Ollex 1")))
    with pytest.raises(RepoError):
        with repo.undoable("assign row 2 to Ollex 2"):
            repo.accept(TripId("sheet", 2), "ollex", 2, None)
    assert repo.undo_head() is None


def test_undo_history_is_capped_and_starts_fresh_per_import(repo):
    seeded(repo)
    import_rows(repo, RawTripRow(2, trip_values()))
    for i in range(repo.UNDO_DEPTH + 5):
        with repo.undoable(f"edit C Notes on row 2 ({i})"):
            repo.set_cell(TripId("sheet", 2), "C Notes", f"note {i}")
    head = repo.undo_head()
    assert head is not None and head["depth"] == repo.UNDO_DEPTH

    # A new import replaces the working set, so nothing before it is undoable.
    import_rows(repo, RawTripRow(2, trip_values()))
    assert repo.undo_head() is None


# -- dataset retention ---------------------------------------------------------
#
# Nothing reads an inactive dataset, so these tests are about the file on disk,
# not about anything the dispatcher can see.

def _big_import(repo: Repo) -> int:
    """An import whose blob is large enough that pruning it moves page_count."""
    return repo.import_dataset(
        ImportedDataset(source_kind="xlsx", rows=(RawTripRow(2, trip_values()),),
                        blob=b"x" * 200_000),
        "big.xlsx")


def _pages(repo: Repo) -> int:
    """Size in pages rather than st_size: in WAL mode the main file does not
    give back its space until a checkpoint, so the file on disk lags what the
    database actually holds."""
    return repo.conn.execute("PRAGMA page_count").fetchone()[0]


def test_import_prunes_datasets_beyond_retention(repo):
    seeded(repo)
    ids = [import_rows(repo, RawTripRow(2, trip_values()))
           for _ in range(repo.DATASET_RETENTION + 3)]
    kept = [r["id"] for r in repo.conn.execute("SELECT id FROM datasets ORDER BY id")]
    assert kept == ids[-repo.DATASET_RETENTION:]
    assert repo.active_dataset_id() == ids[-1]
    # The trips went with them rather than being orphaned behind the gate.
    assert repo.conn.execute("SELECT COUNT(*) FROM trips").fetchone()[0] == \
        repo.DATASET_RETENTION


def test_prune_drops_orphaned_blobs_but_never_the_active_one(repo):
    """The active blob is what the export byte-splices, so losing it would
    break the export silently rather than loudly."""
    seeded(repo)
    for _ in range(repo.DATASET_RETENTION + 3):
        import_rows(repo, RawTripRow(2, trip_values(company="Ollex 1")))
    assert repo.conn.execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == \
        repo.DATASET_RETENTION
    # No surviving dataset points at a blob that is gone...
    assert repo.conn.execute(
        "SELECT COUNT(*) FROM datasets d LEFT JOIN blobs b ON b.id = d.blob_id"
        " WHERE d.blob_id IS NOT NULL AND b.id IS NULL").fetchone()[0] == 0
    # ...and the live one is still there to splice.
    assert repo.build_export_plan().blob == b"fake-xlsx-bytes"


def test_prune_takes_every_child_of_the_dataset_with_it(repo):
    """The prune is one DELETE and five cascades. accepted_links is the one to
    watch: it also references trips with no ON DELETE, and _restore has to
    delete links by hand for that reason — this pins that the dataset cascade
    does not need the same treatment (nothing here raises IntegrityError) and
    that no child is left orphaned behind the working-set gate."""
    seeded(repo)
    import_rows(repo,
                RawTripRow(2, trip_values(company="Ollex 1", arr="CPH")),
                RawTripRow(3, trip_values(start="2026-04-05", end="2026-04-07",
                                          dep="CPH", arr="OSL")))
    repo.accept(TripId("sheet", 3), "ollex", 1, TripId("sheet", 2))
    with repo.undoable("edit C Notes on row 2"):
        repo.set_cell(TripId("sheet", 2), "C Notes", "note")
    repo.set_lock(TripId("sheet", 2), True)

    for _ in range(repo.DATASET_RETENTION):
        import_rows(repo, RawTripRow(2, trip_values()))

    for table in ("accepted_links", "cell_overrides", "trip_locks", "undo_log"):
        assert repo.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, \
            f"{table} rows outlived their dataset"


def test_prune_reclaims_space_from_an_over_full_database(tmp_path):
    """The existing database meets this code already past the window: the next
    import has to shrink it, not merely stop it growing. Covers the VACUUM,
    which without it would leave the freed pages on the freelist."""
    repo = seeded(Repo(db.connect(tmp_path / "grow.db")))
    repo.DATASET_RETENTION = 50         # instance attribute shadows the policy
    for _ in range(9):
        _big_import(repo)
    del repo.DATASET_RETENTION          # back to the real one

    before = _pages(repo)
    _big_import(repo)
    assert _pages(repo) < before
    assert repo.conn.execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == \
        repo.DATASET_RETENTION


def test_import_past_the_window_stops_growing_the_database(tmp_path):
    repo = seeded(Repo(db.connect(tmp_path / "plateau.db")))
    for _ in range(repo.DATASET_RETENTION):
        _big_import(repo)
    plateau = _pages(repo)
    for _ in range(6):
        _big_import(repo)
    # Each import banks ~200 KB, so six unpruned would be several times this.
    assert _pages(repo) <= plateau * 1.1


# -- pick lists, whose home is the Information sheet ---------------------------

def _sheet(**lists):
    """A SeedResult declaring the given pick-list columns."""
    from buschain.codecs.seed import SeedResult, SheetOptionList
    return SeedResult(
        companies=[], cities=[],
        options={t: SheetOptionList(column="O", values=tuple(v), last_row=1 + len(v))
                 for t, v in lists.items()},
    )


def _with_option_values(repo, *, nationality):
    from buschain.codecs.types import COLUMN_LETTERS
    rows = tuple(
        RawTripRow(i, {**trip_values(), COLUMN_LETTERS["Nationality"]: n})
        for i, n in enumerate(nationality, start=2)
    )
    import_rows(repo, *rows)
    return repo


def test_pick_list_bootstraps_from_the_data_when_the_sheet_has_no_column(repo):
    """The workbook that predates the columns still gets a usable dropdown —
    and that list is what the first export writes into the sheet."""
    seeded(repo)
    _with_option_values(repo, nationality=["Swedish", "Korea", "Swedish"])
    assert repo.options()["Nationality"] == ["Korea", "Swedish"]


def test_sync_options_replaces_the_stored_list(repo):
    seeded(repo)
    _with_option_values(repo, nationality=["Swedish", "Korea"])
    repo.options()                                   # bootstrap it first
    repo.sync_options(_sheet(Nationality=["Swedish", "Norwegian"]))
    # The sheet wins outright: 'Korea', pruned there, does not survive here.
    assert repo.options()["Nationality"] == ["Norwegian", "Swedish"]


def test_a_list_emptied_in_the_sheet_is_not_refilled_from_the_data(repo):
    """The bootstrap fill must not undo a deliberate clear. Reading the list
    is what used to resurrect it, so read twice."""
    seeded(repo)
    _with_option_values(repo, nationality=["Swedish", "Korea"])
    repo.sync_options(_sheet(Nationality=[]))
    assert repo.options()["Nationality"] == []
    assert repo.options()["Nationality"] == []


def test_a_workbook_without_the_columns_leaves_the_list_alone(repo):
    """'Nothing to say' is not 'empty' — and it hands the list back to the
    bootstrap fill, so an older workbook does not leave the dropdown blank."""
    seeded(repo)
    _with_option_values(repo, nationality=["Swedish", "Korea"])
    repo.sync_options(_sheet(Nationality=["Norwegian"]))
    repo.sync_options(_sheet())                      # a sheet heading neither column
    assert repo.options()["Nationality"] == ["Norwegian"]
    repo.remove_option("Nationality", "Norwegian")
    assert repo.options()["Nationality"] == ["Korea", "Swedish"]      # refilled
