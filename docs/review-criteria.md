<!-- This file is managed by /review and /review-criteria-prune. -->

# Review Criteria

buschain is a deterministic bus-trip chaining planner whose export must be
byte-identical to the imported xlsx wherever nothing changed — reviews weigh
data preservation and rule fidelity above everything else.

## Critical paths

- `buschain/codecs/xlsx_writer.py` — byte-splice export. A wrong splice
  silently corrupts the customer's workbook; this is the highest-consequence
  code in the repo.
- `buschain/engine/graph.py` — `feasible_edges` is the single rulebook every
  planner (greedy, exact solver, suggester) consumes, and `insertion_ok` is
  the same rulebook for the board's drop gates and `engine/parked.py`'s
  fit list. A wrong edge proposes illegal assignments everywhere at once.
- `buschain/engine/adhoc.py` — rule 11's eligibility gates protect purchases:
  a wrong `releasable`/`refusal` cancels a hire that may not be cancelled, or
  strands a covered trip.
- `buschain/store/repo.py` — the only mutable state: accept/rechain/undo
  transaction boundaries and the undo snapshot protocol.
- `buschain/contract.py` + `buschain/codecs/` import boundary — where silent
  row drops or column-shifted values would enter (acceptance test 2: nothing
  silently dropped).
- `buschain/web/api.py` commit endpoints (`/accept*`, `/rechain`,
  `/reclaim-adhoc`, `/reassign`, `/optimal-plan`,
  `/registry/companies/{key}/rename`) — the paths that write assignments or
  rewrite cells.

## Domain-specific checks

- Any codec or export change: the zero-change export must still return the
  original bytes verbatim (`xlsx_writer.export`'s short-circuit), and AT1
  (`tests/acceptance/test_at1_roundtrip.py`) must pass. The write path must
  never go through an openpyxl save.
- Column references are named handles from `codecs/types.py`
  (`COLUMN_ORDER`-derived), never bare letters; stored edits key on column
  TITLES. A diff introducing a literal `"N"`/`"P"`-style letter outside
  types.py is a bug (letters drifted once and nearly spliced assignments into
  the passthrough grid).
- `buschain/engine/` stays pure: no file/DB/network access, no
  `date.today()`/`random` — today's date enters only as the `as_of` argument
  at the web layer.
- Derived state is never persisted: `Plan`, suggestions, `RechainResult`, and
  solver output must not reach `cell_overrides` or any table. Committing goes
  through the ordinary accept path only.
- Window arithmetic exists twice by design — `engine/graph.py` and
  `web/static/rules.js` — a change to floors/ceilings/near-pairs must touch
  both plus their twin tests (`test_links.py`, `test_rules_js.py`). The
  ceiling must remain `floor + MAX_REST_DAYS`.
- The three day-accountings stay distinct: `gap_days` (ranking),
  `Plan.idle_days` (objective, excludes empty-run travel), free days =
  `gap − 1` (every on-screen label). A UI label showing raw `gap_days` is a
  bug; so is "unifying" them.
- The `reposition` setting is one switch reaching every consumer — edges,
  greedy plan, solver, and the UI's explanations/ghost slots. A diff that
  narrows it in one place but not the others makes the setting disagree with
  itself.
- Solver (`engine/solve.py`) changes must preserve: certified replay of every
  link through `feasible_edges`, determinism (canonical variable order,
  single-threaded CBC), a strictly lexicographic objective — each tier's
  separation must exceed the worst accumulated spread of the tier below it,
  re-derived whenever demand counts grow (the original weights silently
  inverted at 200 links) — and the never-worse-than-greedy fallback in
  `engine.optimal_plan`.
- Store mutations: multi-write operations run in ONE transaction
  (`with self.conn:`); compound operations call the `_`-prefixed no-commit
  cores, never the public registry mutators (which commit and would end the
  enclosing transaction early). New mutations are wrapped in
  `repo.undoable(label)`, their state covered by `_snapshot`/`_restore` (an
  unsnapshotted table silently reverts on undo), and every guard-then-write
  sequence holds `Repo._write_lock` across BOTH steps — the undo protocol is
  three transactions on one shared connection and is only safe under that
  lock.
- Validation lives in `contract.py` only; codecs decode renderings but never
  repair content, and `normalize.py` stays closed (exactly four transforms —
  adding a "helpful" one silently widens the data contract).
- API error mapping: `RepoError` → 409 for conflicts, 422 for validation;
  commit endpoints recompute plans server-side and never accept
  client-supplied links (staleness rule).
- Frontend: no `innerHTML`/`insertAdjacentHTML` anywhere (the rendering layer
  is structurally XSS-free — keep it that way); new mutations go through the
  `mutate()` helper (in-flight guard + stale-`/state` token) — the legacy
  direct-`post` paths (registry chips, delete flows behind confirm dialogs)
  are known exceptions; don't add more. Escape-hatch pickers (direct assign,
  next-trips) must never be filtered down to engine-legal options.
