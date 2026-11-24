"""JSON API. TripIds travel as 'origin:key' strings (e.g. 'sheet:123')."""
from __future__ import annotations

import dataclasses
from datetime import date, timedelta
from typing import Any, Sequence

from fastapi import APIRouter, HTTPException, Request, Response, UploadFile
from pydantic import BaseModel

from .. import engine
from ..codecs import csv_codec, paste, seed, xlsx_reader, xlsx_writer
from ..codecs.types import ALL_COLUMNS, COLUMN_ORDER
from ..engine import adhoc, rechain
from ..model import (
    AdhocReclaimResult, EngineResult, Plan, ProposedChain, RechainResult,
    Status, Suggestion, Trip, TripId,
)
from ..store.repo import Repo, RepoError

router = APIRouter()


def _repo(request: Request) -> Repo:
    return request.app.state.repo


def _tid(s: str) -> TripId:
    origin, _, key = s.partition(":")
    # isdecimal, not isdigit: isdigit accepts superscripts int() refuses,
    # turning this 422 into a 500.
    if origin not in ("sheet", "manual") or not key.isdecimal():
        raise HTTPException(422, f"Bad trip id {s!r}")
    return TripId(origin, int(key))  # type: ignore[arg-type]


def _rid(t: TripId) -> str:
    return f"{t.origin}:{t.key}"


def _where(repo: Repo, t: TripId) -> str:
    """A trip named the way the Data tab numbers it, for undo labels.

    Through the repo so an undo entry and the row on screen cannot disagree:
    a manual row's own key is a rowid nobody recognises, and the number it gets
    instead is derived (see Repo.row_labels). The 'row' word is added here
    rather than stored in the label, because the table shows the number alone
    and only prose like 'edit Status on row 412' needs the noun. Falls back to
    the key rather than failing — a label is never worth losing an action over.
    """
    label = repo.row_labels().get(_rid(t))
    return f"row {label}" if label else f"{t.origin} {t.key}"


def _sid(t: TripId | None) -> str | None:
    return None if t is None else _rid(t)


def _today() -> date:
    """Today's date — the one place the real clock is read.

    A function rather than date.today() written out at its three callers
    (_horizon, _rechain, _reclaim) so the API tests can pin it. They run
    against a reference workbook whose season is fixed, and with the real
    clock they aged out: once that season had passed, three reclaim tests
    failed and six more skipped on an untouched checkout, leaving the commit
    paths for re-chain, reclaim and the horizon untested from then on. The
    engine avoids this by taking `as_of`/`horizon` as arguments; this is the
    same idea one layer up, for the layer whose job is to supply them.

    Not a setting and not a parameter of any route: a client-supplied "today"
    would let a stale browser plan against yesterday (see Settings.horizon).
    """
    return date.today()


def _horizon(settings: dict[str, Any]) -> date | None:
    """The planner's far bound as an absolute date — None for "all".

    The stored setting is a day count and the engine takes a date, and this one
    line is the whole conversion, for the same reason `_rechain` resolves
    `as_of` here: today's date enters the system in the web layer and nowhere
    deeper, so evaluate() stays a pure function its tests can pin at both ends.

    Recomputed per request rather than cached, so a session left open across
    midnight simply plans one day further out on its next refresh.
    """
    horizon = settings["horizon"]
    if horizon == "all":
        return None
    return _today() + timedelta(days=int(horizon))


def _trip_json(t: Trip) -> dict[str, Any]:
    return {
        "id": _sid(t.id), "status": t.status.value, "project": t.project,
        "yn": t.yn, "constraint_company": t.constraint_company,
        "start": t.start.isoformat(), "end": t.end.isoformat(),
        "dep": t.dep_city, "arr": t.arr_city, "pax": t.pax,
        "assignment": (
            None if t.assignment is None
            else {"company": t.assignment.company, "unit": t.assignment.unit}
        ),
        "er": t.er_marked,
        # Every contract column verbatim, keyed by title, for the UI's column
        # picker — the parsed fields above stay the ones logic reads.
        "values": dict(t.display),
    }


def _suggestion_json(s: Suggestion) -> dict[str, Any]:
    return {
        "kind": s.kind, "supply": _sid(s.supply), "demand": _sid(s.demand),
        "company": s.company, "unit": s.unit, "gap_days": s.gap_days,
        "at_risk": [_sid(t) for t in s.at_risk], "rank": s.rank,
        "reposition": s.reposition,
        "from_city": s.from_city, "to_city": s.to_city,
    }


