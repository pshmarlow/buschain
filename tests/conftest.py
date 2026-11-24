from __future__ import annotations

from pathlib import Path

import pytest

from buschain.codecs import seed, xlsx_reader
from buschain.model import Registry

# Every workbook the suite reads lives in excels/, and this is the one place
# that says so. They sat in the repo root until 2026-11-24, with the second
# path spelled out inside the one test that used it — so moving the files
# broke the suite in two places that did not know about each other.
EXCELS = Path(__file__).resolve().parent.parent / "excels"
WORKBOOK = EXCELS / "LDC_PLANNER__test.xlsx"
WINTER_WORKBOOK = EXCELS / "CAPITALS WINTER.xlsx"


@pytest.fixture(scope="session")
def workbook_bytes() -> bytes:
    return WORKBOOK.read_bytes()


@pytest.fixture(scope="session")
def winter_bytes() -> bytes:
    """The new-season workbook: the right header and nothing under it.

    It differs from the reference workbook in the two ways a new season
    always does. Its trip tab is renamed ('CAPITALS'), and it holds NO trips —
    2,987 rows that are formatted but empty. That second fact is what makes it
    a fixture in its own right: every "copy what the data rows do" rule in the
    writer has no data row to copy from here.
    """
    return WINTER_WORKBOOK.read_bytes()


@pytest.fixture(scope="session")
def dataset(workbook_bytes):
    return xlsx_reader.read(workbook_bytes)


@pytest.fixture(scope="session")
def seed_result(workbook_bytes):
    return seed.read_seed(workbook_bytes)


@pytest.fixture(scope="session")
def workbook_with_lists(workbook_bytes, seed_result, tmp_path_factory) -> bytes:
    """The reference workbook after one export: the same file, now carrying the
    Nationality/OP pick lists in Information!O and !P.

    The committed fixture predates those columns, so its very first export
    legitimately grows two of them. Every 'nothing changed -> byte-identical'
    claim is about the steady state that follows, and this file is that state.
    """
    from buschain.codecs import xlsx_writer
    from buschain.store import db
    from buschain.store.repo import Repo
    repo = Repo(db.connect(tmp_path_factory.mktemp("lists") / "bootstrap.db"))
    repo.seed_from(seed_result)
    repo.import_dataset(xlsx_reader.read(workbook_bytes), "LDC_PLANNER__test.xlsx")
    return xlsx_writer.export(repo.build_export_plan())


@pytest.fixture(scope="session")
def seed_result_with_lists(workbook_with_lists):
    return seed.read_seed(workbook_with_lists)


@pytest.fixture(scope="session")
def seeded_registry(seed_result) -> Registry:
    """Registry exactly as seeded — used to test seed behavior itself."""
    return seed_result.to_registry()


@pytest.fixture(scope="session")
def registry(seeded_registry) -> Registry:
    """What the engine sees. Identical to the seed now that the Information
    sheet carries a 'Fleets' column: this fixture used to hand-patch in
    Nordibuzz 1-2 and Vilneda 1, because the old free-text charter block
    could not match them and 31 rows fell out of the operable set as a
    result. Nothing to patch is the point.
    """
    return seeded_registry
