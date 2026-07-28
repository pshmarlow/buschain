"""AT3: every consecutive assigned pair on every numbered unit satisfies the
chain rules or is enumerated (explicit ER / integrity finding)."""
from hypothesis import given, settings

from buschain import contract, engine
from buschain.engine.graph import MAX_GAP_DAYS, MIN_GAP_DAYS
from tests.strategies import registries, reports


def assert_pairs_covered(report, registry):
    result = engine.evaluate(report, registry)
    enumerated = {(f.earlier, f.later) for f in result.integrity}
    for unit_key, trip_ids in result.timelines.items():
        by_id = {t.id: t for t in report.trips}
        for e_id, l_id in zip(trip_ids, trip_ids[1:]):
            earlier, later = by_id[e_id], by_id[l_id]
            gap = (later.start - earlier.end).days
            chains = (MIN_GAP_DAYS <= gap <= MAX_GAP_DAYS
                      and earlier.arr_city == later.dep_city
                      and later.start > earlier.end)
            assert chains or (e_id, l_id) in enumerated, (
                f"unenumerated non-chaining pair {e_id} -> {l_id} on {unit_key}"
            )


@given(report=reports(), registry=registries())
@settings(max_examples=200, deadline=None)
def test_property_pairs_covered(report, registry):
    assert_pairs_covered(report, registry)


def test_real_data_pairs_covered(dataset, registry):
    report = contract.validate(dataset, registry)
    assert_pairs_covered(report, registry)