def _engine_json(result: EngineResult) -> dict[str, Any]:
    return {
        "suggestions_by_trip": {
            _sid(k): [_suggestion_json(s) for s in v]
            for k, v in result.suggestions_by_trip.items()
        },
        "suggestions_by_unit": {
            f"{c}|{u}": [_suggestion_json(s) for s in v]
            for (c, u), v in result.suggestions_by_unit.items()
        },
        "integrity": [
            {"company": f.company, "unit": f.unit, "earlier": _sid(f.earlier),
             "later": _sid(f.later), "kind": f.kind}
            for f in result.integrity
        ],
        "fleet_warnings": [
            {"company": w.company, "day": w.day.isoformat(),
             "count": w.count, "fleet_size": w.fleet_size}
            for w in result.fleet_warnings
        ],
        "broken_chains": [
            {"assignment": _sid(b.assignment),
             "canceled_dependency": _sid(b.canceled_dependency),
             "supply": _sid(b.link.supply)}
            for b in result.broken_chains
        ],
        "timelines": {
            f"{c}|{u}": [_sid(t) for t in ids]
            for (c, u), ids in result.timelines.items()
        },
        "adhoc_by_company": {
            c: [_sid(t) for t in ids]
            for c, ids in result.adhoc_by_company.items()
        },
        # Lets the board separate "no candidate yet" from "no chain will ever
        # reach this" — the two look identical without it. NOT a coverage
        # forecast: it ignores contention, so it far exceeds what `plan` covers.
        "reachable": sorted(_rid(t) for t in result.reachable),
        # Uncovered trips the horizon put out of scope. Without it an empty plan
        # on import day — the normal case, when the season starts more than a
        # month out — is indistinguishable from a fleet that can do nothing.
        "beyond_horizon": result.beyond_horizon,
        "plan": _plan_json(result.plan),
        # Units idle mid-chain longer than any chain window, and the uncovered
        # trips a manual drop could put there. Listed because the planner
        # cannot propose them (cross-city moves are tail-only).
        "parked": [
            {"company": w.company, "unit": w.unit,
             "after": _sid(w.after), "before": _sid(w.before),
             "city": w.city, "days": w.days,
             "fits": [
                 {"demand": _sid(f.demand), "gap_out": f.gap_out,
                  "er_out_days": f.er_out_days, "er_back_days": f.er_back_days,
                  "chains": f.chains}
                 for f in w.fits]}
            for w in result.parked
        ],
    }


def _plan_json(plan: Plan) -> dict[str, Any]:
    return {
        "objective": plan.objective,
        "idle_days": plan.idle_days,
        "covered": sorted(_rid(t) for t in plan.covered),
        "chains": [
            {
                "company": c.company, "unit": c.unit,
                "links": [
                    {"supply": _sid(link.supply), "demand": _sid(link.demand),
                     "company": link.company, "unit": link.unit,
                     "gap_days": link.gap_days, "step": link.step,
                     "reposition": link.reposition,
                     "from_city": link.from_city, "to_city": link.to_city}
                    for link in c.links
                ],
            }
            for c in plan.chains
        ],
    }


@router.get("/state")
def get_state(request: Request) -> dict[str, Any]:
    repo = _repo(request)
    settings = repo.settings()
    report, registry, accepted = repo.load_state()
    locked = repo.locked_ids()
    horizon = _horizon(settings)
    result = engine.evaluate(report, registry, accepted,
                             settings["objective"], settings["reposition"],
                             locked, None, horizon)
    return {
        "settings": settings,
        # The resolved cutoff, so the toolbar can print the date it is planning
        # through instead of making the browser redo today + N and drift.
        "horizon_through": horizon.isoformat() if horizon else None,
        # Trips the planner is told to leave alone. Sent as its own list rather
        # than a flag on each trip: it is not a property of the row, and the
        # export must never learn about it.
        "locked": sorted(_rid(t) for t in locked),
        "seeded": repo.is_seeded(),
        "seed_warnings": repo.seed_warnings(),
        # Fleet numbers the last import could not apply, plus companies whose
        # fleet cannot be written back for want of a row in the sheet.
        "sync_warnings": repo.sync_warnings(),
        "unwritable_companies": repo.unwritable_companies(),
        "dataset": repo.active_dataset_info(),
        # One shared history for every screen — see Repo.undoable.
        "undo": repo.undo_head(),
        # Drives the column picker, so the UI never hardcodes the layout.
        "columns": list(COLUMN_ORDER),
        # How every row is named on screen. Served rather than derived in the
        # client because undo labels are built from the same call — a row the
        # table calls 'new 1672' and an undo entry calls something else is one
        # row the planner has to work out twice.
        "labels": repo.row_labels(),
        # Same idea for rule 1's window: the board explains the engine's
        # verdicts and needs min_gap_days to do it (see graph.window_rules).
        "rules": engine.graph.window_rules(),
        "registry": {
            "companies": [
                {"key": key, "name": c.name, "fleet_size": c.fleet_size,
                 "note": c.note, "units": sorted(c.units)}
                for key, c in sorted(registry.companies.items())
            ],
            "cities": sorted(registry.cities.values()),
            # Pick lists for passthrough columns: dropdown source only, never
            # resolved by the contract.
            "options": repo.options(),
        },
        "trips": [_trip_json(t) for t in report.trips],
        "non_operable": [
            {"id": _sid(r.id), "reasons": list(r.reasons), "display": dict(r.display)}
            for r in report.non_operable
        ],
        "overrides": repo.trip_overrides(),
        # Chain lineage recorded by /accept. The board draws a link only
        # between trips joined here — two trips that merely happen to be
        # chainable are a coincidence, not a routing decision someone made.
        "accepted_links": [
            {"supply": _sid(link.supply), "demand": _sid(link.demand)}
            for link in accepted
        ],
        "engine": _engine_json(result),
    }


