import pytest

from buschain import normalize


@pytest.mark.parametrize("raw,expected", [
    ("CPH trf", "CPH"),
    ("BGO TRF", "BGO"),
    ("OSL(trf)", "OSL"),
    ("OSL (trf)", "OSL"),
    ("STO(trf)", "STO"),
    ("CPH  trf", "CPH"),
    ("Stavanger trf", "Stavanger"),
])
def test_strip_trf_variants(raw, expected):
    assert normalize.strip_trf(raw) == expected


@pytest.mark.parametrize("raw", [
    "OSL trf?",                      # trailing junk: not a pure trf suffix
    "CPH trf at 04:00",              # free text — forbidden to interpret
    "E.R to OSL",
    "ferry Turku-STO",
])
def test_strip_trf_leaves_free_text_alone(raw):
    assert normalize.strip_trf(raw) == raw


def test_casefold_key_collapses_and_folds():
    assert normalize.casefold_key("  Ollex   2 ") == "ollex 2"
    assert normalize.casefold_key("RunWay") == normalize.casefold_key("Runway")
