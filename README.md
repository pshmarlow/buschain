# buschain — bus trip chaining system

Routes and chains trip data so buses never drive empty. The Excel planner
(`Capitals 2026` layout) is the interchange format: **formatting compatibility
yes, content repair no.** Rows failing the data contract are listed as
non-operable with reasons and pass through untouched. See
`bus-chaining-build-brief.md` for the full spec.

## Run

```bash
python3 -m venv .venv
./.venv/bin/pip install -e '.[dev]'
./.venv/bin/python -m buschain.web     # http://127.0.0.1:8000
```

Windows — `py -3`, not `python3`: the latter is an app-execution alias that
opens the Microsoft Store instead of running anything. Double quotes, because
`cmd.exe` does not strip single ones.

```bat
py -3 -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
.venv\Scripts\python -m buschain.web
```

First use: import the planner xlsx on the **Data** tab, then press
**Seed from Information sheet** on the **Registry** tab (one-time; the
registry is maintained in-app afterwards — check the seed warnings for
companies it refused to guess at, e.g. the 'Nordi Bus' alias).

The **Suggestions** tab shows the plan: multi-hop chains the fleet could run,
grouped by bus. Two modes — *manual* offers accept buttons (whole chain, single
link, or the entire plan); *auto* keeps the plan live on the board without
them. Two objectives — *most trips covered* or *fewest idle days*; switching
replans. **Empty runs** can be switched off, which restricts every proposal —
chains, one-hop candidates, board ghost slots alike — to trips departing from
the city the bus last arrived in; on the reference dataset that is 108 links
down to 60. Either way nothing is written until you accept, and provisional
links never appear in an export. **Optimize coverage…** runs the exact solver
over the same rules (a few seconds) and previews the best achievable plan next
to the greedy one — on the reference data 124 covered vs 113, at the cost of
more empty runs, which the preview states before you apply.

On the **Timelines** board, selecting a bar opens its panel; **Show in Data**
(shortcut: `d`) jumps to that trip's row in the Data tab, clearing the Data
filters first if they were hiding it.

State lives in `buschain.db` (SQLite), created in **whatever directory you
start the server from** — not next to the package. Start it somewhere else and
you get a different, empty database rather than an error, so launching by
shortcut or from an elevated shell is worth checking. Keep it on a local disk:
SQLite's WAL mode needs a memory-mapped `-shm` file that network shares do not
provide, and a heavily synced folder (OneDrive, Dropbox) sees the `.db`, `-wal`
and `-shm` as three files to upload at three different moments, so restoring
from version history can reconstitute a torn database.

## Tests

```bash
./.venv/bin/pytest            # unit + all 7 acceptance tests from the brief
.venv\Scripts\pytest          # Windows
```

The reference workbook `LDC_PLANNER__test.xlsx` is the golden fixture:
acceptance test 1 asserts that an export with no in-system changes is
**byte-for-byte identical** to the imported file.

## Architecture (strict one-way layering)

```
web  →  store  →  codecs  →  model
                engine   →  model
```

| package | role |
|---|---|
| `buschain/model.py` | frozen dataclasses/enums; zero I/O |
| `buschain/normalize.py` | the four allowed normalizations, nothing else |
| `buschain/contract.py` | one home for validation: raw row → Trip \| NonOperableRow(reasons) |
| `buschain/engine/` | pure `evaluate(report, registry, accepted, objective, reposition, locked, as_of) → EngineResult`; greedy ranking + multi-hop chain planning over a feasible-link graph, plus `solve.py`: the same graph handed to an exact solver (CBC, optional `.[solver]` extra) behind the on-demand **Optimize coverage…** action |
| `buschain/codecs/` | xlsx/csv importers + exporters behind neutral shapes (`ImportedDataset` in, `ExportPlan` out); swap/add codecs without touching anything else |
| `buschain/store/` | SQLite; the only mutable state — every mutation re-runs the engine |
| `buschain/web/` | thin FastAPI JSON API + one static page (6 tabs; the last, **Manual**, is `static/manual.html` — prose fetched on first open, rendering nothing from state) |