@router.post("/import")
async def import_file(request: Request, file: UploadFile) -> dict[str, Any]:
    repo = _repo(request)
    name = file.filename or "upload"
    data = await file.read()
    try:
        if name.lower().endswith(".xlsx"):
            dataset = xlsx_reader.read(data)
        elif name.lower().endswith(".csv"):
            dataset = csv_codec.read(data)
        else:
            raise HTTPException(422, "Only .xlsx and .csv files are supported")
        repo.import_dataset(dataset, name)
    except (ValueError, csv_codec.CsvError) as e:
        raise HTTPException(422, str(e)) from e

    # The Information sheet owns the fleet numbers, so every xlsx import
    # re-applies them. Ordered after import_dataset so 'is this unit still in
    # use?' is judged against the rows that just arrived — and its warnings
    # are returned, never raised: a fleet that could not be fully synced must
    # not undo an import that already succeeded.
    # The Information sheet also owns the Nationality/OP pick lists, in its
    # own columns. Synced from the same read as the fleets, and outside the
    # is_seeded() guard: a dropdown source has nothing to do with whether the
    # company registry has been seeded.
    warnings: list[str] = []
    if dataset.source_kind == "xlsx":
        try:
            seed_result = seed.read_seed(data)
        except ValueError as e:
            warnings = [f"Information sheet not read: {e}"]
        else:
            repo.sync_options(seed_result)
            # The sheet owns the city registry too, when it heads a 'Cities'
            # column. Outside the is_seeded() guard for the same reason the
            # pick lists are: the column is the sheet's statement of the list,
            # whether or not the company registry has been seeded from it.
            # Its warnings join the fleet ones — a city kept back because live
            # trips still use it is exactly as worth saying.
            warnings = repo.sync_cities(seed_result)
            if repo.is_seeded():
                warnings += repo.sync_fleets(seed_result)   # folds in seed warnings
            else:
                # sync_fleets is what normally reports the reader's own
                # warnings, so before the first seed they would be dropped —
                # and 'the Fleets column is empty' is at its most useful on
                # exactly that import, when nothing is registered yet.
                warnings += seed_result.warnings
    # Written for csv imports too (as empty): the stored warnings describe the
    # active dataset, and a csv import replaces it — keeping the previous
    # workbook's warnings on screen would describe rows that no longer exist.
    repo.set_sync_warnings(warnings)
    return {"rows": len(dataset.rows), "source_kind": dataset.source_kind,
            "warnings": warnings}


@router.get("/export.xlsx")
def export_xlsx(request: Request) -> Response:
    repo = _repo(request)
    try:
        plan = repo.build_export_plan()
        if plan.blob is not None:
            data = xlsx_writer.export(plan)
        else:
            source = repo.csv_source()
            if source is None:          # dataset has neither blob nor csv rows
                raise HTTPException(409, "Active dataset has no exportable source")
            data = csv_codec.build_fresh_workbook(*source)
    except (RepoError, xlsx_writer.XlsxPatchError) as e:
        raise HTTPException(409, str(e)) from e
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="buschain-export.xlsx"'},
    )


@router.get("/export.csv")
def export_csv(request: Request) -> Response:
    repo = _repo(request)
    try:
        source = repo.csv_source()
        if source is not None:                      # csv-sourced dataset
            text = csv_codec.write_rows_csv(*source)
        else:                                       # render from patched xlsx
            data = xlsx_writer.export(repo.build_export_plan())
            text = csv_codec.render_workbook_csv(data)
    except (RepoError, xlsx_writer.XlsxPatchError) as e:
        raise HTTPException(409, str(e)) from e
    return Response(
        content=text, media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="buschain-export.csv"'},
    )


class ManualTrip(BaseModel):
    values: dict[str, str]          # keyed by column letter, operated columns


@router.post("/trips")
def add_trip(request: Request, body: ManualTrip) -> dict[str, Any]:
    # An unknown letter would otherwise vanish silently (the repo keeps only
    # contract columns) — a typo'd key looking like a successful add with a
    # blank cell is exactly the silent drop AT2 forbids.
    unknown = sorted(set(body.values) - set(ALL_COLUMNS))
    if unknown:
        raise HTTPException(422, f"Unknown column letter(s): {', '.join(unknown)}")
    repo = _repo(request)
    try:
        with repo.undoable("add trip"):
            tid = repo.add_manual_trip(dict(body.values))
    except RepoError as e:
        raise HTTPException(409, str(e)) from e
    return {"id": _sid(tid)}


class PastedTrips(BaseModel):
    text: str                       # tab-separated rows as copied from the sheet
    # Column titles the paste carries, in paste order — the planner's
    # declaration of what their sheet hides (Excel omits hidden columns from
    # the copied text, and nothing in it says so). None = the full layout.
    columns: list[str] | None = None


