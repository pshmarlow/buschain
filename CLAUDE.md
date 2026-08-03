# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Use the project venv — the system Python lacks the dependencies:

```bash
.venv/bin/python -m pytest                          # full suite, ~3 min (property-based acceptance tests)
.venv/bin/python -m pytest tests/unit -q            # unit tests only, ~2 min
.venv/bin/python -m pytest tests/unit/test_plan.py::test_name -q   # single test
uvx ruff check buschain tests                       # lint (config in pyproject; B905/E731/E741 ignored on purpose)
.venv/bin/python -m buschain.web                    # run the app on 127.0.0.1:8000
```

On Windows the interpreter is `.venv\Scripts\python.exe` — substitute it for
`.venv/bin/python` throughout. `uvx ruff check` is identical on both.

`tests/unit/test_rules_js.py` runs the browser's rule arithmetic in node and
skips if node is absent. The exact solver needs `pulp` (`buschain[solver]`
extra, pinned `<4` — PuLP 4.0 removes the API in use; migration note in
`engine/solve.py`).

**Caution:** `buschain.db` in the repo root is live user data and the app's
default DB. For any experiment or manual run, pass a scratch path to
`create_app(db_path)` instead. The reference workbook
`LDC_PLANNER__test.xlsx` is the fixture for the whole test suite — never edit
it.

## What this is

A deterministic bus-trip chaining planner for a single human dispatcher:
import an xlsx in the "Capitals" layout, chain buses onto uncovered trips
under rules 1–11 of `bus-chaining-build-brief.md`, export the same xlsx with
only the operated cells changed.

Read before editing: `docs/engine.md` (the maintained deep-dive on the
engine) and the **"Key decisions worth knowing before editing"** list in
`README.md` — nearly every non-obvious behavior (locks, re-chain boundaries,
empty-run policy, the three day-accountings, paste semantics) is documented
there with the measurements that justified it.

## Architecture

Strict one-way layering; each boundary is a design decision, not a habit:

- **`model.py`** — frozen dataclasses only. Mutation lives exclusively in the
  store; the engine is a pure function over these.
- **`contract.py`** — the ONE home of validation. Every source (xlsx, csv,
  paste, manual entry, in-app edit) becomes `RawTripRow`s and passes through
  `validate_row`. Rows never fail silently: they land in `non_operable` with
  all their reasons. The store re-validates by overlaying `cell_overrides`
  onto the raw imported values — an edit behaves exactly like the same text
  arriving via import.
- **`codecs/`** — I/O only, never constructs `model.Trip`. `types.py` is the
  single source of the sheet layout (letters/titles derive from
  `COLUMN_ORDER`; letters were once duplicated and drifted — don't spell them
  inline). **The xlsx export is byte-splice patching of the original file
  (`xlsx_writer.py`), never an openpyxl save** — re-serialization destroys
  formatting and breaks acceptance test 1 (byte-identical round-trip). The
  export plan is exactly the `cell_overrides` table: a cell nobody touched is
  never spliced.
- **`engine/`** — pure: `evaluate(report, registry, accepted, …) →
  EngineResult`, no I/O, no clock (both ends of the planning window, `as_of`
  and `horizon`, are passed in as dates — never as "N days from now", which
  would put a now-relative number inside modules of trip-to-trip predicates).
  Everything it
  returns is derived state, recomputed fully on every mutation and never
  persisted — that is what keeps provisional plans out of the export.
  `graph.feasible_edges` is the rulebook (windows, Y/N constraints, timeline
  validity, tail-only empty runs); `plan.py` walks it greedily per mutation,
  `solve.py` solves it exactly (CBC) as an on-demand preview-commit action
  whose result is certified by replaying every link through `feasible_edges`.
  `rechain.py` never plans: it strips releasable assignments and re-runs the
  ordinary `evaluate` — one planner, one rule set, and it must never release a
  trip the horizon hides from that replan. `adhoc.py` does the same
  for cancellable ad hoc hires (rule 11), and `parked.py` only reports:
  mid-chain idle windows and the fits `insertion_ok` would allow there —
  listed, never planned.
- **`store/`** — all reads/writes (SQLite, WAL). Undo is a snapshot of
  overrides/links/locks/deletions/manual rows/company naming taken BEFORE the
  action, not an inverse
  operation; wrap mutations in `repo.undoable(label)` and they become
  undoable for free. A `threading.RLock` serializes mutations across
  FastAPI's threadpool; compound registry operations call the `_`-prefixed
  no-commit cores inside one transaction (the public methods commit).
  `TripId` is `sheet:<row>` / `manual:<rowid>`; stored edits key on column
  TITLES, not letters, so a layout change cannot repoint them.
- **`web/`** — thin: every route is "mutate store → re-run engine → return
  state". `RepoError` maps to 409 (conflicts) or 422 (validation). The client
  (`static/app.js`, one file with marked sections; no bundler, no modules)
  refetches the whole `/state` after every mutation and only renders — the
  server is the single source of truth, and previews (re-chain, optimize) are
  recomputed server-side on commit so a stale browser can never write a stale
  plan. The board's drag previews targets via `drop_targets` and commits via
  `/reassign`, which re-derives the chain link server-side. `static/rules.js` holds the browser's copy of the window arithmetic,
  pinned to `graph.py`'s boundaries by `tests/unit/test_rules_js.py`.

## Conventions that are easy to break

- **WHY comments are load-bearing.** Nearly every non-obvious decision has
  its rationale (often with measured numbers) at the point of decision.
  Never strip them; match their style when adding code.
- Several "obvious cleanups" are deliberate and documented in place: the
  three day-accountings (`gap_days` for ranking, `Plan.idle_days` for the
  objective, free days = gap − 1 for display) must not be unified; the
  manual-assign pickers deliberately list illegal units (escape hatch);
  fleet warnings deliberately exclude ad hoc assignments; the Company cell's
  combo widget must not become a native `<select>` (macOS renders no option
  colors). When a change looks like simplification, check the comment first.
- Changed behavior belongs in tests: the suite pins decisions
  (`tests/acceptance/` = the brief's AT1–AT7; unit tests carry the rationale
  in their names/docstrings). AT1 byte-identity is the tripwire for any
  codec change.
- This directory is a git repository with a GitHub remote (`origin`). Branch
  before sweeping changes; commit only when asked — but **always push after
  committing**, in the same turn, rather than leaving a branch ahead of its
  remote. The work moves between two machines, so an unpushed commit is an
  invisible one. Never force-push to satisfy this: if a push is rejected as
  non-fast-forward, say so instead of escalating.
