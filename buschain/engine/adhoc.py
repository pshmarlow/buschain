"""Rule 11: take work back off ad hoc buses and give it to the charter.

An ad hoc assignment (bare company) means a bus hired in from someone's yard.
It is chain-invisible everywhere else in this engine — never supply, no
timeline — and, because _split reads any assigned trip as covered, it is not
demand either. That makes it a DEAD END: once a trip is on an ad hoc lane, no
amount of chartered capacity freeing up later will ever reclaim it, and the
fleet keeps paying for work it could now do itself.

This module is the one move that undoes that. Like rechain.py it plans nothing:
it restates the world as if the eligible hires had been cancelled and hands
that to the ordinary evaluate(). One planner, one rule set.

Two differences from a re-chain, and both are the whole design:

* A released assignment leaves a trip with no bus; an ad hoc trip already HAS
  one. So this never strands: a trip the replan cannot place keeps its hire and
  is reported in `stayed_adhoc`, and the commit writes only `reclaimed`.
* Eligibility does not ask WHO wrote the company name — see releasable().

AT6 ("ad hoc assignments never appear as supply") is unaffected, and the reason
is worth stating because it looks close: evaluate() is never called here with an
ad hoc assignment still attached to a trip it might chain from. A trip is either
untouched — still bare-company, still excluded from supply by _split — or
stripped to assignment=None, at which point it is demand like any other and
becomes supply only if the PLAN gives it a numbered unit.
"""
from __future__ import annotations

import dataclasses
from collections import Counter
from datetime import date
from typing import AbstractSet, Sequence

from ..model import (
    AcceptedLink, AdhocReclaimResult, ContractReport, Registry, Status, Trip,
    TripId,
)
from . import evaluate, graph, plan as plan_mod

# Rule 11: a hired-in bus can be sent away without charge up to three weeks
# before the trip; inside that window the hire stands whatever the fleet could
# do instead.
#
# This is the first now-to-trip measurement in the engine — everything else
# (min_gap_days, MAX_REST_DAYS) is trip-to-trip, and `as_of` has until now been
# a binary past/future horizon. It deliberately does NOT live beside
# CHARTERED_PAX and MIN_GAP_DAYS in graph.py: those are facts about a bus and
# two trips, timeless, and every function in that module is a pure predicate
# over trips with no notion of "now". A cancellation deadline is a fact about a
# purchase from a supplier and means nothing except measured from today. Put in
# the one module that never receives `as_of`, it would sooner or later be
# applied to a trip-to-trip gap, where it would silently mean something else.
ADHOC_CANCEL_NOTICE_DAYS = 21


def refusal(trip: Trip, *, locked: AbstractSet[TripId], as_of: date) -> str | None:
    """Which rule forbids cancelling this hire — None if it may be cancelled.

    The tests run in this order so the counters they feed are mutually
    exclusive and a panel's arithmetic adds up (the same discipline as
    RechainResult.frozen_source / frozen_locked).

    The notice test SUBSUMES rechain's "only trips that have not started yet"
    boundary — 21 days out is comfortably in the future — which is why that
    check is not repeated here. It is not missing.
    """
    if trip.assignment is None or trip.assignment.unit is not None:
        return "not_adhoc"
    if trip.status is Status.CANCELED:
        return "canceled"
    if trip.id in locked:
        return "locked"
    # Rule 10, applied here as a POINTLESSNESS rule rather than a safety one: a
    # trip whose Pax is not 49 gets no edge to any numbered unit (graph.py), so
    # cancelling its hire could only ever produce a stayed_adhoc entry. Counted
    # instead, so the panel can say "only an ad hoc bus can take those".
    if not graph.numbered_unit_ok(trip):
        return "pax"
    if (trip.start - as_of).days < ADHOC_CANCEL_NOTICE_DAYS:
        return "notice"
    return None