@router.post("/trips/paste")
def add_pasted_trips(request: Request, body: PastedTrips) -> dict[str, Any]:
    """Add a batch of trips pasted from columns A-O of the planner.

    One undo entry for the batch, not one per row: a paste is a single action,
    and stepping back through 40 of them would also flush everything else out
    of a 50-deep stack. Rows that fail the contract are still added — they land
    in the non-operable list with their reasons, editable in place, exactly as
    they would have on import.
    """
    try:
        parsed = paste.parse(body.text, body.columns)
    except paste.PasteError as e:
        raise HTTPException(422, str(e)) from e
    repo = _repo(request)
    plural = "trip" if len(parsed.rows) == 1 else "trips"
    try:
        with repo.undoable(f"paste {len(parsed.rows)} {plural}"):
            ids = repo.add_manual_trips(parsed.rows)
    except RepoError as e:
        raise HTTPException(409, str(e)) from e
    # Which columns the paste turned out to hold, so the UI can say what was
    # read. Silence here is what let a hidden-column paste land three columns
    # out of step and look fine.
    return {"ids": [_rid(t) for t in ids],
            "columns": list(parsed.columns), "by_header": parsed.by_header,
            "overflow_rows": parsed.overflow_rows}


@router.delete("/trips/{trip_id}")
def delete_trip(request: Request, trip_id: str) -> dict[str, Any]:
    """Take a row out of the working set and out of the export.

    Not a validation decision: a row nobody can fix is already handled (it
    lands in the non-operable list and passes through untouched). This is for
    rows that should not be in the plan at all.
    """
    repo = _repo(request)
    tid = _tid(trip_id)
    try:
        with repo.undoable(f"delete {_where(repo, tid)}"):
            repo.delete_trip(tid)
    except RepoError as e:
        raise HTTPException(409, str(e)) from e
    return {"ok": True}


class TripDeletes(BaseModel):
    ids: list[str]                  # 'origin:key' row ids, operable or non-operable


@router.post("/trips/delete")
def delete_trips(request: Request, body: TripDeletes) -> dict[str, Any]:
    """Delete a ticked batch as one action — one undo entry, like paste, and
    for the same reason: stepping back through 40 per-row entries would also
    flush everything else out of a 50-deep stack. A POST carrying a body
    rather than a DELETE, because DELETE request bodies are dropped by enough
    clients and proxies to be unreliable.

    All or nothing (see Repo.delete_trips): one stale id refuses the batch,
    and a refused action leaves no undo entry.
    """
    if not body.ids:
        raise HTTPException(422, "No rows to delete")
    tids = [_tid(s) for s in body.ids]
    repo = _repo(request)
    label = (f"delete {_where(repo, tids[0])}" if len(tids) == 1
             else f"delete {len(tids)} rows")
    try:
        with repo.undoable(label):
            repo.delete_trips(tids)
    except RepoError as e:
        raise HTTPException(409, str(e)) from e
    return {"ok": True}


class CellEdit(BaseModel):
    column: str                     # column title, e.g. 'C Notes'
    value: str


@router.post("/trips/{trip_id}/cell")
def set_cell(request: Request, trip_id: str, body: CellEdit) -> dict[str, Any]:
    """Edit one cell. The value is not validated here: it is overlaid and
    re-validated through the contract, so a bad value lands the row in the
    non-operable list with a reason instead of being silently refused."""
    # An unknown TITLE is a malformed request, not a state conflict — 422,
    # like every other shape error at this boundary. The repo's own check
    # stays as the backstop.
    if body.column not in COLUMN_ORDER:
        raise HTTPException(
            422, f"{body.column!r} is not a column of the contract layout")
    repo = _repo(request)
    tid = _tid(trip_id)
    try:
        with repo.undoable(f"edit {body.column} on {_where(repo, tid)}"):
            repo.set_cell(tid, body.column, body.value)
    except RepoError as e:
        raise HTTPException(409, str(e)) from e
    return {"ok": True}


class CellEdits(BaseModel):
    values: dict[str, str]          # column title -> value


@router.post("/trips/{trip_id}/cells")
def set_cells(request: Request, trip_id: str, body: CellEdits) -> dict[str, Any]:
    """Edit several cells of one row as one action.

    The board's date drag moves Start date and End date together; see
    Repo.set_cells for why that cannot be two /cell calls.
    """
    if not body.values:
        raise HTTPException(422, "No cells to edit")
    for column in body.values:
        if column not in COLUMN_ORDER:
            raise HTTPException(
                422, f"{column!r} is not a column of the contract layout")
    repo = _repo(request)
    tid = _tid(trip_id)
    what = " and ".join(sorted(body.values))
    try:
        with repo.undoable(f"edit {what} on {_where(repo, tid)}"):
            repo.set_cells(tid, body.values)
    except RepoError as e:
        raise HTTPException(409, str(e)) from e
    return {"ok": True}


class StatusChange(BaseModel):
    status: str


@router.post("/trips/{trip_id}/status")
def set_status(request: Request, trip_id: str, body: StatusChange) -> dict[str, Any]:
    status = Status.parse(body.status)
    if status is None:
        raise HTTPException(422, f"Bad status {body.status!r}")
    repo = _repo(request)
    tid = _tid(trip_id)
    try:
        with repo.undoable(f"set {status.value} on {_where(repo, tid)}"):
            repo.set_status(tid, status)
    except RepoError as e:
        raise HTTPException(409, str(e)) from e
    return {"ok": True}


