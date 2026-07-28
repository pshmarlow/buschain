"""Rule 3: Y/N company-level constraints in edge generation and fallback."""
from buschain import engine
from tests.helpers import mk_registry, mk_trip, report_of


REG = mk_registry(ollex=2, kautra=2)


def test_y_allows_only_that_company():
    ollex = mk_trip("STO", "CPH", 0, 3, company="ollex", unit=1)
    kautra = mk_trip("STO", "CPH", 0, 3, company="kautra", unit=1)
    d = mk_trip("CPH", "OSL", 5, 7, yn="Y", constraint="ollex")
    result = engine.evaluate(report_of(ollex, kautra, d), REG)
    suggestions = result.suggestions_by_trip[d.id]
    assert {s.company for s in suggestions} == {"ollex"}


def test_n_never_that_company():
    ollex = mk_trip("STO", "CPH", 0, 3, company="ollex", unit=1)
    kautra = mk_trip("STO", "CPH", 0, 3, company="kautra", unit=1)
    d = mk_trip("CPH", "OSL", 5, 7, yn="N", constraint="ollex")
    result = engine.evaluate(report_of(ollex, kautra, d), REG)
    suggestions = result.suggestions_by_trip[d.id]
    assert suggestions and all(s.company != "ollex" for s in suggestions)


def test_y_with_no_feasible_chain_gets_adhoc_fallback():
    # Ollex's only unit arrives in the wrong city, so the only chain on offer
    # is a repositioning one. The ad hoc fallback must still be there — another
    # bus from Ollex's yard is the alternative to driving this one BGO -> CPH,
    # and rule 3 exists to put that choice in front of a person.
    ollex = mk_trip("STO", "BGO", 0, 3, company="ollex", unit=1)
    # BGO -> CPH is not on the one-clear-day list, so the empty run needs a
    # gap of 3; at 2 there would be no chain to offer at all.
    d = mk_trip("CPH", "OSL", 6, 8, yn="Y", constraint="ollex")
    result = engine.evaluate(report_of(ollex, d), REG)
    suggestions = result.suggestions_by_trip[d.id]
    chain, fallback = suggestions
    assert chain.kind == "chain" and chain.reposition
    assert (chain.from_city, chain.to_city) == ("BGO", "CPH")
    assert fallback.kind == "adhoc_fallback"
    assert fallback.company == "ollex" and fallback.unit is None


def test_unconstrained_demand_with_no_edges_has_no_fallback():
    d = mk_trip("CPH", "OSL", 5, 7)
    result = engine.evaluate(report_of(d), REG)
    assert d.id not in result.suggestions_by_trip
