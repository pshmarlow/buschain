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

### Using it on Windows

One prerequisite, once per PC: **Python 3.11 or newer** from
https://www.python.org/downloads/, with **Add python.exe to PATH** ticked in
the installer. The launcher can't install Python for you, but it says so in
plain words if it's missing.

Then:

1. Copy (or clone) the whole `buschain` folder onto the machine — somewhere
   local, **not** inside OneDrive. `C:\dev\buschain` is a good choice.
2. **Double-click `start-buschain.bat`.**
3. To keep it off the desktop hunt: right-click that file → **Send to →
   Desktop (create shortcut)**, and pin the shortcut to the taskbar if you
   like.

The first double-click creates the virtualenv and installs everything — a few
minutes, and it needs an internet connection. Every double-click after that
starts straight up. Either way it opens the browser on http://127.0.0.1:8000
once the port is actually answering.

The console window it opens *is* the server — closing it stops the app. Double-
clicking again while it is already running just opens the browser again instead
of failing. And if an install is ever interrupted — no network, a proxy — the
next double-click finishes it instead of failing forever.

#### Doing the setup by hand

Only needed if you're working on the code. `py -3`, not `python3`: the latter
is an app-execution alias that opens the Microsoft Store instead of running
anything. Double quotes, because `cmd.exe` does not strip single ones.

```bat
git clone https://github.com/pshmarlow/buschain.git
cd buschain
py -3 -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
.venv\Scripts\python -m buschain.web
```

Two things specific to a Windows run:

- **`start-buschain.bat` exists because the directory matters.** `DEFAULT_DB`
  is relative, so `buschain.db` is created in *whatever* directory the server
  is launched from — start it elsewhere and you silently get a new empty
  database rather than an error. The launcher pins the directory to its own
  folder, so this only bites if you start the server by hand from a shell.
  The relative path itself is deliberately left unchanged, since fixing it
  would move an existing database.
- **Keep it outside OneDrive.** On Windows 11, Known Folder Move puts
  `Documents` and `Desktop` under sync by default, and the `.db`/`-wal`/`-shm`
  trio doesn't survive being synced as three independent files. `C:\dev\buschain`
  or similar. The launcher warns when it finds itself in a synced folder, but
  it only warns — it still starts.

First use: import the planner xlsx on the **Data** tab, then press
**Seed from Information sheet** on the **Registry** tab (one-time; the
registry is maintained in-app afterwards — check the seed warnings for
companies it refused to guess at, e.g. the 'Nordi Bus' alias).

The **Suggestions** tab shows the plan: multi-hop chains the fleet could run,
grouped by bus, each with accept buttons (whole chain, single link, or the
entire plan). Three settings decide what is planned, and switching any of them
replans. Two objectives — *most trips covered* or *fewest idle days*. **Empty
runs** can be switched off, which restricts every proposal — chains, one-hop
candidates, board ghost slots alike — to trips departing from the city the bus
last arrived in; on the reference dataset that is 113 links down to 60. **Plan
ahead** (2 weeks / 1 month / 3 months / All, default a month) bounds how far
out anything is proposed, and prints the date it resolves to. Whatever the
settings, nothing is written until you accept, and provisional
links never appear in an export. **Optimize coverage…** runs the exact solver
over the same rules (a few seconds) and previews the best achievable plan next
to the greedy one — on the reference data 124 covered vs 113, at the cost of
more empty runs, which the preview states before you apply.

On the **Timelines** board, selecting a bar opens its panel; **Show in Data**
(shortcut: `d`) jumps to that trip's row in the Data tab, clearing the Data
filters first if they were hiding it. Bars can be dragged — sideways to move a
trip's dates, up or down to move it to another bus or take one off. **Show
plan on buses** draws each planned trip, still hatched, on the bus that would
run it instead of in the Uncovered row; it changes the picture, not the plan
(it was once a Manual / Auto "mode" on Suggestions). Below
the board three panels report fleet over-commitment, unit-integrity findings,
and **Parked buses**: units idle mid-chain longer than any chain window, with
the uncovered trips a drop could put there and the empty-run days each would
cost. Back on **Suggestions**, **Re-chain future…** and **Reclaim from ad
hoc…** are the two bulk actions, both preview-first. The **Worklist** tab
collects assignments broken by a cancellation, and the last tab is the
built-in **Manual**.

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