class LockChange(BaseModel):
    locked: bool


@router.post("/trips/{trip_id}/lock")
def set_lock(request: Request, trip_id: str, body: LockChange) -> dict[str, Any]:
    """Exclude a trip from all chaining, or put it back.

    Undoable like every other mutation, which is why locks are part of the undo
    snapshot: a snapshot that omitted them would silently revert locks set after
    it whenever an older action was stepped back.
    """
    repo = _repo(request)
    tid = _tid(trip_id)
    verb = "lock" if body.locked else "unlock"
    try:
        with repo.undoable(f"{verb} {_where(repo, tid)}"):
            repo.set_lock(tid, body.locked)
    except RepoError as e:
        raise HTTPException(409, str(e)) from e
    return {"ok": True}


def _bus(repo: Repo, company_key: str, unit: int | None) -> str:
    """A bus as the UI names it, for undo labels. Falls back to the key: a
    label is never worth failing an action over."""
    company = repo.registry().companies.get(company_key)
    name = company.name if company else company_key
    return name if unit is None else f"{name} {unit}"


class Accept(BaseModel):
    demand: str
    company: str
    unit: int | None = None
    supply: str | None = None


@router.post("/accept")
def accept(request: Request, body: Accept) -> dict[str, Any]:
    repo = _repo(request)
    demand = _tid(body.demand)
    try:
        with repo.undoable(f"assign {_where(repo, demand)} to {_bus(repo, body.company, body.unit)}"):
            repo.accept(
                demand, body.company, body.unit,
                _tid(body.supply) if body.supply else None,
            )
    except RepoError as e:
        raise HTTPException(409, str(e)) from e
    return {"ok": True}


def _drop_targets(
    repo: Repo, tid: TripId,
) -> tuple[list[engine.DropTarget], list[engine.DropRefusal]]:
    """Where this trip may legally be dropped, per the engine, right now.

    One function for both the question (/drop-targets, asked when the drag
    starts) and the answer (/reassign, which re-asks before writing). The
    client never tells the server which link to record — the same rule
    _current_plan follows: a board that has been open for a while must not be
    able to write a decision the engine no longer stands behind.
    """
    report, registry, _ = repo.load_state()
    return engine.drop_targets(
        report, registry, tid, repo.settings()["reposition"])


@router.get("/drop-targets/{trip_id}")
def drop_targets(request: Request, trip_id: str) -> dict[str, Any]:
    targets, refused = _drop_targets(_repo(request), _tid(trip_id))
    return {
        "targets": [
            {"company": t.company, "unit": t.unit, "supply": _sid(t.supply),
             "gap_days": t.gap_days, "successor_gap": t.successor_gap,
             "reposition": t.reposition, "chains": t.chains}
            for t in targets
        ],
        # Refusals travel with the answer so the board can say WHY a row will
        # not take the bar hovering over it, rather than only failing to light
        # it up.
        "refused": [
            {"company": r.company, "unit": r.unit, "reason": r.reason}
            for r in refused
        ],
    }


class Reassign(BaseModel):
    trip: str
    company: str
    unit: int | None = None


@router.post("/reassign")
def reassign(request: Request, body: Reassign) -> dict[str, Any]:
    """Put a trip on a bus, whichever bus it was on before.

    The supply link is derived here, not submitted: the drop says only where
    the bar landed, and the engine says what that means.
    """
    repo = _repo(request)
    tid = _tid(body.trip)
    targets, refused = _drop_targets(repo, tid)
    target = next((t for t in targets
                   if t.company == body.company and t.unit == body.unit), None)
    if target is None:
        # Quote the engine's own reason when it has one: a board that has been
        # open a while can offer a drop the state no longer allows, and "it
        # moved on" is far less use than the trip now in the way.
        why = next((r.reason for r in refused
                    if r.company == body.company and r.unit == body.unit), None)
        raise HTTPException(
            409, f"{_bus(repo, body.company, body.unit)} cannot take "
                 f"{_where(repo, tid)} — "
                 + (why or "the board has moved on since the drag started"))
    try:
        with repo.undoable(
                f"move {_where(repo, tid)} to {_bus(repo, body.company, body.unit)}"):
            repo.reassign(tid, body.company, body.unit, target.supply)
    except RepoError as e:
        raise HTTPException(409, str(e)) from e
    return {"ok": True}


def _current_plan(repo: Repo) -> Plan:
    """Recompute the plan server-side before committing any part of it.

    The client never submits the links it wants written — only which chain. A
    plan is derived state that moves whenever anything else does, so honoring a
    client's copy would let a stale board write assignments the engine no
    longer proposes.
    """
    settings = repo.settings()
    report, registry, accepted = repo.load_state()
    result = engine.evaluate(report, registry, accepted,
                             settings["objective"], settings["reposition"],
                             repo.locked_ids(), None, _horizon(settings))
    return result.plan


