"""Repository: all reads/writes of persistent state, and the glue that turns
stored rows back into contract inputs.

Design rule: validation has ONE home (contract.validate). The store keeps
raw imported values verbatim and overlays the only two in-system changes
(status, assignment) onto them at load time — so a re-validated overlay
behaves exactly like the same value arriving via import, and the export
plan writes exactly the overlay strings and nothing else.
"""
from __future__ import annotations

import io
import json
import sqlite3
import threading
import zipfile
from contextlib import contextmanager
from datetime import date, datetime
from typing import Any, Iterator, Mapping, Sequence

from .. import contract, normalize
from ..codecs import seed
from ..codecs.seed import SeedResult
from ..codecs.types import (
    ALL_COLUMNS, CellWrite, COLUMN_LETTERS, COLUMN_TITLES, COMPANY_NAME,
    CITY_HEADER, ExportPlan, ImportedDataset, INFO_CITY_COL, INFO_OPTION_COLS,
    InfoCellWrite, InfoWrite, NewRow, OPTION_COLUMNS, RawTripRow, STATUS,
)
from ..model import (
    AcceptedLink, Company, ContractReport, Registry, Status, Trip, TripId,
)


def _jsonable(value: object) -> object:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def _column_writes(
    title: str, default_col: str, values: list[str],
    existing: seed.SheetOptionList | None,
) -> list[InfoCellWrite]:
    """One list-valued Information column, written back if it disagrees.

    Compared as sets, not sequences: the list is a set semantically, so
    rewriting a whole column purely to reorder it would churn the file on every
    export for no gain, and leaves an order the user chose in Excel alone. When
    the contents do differ the column is rewritten sorted, and cells below the
    new last value are emptied — otherwise a pruned entry would survive in the
    sheet as a trailing leftover.

    A column the sheet does not head yet is created, header included: this is
    how a workbook that predates a list acquires it.
    """
    if existing is None and not values:
        return []                             # nothing to say, no column to make
    if existing is not None and set(existing.values) == set(values):
        return []
    column = existing.column if existing is not None else default_col
    out: list[InfoCellWrite] = []
    if existing is None:
        out.append(InfoCellWrite(1, column, title))
    out.extend(InfoCellWrite(2 + i, column, v) for i, v in enumerate(values))
    last_row = existing.last_row if existing is not None else 1
    out.extend(InfoCellWrite(row, column, "")
               for row in range(2 + len(values), last_row + 1))
    return out


class RepoError(ValueError):
    pass


# Overrides key on titles; these two carry behavior beyond a plain edit.
_STATUS_TITLE = COLUMN_TITLES[STATUS]
_COMPANY_TITLE = COLUMN_TITLES[COMPANY_NAME]


