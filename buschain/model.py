"""Domain model: frozen dataclasses and enums shared by every layer.

This module has zero I/O and depends only on `buschain.normalize`. All types
are immutable so the engine can be a pure function over them — mutation lives
exclusively in the store layer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Literal, Mapping

from . import normalize


class Status(StrEnum):
    CONFIRMED = "Confirmed"
    PENDING = "Pending"
    CANCELED = "Canceled"

    @classmethod
    def parse(cls, raw: str) -> "Status | None":
        key = normalize.casefold_key(raw)
        for member in cls:
            if normalize.casefold_key(member.value) == key:
                return member
        return None


@dataclass(frozen=True, order=True)
class TripId:
    """Stable identity between import and export.

    For sheet rows the key is the 1-based source row number — stable because
    the system never deletes or reorders source rows. Manual trips get a
    store-assigned sequence number and only receive a sheet row at export time.
    """
    origin: Literal["sheet", "manual"]
    key: int


@dataclass(frozen=True)
class Assignment:
    company: str            # canonical registry key
    unit: int | None        # None => ad hoc (bare company): chain-invisible


@dataclass(frozen=True)
class Trip:
    """An operable row. Only ever constructed by contract.validate()."""
    id: TripId
    status: Status
    project: int | None
    yn: Literal["Y", "N"] | None        # yn and constraint_company: both or neither
    constraint_company: str | None      # canonical registry key
    start: date
    end: date
    dep_city: str                       # canonical city code
    arr_city: str
    # Read for logic since rule 10: a stated Pax other than graph.CHARTERED_PAX
    # bars every numbered unit, leaving the trip to an ad hoc bus. Blank still
    # means "unstated", which the rule lets through.
    pax: int | None
    assignment: Assignment | None       # None + status Confirmed/Pending => demand
    # The ER column is read ONLY to label integrity gaps as explicit ER (per
    # user decision) — never a matching input, never written back.
    er_marked: bool = False
    # Every contract column verbatim, keyed by title, for the UI's column
    # picker. Carried, never read by the engine: the columns the engine needs
    # are the parsed fields above. Defaulted so tests can build bare trips.
    display: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class NonOperableRow:
    id: TripId
    reasons: tuple[str, ...]
    display: Mapping[str, str]          # raw column values, verbatim, for the report UI


@dataclass(frozen=True)
class ContractReport:
    trips: tuple[Trip, ...]
    non_operable: tuple[NonOperableRow, ...]


@dataclass(frozen=True)
class Company:
    name: str                           # display name (Information-sheet spelling wins)
    note: str | None
    units: frozenset[int]

    @property
    def fleet_size(self) -> int:
        """Chartered buses = numbered units.

        Derived, not stored: these were two independently-written fields and
        they drifted (a company sat at fleet_size 4 with units 1-5, and units
        added in-app left fleet_size null). One number cannot disagree with
        itself. The Information sheet's 'Fleets' column sets the unit list on
        import, so the sheet still owns the value — it just owns it once.
        """
        return len(self.units)


@dataclass(frozen=True)
class Registry:
    """Closed registries for companies/units and city codes.

    Keys are normalized (whitespace-collapsed, casefolded) so 'RunWay' and
    'Runway ' resolve to one company; values keep a display spelling.
    """
    companies: Mapping[str, Company]    # canonical key -> Company
    cities: Mapping[str, str]           # casefold key -> canonical display code

    def resolve_city(self, raw: str) -> str | None:
        # trf-suffix stripping is an allowed formatting normalization:
        # 'CPH trf' / 'CPH(trf)' / 'CPH TRF' all mean the city CPH.
        stripped = normalize.strip_trf(normalize.collapse_ws(normalize.trim(raw)))
        return self.cities.get(normalize.casefold_key(stripped))

    def resolve_company(self, raw: str) -> tuple[str, int | None] | None:
        """Resolve a Company-name value to (canonical key, unit or None).

        Tries the whole string as a bare company first, then a trailing
        integer as a numbered unit ('Kautra 1'). The unit must exist in the
        registry — an unknown unit number does not resolve (closed registry,
        no auto-creation).
        """
        cleaned = normalize.collapse_ws(normalize.trim(raw))
        key = normalize.casefold_key(cleaned)
        if key in self.companies:
            return key, None
        head, _, tail = cleaned.rpartition(" ")
        # isdecimal, not isdigit: isdigit accepts superscripts ('²') that
        # int() then refuses — a crash instead of "does not resolve".
        if head and tail.isdecimal():
            head_key = normalize.casefold_key(head)
            unit = int(tail)
            company = self.companies.get(head_key)
            if company is not None and unit in company.units:
                return head_key, unit
        return None


@dataclass(frozen=True)
class AcceptedLink:
    """Provenance of an in-system acceptance (supply None = ad hoc fallback)."""
    supply: TripId | None
    demand: TripId


# ---------------------------------------------------------------------------
# Engine outputs
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Suggestion:
    kind: Literal["chain", "adhoc_fallback"]
    supply: TripId | None               # arrival trip feeding the link; None for ad hoc
    demand: TripId
    company: str
    unit: int | None
    gap_days: int | None                # demand.start - supply.end; None for ad hoc
    at_risk: tuple[TripId, ...]         # Pending trips this link depends on
    rank: int
    # Accepting this needs an empty run first — see PlannedLink.reposition.
    reposition: bool = False
    from_city: str = ""
    to_city: str = ""


@dataclass(frozen=True)
class IntegrityFinding:
    company: str
    unit: int
    earlier: TripId
    later: TripId
    # pax_mismatch names ONE trip (earlier == later): a non-49 booking sitting
    # on a numbered unit, which only an imported sheet can produce — see
    # checks.unit_integrity.
    kind: Literal["overlap", "gap_out_of_window", "city_mismatch",
                  "explicit_er", "reposition", "pax_mismatch"]


@dataclass(frozen=True)
class FleetWarning:
    company: str
    day: date
    count: int
    fleet_size: int


@dataclass(frozen=True)
class BrokenChainItem:
    assignment: TripId                  # downstream assigned trip now unsupported
    canceled_dependency: TripId
    link: AcceptedLink


@dataclass(frozen=True)
class PlannedLink:
    """One hop of a proposed chain. Derived state — never persisted.

    Carries the same four fields repo.accept() takes, so committing a planned
    link is the ordinary accept path with no special case.
    """
    supply: TripId
    demand: TripId
    company: str
    unit: int
    gap_days: int
    step: int                           # position within its chain, 0-based
    # An empty repositioning run precedes this hop: the bus ends the supply trip
    # in from_city and must be driven to to_city to start the demand trip. Rule 6
    # calls that pair a city mismatch unless it is an explicit ER, so a link
    # carrying this flag is proposing the ER, not ignoring the rule.
    reposition: bool = False
    from_city: str = ""
    to_city: str = ""


@dataclass(frozen=True)
class ProposedChain:
    company: str
    unit: int
    links: tuple[PlannedLink, ...]      # in chain order (step ascending)


@dataclass(frozen=True)
class Plan:
    """What the fleet could cover beyond what is already committed.

    Recomputed on every evaluate() and never stored: persisting it would put
    provisional assignments into cell_overrides, which IS the xlsx export.
    """
    objective: str
    chains: tuple[ProposedChain, ...]
    covered: frozenset[TripId]
    idle_days: int


@dataclass(frozen=True)
class EngineResult:
    suggestions_by_unit: Mapping[tuple[str, int], tuple[Suggestion, ...]]
    suggestions_by_trip: Mapping[TripId, tuple[Suggestion, ...]]
    integrity: tuple[IntegrityFinding, ...]
    fleet_warnings: tuple[FleetWarning, ...]
    broken_chains: tuple[BrokenChainItem, ...]
    timelines: Mapping[tuple[str, int], tuple[TripId, ...]]
    adhoc_by_company: Mapping[str, tuple[TripId, ...]]
    # Uncovered trips some sequence of accepts could chain into WITHOUT an empty
    # run. Contention is ignored, so this is NOT a coverage forecast. It is also
    # not a superset of `plan.covered`: the planner may spend a repositioning
    # run to reach a trip this walk cannot (see reach.py).
    reachable: frozenset[TripId] = frozenset()
    # Multi-hop chains built over the same edges (engine/plan.py). Provisional
    # by construction: recomputed here every call, never written anywhere.
    plan: Plan = field(
        default_factory=lambda: Plan(
            objective="coverage", chains=(), covered=frozenset(), idle_days=0))


@dataclass(frozen=True)
class RechainResult:
    """A proposed rearrangement of the future — derived state, like Plan.

    `engine` was computed as if every trip in `released` were unassigned, so
    its plan is what the fleet would do if those assignments were taken back.
    Nothing is written until someone commits it.
    """
    as_of: date                         # trips starting on or before this are untouched
    released: tuple[TripId, ...]        # assignments the rearrangement takes back
    # Released trips the replan could not re-cover. Committing leaves these with
    # no bus at all, so it is the number that decides whether to commit — the
    # replan is not monotone and can strand a trip that currently has one.
    stranded: tuple[TripId, ...]
    engine: EngineResult
    # Future assignments the rearrangement is not allowed to touch, so a UI can
    # explain why 60 is the answer on a dataset with 448 future trips. Counted
    # here rather than re-derived by the client: the source-file boundary is
    # decided in exactly one place (repo.system_assigned_ids) and stays there.
    frozen_source: int = 0
    frozen_locked: int = 0


@dataclass(frozen=True)
class AdhocReclaimResult:
    """A proposed move of ad hoc work onto chartered buses — derived state.

    `engine` was computed as if every trip in `eligible` were unassigned, so its
    plan is what the fleet would do if those hires were cancelled. Nothing is
    written until someone commits it.

    The asymmetry with RechainResult is the point: a released assignment leaves
    a trip with no bus, but an ad hoc trip already HAS one. So the commit writes
    only `reclaimed`, and `stayed_adhoc` keeps its hire — this action cannot
    strand anything, which is why there is no field for it.
    """
    as_of: date
    # Carried rather than let the client hardcode 21, for the same reason
    # graph.window_rules() is served to the browser: the engine owns the number.
    notice_days: int
    eligible: tuple[TripId, ...]        # ad hoc hires that could still be cancelled
    reclaimed: tuple[TripId, ...]       # eligible trips the replan puts on a numbered bus
    stayed_adhoc: tuple[TripId, ...]    # eligible, uncovered — keep their hire, untouched
    # Trips the CURRENT plan proposes and this one does not. NOT stranded and not
    # a loss of a bus: they have none today either, and what they lose is a
    # proposal. Named `regressed` because "lost their bus" reads as `stranded`,
    # and a dispatcher who reads it that way will refuse a strictly better plan.
    regressed: tuple[TripId, ...]
    # Eligible trips whose company name came from the spreadsheet. This action is
    # deliberately allowed to cancel those (an ad hoc booking is a purchase, not
    # a fact about which vehicle exists) — so the carve-out is surfaced here
    # rather than left silent. Filled by the web layer: provenance is a store
    # concept and the engine stays ignorant of it.
    from_source: tuple[TripId, ...]
    engine: EngineResult
    # Live ad hoc trips the action will not touch, counted so a panel can say
    # why. Mutually exclusive by construction — see adhoc.refusal's test order.
    frozen_locked: int = 0
    frozen_pax: int = 0                 # not 49: no numbered unit could take them (rule 10)
    frozen_notice: int = 0              # inside the cancellation window (rule 11)