def _commit_chains(repo: Repo, chains: Sequence[ProposedChain], what: str) -> int:
    links = [
        (link.demand, link.company, link.unit, link.supply)
        for chain in chains for link in chain.links
    ]
    try:
        # One undo entry for the whole commit, matching the transaction: the
        # chain was accepted as a unit and comes back as one.
        with repo.undoable(f"accept {what} ({len(links)} links)"):
            return repo.accept_chain(links)
    except RepoError as e:
        raise HTTPException(409, str(e)) from e


class ChainRef(BaseModel):
    company: str
    unit: int


@router.post("/accept-chain")
def accept_chain(request: Request, body: ChainRef) -> dict[str, Any]:
    """Commit one unit's whole proposed chain, atomically and in order."""
    repo = _repo(request)
    plan = _current_plan(repo)
    chain = next((c for c in plan.chains
                  if c.company == body.company and c.unit == body.unit), None)
    if chain is None:
        raise HTTPException(409, "That chain is no longer proposed — the plan has moved on")
    return {"ok": True, "accepted": _commit_chains(
        repo, [chain], f"chain on {_bus(repo, body.company, body.unit)}")}


@router.post("/accept-plan")
def accept_plan(request: Request) -> dict[str, Any]:
    """Commit every proposed chain. One transaction: a partially written plan
    would leave chains whose tail the planner never saw."""
    repo = _repo(request)
    plan = _current_plan(repo)
    if not plan.chains:
        raise HTTPException(409, "Nothing to accept — the plan is empty")
    return {"ok": True, "accepted": _commit_chains(
        repo, plan.chains, f"the whole plan across {len(plan.chains)} chains")}


def _optimal(repo: Repo) -> tuple[Any, int]:
    """Recompute the rule-8 solve from committed state — same staleness rule
    as _current_plan and _rechain: the client never submits links, so a
    preview left open while something changed cannot commit a plan the solver
    no longer proposes."""
    settings = repo.settings()
    report, _, _ = repo.load_state()
    try:
        return engine.optimal_plan(
            report, reposition=settings["reposition"], locked=repo.locked_ids(),
            horizon=_horizon(settings), objective=settings["objective"])
    except engine.solve.SolverUnavailable as e:
        raise HTTPException(409, str(e)) from e


@router.get("/optimal-plan")
def optimal_preview(request: Request) -> dict[str, Any]:
    """What the exact solver would cover, next to what the greedy plan does.
    Writes nothing; Apply (the POST) recomputes and commits."""
    solved, greedy_covered = _optimal(_repo(request))
    return {
        "status": solved.status,
        "seconds": round(solved.seconds, 1),
        "greedy_covered": greedy_covered,
        # Links the engine's own replay refused (chain suffixes dropped with
        # them). Zero in every measured run; reported so a nonzero one is
        # visible rather than silently absorbed.
        "refused": solved.refused,
        "plan": _plan_json(solved.plan),
    }


@router.post("/optimal-plan")
def optimal_apply(request: Request) -> dict[str, Any]:
    repo = _repo(request)
    solved, _ = _optimal(repo)
    if not solved.plan.chains:
        raise HTTPException(409, "Nothing to optimize — no coverable trips")
    return {"ok": True, "accepted": _commit_chains(
        repo, solved.plan.chains,
        f"the optimal plan across {len(solved.plan.chains)} chains")}


def _rechain(repo: Repo, settings: dict[str, Any]) -> RechainResult:
    """Recompute the rearrangement from committed state.

    today's date enters the system here and nowhere deeper: the engine takes
    `as_of` as an argument so it stays a pure function of its inputs. Same
    reason _current_plan recomputes rather than trusting the client — a preview
    the browser is still holding may already be stale.
    """
    report, registry, accepted = repo.load_state()
    return rechain.build(
        report, registry, accepted,
        system_assigned=repo.system_assigned_ids(),
        locked=repo.locked_ids(),
        as_of=_today(),
        horizon=_horizon(settings),
        objective=settings["objective"],
        reposition=settings["reposition"],
    )


def _rechain_json(result: RechainResult, settings: dict[str, Any]) -> dict[str, Any]:
    return {
        "as_of": result.as_of.isoformat(),
        "horizon": result.horizon.isoformat() if result.horizon else None,
        "objective": settings["objective"],
        "reposition": settings["reposition"],
        "released": [_rid(t) for t in result.released],
        "stranded": [_rid(t) for t in result.stranded],
        "frozen_source": result.frozen_source,
        "frozen_locked": result.frozen_locked,
        "frozen_horizon": result.frozen_horizon,
        "plan": _plan_json(result.engine.plan),
    }


@router.get("/rechain")
def rechain_preview(request: Request) -> dict[str, Any]:
    """What a master re-chain would do. Writes nothing."""
    repo = _repo(request)
    settings = repo.settings()
    return _rechain_json(_rechain(repo, settings), settings)