The workbooks the suite reads live in `excels/`. The reference workbook
`excels/LDC_PLANNER__test.xlsx` is the golden fixture: acceptance test 1
asserts that an export with no in-system changes is **byte-for-byte
identical** to the imported file. `excels/CAPITALS WINTER.xlsx` is the second
one — a new season's workbook, header only, no trips.

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
| `buschain/engine/` | pure `evaluate(report, registry, accepted, objective, reposition, locked, as_of, horizon) → EngineResult`; greedy ranking + multi-hop chain planning over a feasible-link graph, plus `solve.py`: the same graph handed to an exact solver (CBC, optional `.[solver]` extra) behind the on-demand **Optimize coverage…** action; `parked.py` enumerates the mid-chain idle windows the tail-only rule hides (listed, never planned) |
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
- **Deleting a row is soft, and undo is a snapshot.** A deleted row leaves the
  working set and the export, but its content stays so the deletion reverses
  like everything else. Undo is one shared 50-step history per dataset — a
  snapshot of overrides/links/locks/deletions/manual rows/company naming taken
  *before* each action, not an inverse operation — and an import starts a
  fresh history: nothing from before it is undoable.
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
- **Pax is a hard constraint, not a note (rule 10).** The chartered coaches
  seat `graph.CHARTERED_PAX` (49), so a trip stating any other party size can
  only go to an ad hoc bus — every gate that hands a trip to a numbered unit
  applies `numbered_unit_ok`, and the board refuses the drop by name. Two
  choices inside it: a **blank** Pax passes (unstated is not "not 49" — paste
  and manual entry produce blank rows constantly, and the other reading would
  silently strip them out of the fleet's reach), and the rule governs what a
  bus may **pick up**, not what it is already carrying, so an imported non-49
  trip on a numbered unit stays supply and is *listed* as `pax_mismatch` rather
  than refused. That finding names one trip, so it carries the same id in
  `earlier` and `later`. Free on the reference workbook: all 16 of its non-49
  rows are already ad hoc, so coverage stays at 113/190 with zero findings.
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
- **Manual rows are numbered for their export row.** The Data tab gives a
  hand-added trip `1672` — the sheet row it will occupy on export (the rule
  `_append_rows` follows), served from `Repo.row_labels` so the table and the
  undo labels cannot disagree. The label is the bare number: it is the row's
  identity, and only prose adds the noun (`api._where` writes "row 1672" for
  an undo entry). Sheet and manual numbers cannot collide, since a manual row
  always numbers above the last sheet row. Derived, not stored: deleting a
  sheet row below the data renumbers it, which beats freezing a number the
  exported file then contradicts. Its internal id stays the SQLite rowid.
- **The Data table's sort has three rules that are not the obvious ones.**
  Blanks sort last in *both* directions, because an empty cell is missing
  rather than least — so the direction is applied inside `compareText`, not by
  negating it at the call site, which used to flip the blank branch too.
  Ascending on *Row* reproduces the unsorted order, which is the source file's
  own (`Repo._trip_rows`, `ORDER BY id`) — free now that the label is a bare
  number, but it needed a separate sort key while the cells read `row 12` and
  `new 1672`, which as text put every pasted row above row 2 on nothing but
  `"new" < "row"`. And hiding the sorted column clears the sort, as hiding a
  filtered column already cleared its filter — otherwise `viewRows` finds no
  column, silently drops to source order, and the sort comes back on unhide.
- **On the board, hue is the status and nothing else.** Green Confirmed,
  yellow Pending, red Canceled, in every lane — set as `--bar-bg`/`--bar-fg`/
  `--bar-line`/`--bar-stripe` by the status class that `tripBar` puts on every
  bar unconditionally. Everything else a bar must say is said without hue:
  provisional plans keep their diagonal hatch but cut it from the status
  colour, an empty run tightens the hatch and dots the border, and the
  uncovered lane's three-way split (actionable / opens up later / no chain
  reaches it) runs solid-bold → dashed → dotted-and-faded. Lanes differ only
  in gamma, written out per status rather than applied as a `filter()`, which
  would also recolour `.selected`'s ring and `.overlap`'s outline. Before
  this, the same Confirmed trip was green on a unit row, orange in the
  uncovered lane and purple in ad hoc — the purple survives on the ad hoc
  row band, where section identity belongs.
