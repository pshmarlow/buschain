# Bus Trip Chaining System — Build Brief (v5)

## Purpose & stance
Build a system that routes and chains NEW trip data so buses never drive empty. The
current Excel is the conceptual reference and the interchange format — not data to be
repaired. Guiding line: **formatting compatibility yes, content repair no.** The system
never derives, fixes, or reinterprets content; rows that fail the contract are listed as
non-operable with reasons and pass through untouched.

## Data contract (interchange: xlsx, sheet `Capitals 2026` layout, header row 1)
**Operated columns** — the system reads these and only these:
- `A Status` ∈ {Confirmed, Pending, Canceled}
- `B Project` — numeric id; NOT unique (large groups split across vehicles share it)
- `D Y-N` ∈ {Y, N, empty} + `E B Comp` — constraint pair; both set or both empty;
  B Comp must resolve to a registry company
- `G Start date`, `H End date` — valid dates, start ≤ end
- `I Departure`, `J Arrival` — canonical city codes (registry-validated)
- `N Pax` — informational
- `P Company name` — empty (= needs a bus) | company (= ad hoc) | numbered unit
**Passthrough columns** — never read for logic, preserved verbatim on export:
`C Bus Preference`, `F P Notes`, `K Nationality`, `L OP`, `M Quoted rate`, `O ER`,
`Q C Notes`, and the legacy date grid (columns R+; the system does not maintain it).
**Formatting normalization (allowed):** trim, case-fold, whitespace collapse,
`X trf` / `X(trf)` / `X TRF` → X. **Content interpretation (forbidden):** deriving
constraints from notes, repairing dates, parsing free-text arrival strings.

## Registry (system-owned)
Companies · numbered units per company · fleet_size · optional display note. Seedable
once from the `Information` sheet, maintained in the system thereafter. Every
`Company name` and `B Comp` value must resolve here; city codes likewise come from a
closed registry.

## Core concepts
- **Demand:** Confirmed or Pending trip with empty Company. Confirmed = firm;
  Pending = at-risk (may cancel).
- **Numbered unit** (`Ollex 1`) = physical vehicle, chainable. **Bare company** (`Ollex`)
  = ad hoc — "some bus from this company": a legitimate assignment that is
  chain-invisible (generates no supply; exempt from per-unit overlap rules).
- **Supply:** arrivals of assigned numbered-unit trips.

## Chaining rules (derived from the reference data, validated there)
1. A unit arriving in city C on day D can take an uncovered trip departing C on
   **D+1 … D+4** (max 3 rest days; rest = gap − 1). Same-day handover is invalid.
2. City match = equality on canonical codes.
3. **Constraints (hard, company-level):** `Y` + company X → only X may serve the trip —
   numbered X units first (chaining), **ad hoc X as the fallback suggestion when no
   chain is feasible**. `N` + X → X never. Empty = unconstrained.
4. **Risk:** every suggestion lists the Pending trips it depends on. Any cancellation →
   recompute → broken-chain worklist of downstream assignments that relied on it.
5. **Fleet check:** per company per day, simultaneous non-canceled assigned trips
   (numbered + ad hoc) ≤ fleet_size → otherwise an over-commitment warning.
6. **Unit integrity:** a numbered unit's assigned trips never overlap; each consecutive
   pair satisfies rule 1 + same-city, or is an explicit ER. Violations are listed, never
   auto-fixed.
7. Canceled trips: excluded from matching, retained for audit.
8. **Objective:** minimize empty runs / maximize chained links. Greedy per-arrival
   matching in v1, but model trips + feasible links as a graph so a min-cost-flow / ILP
   optimizer can replace greedy without model changes (registry day rates and ER fees
   are the future cost function).
9. **Human-in-the-loop:** ranked candidates (gap ascending), both directions — per free
   unit and per uncovered trip; a person accepts; acceptance writes the assignment.
   Nothing is ever auto-assigned.

## Interchange behavior
Import an xlsx in the contract layout → operate on the operated columns → export the
same layout: system-written values land in their columns (P for assignments, A for
status changes made in-system), passthrough columns and non-operable rows byte-preserved.
New trips may be entered in-system or arrive via xlsx; one contract either way.

## v1 outputs
Contract report (operable rows; non-operable rows + reasons) · per-unit timelines
(ad hoc assignments listed per company, marked chain-invisible) · suggestions view
(both directions, ranked, constraint-filtered, at-risk marks, ad hoc fallback for
Y-rows) · broken-chain worklist · accept action · xlsx export.

## Acceptance tests (property-based)
1. **Round-trip:** import → export with no in-system changes reproduces every
   passthrough cell and every non-operable row exactly.
2. Every source row is either operable or listed with a reason; nothing silently dropped.
3. Every consecutive assigned pair on every numbered unit satisfies the rules or is
   enumerated (explicit ER / integrity item).
4. Blanking any discovered chained assignment → the suggester re-proposes that link
   among top candidates.
5. No suggestion ever violates a Y/N rule; a Y-row with no feasible numbered chain
   receives the ad hoc fallback of that company.
6. Canceled trips never appear in matching output; ad hoc assignments never appear as
   supply.
7. Days where a company exceeds fleet_size are reported.

## Out of scope v1
Any legacy repair or derivation (incl. populating Y-N/B Comp from notes); parsing
P Notes, C Notes, or Information free text into rules; per-unit seat capacity; cost
optimization; paid repositioning proposals beyond honoring explicit assignments;
maintaining the legacy date grid. Tech stack: builder's choice; keep the matching core
pure and unit-testable, separate from the importer/exporter.
