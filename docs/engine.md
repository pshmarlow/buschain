# The routing/chaining engine

How `buschain/engine/` decides which bus can take which trip, and how it strings
those decisions into chains. Written for the maintainer: plain language first,
code pointers second. Rule numbers refer to `bus-chaining-build-brief.md`.

https://claude.ai/code/artifact/d2953d15-5464-4cee-a5f4-5b6442da750b

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

## 3. The pipeline: what `evaluate()` does

`evaluate()` (`buschain/engine/__init__.py:21`) is the single entry point and a
pure function: `(contract report, registry, accepted links, objective,
reposition, locked, as_of) → EngineResult`. No files, no clock, no database —
which is why every mutation elsewhere in the app is simply "change state,
re-run evaluate", with zero cache-invalidation logic anywhere.

```
report.trips
  │ drop Canceled (kept only for audit / broken-chain roots — rule 7)
  ▼
live trips ──┬─ assigned to numbered unit ──► SUPPLY + per-unit timelines
             ├─ assigned to bare company ──► ad hoc lists (chain-invisible)
             └─ unassigned, not locked,   ──► DEMAND
                starts after as_of
  ▼
graph.feasible_edges(supply, demand, timelines)     ← the rules live here
  ├──► suggest.build_suggestions  → ranked one-hop candidates, both directions
  ├──► plan.build_plan            → multi-hop provisional chains
  ├──► reach.reachable_demands    → "could any chain ever get here?"
  └──► checks.*                   → integrity findings, fleet warnings, broken chains
```

The supply/demand reading itself lives in `_split`
(`buschain/engine/__init__.py:80`), shared by `evaluate()` and the exact
solver's entry point `optimal_plan()` (`__init__.py:113`) so the two can never
disagree about what is in play. `optimal_plan` is deliberately *not* part of
`evaluate()` — see the solver section below.

The `EngineResult` fields, and who they're for:

- `suggestions_by_unit` / `suggestions_by_trip` — the accept UI (rule 9: a
  human accepts; nothing is ever auto-assigned).
- `plan` — the multi-hop proposal panel. Provisional by construction.
- `reachable` — lets the board distinguish "waiting on an earlier decision"
  from "no chain will ever reach this".
- `integrity`, `fleet_warnings`, `broken_chains` — reports. Listed, never
  auto-fixed (rules 4–6).
- `timelines`, `adhoc_by_company` — display.

(Canceled trips are dropped inside `_split` — rule 7: excluded from matching,
retained for audit and as broken-chain roots.)

Two parameters keep purity honest: `as_of` is *passed in* (an engine that read
`date.today()` would give different answers on different days for the same
state, untestable), and `reposition` reaches **both** the edges and the plan —
one switch, so the suggestion board and the planner can't disagree about
whether empty runs exist.

## 4. When can a bus take a trip? (`graph.py`)

`feasible_edges` (`buschain/engine/graph.py:133`) emits an edge only when four
gates all pass. This edge list *is* the optimization model (rule 8): the greedy
v1 ranks it, and a future min-cost-flow/ILP solver would consume the identical
edges plus a cost function.

**Gate 1 — city and time window (rules 1–2).** The gap is
`demand.start − supply.end` in days. The floor depends on where the bus is and
where it must be (`min_gap_days`, `graph.py:39`):

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

**Gate 2 — Y/N constraint (rule 3).** `_constraint_ok` (`graph.py:82`):
`Y` + company X → only X's buses may serve; `N` + X → X never; blank →
anyone. Hard filter, company-level.

**Gate 3 — the unit's whole timeline stays valid (rule 6).**
`_unit_timeline_ok` (`graph.py:97`) simulates the accept: no overlap with any
existing trip, no existing trip already sitting inside the gap, and if the unit
has a later trip, that successor must still chain from the *demand* trip
afterwards (unless the successor is ER-marked — its explicit repositioning
covers the move). Two tolerances keep one bad row from freezing a whole bus:
trips already *overlapping* the supply trip are ignored here (they're
pre-existing violations the integrity report enumerates — e.g. the typo'd-year
row in the reference data), and a successor pair that was *already* broken
can't get vetoed further — inserting a trip into an already-invalid gap
typically shrinks it.

**Gate 4 — repositioning only off the tail.** An empty-run edge
(`arr_city ≠ dep_city`) is emitted only when `reposition=True` **and** nothing
follows the supply trip on that unit (`graph.py:159`). Mid-chain, driving the
bus somewhere else would strand whatever comes next in the wrong city.

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

`chain_order` (`plan.py:200`) gives each chain's demands in accept order —
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
model (`solve.py:117-247`).

Three properties keep the result trustworthy: it restates no rules (windows
from `min_gap_days`, Y/N from `reach._company_ok`, tail-only repositioning);
every link is **certified** by replaying the solution hop-by-hop through
`feasible_edges` before it reaches the caller (`_certify`, `solve.py:254`);
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
  (`gap − min_gap_days(from, to)`, `plan.py:195`) — days spent driving are
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
| `optimal_plan` (`solve.py:95`, on demand) | "Here's the *most* the fleet could cover." Exact, certified, deterministic. | Cost beyond empty-run counts — and it's seconds, not milliseconds, hence preview-only. |
| `reachable` (`reach.py:48`) | "Some accept sequence could chain into this trip *without any empty run*." | Contention entirely — and repositioning, on purpose. |

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