- Any guard that certifies a conversion cannot raise must be exactly that
  conversion's precondition. Three live failures of this class:
  `isdigit()` before `int()` (`'²'` passes, `int()` raises — a 500 on page
  load), `lstrip("-").isdecimal()` (`"--5"` passed, the export died), and a
  shape-only date regex (`"2026-13-45"` matched, `fromisoformat` raised, the
  export died). Prefer try/except, or a regex that is literally the
  converter's grammar.
- Every `<select>` cell editor keeps an unmatched current value as an
  explicit "(keep as is)" option — a missing guard means blur silently
  rewrites the cell to the first option (the Status editor wiped valid
  lowercase statuses this way).
- Code that measures rendered geometry (`getBoundingClientRect`,
  `scrollLeft`, `offsetHeight`) must not run while the element is
  `display:none` — hidden elements report 0 and the zero gets persisted into
  styles or state (the filter-row pin and the board-scroll jump were both
  this).

## Known fragile areas

- `xlsx_writer.py`'s hand-rolled XML regexes: the self-closing-vs-open cell
  alternatives, `_scan_populated`'s populated-vs-last-row distinction, and
  the grow-only `dimension`/`autoFilter` handling each encode file behavior
  invisible from the code — the comments carry the derivations; changing a
  regex requires re-deriving them.
- The Information sheet is read by header but was once written by fixed
  letter (the Fleets-column drift); anything writing to that sheet must carry
  its discovered column, not a constant. The seed's name-matching has a
  history (the 'Nordi Bus'/'Nordibuzz' incident cost 31 rows).
- The reference data contains 76 typo'd-year rows (366-376 day spans, where
  every sound row spans 0-11). `contract.MAX_TRIP_SPAN_DAYS = 60` refuses them
  into the non-operable list — the one heuristic in a module of objective
  checks, justified by the empty 355-day gap between the two populations, and
  refusal rather than repair. Changing or removing it moves AT2's pinned
  1527/143 split and brings back 20 false fleet warnings.
- `_unit_timeline_ok` deliberately ignores trips overlapping the supply trip so
  a bad row cannot freeze a unit. No reference row exercises that branch since
  the contract started refusing the typo'd-year rows, so
  `test_links.py::test_trip_overlapping_the_supply_does_not_block` is the only
  thing standing between it and a silent regression — do not delete it.
- Excel's clipboard omits hidden columns with no trace, so paste correctness
  rests on the declared-columns picker (`codecs/paste.py`) — positional
  "fixes" reintroduce silently shifted rows that still validate.
- PuLP is pinned `<4`: 4.0 removes `LpVariable()`/`PULP_CBC_CMD`
  (migration note in `engine/solve.py`). An unpinned bump breaks the solver.
- Locked trips exist server-side as non-demand but client-side as uncovered:
  every explainer/action surface that walks `uncoveredTrips()` must branch on
  `isLocked` first, or it fabricates a wrong reason (this produced an
  "Assign anyway · undefined" button and a false "permanently unreachable"
  verdict before the guards went in).

## Key documents

- `bus-chaining-build-brief.md` — the spec: chaining rules 1–11, acceptance
  tests AT1–AT7, and the out-of-scope list. Rule changes must trace here.
- `docs/engine.md` — the maintained engine deep-dive; verify engine diffs
  against it and update it when behavior changes.
- `README.md` — "Key decisions worth knowing before editing": the measured
  rationale behind locks, re-chain boundaries, empty-run policy, paste
  semantics. A diff contradicting one of these needs an explicit reason.
- `CLAUDE.md` — commands, layering, and the conventions-easy-to-break list.
