from datetime import datetime

from buschain.codecs.types import COMPANY_NAME, START_DATE, STATUS


def test_row_count_matches_profile(dataset):
    # 1670 rows with content in the contract columns — profiled from
    # the reference file. Rows past these carry only date-grid residue.
    assert len(dataset.rows) == 1670


def test_sheet_rows_are_stable_ids(dataset):
    assert dataset.rows[0].sheet_row == 2
    assert all(r.sheet_row is not None for r in dataset.rows)


def test_values_are_raw_and_typed(dataset):
    first = dataset.rows[0]
    assert first.values[STATUS] == "Confirmed"
    assert isinstance(first.values[START_DATE], datetime)
    assert first.values[COMPANY_NAME] == "Ollex"


def test_blob_rides_along(dataset, workbook_bytes):
    assert dataset.blob == workbook_bytes
