"""SQLite connection + schema.

SQLite (stdlib) over JSON files because accept/status-change are multi-table
writes that need atomicity, and the original workbook blob belongs next to
the rows it produced. Single local user: WAL mode, no pooling.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
-- No fleet_size column: a company's fleet IS its numbered-unit count
-- (model.Company.fleet_size). The two were stored separately and drifted.
CREATE TABLE IF NOT EXISTS registry_companies (
    id INTEGER PRIMARY KEY,
    canonical_key TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    note TEXT
);
CREATE TABLE IF NOT EXISTS registry_units (
    company_id INTEGER NOT NULL REFERENCES registry_companies(id) ON DELETE CASCADE,
    unit_number INTEGER NOT NULL,
    UNIQUE (company_id, unit_number)
);
CREATE TABLE IF NOT EXISTS registry_cities (
    code TEXT PRIMARY KEY
);
-- Pick lists for passthrough columns (Nationality, OP). Keyed by column title
-- so adding another such column is data, not a table. Unlike cities these are
-- never resolved by the contract — they only populate the UI's dropdown.
CREATE TABLE IF NOT EXISTS registry_options (
    column_title TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (column_title, value)
);
CREATE TABLE IF NOT EXISTS blobs (
    id INTEGER PRIMARY KEY,
    filename TEXT NOT NULL,
    uploaded_at TEXT NOT NULL DEFAULT (datetime('now')),
    content BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS datasets (
    id INTEGER PRIMARY KEY,
    blob_id INTEGER REFERENCES blobs(id),
    source_kind TEXT NOT NULL CHECK (source_kind IN ('xlsx', 'csv')),
    csv_header TEXT,              -- JSON array; csv-sourced datasets only
    imported_at TEXT NOT NULL DEFAULT (datetime('now')),
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS trips (
    id INTEGER PRIMARY KEY,
    dataset_id INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    origin TEXT NOT NULL CHECK (origin IN ('sheet', 'manual')),
    sheet_row INTEGER,            -- 1-based source row; NULL for manual
    raw TEXT NOT NULL,            -- JSON of column values as imported/entered
    csv_full TEXT,                -- JSON of the complete source row (csv datasets)
    -- Soft delete: the row leaves the working set and is dropped from the
    -- export, but its content stays so undo can put it back. Hard-deleting
    -- would make a deletion the one action in the app that cannot be reversed.
    deleted INTEGER NOT NULL DEFAULT 0,
    UNIQUE (dataset_id, sheet_row)
);
-- Every in-system cell edit, including the assignment an acceptance writes.
-- One mechanism rather than a column per editable field: the export plan is
-- exactly this table, and re-validation is 'overlay these onto raw, then run
-- the one contract path'. Keyed by column TITLE so a layout change (a column
-- inserted or deleted) does not silently repoint stored edits.
CREATE TABLE IF NOT EXISTS cell_overrides (
    trip_id INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    column_title TEXT NOT NULL,
    value TEXT NOT NULL,          -- '' is a real edit: the user cleared the cell
    PRIMARY KEY (trip_id, column_title)
);
-- Trips the planner must leave alone: never released by a re-chain, and while
-- unassigned they draw no suggestion at all. Its own table rather than a
-- cell_overrides row, because that table IS the xlsx splice — a lock is a
-- decision about the plan, not a value the source sheet should ever carry.
CREATE TABLE IF NOT EXISTS trip_locks (
    trip_id INTEGER PRIMARY KEY REFERENCES trips(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS accepted_links (
    id INTEGER PRIMARY KEY,
    dataset_id INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    supply_trip_id INTEGER REFERENCES trips(id),   -- NULL = ad hoc fallback
    demand_trip_id INTEGER NOT NULL REFERENCES trips(id),
    accepted_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- Undo history: one row per reversible action, holding the state as it was
-- BEFORE that action. A snapshot rather than an inverse operation, because
-- accepting a plan writes 59 links at once and 'the state before' reverses
-- that as cheaply as it reverses a single cell edit. Scoped to a dataset:
-- importing replaces the working set, so nothing before it is undoable.
CREATE TABLE IF NOT EXISTS undo_log (
    id INTEGER PRIMARY KEY,
    dataset_id INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    label TEXT NOT NULL,          -- shown on the button, e.g. 'edit Status on row 412'
    state TEXT NOT NULL,          -- JSON; see Repo._snapshot
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
-- accepted_links is queried by dataset, by demand, and by (demand OR supply)
-- once per released/deleted row — without these, each is a full scan, and the
-- un-indexed trip FKs also force a parent-key scan on every trips DELETE.
CREATE INDEX IF NOT EXISTS idx_links_dataset ON accepted_links(dataset_id);
CREATE INDEX IF NOT EXISTS idx_links_demand ON accepted_links(demand_trip_id);
CREATE INDEX IF NOT EXISTS idx_links_supply ON accepted_links(supply_trip_id);
-- The working-set gate (_trip_rows) filters on dataset_id; the UNIQUE
-- (dataset_id, sheet_row) index cannot serve manual rows (sheet_row NULL).
CREATE INDEX IF NOT EXISTS idx_trips_dataset ON trips(dataset_id);
CREATE INDEX IF NOT EXISTS idx_undo_dataset ON undo_log(dataset_id, id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    # check_same_thread=False: FastAPI runs sync endpoints in a threadpool.
    # Single local user + SQLite's own serialization make this safe.
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _drop_fleet_size(conn)
    _add_trip_deleted(conn)
    return conn


def _add_trip_deleted(conn: sqlite3.Connection) -> None:
    """Add trips.deleted to databases created before row deletion existed.

    CREATE TABLE IF NOT EXISTS is a no-op on an existing table, so a new
    column has to be added explicitly or every query naming it fails against
    a database that predates it.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(trips)")}
    if "deleted" not in cols:
        conn.execute("ALTER TABLE trips ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0")
        conn.commit()


def _drop_fleet_size(conn: sqlite3.Connection) -> None:
    """Retire the stored fleet_size column on databases created before it
    became derived. Dropping rather than leaving it vestigial: a stale column
    that nothing writes is exactly how it drifted out of step with the unit
    list in the first place. Needs SQLite 3.35+ for DROP COLUMN."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(registry_companies)")}
    if "fleet_size" in cols:
        conn.execute("ALTER TABLE registry_companies DROP COLUMN fleet_size")
        conn.commit()
