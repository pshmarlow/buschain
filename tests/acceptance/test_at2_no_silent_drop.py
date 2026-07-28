"""AT2: every source row is either operable or listed with a reason —
nothing silently dropped."""
from buschain import contract
from buschain.model import Status


def test_every_row_classified(dataset, registry):
    report = contract.validate(dataset, registry)
    assert len(report.trips) + len(report.non_operable) == len(dataset.rows)


def test_known_junk_rows_are_listed_with_reasons(dataset, registry):
    report = contract.validate(dataset, registry)
    junk_displays = " | ".join(
        " ".join(r.display.values()) for r in report.non_operable
    )
    assert "ferry Turku-STO" in junk_displays
    assert "Karlshamm - CPH" in junk_displays
    assert all(r.reasons for r in report.non_operable)


def test_profile_counts_regression(dataset, registry):
    """Reader/contract regression pinned to the profiled reference data."""
    report = contract.validate(dataset, registry)
    by_status = {s: 0 for s in Status}
    for t in report.trips:
        by_status[t.status] += 1
    # All 1670 rows carry a valid status; operable counts per status can be
    # lower than the raw profile (741/217/712) only because of OTHER field
    # failures, never higher.
    assert by_status[Status.CONFIRMED] <= 741
    assert by_status[Status.PENDING] <= 217
    assert by_status[Status.CANCELED] <= 712
    # Pinned to the reference file (post-maintenance registry): any change
    # to these numbers means reader/normalization/contract behavior moved.
    assert len(report.trips) == 1602
    assert len(report.non_operable) == 68
    demand = [t for t in report.trips
              if t.assignment is None and t.status is not Status.CANCELED]
    assert len(demand) == 190