One more suggestion rule (rule 3/5, `suggest.py:67`): a `Y`-constrained trip
with no feasible *free* chain gets an **ad hoc fallback** suggestion of its
mandated company, ranked last. A repositioning-only chain doesn't suppress the
fallback — that's exactly when "another bus from the same yard" is the obvious
alternative to driving one across the map.

## 8. Master re-chain (`rechain.py`)

Everything above is additive — a committed assignment is immovable supply the
chains route around (that's what makes confirmed links immune to replanning,
with no lock flag needed anywhere). `rechain.build`
(`buschain/engine/rechain.py:41`) is the one place assignments are taken
*back*, and it plans nothing itself: it restates the world as if the released
assignments had never been made and hands that to the ordinary `evaluate()`.
One planner, one rule set — a second "replanner" would be a second place for
the rules to drift.

Three release boundaries, none negotiable:

1. **Only app-written assignments** (`system_assigned`). A company name that
   arrived on the spreadsheet is source data; re-chain must not become the back
   door around `repo.unassign`'s refusal to clear it.
2. **Only trips that haven't started** (`start > as_of`). A bus already
   driving can't be re-decided.
3. **Locked trips are exempt** on top of both.

Accepted links touching a released trip are dropped in both directions
(`rechain.py:80`): the link *into* it dies with the assignment; the link *out*
of it would claim a bus that may now be elsewhere fed the next trip.

The result is **not monotone**: releasing N assignments does not bring N back.
The replan may spend a bus on a previously-uncovered trip and strand the trip
it took the bus from — measured: 18 of 60 released trips came back with no bus
(idle objective, reference data). That's why `stranded` is a first-class field
(it's the number that decides whether to commit) and why the web layer previews
before committing. `frozen_source` / `frozen_locked` counts let the UI explain
why only 60 of 448 future trips were even in play. `commit_links`
(`rechain.py:99`) emits writes in chain order so the store's own
"supply-must-be-assigned-first" check passes naturally.

## 9. Safety nets (`checks.py`, `deps.py`)

All *listed, never auto-fixed*.

- **Unit integrity** (rule 6, `checks.py:56`): every consecutive pair on every
  numbered unit is classified — `overlap` (violation), `gap_out_of_window`,
  `city_mismatch`, `explicit_er` (sheet's ER mark: enumerated fact, not
  violation), or `reposition` (the pair matches an *accepted* link — the
  planner's own approved empty run, listed as a cost worth seeing rather than
  a mismatch to fix).
- **Fleet warnings** (rule 5, `checks.py:18`): per company per day, concurrent
  trips on *numbered units* > fleet size. Deliberate deviation from the brief,
  which counts ad hoc too: fleet size means *chartered* vehicles, while ad hoc
  means "some further bus from their yard" — capacity the charter count says
  nothing about. Counting ad hoc produced 1448 warning-days on the reference
  data (Ollex: 2 chartered, 120 ad hoc trips), burying the real
  over-commitments. Intervals count both endpoints — a bus is occupied on its
  end day.
- **Broken chains** (rule 4, `checks.py:96`): when a supply trip is canceled,
  unassigned, or gone, every accepted link depending on it — transitively down
  the chain — lands on the worklist. Fixpoint iteration, since links arrive in
  any order.
- **At-risk tracing** (rule 4, `deps.py:15`): every suggestion lists the
  Pending trips in its supply's upstream accepted chain — the trips whose
  cancellation would break the plan — so the human sees the risk *before*
  accepting.

## 10. Rule-to-code map

| Brief rule | Where |
|---|---|
| 1 — D+1…D+4 window, max 3 rest days | `graph.py:16` constants, `min_gap_days` `graph.py:39`, applied `graph.py:168` |
| 2 — city match on canonical codes | `graph.py:161` (`arr_city`/`dep_city` equality; codes canonicalized upstream in `model.Registry.resolve_city`) |
| 3 — Y/N constraints + ad hoc fallback | `graph.py:82` (`_constraint_ok`), `reach.py:38`, fallback `suggest.py:66` |
| 4 — risk marks + broken-chain worklist | `deps.py:15` (`at_risk_chain`), `checks.py:96` (`broken_chains`) |
| 5 — fleet over-commitment | `checks.py:18` (`fleet_warnings`; numbered-only, documented deviation) |
| 6 — unit integrity | `checks.py:56` (reporting), `graph.py:97` (`_unit_timeline_ok`, prevention) |
| 7 — canceled excluded, retained | `__init__.py:84` (inside `_split`) |
| 8 — graph model, greedy + exact | `graph.py` (edges = the model), `suggest.py` + `plan.py` (greedy consumers), `solve.py` (exact consumer — same edges, solved optimally) |
| 9 — human-in-the-loop ranking | `suggest.py` (engine never assigns; `plan` is provisional; only `repo.accept` writes) |
