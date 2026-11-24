# The routing/chaining engine

How `buschain/engine/` decides which bus can take which trip, and how it strings
those decisions into chains. Written for the maintainer: plain language first,
code pointers second. Rule numbers refer to `bus-chaining-build-brief.md`.
Code pointers name symbols rather than line numbers where they can — three
separate commits have invalidated cited lines before.

---

## 1. The mental model in one paragraph

A trip is a date interval between two cities. When a trip is assigned to a
**numbered unit** (a physical bus, e.g. `Kautra 3`), its **arrival** creates
*supply*: on that day, in that city, a bus becomes free. An unassigned
Confirmed/Pending trip is *demand*: it needs a bus in its departure city before
it starts. The engine's whole job is to connect supply arrivals to demand
departures — first as single candidate links a human can accept, then as
multi-hop chains where each accepted trip's own arrival becomes the next
supply. Everything the engine produces is **derived state**: recomputed from
scratch on every call, never persisted. The only durable facts are the
assignments a human commits; the engine re-reads them and rebuilds every
suggestion, plan, and warning in milliseconds.

## 2. Vocabulary

| Term | Meaning |
|---|---|
| **Demand** | Confirmed or Pending trip with no assignment. Pending = may cancel (at-risk). |
| **Supply** | The arrival of a trip assigned to a *numbered* unit. |
| **Numbered unit** | `Ollex 1` — a chainable physical bus with a timeline. |
| **Ad hoc** | Bare company (`Ollex`) — "some bus from their yard". Legitimate, but chain-invisible: never supply, never on a unit timeline. |
| **Edge** | A feasible (supply trip → demand trip) link the rules allow. Candidate, not decision. |
| **Link** | An edge someone (or the planner, provisionally) has applied. |
| **Chain** | A unit's sequence of links: arrive, rest, depart, repeat. |
| **Empty run / reposition** | Driving a bus with no passengers to a *different* city to start its next trip. A real cost the model can't price (no distances), so it's always last resort. |
| **ER** | The source sheet's explicit empty-run mark. Read-only — honored, never written. |
| **Locked** | A trip the planner must not re-decide. It stays on its unit's timeline and still feeds the next hop; it just leaves the demand set. |
| **Parked window** | A consecutive committed pair wider than any chain window — the bus idles mid-chain. Invisible to the planner (Gate 4 makes cross-city moves tail-only), so `parked.py` enumerates it for a human instead. |

## 3. The pipeline: what `evaluate()` does

`evaluate()` (`buschain/engine/__init__.py`) is the single entry point and a
pure function: `(contract report, registry, accepted links, objective,
reposition, locked, as_of, horizon) → EngineResult`. No files, no clock, no
database — which is why every mutation elsewhere in the app is simply "change
state, re-run evaluate", with zero cache-invalidation logic anywhere.

```
report.trips
  │ drop Canceled (kept only for audit / broken-chain roots — rule 7)
  ▼
live trips ──┬─ assigned to numbered unit ──► SUPPLY + per-unit timelines
             ├─ assigned to bare company ──► ad hoc lists (chain-invisible)
             └─ unassigned, not locked,   ──► DEMAND
                starts after as_of,
                starts on or before horizon
  ▼
graph.feasible_edges(supply, demand, timelines)     ← the rules live here
  ├──► suggest.build_suggestions  → ranked one-hop candidates, both directions
  ├──► plan.build_plan            → multi-hop provisional chains
  ├──► reach.reachable_demands    → "could any chain ever get here?"
  ├──► checks.*                   → integrity findings, fleet warnings, broken chains
  └──► parked.windows             → mid-chain idle windows + the trips that fit them
                                    (via insertion_ok — same rules, asked the drop's way)
```

The supply/demand reading itself lives in `_split`
(`buschain/engine/__init__.py`), shared by `evaluate()` and the exact
solver's entry point `optimal_plan()` (same module) so the two can never
disagree about what is in play. `optimal_plan` is deliberately *not* part of
`evaluate()` — see the solver section below.

The `EngineResult` fields, and who they're for:

- `suggestions_by_unit` / `suggestions_by_trip` — the accept UI (rule 9: a
  human accepts; nothing is ever auto-assigned).
- `plan` — the multi-hop proposal panel. Provisional by construction.
- `reachable` — lets the board distinguish "waiting on an earlier decision"
  from "no chain will ever reach this".
- `parked` — the Timelines tab's **Parked buses** panel: units idle mid-chain
  longer than any chain window, with the uncovered trips a manual drop could
  put there and each fit's empty-run day cost (`parked.py`). Listed, never
  proposed — the planner's cross-city moves are tail-only, so these blocks
  are exactly what it cannot see.