def releasable(trip: Trip, *, locked: AbstractSet[TripId], as_of: date) -> bool:
    """May this ad hoc hire be cancelled?

    Note what is NOT a parameter: who wrote the company name. Everywhere else
    automation refuses to touch a value that arrived on the spreadsheet
    (repo.system_assigned_ids), and that boundary is deliberately not applied
    here. An ad hoc booking is a cancellable purchase, not a fact about which
    vehicle exists: the sheet naming "Ollex" records that somebody hired a bus,
    and cancelling that inside the notice period is a decision this app is
    allowed to PROPOSE.

    Two things make that safe, and neither is a convention:
    * the only caller is a preview whose commit is a human click — there is no
      background sweep, and adding one would need this comment answered first;
    * repo._require_adhoc re-asks the question against the row about to be
      written and refuses anything that is not a bare-company cell, so the
      carve-out cannot widen by accident.
    """
    return refusal(trip, locked=locked, as_of=as_of) is None


def build(
    report: ContractReport,
    registry: Registry,
    accepted: Sequence[AcceptedLink] = (),
    *,
    locked: AbstractSet[TripId] = frozenset(),
    as_of: date,
    objective: plan_mod.Objective = plan_mod.DEFAULT_OBJECTIVE,
    reposition: bool = True,
) -> AdhocReclaimResult:
    """What the fleet would do if the cancellable hires were cancelled."""
    frozen: Counter[str] = Counter()
    eligible: set[TripId] = set()
    for t in report.trips:
        why = refusal(t, locked=locked, as_of=as_of)
        if why is None:
            eligible.add(t.id)
        elif why not in ("not_adhoc", "canceled"):
            # Canceled and non-ad-hoc trips are not "untouched ad hoc work" and
            # would only inflate the counts a panel shows next to `eligible`.
            frozen[why] += 1

    stripped = ContractReport(
        trips=tuple(
            dataclasses.replace(t, assignment=None) if t.id in eligible else t
            for t in report.trips
        ),
        non_operable=report.non_operable,
    )
    # Links both ways, as in rechain.build. The difference from there is that
    # the PREVIEW drops rows for every eligible trip while the COMMIT deletes
    # rows only for the ones actually reclaimed — a trip that keeps its hire
    # keeps its assignment, and therefore its provenance. Preview and commit
    # deliberately do not agree here, because the preview is asking a
    # hypothetical ("if all of these were cancelled") that the commit only
    # partly acts on.
    kept = [
        link for link in accepted
        if link.demand not in eligible and link.supply not in eligible
    ]

    # The baseline runs on the untouched report with the SAME objective,
    # reposition, locked and as_of, so `regressed` isolates the effect of the
    # reclaim rather than of the horizon — the same reason optimal_plan hands
    # back the greedy count alongside the solved one.
    baseline = evaluate(report, registry, accepted, objective, reposition,
                        locked, as_of)
    result = evaluate(stripped, registry, kept, objective, reposition,
                      locked, as_of)

    covered = result.plan.covered
    return AdhocReclaimResult(
        as_of=as_of,
        notice_days=ADHOC_CANCEL_NOTICE_DAYS,
        eligible=tuple(sorted(eligible)),
        reclaimed=tuple(sorted(eligible & covered)),
        stayed_adhoc=tuple(sorted(eligible - covered)),
        # Reclaims and genuinely uncovered trips compete in one pool, ranked by
        # the objective — which cannot see that one of them is already handled
        # and the other is not, because the objective counts links and knows
        # nothing about money. Deliberately not fixed by bolting a tiebreak onto
        # plan.py: one planner, one rule set. This list is the alternative — the
        # human sees what the trade costs and overrules it by not committing.
        regressed=tuple(sorted(baseline.plan.covered - covered)),
        from_source=(),                 # filled by the web layer; see the model
        engine=result,
        frozen_locked=frozen["locked"],
        frozen_pax=frozen["pax"],
        frozen_notice=frozen["notice"],
    )


def commit_links(
    result: AdhocReclaimResult,
) -> list[tuple[TripId, str, int | None, TripId | None]]:
    """The plan's links in the order repo.reclaim_adhoc has to write them.

    The whole replan, not only the hops that land on a reclaimed trip. That is
    forced rather than chosen: a reclaimed trip is frequently fed by a hop whose
    demand is an ordinary uncovered trip, and dropping that hop would leave the
    store's "supply must be assigned first" check looking at a bus nobody
    assigned. Every extra link here is one the ordinary "accept the whole plan"
    button would have written anyway — but the panel has to say so, or the size
    of the write is a surprise.
    """
    return [
        (link.demand, link.company, link.unit, link.supply)
        for chain in result.engine.plan.chains
        for link in chain.links
    ]
