"""The rule-8 exact solver: beats the greedy where greedy is provably myopic,
never returns less than it, and every link survives the engine's own check."""
import dataclasses

import pytest

from buschain.engine import graph, plan, solve
from buschain.model import Assignment
from tests.helpers import mk_trip

# solve imports pulp lazily, so importing it above is always safe; the tests
# themselves need a real solver.
pytest.importorskip("pulp", reason="solver extra not installed")


def units_of(*trips):
    out = {}
    for t in trips:
        out.setdefault((t.assignment.company, t.assignment.unit), []).append(t)
    return out


def test_solver_escapes_the_greedy_trap():
    """The classic myopia case: the shortest-gap link leads to a dead end,
    the longer one opens a chain. Greedy takes the dead end (its ranking is
    gap-ascending by design); the solver must not."""
    tail = mk_trip("OSL", "CPH", 0, 2, company="ollex", unit=1)
    dead_end = mk_trip("CPH", "HEL", 3, 4)     # gap 1 — greedy's first pick
    opener = mk_trip("CPH", "CPH", 4, 5)       # gap 2 — but it comes back
    follow = mk_trip("CPH", "STO", 7, 8)       # chains only after `opener`
    units = units_of(tail)
    demand = [dead_end, opener, follow]

    greedy = plan.build_plan([tail], demand, {k: list(v) for k, v in units.items()},
                             reposition=False)
    solved = solve.build_plan(demand, units, reposition=False)

    assert len(greedy.covered) == 1            # took the dead end
    assert solved.covered == 2                 # opener + follow
    assert solved.plan.covered == {opener.id, follow.id}
    assert solved.refused == 0


def test_solver_result_is_deterministic():
    tail = mk_trip("OSL", "CPH", 0, 2, company="ollex", unit=1)
    demand = [mk_trip("CPH", "STO", 3 + i, 4 + i) for i in range(4)]
    a = solve.build_plan(demand, units_of(tail))
    b = solve.build_plan(demand, units_of(tail))
    assert a.plan == b.plan


def test_every_link_is_engine_feasible():
    """Certification is the contract: replay each returned link through
    graph.feasible_edges independently and none may be refused."""
    t1 = mk_trip("OSL", "CPH", 0, 2, company="ollex", unit=1)
    t2 = mk_trip("STO", "OSL", 0, 3, company="ollex", unit=2)
    demand = [
        mk_trip("CPH", "STO", 4, 6),
        mk_trip("STO", "CPH", 8, 9),
        mk_trip("OSL", "BGO", 5, 7),
        mk_trip("HEL", "HEL", 6, 8),           # reachable only by empty run
    ]
    units = units_of(t1, t2)
    solved = solve.build_plan(demand, units)
    assert solved.refused == 0

    by_id = {d.id: d for d in demand}
    live = {k: list(v) for k, v in units.items()}
    supply_by_id = {t.id: t for t in (t1, t2)}
    for chain in solved.plan.chains:
        for link in chain.links:
            s = supply_by_id.get(link.supply) or next(
                t for t in live[(chain.company, chain.unit)]
                if t.id == link.supply)
            d = by_id[link.demand]
            assert graph.feasible_edges([s], [d], live, True), link
            planned = dataclasses.replace(
                d, assignment=Assignment(chain.company, chain.unit))
            live[(chain.company, chain.unit)].append(planned)
            supply_by_id[d.id] = planned


def test_respects_constraints_and_reposition_setting():
    tail = mk_trip("OSL", "CPH", 0, 2, company="ollex", unit=1)
    forbidden = mk_trip("CPH", "STO", 4, 5, yn="N", constraint="ollex")
    faraway = mk_trip("STO", "OSL", 5, 6)      # needs an empty run CPH->STO
    units = units_of(tail)

    on = solve.build_plan([forbidden, faraway], units, reposition=True)
    assert on.plan.covered == {faraway.id}     # Y/N still binds the solver
    off = solve.build_plan([forbidden, faraway], units, reposition=False)
    assert off.covered == 0                    # and so does the setting


def test_empty_demand_returns_empty_plan_without_solving():
    tail = mk_trip("OSL", "CPH", 0, 2, company="ollex", unit=1)
    solved = solve.build_plan([], units_of(tail))
    assert solved.status == "Empty" and solved.covered == 0


def test_gap_insert_is_used_and_valid():
    """A demand that fits only inside a committed gap (same city both sides)
    is coverable — the piece the tail-only view would miss."""
    a = mk_trip("OSL", "CPH", 0, 1, company="ollex", unit=1)
    b = mk_trip("CPH", "STO", 8, 9, company="ollex", unit=1)
    inside = mk_trip("CPH", "CPH", 3, 5)       # gaps 2 and 3: both in window
    solved = solve.build_plan([inside], units_of(a, b))
    assert solved.plan.covered == {inside.id}
    assert solved.refused == 0


@pytest.fixture
def client(tmp_path, workbook_bytes):
    from fastapi.testclient import TestClient
    from buschain.web.app import create_app
    app = create_app(tmp_path / "solve-api.db")
    with TestClient(app) as c:
        res = c.post("/api/import", files={
            "file": ("t.xlsx", workbook_bytes, "application/octet-stream")})
        assert res.status_code == 200, res.text
        assert c.post("/api/registry/seed").status_code == 200
        # The solve is compared against the greedy plan on a fixed-season
        # workbook; the default month-ahead horizon would shrink both to zero
        # and make "+11" a comparison of nothing. See test_api.py's fixture.
        assert c.post("/api/settings", json={"horizon": "all"}).status_code == 200
        yield c


def test_optimal_preview_reports_both_sides(client):
    preview = client.get("/api/optimal-plan").json()
    links = sum(len(c["links"]) for c in preview["plan"]["chains"])
    assert preview["status"] == "Optimal"
    assert preview["refused"] == 0
    assert links > preview["greedy_covered"]   # the measured +11
    # The greedy figure must be the same one the board shows.
    state = client.get("/api/state").json()
    assert preview["greedy_covered"] == len(state["engine"]["plan"]["covered"])


def test_optimal_apply_commits_and_one_undo_reverses(client):
    before = len(client.get("/api/state").json()["engine"]["plan"]["covered"])
    res = client.post("/api/optimal-plan").json()
    assert res["ok"] and res["accepted"] > before
    state = client.get("/api/state").json()
    assert state["undo"]["label"].startswith("accept the optimal plan")
    assert client.post("/api/undo").status_code == 200
    after = client.get("/api/state").json()
    assert len(after["engine"]["plan"]["covered"]) == before


def test_never_worse_than_greedy_on_reference_data(dataset, seeded_registry):
    """The headline property, on the real workbook: solver coverage >= greedy,
    with every link certified. (The measured gap is +11; asserting >= keeps
    the test stable if either side improves.)"""
    from buschain import contract, engine
    report = contract.validate(dataset, seeded_registry)
    solved, greedy_covered = engine.optimal_plan(report)
    assert solved.refused == 0
    assert solved.covered >= greedy_covered
    assert solved.covered >= 124               # the measured optimum today