class Repo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        # Serializes mutations across FastAPI's threadpool. The shared
        # connection is thread-safe at the C level, but the undo protocol is
        # three transactions (snapshot -> action -> push) and two interleaved
        # requests could record an undo entry describing a state that never
        # existed. Single local user, so serializing writes costs nothing.
        # Reentrant because compound mutators (seed_from) call locked public
        # ones (upsert_company).
        self._write_lock = threading.RLock()
        # (blob_id, parsed Information grid) — see _info_grid_for_dataset.
        self._grid_cache: tuple[int, list[list[object]] | None] | None = None

    # -- registry ----------------------------------------------------------

    def registry(self) -> Registry:
        # Two fixed queries, not one per company: registry() runs at least
        # twice per /state and once per accept.
        units_by_company: dict[int, set[int]] = {}
        for r in self.conn.execute("SELECT company_id, unit_number FROM registry_units"):
            units_by_company.setdefault(r["company_id"], set()).add(r["unit_number"])
        companies = {
            row["canonical_key"]: Company(
                name=row["display_name"], note=row["note"],
                units=frozenset(units_by_company.get(row["id"], ())),
            )
            for row in self.conn.execute("SELECT * FROM registry_companies")
        }
        cities = {
            normalize.casefold_key(r["code"]): r["code"]
            for r in self.conn.execute("SELECT code FROM registry_cities")
        }
        return Registry(companies=companies, cities=cities)

    def is_seeded(self) -> bool:
        row = self.conn.execute("SELECT value FROM meta WHERE key = 'seeded'").fetchone()
        return row is not None

    def seed_from(self, seed_result: SeedResult) -> None:
        if self.is_seeded():
            raise RepoError("Registry already seeded; maintain it via the Registry tab")
        # The _-prefixed cores, not the public mutators: those commit, which
        # would end this transaction early — a failure after that point would
        # leave a written registry with no 'seeded' marker, and the next
        # attempt would re-seed on top of it.
        with self._write_lock, self.conn:
            for c in seed_result.companies:
                self._upsert_company(c.name, c.note)
                for unit in c.units:
                    self._add_unit(normalize.casefold_key(c.name), unit)
            for city in seed_result.cities:
                self._add_city(city)
            self.conn.execute(
                "INSERT INTO meta (key, value) VALUES ('seeded', ?)",
                (json.dumps(seed_result.warnings),),
            )

    def seed_warnings(self) -> list[str]:
        row = self.conn.execute("SELECT value FROM meta WHERE key = 'seeded'").fetchone()
        return json.loads(row["value"]) if row else []

    # Kept from the last import so the UI can show them. The seed warning that
    # cost 31 rows was written once and never read again; these are meant to
    # stay in front of the user until the sheet is fixed.
    def set_sync_warnings(self, warnings: list[str]) -> None:
        with self._write_lock, self.conn:
            self.conn.execute(
                "INSERT INTO meta (key, value) VALUES ('fleet_sync', ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (json.dumps(warnings),),
            )

    def sync_warnings(self) -> list[str]:
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key = 'fleet_sync'").fetchone()
        return json.loads(row["value"]) if row else []

    # -- planning settings ---------------------------------------------------
    # Two meta rows, not a table: the plan itself is derived state recomputed on
    # every load, so the only thing worth persisting is which plan to compute
    # and how to present it.
    MODES = ("auto", "manual")
    OBJECTIVES = ("coverage", "idle")
    _SETTING_KEYS = ("plan_mode", "plan_objective", "plan_reposition")

    def settings(self) -> dict[str, Any]:
        marks = ", ".join("?" * len(self._SETTING_KEYS))
        rows = {
            r["key"]: r["value"]
            for r in self.conn.execute(
                f"SELECT key, value FROM meta WHERE key IN ({marks})",
                self._SETTING_KEYS)
        }
        return {
            "mode": rows.get("plan_mode", "manual"),
            "objective": rows.get("plan_objective", "coverage"),
            # Defaults on, which is what the engine did before this was settable
            # — an upgraded database has no row and must not silently replan.
            "reposition": rows.get("plan_reposition", "1") == "1",
        }

    def set_settings(self, mode: str | None = None,
                     objective: str | None = None,
                     reposition: bool | None = None) -> dict[str, Any]:
        if mode is not None and mode not in self.MODES:
            raise RepoError(f"Unknown mode {mode!r}")
        if objective is not None and objective not in self.OBJECTIVES:
            raise RepoError(f"Unknown objective {objective!r}")
        with self._write_lock, self.conn:
            for key, value in (
                ("plan_mode", mode),
                ("plan_objective", objective),
                # meta values are TEXT; the bool round-trips as "1"/"0" rather
                # than str(bool), which would make "False" a truthy stored row.
                ("plan_reposition", None if reposition is None else str(int(reposition))),
            ):
                if value is not None:
                    self.conn.execute(
                        "INSERT INTO meta (key, value) VALUES (?, ?) "
                        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                        (key, value),
                    )
        return self.settings()

    # Registry mutators come in pairs: a public method owning lock and
    # transaction, and a _-prefixed core that only executes SQL. Compound
    # operations (seed_from, sync_fleets) call the cores inside ONE
    # transaction — calling the public forms there would commit mid-way and
    # leave a half-applied registry behind a failure.

    def upsert_company(self, display_name: str, note: str | None) -> None:
        with self._write_lock, self.conn:
            self._upsert_company(display_name, note)

    def _upsert_company(self, display_name: str, note: str | None) -> None:
        key = normalize.casefold_key(display_name)
        if not key:
            raise RepoError("Company name must not be empty")
        self.conn.execute(
            """INSERT INTO registry_companies (canonical_key, display_name, note)
               VALUES (?, ?, ?)
               ON CONFLICT (canonical_key)
               DO UPDATE SET display_name = excluded.display_name,
                             note = excluded.note""",
            (key, normalize.collapse_ws(normalize.trim(display_name)), note),
        )

    def _company_id(self, key: str) -> int:
        row = self.conn.execute(
            "SELECT id FROM registry_companies WHERE canonical_key = ?", (key,)
        ).fetchone()
        if row is None:
            raise RepoError(f"Unknown company {key!r}")
        return row["id"]

    def delete_company(self, key: str) -> None:
        # Same guard as remove_unit, one level up: registry_units cascades, so
        # this would drop every unit at once and every row naming the company
        # (numbered or ad hoc) would stop resolving and silently leave the
        # operable set. Refuse and name the rows; reassignment is the user's
        # decision. Guard and delete under ONE lock: a checked-then-deleted
        # gap lets a concurrent accept assign onto the company mid-flight —
        # the exact silent drop the guard exists to prevent.
        with self._write_lock:
            if self.conn.execute(
                "SELECT 1 FROM registry_companies WHERE canonical_key = ?",
                (key,),
            ).fetchone() is None:
                # A typo'd key was a silent no-op 200; the unit path already
                # refuses unknowns, so this does too.
                raise RepoError(f"Unknown company {key!r}")
            report, _, _ = self.load_state()
            trips = [
                t.id for t in report.trips
                if t.status is not Status.CANCELED
                and t.assignment is not None and t.assignment.company == key
            ]
            if trips:
                shown = ", ".join(f"{t.origin}:{t.key}" for t in trips[:5])
                more = f" and {len(trips) - 5} more" if len(trips) > 5 else ""
                raise RepoError(
                    f"Company still has {len(trips)} live trip(s) assigned "
                    f"({shown}{more}). Reassign them before deleting it."
                )
            with self.conn:
                self.conn.execute(
                    "DELETE FROM registry_companies WHERE canonical_key = ?", (key,))

    def add_unit(self, company_key: str, unit: int) -> None:
        with self._write_lock, self.conn:
            self._add_unit(company_key, unit)

    def _add_unit(self, company_key: str, unit: int) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO registry_units (company_id, unit_number) VALUES (?, ?)",
            (self._company_id(company_key), unit),
        )

    def _units(self, company_key: str) -> set[int]:
        return {
            r["unit_number"] for r in self.conn.execute(
                "SELECT unit_number FROM registry_units WHERE company_id = ?",
                (self._company_id(company_key),),
            )
        }

    def _delete_unit(self, company_key: str, unit: int) -> None:
        self.conn.execute(
            "DELETE FROM registry_units WHERE company_id = ? AND unit_number = ?",
            (self._company_id(company_key), unit),
        )

    def _units_in_use(self) -> dict[tuple[str, int], list[TripId]]:
        """Live trips per numbered unit, in the active dataset.

        Read through the contract, not the raw assignment strings: a unit is
        'in use' precisely when a row resolves to it, which is the same test
        that decides whether removing it would drop the row.
        """
        report, _, _ = self.load_state()
        out: dict[tuple[str, int], list[TripId]] = {}
        for t in report.trips:
            a = t.assignment
            if t.status is Status.CANCELED or a is None or a.unit is None:
                continue
            out.setdefault((a.company, a.unit), []).append(t.id)
        return out

    def remove_unit(self, company_key: str, unit: int) -> None:
        # Removing a unit that still has trips does not merely shrink the
        # registry: every row naming it stops resolving and silently leaves
        # the operable set, taking its supply and timeline with it. Refuse and
        # name the rows instead — reassignment is the user's decision. Guard
        # and delete under ONE lock (see delete_company).
        with self._write_lock:
            trips = self._units_in_use().get((company_key, unit), [])
            if trips:
                shown = ", ".join(f"{t.origin}:{t.key}" for t in trips[:5])
                more = f" and {len(trips) - 5} more" if len(trips) > 5 else ""
                raise RepoError(
                    f"Unit {unit} still has {len(trips)} live trip(s) assigned "
                    f"({shown}{more}). Reassign them before removing it."
                )
            with self.conn:
                self._delete_unit(company_key, unit)

    def sync_fleets(self, seed_result: SeedResult) -> list[str]:
        """Apply the Information sheet's 'Fleets' column to an already-seeded
        registry. Runs on every xlsx import: the sheet owns the fleet numbers,
        so unlike seed_from() this is idempotent and re-runnable.

        Never fails the import. A unit still carrying live trips is kept and
        reported — a registry one unit stale is far cheaper than an import
        that pushed rows out of the operable set on its way through.
        """
        warnings = list(seed_result.warnings)
        in_use = self._units_in_use()
        # One transaction for the whole sync: each mutation used to commit on
        # its own, so an exception mid-loop left a half-synced registry.
        with self._write_lock, self.conn:
            for c in seed_result.companies:
                key = normalize.casefold_key(c.name)
                existing = self.conn.execute(
                    "SELECT note FROM registry_companies WHERE canonical_key = ?", (key,)
                ).fetchone()
                # The sheet owns fleet counts, not display text: a note edited
                # in-app survives re-import.
                self._upsert_company(c.name, existing["note"] if existing else c.note)
                target = set(c.units)
                current = self._units(key)
                for unit in sorted(target - current):
                    self._add_unit(key, unit)
                for unit in sorted(current - target):
                    trips = in_use.get((key, unit), [])
                    if trips:
                        warnings.append(
                            f"{c.name} {unit}: kept, because {len(trips)} live trip(s) are "
                            f"assigned to it, though {seed.FLEET_HEADER} says {len(target)}. "
                            "Reassign them or correct the sheet."
                        )
                    else:
                        self._delete_unit(key, unit)
        return warnings

    def add_city(self, code: str) -> None:
        with self._write_lock, self.conn:
            self._add_city(code)

    def _add_city(self, code: str) -> None:
        code = normalize.collapse_ws(normalize.trim(code))
        if not code:
            raise RepoError("City code must not be empty")
        self.conn.execute("INSERT OR IGNORE INTO registry_cities (code) VALUES (?)", (code,))

    def remove_city(self, code: str) -> None:
        # Same guard as remove_unit: every live trip departing or arriving
        # here would stop resolving and silently leave the operable set.
        # Guard and delete under ONE lock (see delete_company).
        with self._write_lock:
            if self.conn.execute(
                "SELECT 1 FROM registry_cities WHERE code = ?", (code,)
            ).fetchone() is None:
                raise RepoError(f"Unknown city {code!r}")
            report, _, _ = self.load_state()
            trips = [
                t.id for t in report.trips
                if t.status is not Status.CANCELED and code in (t.dep_city, t.arr_city)
            ]
            if trips:
                shown = ", ".join(f"{t.origin}:{t.key}" for t in trips[:5])
                more = f" and {len(trips) - 5} more" if len(trips) > 5 else ""
                raise RepoError(
                    f"City {code!r} still appears on {len(trips)} live trip(s) "
                    f"({shown}{more}). Edit those trips before removing it."
                )
            with self.conn:
                self.conn.execute("DELETE FROM registry_cities WHERE code = ?", (code,))

    # -- pick lists for passthrough columns -----------------------------------

    def options(self) -> dict[str, list[str]]:
        self._fill_empty_option_lists()
        out: dict[str, list[str]] = {c: [] for c in OPTION_COLUMNS}
        for row in self.conn.execute(
            "SELECT column_title, value FROM registry_options ORDER BY value"
        ):
            out.setdefault(row["column_title"], []).append(row["value"])
        return out

    def add_option(self, column: str, value: str) -> None:
        if column not in OPTION_COLUMNS:
            raise RepoError(f"{column!r} has no pick list")
        value = normalize.collapse_ws(normalize.trim(value))
        if not value:
            raise RepoError("Value must not be empty")
        with self._write_lock, self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO registry_options (column_title, value)"
                " VALUES (?, ?)",
                (column, value),
            )

    def remove_option(self, column: str, value: str) -> None:
        with self._write_lock, self.conn:
            self.conn.execute(
                "DELETE FROM registry_options WHERE column_title = ? AND value = ?",
                (column, value),
            )

    def sync_options(self, seed_result: SeedResult) -> None:
        """Apply the Information sheet's pick-list columns to the stored lists.

        The sheet owns these lists the way it owns fleet counts, so this runs
        on every xlsx import and is idempotent: what the column says replaces
        what is stored, which is what makes a value pruned in Excel stay
        pruned. A column the sheet does not head is left alone entirely — see
        seed.sheet_option_lists on why absent and empty must not be conflated.

        Not gated on is_seeded(): a pick list is a dropdown source, unrelated
        to whether the company registry has been seeded.
        """
        with self._write_lock, self.conn:
            for title, lst in seed_result.options.items():
                if title not in OPTION_COLUMNS:
                    continue
                self.conn.execute(
                    "DELETE FROM registry_options WHERE column_title = ?", (title,))
                self.conn.executemany(
                    "INSERT OR IGNORE INTO registry_options (column_title, value)"
                    " VALUES (?, ?)",
                    [(title, v) for v in lst.values],
                )
            # Which lists this workbook claims ownership of. Rewritten whole,
            # so importing a workbook without the columns hands the lists back
            # to the bootstrap fill below.
            self.conn.execute(
                "INSERT INTO meta (key, value) VALUES ('options_from_sheet', ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (json.dumps(sorted(
                    t for t in seed_result.options if t in OPTION_COLUMNS)),),
            )

    def sync_cities(self, seed_result: SeedResult) -> list[str]:
        """Apply the Information sheet's 'Cities' column to the registry.

        The sheet owns the list, as it owns fleet counts and pick lists, so
        this runs on every xlsx import and is idempotent. A workbook that does
        not head the column says nothing and changes nothing.

        Unlike sync_options this cannot just delete what the sheet dropped.
        The contract resolves Departure/Arrival against these codes, so
        removing one in Excel would push every trip using it out of the
        operable set — silently, on import, with the rows blaming a city that
        no longer exists. Same answer as sync_fleets gives a unit that still
        carries trips: keep it, and say so. A registry one city stale is far
        cheaper than an import that quietly broke rows on its way through.
        Removing it for real stays a Registry-tab action, where remove_city
        names the trips first.
        """
        if seed_result.city_list is None:
            return []
        target = {
            code for code in (
                normalize.collapse_ws(normalize.trim(c))
                for c in seed_result.city_list.values
            ) if code
        }
        warnings: list[str] = []
        with self._write_lock:
            current = {
                row["code"] for row in
                self.conn.execute("SELECT code FROM registry_cities")
            }
            # Only when something would actually be dropped: load_state parses
            # and re-validates the whole dataset, far too costly to spend on
            # the common import where the list is unchanged or only grew.
            in_use: set[str] = set()
            if current - target:
                report, _, _ = self.load_state()
                for t in report.trips:
                    if t.status is not Status.CANCELED:
                        in_use.update((t.dep_city, t.arr_city))
            with self.conn:
                for code in sorted(target - current):
                    self._add_city(code)
                for code in sorted(current - target):
                    if code in in_use:
                        warnings.append(
                            f"City {code!r}: kept, because live trips still depart "
                            "from or arrive there. Remove it in the Registry tab, "
                            "which lists those trips first."
                        )
                        continue
                    self.conn.execute(
                        "DELETE FROM registry_cities WHERE code = ?", (code,))
        return warnings

    def _sheet_owned_options(self) -> set[str]:
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key = 'options_from_sheet'").fetchone()
        return set(json.loads(row["value"])) if row else set()

    def _fill_empty_option_lists(self) -> None:
        """An empty pick list is filled from the values in the active dataset.

        Checked on read rather than only at import, so the list is never empty
        just because the column gained a pick list after the data was already
        imported. Only when empty, so pruning sticks — clearing a list is the
        way to rebuild it from the data.

        A list the imported workbook heads its own column for is excluded: the
        sheet is its home, and 'the user emptied that column in Excel' would
        otherwise be undone from the trip data on the very next read.
        """
        dataset_id = self.active_dataset_id()
        if dataset_id is None:
            return
        sheet_owned = self._sheet_owned_options()
        empty = [
            c for c in OPTION_COLUMNS
            if c not in sheet_owned
            and self.conn.execute(
                "SELECT 1 FROM registry_options WHERE column_title = ? LIMIT 1", (c,)
            ).fetchone() is None
        ]
        if not empty:
            return
        seen: dict[str, set[str]] = {c: set() for c in empty}
        for row in self._trip_rows(dataset_id):
            raw = json.loads(row["raw"])
            for column in empty:
                value = raw.get(COLUMN_LETTERS[column])
                if value not in (None, ""):
                    seen[column].add(normalize.collapse_ws(normalize.trim(str(value))))
        with self._write_lock, self.conn:
            for column in empty:
                self.conn.executemany(
                    "INSERT OR IGNORE INTO registry_options (column_title, value)"
                    " VALUES (?, ?)",
                    [(column, v) for v in sorted(seen[column]) if v],
                )

    # -- dataset lifecycle ---------------------------------------------------

    def active_dataset_id(self) -> int | None:
        row = self.conn.execute("SELECT id FROM datasets WHERE active = 1").fetchone()
        return row["id"] if row else None

    def active_dataset_info(self) -> dict | None:
        row = self.conn.execute(
            """SELECT d.id, d.source_kind, d.imported_at, b.filename
               FROM datasets d LEFT JOIN blobs b ON b.id = d.blob_id
               WHERE d.active = 1"""
        ).fetchone()
        return dict(row) if row else None

    def import_dataset(self, dataset: ImportedDataset, filename: str) -> int:
        """Replace the active dataset, then drop imports that have fallen out
        of the retention window (see _prune_datasets)."""
        with self._write_lock:
            with self.conn:
                self.conn.execute("UPDATE datasets SET active = 0 WHERE active = 1")
                blob_id = None
                if dataset.blob is not None:
                    blob_id = self.conn.execute(
                        "INSERT INTO blobs (filename, content) VALUES (?, ?)",
                        (filename, dataset.blob),
                    ).lastrowid
                dataset_id = self.conn.execute(
                    "INSERT INTO datasets (blob_id, source_kind, csv_header)"
                    " VALUES (?, ?, ?)",
                    (blob_id, dataset.source_kind,
                     json.dumps(dataset.csv_header) if dataset.csv_header else None),
                ).lastrowid
                if dataset_id is None:          # pragma: no cover - sqlite invariant
                    raise RepoError("Insert produced no dataset id")
                csv_rows = dataset.csv_rows or ()
                for i, row in enumerate(dataset.rows):
                    raw = {c: _jsonable(row.values.get(c)) for c in ALL_COLUMNS}
                    self.conn.execute(
                        """INSERT INTO trips (dataset_id, origin, sheet_row, raw, csv_full)
                           VALUES (?, 'sheet', ?, ?, ?)""",
                        (dataset_id, row.sheet_row, json.dumps(raw),
                         json.dumps(csv_rows[i]) if i < len(csv_rows) else None),
                    )
                # Inside the import's transaction: an import that fails must
                # prune nothing. Imports are already a hard undo boundary
                # (nothing before one is undoable), so this adds no new
                # irreversible surface — the pruned datasets were unreachable
                # from the moment the import that replaced them succeeded.
                pruned = self._prune_datasets()
            # VACUUM cannot run inside a transaction, hence outside the `with`
            # — but still under the write lock. Without it the freed pages go
            # to the freelist and the file stops growing but never shrinks.
            # Gated on `pruned` so an import that deleted nothing never pays
            # for a full-file rewrite.
            if pruned:
                # Best-effort, and it has to be: the import is committed by
                # now, so raising here would report a successful import as a
                # 500 and skip the fleet sync the route runs afterwards.
                # VACUUM needs a temp file and an exclusive lock, and either
                # can be denied by something outside this app — a locked-down
                # %TEMP% on Windows, a second instance of the app, a reader
                # holding the database open. Losing it costs only the reclaim:
                # the freed pages stay on the freelist, which is exactly how
                # this behaved before pruning existed, and the next import
                # tries again.
                try:
                    self.conn.execute("VACUUM")
                except sqlite3.Error:
                    pass
        return dataset_id

    # Datasets accumulate. Importing does not delete the one it replaces, and
    # every xlsx import banks a verbatim copy of the workbook in `blobs`
    # (~1.9 MB) plus a full trip row set (~380 KB of JSON). Nothing reads an
    # inactive dataset — active_dataset_id() gates every read path and no route
    # exposes an older one — so unbounded the file grows ~2.3 MB per import
    # forever: nine imports produced a 17 MB database that was 92% unreachable.
    #
    # Five rather than one, because the blob archive is the only copy of a
    # workbook the app has ever seen and has already served as a recovery path
    # (the golden test fixture was recovered from it). A handful of imports
    # costs ~10 MB of disk and keeps that escape hatch open; either way the
    # file plateaus instead of growing without limit, which is the point.
    DATASET_RETENTION: int = 5

    def _prune_datasets(self) -> bool:
        """Drop every dataset outside the retention window, and any blob left
        unreferenced. Caller owns the transaction; returns whether anything
        was deleted.

        What goes with a dataset includes its cell_overrides and accepted_links
        — the dispatcher's actual work on that sheet. Nothing observable
        changes, since that work was already unreachable; what changes is that
        the loss becomes permanent. DATASET_RETENTION is the window that buys
        back.
        """
        doomed = [r["id"] for r in self.conn.execute(
            # active = 0 is redundant — the active dataset is always the newest
            # by id, so the LIMIT already spares it. Kept anyway: deleting the
            # active blob breaks export silently (it byte-splices from that
            # blob, see build_export_plan), and a guard this cheap belongs in
            # front of a failure that severe.
            "SELECT id FROM datasets WHERE active = 0 AND id NOT IN"
            " (SELECT id FROM datasets ORDER BY id DESC LIMIT ?)",
            (self.DATASET_RETENTION,),
        )]
        if not doomed:
            return False
        marks = ",".join("?" * len(doomed))
        # One statement: cascades to trips, and from there to cell_overrides
        # and trip_locks; accepted_links and undo_log cascade off the dataset
        # directly. accepted_links ALSO references trips with no ON DELETE, and
        # _restore has to delete links by hand before touching trips for
        # exactly that reason — but not here, and the difference is worth
        # knowing before someone "fixes" it: SQLite checks immediate foreign
        # keys at the conclusion of the statement, not mid-cascade, and both
        # children hang off the same dataset row, so no link outlives the trip
        # it names. _restore hard-deletes trips while their links survive,
        # which is the case that genuinely raises.
        self.conn.execute(f"DELETE FROM datasets WHERE id IN ({marks})", doomed)
        # blobs is the one child with no ON DELETE clause, so it needs its own
        # pass. Keyed on 'unreferenced' rather than 'belonged to a doomed
        # dataset', which also sweeps a blob orphaned by an earlier failure.
        self.conn.execute(
            "DELETE FROM blobs WHERE id NOT IN"
            " (SELECT blob_id FROM datasets WHERE blob_id IS NOT NULL)")
        return True

    # -- state loading -------------------------------------------------------

    def _trip_rows(self, dataset_id: int) -> list[sqlite3.Row]:
        """The working set: every row of the dataset except deleted ones.

        The single gate for deletion. Validation, the engine, both exports and
        the option pick lists all read rows through here, so a deleted row
        leaves every one of them at once rather than each having to remember.
        """
        return list(self.conn.execute(
            "SELECT * FROM trips WHERE dataset_id = ? AND deleted = 0 ORDER BY id",
            (dataset_id,),
        ))

    def _trip_index(self, dataset_id: int) -> list[sqlite3.Row]:
        """The working set's identity columns only — same gate as _trip_rows
        (keep the two WHERE clauses identical), minus the ~1.5 MB of raw JSON.
        For callers that never read cell values: labels, override metadata."""
        return list(self.conn.execute(
            "SELECT id, origin, sheet_row FROM trips"
            " WHERE dataset_id = ? AND deleted = 0 ORDER BY id",
            (dataset_id,),
        ))

    @staticmethod
    def _trip_id(row: sqlite3.Row) -> TripId:
        if row["origin"] == "sheet":
            return TripId("sheet", row["sheet_row"])
        return TripId("manual", row["id"])

    def row_labels(self) -> dict[str, str]:
        """How each row is numbered on screen, keyed by 'origin:key'.

        Just the number, as a string. A sheet row is its sheet row. A manual
        row gets the row it will take in the exported sheet — the first free
        row below the data plus its place in the queue, the same rule
        `_append_rows` follows at export time. Its own key is the SQLite rowid,
        which counts every row of every dataset ever imported (13361 on the
        ninth import of a 1670-row sheet) and means nothing to anyone.

        Bare numbers, with no 'row'/'new' word in front: the number alone is
        the row's identity, and prose that needs a noun says "row {label}" at
        the point it is written (see api._where). The two kinds cannot collide
        — a manual row always numbers above the last sheet row — so dropping
        the word costs no information beyond which rows arrived by paste, and
        that is what the table's own ordering already shows.

        Derived, not stored, and that is the tradeoff: deleting a sheet row
        below the data renumbers these. Storing them instead would freeze a
        number that the export then contradicts, which is the worse of the two.
        """
        dataset_id = self.active_dataset_id()
        if dataset_id is None:
            return {}
        rows = self._trip_index(dataset_id)
        last_sheet_row = max(
            (r["sheet_row"] for r in rows if r["origin"] == "sheet"), default=0)
        labels = {}
        manual = 0
        for row in rows:
            tid = self._trip_id(row)
            if row["origin"] == "sheet":
                labels[f"sheet:{tid.key}"] = str(tid.key)
            else:
                manual += 1
                labels[f"manual:{tid.key}"] = str(last_sheet_row + manual)
        return labels

    def _overrides_for(self, trip_db_id: int) -> dict[str, str]:
        """Edited cells for one trip, keyed by column title."""
        return {
            r["column_title"]: r["value"]
            for r in self.conn.execute(
                "SELECT column_title, value FROM cell_overrides WHERE trip_id = ?",
                (trip_db_id,),
            )
        }

    def _overrides_by_trip(self, dataset_id: int) -> dict[int, dict[str, str]]:
        """All edited cells of a dataset in one query, keyed by trip db id.

        The per-row _overrides_for is fine for single-trip mutations, but the
        loops that walk the whole working set (load_state, export, csv) were
        issuing it per row — ~2 queries per row per /state, and /state runs
        after every mutation.
        """
        out: dict[int, dict[str, str]] = {}
        for r in self.conn.execute(
            "SELECT o.trip_id, o.column_title, o.value FROM cell_overrides o"
            " JOIN trips t ON t.id = o.trip_id WHERE t.dataset_id = ?",
            (dataset_id,),
        ):
            out.setdefault(r["trip_id"], {})[r["column_title"]] = r["value"]
        return out

    def _raw_with_overlays(self, row: sqlite3.Row,
                           edits: dict[str, str] | None = None) -> dict[str, object]:
        raw = json.loads(row["raw"])
        # In-system edits behave exactly as if the same text had been
        # imported: overlay, then re-validate through the one contract path.
        # `edits` lets working-set loops pass the batch-loaded overrides in;
        # single-row callers omit it and pay one query.
        if edits is None:
            edits = self._overrides_for(row["id"])
        for title, value in edits.items():
            letter = COLUMN_LETTERS.get(title)
            if letter is not None:        # a title from a previous layout
                raw[letter] = value
        return raw

    def load_state(self) -> tuple[ContractReport, Registry, list[AcceptedLink]]:
        registry = self.registry()
        dataset_id = self.active_dataset_id()
        if dataset_id is None:
            return ContractReport(trips=(), non_operable=()), registry, []

        rows = self._trip_rows(dataset_id)
        edits_by_trip = self._overrides_by_trip(dataset_id)
        raw_rows = []
        ids = []
        for row in rows:
            values = self._raw_with_overlays(row, edits_by_trip.get(row["id"], {}))
            raw_rows.append(RawTripRow(sheet_row=row["sheet_row"], values=values))
            ids.append(self._trip_id(row))

        # validate() derives manual ids positionally; validate row-by-row so
        # ids stay exactly the store's ids.
        trips, non_operable = [], []
        for raw_row, trip_id in zip(raw_rows, ids):
            result = contract.validate_row(raw_row, trip_id, registry)
            (trips if isinstance(result, Trip) else non_operable).append(result)
        report = ContractReport(trips=tuple(trips), non_operable=tuple(non_operable))

        db_to_tripid = {row["id"]: self._trip_id(row) for row in rows}
        accepted = [
            AcceptedLink(
                supply=db_to_tripid.get(link["supply_trip_id"]),
                demand=db_to_tripid[link["demand_trip_id"]],
            )
            for link in self.conn.execute(
                "SELECT * FROM accepted_links WHERE dataset_id = ? ORDER BY id",
                (dataset_id,),
            )
        ]
        return report, registry, accepted

    def trip_overrides(self) -> dict[str, dict[str, str]]:
        """UI metadata: which cells carry in-system edits, by trip and title."""
        dataset_id = self.active_dataset_id()
        if dataset_id is None:
            return {}
        edits_by_trip = self._overrides_by_trip(dataset_id)
        out: dict[str, dict[str, str]] = {}
        for row in self._trip_index(dataset_id):
            edits = edits_by_trip.get(row["id"])
            if edits:
                tid = self._trip_id(row)
                out[f"{tid.origin}:{tid.key}"] = edits
        return out

    # -- mutations -----------------------------------------------------------

    def _db_row(self, trip_id: TripId) -> sqlite3.Row:
        dataset_id = self.active_dataset_id()
        if dataset_id is None:
            raise RepoError("No active dataset")
        # deleted = 0 throughout: a deleted row is out of the working set, so
        # every mutation must miss it exactly as if it had never been imported.
        if trip_id.origin == "sheet":
            row = self.conn.execute(
                "SELECT * FROM trips WHERE dataset_id = ? AND sheet_row = ? AND deleted = 0",
                (dataset_id, trip_id.key),
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT * FROM trips WHERE dataset_id = ? AND id = ?"
                " AND origin = 'manual' AND deleted = 0",
                (dataset_id, trip_id.key),
            ).fetchone()
        if row is None:
            raise RepoError(f"No trip {trip_id.origin}:{trip_id.key} in the active dataset")
        return row

    def _assignment_display(self, company_key: str, unit: int | None,
                            registry: Registry) -> str:
        company = registry.companies.get(company_key)
        if company is None:
            raise RepoError(f"Unknown company {company_key!r}")
        if unit is None:
            return company.name
        if unit not in company.units:
            raise RepoError(f"Company {company.name!r} has no unit {unit}")
        return f"{company.name} {unit}"

    def _accept_one(self, demand: TripId, company_key: str, unit: int | None,
                    supply: TripId | None, registry: Registry) -> None:
        """One acceptance, no transaction management — see accept/accept_chain."""
        display = self._assignment_display(company_key, unit, registry)
        demand_row = self._db_row(demand)
        raw = self._raw_with_overlays(demand_row)
        if str(raw.get(COMPANY_NAME) or "").strip():
            raise RepoError("Trip already has an assignment")
        supply_db_id = None
        if supply is not None:
            supply_row = self._db_row(supply)
            # A link may only hang off a bus that is actually somewhere. A
            # planned chain's later hops are fed by earlier hops, so accepting
            # one out of order (or from a stale plan the client still holds)
            # would record a dependency on a trip nobody is running.
            supply_raw = self._raw_with_overlays(supply_row)
            if not str(supply_raw.get(COMPANY_NAME) or "").strip():
                raise RepoError(
                    "Supply trip is not assigned yet — accept the earlier link first")
            supply_db_id = supply_row["id"]
        self._write_override(demand_row["id"], _COMPANY_TITLE, display)
        self.conn.execute(
            """INSERT INTO accepted_links (dataset_id, supply_trip_id, demand_trip_id)
               VALUES (?, ?, ?)""",
            (demand_row["dataset_id"], supply_db_id, demand_row["id"]),
        )

    def accept(self, demand: TripId, company_key: str, unit: int | None,
               supply: TripId | None) -> None:
        """Rule 9: acceptance writes the assignment. One transaction."""
        registry = self.registry()
        with self.conn:
            self._accept_one(demand, company_key, unit, supply, registry)

    def accept_chain(
        self,
        links: Sequence[tuple[TripId, str, int | None, TripId | None]],
    ) -> int:
        """Commit a whole proposed chain atomically, in chain order.

        Order matters: each link's supply is the previous link's demand, so it
        has to be assigned before the next _accept_one checks it. All or
        nothing — a chain half-written would leave assignments whose supply
        the planner never agreed to.
        """
        registry = self.registry()
        with self.conn:
            for demand, company_key, unit, supply in links:
                self._accept_one(demand, company_key, unit, supply, registry)
        return len(links)

    def unassign(self, trip_id: TripId) -> None:
        """Clear an in-system assignment (e.g. from the broken-chain worklist).
        Only assignments the system wrote can be cleared — source-file
        assignments are source data."""
        row = self._db_row(trip_id)
        if _COMPANY_TITLE not in self._overrides_for(row["id"]):
            raise RepoError("Trip's assignment came from the source file; edit it there")
        with self.conn:
            self.conn.execute(
                "DELETE FROM cell_overrides WHERE trip_id = ? AND column_title = ?",
                (row["id"], _COMPANY_TITLE),
            )
            self.conn.execute(
                "DELETE FROM accepted_links WHERE demand_trip_id = ?", (row["id"],)
            )

    # -- locks ---------------------------------------------------------------
    # A lock says "do not re-decide this trip". It is the exemption list for
    # rechain.build, and nothing else in the store reads it — the engine does
    # the rest (a locked trip leaves the demand set; assigned, it keeps feeding
    # the next hop).

    def locked_ids(self) -> set[TripId]:
        dataset_id = self.active_dataset_id()
        if dataset_id is None:
            return set()
        return {
            self._trip_id(row) for row in self.conn.execute(
                "SELECT t.* FROM trip_locks l JOIN trips t ON t.id = l.trip_id"
                " WHERE t.dataset_id = ? AND t.deleted = 0",
                (dataset_id,),
            )
        }

    def set_lock(self, trip_id: TripId, locked: bool) -> None:
        row = self._db_row(trip_id)
        with self.conn:
            if locked:
                self.conn.execute(
                    "INSERT OR IGNORE INTO trip_locks (trip_id) VALUES (?)", (row["id"],))
            else:
                self.conn.execute(
                    "DELETE FROM trip_locks WHERE trip_id = ?", (row["id"],))

    def system_assigned_ids(self) -> set[TripId]:
        """Trips whose assignment this app wrote — the only ones it may take back.

        The same boundary unassign() enforces one row at a time: a company name
        that arrived on the spreadsheet is source data, and a re-chain must not
        become the back door around that rule. Read from cell_overrides, which
        is precisely the record of what the app changed.

        The blank-raw test is the part that is easy to leave out and wrong to.
        An override can also SIT ON TOP of an imported company name (someone
        typed over it in the Data tab), and dropping that override does not free
        the trip — it restores whatever the sheet said. A re-chain that released
        such a row would plan it as demand and then fail to write the plan,
        because the trip is assigned again the moment the override is gone.
        """
        dataset_id = self.active_dataset_id()
        if dataset_id is None:
            return set()
        out: set[TripId] = set()
        for row in self.conn.execute(
            "SELECT t.* FROM cell_overrides o JOIN trips t ON t.id = o.trip_id"
            " WHERE t.dataset_id = ? AND t.deleted = 0 AND o.column_title = ?",
            (dataset_id, _COMPANY_TITLE),
        ):
            raw = json.loads(row["raw"])
            if not str(raw.get(COMPANY_NAME) or "").strip():
                out.add(self._trip_id(row))
        return out

    def rechain(
        self,
        released: Sequence[TripId],
        links: Sequence[tuple[TripId, str, int | None, TripId | None]],
    ) -> int:
        """Take back the released assignments and write the replanned links.

        One transaction, in that order: every released trip must be clear of its
        assignment before _accept_one checks it, and a released trip is often a
        link's demand further down the same list. Links arrive in chain order
        for the same reason accept_chain needs them to.

        All or nothing. A half-applied re-chain is the worst state this app can
        reach — assignments taken back with nothing put in their place.
        """
        registry = self.registry()
        with self.conn:
            for trip_id in released:
                row = self._db_row(trip_id)
                self.conn.execute(
                    "DELETE FROM cell_overrides WHERE trip_id = ? AND column_title = ?",
                    (row["id"], _COMPANY_TITLE),
                )
                # Both directions. Clearing the cell kills the link INTO this
                # trip; the link OUT of it now claims a bus that no longer runs
                # this trip fed the next one, so it goes too. That downstream
                # trip keeps the assignment already written into its cell — the
                # same trade delete_trip makes, work kept and provenance
                # dropped.
                self.conn.execute(
                    "DELETE FROM accepted_links"
                    " WHERE demand_trip_id = ? OR supply_trip_id = ?",
                    (row["id"], row["id"]),
                )
            for demand, company_key, unit, supply in links:
                self._accept_one(demand, company_key, unit, supply, registry)
        return len(links)

    def _write_override(self, trip_db_id: int, title: str, value: str) -> None:
        self.conn.execute(
            """INSERT INTO cell_overrides (trip_id, column_title, value) VALUES (?, ?, ?)
               ON CONFLICT (trip_id, column_title) DO UPDATE SET value = excluded.value""",
            (trip_db_id, title, value),
        )

    def set_cell(self, trip_id: TripId, title: str, value: str) -> None:
        """Record an in-system edit of one cell.

        No validation here by design: the value is overlaid onto the raw row
        and re-validated through the one contract path on the next load, so a
        typo lands the row in the non-operable list with a reason rather than
        being rejected at the keystroke.
        """
        letter = COLUMN_LETTERS.get(title)
        if letter is None:
            raise RepoError(f"{title!r} is not a column of the contract layout")
        row = self._db_row(trip_id)
        original = json.loads(row["raw"]).get(letter)
        with self.conn:
            if value == ("" if original is None else str(original)):
                # Edited back to the imported value: drop the override so the
                # export does not splice a cell that did not actually change.
                self.conn.execute(
                    "DELETE FROM cell_overrides WHERE trip_id = ? AND column_title = ?",
                    (row["id"], title),
                )
            else:
                self._write_override(row["id"], title, value)
            if title == _COMPANY_TITLE:
                # Typing over the assignment discards the chain provenance:
                # the recorded supply no longer explains what the cell says.
                self.conn.execute(
                    "DELETE FROM accepted_links WHERE demand_trip_id = ?", (row["id"],)
                )
                # A NEW assignment also kills the link OUT of this trip — it
                # claims a bus that no longer runs this trip fed the next one
                # (the same trade rechain and delete_trips make). Clearing
                # the cell keeps it: the downstream trip then lands on the
                # broken-chain worklist, which is the designed unassign flow.
                if value.strip():
                    self.conn.execute(
                        "DELETE FROM accepted_links WHERE supply_trip_id = ?",
                        (row["id"],)
                    )

    def set_status(self, trip_id: TripId, status: Status) -> None:
        self.set_cell(trip_id, _STATUS_TITLE, status.value)

    def delete_trips(self, trip_ids: Sequence[TripId]) -> None:
        """Take rows out of the working set — one action, all or nothing.

        Soft, so undo can put them back, and so each row's original cells
        survive for a re-import comparison. Accepted links go with a row: a
        link is a statement that one bus runs both trips, and half of that is
        now gone. A downstream trip keeps the assignment already written into
        its cell — the same trade set_cell makes when someone types over an
        assignment — so the work is not silently un-done, only its provenance
        is dropped.

        Every id is resolved before anything is written: a stale id in the
        batch (a row already deleted, or repaired out from under the ticks)
        refuses the whole batch, rather than leaving a half-applied delete the
        user would have to reconcile against what they actually selected.
        """
        rows = [self._db_row(t) for t in trip_ids]
        with self.conn:
            for row in rows:
                self.conn.execute("UPDATE trips SET deleted = 1 WHERE id = ?", (row["id"],))
                self.conn.execute(
                    "DELETE FROM accepted_links"
                    " WHERE demand_trip_id = ? OR supply_trip_id = ?",
                    (row["id"], row["id"]),
                )

    def delete_trip(self, trip_id: TripId) -> None:
        self.delete_trips([trip_id])

    def add_manual_trips(self, rows: Sequence[Mapping[str, object]]) -> list[TripId]:
        """Append manually entered trips — one form submission, or one paste.

        Plural at this level, and committed once, because a pasted batch is a
        single action: half a paste surviving a failure part-way through would
        leave the planner reconciling the sheet by hand. The caller wraps it in
        one `undoable`, and the snapshot records which manual rows existed
        before (see `_snapshot`), so undo removes the whole batch together.
        """
        dataset_id = self.active_dataset_id()
        if dataset_id is None:
            raise RepoError("Import or create a dataset before adding trips")
        ids = []
        # A real transaction, not inserts + a trailing commit: with the bare
        # commit a failure mid-batch left the earlier inserts pending on the
        # shared connection, to be committed by whatever transaction came next.
        with self.conn:
            for values in rows:
                raw = {c: _jsonable(values.get(c)) for c in ALL_COLUMNS}
                cur = self.conn.execute(
                    "INSERT INTO trips (dataset_id, origin, raw) VALUES (?, 'manual', ?)",
                    (dataset_id, json.dumps(raw)),
                )
                if cur.lastrowid is None:       # pragma: no cover - sqlite invariant
                    raise RepoError("Insert produced no row id")
                ids.append(TripId("manual", cur.lastrowid))
        return ids

    def add_manual_trip(self, values: dict[str, object]) -> TripId:
        return self.add_manual_trips([values])[0]

    # -- undo ----------------------------------------------------------------
    # One stack for the whole app, not one per screen. A cell edit in the Data
    # tab can invalidate an acceptance made in Suggestions, so undoing them out
    # of order would rebuild a state the user was never actually in.
    #
    # An entry is a snapshot of the state as it was BEFORE the action, not an
    # inverse operation. Compound actions are what make that the cheaper
    # design: 'accept the entire plan' writes 59 links across as many rows, and
    # one snapshot reverses it as simply as it reverses a single edit. It also
    # means a mutation added later is undoable by wrapping it, with no second
    # implementation of how to walk it backwards.

    UNDO_DEPTH = 50

    def _snapshot(self, dataset_id: int) -> str:
        """Everything an undoable action can change, for one dataset.

        Deliberately not the trips themselves: their `raw` is ~1.5 MB and no
        action rewrites it. What moves is the overlay (cell_overrides), the
        chain lineage (accepted_links), and which rows are live — so the
        snapshot is those three, and it stays a few KB.
        """
        return json.dumps({
            "overrides": [
                [r["trip_id"], r["column_title"], r["value"]]
                for r in self.conn.execute(
                    "SELECT o.trip_id, o.column_title, o.value FROM cell_overrides o"
                    " JOIN trips t ON t.id = o.trip_id WHERE t.dataset_id = ?"
                    " ORDER BY o.trip_id, o.column_title",
                    (dataset_id,),
                )
            ],
            "links": [
                [r["supply_trip_id"], r["demand_trip_id"]]
                for r in self.conn.execute(
                    "SELECT supply_trip_id, demand_trip_id FROM accepted_links"
                    " WHERE dataset_id = ? ORDER BY id",
                    (dataset_id,),
                )
            ],
            "deleted": [
                r["id"] for r in self.conn.execute(
                    "SELECT id FROM trips WHERE dataset_id = ? AND deleted = 1", (dataset_id,))
            ],
            "locks": [
                r["trip_id"] for r in self.conn.execute(
                    "SELECT l.trip_id FROM trip_locks l JOIN trips t ON t.id = l.trip_id"
                    " WHERE t.dataset_id = ? ORDER BY l.trip_id",
                    (dataset_id,),
                )
            ],
            # Which manual rows existed, so undoing 'add trip' removes it.
            "manual": [
                r["id"] for r in self.conn.execute(
                    "SELECT id FROM trips WHERE dataset_id = ? AND origin = 'manual'",
                    (dataset_id,))
            ],
        })

    def _restore(self, dataset_id: int, snap: dict) -> None:
        """Put the dataset back to a snapshot. Caller owns the transaction."""
        # Links first: accepted_links references trips with no ON DELETE, so
        # hard-deleting a manual row that carries a link would otherwise raise
        # IntegrityError. (They are rewritten wholesale from the snapshot at
        # the bottom, so deleting them all here loses nothing.)
        self.conn.execute(
            "DELETE FROM accepted_links WHERE dataset_id = ?", (dataset_id,))
        manual = [int(i) for i in snap["manual"]]
        # Manual rows created since the snapshot are hard-deleted (their
        # overrides cascade). Sheet rows are never hard-deleted, so every
        # trip_id the snapshot's overrides name still exists below.
        keep = ",".join("?" * len(manual))
        self.conn.execute(
            "DELETE FROM trips WHERE dataset_id = ? AND origin = 'manual'"
            + (f" AND id NOT IN ({keep})" if manual else ""),
            [dataset_id, *manual],
        )
        self.conn.execute(
            "UPDATE trips SET deleted = 0 WHERE dataset_id = ?", (dataset_id,))
        self.conn.executemany(
            "UPDATE trips SET deleted = 1 WHERE id = ? AND dataset_id = ?",
            [(int(i), dataset_id) for i in snap["deleted"]],
        )
        self.conn.execute(
            "DELETE FROM cell_overrides WHERE trip_id IN"
            " (SELECT id FROM trips WHERE dataset_id = ?)", (dataset_id,))
        self.conn.executemany(
            "INSERT INTO cell_overrides (trip_id, column_title, value) VALUES (?, ?, ?)",
            [tuple(o) for o in snap["overrides"]],
        )
        # .get, not [...]: undo entries written before locks existed have no
        # such key, and an upgraded database must still be able to step back.
        self.conn.execute(
            "DELETE FROM trip_locks WHERE trip_id IN"
            " (SELECT id FROM trips WHERE dataset_id = ?)", (dataset_id,))
        self.conn.executemany(
            "INSERT INTO trip_locks (trip_id) VALUES (?)",
            [(int(i),) for i in snap.get("locks", ())],
        )
        # Rewritten wholesale rather than diffed (deleted at the top of this
        # method). accepted_at is reset as a result; nothing reads it, and
        # lineage is what the board draws from.
        self.conn.executemany(
            "INSERT INTO accepted_links (dataset_id, supply_trip_id, demand_trip_id)"
            " VALUES (?, ?, ?)",
            [(dataset_id, s, d) for s, d in snap["links"]],
        )

    @contextmanager
    def undoable(self, label: str) -> Iterator[None]:
        """Record an undo point iff the wrapped action succeeds.

        The snapshot is taken first (it has to describe the state before) but
        only pushed after the body returns, so a refused action — an already
        assigned trip, a bad column — leaves no entry to undo.
        """
        # The lock spans snapshot, action, and push: the three are separate
        # transactions, and another request landing between them would make
        # this entry describe a state that never existed.
        with self._write_lock:
            dataset_id = self.active_dataset_id()
            before = self._snapshot(dataset_id) if dataset_id is not None else None
            yield
            if before is not None and dataset_id is not None:
                self._push_undo(dataset_id, label, before)

    def _push_undo(self, dataset_id: int, label: str, state: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO undo_log (dataset_id, label, state) VALUES (?, ?, ?)",
                (dataset_id, label, state),
            )
            self.conn.execute(
                "DELETE FROM undo_log WHERE dataset_id = ? AND id NOT IN"
                " (SELECT id FROM undo_log WHERE dataset_id = ?"
                "  ORDER BY id DESC LIMIT ?)",
                (dataset_id, dataset_id, self.UNDO_DEPTH),
            )

    def undo_head(self) -> dict[str, object] | None:
        """What the Undo button says, or None when there is nothing to undo."""
        dataset_id = self.active_dataset_id()
        if dataset_id is None:
            return None
        rows = list(self.conn.execute(
            "SELECT label FROM undo_log WHERE dataset_id = ? ORDER BY id DESC",
            (dataset_id,)))
        if not rows:
            return None
        return {"label": rows[0]["label"], "depth": len(rows)}

    def undo(self) -> str:
        """Step back one action. Not itself undoable — no redo by design."""
        with self._write_lock:
            dataset_id = self.active_dataset_id()
            row = None if dataset_id is None else self.conn.execute(
                "SELECT * FROM undo_log WHERE dataset_id = ? ORDER BY id DESC LIMIT 1",
                (dataset_id,)).fetchone()
            if row is None:
                raise RepoError("Nothing to undo")
            with self.conn:
                self._restore(dataset_id, json.loads(row["state"]))  # type: ignore[arg-type]
                self.conn.execute("DELETE FROM undo_log WHERE id = ?", (row["id"],))
            return row["label"]

    # -- export --------------------------------------------------------------

    def build_export_plan(self) -> ExportPlan:
        dataset_id = self.active_dataset_id()
        if dataset_id is None:
            raise RepoError("No active dataset to export")
        blob = None
        ds = self.conn.execute("SELECT blob_id FROM datasets WHERE id = ?",
                               (dataset_id,)).fetchone()
        if ds["blob_id"] is not None:
            blob = self.conn.execute("SELECT content FROM blobs WHERE id = ?",
                                     (ds["blob_id"],)).fetchone()["content"]
        writes: list[CellWrite] = []
        new_rows: list[NewRow] = []
        edits_by_trip = self._overrides_by_trip(dataset_id)
        for row in self._trip_rows(dataset_id):
            edits = edits_by_trip.get(row["id"], {})
            if row["origin"] == "manual":
                new_rows.append(NewRow(values=self._raw_with_overlays(row, edits)))
                continue
            # The plan is exactly the edited cells — nothing is diffed, so a
            # cell nobody touched is never spliced and stays byte-identical.
            for title, value in sorted(edits.items()):
                letter = COLUMN_LETTERS.get(title)
                if letter is not None:
                    writes.append(CellWrite(row["sheet_row"], letter, value))
        grid = self._info_grid_for_dataset()
        return ExportPlan(
            blob=blob, cell_writes=tuple(writes), new_rows=tuple(new_rows),
            info_writes=self._info_writes(grid),
            info_cell_writes=self._info_option_writes(grid),
            deleted_rows=self._deleted_sheet_rows(dataset_id),
        )

    def _deleted_sheet_rows(self, dataset_id: int) -> tuple[int, ...]:
        """Source rows to drop from the exported sheet. Manual rows are absent
        here because a deleted one is simply never appended."""
        return tuple(sorted(
            r["sheet_row"] for r in self.conn.execute(
                "SELECT sheet_row FROM trips"
                " WHERE dataset_id = ? AND deleted = 1 AND origin = 'sheet'",
                (dataset_id,),
            )
        ))

    @staticmethod
    def _info_grid(blob: bytes | None) -> list[list[object]] | None:
        """The Information sheet, or None if this blob has none to read.

        Absence is not an error here: csv-sourced datasets have no blob, and
        a workbook may legitimately lack the sheet. Nothing to read means
        nothing to sync or write back — the export path still fails loudly if
        the bytes turn out to be unusable when it goes to patch them.
        """
        if blob is None or not zipfile.is_zipfile(io.BytesIO(blob)):
            return None
        try:
            return seed.read_info_grid(blob)
        except ValueError:
            return None

    def _info_grid_for_dataset(self) -> list[list[object]] | None:
        """The active dataset's Information grid, cached per blob id.

        unwritable_companies runs on every /state — i.e. after every mutation
        — and re-parsing the multi-MB workbook each time was the single
        largest per-request cost. Blob rows are insert-only, so a blob id is
        immutable and the cache can never go stale.
        """
        dataset_id = self.active_dataset_id()
        if dataset_id is None:
            return None
        ds = self.conn.execute("SELECT blob_id FROM datasets WHERE id = ?",
                               (dataset_id,)).fetchone()
        if ds is None or ds["blob_id"] is None:
            return None
        if self._grid_cache is not None and self._grid_cache[0] == ds["blob_id"]:
            return self._grid_cache[1]
        blob = self.conn.execute("SELECT content FROM blobs WHERE id = ?",
                                 (ds["blob_id"],)).fetchone()["content"]
        grid = self._info_grid(blob)
        self._grid_cache = (ds["blob_id"], grid)
        return grid

    def _info_writes(self, grid: list[list[object]] | None) -> tuple[InfoWrite, ...]:
        """Fleet counts that no longer match the sheet, back into their rows.

        Diffed rather than written wholesale: with the registry in step with
        the sheet this returns nothing, so an untouched export stays
        byte-identical (AT1). Companies created in-app have no row in the
        'Bus Companies' column and cannot be written back — adding rows to
        the Information sheet is out of scope, so they are reported instead.
        """
        if grid is None:
            return ()
        companies = self.registry().companies
        # The column is discovered from the sheet, like the reader discovers
        # it — see seed.fleet_column.
        column = seed.fleet_column(grid)
        writes = [
            InfoWrite(sheet_row=row, value=len(companies[key].units), column=column)
            for row, name, current in seed.sheet_fleets(grid)
            if (key := normalize.casefold_key(name)) in companies
            and current != len(companies[key].units)
        ]
        return tuple(writes)

    def _info_option_writes(
            self, grid: list[list[object]] | None) -> tuple[InfoCellWrite, ...]:
        """Pick lists back into their own columns on the Information sheet.

        Compared as sets, not sequences: the list is a set semantically, so
        rewriting a whole column purely to reorder it would churn the file on
        every export for no gain, and leaves an order the user chose in Excel
        alone. When the contents do differ the column is rewritten sorted, and
        cells below the new last value are emptied — otherwise a pruned entry
        would survive in the sheet as a trailing leftover.

        A column the sheet does not head yet is created, header included: this
        is how a workbook that predates the feature acquires its lists.
        """
        if grid is None:
            return ()
        stored = self.options()               # bootstraps from trip data if empty
        on_sheet = seed.sheet_option_lists(grid)
        out: list[InfoCellWrite] = []
        for title, default_col in INFO_OPTION_COLS.items():
            out.extend(_column_writes(
                title, default_col, sorted(stored.get(title, [])), on_sheet.get(title)))
        # The city registry rides the same machinery — same shape, same
        # create-the-column-if-absent rule — but is not a pick list: see
        # types.CITY_HEADER on why it is kept out of INFO_OPTION_COLS.
        out.extend(_column_writes(
            CITY_HEADER, INFO_CITY_COL,
            sorted(self.registry().cities.values()),
            seed.sheet_city_list(grid)))
        return tuple(out)

    def unwritable_companies(self) -> list[str]:
        """Registry companies with no row in the Information sheet, whose
        fleet number therefore cannot round-trip. Surfaced in the UI so the
        gap is visible rather than a silently dropped write."""
        grid = self._info_grid_for_dataset()
        if grid is None:
            return []
        in_sheet = {normalize.casefold_key(n) for _, n in seed.company_rows(grid)}
        return sorted(
            c.name for key, c in self.registry().companies.items()
            if key not in in_sheet and c.units
        )

    def csv_source(self) -> tuple[tuple[str, ...], list[list[object]]] | None:
        """Full-layout rows for csv-sourced datasets, overlays applied.
        Returns None for xlsx-sourced datasets (their csv export renders
        from the patched workbook instead — one rendering path)."""
        dataset_id = self.active_dataset_id()
        if dataset_id is None:
            raise RepoError("No active dataset to export")
        ds = self.conn.execute("SELECT source_kind, csv_header FROM datasets WHERE id = ?",
                               (dataset_id,)).fetchone()
        if ds["source_kind"] != "csv":
            return None
        header = tuple(json.loads(ds["csv_header"]))
        col_index = {c: i for i, c in enumerate(ALL_COLUMNS)}
        edits_by_trip = self._overrides_by_trip(dataset_id)
        out: list[list[object]] = []
        for row in self._trip_rows(dataset_id):
            full: list[object]
            edits = edits_by_trip.get(row["id"], {})
            if row["origin"] == "manual":
                full = [""] * len(header)
                for col, value in self._raw_with_overlays(row, edits).items():
                    full[col_index[col]] = "" if value is None else value
            else:
                full = list(json.loads(row["csv_full"]))
                full += [""] * (len(header) - len(full))
                for title, value in edits.items():
                    letter = COLUMN_LETTERS.get(title)
                    if letter is not None:
                        full[col_index[letter]] = value
            out.append(full)
        return header, out