- **A 370-day trip is a typo, and the contract refuses it.**
  `contract.MAX_TRIP_SPAN_DAYS = 60` is the one *judgment* in a module whose
  every other reason is objective, and it earns its place by measurement: the
  reference workbook's 1594 sound rows span 0–11 days, its 76 typo'd-year rows
  span 366–376, and nothing falls in between, so any threshold in that 355-day
  gap separates them exactly. Before it those 76 passed every check and were
  fully operable — invisible to every report, yet sorted above every real trip
  by date, and actively poisoning the two reports they did reach: a 370-day
  trip holds a numbered unit for a year, which was the sole cause of **all 20**
  fleet over-commitment warnings and **all 4** overlap findings on the
  reference data. Refusing them left coverage untouched (113 greedy, 124
  solver, 190 demand) and took those to 0 and 0. It is refusal, not repair: the
  date stays exactly as the sheet has it, the row is listed with its span and
  the limit, and editing the cell re-validates it straight back (the store
  re-runs the contract on every load). One consequence to know: no real row now
  exercises `_unit_timeline_ok`'s supply-overlap tolerance, so
  `test_links.py::test_trip_overlapping_the_supply_does_not_block` covers it
  instead. A per-row check is also the only kind that works here — the board
  banner this replaced triggered on the board's *total* extent exceeding 1100
  days, which the reference data's 576 never reached, so the warning written
  for exactly these rows had never once fired.
