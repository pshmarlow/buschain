"""rules.js boundary tests, mirroring test_links.py's window cases.

The JS mirror of the rule-1 window arithmetic writes the board's diagnosis
prose; a wrong boundary there misleads on every hover, and until this file it
was the one copy of the rules with no tests. Runs the real rules.js in node's
vm against the same window_rules payload the server serves (/state "rules"),
so the fixture cannot drift from what the browser actually receives.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from buschain.engine import graph

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

RULES_JS = Path(__file__).resolve().parents[2] / "buschain" / "web" / "static" / "rules.js"

# The harness evaluates rules.js exactly as a browser would (a classic script)
# and appends `;Rules` so the top-level const — which never becomes a property
# of the vm context — is returned as the script's completion value.
_HARNESS = """
const vm = require("node:vm");
const fs = require("node:fs");
const src = fs.readFileSync(process.argv[2], "utf8");
const Rules = vm.runInNewContext(src + ";Rules", {});
const rules = JSON.parse(process.argv[3]);
const out = {};

out.freeDays = [0, 1, 4, 5].map(Rules.freeDays);
out.minGap = [
  Rules.minGapDays(rules, "CPH", "CPH"),        // same city
  Rules.minGapDays(rules, "CPH", "STO"),        // near pair
  Rules.minGapDays(rules, "STO", "CPH"),        // ... in both directions
  Rules.minGapDays(rules, "CPH", "Hamburg"),    // any other pair
];
out.driving = [
  Rules.drivingDays(rules, "CPH", "CPH"),
  Rules.drivingDays(rules, "CPH", "OSL"),
  Rules.drivingDays(rules, "CPH", "Hamburg"),
];
out.daysBetween = Rules.daysBetween("2026-07-05", "2026-07-09");

const tail = { id: "tail", start: "2026-07-01", end: "2026-07-05", arr: "CPH" };
const mk = (start, dep) => ({ id: dep + "@" + start, start, end: start, dep, arr: "STO" });
const uncovered = [
  mk("2026-07-06", "CPH"),      // gap 1, same city: window floor
  mk("2026-07-09", "CPH"),      // gap 4: last same-city day
  mk("2026-07-10", "CPH"),      // gap 5: one past the ceiling
  mk("2026-07-06", "STO"),      // gap 1, near-pair floor 2: one early
  mk("2026-07-07", "STO"),      // gap 2: near floor
  mk("2026-07-10", "STO"),      // gap 5: last near day
  mk("2026-07-11", "STO"),      // gap 6: one late
  mk("2026-07-07", "Hamburg"),  // gap 2, far floor 3: one early
  mk("2026-07-05", "CPH"),      // starts ON the tail's end day: not aimable
];
const res = Rules.tailOptions(rules, [tail], uncovered);
out.tailId = res.tail.id;
out.measured = res.measured.map((m) => [m.d.id, m.gap, m.empty, m.early, m.late]);

out.constraint = [
  Rules.constraintBlocks({ yn: "Y", constraint_company: "kautra" }, "kautra"),
  Rules.constraintBlocks({ yn: "Y", constraint_company: "kautra" }, "ollex"),
  Rules.constraintBlocks({ yn: "N", constraint_company: "kautra" }, "kautra"),
  Rules.constraintBlocks({ yn: "N", constraint_company: "kautra" }, "ollex"),
  Rules.constraintBlocks({ yn: null, constraint_company: null }, "ollex"),
];
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def js(tmp_path_factory):
    harness = tmp_path_factory.mktemp("rulesjs") / "harness.js"
    harness.write_text(_HARNESS)
    proc = subprocess.run(
        [NODE, str(harness), str(RULES_JS), json.dumps(graph.window_rules())],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_free_days_is_gap_minus_one(js):
    assert js["freeDays"] == [-1, 0, 3, 4]


def test_gap_floor_follows_the_city_pair(js):
    # Same city 1; near pair 2 (unordered — both directions); far 3.
    # The same table graph.min_gap_days applies (test_links pins that side).
    assert js["minGap"] == [1, 2, 2, 3]


def test_driving_days_is_floor_minus_one(js):
    assert js["driving"] == [0, 1, 2]


def test_days_between_is_exact_day_arithmetic(js):
    assert js["daysBetween"] == 4


def test_tail_options_measures_each_trip_against_its_own_window(js):
    assert js["tailId"] == "tail"
    assert js["measured"] == [
        # [id, gap, empty, early, late] — early/late are miss distances, and
        # at most one of them is ever non-zero.
        ["CPH@2026-07-06", 1, False, 0, 0],
        ["CPH@2026-07-09", 4, False, 0, 0],
        ["CPH@2026-07-10", 5, False, 0, 1],
        ["STO@2026-07-06", 1, True, 1, 0],
        ["STO@2026-07-07", 2, True, 0, 0],
        ["STO@2026-07-10", 5, True, 0, 0],
        ["STO@2026-07-11", 6, True, 0, 1],
        ["Hamburg@2026-07-07", 2, True, 1, 0],
        # The trip starting on the tail's own end day is absent: a chain only
        # extends from strictly after its tail.
    ]


def test_constraint_blocks_matches_rule_3(js):
    # Y+X -> only X may serve; N+X -> X never; blank -> anyone.
    assert js["constraint"] == [False, True, True, False, False]