@router.post("/rechain")
def rechain_apply(request: Request) -> dict[str, Any]:
    """Take back the releasable future assignments and write the replan.

    Recomputed here rather than taken from the request: the client sends no
    links at all, so a preview left open while something else changed cannot
    write a plan the engine has since abandoned. The response reports what was
    actually written, which is how a divergence from the preview becomes
    visible instead of silent.
    """
    repo = _repo(request)
    result = _rechain(repo, repo.settings())
    links = rechain.commit_links(result)
    if not result.released and not links:
        raise HTTPException(409, "Nothing to re-chain — no future assignment can be "
                                 "released and no uncovered trip can be chained")
    try:
        # One undo entry for the whole rearrangement, as with accept-plan: it
        # was one decision and it comes back as one.
        with repo.undoable(
                f"re-chain {len(result.released)} released, {len(links)} assigned"):
            repo.rechain(result.released, links)
    except RepoError as e:
        raise HTTPException(409, str(e)) from e
    return {"ok": True, "released": len(result.released), "accepted": len(links),
            "stranded": [_rid(t) for t in result.stranded]}


def _reclaim(repo: Repo, settings: dict[str, Any]) -> AdhocReclaimResult:
    """Recompute the ad hoc reclaim from committed state.

    today's date enters here and nowhere deeper, as in _rechain — rule 11 is
    measured from it, so the engine takes it as an argument and stays a pure
    function of its inputs.

    A preview held across midnight commits with one fewer eligible trip, and
    that is fine rather than a bug to fix: the POST recomputes, so a trip that
    has fallen inside the notice window simply is not in the links. Do not
    "solve" it by letting the client submit an as_of — that would let a stale
    browser cancel a hire the deadline has already closed.
    """
    report, registry, accepted = repo.load_state()
    result = adhoc.build(
        report, registry, accepted,
        locked=repo.locked_ids(),
        as_of=_today(),
        horizon=_horizon(settings),
        objective=settings["objective"],
        reposition=settings["reposition"],
    )
    # Provenance is a store concept, so the engine does not ask for it — but the
    # panel must be able to say how many of these hires the spreadsheet named,
    # because that is the boundary this action is allowed to cross.
    system = repo.system_assigned_ids()
    return dataclasses.replace(result, from_source=tuple(
        t for t in result.eligible if t not in system))


def _reclaim_json(result: AdhocReclaimResult,
                  settings: dict[str, Any]) -> dict[str, Any]:
    return {
        "as_of": result.as_of.isoformat(),
        "notice_days": result.notice_days,
        # Served rather than computed in JS, same rule as graph.window_rules():
        # the deadline is the engine's number and the browser must not re-derive
        # it from a constant of its own.
        "cutoff": (result.as_of + timedelta(days=result.notice_days)).isoformat(),
        # The other end of the same window. When it lands before `cutoff` the
        # two rules cross and nothing can ever be eligible — the panel needs
        # both dates to say that, rather than showing an empty list.
        "horizon": result.horizon.isoformat() if result.horizon else None,
        "objective": settings["objective"],
        "reposition": settings["reposition"],
        "eligible": [_rid(t) for t in result.eligible],
        "reclaimed": [_rid(t) for t in result.reclaimed],
        "stayed_adhoc": [_rid(t) for t in result.stayed_adhoc],
        "regressed": [_rid(t) for t in result.regressed],
        "from_source": [_rid(t) for t in result.from_source],
        "frozen_locked": result.frozen_locked,
        "frozen_pax": result.frozen_pax,
        "frozen_notice": result.frozen_notice,
        "frozen_horizon": result.frozen_horizon,
        "plan": _plan_json(result.engine.plan),
    }


@router.get("/reclaim-adhoc")
def reclaim_preview(request: Request) -> dict[str, Any]:
    """What moving cancellable ad hoc work onto the charter would do. Writes
    nothing."""
    repo = _repo(request)
    settings = repo.settings()
    return _reclaim_json(_reclaim(repo, settings), settings)


@router.post("/reclaim-adhoc")
def reclaim_apply(request: Request) -> dict[str, Any]:
    """Cancel the hires the replan can replace, and write the replan.

    Recomputed here rather than taken from the request, for the same reason as
    /rechain: the client sends no links, so a stale preview cannot write a plan
    the engine has since abandoned.
    """
    repo = _repo(request)
    result = _reclaim(repo, repo.settings())
    links = adhoc.commit_links(result)
    if not result.reclaimed:
        raise HTTPException(409, "Nothing to reclaim — no cancellable ad hoc trip "
                                 "can be moved onto a chartered bus")
    try:
        # One undo entry for the whole move. The release set is `reclaimed`, NOT
        # `eligible` — that identity is the guarantee that a hire is only ever
        # cancelled when a bus is being written in its place.
        with repo.undoable(
                f"reclaim {len(result.reclaimed)} from ad hoc, "
                f"{len(links)} assigned"):
            repo.reclaim_adhoc(set(result.reclaimed), links)
    except RepoError as e:
        raise HTTPException(409, str(e)) from e
    return {"ok": True, "reclaimed": len(result.reclaimed), "accepted": len(links),
            "stayed_adhoc": [_rid(t) for t in result.stayed_adhoc],
            "regressed": [_rid(t) for t in result.regressed]}


class Settings(BaseModel):
    mode: str | None = None
    objective: str | None = None
    # None means "leave as is" — the client patches one setting at a time.
    reposition: bool | None = None
    # A day count or "all", never a date: the client picks how far, the server
    # decides from when (_horizon). A client-submitted cutoff would let a
    # browser open since yesterday plan against a stale today.
    horizon: str | None = None


