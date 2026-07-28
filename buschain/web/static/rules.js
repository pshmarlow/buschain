/* The rule-1 window arithmetic the board reasons with, DOM-free and state-free.

   Split out of app.js so it can be tested: graph.feasible_edges's Python twin
   is pinned by tests/unit/test_links.py, and this mirror had none — a wrong
   boundary here writes wrong prose on every diagnosis the board shows.
   tests/unit/test_rules_js.py runs this file in node against the same window
   rules the server serves (graph.window_rules) and the same boundary cases.

   Everything takes `rules` — the /state payload's window table — explicitly.
   Nothing here reads app state; app.js binds these to its live S. Loaded as a
   plain <script> before app.js: a top-level const in one classic script is in
   scope for the next, so no module machinery is needed (there is no bundler). */
const Rules = (() => {
  const DAY_MS = 86400000;

  /* Every day count on screen is FREE DAYS: the days between two trips on
     which the bus runs nothing. That is what a person counts on the board —
     the blank columns between two bars — and it was worth a rename, because
     the engine's `gap_days` is arrival-day to departure-day and is therefore
     always one larger. A bus arriving the 3rd and leaving the 7th has a gap
     of 4 and three free days (4th, 5th, 6th); labelling that "4d" beside
     three blank columns read as an off-by-one bug in the planner.
     gap_days stays the engine's metric — it ranks links (plan.py) — and
     simply never reaches a label. */
  const freeDays = (gapDays) => gapDays - 1;

  /* Served by the API rather than mirrored: min_gap_days is a claim about the
     map, and a second copy in JS is a second place for it to be wrong (see
     graph.window_rules). */
  const minGapDays = (rules, from, to) => {
    if (from === to) return rules.min_gap_days;
    return rules.near_pairs.some(([a, b]) =>
      (a === from && b === to) || (a === to && b === from))
      ? rules.near_gap_days : rules.far_gap_days;
  };

  /* Of a link's free days, the ones spent driving an empty run rather than
     parked. Zero for same-city links, which is why the board's count and
     Plan.idle_days agree everywhere except a reposition. */
  const drivingDays = (rules, from, to) => minGapDays(rules, from, to) - 1;

  /* Date.parse of an ISO date is UTC midnight, so differencing two of them is
     exact day arithmetic — no timezone drift, no DST hour to lose. */
  const daysBetween = (fromISO, toISO) =>
    Math.round((Date.parse(toISO) - Date.parse(fromISO)) / DAY_MS);

  /* A unit's tail, and every uncovered trip that tail could aim for, each
     measured against its OWN window: the floor is the city pair's, because
     the empty run has to fit inside the gap, and the ceiling rides on it so
     "max 3 rest days" means three days parked however far the bus drove. One
     of `early`/`late` is always zero — a candidate misses on one side or
     neither.

     Only trips starting after the tail, because a chain only ever extends
     from the tail. Shared by the two surfaces that report on it — the row
     label's diagnosis and the next-trip list — so the explanation and the
     offer cannot disagree about what this bus can reach. */
  const tailOptions = (rules, unitTrips, uncovered) => {
    const tail = unitTrips.reduce((a, b) => (b.end > a.end ? b : a));
    const measured = uncovered.filter((d) => d.start > tail.end).map((d) => {
      const gap = daysBetween(tail.end, d.start);
      const lo = minGapDays(rules, tail.arr, d.dep);
      return { d, gap, empty: d.dep !== tail.arr,
               early: Math.max(lo - gap, 0),
               late: Math.max(gap - (lo + rules.max_rest_days), 0) };
    });
    return { tail, measured };
  };

  /* Rule 3 exactly as the engine applies it (graph._constraint_ok), read off
     the demand trip's own columns. The board needs it to say which rule
     refused a trip whose dates fit perfectly. */
  const constraintBlocks = (d, company) =>
    (d.yn === "Y" && d.constraint_company !== company)
    || (d.yn === "N" && d.constraint_company === company);

  return { freeDays, minGapDays, drivingDays, daysBetween, tailOptions,
           constraintBlocks };
})();
