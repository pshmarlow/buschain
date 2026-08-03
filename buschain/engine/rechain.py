"""Master re-chain: replan the future without disturbing the past.

Everything else the planner does is additive — it proposes buses for trips that
have none, and a committed assignment is immovable by construction (see
plan.py's module docstring). This is the one place that takes an assignment
BACK, so a fleet can be rearranged rather than only extended.

Five boundaries make that safe, and none of them is negotiable:

* Only assignments this app wrote are released. A company name that arrived on
  the spreadsheet is source data for AUTOMATION — a person may clear it (the
  Data tab, the worklist and the board drag all write the same Company-cell
  override), but a bulk replan must never overwrite it silently. That is why
  the boundary lives in repo.system_assigned_ids.
* Only trips that have not started yet. A bus already driving cannot be
  re-decided, whatever the plan would prefer.
* Locked trips are exempt on top of both.
* Only NUMBERED assignments. Ad hoc hires are engine/adhoc.py's business — see
  the comment on future_assigned. The two release sets are disjoint by
  construction, and test_rechain.py pins that.
* Only trips inside the planning horizon. Never release what you cannot
  replan: the same horizon bounds evaluate's demand, so an assignment past the
  cutoff would be taken back and then be invisible to the planner that is
  supposed to re-place it — stranded by arithmetic, every time, on a trip that
  had a bus a moment ago. This boundary is not a policy preference like the
  others; without it the horizon silently turns re-chain into a wrecking ball.

Nothing here writes, and nothing here plans: it restates the world as if the
released assignments had never been made and hands that to the ordinary
evaluate(). One planner, one set of rules — a second one that "replans" would
be a second place for the chaining rules to be subtly different.

The result is NOT monotone. Releasing N assignments does not guarantee N come
back: the replan may spend a bus on a trip the old arrangement left uncovered
and strand the one it took the bus from. Measured on the reference data with
the idle objective, 18 of 60 released trips came back with no bus at all. That
is why `stranded` is part of the result rather than something a caller has to
think to derive, and why the web layer previews before it commits.
"""
from __future__ import annotations

import dataclasses
from datetime import date
from typing import AbstractSet, Sequence

from ..model import (
    AcceptedLink, ContractReport, Registry, RechainResult, Status, TripId,
)
from . import evaluate, plan as plan_mod


def build(
    report: ContractReport,
    registry: Registry,
    accepted: Sequence[AcceptedLink] = (),
    *,
    system_assigned: AbstractSet[TripId],
    locked: AbstractSet[TripId] = frozenset(),
    as_of: date,
    horizon: date | None = None,
    objective: plan_mod.Objective = plan_mod.DEFAULT_OBJECTIVE,
    reposition: bool = True,
) -> RechainResult:
    """What the fleet would do if the releasable future were replanned.

    system_assigned: trips whose assignment came from this app rather than the
    source file (repo.system_assigned_ids).

    horizon: the planner's far bound, applied to the RELEASE set as well as to
    the replan — see the fifth boundary in the module docstring.
    """
    # Numbered assignments only. Releasing one gives a bus back to the pot and
    # the worst case is a trip back on the worklist. Releasing an ad hoc one
    # CANCELS A PURCHASE, which has a deadline (rule 11) and must never end with
    # the trip on no bus at all — two rules this function does not implement and
    # should not. engine/adhoc.py owns them, and its result keeps a trip's hire
    # when the replan cannot re-place it. Without this line an app-written ad
    # hoc trip was released and then guaranteed to strand, since plan.chains
    # only ever contain numbered units.
    future_assigned = [
        t for t in report.trips
        if t.assignment is not None
        and t.assignment.unit is not None
        and t.status is not Status.CANCELED
        and t.start > as_of
    ]
    # The horizon cut lives HERE and not in the `released` comprehension below,
    # so a trip past the cutoff is counted as untouched work rather than as
    # something the app refused to hand back — the same distinction
    # frozen_source and frozen_locked already draw, and the reason all three
    # counters are derived from one list.
    in_horizon = [
        t for t in future_assigned
        if horizon is None or t.start <= horizon
    ]
    released = frozenset(
        t.id for t in in_horizon
        if t.id in system_assigned and t.id not in locked
    )

    stripped = ContractReport(
        trips=tuple(
            dataclasses.replace(t, assignment=None) if t.id in released else t
            for t in report.trips
        ),
        non_operable=report.non_operable,
    )
    # Links pointing at a released trip go both ways. The one INTO it dies with
    # the assignment; the one OUT of it claims that a bus which may now be
    # somewhere else entirely fed the next trip, so it cannot be shown either.
    # repo.rechain deletes exactly these rows, so the preview and the commit
    # agree about what survives.
    kept = [
        link for link in accepted
        if link.demand not in released and link.supply not in released
    ]

    result = evaluate(stripped, registry, kept, objective, reposition,
                      locked, as_of, horizon)
    # The three counters partition future_assigned with `released`, so a panel
    # can show four numbers that sum to the total. That is why frozen_source and
    # frozen_locked count over in_horizon rather than future_assigned: a
    # source-written trip past the cutoff is out of scope first and frozen by
    # provenance second, and counting it twice would make the arithmetic lie.
    return RechainResult(
        as_of=as_of,
        horizon=horizon,
        released=tuple(sorted(released)),
        stranded=tuple(sorted(released - result.plan.covered)),
        engine=result,
        frozen_source=sum(
            1 for t in in_horizon if t.id not in system_assigned),
        frozen_locked=sum(
            1 for t in in_horizon if t.id in system_assigned and t.id in locked),
        frozen_horizon=len(future_assigned) - len(in_horizon),
    )


def commit_links(
    result: RechainResult,
) -> list[tuple[TripId, str, int | None, TripId | None]]:
    """The plan's links in the order repo.rechain has to write them.

    Chain order within each chain: a link's supply is the previous link's
    demand, and a released trip is frequently the supply of a later hop — so
    the write order is what makes the store's own "supply must be assigned
    first" check pass rather than something to work around.
    """
    return [
        (link.demand, link.company, link.unit, link.supply)
        for chain in result.engine.plan.chains
        for link in chain.links
    ]