@router.post("/settings")
def set_settings(request: Request, body: Settings) -> dict[str, Any]:
    try:
        return {"ok": True, "settings": _repo(request).set_settings(
            body.mode, body.objective, body.reposition, body.horizon)}
    except RepoError as e:
        raise HTTPException(422, str(e)) from e


class TripRef(BaseModel):
    trip: str


@router.post("/unassign")
def unassign(request: Request, body: TripRef) -> dict[str, Any]:
    repo = _repo(request)
    tid = _tid(body.trip)
    try:
        with repo.undoable(f"unassign {_where(repo, tid)}"):
            repo.unassign(tid)
    except RepoError as e:
        raise HTTPException(409, str(e)) from e
    return {"ok": True}


@router.post("/undo")
def undo(request: Request) -> dict[str, Any]:
    """Step back one action, whichever screen made it."""
    try:
        return {"ok": True, "label": _repo(request).undo()}
    except RepoError as e:
        raise HTTPException(409, str(e)) from e


@router.post("/registry/seed")
def seed_registry(request: Request) -> dict[str, Any]:
    repo = _repo(request)
    info = repo.active_dataset_info()
    if info is None or info["source_kind"] != "xlsx":
        raise HTTPException(409, "Seeding needs an imported xlsx (its Information sheet)")
    plan = repo.build_export_plan()
    if plan.blob is None:
        raise HTTPException(409, "The active xlsx dataset has no stored workbook")
    try:
        result = seed.read_seed(plan.blob)
        repo.seed_from(result)
    except (ValueError, RepoError) as e:
        raise HTTPException(409, str(e)) from e
    return {"companies": len(result.companies), "warnings": result.warnings}


class CompanyBody(BaseModel):
    # No fleet_size: it is the unit count. Change the fleet by adding or
    # removing units, or by editing the Information sheet's Fleets column.
    name: str
    note: str | None = None


@router.post("/registry/companies")
def upsert_company(request: Request, body: CompanyBody) -> dict[str, Any]:
    try:
        _repo(request).upsert_company(body.name, body.note)
    except RepoError as e:
        raise HTTPException(422, str(e)) from e
    return {"ok": True}


class RenameBody(BaseModel):
    name: str


@router.post("/registry/companies/{key}/rename")
def rename_company(request: Request, key: str, body: RenameBody) -> dict[str, Any]:
    """Rename a company and carry the new name through the working set.

    Undoable, and it has to be: this rewrites every Company name and Bus
    Preference cell that named the company — hundreds of them on real data —
    and the undo snapshot covers the registry alongside the cells, so stepping
    back cannot leave the two disagreeing (Repo._snapshot).
    """
    repo = _repo(request)
    try:
        with repo.undoable(f"rename {key} to {body.name}"):
            cells = repo.rename_company(key, body.name)
    except RepoError as e:
        # 409 like delete_company's refusals: both are conflicts with state
        # (unknown company, or a name already taken), not malformed input.
        raise HTTPException(409, str(e)) from e
    return {"ok": True, "cells": cells}


@router.delete("/registry/companies/{key}")
def delete_company(request: Request, key: str) -> dict[str, Any]:
    try:
        _repo(request).delete_company(key)
    except RepoError as e:
        raise HTTPException(409, str(e)) from e
    return {"ok": True}


class UnitBody(BaseModel):
    unit: int


@router.post("/registry/companies/{key}/units")
def add_unit(request: Request, key: str, body: UnitBody) -> dict[str, Any]:
    try:
        _repo(request).add_unit(key, body.unit)
    except RepoError as e:
        raise HTTPException(422, str(e)) from e
    return {"ok": True}


@router.delete("/registry/companies/{key}/units/{unit}")
def remove_unit(request: Request, key: str, unit: int) -> dict[str, Any]:
    try:
        _repo(request).remove_unit(key, unit)
    except RepoError as e:
        # In-use refusals are state conflicts, like delete_company's and
        # remove_city's identical guards.
        raise HTTPException(409, str(e)) from e
    return {"ok": True}


class CityBody(BaseModel):
    code: str


@router.post("/registry/cities")
def add_city(request: Request, body: CityBody) -> dict[str, Any]:
    try:
        _repo(request).add_city(body.code)
    except RepoError as e:
        raise HTTPException(422, str(e)) from e
    return {"ok": True}


@router.delete("/registry/cities/{code}")
def remove_city(request: Request, code: str) -> dict[str, Any]:
    try:
        _repo(request).remove_city(code)
    except RepoError as e:
        raise HTTPException(409, str(e)) from e
    return {"ok": True}


class OptionBody(BaseModel):
    column: str                     # column title, e.g. 'Nationality'
    value: str


@router.post("/registry/options")
def add_option(request: Request, body: OptionBody) -> dict[str, Any]:
    try:
        _repo(request).add_option(body.column, body.value)
    except RepoError as e:
        raise HTTPException(422, str(e)) from e
    return {"ok": True}


@router.delete("/registry/options/{column}/{value}")
def remove_option(request: Request, column: str, value: str) -> dict[str, Any]:
    _repo(request).remove_option(column, value)
    return {"ok": True}
