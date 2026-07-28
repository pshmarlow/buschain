"""AT4: blanking any discovered chained assignment -> the suggester
re-proposes that exact link among the top candidates."""
from dataclasses import replace

from buschain import contract, engine
from buschain.engine.graph import MAX_GAP_DAYS, MIN_GAP_DAYS
from buschain.model import ContractReport, Status


def test_every_discovered_chain_link_is_reproposed(dataset, registry):
    report = contract.validate(dataset, registry)
    result = engine.evaluate(report, registry)
    by_id = {t.id: t for t in report.trips}

    discovered = []
    for trip_ids in result.timelines.values():
        for e_id, l_id in zip(trip_ids, trip_ids[1:]):
            earlier, later = by_id[e_id], by_id[l_id]
            gap = (later.start - earlier.end).days
            if (MIN_GAP_DAYS <= gap <= MAX_GAP_DAYS
                    and earlier.arr_city == later.dep_city
                    and later.status is not Status.CANCELED):
                discovered.append((earlier, later))

    assert discovered, "reference data should contain chained assignments"

    missed = []
    for earlier, later in discovered:
        blanked = replace(later, assignment=None)
        trips = tuple(blanked if t.id == later.id else t for t in report.trips)
        re_result = engine.evaluate(
            ContractReport(trips=trips, non_operable=report.non_operable), registry
        )
        candidates = re_result.suggestions_by_trip.get(blanked.id, ())
        if not any(s.supply == earlier.id for s in candidates):
            missed.append((earlier.id, later.id))
    assert not missed, f"{len(missed)}/{len(discovered)} links not re-proposed: {missed[:5]}"