- `integrity`, `fleet_warnings`, `broken_chains` — reports. Listed, never
  auto-fixed (rules 4–6).
- `timelines`, `adhoc_by_company` — display.
- `beyond_horizon` — how many uncovered trips the horizon put out of scope, so
  a UI can tell "the fleet can do nothing" from "you are not looking there yet".
- `out_of_scope` — the same thing trip by trip, for both ends of the window:
  each uncovered trip the planner was not asked about, mapped to `"started"`
  (it begins on or before `as_of`) or `"horizon"` (it begins after the far
  bound). A board labelling one bar needs it, because such a trip has no
  suggestion and is not reachable — exactly what a trip no chain can ever
  reach looks like. `beyond_horizon` is its count of `"horizon"` entries.
  Locked trips are absent: the lock is their reason.

(Canceled trips are dropped inside `_split` — rule 7: excluded from matching,
retained for audit and as broken-chain roots.)

Three parameters keep purity honest. `as_of` and `horizon` are the two ends of
the planning window and are both *passed in* as dates (an engine that read
`date.today()` would give different answers on different days for the same
state, untestable). `reposition` reaches **both** the edges and the plan — one
switch, so the suggestion board and the planner can't disagree about whether
empty runs exist.

### The planning horizon

`horizon` is how far ahead the planner may commit a bus, stored as the
`plan_horizon` setting (`14` / `30` / `90` / `all`, default `30`) and resolved
to a date by `web/api.py:_horizon` — today + N. The engine only ever sees the
resolved date, deliberately: a now-relative day count loose in a module of
trip-to-trip predicates is one refactor away from being read as a gap, the same
argument that keeps `ADHOC_CANCEL_NOTICE_DAYS` out of `graph.py`.