- **The Information sheet owns three list columns**: Nationality (O), OP (P)
  and Cities (Q) — the same arrangement as the `Fleets` column, and all three
  located by header rather than by letter so a column inserted to the left
  cannot repoint the reader (or, worse, get the list written back over
  someone else's data). Import replaces the stored list from there; export
  writes the current list back, creating the column and its header if the
  workbook predates it. So a workbook's *first* export legitimately grows
  those columns and is the one export that is not byte-identical; every one
  after it is, because the file is then a fixed point (AT1 covers both
  halves). A column the sheet does not head is left alone rather than read as
  empty — absent and empty must never collapse into one state, or a workbook
  that says nothing would wipe the list.
- **Cities are on that sheet but are not a pick list.** Nationality and OP are
  dropdown sources the contract never validates against, so pruning a typo
  there can never knock a row out of the operable set. `Departure`/`Arrival`
  *are* resolved against the city registry, so `Repo.sync_cities` cannot
  simply honour a deletion the way `sync_options` does: a code still used by
  live trips is kept and reported, exactly as `sync_fleets` keeps a unit that
  still carries trips. Deleting one for real stays a Registry-tab action,
  where `remove_city` names the affected trips first. Cities live in
  `registry_cities`, never in `registry_options`, and `CITY_HEADER` is
  deliberately excluded from `INFO_OPTION_COLS` so the two paths cannot be
  confused. `seed.DEFAULT_CITIES` is now only the fallback for a workbook
  that heads no Cities column.
- **The trip sheet is the first tab, whatever it is called, and is proved by
  its header.** The tab is renamed every season (`Capitals 2026` →
  `CAPITALS`), and finding it by name — from a list of accepted names, which
  is how this used to work — made every new season a refused import until its
  name had been added to the code. Position is what stays put. All three
  codecs apply the one rule: the reader takes tab 1, the xlsx writer patches
  tab 1 (the first `<sheet>` in `workbook.xml`), the csv export renders tab 1,
  and `test_xlsx_reader.py` pins them to each other, because a writer that
  disagreed with the reader would splice assignments into a tab the trips were
  never read from. The row-1 layout check is unchanged and is now the *only*
  thing that says the tab is the right one: a workbook with some other tab in
  front — a notes sheet, a chart, a hidden tab — is refused with that tab
  named and the others listed, never read one tab off. It is deliberately not
  rescued by searching the later tabs for the header; the fix is to drag one
  tab in Excel, and a rule with a fallback is two rules. The same check is
  still the tripwire that stops a shifted column importing every value one
  slot off. The `Information` sheet is the opposite case and *is* found by
  name: nobody promised it a position, and its name does not change.
- **Renaming a company rewrites the data, not just the registry.** A trip
  carries its company as *text* (`Ollex 1` in Company name, `NO Ollex` in Bus
  Preference) and the contract resolves that text, so moving only the
  canonical key would stop those cells resolving and drop every one of the
  rows out of the operable set — silently, which is exactly what
  `delete_company` refuses to allow. `Repo.rename_company` therefore rewrites
  the cells as overrides in the same transaction; on the reference data
  renaming Ollex touches 216 of them and the operable count does not move.
  Three consequences worth knowing. Cells the app had never touched acquire
  an override, so an export after a rename is no longer byte-identical to the
  file that was imported — correctly, and re-importing that export is what
  keeps the rename stable. `_snapshot` carries the registry's naming
  alongside the cells, because an undo that restored one without the other
  would leave every row resolving against nothing. And
  `registry_companies.former_keys` records the old key so the Information
  sheet's `Bus Companies` cell — matched *by name* to decide where a fleet
  count is written back — is rewritten too, rather than the company quietly
  dropping out of that round-trip. Renaming onto an existing company is
  refused: that is a merge, and their units and trips differ.
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
- **Dragging a bar refuses the impossible, not the suboptimal.** A trip dragged
  up or down the board moves to another bus; sideways it moves its own dates.
  The two are locked to one axis per gesture, because they are different kinds
  of decision — one records a routing choice, the other edits source data the
  export splices — and a diagonal drag would have to mean both at once inside a
  single undo step. The vertical drop is gated by `graph.insertion_ok`, which is
  `feasible_edges` asked from the timeline's side: `feasible_edges` walks supply
  trips, so it can only ever name a bus that already has work to hand over, and
  it would refuse every parked one. The line `insertion_ok` draws instead is the
  brief's own (acceptance criterion 3: every consecutive pair satisfies the
  rules **or is enumerated**) — it refuses running two trips at once, departing
  before the bus could have arrived (`min_gap_days`, empty run included), and a
  company rule 3 forbids; it permits the rest and leaves rule 1's three-rest-day
  ceiling to `checks.unit_integrity`. Measured, not assumed: 34 of the reference
  workbook's 183 consecutive pairs are already wider than the window, so a drop
  refusing them would refuse about a fifth of what the sheet in front of the
  planner records. A placement inside the window records a chain link exactly as
  an accepted suggestion would; a wider one records the assignment alone, and
  the note under the pointer says so before the drop. `test_links.py` pins the
  two rulebooks together on every timeline a bus could actually run, and names
  the two corners where they diverge on timelines that were already impossible.
  The client sends only *where* the bar landed — `/reassign` re-derives what
  that means, so a board left open cannot write a decision the engine no longer
  stands behind. An imported bus can be dragged like any other: see the
  re-chain bullet below for why that is consistent with the planner never
  touching one.
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
  means, and none is negotiable: only assignments **this app wrote**
  (`repo.system_assigned_ids`, and only where the imported cell was blank — an
  override sitting *on top* of a sheet value would not free the trip, it would
  restore the sheet's); only trips that have **not started yet**; and never a
  locked one. On the reference data with the full plan accepted (measured
  2026-08) that is 113 releasable, with 56 future source-file assignments —
  and every ad hoc booking — left untouched. This is a rule about **automation**,
  not about who may edit what: a person can retype or clear an imported bus
  from the Data tab, and (since the drag) from the board — both write the same
  Company-cell override, and clearing one exports a blank where the sheet named
  a bus. What must never happen is a bulk replan silently overwriting what the
  spreadsheet said, which is why the boundary lives in `system_assigned_ids`
  rather than in `repo.unassign`, where it used to sit as a side effect of
  clearing-by-deleting-the-override. A fourth boundary joined the three above
  with rule 11: a re-chain releases **numbered** assignments only, because
  releasing an ad hoc one cancels a purchase — see the next bullet.
- **Reclaiming ad hoc work is the one automation allowed past the source-data
  boundary (rule 11).** An ad hoc assignment used to be a dead end: it is
  chain-invisible *and* not demand, so work given to a hired-in bus never came
  back however much chartered capacity freed up later. `engine/adhoc.py` strips
  the cancellable hires and hands the result to the ordinary `evaluate()`, and
  it does **not** ask who wrote the company name — unlike every other automation
  here. The justification is that an ad hoc booking is a cancellable *purchase*,
  not a statement about which vehicle exists, and the alternative is a feature
  that does nothing: all 82 currently-eligible hires in the reference workbook
  came in on the sheet. Two things keep it narrow, and both are structural
  rather than conventional — the only caller is a preview whose commit is a
  human click, and `repo._require_adhoc` re-asks the question against the row it
  is about to blank and refuses anything that is not a bare-company cell. Three
  further conditions: never a locked trip; never a non-49 one (rule 10 means no
  numbered unit could take it, so releasing it could only fail); and **never at
  the cost of stranding**, since the trip already has a bus — a hire the replan
  cannot replace simply keeps it, which is why the commit writes `reclaimed` and
  not `eligible`. The 21-day notice binds the *button*, not the person: clearing
  the Company cell in the Data tab or dragging the bar to Uncovered is
  unaffected, exactly as with the re-chain boundary above.
- **A reclaim is previewed for the same reason a re-chain is, and the number it
  reports is not `stranded`.** Reclaims compete with genuinely uncovered trips in
  one pool, ranked by an objective that counts links and cannot see that one of
  them is already handled. So `regressed` names the trips the *current* plan
  proposes and this one drops — they have no bus today either, and what they lose
  is a proposal. Wording it as a loss would read as `stranded` and make a
  dispatcher refuse a better plan. Measured (coverage objective, reference
  workbook): 25 hires cancelled against 26 proposals dropped, which sounds
  break-even and is not — the plan covers **113 → 122** trips, because a
  reclaimed trip becomes supply and extends the chain past itself.
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
- **Today's date enters at the web layer, never in the engine.** Both ends of
  the planning window are explicit `as_of` / `horizon` arguments, and both are
  dates rather than day counts. A planner that consulted `date.today()` itself
  would stop being a pure function of its inputs — the same state would produce
  different plans on different days and no test could pin either.
- **The planner leaves a trip alone from its start date on.** The live plan,
  the one-hop candidates and the exact solve all take today as the near end of
  the window (`as_of`, the bound a re-chain already used), so a trip that has
  started draws no proposal and no commit writes one. Before this `/state`
  passed no near bound at all: once the reference season had gone by, the board
  proposed buses for 113 trips that had all left, and mid-season it was worse
  than noise — a bus "planned" onto a trip it never ran was then routed onward
  from a city it never reached. Manual assignment is untouched, as with the
  horizon below: recording which bus really ran a trip is not planning it.
  **The consequence to know:** a chain only extends from a bus's *recorded*
  trips, so the plan no longer chains *through* trips that have left unassigned.
  On the reference workbook the last recorded trip ends 2026-09-09; pinned to
  the 5th the plan has 91 links, pinned to the 15th it has none, with 134
  uncovered trips still ahead (the empty case is pinned in `test_api.py`). That
  is the honest
  answer — the sheet has stopped saying where the buses are — and the summary
  says so in those words rather than blaming capacity. Assigning the bus that
  actually ran a started trip is what gives the planner a tail to extend again.
- **Every trip the window leaves out is named, with the end that left it out.**
  `EngineResult.out_of_scope` maps each such trip to `"started"` or `"horizon"`
  (`beyond_horizon` is its count of the latter). The board needs it per trip:
  an out-of-scope trip has no candidate and is not reachable, which is exactly
  how a trip no chain can ever reach looks, and until this existed that is what
  every bar and every panel said about it — on the default month, 187 of the
  reference workbook's 190 uncovered trips were labelled "needs an ad hoc bus
  or a subcontractor" for being a few weeks out. Every surface that explains
  an uncovered trip asks `outOfScope` before it reasons about rules, as it asks
  `isLocked`; locked trips are deliberately absent from the map, because the
  lock is their reason.
- **The planner only commits a month ahead, and that is a setting.** *Plan
  ahead* (2 weeks / 1 month / 3 months / All, stored as `plan_horizon`,
  default a month) bounds how far out the engine will decide anything: an
  unbounded planner ties buses to work months out on a schedule still being
  edited, while rule 11 gives only 21 days to cancel a hire. One filter in
  `_split`, so it reaches the chains, the one-hop candidates, reachability, the
  parked fits and the exact solve together — but never the board drag or the
  assign pickers, which are manual escape hatches. Three things follow that are
  not obvious. **It cuts demand only:** a bus that finished before the window
  opened is still supply, or the first trips in the window would be unreachable
  for no reason. **A re-chain must not release past the cutoff**, because the
  replan cannot see those trips and would strand every one of them — the fifth
  release boundary, and the only one that is a correctness rule rather than a
  policy. **Below 21 days the reclaim goes dead**, since nothing is both far
  enough out to cancel and near enough to plan; `frozen_horizon` is counted
  apart from `frozen_notice` so the panel says which rule bit. Unlike the
  empty-runs switch this default *does* change an existing database on upgrade,
  which is the feature rather than an oversight — and on the reference workbook
  (measured 2026-08-02, a season starting 27 days out) it is the difference
  between 113 covered trips and 0, so the resolved cutoff date and the count of
  trips beyond it are printed next to the plan. An empty board that cannot say
  why reads as a broken engine.
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
- **A parked bus is surfaced, never planned.** The tail-only rule above has a
  blind spot: one far-future committed trip pins the end of a unit's timeline,
  so the bus can idle for weeks mid-chain and the planner proposes nothing —
  measured on live data, one unit sat parked 56 days behind a single
  source-file assignment while six uncovered trips fit inside the block, every
  one already a legal board drop. `engine/parked.py` enumerates those windows
  (consecutive committed pairs wider than `min_gap_days + MAX_REST_DAYS`) with
  the fitting uncovered trips, priced in empty-run days and ranked
  cheapest-drive-first, on the board's **Parked buses** panel. Widening the
  planner instead was measured and rejected: ~1 extra covered trip, bought by
  auto-proposing *double* empty runs (out and back) the model cannot price —
  against the policy above. `insertion_ok` stays the one legality gate; the
  panel lists, a person drags.
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