Key decisions worth knowing before editing:

- **Export never goes through openpyxl.** `codecs/xlsx_writer.py` patches the
  original zip byte-for-byte, splicing only the cells the system wrote — the
  export plan is exactly the `cell_overrides` table, and any contract column
  can carry an edit (columns are named handles derived from `COLUMN_ORDER`,
  never spelled as letters, since the layout has shifted once) — openpyxl's
  save would destroy the workbook's comments part, inline strings, and cached
  formula values.
- **In-system changes are overlays.** The store keeps imported values
  verbatim and overlays status/assignment strings at load time, so they are
  re-validated by the same contract as imported data and exported exactly
  as stored.
- **Importing keeps the last five workbooks, not all of them.** An import does
  not delete the dataset it replaces — it flips `active` and inserts a new one —
  and every xlsx import also banks a verbatim copy of the workbook in `blobs`.
  Nothing reads an inactive dataset (`active_dataset_id()` gates every read path
  and no route exposes an older one), so left alone the file grew ~2.3 MB per
  import forever: nine imports produced a 17 MB database that was 92%
  unreachable. `Repo._prune_datasets` runs at the end of every import and drops
  everything outside `DATASET_RETENTION = 5`, then `VACUUM`s — without which the
  freed pages sit on the freelist and the file stops growing but never shrinks.
  Five rather than one because the blob archive is the only copy of a workbook
  the app has ever seen and has already served as a recovery path (the golden
  test fixture was recovered from it); a handful of imports costs ~10 MB and
  keeps that hatch open. What goes with a pruned dataset includes its
  `cell_overrides` and `accepted_links` — real work, but work that was already
  unreachable; the window is what buys back.
- **ER (column M) is read only to label** an already-detected integrity gap
  as `explicit_er`. It is never a matching input and never written.