It cuts **demand only**, in `_split`, so it reaches every derived list at once —
chains, one-hop candidates, reachability, parked fits, and the exact solve. Two
places deliberately ignore it, both manual escape hatches: `drop_targets` (the
board's drag) and the assign pickers. The horizon says what the planner decides
on its own, not what a dispatcher may do by hand.

Supply is never cut. A bus that finished the day before the window opens is
standing there ready, and dropping it would make the first trips in the window
unreachable for no reason anybody asked for.

### The near end: trips that have already started

`as_of` is the other end of the same window, and the web layer passes today's
date for it everywhere the planner decides something: `/state`, the commit
routes that recompute the plan, the exact solve, and (as before) re-chain and
reclaim. A trip beginning on or before `as_of` is out of demand — "started" in
`out_of_scope` — so it draws no suggestion and no plan link. The comparison is
`<=`: a trip leaving today is already today's problem, the rule a re-chain has
always used for what it may release.

Like the horizon it cuts demand only and never the manual paths. Recording
which bus actually ran a trip that has left is an assignment by hand, not a
plan.

The consequence is easy to mistake for a fault. A chain extends from a bus's
*recorded* trips, so once the planner stops proposing for started trips it also
stops chaining **through** them. If trips went out and the sheet was never told
which bus took them, the fleet's last recorded arrivals fall more than a chain
window behind everything still ahead, and there are no edges at all. Measured
on the reference workbook (last recorded trip ends 2026-09-09, horizon
unbounded): `as_of` 2026-09-05 gives 91 links; 2026-09-15 gives none, with 134
uncovered trips still in scope. Before the near bound was passed, that plan
kept going by assuming buses had run trips that left without them — and routed
them onward from cities they had never reached. Assigning the real bus to a
started trip restores a tail the planner can extend.

Two consequences worth knowing before changing the horizon:

- **Re-chain must not release past the cutoff** — see §8's fifth boundary.
- **A horizon below 21 days makes reclaim a no-op**, because nothing is both
  far enough out to cancel and near enough to plan. Counted separately
  (`frozen_horizon` vs `frozen_notice`) so the panel can say which rule bit.

## 4. When can a bus take a trip? (`graph.py`)

`feasible_edges` (`buschain/engine/graph.py`) emits an edge only when four
gates all pass. This edge list *is* the optimization model (rule 8): the greedy
planner ranks it, and the exact solver (§5, `solve.py`) poses the same rules as
an integer program and certifies its answer by replaying it through these edges.

**Gate 1 — city and time window (rules 1–2).** The gap is
`demand.start − supply.end` in days. The floor depends on where the bus is and
where it must be (`min_gap_days`):

| Situation | Floor | Window (gap) | Why |
|---|---|---|---|
| Same city | 1 | 1–4 | Overnight turnaround; same-day handover invalid. |
| Near pair (CPH↔OSL, CPH↔STO, OSL↔STO, OSL↔BGO) | 2 | 2–5 | One clear day covers the empty drive. |
| Any other pair | 3 | 3–6 | Two clear days. |

The ceiling always rides on the floor (`floor + MAX_REST_DAYS`), so "max 3
days rest" means the same thing everywhere — three days *parked*, whether or
not the bus had to drive first. The near-pair table (`graph.py:29`) is
deliberately a code literal, not user config: each pair is a claim about the
map, the source data carries no distances to derive it from, and it is *not*
transitive (CPH–BGO is two short hops via OSL but is not itself short).

**Gate 2 — Y/N constraint (rule 3).** `company_ok` (and `_constraint_ok` over
it, `graph.py`): `Y` + company X → only X's buses may serve; `N` + X → X
never; blank → anyone. Hard filter, company-level. Where Y/N comes from
*today*: the brief's `Y-N`/`B Comp` column pair is retired — the contract
derives both from the **Bus Preference** column's free text
(`contract.py`): a bare company name means `Y` (only them), a `NO ` prefix
means `N` (never them), and text that does not resolve to a registry company
fails the row rather than being ignored.

**Gate 2b — capacity (rule 10).** `numbered_unit_ok` (`graph.py`): the chartered
coaches seat `CHARTERED_PAX` (49), so a demand trip stating any other `Pax`
gets no edge to any numbered unit — it is ad hoc work. Applied by filtering the
demand list once, since every supply in this graph is a numbered unit by
definition. Blank `Pax` passes: the rule bites on a stated number that
disagrees, not on a missing one, because paste and manual entry routinely
produce rows with no `Pax` and reading blank as "not 49" would silently drop
them out of the fleet's reach. The constant is a code literal for the same
reason as the near-pair table — the sheet carries no seat counts, and one
number true of the whole charter is not a configuration knob. Costs nothing on
the reference workbook: its 16 non-49 rows are all already ad hoc, so coverage
stays at 113/190 and the rule enumerates zero findings.

**Gate 3 — the unit's whole timeline stays valid (rule 6).**
`_unit_timeline_ok` (`graph.py`) simulates the accept: no overlap with any
existing trip, no existing trip already sitting inside the gap, and if the unit
has a later trip, that successor must still chain from the *demand* trip
afterwards (unless the successor is ER-marked — its explicit repositioning
covers the move). Two tolerances keep a bad row from freezing a whole bus:
trips already *overlapping* the supply trip are ignored here (they're
pre-existing violations the integrity report enumerates — the case this was
written for, the reference data's 76 typo'd-year rows, is now refused by
`contract.MAX_TRIP_SPAN_DAYS` before it ever reaches the engine, which took the
fixture's overlap findings from 4 to 0; the branch stays for overlaps arriving
by other routes and is covered by `test_links.py`), and a successor pair that
was *already* broken
can't get vetoed further — inserting a trip into an already-invalid gap
typically shrinks it.

**Gate 4 — repositioning only off the tail.** An empty-run edge
(`arr_city ≠ dep_city`) is emitted only when `reposition=True` **and** nothing
follows the supply trip on that unit (the tail check in `feasible_edges`).
Mid-chain, driving the bus somewhere else would strand whatever comes next in
the wrong city. The blind spot this rule creates — a bus idle for weeks
mid-timeline behind one far-off committed trip — is enumerated rather than
planned: see `parked.py` (§7).

### The same rules asked the other way round (`insertion_ok`)

`feasible_edges` answers *"what can this bus pick up next?"* — it walks supply
trips. That is the right question for the planner and the wrong one for the
board's drag gesture, which has to say whether a bus may take a trip *at all*:
a unit with no work to hand over (idle, or one whose trips all start later)
produces no edge, and most of the fleet is parked most of the time.

`insertion_ok` / `insertion_verdict` (`graph.py`) ask from the timeline's side —
*"with this trip inserted, could one bus run this?"* — and draw the line at
**impossible**, not **suboptimal**, which is where acceptance criterion 3 draws
it too (every consecutive pair satisfies the rules *or is enumerated*):

| Refused | Permitted, and enumerated instead |
|---|---|
| Overlapping an existing trip on that unit | A gap wider than `floor + MAX_REST_DAYS` — the bus is simply parked |
| A gap below `min_gap_days` on either side — the bus cannot be there | A city change with time to spare, needing no approved ER |
| A company the trip's `Y`/`N` names (rule 3 is hard) | A successor the bus already could not reach before the insert |
| A `Pax` other than 49 on a numbered unit (rule 10 is hard) | A non-49 trip already imported onto a numbered unit — listed as `pax_mismatch` |

That relaxation is measured, not assumed: **34 of the reference workbook's 183
consecutive pairs are already wider than the window**, and the integrity check
lists 36 findings on it. A drop that refused them would refuse roughly a fifth
of what the sheet itself records.

The narrower question — *is this pair a rule-1 handover?* — is answered
separately as `Insertion.chains`, and that is what decides whether an accepted
link is recorded. Inside the window, a dragged trip leaves exactly the
provenance an accepted suggestion would; outside it, the assignment is written
with no link, because a chain nobody could run is not worth recording.
`reposition` gates the *link*, never the legality: a bus with a fortnight to
spare needs no one's approval to be driven somewhere.

`test_links.py` pins the two functions together — every edge `feasible_edges`
emits must be an insertion `insertion_ok` allows, with the same supply, gap and
ER flag — restricted to timelines a bus could actually run. Both documented
divergences are on timelines that were already impossible, and both are named
tests.

### Worked example

`Kautra 3` finishes a trip in **CPH on Jul 5**. Candidate next trips:

- Departs **CPH Jul 7** → gap 2, same-city window 1–4 ✓ (1 rest day).
- Departs **CPH Jul 10** → gap 5 ✗ — too long parked.
- Departs **STO Jul 7** → gap 2, near pair, window 2–5 ✓ but it's an empty
  run, so the edge carries `reposition=True` and ranks behind every free move.
- Departs **BGO Jul 7** → gap 2, far pair needs 3 ✗ — can't get there in time.

## 5. How chains are built (`plan.py`)

One accepted trip's arrival unlocks the next link, but one-hop suggestions
can't see past the first hop. `build_plan` (`buschain/engine/plan.py:79`) runs
the same edge graph *forward*, provisionally:

```
while demand remains:
    1. edges = feasible_edges(supply, remaining demand, timelines)
    2. sort edges by the objective's ranking key
    3. walk the sorted edges, applying at most ONE per unit this round,
       and DEFERRING any empty run whose demand had a same-city edge this round
    4. each placed demand joins supply and its unit's timeline
    5. stop when a full round places nothing
```

Nothing is written: each placement is a `dataclasses.replace` on a frozen Trip,
discarded when the call returns. That is what keeps provisional assignments out
of `cell_overrides` and therefore out of the xlsx export.

Three rules inside the loop, each earning its place with measurements (kept in
the code comments at `plan.py:33–59` and `plan.py:112–130`):

- **Ranking** (`_key_coverage`, `plan.py:60`): `reposition` first (free moves
  always beat empty runs), then shortest gap, then earliest demand *end* —
  classic interval scheduling: the trip that hands the bus back soonest wins,
  because that bus is the next link's supply. Ends in TripId tiebreakers so
  identical input always yields an identical plan. This replaced a
  most-constrained-demand-first heuristic that got *worse* once empty runs
  existed (repositioning gives nearly every demand a candidate, flattening the
  scarcity signal): constrained-first covered 46/131 and 100/190 on the two
  datasets; this key covered 51/131 and 117/190 when that comparison was run.
  (Today's end-to-end greedy covers 113/190 — the deferral rule below traded a
  few of those links for fewer empty runs, deliberately.)

- **One edge per unit per round** (`plan.py:137`): every edge was validated
  against the timeline *as it stood at round start*; a second edge for the same
  unit would be checked against stale state and could land two overlapping
  trips on one bus. Deferring costs a round; the next pass re-validates.

- **Empty runs are deferred, not just ranked last** (`plan.py:130`): sorting
  alone leaks them — the one-per-unit rule can consume a demand's same-city
  supply on an earlier demand, and the loop then reaches it with only its
  repositioning edge left. Measured: 10 of 26 empty runs went to demands that
  had a same-city option *in that very round*. So any demand with a same-city
  edge this round refuses empty runs this round and waits. Deferring covers
  **more**, not less (coverage 108→113 links on 26→25 empty runs): the bus an
  empty run would have tied up stays free for work only it can do. The trade:
  a demand whose same-city supply is taken *every* round is never covered here.

The loop terminates because a round that places nothing can't do better next
time — inputs unchanged (`plan.py:169`).

### The deferral, on a tiny fleet

Vilkas 1 ends **Jul 5 in CPH**; Vilkas 2 ends **Jul 5 in OSL**. Demand: trip X
(Jul 6–8, CPH→CPH tour) and trip Y (Jul 9–11, CPH→STO).

- **Round 1** edges, sorted: `V1→X` (gap 1, free), `V1→Y` (gap 4, free),
  `V2→Y` (gap 4, empty run OSL→CPH). Place `V1→X`. `V1→Y` skipped — V1
  already used this round. `V2→Y` is an empty run and Y had a free option
  this round → **deferred**.
- **Round 2**: X is now provisional supply — V1 is back in CPH on Jul 8. New
  edge `X→Y` (gap 1, free) outranks `V2→Y`. Place it.

Both trips covered, zero empty runs, Vilkas 2 still free. Had `V2→Y` been
taken in round 1, the fleet would have paid an empty run for a trip V1 could
reach for free.

`chain_order` (`plan.py`) gives each chain's demands in accept order —
each link's supply must be assigned before the link itself, so committing a
chain walks it front to back.

### The exact solver (rule 8's second half — `solve.py`)

The greedy loop measurably leaves coverage behind: 113 of 190 demands on the
reference data where **124 is provably achievable** (60 vs 78 with empty runs
off). `solve.build_plan` formulates coverage over the *same* edges as an
integer program (CBC via the optional `buschain[solver]` extra) and returns
the same `Plan` shape, so committing it is the ordinary accept path.

**How the model thinks** — where the greedy asks "what is the best next
link?", the solver asks one question about the whole future at once. It
describes every way a demand could be covered as a yes/no decision of three
kinds, then lets CBC pick the best consistent set:

- *tail extension* (`x`): demand `d` becomes the first new trip after unit
  `u`'s last one — the only place an empty run is allowed, exactly like
  `feasible_edges`' tail rule;
- *gap insert* (`g`): `d` slots into an existing gap between two committed
  trips — same city on both sides, both windows valid, one insert per gap
  (mirroring `_unit_timeline_ok`'s successor re-check);
- *chain hop* (`w`): `d` follows another demand the solver has already
  decided to cover — allowed only if that predecessor was itself covered by
  a tail extension or an earlier hop, which is what turns loose picks into
  real chains.

The consistency rules are the ones you already know from the greedy: each
demand covered at most once, one successor per tail, and a hop needs its
predecessor. Time ordering makes cycles impossible, so that is the whole
model (`solve.py`, `_solve`).

Three properties keep the result trustworthy: it restates no rules (windows
from `min_gap_days`, Y/N from `graph.company_ok`, tail-only repositioning);
every link is **certified** by replaying the solution hop-by-hop through
`feasible_edges` before it reaches the caller (`_certify`, `solve.py`);
and it is deterministic (canonical variable order + single-threaded CBC). The
objective prices what the model can honestly price — a covered trip
(100,000,000) always dominates, then fewer empty runs (100,000 each), then
tighter turnarounds (1 per gap day); real money waits for registry rates.

**The trade it makes visible**: on the reference data the optimum buys its
+11 trips with ~12 more empty runs (37 vs the greedy's ~25). With no costs in
the model, "optimal coverage" spends drives freely — which is why the empty-run
count is in the preview's headline and per chain, and why the human commits.

Deliberately **not** part of `evaluate()`: a solve takes seconds and
`evaluate` runs on every mutation, so the solver is a preview-and-commit
action like the master re-chain (`GET`/`POST /optimal-plan`, the "Optimize
coverage…" button). `engine.optimal_plan` also runs the greedy on the same
inputs and falls back to it if a timeout or certification refusal ever drops
the solve below it — the action can only match or improve the board's plan.

## 6. Two objectives, two day-accountings (don't unify them)

Two ranking keys (`plan.py:60–68`): **coverage** (default; earliest *end*
first — maximize links) and **idle** (earliest *start* first — mirror the
one-hop suggester's shortest-turnaround feel). Both put `reposition` and
`gap_days` first.

Two distinct measures of "days", intentionally different:

- **Ranking uses raw `gap_days`** — a bus mid-empty-run is out of service just
  as surely as one parked in a yard. Question: *which bus is free first?*
- **`Plan.idle_days` subtracts the travel floor**
  (`gap − min_gap_days(from, to)`, computed in `_assemble`) — days spent driving are
  worked, not idled. Question: *how much time was wasted?* Charging travel as
  idleness would make the idle objective avoid distance rather than avoid
  waiting.

(The UI adds a third: it displays *free days* = gap − 1. Three accountings,
three real questions — see the free-days memory/tests before touching any.
The browser's copy of the window arithmetic lives in `web/static/rules.js`
and is pinned to the same boundaries as `graph.py` by
`tests/unit/test_rules_js.py`.)

## 7. Suggestions vs. plan vs. reachable — three different claims

| Output | Claim | Blind to |
|---|---|---|
| `suggestions_*` (`suggest.py:25`) | "You could accept this link *now*." One hop only, ranked, both directions (per free unit, per uncovered trip). | Everything past the first hop. |
| `plan` (`plan.py:79`) | "Here's a full future the fleet could run." Contention-aware — one bus can't be in two places. | Optimality (greedy) and locked/committed alternatives. |
| `optimal_plan` (`engine/__init__.py`, on demand) | "Here's the *most* the fleet could cover." Exact, certified, deterministic. | Cost beyond empty-run counts — and it's seconds, not milliseconds, hence preview-only. |
| `reachable` (`reach.py:48`) | "Some accept sequence could chain into this trip *without any empty run*." | Contention entirely — and repositioning, on purpose. |
| `parked` (`parked.py`, the board's **Parked buses** panel) | "This bus is idle mid-chain longer than any window; these uncovered trips would fit, at this empty-run price." | Nothing is proposed. Acting on one is the board's drag — the planner abstains because its cross-city moves are tail-only, and the double empty run is a person's call (rule 6). |

None subsumes another. `reachable` ignores contention, so it overstates what
the fleet can serve; but the planner may *spend* an empty run to cover a trip
the reachability walk excludes, so `plan.covered ⊄ reachable` either. The
design choice in `reach.py`: allowing empty runs would make almost everything
reachable (an empty run answers "wrong city" by definition), and an
always-true signal tells a planner nothing. Excluding them makes the
**negative** answer the sound one — a trip outside `reachable` cannot be
chained into no matter what order anyone accepts things. The walk runs over
`(trip, company)` pairs, not bare trips, because two companies reaching the
same trip satisfy different Y/N constraints downstream; collapsing to trips
would invent false dead ends.

One more suggestion rule (rule 3/5, `suggest.py`): a `Y`-constrained trip
with no feasible *free* chain gets an **ad hoc fallback** suggestion of its
mandated company, ranked last. A repositioning-only chain doesn't suppress the
fallback — that's exactly when "another bus from the same yard" is the obvious
alternative to driving one across the map.

## 8. Master re-chain (`rechain.py`)

Everything above is additive — a committed assignment is immovable supply the
chains route around (that's what makes confirmed links immune to replanning,
with no lock flag needed anywhere). `rechain.build`
(`buschain/engine/rechain.py`) is the one place assignments are taken
*back*, and it plans nothing itself: it restates the world as if the released
assignments had never been made and hands that to the ordinary `evaluate()`.
One planner, one rule set — a second "replanner" would be a second place for
the rules to drift.

Five release boundaries, none negotiable:

1. **Only app-written assignments** (`repo.system_assigned_ids` — overrides
   whose imported cell was blank). A company name that arrived on the
   spreadsheet is source data *for automation*: a person may retype or clear
   an imported bus (the Data tab, the worklist and the board drag all write
   the same Company-cell override), but a bulk replan must never silently
   overwrite what the sheet said. That is why the boundary lives in
   `system_assigned_ids` rather than in a `repo.unassign` refusal, where it
   used to sit.
2. **Only trips that haven't started** (`start > as_of`). A bus already
   driving can't be re-decided.
3. **Locked trips are exempt** on top of both.
4. **Only numbered assignments.** An ad hoc hire is §9's business. Releasing one
   here was a latent bug: `plan.chains` hold numbered units only, so a released
   hire could never be re-placed and was *guaranteed* to strand — turning a
   served trip into an uncovered one, with no notice check. It moved no measured
   number (accepting a plan only ever writes numbered assignments, so the
   reference data has zero app-written ad hoc rows and the figures below are
   unchanged), but the path existed.
5. **Only trips inside the planning horizon** (`start <= horizon`). The same
   horizon bounds `evaluate`'s demand, so an assignment past the cutoff would be
   taken back and then be invisible to the planner meant to re-place it —
   stranded by arithmetic, every time. Unlike the four above this is not a
   policy preference: without it the horizon silently turns a re-chain into a
   wrecking ball. Counted as `frozen_horizon`, and `frozen_source` /
   `frozen_locked` count *inside* the horizon so the four numbers partition the
   future assignments and the panel's arithmetic adds up.

Accepted links touching a released trip are dropped in both directions
(in `build`): the link *into* it dies with the assignment; the link *out*
of it would claim a bus that may now be elsewhere fed the next trip.

The result is **not monotone**: releasing N assignments does not bring N back.
The replan may spend a bus on a previously-uncovered trip and strand the trip
it took the bus from — measured: 18 of 60 released trips came back with no bus
(idle objective, reference data). That's why `stranded` is a first-class field
(it's the number that decides whether to commit) and why the web layer previews
before committing. `frozen_source` / `frozen_locked` counts let the UI explain
why the releasable number is small against the future as a whole — most future
assignments came in on the sheet. (Illustration on the reference data with the
full plan accepted, measured 2026-08: 113 releasable, 56 frozen as source
data, none locked.) `commit_links` (`rechain.py`) emits writes in chain order
so the store's own "supply-must-be-assigned-first" check passes naturally.

## 9. Reclaiming ad hoc trips (`adhoc.py`, rule 11)

An ad hoc assignment is chain-invisible *and* not demand — `_split` reads any
assigned trip as covered — so before this it was a **dead end**: work given to a
hired-in bus could never come back, however much chartered capacity freed up
later. `adhoc.build` (`buschain/engine/adhoc.py`) is the move that undoes it,
and like §8 it plans nothing: it strips the eligible hires and hands the result
to the ordinary `evaluate()`.

**Eligibility** (`refusal`, tested in this order so the counters a UI shows are
mutually exclusive): must be a bare-company assignment; not canceled; not
locked; `graph.numbered_unit_ok` — rule 10 applied as a *pointlessness* test,
since a non-49 trip gets no edge to any numbered unit and releasing it could
only ever produce a `stayed_adhoc`; and finally
`(start - as_of).days >= ADHOC_CANCEL_NOTICE_DAYS` (21).

That last one is the **first now-to-trip measurement in the engine** —
everything else is trip-to-trip, and `as_of` had until now been a binary
past/future horizon. It deliberately does *not* sit beside `CHARTERED_PAX` and
`MIN_GAP_DAYS` in `graph.py`: those are timeless facts about a bus and two
trips, and every function there is a pure predicate with no notion of "now".
Put a now-relative constant in the one module that never receives `as_of` and it
will eventually be applied to a trip-to-trip gap, where it means something else.
It also **subsumes** §8's `start > as_of` boundary (21 ≥ 1), which is why
`refusal` does not repeat it.

It does **not** subsume the planning horizon, and `refusal` asks the two in a
fixed order — notice first, then horizon — because they mean opposite things.
Notice is the supplier's deadline: the hire cannot be cancelled at any price.
The horizon is a window the dispatcher chose and can widen in one click.
Reported the other way round, "frozen by the cancellation deadline" would be
advice to give up on work that is merely out of view. Their arithmetic:
**a horizon shorter than 21 days leaves nothing eligible at all**, since no
trip can be both far enough out to cancel and near enough to plan. At the
default month the usable band is days 21–30. Measured on the reference data
(2026-08-02): 79 eligible unbounded, 24 at the 1-month default (55 frozen by
the horizon), 0 at 2 weeks (all 79 frozen by it).

**Three things this action does differently from a re-chain:**

- **It never strands.** A released assignment leaves a trip with no bus; an ad
  hoc trip already *has* one. So the commit writes only `reclaimed`, and
  `stayed_adhoc` keeps its hire untouched. `repo.reclaim_adhoc` interleaves the
  clear with the accept for exactly this reason — a pre-clear loop over the
  eligible set (which is what `repo.rechain` does, correctly, for its own case)
  would be the bug.
- **It crosses the source-data boundary, on purpose.** Everywhere else
  automation refuses to touch a company name that arrived on the spreadsheet
  (`system_assigned_ids`). `releasable()` has no such parameter: an ad hoc
  booking is a cancellable purchase, not a statement about which vehicle exists.
  On the reference workbook this is the difference between the feature working
  and doing nothing — **all 82 currently-eligible hires came in on the sheet.**
  Two things keep it narrow: the only caller is a preview whose commit is a
  human click, and `repo._require_adhoc` re-asks the question against the row
  about to be written and refuses anything that is not a bare-company cell. The
  preview reports `from_source` so the crossing is visible rather than silent.
- **Reclaims compete with genuinely uncovered work in one pool**, ranked by the
  ordinary objective — which counts links and cannot see that one trip is
  already handled and the other is not. Deliberately not fixed by bolting a
  tiebreak onto `plan.py` (one planner, one rule set); `regressed` is the
  alternative — the trips the *current* plan proposes and this one drops. They
  are **not** stranded and the UI must not word them as such: they have no bus
  today either, and what they lose is a proposal.

Measured on the reference workbook (fresh import, 1527 operable rows, 478 live
ad hoc trips, `as_of` = 2026-08-01): 82 eligible, 380 frozen by the notice
window, 16 by rule 10, 0 locked. Under the coverage objective 25 hires are
cancelled and 26 proposals dropped — which sounds break-even and is not: the
plan covers **113 → 122** trips, because a reclaimed trip becomes *supply* and
extends the chain past itself. Under the idle objective, 21 reclaimed, 29
dropped, 109 → 112.

## 10. Safety nets (`checks.py`, `deps.py`)

All *listed, never auto-fixed*.

- **Unit integrity** (rule 6, `checks.unit_integrity`): every consecutive pair on every
  numbered unit is classified — `overlap` (violation), `gap_out_of_window`,
  `city_mismatch`, `explicit_er` (sheet's ER mark: enumerated fact, not
  violation), or `reposition` (the pair matches an *accepted* link — the
  planner's own approved empty run, listed as a cost worth seeing rather than
  a mismatch to fix).
- **Fleet warnings** (rule 5, `checks.fleet_warnings`): per company per day, concurrent
  trips on *numbered units* > fleet size. Deliberate deviation from the brief,
  which counts ad hoc too: fleet size means *chartered* vehicles, while ad hoc
  means "some further bus from their yard" — capacity the charter count says
  nothing about. Counting ad hoc produced 1448 warning-days on the reference
  data (Ollex: 2 chartered, 120 ad hoc trips), burying the real
  over-commitments. Intervals count both endpoints — a bus is occupied on its
  end day.
- **Broken chains** (rule 4, `checks.broken_chains`): when a supply trip is canceled,
  unassigned, or gone, every accepted link depending on it — transitively down
  the chain — lands on the worklist. Fixpoint iteration, since links arrive in
  any order.
- **At-risk tracing** (rule 4, `deps.at_risk_chain`): every suggestion lists the
  Pending trips in its supply's upstream accepted chain — the trips whose
  cancellation would break the plan — so the human sees the risk *before*
  accepting.

## 11. Rule-to-code map

| Brief rule | Where |
|---|---|
| 1 — D+1…D+4 window, max 3 rest days | `graph.py:16` constants, `min_gap_days`, applied in `feasible_edges` |
| 2 — city match on canonical codes | `graph.py` (`_pair_chainable` / `feasible_edges` — `arr_city`/`dep_city` equality; codes canonicalized upstream in `model.Registry.resolve_city`; Y/N and the constraint company are parsed from Bus Preference by `contract.py`) |
| 3 — Y/N constraints + ad hoc fallback | `graph.py` (`company_ok`, and `_constraint_ok` over it; also applied by `reach.py` and `solve.py`), fallback in `suggest.build_suggestions` |
| 4 — risk marks + broken-chain worklist | `deps.py` (`at_risk_chain`), `checks.py` (`broken_chains`) |
| 5 — fleet over-commitment | `checks.py` (`fleet_warnings`; numbered-only, documented deviation) |
| 6 — unit integrity | `checks.py` (`unit_integrity`), `graph.py` (`_unit_timeline_ok` for the planner, `insertion_ok` for the board's drag — same rules, opposite question); `parked.py` (`windows`) enumerates the mid-chain idle blocks the tail-only rule hides, with the fits `insertion_ok` allows |
| 7 — canceled excluded, retained | `engine/__init__.py` (inside `_split`) |
| 8 — graph model, greedy + exact | `graph.py` (edges = the model), `suggest.py` + `plan.py` (greedy consumers), `solve.py` (exact consumer — same edges, solved optimally) |
| 9 — human-in-the-loop ranking | `suggest.py` (engine never assigns; `plan` is provisional; only `repo.accept` writes) |
| 10 — non-49 Pax is ad hoc only | `graph.py` (`CHARTERED_PAX`, `numbered_unit_ok`), applied `feasible_edges` + `insertion_verdict`, `reach.py`, `solve.py`; imported violations listed by `checks.unit_integrity` as `pax_mismatch` |
| 11 — ad hoc hires are cancellable at 21 days | `adhoc.py` (`ADHOC_CANCEL_NOTICE_DAYS`, `refusal`/`releasable`, `build`), committed by `repo.reclaim_adhoc` behind `_require_adhoc`; `rechain.py` deliberately leaves ad hoc alone |