- **Pasting rows is a codec, not a form.** `codecs/paste.py` takes tab-separated
  rows copied out of columns A-O and decodes them the way `xlsx_reader` decodes
  a day serial: the clipboard holds a *rendering* of the sheet, so `1-Apr-26`
  (the sheet's own `d-mmm-yy` format) becomes `2026-04-01`. It is not the date
  repair the contract forbids — a rendering that means more than one day
  (`04/05/2026`) is passed through verbatim so the contract fails the row and
  quotes the value, rather than being guessed at. The other thing the clipboard
  renders away is *which columns you copied*: Excel omits hidden columns from
  the text it writes, so a positional read shifts every value after the gap
  left — and when the hidden columns sit after 'Arrival', the shifted row
  still *validates*, so the contract is no safety net here. The fix is
  declaration, not detection: the paste form carries a ticked-columns picker
  (persisted in the browser) that says what the paste contains, the fields are
  read in that order, and the response names the columns that were and were
  not read plus any rows overflowing the declared width. A pasted header row,
  if present, is detected and overrides the ticks — per-paste evidence beats a
  standing setting — but no header is required. Everything downstream is
  ordinary: rows go in as manual trips, are validated by the same contract as
  imports, and failures land in the non-operable list. The whole paste is one
  undo entry, since a batch is one action and 40 of them would flush a
  50-deep stack.
- **Manual rows are named for their export row.** The Data tab labels a
  hand-added trip `new 1672` — the sheet row it will occupy on export (the
  rule `_append_rows` follows), served from `Repo.row_labels` so the table and
  the undo labels cannot disagree. The label is derived, not stored: deleting
  a sheet row below the data renumbers it, which beats freezing a number the
  exported file then contradicts. Its internal id stays the SQLite rowid.
- **The Information sheet owns the Nationality/OP pick lists**, in its own
  columns O and P — the same arrangement as the `Fleets` column. Import
  replaces the stored list from there; export writes the current list back,
  creating the column and its header if the workbook predates them. So a
  workbook's *first* export legitimately grows two columns and is the one
  export that is not byte-identical; every one after it is, because the file
  is then a fixed point (AT1 covers both halves). The lists are still only
  dropdown sources — the contract never validates against them, so pruning a
  typo can never knock a row out of the operable set. A column the sheet does
  not head is left alone rather than read as empty, and only then does the
  list bootstrap itself from values in the trip data.
- **Nothing is auto-assigned.** The engine plans and ranks; a human accepts,
  and acceptance writes the assignment.
- **The board can assign outside the plan.** Every other accept control renders
  a candidate the engine proposed, which means a fully-planned dataset — where
  the planner has no feasible link left — offers nothing to click anywhere on
  screen. Selecting an uncovered trip therefore also offers a bus picker over
  the whole registry, deliberately unfiltered: `repo.accept` has always taken
  `supply=None`, and the rules are enforced *downstream*, by re-validating
  through the contract and reporting whatever the assignment breaks in the
  integrity check. Refusing the action instead would leave a planner with a
  bus, a trip, and no way to say so. It records no chain provenance, so no
  connector is drawn and `broken_chains` has nothing to hang off.
- **The plan is derived state, never stored.** `engine/plan.py` builds
  multi-hop chains by running the feasible-link graph forward — a covered trip
  becomes supply for the next hop. It is recomputed on every `evaluate()` and
  written nowhere, which is what keeps provisional links out of
  `cell_overrides` and therefore out of the xlsx export. Confirming a chain is
  the ordinary accept path; a confirmed assignment is then real supply, so the
  planner routes around it and ordinary replanning can never move it — which is
  why locks are *not* what protects an assignment from the planner.
- **A lock holds a trip out of chaining; it does not park its bus.** A locked
  trip leaves the demand set entirely — no suggestion, no plan link, nothing to
  accept — and a master re-chain refuses to release it. It keeps its place on
  its unit's timeline and, when assigned, still feeds the next hop: locking says
  "do not re-decide this trip", not "pretend this bus is not there". The
  alternative reading (no link on either side) would dead-end the unit and is a
  materially different feature. Stored in its own `trip_locks` table rather than
  as a cell override, because `cell_overrides` *is* the xlsx splice and a lock
  is a decision about the plan, not a value the sheet should carry. It is part
  of the undo snapshot, so lock/unlock steps back like anything else.
- **The master re-chain is the only thing that takes an assignment back.**
  `engine/rechain.py` restates the world as if the releasable future
  assignments had never been made and hands that to the ordinary `evaluate()` —
  one planner, one set of rules. Three boundaries decide what "releasable"
  means, and none is negotiable: only assignments **this app wrote** (a company
  name from the spreadsheet is source data, the same boundary `repo.unassign`
  enforces one row at a time, and only where the imported cell was blank — an
  override sitting *on top* of a sheet value would not free the trip, it would
  restore the sheet's); only trips that have **not started yet**; and never a
  locked one. On the reference data that is 60 releasable against 250 future
  assignments frozen by the source-file rule.
- **A re-chain is previewed because coverage is not monotone under a release.**
  Releasing N assignments does not return N: the replan can spend a bus on a
  trip the old arrangement left uncovered and strand the one it took the bus
  from. Measured on the reference data — coverage objective with empty runs
  allowed, 46 → **113** covered and nothing stranded; the idle objective with
  empty runs off, **18 of 60** released trips came back with no bus at all. So
  `RechainResult.stranded` is part of the result rather than something a caller
  has to think to derive, and the UI states it before the gains. Applying is one
  transaction and one undo step: a half-applied re-chain would be assignments
  taken back with nothing put in their place.
- **Today's date enters at the web layer, never in the engine.** The horizon is
  an explicit `as_of` argument. A planner that consulted `date.today()` itself
  would stop being a pure function of its inputs — the same state would produce
  different plans on different days and no test could pin either.
- **One planned link per unit per round.** Edges are validated against a unit's
  timeline as it stood at round start, so taking two for the same bus in one
  round can land overlapping trips on it. Removing this rule produced 132 new
  integrity violations against a baseline of 40 on the reference data.
- **Repositioning is proposed, never assumed.** A chain only ever extends from
  its tail, so a bus parked where nothing departs within rule 1's 1–4 day window
  leaves the model permanently — on the reference data that was 12 of 15 units
  and a plan of *zero* links. `feasible_edges(..., reposition=True)` also emits
  links where the bus is driven empty to the departure city, allowed only off a
  chain's tail (mid-chain it would strand whatever comes next). Both ranking
  keys lead with `reposition`, so every free link is placed before any link that
  costs an empty run: they are spent only on demand nothing else can serve.
  Coverage went 0 → 51 of 131 trips for 28 empty runs. There are no distances in
  the registry, so the model cannot price a run — "fewest, last resort" is the
  only defensible policy until it can.
- **Last resort takes a deferral, not just a sort.** Ranking places same-city
  edges first, but the one-per-unit rule can spend a demand's same-city bus on an
  earlier demand — and the loop then reaches that demand with only its empty run
  left. On the reference data 10 of 26 empty runs went to demands that had a free
  option *in that very round*. `build_plan` now defers those to the next round
  instead, which covers **more**, not less: coverage 108 → 113 links on 26 → 25
  empty runs, idle 101 → 109 on 25 → 25 — the bus an empty run would have tied up
  stays free for work only it can do. The one-hop list in `suggest.py` is
  deliberately *not* filtered this way: it shows a human every option, already
  ranked free-first, and removing one would remove a choice rather than a cost.
- **The empty-run switch governs the whole engine, and the UI with it.** One
  `plan_reposition` setting reaches both `feasible_edges` and `build_plan`: a
  ghost slot the board offers while the planner refuses to chain it would be the
  same setting disagreeing with itself. It also narrows the *explanations* — with
  empty runs off, `unitIdleReason` measures only same-city candidates, because
  reporting a cross-city trip as "3 days short for the drive" names a miss the
  planner never looked for. The one place it does not narrow is the manual
  override in `nextTrips`, which still lists cross-city trips, labelled: it is
  the hatch for when the engine proposes nothing, and filtering it would defeat
  the point.
- **An accepted empty run is a record, not damage.** Rule 6 calls a cross-city
  pair a `city_mismatch`; when an accepted link explains it, `unit_integrity`
  labels it `reposition` instead — listed like `explicit_er`, not reported as a
  violation. The ER column stays read-only; the accepted link carries the
  intent.
- **The UI counts free days; the engine counts gaps.** `gap_days` is
  arrival-day to departure-day, so it is always one larger than the blank
  columns between two bars on the board — labelling a bus that arrives the 3rd
  and leaves the 7th "4d" read as an off-by-one bug. Every day count on screen
  is therefore `freeDays(gap) = gap - 1`, and `gap_days` never reaches a label;
  it stays what ranks links in `plan.py`. That makes three accountings, all
  deliberate: **gap** (ranking: which bus is free first), **free days**
  (display: what you can see), and `Plan.idle_days` (the objective: parked days
  only, excluding an empty run's travel). free = idle + driving, and the plan
  summary derives the driving term from the server's `idle_days` rather than
  recomputing it, so the two cannot drift.
- **Rule 1's window is served, not mirrored.** `graph.window_rules()` ships the
  gap floors and the near-pair table to the browser in `/state`. The board only
  ever explains the engine's verdict, and it needs `min_gap_days` to say how
  many of a link's free days go to driving — a copy of that table in JS would be
  a second place for the map to be wrong.
- **Reachability is not a coverage forecast.** `engine/reach.py` ignores
  contention by design; it reports far more trips than the fleet can absorb.
  It also ignores repositioning, so it is *not* a superset of `plan.covered` —
  it is the stronger claim "reachable without spending an empty run".
  `EngineResult.plan` is the answer to "what can we actually run".
