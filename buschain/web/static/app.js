/* buschain UI — one state object fetched whole after every mutation.
   The server is the single source of truth; the client only renders. */
let S = null;

const $ = (sel) => document.querySelector(sel);
const el = (tag, attrs = {}, ...children) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    // Live properties where the name is one: setAttribute("checked", …) and
    // setAttribute("value", …) set only the DEFAULT state, which is how four
    // call sites grew their own Object.assign wrappers around el().
    else if (k in node) node[k] = v;
    else node.setAttribute(k, v);
  }
  node.append(...children);
  return node;
};

function toast(msg, isError = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = isError ? "error" : "";
  clearTimeout(t._timer);
  // Errors stay until the next toast replaces them: an auto-hidden error
  // leaves a control that "did nothing" and no trace of why.
  if (!isError) t._timer = setTimeout(() => t.classList.add("hidden"), 4000);
}

async function api(path, options = {}) {
  const res = await fetch("/api" + path, options);
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const d = (await res.json()).detail;
      // A validation 422 carries detail as an array of objects; rendering
      // that raw would toast "[object Object]".
      if (d) detail = typeof d === "string" ? d : JSON.stringify(d);
    } catch {}
    throw new Error(detail);
  }
  return res.json();
}

async function post(path, body) {
  return api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

/* Monotonic guard: with two refreshes in flight, the older response must not
   land on top of the newer one — the plan toggles (and everything else) render
   server state, and a stale S would show the setting the user just left. */
let REFRESH_SEQ = 0;

/* The pending refresh, so a preview started while one is in flight can wait
   for it to settle rather than being nulled by it moments after rendering. */
let REFRESH_PROMISE = null;

async function refresh() {
  REFRESH_PROMISE = _refreshNow();
  return REFRESH_PROMISE;
}

async function _refreshNow() {
  const seq = ++REFRESH_SEQ;
  const state = await api("/state");
  if (seq !== REFRESH_SEQ) return;    // a newer refresh is in flight; it wins
  S = state;
  // The board resolves ids thousands of times per render (every bar, every
  // link, every label), so the lookup has to be a hash, not a scan.
  S._byId = new Map(S.trips.map((t) => [t.id, t]));
  S._locked = new Set(S.locked || []);
  // Uncovered trips the planning window left out, each with the end of it
  // that did ("started" | "horizon"). The server's verdict, as with locks:
  // re-deriving it here would mean a second copy of the demand rule and of
  // both cutoffs, in a browser whose clock is not the one the plan used.
  S._scope = new Map(Object.entries((S.engine && S.engine.out_of_scope) || {}));
  // Ticks on rows that left their table are dropped, not carried: a repaired
  // row moves from non-operable to operable, and its old tick surviving there
  // would put it in a delete batch nobody can see anymore. (SELECTED is
  // declared below, but refresh only ever runs after the whole script has.)
  const live = {
    trips: new Set(S.trips.map((t) => t.id)),
    nonop: new Set(S.non_operable.map((r) => r.id)),
  };
  for (const key of ["trips", "nonop"]) {
    for (const id of SELECTED[key]) {
      if (!live[key].has(id)) SELECTED[key].delete(id);
    }
  }
  // Any change at all invalidates an open re-chain, optimize or reclaim
  // preview: all were computed against the state that just moved. Dropping
  // them here rather than trying to patch them keeps the panels from ever
  // showing a plan the server would no longer produce.
  RECHAIN = null;
  OPTIMAL = null;
  RECLAIM = null;
  renderAll();
}

/* One shape for every plain mutation: POST, toast, refresh — previously seven
   hand-rolled copies of this block and twenty-odd catch-toasts. Also the
   double-click guard: while one mutation is in flight, further calls are
   dropped, so "Accept chain" clicked twice cannot fire two POSTs.
   okMsg: string, or a function of the response; omitted = no toast.
   opts.keepSel: leave the board selection alone (locks, settings). */
let MUTATING = false;

async function mutate(path, body, okMsg, opts = {}) {
  if (MUTATING) return null;
  MUTATING = true;
  try {
    const res = await post(path, body);
    if (okMsg) toast(typeof okMsg === "function" ? okMsg(res) : okMsg);
    if (!opts.keepSel) SEL = null;
    await refresh();
    return res;
  } catch (err) {
    toast(err.message, true);
    return null;
  } finally {
    MUTATING = false;
  }
}

/* ---------- undo ---------- */

/* One history for the whole app, rendered into every toolbar that has a slot.
   Per-tab stacks were the obvious reading of "an undo button per window", but
   the actions are not independent: editing a date in Data can invalidate an
   assignment accepted in Suggestions, so undoing that assignment afterwards —
   out of order — would reconstruct a state that never existed. The button
   names the action it will reverse, so which screen it came from is visible
   rather than assumed. */
async function doUndo() {
  // SEL cleared by mutate: the selection may have been the undone accept.
  await mutate("/undo", {}, (res) => `Undone: ${res.label}`);
}

function renderUndo() {
  const u = S.undo;
  for (const btn of document.querySelectorAll(".undo-btn")) {
    btn.disabled = !u;
    btn.textContent = u ? `↶ Undo: ${u.label}` : "↶ Nothing to undo";
    btn.title = u
      ? `Reverses the last action taken anywhere in the app. ${u.depth} step(s) available.`
      : "Undo covers edits, deletions, assignments and unassignments. "
        + "Importing starts a new history.";
  }
}

document.addEventListener("click", (e) => {
  if (e.target.closest(".undo-btn")) doUndo();
});

/* ---------- lookups ---------- */

const tripById = (id) => S._byId.get(id);
const companyName = (key) => {
  const c = S.registry.companies.find((c) => c.key === key);
  return c ? c.name : key;
};
const unitLabel = (key, unit) => unit == null ? companyName(key) : `${companyName(key)} ${unit}`;
/* Both dates, everywhere a trip is named. A chain row reads as a sequence of
   spans — "runs to the 12th, next one departs the 14th" — and with only the
   start date on screen the gap figure beside it was the sole evidence the
   trips did not simply follow one another. */
const tripLabel = (id) => {
  const t = tripById(id);
  if (!t) return id;
  return `#${id.split(":")[1]} ${t.dep}→${t.arr} ${t.start} → ${t.end}`;
};
const statusBadge = (s) => el("span", { class: "badge " + s.toLowerCase() }, s);

/* ---------- rule 1's window, and how the UI counts it ---------- */

/* The arithmetic itself lives in rules.js (loaded before this file), where
   tests/unit/test_rules_js.py can pin its boundaries the way test_links.py
   pins the Python twin's. These bindings close over the live S so call sites
   stay as they were. Safe to read S unguarded — every caller runs during a
   render, and a render only happens after /state has landed. */
const freeDays = Rules.freeDays;
const minGapDays = (from, to) => Rules.minGapDays(S.rules, from, to);
const drivingDays = (from, to) => Rules.drivingDays(S.rules, from, to);
const daysBetween = Rules.daysBetween;
const tailOptions = (unitTrips, uncovered) =>
  Rules.tailOptions(S.rules, unitTrips, uncovered);
const constraintBlocks = Rules.constraintBlocks;

/* The same-city window, in the terms each caller explains it in. */
const minGap = () => S.rules.min_gap_days;
const maxGap = () => minGap() + S.rules.max_rest_days;
const maxRest = () => S.rules.max_rest_days;

/* Once per state load: S is replaced wholesale by refresh, so caching against
   the object itself can never go stale. Each of these used to be rebuilt
   three or four times per render pass (the board, the detail panel and the
   plan summary all ask). */
const perState = (build) => {
  let src = null, val = null;
  // Build BEFORE marking current: the other order would let a throwing
  // builder serve the previous state's value for the rest of this state.
  return () => (src === S ? val : ((val = build()), (src = S), val));
};

/* ---------- Data tab ---------- */

/* Columns hidden in the operable table, by sheet title. Persisted so the view
   survives a reload. The default hides the free-text columns, so the table
   looks as it did before the picker existed; everything is one click away. */
const HIDDEN_KEY = "buschain.hidden-columns";
const DEFAULT_HIDDEN = ["P Notes", "Nationality", "OP", "Quoted rate", "C Notes"];

/* Guarded read of a stored string set — localStorage throws in some
   private-browsing modes, and a lost preference must not take the UI down
   with it. Shared with the paste picker's unticked-columns set. */
function loadSet(storageKey, fallback = []) {
  try {
    const stored = localStorage.getItem(storageKey);
    if (stored) return new Set(JSON.parse(stored));
  } catch (err) { /* fall through to the fallback */ }
  return new Set(fallback);
}

let HIDDEN = loadSet(HIDDEN_KEY, DEFAULT_HIDDEN);

function setColumnHidden(title, hidden) {
  if (hidden) {
    HIDDEN.add(title);
    // Its filter goes with it: viewRows walks only visible columns, so the
    // filter would silently stop applying yet survive — invisible in the
    // filter row, still counted by clearEmptiedView, resurrected on unhide.
    // The sort goes for the same reason, and it read worse: viewRows finds no
    // column to sort by and quietly leaves the rows in source order, so the
    // table jumped back to unsorted with the sort mark gone and no way to see
    // why — and unhiding re-applied a sort nobody asked for again.
    for (const key of ["trips", "nonop"]) {
      VIEWS[key].filters.delete(title);
      if (VIEWS[key].sort === title) { VIEWS[key].sort = null; VIEWS[key].dir = 1; }
    }
  } else {
    HIDDEN.delete(title);
  }
  try { localStorage.setItem(HIDDEN_KEY, JSON.stringify([...HIDDEN])); } catch (err) {}
  renderTrips();
}

/* How a column's value is shown when not being edited. Columns the contract
   parsed add their parsed reading (badges, canonical codes) beside the raw
   text; the rest are the raw cell. A missing key means the cell is empty —
   the row payloads omit blanks, the column list is the source of truth for
   which cells exist. */
const DATE_COLUMNS = new Set(["Start date", "End date"]);
const STATUS_OPTIONS = ["Confirmed", "Pending", "Canceled"];
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

const CELL_DISPLAY = {
  "Status": (raw, t) => (t ? statusBadge(t.status) : raw),
  // The sheet's own text plus how the contract read it, so a preference that
  // did not parse is visible rather than silently inert.
  "Bus Preference": (raw, t) => el("span", {}, raw,
    t && t.yn ? el("span", { class: "badge " + (t.yn === "Y" ? "system" : "warn") },
      t.yn === "Y" ? " must use" : " must avoid") : ""),
  "Company name": (raw, t) => (t && t.assignment
    ? el("span", {}, unitLabel(t.assignment.company, t.assignment.unit),
        t.assignment.unit == null ? el("span", { class: "badge adhoc" }, " ad hoc") : "")
    : raw),
};

function cellDisplay(title, raw, trip) {
  const render = CELL_DISPLAY[title];
  return render ? render(raw, trip) : raw;
}

/* An editable cell. Editing is delegated from the table (one listener, not
   one per cell across ~25k cells), so the td only carries the data the
   handler needs. */
function cell(rowId, title, raw, trip, edited) {
  const td = el("td", { class: "cell" + (edited ? " edited" : "") });
  td.dataset.row = rowId;
  td.dataset.col = title;
  td.dataset.raw = raw;
  td.append(cellDisplay(title, raw, trip));
  return td;
}

const option = (value, label, selected) =>
  el("option", { value, selected }, label);

/* Bus Preference is a closed choice: a registry company (must use) or the
   same company negated (must avoid). Free text here only ever produced
   non-operable rows, since the contract has to resolve it to a company. */
function preferenceInput(raw) {
  const names = (S.registry.companies || []).map((c) => c.name)
    .sort((a, b) => a.localeCompare(b));
  const known = new Set(["", ...names, ...names.map((n) => `NO ${n}`)]);
  return el("select", {},
    option("", "— no preference —", raw === ""),
    // An imported value the registry cannot offer (free text, or a company
    // since deleted) is kept as a choice. Dropping it would let the select
    // silently rewrite the cell to the first option on the next blur.
    ...(known.has(raw) ? [] : [option(raw, `${raw} (not in registry)`, true)]),
    el("optgroup", { label: "Must use" },
      ...names.map((n) => option(n, n, n === raw))),
    el("optgroup", { label: "Must avoid" },
      ...names.map((n) => option(`NO ${n}`, `NO ${n}`, `NO ${n}` === raw))),
  );
}

/* The units the engine itself proposes for this trip, as picker values. These
   passed the whole rule set — gap window, city reachability, constraints — so
   they are the only tier that can honestly be called *legal*. A rule-3 ad hoc
   fallback carries unit null and maps to the company-only entry. */
function suggestedValues(trip) {
  const out = new Set();
  for (const s of S.engine.suggestions_by_trip[trip.id] || []) {
    out.add(s.unit == null ? companyName(s.company)
                           : `${companyName(s.company)} ${s.unit}`);
  }
  return out;
}

/* Numbered units already carrying a trip that overlaps this one's dates, mapped
   to the clashing trip so the picker can name it.

   Deliberately *only* an overlap test, not a second opinion on legality: the
   engine owns that (suggestedValues above), and a JS half-copy of the gap and
   city rules would be a second place for them to be wrong. What this answers is
   the narrower question a planner asks when the engine has proposed nothing —
   "is that bus even doing anything that week?" — and it answers it from data the
   page already holds. Canceled trips occupy no bus, matching the engine's
   timelines; ad hoc assignments name no specific bus, so they cannot make one
   busy. ISO dates compare correctly as plain strings. */
function busyUnits(trip) {
  const busy = new Map();
  for (const t of S.trips) {
    if (t.id === trip.id) continue;             // its own assignment is not a clash
    const a = t.assignment;
    if (!a || a.unit == null || t.status === "Canceled") continue;
    if (t.end < trip.start || t.start > trip.end) continue;
    busy.set(`${companyName(a.company)} ${a.unit}`, tripLabel(t.id));
  }
  return busy;
}

/* A listbox built from divs, used by the Company name cell and nothing else.

   The reason is narrow, and worth recording so this does not get "simplified"
   back into a <select>: a native select's popup is drawn by the OS on macOS,
   which ignores background-color on <option>. The availability tiers below were
   invisible there — the colour is the whole point of them, so the picker had to
   stop being a native control. Every other cell editor in this table is still a
   plain select, which remains the right default; this one buys its complexity
   with a rendering guarantee.

   Typed text is only ever a FILTER — it never becomes the value. That preserves
   the property the native version had for free and that this cell depends on:
   the cell commits on dismissal, so a widget that could commit whatever was
   typed would rewrite cells to values nobody picked (see 'keep as is' below).

   Contract with beginEdit: a node carrying .value, dispatching `change` when it
   settles — on a pick, and on a dismissal that leaves .value alone, which the
   caller reads as "restore". .mount() is called once it is in the document,
   because the menu cannot be positioned before it has a box.

   groups: [{ label, items: [{ value, label, tier, note }] }] — a group with no
   items is skipped, and an empty label renders no heading. */
function comboInput(raw, groups) {
  const search = el("input", { class: "combo-search", type: "text",
    placeholder: "Type to filter…", "aria-label": "Filter the list" });
  search.value = raw;
  const menu = el("div", { class: "combo-menu", role: "listbox" });
  const root = el("div", { class: "combo" }, search, menu);
  root.value = raw;

  const settle = (value) => {
    root.value = value;
    root.dispatchEvent(new Event("change"));
  };

  const entries = [];                  // { node, head, value }
  const heads = [];
  for (const g of groups) {
    if (!g.items.length) continue;
    const head = g.label ? el("div", { class: "combo-group" }, g.label) : null;
    if (head) { heads.push(head); menu.append(head); }
    for (const it of g.items) {
      const node = el("div", { class: "combo-opt", role: "option" },
                      it.label ?? it.value);
      if (it.tier) node.dataset.tier = it.tier;
      // The clashing trip names both dates and both cities — too wide for the
      // row, so it stays a tooltip.
      if (it.note) node.title = it.note;
      if (it.value === raw) node.classList.add("current");
      node.addEventListener("click", () => settle(it.value));
      menu.append(node);
      entries.push({ node, head, value: it.value });
    }
  }

  let visible = [];                    // entries passing the filter, in order
  let active = -1;

  const setActive = (i) => {
    if (active >= 0 && visible[active]) visible[active].node.classList.remove("active");
    active = i;
    if (i >= 0 && visible[i]) {
      visible[i].node.classList.add("active");
      visible[i].node.scrollIntoView({ block: "nearest" });
    }
  };

  // The box opens showing the current value as text, and that text is not a
  // filter — only what the user types after opening is. Otherwise opening a
  // cell that already reads 'Vilneda 1' would show a list of one.
  let typed = false;
  const applyFilter = () => {
    const q = typed ? search.value.trim().toLowerCase() : "";
    const live = new Set();
    visible = [];
    for (const e of entries) {
      const show = !q || e.value.toLowerCase().includes(q);
      e.node.classList.toggle("hidden", !show);
      if (show) { visible.push(e); if (e.head) live.add(e.head); }
    }
    // A heading whose every option is filtered out would label nothing.
    for (const h of heads) h.classList.toggle("hidden", !live.has(h));
    active = -1;
    // Pre-arm Enter on the best remaining match rather than requiring an arrow
    // press first — with a filter typed, the top hit is what you meant.
    setActive(visible.length ? (q ? 0 : visible.findIndex((e) => e.value === raw)) : -1);
  };

  search.addEventListener("input", () => { typed = true; applyFilter(); });
  search.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const step = e.key === "ArrowDown" ? 1 : -1;
      setActive(Math.min(Math.max(active + step, 0), visible.length - 1));
    } else if (e.key === "Enter" && active >= 0) {
      // Enter with nothing matching is deliberately not handled here: it falls
      // through to beginEdit, which restores the cell rather than committing
      // text that matches no option.
      e.preventDefault();
      settle(visible[active].value);
    }
    // Escape is left to bubble — beginEdit cancels the edit.
  });

  /* Clicking an option must not blur the search box first: the blur below
     settles the widget, which would tear it down mid-click and lose the pick. */
  menu.addEventListener("mousedown", (e) => e.preventDefault());
  // Light dismiss. .value is unchanged here, so beginEdit takes its restore path.
  search.addEventListener("blur", () => settle(root.value));

  /* Fixed rather than absolute: the table scrolls inside .table-wrap
     (overflow: auto), which would clip the menu on the lower rows — exactly the
     rows a long sheet is usually scrolled to. A fixed box is positioned against
     the viewport and ancestor overflow does not clip it; no ancestor here sets
     transform/filter, which would otherwise re-base it onto that ancestor. The
     cost is that it does not travel with the scroll, hence the listener. */
  const place = () => {
    const r = search.getBoundingClientRect();
    const h = menu.offsetHeight;
    const room = window.innerHeight - r.bottom;
    // Flip above only when below genuinely cannot hold it and above can.
    const up = room < h + 8 && r.top > h + 8;
    menu.style.left = `${r.left}px`;
    menu.style.minWidth = `${r.width}px`;
    menu.style.top = up ? `${r.top - h - 2}px` : `${r.bottom + 2}px`;
  };
  const follow = () => {
    // The table re-renders under this widget on any refresh; when it does, the
    // node is gone and the listener has nothing left to position.
    if (!root.isConnected) {
      window.removeEventListener("scroll", follow, true);
      window.removeEventListener("resize", follow);
      return;
    }
    place();
  };
  root.mount = () => {
    applyFilter();
    place();
    window.addEventListener("scroll", follow, true);
    window.addEventListener("resize", follow);
    search.focus();
    search.select();                   // so the first keystroke filters
  };
  return root;
}

/* Company name is a closed choice too, but its options are not a stored pick
   list like OP's — the contract resolves this cell through the registry
   (resolve_company), so the legal values are exactly: blank, 'Vilneda 3' for
   each numbered unit, or a bare 'Vilneda' for an ad hoc, chain-invisible
   assignment. Numbered units and company-level entries stay in separate groups,
   for the same reason as the board's directAssign picker: most companies have no
   units, so interleaving them by name buries the actual buses.

   Still deliberately unfiltered — every unit is offered, including ones that
   cannot legally take the trip. An overlap or city mismatch is caught downstream
   by the integrity check; refusing it here would leave a planner with a bus, a
   trip, and no way to say so. What the tiers below add is *ordering and colour*,
   never removal: the buses that can take this trip come first and are green, the
   rest are still one scroll away. Turning any of this into a filter would break
   the escape hatch this picker exists to be. */
/* Registry companies in display order — the base list every picker builds
   from (cell editor, manual form, the board's direct-assign). */
const sortedCompanies = () => [...(S.registry.companies || [])]
  .sort((a, b) => a.name.localeCompare(b.name));

function companyValues() {
  const companies = sortedCompanies();
  return {
    units: companies.flatMap((c) => c.units.map((u) => `${c.name} ${u}`)),
    adhoc: companies.map((c) => c.name),
  };
}

function companyNameInput(raw, trip) {
  const { units, adhoc } = companyValues();
  const known = new Set(["", ...units, ...adhoc]);

  // The non-operable table has no parsed trip to judge availability against —
  // its rows are exactly the ones the contract could not read — so it falls
  // back to the flat list.
  const suggested = trip ? suggestedValues(trip) : new Set();
  const busy = trip ? busyUnits(trip) : new Map();

  const item = (v, tier) => ({ value: v, tier,
    note: busy.has(v) ? `Already on ${busy.get(v)}` : "" });
  const group = (label, items) => ({ label, items });

  const tiered = trip
    ? [
        group("Buses — the engine suggests these",
              units.filter((v) => suggested.has(v)).map((v) => item(v, "suggested"))),
        group("Buses — free on these dates",
              units.filter((v) => !suggested.has(v) && !busy.has(v))
                   .map((v) => item(v, "free"))),
        group("Buses — busy on these dates",
              units.filter((v) => !suggested.has(v) && busy.has(v)).map((v) => item(v))),
      ]
    : [group("Buses", units.map((v) => item(v)))];

  return comboInput(raw, [
    group("", [
      { value: "", label: "— needs a bus —" },
      /* Any value not matching an option verbatim is kept as a choice, or the
         picker would offer no way back to it and dismissing would rewrite the
         cell — see preferenceInput. Labelled 'keep as is' rather than 'not in
         registry' because it is often neither: the sheet holds 'Runway' and
         'Connect bus' where the registry says 'RunWay' and 'Connect Bus', and
         the contract resolves those perfectly well. Matching loosely so they
         resolve to the canonical option instead would be worse — opening a cell
         and dismissing it would rewrite a spelling nobody asked to change. */
      ...(known.has(raw) ? [] : [{ value: raw, label: `${raw} (keep as is)` }]),
    ]),
    ...tiered,
    /* Not tiered by availability: a company-level assignment names no specific
       bus, so "free on these dates" has nothing to be true of. It is green only
       where the engine named it — the rule-3 fallback for a Y-constrained trip
       with no feasible chain. */
    group("Ad hoc — company only, no unit (never chains)",
          adhoc.map((v) => item(v, suggested.has(v) ? "suggested" : null))),
  ]);
}

/* A closed list with a blank first entry. Any current value the list cannot
   offer is kept as a choice, or the select would show option one and the next
   blur would silently rewrite the cell — the same trap the date picker has. */
function listInput(values, raw, blankLabel) {
  const known = new Set(values);
  return el("select", {},
    option("", blankLabel, raw === ""),
    ...(raw === "" || known.has(raw)
      ? [] : [option(raw, `${raw} (not in list)`, true)]),
    ...values.map((v) => option(v, v, v === raw)));
}

function cellInput(title, raw, trip) {
  if (title === "Bus Preference") return preferenceInput(raw);
  if (title === "Company name") return companyNameInput(raw, trip);
  if (title === "Status") {
    const known = new Set(["", ...STATUS_OPTIONS]);
    return el("select", {},
      // Same guard as every other closed editor: an unmatched current value
      // ('confirmed' — the contract casefolds, this list does not) is kept
      // as a choice, or blur would silently blank the cell.
      ...(known.has(raw) ? [] : [option(raw, `${raw} (keep as is)`, true)]),
      ...["", ...STATUS_OPTIONS].map((s) => option(s, s, s === raw)));
  }
  if (title === "Departure" || title === "Arrival") {
    return listInput(S.registry.cities || [], raw, "— none —");
  }
  const list = (S.registry.options || {})[title];
  if (list) return listInput(list, raw, "— none —");
  // A date picker only where the current value is one a date picker can hold.
  // Forcing type=date on junk like 'Karlshamm - CPH' would blank the cell on
  // the first click — exactly the value the user opened it to repair.
  const input = el("input", {
    type: DATE_COLUMNS.has(title) && (raw === "" || ISO_DATE.test(raw)) ? "date" : "text",
  });
  input.value = raw;
  return input;
}

function beginEdit(td) {
  if (td.querySelector("input, select")) return;
  const { row, col, raw } = td.dataset;
  // Operable rows are keyed by their trip id, so this is the row's own trip;
  // for a non-operable row there is none, and the picker degrades to a flat
  // list. Read from the dataset rather than passed down from cell(), which
  // would mean holding a trip reference on ~25k cells to serve the one being
  // edited.
  const input = cellInput(col, raw, tripById(row));
  let settled = false;
  const finish = async (save) => {
    if (settled) return;
    settled = true;
    if (save && input.value !== raw) {
      try {
        await post(`/trips/${row}/cell`, { column: col, value: input.value });
        await refresh();                  // re-validates; row may change table
        return;
      } catch (err) { toast(err.message, true); }
    }
    // Restore just this cell. This path runs on every Escape and every blur
    // that changed nothing — rebuilding both tables (~25k cells) for that
    // also threw away the scroll position mid-edit.
    td.replaceChildren(cellDisplay(col, raw, tripById(row)));
  };
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); finish(true); }
    else if (e.key === "Escape") { e.preventDefault(); finish(false); }
  });
  // A dropdown commits on pick; the blur that follows is a no-op via `settled`.
  // The combo (see comboInput) is the same shape — it dispatches change both
  // when a value is picked and when it is dismissed unchanged, which lands on
  // the restore path below. Its own blur never reaches here: focus sits on an
  // inner field, and blur does not bubble.
  if (input.tagName === "SELECT" || input.mount) {
    input.addEventListener("change", () => finish(true));
  }
  input.addEventListener("blur", () => finish(true));
  td.replaceChildren(input);
  // A widget that positions itself needs a box first, so it is mounted rather
  // than merely focused.
  if (input.mount) input.mount();
  else {
    input.focus();
    if (input.select) input.select();
  }
}

/* Deleting a row is the one destructive thing the Data tab can do, so it asks
   first — the table is dense and the button sits a few pixels from the cells
   you click to edit. It stays a question even though undo would cover it:
   noticing you deleted the wrong row is the hard part, not reversing it. */
async function deleteRow(rowId, key) {
  if (!confirm(`Delete ${rowName(rowId)}?\n\n`
      + "It leaves the working set — no validation, no chaining, no plan — and "
      + "is dropped from the exported sheet. Undo puts it back.")) return;
  try {
    await api(`/trips/${rowId}`, { method: "DELETE" });
    toast(`Deleted ${rowName(rowId)}`);
    if (SEL === rowId) SEL = null;
    await refresh();
    clearEmptiedView(key, rowName(rowId));
  } catch (err) { toast(err.message, true); }
}

/* A delete that empties the view it was launched from leaves its filter
   matching nothing — a table reading '0 of 1600' right after 'delete
   selected' looks like data loss, not like a filter. Cleared rather than
   left standing, but only when the delete is what emptied it (rows remain,
   view shows none), and announced — same rule as jumpToDataRow: clearing
   something the user set is a visible change, never done silently. */
function clearEmptiedView(key, deletedWhat) {
  const v = VIEWS[key];
  if (v.shown === 0 && v.total > 0 && (v.search || v.filters.size)) {
    clearFilters(key);
    toast(`Deleted ${deletedWhat} — cleared the ${TABLES[key].label} filters,`
      + " nothing matched them anymore.");
  }
}

/* Same question as the single delete, same reasoning: noticing you ticked the
   wrong rows is the hard part, not reversing it — the batch is one undo. */
async function deleteSelected(key) {
  const ids = [...SELECTED[key]];
  if (!ids.length) return;
  const what = ids.length === 1
    ? rowName(ids[0]) : `${ids.length} selected rows`;
  if (!confirm(`Delete ${what}?\n\n`
      + "They leave the working set — no validation, no chaining, no plan — and "
      + "are dropped from the exported sheet. One Undo puts them all back.")) return;
  try {
    await post("/trips/delete", { ids });
    toast(`Deleted ${what}`);
    if (ids.includes(SEL)) SEL = null;
    SELECTED[key].clear();
    await refresh();
    clearEmptiedView(key, what);
  } catch (err) { toast(err.message, true); }
}

$("#trips-del-sel").addEventListener("click", () => deleteSelected("trips"));
$("#nonop-del-sel").addEventListener("click", () => deleteSelected("nonop"));

for (const [id, key] of [["#trips-table", "trips"], ["#nonop-table", "nonop"]]) {
  $(id).addEventListener("click", (e) => {
    // Delegated like the cell editor, and for the same reason: a listener per
    // row would mean ~1700 of them, rebuilt on every filter keystroke.
    const del = e.target.closest("button.row-del");
    if (del) { deleteRow(del.dataset.row, key); return; }
    const td = e.target.closest("td.cell");
    if (td) beginEdit(td);
  });
}

// The sheet-row identity. Not a contract column, but it belongs in the picker
// alongside them — it takes width and is noise once you know the rows.
const ROW_COLUMN = "Row";
const pickerColumns = () => [ROW_COLUMN, ...(S.columns || [])];

function renderColumnToggles() {
  $("#column-toggles").replaceChildren(...pickerColumns().map((title) =>
    el("label", { class: "col-toggle" },
      el("input", {
        type: "checkbox",
        checked: !HIDDEN.has(title),
        onchange: (e) => setColumnHidden(title, !e.target.checked),
      }),
      title)));
}

/* The server numbers rows (see Repo.row_labels): a manual row takes the sheet
   row it will occupy on export, which its own key — a rowid counting every row
   of every dataset ever imported — cannot tell anyone. Just the number, which
   is what the Row column shows; prose adds the noun via rowName below. The
   fallback strips the origin for a state fetched before labels existed. */
const rowLabel = (id) => (S.labels && S.labels[id])
  || id.replace(/^(sheet|manual):/, "");
/* For sentences — "Delete row 8?" — where the bare number would read as a
   count rather than as a place. Never used in the Row column itself. */
const rowName = (id) => `row ${rowLabel(id)}`;

/* Today in the user's own timezone, as ISO. Built by hand rather than sliced
   off toISOString(), which is UTC — west of Greenwich that reports yesterday
   for most of the working day. Shared by the table's past-fade and the board's
   today line, so the two can never disagree about which day it is. */
const todayISO = (() => {
  const d = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
})();

/* Row decoration for the operable table. Leans on ISO dates comparing
   correctly as plain strings, so no Date objects are built per row. */
function decorateTripRow(tr, r, today) {
  // Also the marker for "this is a real trip row" — it keeps the empty-state
  // placeholder row out of the jump-to-today scan below.
  tr.dataset.start = r.start;
  // The board's handle back into this table (jumpToDataRow).
  tr.dataset.trip = r.id;
  // Keyed on the START date, not the end date. Keying on the end date instead
  // left long trips that began weeks ago but have not finished sitting at full
  // strength among faded neighbours, which reads as rows being randomly
  // emphasised rather than as information; keyed on the start, the fade falls
  // as one clean boundary down the Start date column whenever the table is in
  // start-date order (sorted, or unsorted on a chronological sheet — the
  // default order is the sheet's own, see Repo._trip_rows). The cost is that a
  // trip running across today fades too.
  if (r.start < today) tr.classList.add("past");
}

/* Search / sort / filter state, one set per table so the two never interfere.
   Deliberately not persisted: a filter silently restored on load hides rows,
   which reads as data loss rather than as an active view. */
const makeView = () => ({
  search: "", sort: null, dir: 1, filters: new Map(),
  shown: 0, total: 0, timer: null,
});
const VIEWS = { trips: makeView(), nonop: makeView() };

/* The two tables differ only in where a cell's text comes from, whether the
   contract parsed the row, and the extra Reasons column. */
const TABLES = {
  trips: {
    id: "#trips-table", label: "operable",
    rows: () => S.trips,
    raw: (r, c) => r.values[c] || "",
    trip: (r) => r,
    // Only this table has parsed start/end on every row; the non-operable rows
    // hold raw text that may be exactly why the contract rejected them.
    decorate: decorateTripRow,
  },
  nonop: {
    id: "#nonop-table", label: "non-operable",
    rows: () => S.non_operable,
    raw: (r, c) => r.display[c] || "",
    // The contract rejected these rows, so there is no parsed trip to render
    // badges from — the raw cell text is all there is.
    trip: () => null,
    // Reasons stays pinned: it is what the user acts on, and hiding it would
    // leave a dead row with no clue which cell to fix.
    extra: {
      title: "Reasons",
      text: (r) => r.reasons.join("; "),
      cell: (r) => el("td", { class: "reason" }, r.reasons.join("; ")),
    },
  },
};

/* Ticked rows per table, keyed by row id. On the id, not the position, so a
   tick survives sorting and filtering; pruned on every refresh so a row that
   left the table underneath it (deleted from the other tab's view, or
   repaired out of non-operable) can never be deleted by a stale tick. */
const SELECTED = { trips: new Set(), nonop: new Set() };

/* The anchor for shift-click ranges: the last row ticked by hand. An id, not
   an index — the order it anchors is resolved at click time, so a re-sort
   between the two clicks ranges over what is on screen now. */
const LAST_TICK = { trips: null, nonop: null };

/* One tick, or — shift held — the whole visible span from the last tick to
   this one, set to this checkbox's new state. The span is read from
   visibleRows in displayed order, so it means exactly the rows between the
   two clicks as the user sees them, whatever sort or filter produced that.
   An anchor that has left the view (refiltered, deleted) degrades to a
   single tick rather than guessing at a range nobody can see. */
function applyTick(key, id, on, ranged) {
  const sel = SELECTED[key];
  const ids = [id];
  if (ranged && LAST_TICK[key] !== null && LAST_TICK[key] !== id) {
    const order = visibleRows(key).map((r) => r.id);
    const a = order.indexOf(LAST_TICK[key]), b = order.indexOf(id);
    if (a !== -1 && b !== -1) {
      ids.push(...order.slice(Math.min(a, b), Math.max(a, b) + 1));
    }
  }
  for (const i of ids) { if (on) sel.add(i); else sel.delete(i); }
  LAST_TICK[key] = id;
  syncTickBoxes(key);
  updateDeleteSelected(key);
}

/* Re-tick the visible checkboxes in place, header select-all included. The
   tbody used to be rebuilt for this — ~6000 tds discarded on a shift-click
   over 400 rows, purely to set some .checked flags — and the header box was
   not updated at all, so hand-ticking every row left it contradicting them. */
function syncTickBoxes(key) {
  const table = $(TABLES[key].id);
  const sel = SELECTED[key];
  let all = true, any = false;
  for (const box of table.querySelectorAll("tbody td.row-select input")) {
    const on = sel.has(box.dataset.row);
    box.checked = on;
    all = all && on;
    any = true;
  }
  const head = table.querySelector("thead th.row-select input");
  if (head) head.checked = any && all;
}

function updateDeleteSelected(key) {
  const btn = $(`#${key}-del-sel`);
  const n = SELECTED[key].size;
  btn.classList.toggle("hidden", n === 0);
  btn.textContent = `Delete selected (${n})`;
}

/* One descriptor per rendered column, so Row, Reasons and the contract columns
   can all be sorted and filtered through the same code path. */
function tableColumns(cfg, key) {
  const cols = [];
  /* Tick first, like the delete button last: an action handle, not a value —
     nothing to sort or filter, not in the column picker. */
  cols.push({
    title: "", select: true,
    text: () => "",
    cell: (r) => {
      const box = el("input", { type: "checkbox",
        title: "Tick — shift-click ticks the whole range from your last tick",
        "aria-label": `Select ${rowName(r.id)}`,
        checked: SELECTED[key].has(r.id) });
      // syncTickBoxes re-ticks boxes in place and needs to know whose is whose.
      box.dataset.row = r.id;
      /* click, not change: by the time click handlers run the checkbox has
         already flipped, and only the MouseEvent carries shiftKey — change
         would say what happened but not how. The text-selection shift-click
         would otherwise smear across the rows is suppressed in CSS
         (.row-select user-select) rather than by cancelling mousedown here:
         mousedown-cancellation semantics around form controls are exactly
         where browsers disagree, and a browser that lets it swallow the
         click would leave the checkbox dead. */
      box.addEventListener("click", (e) =>
        applyTick(key, r.id, box.checked, e.shiftKey));
      return el("td", { class: "row-select" }, box);
    },
  });
  if (!HIDDEN.has(ROW_COLUMN)) {
    /* The label IS the number (Repo.row_labels), which is the export row for
       sheet and pasted rows alike, so sorting and displaying it are the same
       thing and compareText's numeric:true keeps 2 before 1672 without
       padding. This carried a separate `key` while the labels read "row 12" /
       "new 1672": comparing those as text put every pasted row above row 2
       ascending, on nothing but "new" < "row". */
    cols.push({ title: ROW_COLUMN, text: (r) => rowLabel(r.id),
      cell: (r) => el("td", {}, rowLabel(r.id)) });
  }
  if (cfg.extra) cols.push(cfg.extra);
  for (const c of (S.columns || []).filter((t) => !HIDDEN.has(t))) {
    cols.push({
      title: c,
      text: (r) => cfg.raw(r, c),
      cell: (r) => cell(r.id, c, cfg.raw(r, c), cfg.trip(r),
                        c in (S.overrides[r.id] || {})),
    });
  }
  /* Delete last, and outside the column picker: it is an action on the row,
     not one of the row's values, so it has nothing to sort or filter by and
     hiding it would only ever be an accident. */
  cols.push({
    title: "", action: true,
    text: () => "",
    cell: (r) => {
      const btn = el("button", { class: "btn small row-del",
        title: `Delete ${rowName(r.id)}` }, "×");
      btn.dataset.row = r.id;
      return el("td", { class: "row-actions" }, btn);
    },
  });
  return cols;
}

/* A column gets a dropdown when the data holds few enough distinct values to
   pick from, and a contains-match text box otherwise. Deriving that from the
   rows rather than a hardcoded list of "closed" columns means a dropdown never
   offers a value that matches nothing, and it keeps working if the sheet
   layout changes. */
const FILTER_CHOICE_LIMIT = 25;

function columnChoices(rows, col) {
  const seen = new Set();
  for (const r of rows) {
    const v = col.text(r);
    if (v !== "") seen.add(v);
    if (seen.size > FILTER_CHOICE_LIMIT) return null;   // too open-ended to list
  }
  return [...seen].sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));
}

/* Cells are raw sheet text. numeric:true keeps "10" after "9" without treating
   the column as a number, and ISO dates already order correctly as text.
   Blanks sort last in both directions — an empty cell is missing, not least.

   The direction is applied HERE rather than by negating the result at the call
   site, which is what the caller used to do: negating the whole comparator
   negates the blank branch too, so descending put every empty cell first —
   exactly the "missing reads as least" behaviour the line above rules out. */
function compareText(a, b, dir) {
  if (a === b) return 0;
  if (a === "") return 1;
  if (b === "") return -1;
  return dir * a.localeCompare(b, undefined, { numeric: true });
}

function viewRows(cfg, cols, view, choices) {
  let rows = cfg.rows();
  for (const col of cols) {
    const term = view.filters.get(col.title);
    if (!term) continue;
    // A Set is the multi-select column's filter (see checkboxFilter): any one
    // of the ticked values matches. Never empty — an emptied Set is deleted, so
    // "no filter" stays a single state for the guard above and clearFilters.
    if (term instanceof Set) {
      rows = rows.filter((r) => term.has(col.text(r)));
      continue;
    }
    // A value picked from a list matches exactly; typed text is a substring,
    // since you are narrowing rather than naming an exact cell.
    rows = choices.get(col.title)
      ? rows.filter((r) => col.text(r) === term)
      : rows.filter((r) => col.text(r).toLowerCase().includes(term.toLowerCase()));
  }
  const q = view.search.trim().toLowerCase();
  // Only the columns on screen: matching a hidden one would surface a row with
  // no visible reason for being there.
  if (q) rows = rows.filter((r) => cols.some((c) => c.text(r).toLowerCase().includes(q)));
  if (view.sort) {
    const col = cols.find((c) => c.title === view.sort);
    // Sorted on the same text that is filtered, searched and displayed — one
    // column, one value, so the order can never disagree with what is on
    // screen. (Row briefly needed its own sort key, back when its cell read
    // "row 12" rather than 12.)
    // Copy first — with no filters applied `rows` is still the live state array.
    if (col) rows = [...rows].sort((a, b) => compareText(col.text(a), col.text(b), view.dir));
  }
  return rows;
}

/* asc -> desc -> off. Off matters: unsorted is the source file's own row
   order, which is the order the sheet is read and discussed in. */
function toggleSort(key, title) {
  const v = VIEWS[key];
  if (v.sort !== title) { v.sort = title; v.dir = 1; }
  else if (v.dir === 1) { v.dir = -1; }
  else { v.sort = null; }
  renderTable(key);
}

function setFilter(key, title, value) {
  const v = VIEWS[key];
  if (value === "") v.filters.delete(title); else v.filters.set(title, value);
  renderRows(key);
}

/* Typing re-filters up to ~1500 rows and rebuilds the body; a short debounce
   keeps that off the keystroke path. Per-table timers, so typing in one
   table's box cannot cancel the other's pending update. */
function debounceRows(key) {
  const v = VIEWS[key];
  clearTimeout(v.timer);
  v.timer = setTimeout(() => renderRows(key), 120);
}

function headerRow(key, cols, view, rows) {
  return el("tr", {}, ...cols.map((c) => {
    if (c.select) {
      /* Select-all over the rows the view currently shows, not the whole
         table: with a filter on, "everything" means everything you can see —
         which is also what makes 'filter to Canceled, tick all, delete' the
         whole bulk workflow. Recomputed at click time, not captured: filter
         changes rebuild only the tbody (renderRows), so a set captured here
         would silently be the view as it was when the header was built. */
      const box = el("input", { type: "checkbox",
        title: "Select all rows in the current view",
        "aria-label": "Select all rows in the current view" });
      box.checked = rows.length > 0 && rows.every((r) => SELECTED[key].has(r.id));
      box.addEventListener("change", () => {
        const sel = SELECTED[key];
        for (const r of visibleRows(key)) {
          if (box.checked) sel.add(r.id); else sel.delete(r.id);
        }
        syncTickBoxes(key);        // in place — no tbody rebuild
        updateDeleteSelected(key);
      });
      return el("th", { class: "row-select" }, box);
    }
    if (c.action) return el("th", { class: "row-actions" });
    const active = view.sort === c.title;
    return el("th", {
      class: "sortable" + (active ? " sorted" : ""),
      title: `Sort by ${c.title}`,
      onclick: () => toggleSort(key, c.title),
    }, c.title, el("span", { class: "sort-mark" },
      active ? (view.dir === 1 ? "▲" : "▼") : ""));
  }));
}

/* The one column worth filtering to more than one value at a time: "everything
   still live" is Confirmed *plus* Pending, and a single-pick dropdown cannot say
   that. The rest stay single-pick deliberately — naming two departure cities or
   two operators is not a question this table gets asked, and the popover costs a
   click the plain dropdown does not. */
const MULTI_FILTER_COLUMN = "Status";

/* Its chosen values live in view.filters as a Set where every other column
   stores a string; viewRows branches on which.

   <details> rather than a bare list of checkboxes in the cell, because the
   filter row is one row shared by every column — three stacked checkboxes in
   the Status cell would set the height of the whole row, on both tables. */
function checkboxFilter(key, title, view, list) {
  const stored = view.filters.get(title);
  // Reused rather than copied, so the Set in view.filters is the same object
  // this popover mutates — no reconciliation step to forget.
  const chosen = stored instanceof Set ? stored : new Set();
  const summary = el("summary", { title: `Filter ${title}` });
  const sync = () => {
    summary.textContent = chosen.size
      ? list.filter((v) => chosen.has(v)).join(", ") : "All";
  };
  const menu = el("div", { class: "chk-menu" }, ...list.map((v) => {
    const box = el("input", { type: "checkbox", checked: chosen.has(v) });
    box.addEventListener("change", () => {
      if (box.checked) chosen.add(v); else chosen.delete(v);
      if (chosen.size) view.filters.set(title, chosen);
      else view.filters.delete(title);
      sync();
      // Body only, so the popover stays open and the second value is one click
      // away — picking two statuses in a row is the whole point of the control.
      renderRows(key);
    });
    return el("label", { class: "chk-opt" }, box, v);
  }));
  sync();
  return el("details", { class: "chk-filter" }, summary, menu);
}

/* <details> has no light-dismiss of its own, and a filter popover left hanging
   over the rows reads as part of the table rather than as an open menu. */
document.addEventListener("click", (e) => {
  for (const d of document.querySelectorAll("details.chk-filter[open]")) {
    if (!d.contains(e.target)) d.open = false;
  }
});

function filterRow(key, cols, view, choices) {
  return el("tr", { class: "filter-row" }, ...cols.map((c) => {
    if (c.select) return el("td", { class: "filter-cell row-select" });
    if (c.action) return el("td", { class: "filter-cell row-actions" });
    const list = choices.get(c.title);
    if (c.title === MULTI_FILTER_COLUMN && list && list.length) {
      return el("td", { class: "filter-cell" },
                checkboxFilter(key, c.title, view, list));
    }
    // Only a string can go in a dropdown or a text box. The guard matters if
    // this column ever stops being the multi-select one with a Set still held:
    // the Set would otherwise reach the input as "[object Set]".
    const stored = view.filters.get(c.title);
    const current = typeof stored === "string" ? stored : "";
    let input;
    if (list && list.length) {
      input = el("select", {}, option("", "All", current === ""),
        ...list.map((v) => option(v, v, v === current)));
      input.addEventListener("change", () => setFilter(key, c.title, input.value));
    } else {
      input = el("input", { placeholder: "filter", "aria-label": `Filter ${c.title}` });
      input.value = current;
      input.addEventListener("input", () => {
        // Stored immediately so the value is never lost, applied on a timer.
        const v = VIEWS[key];
        if (input.value === "") v.filters.delete(c.title);
        else v.filters.set(c.title, input.value);
        debounceRows(key);
      });
    }
    return el("td", { class: "filter-cell" }, input);
  }));
}

function updateSummary() {
  const part = (v, label) => (v.shown === v.total
    ? `${v.total} ${label}` : `${v.shown} of ${v.total} ${label}`);
  $("#contract-summary").textContent =
    `${part(VIEWS.trips, "operable")} · ${part(VIEWS.nonop, "non-operable")}`;
}

function bodyRows(cfg, cols, rows) {
  if (!rows.length) {
    return [el("tr", {}, el("td", { class: "hint", colspan: cols.length },
      "No rows match the current search or filters."))];
  }
  return rows.map((r) => {
    const tr = el("tr", {}, ...cols.map((c) => c.cell(r)));
    if (cfg.decorate) cfg.decorate(tr, r, todayISO);
    return tr;
  });
}

/* The one prologue every table pass needs: columns, filter choices, and the
   rows the view currently shows. Was pasted into three functions, which is
   three places for them to disagree about what "visible" means. */
function computeView(key) {
  const cfg = TABLES[key], view = VIEWS[key];
  const cols = tableColumns(cfg, key);
  const all = cfg.rows();
  const choices = new Map(cols.map((c) => [c.title, columnChoices(all, c)]));
  const rows = viewRows(cfg, cols, view, choices);
  view.shown = rows.length;
  view.total = all.length;
  return { cfg, view, cols, choices, rows };
}

/* The rows the view currently shows, recomputed from live state — the one
   answer to "what does select-all mean right now". */
const visibleRows = (key) => computeView(key).rows;

/* Body only. Keeping the header and filter row in place is what lets you keep
   typing in a filter box — rebuilding them would blur the input mid-word. */
function renderRows(key) {
  const { cfg, cols, rows } = computeView(key);
  $(cfg.id).querySelector("tbody").replaceChildren(...bodyRows(cfg, cols, rows));
  syncTickBoxes(key);          // the header select-all tracks the new view
  updateSummary();
}

function renderTable(key) {
  const { cfg, view, cols, choices, rows } = computeView(key);
  updateDeleteSelected(key);

  const table = $(cfg.id);
  table.replaceChildren(
    el("thead", {}, headerRow(key, cols, view, rows), filterRow(key, cols, view, choices)),
    el("tbody", {}, ...bodyRows(cfg, cols, rows)));

  alignFilterRow(table);
  updateSummary();
}

/* The header sticks to the top of the scroll and the filter row must sit
   exactly under it. Header height depends on font and zoom, so measure it
   rather than hardcoding an offset that drifts — but only while visible:
   getBoundingClientRect on a display:none table reports 0, which pinned the
   filter row BEHIND the header after any refresh taken on another tab.
   showTab settles the debt when Data opens. */
function alignFilterRow(table) {
  if (!table.offsetParent) return;
  const top = table.querySelector("thead tr").getBoundingClientRect().height;
  for (const td of table.querySelectorAll("tr.filter-row td")) td.style.top = `${top}px`;
}

/* Jump to the first row that has not finished yet — i.e. the first row the
   past-fade does *not* apply to, so the button and the greying always agree.
   The scan is in displayed order, so under a sort that is not by date this
   lands on the first unfinished row in *that* order rather than the nearest
   one in time. Well defined either way, but only meaningful in date order. */
function jumpToToday() {
  const table = $("#trips-table");
  const target = table.querySelector("tbody tr[data-start]:not(.past)");
  if (!target) {
    // Everything in the current view is finished; the end of the table is the
    // closest thing to "now" that exists here.
    const wrap = table.closest(".table-wrap");
    wrap.scrollTo({ top: wrap.scrollHeight, behavior: "smooth" });
    toast("No unfinished trips in this view — jumped to the end.");
    return;
  }
  scrollRowIntoView(table, target);
}

/* Shared by both jumps so they cannot drift on the header offset — the two
   otherwise land at different heights on the same table. */
function scrollRowIntoView(table, target) {
  // Offset by the frozen header, or it covers the row we just navigated to.
  const wrap = table.closest(".table-wrap");
  const headH = table.querySelector("thead").getBoundingClientRect().height;
  const delta = target.getBoundingClientRect().top
    - wrap.getBoundingClientRect().top - headH;
  wrap.scrollTo({ top: wrap.scrollTop + delta, behavior: "smooth" });
  // The row looks like any other once it is in view, so flash it. Removing and
  // forcing a reflow before re-adding restarts the animation on a repeat click,
  // which the browser would otherwise coalesce into no change at all.
  target.classList.remove("jump-target");
  void target.offsetWidth;
  target.classList.add("jump-target");
}

/* Timelines -> Data. The board says which bus runs what and when; the row says
   what the sheet actually holds, and every correction is made there. Without
   this the trip you are looking at has to be found again by hand in 1670 rows.
   Rows carry data-trip (decorateTripRow) purely so this can find one.

   The failure worth handling is the row being filtered or searched out of the
   view: the button would appear to do nothing at all, which reads as broken
   rather than as filtered. Clearing the view is a visible change to something
   the user set, so it is a fallback and it is announced — never the first move.

   Called after showTab because the table is laid out but the tab was hidden,
   and getBoundingClientRect on a display:none section returns zeroes. */
function jumpToDataRow(tripId) {
  showTab("data");
  const table = $("#trips-table");
  const find = () => table.querySelector(`tbody tr[data-trip="${CSS.escape(tripId)}"]`);

  let target = find();
  if (!target) {
    clearFilters("trips");
    target = find();
    if (!target) {
      // The only way left to miss: the contract rejected the row, so it lives
      // in the non-operable table and has no parsed trip at all. The board
      // draws only parsed trips, so this should be unreachable — say so rather
      // than scroll to nothing.
      toast("That row is not in the operable table.", true);
      return;
    }
    toast("Cleared the Data filters — the row was hidden by them.");
  }
  scrollRowIntoView(table, target);
}

function clearFilters(key) {
  const v = VIEWS[key];
  v.filters.clear();
  v.search = "";
  const box = document.querySelector(`.search[data-table="${key}"]`);
  if (box) box.value = "";
  renderTable(key);              // full rebuild: the filter inputs must reset too
}

function renderTrips() {
  // The column toggles are NOT rebuilt here: they depend only on S.columns and
  // HIDDEN, and renderAll owns them — rebuilding the picker on every cell-edit
  // restore threw away nothing but did the work anyway.
  renderTable("trips");
  renderTable("nonop");
}

/* ---------- Suggestions tab ---------- */

/* The one way an assignment is made, from either the list or the board.
   mutate clears SEL: the demand is covered now, so the candidate view it
   opened is spent. */
async function acceptSuggestion(s) {
  await mutate("/accept", { demand: s.demand, company: s.company,
                            unit: s.unit, supply: s.supply },
               "Assignment accepted");
}

/* An empty run is the one thing in a candidate that costs money whether or not
   the trip earns any, so it is never folded into the gap text — it gets its own
   badge naming both cities, on every surface that offers the link. */
const erBadge = (link) => el("span", { class: "badge er",
  title: `The bus finishes in ${link.from_city} and must be driven empty to `
    + `${link.to_city} to start this trip — ${drivingDays(link.from_city, link.to_city)} `
    + `of the ${freeDays(link.gap_days)} free days between the two trips are that `
    + "drive, not rest. Rule 6 records the pair as an explicit ER once accepted." },
  `empty run ${link.from_city}→${link.to_city}`);

/* opts.chosen: this candidate is the one the plan already proposes. Marked in
   place rather than pulled out into its own section — the point of the list is
   that the plan's pick is one option among several, ranked by the same key as
   the rest, and lifting it out would restate it as a verdict. */
function candidateRow(s, opts = {}) {
  const parts = [];
  /* opts.showDemand: name the trip being taken — the per-unit and supply-side
     views group by bus, so the row must say which trip it is offering. Was
     hand-prepended identically at two call sites. */
  if (opts.showDemand) {
    const d = tripById(s.demand);
    parts.push(el("span", {}, `take ${tripLabel(s.demand)}`,
      " ", d ? statusBadge(d.status) : ""));
  }
  if (s.kind === "chain") {
    parts.push(el("span", {}, `${unitLabel(s.company, s.unit)}`),
      el("span", { class: "gap" },
        `after ${tripLabel(s.supply)} · ${plural(freeDays(s.gap_days), "free day")}`));
    if (s.reposition) parts.push(erBadge(s));
  } else {
    parts.push(el("span", {}, `${companyName(s.company)} (ad hoc fallback)`),
      el("span", { class: "badge adhoc" }, "chain-invisible"));
  }
  if (opts.chosen) {
    /* Deliberately not "the best option". The planner allocates across every
       trip at once and takes one link per bus per round, so a cheaper-looking
       candidate below may simply have been spent on another trip — it is not
       something the ranking overlooked, and saying "ranked first" here would
       be false on exactly the rows a person is trying to compare. */
    parts.push(el("span", { class: "badge chosen-badge",
      title: "What the planner allocated here. It solves all trips together, so "
        + "a cheaper-looking option below may have been used for another trip "
        + "rather than passed over." },
      "the plan's choice"));
  }
  /* Condensed to a count. A deep chain carries 7-9 at-risk trips and one span
     each turned every candidate into a paragraph — unreadable in the
     Suggestions tab, and actively defeating the alternatives list, whose whole
     job is letting two buses be compared at a glance. The number is what
     differs between candidates; the trips themselves are one hover away. */
  if (s.at_risk.length) {
    parts.push(el("span", { class: "risk",
      title: s.at_risk.map(tripLabel).join("\n") },
      `depends on ${plural(s.at_risk.length, "Pending trip")}`));
  }
  parts.push(el("button", { class: "btn small primary",
    onclick: () => acceptSuggestion(s) }, "Accept"));
  return el("div", { class: "cand" + (s.kind === "adhoc_fallback" ? " fallback" : "")
    + (opts.chosen ? " chosen" : "") }, ...parts);
}

/* Units other than `unit` that could take this trip right now. The plan names
   exactly one bus per trip and nothing else on screen admits the others exist,
   which is the whole reason this is here. */
function otherUnitsFor(demandId, company, unit) {
  const seen = new Map();
  for (const s of S.engine.suggestions_by_trip[demandId] || []) {
    if (s.kind !== "chain") continue;
    if (s.company === company && String(s.unit) === String(unit)) continue;
    seen.set(`${s.company}|${s.unit}`, s);
  }
  return [...seen.values()];
}

/* ---------- the plan: multi-hop chains ---------- */

/* Provisional links are derived state — the server recomputes them on every
   request and never stores them, so they are absent from column P and from any
   export until someone confirms them. Everything below renders that fact; the
   one thing it must never do is let a planned trip read as a committed one. */

/* demand trip id -> the chain link that proposes it. perState-cached: the
   board asks per bar (thousands of lookups) and three different renderers ask
   per pass — the comment used to say "built once per render" while the call
   graph had grown to three builds. */
const plannedLinks = perState(() => {
  const out = new Map();
  for (const chain of S.engine.plan.chains) {
    for (const link of chain.links) out.set(link.demand, link);
  }
  return out;
});

/* Live trips with no bus — the board's work queue, the plan summary's
   denominator, and the diagnosis input. Also asked for 3-4 times per pass. */
const uncoveredTrips = perState(() =>
  S.trips.filter((t) => t.status !== "Canceled" && !t.assignment));

/* A chain's links are ordered, and each one's supply is the previous one's
   demand. Only the first is acceptable on its own — the rest have no assigned
   bus to hang off yet, which the store enforces too (repo._accept_one). */
function chainRow(chain, links) {
  const rows = links.map((link, i) => {
    const t = tripById(link.demand);
    const parts = [
      el("span", { class: "step" }, `${i + 1}.`),
      el("span", {}, tripLabel(link.demand)),
      el("span", { class: "gap" },
        `after ${tripLabel(link.supply)} · ${plural(freeDays(link.gap_days), "free day")}`),
      link.reposition ? erBadge(link) : "",
      t ? statusBadge(t.status) : "",
    ];
    /* Without this the plan reads as the only possibility. A count here, and
       the board for the actual choosing — duplicating the candidate list into
       every chain row would bury the chain it is meant to annotate. */
    const others = otherUnitsFor(link.demand, link.company, link.unit);
    if (others.length) {
      parts.push(el("button", { class: "linklike",
        title: "Open this trip on the board to compare and pick a different bus",
        onclick: () => { showTab("timelines"); selectOnBoard(link.demand); } },
        `${others.length} other bus${others.length === 1 ? "" : "es"} could take it`));
    }
    /* Unconditional. These used to render only in what was then "manual mode",
       which left "auto mode" with no way to commit anything while its own
       summary promised "nothing is written until you confirm a chain" — a
       confirmation with no control. That setting is now the board's "Show plan
       on buses" box: it governs how the plan is DRAWN (see boardRows), not
       whether it can be accepted. */
    parts.push(i === 0
      ? el("button", { class: "btn small",
          onclick: () => acceptOneLink(link) }, "Accept this link")
      : el("span", { class: "hint" }, "needs the link above first"));
    return el("div", { class: "cand planned" }, ...parts);
  });

  const runs = links.filter((l) => l.reposition).length;
  /* Split out rather than summed into one "idle days" figure, which is what
     this said before and was wrong on any chain containing an empty run: the
     server's Plan.idle_days excludes travel (a minimum-gap reposition wastes
     nothing), so the header claimed idle days the summary line did not. Free
     days is the number the board shows; naming the driving part reconciles
     the two instead of leaving them to disagree. */
  const free = links.reduce((n, l) => n + freeDays(l.gap_days), 0);
  const driving = links.reduce((n, l) => n + drivingDays(l.from_city, l.to_city), 0);
  const head = el("div", { class: "head" },
    el("strong", {}, unitLabel(chain.company, chain.unit)),
    el("span", { class: "hint" }, ` ${links.length} trip(s) · `
      + `${plural(free, "free day")}${driving ? ` (${driving} driving)` : ""}`
      // Surfaced on the header, not just per link: accepting a chain accepts
      // every empty run in it at once, and that is the number to weigh.
      + (runs ? ` · ${runs} empty run${runs === 1 ? "" : "s"}` : "")));
  head.append(el("button", { class: "btn small primary",
    onclick: () => acceptChain(chain) }, `Accept chain (${links.length})`));
  return el("div", { class: "suggestion-group planned" }, head, ...rows);
}

async function acceptOneLink(link) {
  await mutate("/accept", { demand: link.demand, company: link.company,
                            unit: link.unit, supply: link.supply },
               "Link accepted");
}

async function acceptChain(chain) {
  await mutate("/accept-chain", { company: chain.company, unit: chain.unit },
    (res) => `Accepted ${res.accepted} link(s) on ${unitLabel(chain.company, chain.unit)}`);
}

/* Links and empty runs across a set of chains — the two numbers every summary
   states. Was hand-reduced in three places. */
const chainLinkCount = (chains) => chains.reduce((a, c) => a + c.links.length, 0);
const chainRunCount = (chains) =>
  chains.reduce((a, c) => a + c.links.filter((l) => l.reposition).length, 0);

async function acceptWholePlan() {
  const plan = S.engine.plan;
  const n = chainLinkCount(plan.chains);
  const runs = chainRunCount(plan.chains);
  if (!confirm(`Accept all ${n} proposed links across ${plan.chains.length} chains? `
      + "They become real assignments and will appear in the xlsx export."
      // Committing empty runs in bulk commits real driving cost, so the count
      // goes in the confirm rather than only in the chain list behind it.
      + (runs ? `\n\n${runs} of them require an empty repositioning run first.` : ""))) return;
  await mutate("/accept-plan", {}, (res) => `Accepted ${res.accepted} link(s)`);
}

/* Uncovered trips the horizon put out of scope — the server's count, because
   the client would have to re-derive both the demand rule and the cutoff to get
   it, and a wrong number here is worse than none. */
function beyondHorizon() {
  return (S.engine && S.engine.beyond_horizon) || 0;
}

/* Why the planner is not considering an uncovered trip at all — "started" (it
   began on or before today) or "horizon" (it begins after the plan-ahead
   window) — or undefined when it IS being considered.

   Every surface that explains an uncovered trip has to ask this before it
   reasons about rules, exactly as it asks isLocked: such a trip was never put
   to the fleet, so it has no candidate and is not reachable, and those two
   facts are precisely what "no chain can ever reach this" looks like. Until
   this existed the board said just that about every trip beyond the window —
   187 of the reference workbook's 190 on the default month — and sent people
   looking for an ad hoc bus for work that was merely not being planned yet. */
const outOfScope = (id) => S._scope.get(id);

/* What to say about one, wherever a single trip is being described. */
const SCOPE_NOTE = {
  started: "already started — the planner leaves it alone",
  horizon: "outside the plan-ahead window — not being planned yet",
};

const startedCount = perState(() =>
  [...S._scope.values()].filter((why) => why === "started").length);

/* The other end of the window from horizonHint, and the same job: a plan that
   is short or empty because trips have already left must say so, or it reads
   as a fleet that cannot cope. Worded as a fact rather than a fault — these
   are still rows, still on the board, and recording which bus really ran one
   is an ordinary assignment by hand. */
function startedHint(box) {
  const started = startedCount();
  if (!started) return;
  box.append(el("p", { class: "hint" },
    `${plural(started, "uncovered trip")} already started (on or before `
    + `${S.today}), so the planner leaves ${started === 1 ? "it" : "them"} alone. `
    + "Still listed everywhere else, and can still be assigned by hand — which "
    + "is also what tells the planner where that bus is now."));
}

/* The one message that turns an empty board back into an explicable one. On
   import day the season usually starts more than a month out, so the default
   window is genuinely empty — and without this the summary blames the fleet
   ("no chain can be built from the currently assigned units") for a window the
   dispatcher set and can widen in one click. Same reasoning as the empty-runs
   hint below it: a setting must never read as a broken engine. */
function horizonHint(box) {
  const beyond = beyondHorizon();
  if (!beyond) return;
  box.append(el("p", { class: "hint" },
    `${plural(beyond, "more uncovered trip")} outside the plan-ahead window `
    + `(after ${S.horizon_through}). Widen it to bring them in — they are still `
    + `listed everywhere else, and can still be assigned by hand.`));
}

function renderPlanSummary() {
  const box = $("#plan-summary");
  box.replaceChildren();
  const plan = S.engine.plan;
  const n = chainLinkCount(plan.chains);
  const uncovered = uncoveredTrips().length;

  if (!n) {
    box.classList.remove("hidden");
    /* Four readings of "no plan", and they send the reader somewhere different:
       nothing to do, nothing in scope, nothing chainable in a window that hid
       some, or a fleet with no room. The two middle ones are why this is split
       at all — blaming the fleet for a window the dispatcher set sends people
       hunting a capacity problem that is not there. "In scope" has two edges:
       trips past the plan-ahead window, and trips that have already started. */
    const inScope = uncovered - beyondHorizon() - startedCount();
    box.append(el("span", {}, !uncovered
      ? "Nothing uncovered — no plan needed."
      /* A chain only ever extends from a trip that already has a bus
         (plan.py's supply is the assigned trips), so a sheet with no bus on
         any trip — every new-season workbook on import day — can never get a
         plan, whatever the window or the fleet. Checked before the scope
         readings below: blaming reach or the window would send the dispatcher
         widening settings that cannot help, when the move is one assignment
         by hand per bus. */
      : !S.trips.some((t) => t.assignment)
        ? "No bus has a recorded trip yet, so there is nothing to chain from. "
          + "Assign each bus's first trip by hand (Data tab or the board) and "
          + "the planner extends from there."
      : !inScope
        /* The first wording is word for word what this said before started
           trips were left out, and still what it says when none have. */
        ? (startedCount()
          ? "Nothing left to plan: every uncovered trip has already started"
            + (beyondHorizon() ? " or lies outside the plan-ahead window." : ".")
          : "Nothing to plan inside the plan-ahead window.")
        : beyondHorizon() || startedCount()
          /* Naming the count only matters when the window hid some: "no chain
             from 190 uncovered trips" reads as a failing fleet when 186 of them
             were never considered. "Still ahead" is for Plan ahead: All, where
             started trips are the only ones left out and there is no window
             to be inside of. */
          ? `No chain can be built from the ${plural(inScope, "uncovered trip")} `
            + (beyondHorizon() ? "inside the plan-ahead window" : "still ahead")
            /* Once trips have started, "committed elsewhere" is usually the
               wrong culprit. A chain extends from a bus's recorded trips, and
               when the sheet has not been told which bus ran the ones that
               left, those records stop days short of everything still ahead
               — the fleet is not busy, it is unaccounted for. Blaming
               capacity sends people to hire buses; the hint below says what
               actually moves the plan on. */
            + (startedCount()
              ? ": none is within reach of any bus's recorded trips."
              : ". Each needs a bus that is not already committed elsewhere.")
          /* Word for word what this said before the horizon existed. With
             nothing out of scope the panel has to say what it always said, or
             "Plan ahead: All" stops meaning "as before". */
          : "No chain can be built from the currently assigned units. Every uncovered "
            + "trip needs a bus that is not already committed elsewhere."));
    startedHint(box);
    horizonHint(box);
    /* With empty runs off, a bus can only ever take a trip departing where it
       last arrived, and most of the fleet is parked where nothing departs. That
       is a setting, not a fact about the fleet, so an empty plan has to say so
       — otherwise the toggle reads as a broken engine. */
    if (uncovered && !repositionAllowed()) {
      box.append(el("p", { class: "hint" },
        "Empty runs are off, so a unit can only take a trip departing from the "
        + "city it last arrived in. Allow them to let the planner drive a bus "
        + "empty to the departure city first."));
    }
    return;
  }
  box.classList.remove("hidden");
  /* Derived from the server's own idle_days rather than recomputed, so the two
     accountings cannot drift: free days is what the board counts (gap - 1 per
     link), idle_days is what the objective minimizes (parked days only), and
     the difference is exactly the driving. */
  const free = plan.chains.reduce(
    (a, c) => a + c.links.reduce((b, l) => b + freeDays(l.gap_days), 0), 0);
  const driving = free - plan.idle_days;
  box.append(el("span", {},
    el("strong", {}, `${n} of ${uncovered} uncovered trips`),
    ` can be chained onto ${plan.chains.length} unit(s), costing `
    + `${plural(free, "free day")}`
    + (driving ? ` (${driving} of them driving an empty run, the rest idle)` : "")
    + ". ",
    // One sentence whatever the board's "Show plan on buses" box says: that
    // box decides where the plan is DRAWN, and this tab reads the same either
    // way. It keeps the promise the old auto-mode wording made — provisional
    // until accepted — because that was never true of one mode only.
    "Nothing is written until you accept a chain, a single link, or the "
    + "entire plan."));

  box.append(el("button", { class: "btn small primary",
    onclick: acceptWholePlan }, `Accept entire plan (${n})`));
  /* The gap between what a chain can reach and what the fleet can carry is the
     question people actually ask when the number looks low, so answer it here
     rather than letting them infer the engine is broken. */
  const reach = (S.engine.reachable || []).length;
  if (reach > n) {
    box.append(el("p", { class: "hint" },
      `${reach} uncovered trips are theoretically reachable by some chain, but `
      + `reachability ignores whether a bus is free. ${n} is what the fleet can `
      + `actually absorb — the rest need capacity, not planning.`));
  }
  /* Also on a non-empty plan: "12 of 190 uncovered trips" reads as a fleet
     that can barely cope when most of the 190 were never considered. */
  startedHint(box);
  horizonHint(box);
}

/* The toggles reflect server state, never local state: the objective changes
   what the server computes, so the button that looks active has to be the one
   the returned plan was actually built with. */
function renderPlanToggles() {
  const s = S.settings || { mode: "manual", objective: "coverage", reposition: true };
  /* Sits on the Timelines toolbar, since it only changes how the board is
     drawn, but is set here with the rest: it is server state like them, and a
     tick whose save did not land must not be left standing. */
  $("#plan-on-units").checked = s.mode === "auto";
  document.querySelectorAll("#objective-group button").forEach((b) =>
    b.classList.toggle("active", b.dataset.objective === s.objective));
  /* data-* is always a string, so compare against the rendered bool rather
     than letting "0" pass as truthy. */
  document.querySelectorAll("#reposition-group button").forEach((b) =>
    b.classList.toggle("active", (b.dataset.reposition === "1") === !!s.reposition));
  document.querySelectorAll("#horizon-group button").forEach((b) =>
    b.classList.toggle("active", b.dataset.horizon === s.horizon));
  /* The server's resolved date, never today + N recomputed here: the plan on
     screen was built against that exact cutoff, and a browser left open
     overnight would otherwise print a date one day off the one it is showing. */
  $("#horizon-through").textContent =
    S.horizon_through ? `through ${S.horizon_through}` : "";
}

/* Whether the engine may propose an empty repositioning run. Read from server
   settings, because every count on screen (plan links, ghost slots, stranded
   units) was computed under it. */
function repositionAllowed() {
  return S.settings ? !!S.settings.reposition : true;
}

async function setPlanSetting(patch) {
  // keepSel: changing the objective must not close an open board panel.
  await mutate("/settings", patch, null, { keepSel: true });
}

/* ---------- locks, and the master re-chain ---------- */

/* A lock says "do not re-decide this trip". The engine drops it from the demand
   set, so it draws no suggestion and appears in no chain; the re-chain refuses
   to release it. Server state throughout — a lock is stored, never inferred
   from what the plan happens to contain. */
const isLocked = (id) => S._locked.has(id);

async function toggleLock(id) {
  // keepSel: the lock button lives on the selected trip's own panel.
  await mutate(`/trips/${id}/lock`, { locked: !isLocked(id) }, null,
               { keepSel: true });
}

/* The pending preview, or null. Never survives a refresh (see there): a
   rearrangement is derived from state, and a stale one on screen would be a
   promise the server no longer intends to keep. */
let RECHAIN = null;

async function previewRechain() {
  try {
    // A refresh already in flight will null this preview when it lands —
    // let it settle first, then compute against the state that stays.
    if (REFRESH_PROMISE) await REFRESH_PROMISE.catch(() => {});
    const seq = REFRESH_SEQ;
    const preview = await api("/rechain");
    /* If any refresh landed while the preview was computing, it was computed
       against state that has already moved — the same staleness refresh's own
       RECHAIN = null exists to prevent, arriving through the other door. */
    if (seq !== REFRESH_SEQ) {
      toast("The plan moved while the preview was computing — try again.", true);
      return;
    }
    RECHAIN = preview;
    renderRechain();
    showTab("suggestions");
    $("#rechain-panel").scrollIntoView({ block: "nearest" });
  } catch (err) { toast(err.message, true); }
}

async function applyRechain() {
  /* The toast reports what the server actually wrote, not what the panel
     promised. The two agree unless something changed underneath, and if it
     did, that is exactly the case worth seeing rather than smoothing over.
     mutate's refresh clears the preview. */
  await mutate("/rechain", {}, (res) =>
    `Re-chained: ${res.released} released, ${res.accepted} assigned`
    + (res.stranded.length ? `, ${res.stranded.length} left with no bus` : ""));
}

function renderRechain() {
  const box = $("#rechain-panel");
  box.replaceChildren();
  box.classList.toggle("hidden", !RECHAIN);
  if (!RECHAIN) return;
  const r = RECHAIN;
  const links = chainLinkCount(r.plan.chains);
  const runs = chainRunCount(r.plan.chains);
  /* The number that decides the whole thing. Coverage is not monotone under a
     release — the replan can spend a bus on a trip the old arrangement left out
     and strand the one it took the bus from — so this is stated before the
     gains, not below them. */
  const net = links - r.released.length;

  box.append(el("div", { class: "detail-head" },
    el("strong", {}, "Re-chain the future"),
    el("span", { class: "hint" },
      ` · trips starting after ${r.as_of} · `
      + (r.objective === "coverage" ? "most trips covered" : "fewest idle days")
      + (r.reposition ? " · empty runs allowed" : " · same city only"))));

  const buses = r.plan.chains.length;
  box.append(el("p", {},
    `Takes back ${plural(r.released.length, "assignment")} this app made and `
    + `rebuilds ${plural(links, "link")} across `
    + `${buses} bus${buses === 1 ? "" : "es"}`      // plural() cannot do this one
    + (runs ? `, ${runs} of them needing an empty run` : "")
    + `. Net ${net >= 0 ? "+" : ""}${net} trips covered.`));

  /* Why the release is the size it is. Without this a planner reads "60
     released" on a dataset with hundreds of future trips as the feature being
     broken, rather than as the two boundaries doing their job. */
  const frozen = [];
  if (r.frozen_source) {
    frozen.push(`${r.frozen_source} future assignment(s) that came from the source `
      + "file — edit those in the Data tab");
  }
  if (r.frozen_locked) frozen.push(`${r.frozen_locked} are locked`);
  /* Listed with the other two because it is the same kind of fact — work this
     action deliberately left alone — but it is the only one the reader can undo
     from the toolbar, so it names the date and the way out. */
  if (r.frozen_horizon) {
    frozen.push(`${r.frozen_horizon} start after ${r.horizon} and are outside `
      + "the plan-ahead window");
  }
  if (frozen.length) {
    box.append(el("p", { class: "hint" }, "Untouched: " + frozen.join("; ")
      + ". Trips that have already started are never re-decided."));
  }

  if (r.stranded.length) {
    const list = el("p", { class: "reason" },
      `⚠ ${plural(r.stranded.length, "trip")} would be left with no bus at all — `
      + "they have one today and the rearrangement does not give them one back: ");
    r.stranded.slice(0, 12).forEach((id, i) => {
      if (i) list.append(", ");
      list.append(el("button", { class: "linklike",
        onclick: () => { showTab("timelines"); selectOnBoard(id); } },
        tripLabel(id)));
    });
    if (r.stranded.length > 12) list.append(`, and ${r.stranded.length - 12} more`);
    box.append(list);
  }

  const foot = el("div", { class: "toolbar" });
  foot.append(el("button", { class: "btn small primary", onclick: applyRechain },
    `Apply (${r.released.length} released, ${links} assigned)`));
  foot.append(el("button", { class: "btn small",
    onclick: () => { RECHAIN = null; renderRechain(); } }, "Discard"));
  foot.append(el("span", { class: "hint" },
    "Nothing is written until you apply. One undo step reverses all of it."));
  box.append(foot);
}

/* ---------- rule 11: reclaim work from ad hoc buses ---------- */

/* Same lifecycle as RECHAIN, and null'd by refresh for the same reason. */
let RECLAIM = null;

async function previewReclaim() {
  try {
    // Same settle-first rule as previewRechain.
    if (REFRESH_PROMISE) await REFRESH_PROMISE.catch(() => {});
    const seq = REFRESH_SEQ;
    const preview = await api("/reclaim-adhoc");
    if (seq !== REFRESH_SEQ) {
      toast("The plan moved while the preview was computing — try again.", true);
      return;
    }
    RECLAIM = preview;
    renderReclaim();
    showTab("suggestions");
    $("#reclaim-panel").scrollIntoView({ block: "nearest" });
  } catch (err) { toast(err.message, true); }
}

async function applyReclaim() {
  await mutate("/reclaim-adhoc", {}, (res) =>
    `Reclaimed ${res.reclaimed} from ad hoc, ${res.accepted} links assigned`
    + (res.stayed_adhoc.length
        ? `, ${res.stayed_adhoc.length} kept their hired bus` : ""));
}

function renderReclaim() {
  const box = $("#reclaim-panel");
  box.replaceChildren();
  box.classList.toggle("hidden", !RECLAIM);
  if (!RECLAIM) return;
  const r = RECLAIM;
  const links = chainLinkCount(r.plan.chains);

  box.append(el("div", { class: "detail-head" },
    el("strong", {}, "Reclaim trips from ad hoc"),
    el("span", { class: "hint" },
      ` · hires that can still be cancelled (${r.notice_days} days' notice, so `
      + `starting ${r.cutoff} or later) · `
      + (r.objective === "coverage" ? "most trips covered" : "fewest idle days")
      + (r.reposition ? " · empty runs allowed" : " · same city only"))));

  box.append(el("p", {},
    `${plural(r.eligible.length, "ad hoc booking")} could still be cancelled. `
    + `${r.reclaimed.length} of them get a chartered bus; `
    + `the other ${r.stayed_adhoc.length} keep their hired bus — `
    + "nothing is cancelled for those."));

  /* The source-file carve-out, stated rather than left to be discovered. Every
     other automation in this app refuses to touch a company name that arrived
     on the spreadsheet; this one may, because an ad hoc booking is a purchase
     and not a fact about which bus exists. Saying so here is half of what makes
     that safe — the other half is that this button is the only way in. */
  if (r.from_source.length) {
    box.append(el("p", { class: "hint" },
      `${r.from_source.length} of them were named in the spreadsheet. Cancelling `
      + "a hire is a commercial decision, not an edit to what the sheet says a "
      + "bus is — so this action may propose it, and only you can commit it."));
  }

  /* Not `stranded`, and the wording has to keep those apart: these trips have
     no bus TODAY either. What they lose is a proposal on this tab. Worded as a
     loss, a dispatcher reads it as damage and refuses a better plan. */
  if (r.regressed.length) {
    const list = el("p", { class: "reason" },
      `${plural(r.regressed.length, "trip")} the current plan proposes would no `
      + "longer be covered by this one — they have no bus today either, and the "
      + "buses go to the reclaimed trips instead: ");
    r.regressed.slice(0, 12).forEach((id, i) => {
      if (i) list.append(", ");
      list.append(el("button", { class: "linklike",
        onclick: () => { showTab("timelines"); selectOnBoard(id); } },
        tripLabel(id)));
    });
    if (r.regressed.length > 12) {
      list.append(`, and ${r.regressed.length - 12} more`);
    }
    box.append(list);
  }

  // Why the eligible list is the size it is — the same job the re-chain
  // panel's "Untouched" line does.
  const frozen = [];
  if (r.frozen_notice) {
    frozen.push(`${r.frozen_notice} start within ${r.notice_days} days, so the `
      + "hire stands");
  }
  if (r.frozen_pax) {
    frozen.push(`${r.frozen_pax} carry a party other than 49 — only an ad hoc `
      + "bus can take those (rule 10)");
  }
  if (r.frozen_locked) frozen.push(`${r.frozen_locked} are locked`);
  if (r.frozen_horizon) {
    frozen.push(`${r.frozen_horizon} start after ${r.horizon} and are outside `
      + "the plan-ahead window");
  }
  if (frozen.length) box.append(el("p", { class: "hint" },
    "Untouched: " + frozen.join("; ") + "."));

  /* The two windows can cross: a plan-ahead window shorter than the notice
     period leaves no trip that is both far enough away to cancel and near
     enough to plan, so the eligible list is empty for a reason no count above
     makes obvious. Said plainly, because the alternative is a button that looks
     broken every time it is pressed. */
  if (!r.eligible.length && r.horizon && r.horizon < r.cutoff) {
    box.append(el("p", { class: "reason" },
      `Nothing can be reclaimed: a hire needs ${r.notice_days} days' notice `
      + `(so ${r.cutoff} or later), but the plan-ahead window ends ${r.horizon}. `
      + "Widen it past the notice date to reclaim anything."));
  }

  const foot = el("div", { class: "toolbar" });
  /* Both numbers, because they differ and the gap surprises people: the write
     is the whole replan, since a reclaimed trip is often fed by a hop onto an
     ordinary uncovered trip. Every extra link is one "Accept the whole plan"
     would have written anyway. */
  foot.append(el("button", { class: "btn small primary", onclick: applyReclaim },
    `Apply (${r.reclaimed.length} reclaimed, ${links} links written)`));
  foot.append(el("button", { class: "btn small",
    onclick: () => { RECLAIM = null; renderReclaim(); } }, "Discard"));
  foot.append(el("span", { class: "hint" },
    "Nothing is written until you apply. One undo step reverses all of it."));
  box.append(foot);
}

/* ---------- the rule-8 solver: optimize coverage ---------- */

/* Same lifecycle as RECHAIN: a preview computed from a state snapshot, never
   surviving a refresh, committed only by an explicit Apply that recomputes
   server-side. */
let OPTIMAL = null;

async function previewOptimal() {
  const btn = $("#optimize-btn");
  btn.disabled = true;
  btn.textContent = "Solving…";
  try {
    // Same settle-first rule as previewRechain.
    if (REFRESH_PROMISE) await REFRESH_PROMISE.catch(() => {});
    const seq = REFRESH_SEQ;
    const preview = await api("/optimal-plan");
    if (seq !== REFRESH_SEQ) {
      toast("The plan moved while the solver was running — try again.", true);
      return;
    }
    OPTIMAL = preview;
    renderOptimal();
    showTab("suggestions");
    $("#optimal-panel").scrollIntoView({ block: "nearest" });
  } catch (err) { toast(err.message, true); }
  finally {
    btn.disabled = false;
    btn.textContent = "Optimize coverage…";
  }
}

function renderOptimal() {
  const box = $("#optimal-panel");
  box.replaceChildren();
  box.classList.toggle("hidden", !OPTIMAL);
  if (!OPTIMAL) return;
  const o = OPTIMAL;
  const links = chainLinkCount(o.plan.chains);
  const runs = chainRunCount(o.plan.chains);
  const gain = links - o.greedy_covered;

  box.append(el("div", { class: "detail-head" },
    el("strong", {}, "Optimal coverage"),
    el("span", { class: "hint" },
      ` · exact solve over the same chaining rules · ${o.seconds}s`
      + (o.status === "Optimal" ? "" : ` · ${o.status}`))));

  box.append(el("p", {},
    `Covers ${plural(links, "trip")} across ${plural(o.plan.chains.length, "chain")}`
    + (runs ? `, ${runs} of them needing an empty run` : ", no empty runs")
    + `. The greedy plan below covers ${o.greedy_covered} — `
    + (gain > 0 ? `${gain} more with this one.` : "no improvement right now.")));

  /* Every link was replayed through the engine's own feasibility check before
     it reached this panel; a nonzero count here means some claims did not
     survive that replay and were dropped rather than shown. */
  if (o.refused) {
    box.append(el("p", { class: "reason" },
      `⚠ ${plural(o.refused, "proposed link")} failed the engine's own check `
      + "and was dropped from this plan."));
  }

  // Compact chain digest — the full link-by-link reading lives in the plan
  // list once applied. Deliberately no per-link accept buttons: a partial
  // commit changes the problem, and the whole point of this plan is that it
  // was solved together.
  for (const chain of o.plan.chains) {
    const cruns = chain.links.filter((l) => l.reposition).length;
    box.append(el("div", { class: "cand planned" },
      el("strong", {}, unitLabel(chain.company, chain.unit)),
      el("span", { class: "gap" },
        ` ${plural(chain.links.length, "trip")}`
        + (cruns ? ` · ${cruns} empty run${cruns === 1 ? "" : "s"}` : "")
        + ": " + chain.links.map((l) => {
            const t = tripById(l.demand);
            return t ? `${t.dep}→${t.arr} ${t.start}` : l.demand;
          }).join("  ·  "))));
  }

  const foot = el("div", { class: "toolbar" });
  foot.append(el("button", { class: "btn small primary",
    onclick: applyOptimal }, `Apply (${links} links)`));
  foot.append(el("button", { class: "btn small",
    onclick: () => { OPTIMAL = null; renderOptimal(); } }, "Discard"));
  foot.append(el("span", { class: "hint" },
    "Nothing is written until you apply. One undo step reverses all of it."));
  box.append(foot);
}

async function applyOptimal() {
  // The server recomputes before writing (same rule as re-chain), so the
  // toast reports what was actually committed, not what the panel promised.
  await mutate("/optimal-plan", {}, (res) => `Accepted ${res.accepted} link(s)`);
}

function renderPlanChains() {
  const box = $("#plan-chains");
  box.replaceChildren();
  for (const chain of S.engine.plan.chains) {
    box.append(chainRow(chain, chain.links));
  }
}

/* The loose-candidates list renders hundreds of groups into a <details> that
   starts collapsed — so while it is closed the work is only marked owed, and
   the toggle listener below settles it on expand. */
let SUGGESTIONS_STALE = true;

function renderSuggestions() {
  const details = $("#loose-candidates");
  if (!details.open) { SUGGESTIONS_STALE = true; return; }
  SUGGESTIONS_STALE = false;
  const box = $("#suggestions");
  box.replaceChildren();
  const direction = document.querySelector('input[name="direction"]:checked').value;
  if (direction === "by-trip") {
    const entries = Object.entries(S.engine.suggestions_by_trip);
    if (!entries.length) box.append(el("p", { class: "hint" }, "No uncovered trips with candidates."));
    for (const [tripId, suggestions] of entries) {
      const t = tripById(tripId);
      box.append(el("div", { class: "suggestion-group" },
        el("div", { class: "head" },
          tripLabel(tripId), " ", t ? statusBadge(t.status) : ""),
        ...suggestions.map((s) => candidateRow(s))));
    }
  } else {
    const entries = Object.entries(S.engine.suggestions_by_unit);
    if (!entries.length) box.append(el("p", { class: "hint" }, "No free units with candidates."));
    for (const [unitKey, suggestions] of entries) {
      const [company, unit] = unitKey.split("|");
      box.append(el("div", { class: "suggestion-group" },
        el("div", { class: "head" }, `${unitLabel(company, unit)}`),
        ...suggestions.map((s) => candidateRow(s, { showDemand: true }))));
    }
  }
}

$("#loose-candidates").addEventListener("toggle", () => {
  if ($("#loose-candidates").open && SUGGESTIONS_STALE) renderSuggestions();
});

/* ---------- Timelines tab: the board ---------- */

/* A day-granularity Gantt. Every trip covers whole days with both endpoints
   inclusive, so one day is one column and a trip spans (end - start + 1) of
   them. Date.parse of an ISO date is UTC midnight, so differencing two of them
   is exact day arithmetic — no timezone drift, no DST hour to lose. */
const DAY_MS = 86400000;
const LANE_H = 24;                  // px per stacked lane inside one row
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

let DAY_W = 22;                     // px per day — the zoom control
let SHOW_CANCELED = false;
let SHOW_ADHOC = true;

/* "All uncovered" starts ticked and remembers its state per browser. The
   actionable-only view used to be the default, and the first assignment on a
   new-season sheet dropped the board from 287 bars to 4: the buses with no
   trip yet need their first one assigned by hand, and their candidates were
   exactly what got hidden. Stored in localStorage, not server settings: it is
   a view preference of whoever is at this browser, not plan data. Guarded
   like loadSet — storage that throws must fall back, not break the board. */
const ALL_UNCOVERED_KEY = "buschain.all-uncovered";
let ALL_UNCOVERED = (() => {
  try { return localStorage.getItem(ALL_UNCOVERED_KEY) !== "false"; }
  catch (err) { return true; }
})();

/* Horizontal scroll is state the user owns: re-rendering on a selection or an
   accept must not yank the view back. Kept as a day index rather than pixels
   so it survives a zoom change with the same date under the left edge.
   BOARD_RETARGET says "BOARD_DAY is already the position I want, do not read
   it back off the element" — which zoom needs (scrollLeft is still in the old
   pixel scale) and the Today button needs (null means: recompute from today). */
let BOARD_DAY = null;
let BOARD_RETARGET = true;

/* The board skipped a render because its tab was hidden; showTab settles the
   debt when the tab opens. */
let BOARD_STALE = false;

/* Both sections start open. Ad hoc used to start collapsed, to keep its ~60
   lanes of bars (reference data) from pushing every unit row below the fold —
   but the effect was that half the fleet load was invisible unless you knew to
   click a grey header, and "what is this company actually running" is exactly
   what the board is read for. The height is the accepted cost; the toolbar's
   Ad hoc switch is the way out of it, and folding a section by clicking its
   header still works for a temporary one. */
let COLLAPSED = new Set();

/* The trip whose chain options the board is showing. Whether it reads as a
   demand (uncovered — "who could take this?") or a supply (assigned — "what
   can this pick up next?") is derived from S at render time, so a refresh that
   changes the trip's assignment flips the view instead of leaving a stale
   mode behind. */
let SEL = null;

/* A trip to bring into view on the next render. Set by callers outside the
   board (the integrity list), which cannot know a trip's x position — only
   renderBoard knows where the axis starts. */
let BOARD_FOCUS = null;

function selectOnBoard(tripId) {
  SEL = tripId;
  BOARD_FOCUS = tripId;
  NEXT_ALL = false;
  renderBoard();
}

/* Rows top to bottom: the uncovered work queue, one row per numbered unit,
   then one per company serving ad hoc. The engine's timelines hold only live
   trips; canceled ones are folded back in here on request, because they are
   history a planner sometimes needs and the engine has no reason to carry
   them. A canceled trip with no assignment is neither demand nor anyone's
   history, so it stays off the board in either mode. */
function boardRows() {
  const rows = [];
  const canceled = SHOW_CANCELED
    ? S.trips.filter((t) => t.status === "Canceled" && t.assignment)
    : [];

  /* Most uncovered trips have no feasible unit at all, and drawing all of
     them packs ~35 lanes of bars nobody can act on across the top of the
     board, burying the chains underneath. Unticking "All uncovered" narrows
     the row to the actionable subset (opt-in now; see ALL_UNCOVERED). What
     that leaves out is counted on the row, never dropped silently. */
  const uncovered = uncoveredTrips();
  const planned = plannedLinks();
  /* "Show plan on buses" is for seeing the plan as a running schedule, so its
     trips sit on the bus that would run them. They keep the provisional style
     (tripBar) — moving a bar must not be what tells someone it is committed.
     Stored as mode "auto": see the #plan-on-units listener. */
  const onUnitRows = !!S.settings && S.settings.mode === "auto";
  const loose = onUnitRows ? uncovered.filter((t) => !planned.has(t.id)) : uncovered;

  const actionable = loose.filter((t) => S.engine.suggestions_by_trip[t.id]);
  const reach = reachableSet();
  const later = loose.filter(
    (t) => !S.engine.suggestions_by_trip[t.id] && reach.has(t.id));
  /* Counted apart from "no chain", which they would otherwise swell: a trip
     outside the planning window has no candidate and is not reachable, but
     nobody asked the fleet about it — and "187 no chain" on the default month
     is a capacity alarm for trips that are simply not being planned yet. */
  const started = loose.filter((t) => outOfScope(t.id) === "started").length;
  const beyond = loose.filter((t) => outOfScope(t.id) === "horizon").length;
  const never = loose.length - actionable.length - later.length - started - beyond;
  const scopeNote = (started ? ` · ${started} already started` : "")
    + (beyond ? ` · ${beyond} outside the window` : "");
  const plannedCount = uncovered.length - loose.length;
  /* The actionable default exists to stop un-actionable bars burying the
     chains — but when NOTHING is actionable it hides the entire row, and a
     fully-planned dataset (the engine with no feasible link left) is exactly
     that case. The result was a board with no uncovered trip drawn anywhere,
     so there was nothing to click, and therefore no way to reach the manual
     assign control — the escape hatch sat behind the same gate it exists to
     escape. An empty row buries the chains no less thoroughly than a tall one
     and tells you nothing, so fall back to showing them. */
  const noneActionable = !actionable.length && loose.length > 0;
  /* The same reasoning one step further: with no unit and no ad hoc row on
     the board there are no chains to bury, so the filter protects nothing and
     only hides the work. That is the board on a new-season import, where no
     trip has a bus yet — and noneActionable alone does not catch it, because
     a single Y trip's rule-3 ad hoc fallback counts as actionable. Measured:
     "1 of 288 actionable", one bar, and a two-day axis under Plan ahead: All. */
  const nothingToBury = !Object.keys(S.engine.timelines).length
    && !Object.keys(S.engine.adhoc_by_company).length;
  /* A locked trip is dropped from the demand set, so it can never be
     actionable, so the actionable default would hide every one of them — and
     the only control that unlocks a trip is on the panel you reach by clicking
     its bar. Drawn unconditionally, or locking is a one-way door. */
  const lockedLoose = loose.filter((t) => isLocked(t.id));
  const lockNote = lockedLoose.length ? ` · ${lockedLoose.length} locked` : "";
  rows.push({
    kind: "uncovered", key: "uncovered", label: "Uncovered",
    /* Every trip drawn means the breakdown, not "N of M actionable" — that
       wording names a filter, and it is not being applied. */
    note: (ALL_UNCOVERED || (nothingToBury && !noneActionable)
      ? `${actionable.length} now · ${later.length} later · ${never} no chain`
        + scopeNote
      : noneActionable
      ? `${loose.length} uncovered · none the planner can act on — `
        + "select one to assign it by hand"
      : `${actionable.length} of ${loose.length} actionable`)
      + lockNote
      + (plannedCount ? ` · ${plannedCount} planned below` : ""),
    trips: ALL_UNCOVERED || noneActionable || nothingToBury
      ? loose : [...actionable, ...lockedLoose],
  });

  for (const [unitKey, ids] of Object.entries(S.engine.timelines)) {
    const [company, unit] = unitKey.split("|");
    const committed = ids.map(tripById).filter(Boolean);
    /* Diagnosed against the full uncovered set, not `loose`: what a stranded
       bus forfeits is every trip it can no longer reach, whether or not some
       other bus is provisionally penciled in for it. */
    const idle = unitIdleReason(unitKey, committed, uncovered);
    rows.push({
      kind: "unit", key: unitKey, label: unitLabel(company, unit),
      note: idle && idle.note ? `${committed.length} · ${idle.note}` : "",
      title: idle ? `${unitLabel(company, unit)} — ${idle.title}` : "",
      state: idle ? idle.state : "",
      short: idle && idle.short,
      trips: [
        ...committed,
        ...canceled.filter((t) => t.assignment.company === company
          && String(t.assignment.unit) === unit),
        ...(onUnitRows
          ? uncovered.filter((t) => {
              const link = planned.get(t.id);
              return link && link.company === company
                && String(link.unit) === unit;
            })
          : []),
      ],
    });
  }

  /* A registered bus with no trips yet still gets a row. The engine's
     timelines are built from assigned trips, so without this a bus added on
     the Registry tab is invisible here — and the board is where a trip gets
     dragged onto a bus, so its first trip had nowhere to land. On a new
     season's workbook, which names no bus on any trip, that is every bus.
     drop_targets already answers for idle buses; only the row was missing. */
  const haveRows = new Set(Object.keys(S.engine.timelines));
  for (const c of S.registry.companies) {
    for (const u of c.units) {
      const key = `${c.key}|${u}`;
      if (haveRows.has(key)) continue;
      // A canceled-only unit is drawn by the fold-in below, with its trips.
      if (SHOW_CANCELED && canceled.some((t) => t.assignment.company === c.key
        && t.assignment.unit === u)) continue;
      rows.push({ kind: "unit", key, label: unitLabel(c.key, String(u)),
                  note: "no trips yet", trips: [] });
    }
  }

  /* A unit whose ONLY trips are canceled has no timeline row — the engine's
     timelines hold live trips — so fold-in must create the row or "Show
     canceled" cannot show what it promises. Appended after the real units to
     keep the section contiguous. */
  if (SHOW_CANCELED) {
    const haveUnits = new Set(Object.keys(S.engine.timelines));
    for (const t of canceled) {
      if (t.assignment.unit == null) continue;
      const key = `${t.assignment.company}|${t.assignment.unit}`;
      if (haveUnits.has(key)) continue;
      haveUnits.add(key);
      rows.push({
        kind: "unit", key, label: unitLabel(t.assignment.company, t.assignment.unit),
        note: "canceled only",
        trips: canceled.filter((c) => c.assignment.unit != null
          && `${c.assignment.company}|${c.assignment.unit}` === key),
      });
    }
  }

  /* Dropped from the row set rather than collapsed, so the switch means "leave
     ad hoc off this board" and not "fold it away" — the section header goes
     too. One consequence worth knowing: renderBoard takes the axis extent from
     the rows it is given, so hiding ad hoc can move the board's first and last
     day — a dataset whose earliest or latest trip is an ad hoc one will
     visibly re-scale when this is unticked. */
  if (SHOW_ADHOC) {
    for (const [company, ids] of Object.entries(S.engine.adhoc_by_company)) {
      rows.push({
        kind: "adhoc", key: "adhoc|" + company, label: companyName(company),
        trips: [
          ...ids.map(tripById).filter(Boolean),
          ...canceled.filter((t) => t.assignment.company === company
            && t.assignment.unit == null),
        ],
      });
    }
    // Same orphan rule as the unit section: a company whose only ad hoc
    // trips are canceled has no adhoc_by_company key and needs its row made.
    if (SHOW_CANCELED) {
      const haveCompanies = new Set(Object.keys(S.engine.adhoc_by_company));
      for (const t of canceled) {
        if (t.assignment.unit != null) continue;
        const company = t.assignment.company;
        if (haveCompanies.has(company)) continue;
        haveCompanies.add(company);
        rows.push({
          kind: "adhoc", key: "adhoc|" + company, label: companyName(company),
          note: "canceled only",
          trips: canceled.filter((c) => c.assignment.unit == null
            && c.assignment.company === company),
        });
      }
    }
  }
  return rows;
}

/* Greedy interval packing: a trip takes the first lane whose last trip ended
   before it starts. Inclusive end dates make a same-day touch an overlap
   (checks.py reads them the same way), so those bars stack instead of sitting
   side by side — the stacking is the point, it is how an overlap becomes
   visible without reading any dates. */
function packLanes(trips, flagOverlap) {
  const sorted = [...trips].sort((a, b) =>
    a.start.localeCompare(b.start) || a.end.localeCompare(b.end)
    || a.id.localeCompare(b.id));
  const laneEnd = [];
  const lane = new Map();
  for (const t of sorted) {
    let i = laneEnd.findIndex((end) => end < t.start);
    if (i === -1) i = laneEnd.length;
    laneEnd[i] = t.end;
    lane.set(t.id, i);
  }
  /* Overlap is a conflict only where one physical bus is implied: two trips
     on the same numbered unit cannot both happen. Uncovered trips overlap all
     the time (that is just a busy week), and same-company ad hoc trips are
     separate buses — over-commitment there is what fleet_warnings measures.
     Canceled trips are excluded either way: they are not happening. */
  const overlap = new Set();
  const live = flagOverlap ? sorted.filter((t) => t.status !== "Canceled") : [];
  for (let i = 0; i < live.length; i++) {
    for (let j = i + 1; j < live.length && live[j].start <= live[i].end; j++) {
      overlap.add(live[i].id);
      overlap.add(live[j].id);
    }
  }
  return { lane, overlap, lanes: Math.max(laneEnd.length, 1) };
}

/* Uncovered trips some sequence of accepts could chain into (engine/reach.py).
   A trip outside this set is not waiting on a decision — no chain will ever
   reach it, so it needs capacity rather than planning. */
const reachableSet = perState(() => new Set(S.engine.reachable || []));

/* The trip a unit's chain currently ends on — the only one it can be extended
   from, and therefore what every "what next" control resolves to. */
const unitTailId = (unitKey) => {
  const trips = (S.engine.timelines[unitKey] || []).map(tripById).filter(Boolean);
  return trips.length ? trips.reduce((a, b) => (b.end > a.end ? b : a)).id : null;
};

/* Every live trip on a numbered unit — the only trips that can feed a chain.
   Ad hoc assignments are chain-invisible and deliberately absent. */
function supplyTrips() {
  const out = [];
  for (const [unitKey, ids] of Object.entries(S.engine.timelines)) {
    const [company, unit] = unitKey.split("|");
    for (const id of ids) {
      const t = tripById(id);
      if (t) out.push({ t, company, unit, label: unitLabel(company, unit) });
    }
  }
  return out;
}

/* "No feasible chain" on its own leaves you guessing which of the four rules
   bit. Re-walk them in the engine's own order (city, then window, then
   constraint, then the unit's timeline) and report the first one that ruled
   every unit out — naming the nearest arrival and how far it misses by, since
   that is the fact you would otherwise go digging for by hand. */
function noChainReason(d) {
  const supply = supplyTrips();
  const inCity = supply.filter((s) => s.t.arr === d.dep);
  const gapOf = (s) => daysBetween(s.t.end, d.start);

  if (!inCity.length) {
    if (!repositionAllowed()) {
      return `No unit ever arrives in ${d.dep}, and empty runs are off, so `
        + "nothing can chain into this trip.";
    }
    /* Empty runs widen the question to every unit's TAIL (the one place a
       drive is allowed), each judged against its own pair window — the same
       measurement tailOptions makes. Safe to narrate without re-deciding:
       this function only runs when the engine found no edge, so every
       candidate below is a genuine miss; the job is to name the closest. */
    const tails = Object.keys(S.engine.timelines).map((key) => {
      const t = tripById(unitTailId(key));
      if (!t || d.start <= t.end) return null;
      if (constraintBlocks(d, key.split("|")[0])) return null;
      const gap = daysBetween(t.end, d.start);
      const lo = minGapDays(t.arr, d.dep);
      return { key, t, gap, lo,
               miss: gap < lo ? lo - gap : Math.max(gap - (lo + maxRest()), 0) };
    }).filter(Boolean);
    if (!tails.length) {
      return `No unit ever arrives in ${d.dep}, and no unconstrained chain `
        + "ends before this trip starts, so not even an empty run can reach it.";
    }
    const near = tails.reduce((a, b) => (b.miss < a.miss ? b : a));
    const who = unitLabel(...near.key.split("|"));
    return near.gap < near.lo
      ? `No unit arrives in ${d.dep}. The closest empty run — ${who} from `
        + `${near.t.arr} — needs ${plural(drivingDays(near.t.arr, d.dep), "clear day")} `
        + `to drive over and misses by ${plural(near.lo - near.gap, "day")}.`
      : `No unit arrives in ${d.dep}. The nearest chain end (${who}, free from `
        + `${near.t.end}) would leave the bus standing `
        + `${plural(freeDays(near.gap), "free day")} — past the `
        + `${maxRest()}-day rest limit.`;
  }

  const inWindow = inCity.filter((s) => gapOf(s) >= minGap() && gapOf(s) <= maxGap());
  if (!inWindow.length) {
    // Closest miss: distance to the nearest edge of the window. When empty
    // runs are on, the engine's no-edge verdict also covers every cross-city
    // drive — say so, or this reads as if only same-city was considered.
    const miss = (s) => (gapOf(s) < minGap() ? minGap() - gapOf(s) : gapOf(s) - maxGap());
    const near = inCity.reduce((a, b) => (miss(b) < miss(a) ? b : a));
    const gap = gapOf(near);
    const also = repositionAllowed()
      ? " (No empty run from another city fits its window either.)" : "";
    return (gap < minGap()
      ? `A unit reaches ${d.dep}, but not before this trip departs — the closest is `
        + `${near.label} arriving ${near.t.end}, ${minGap() - gap} day(s) too late.`
      : `Nearest arrival in ${d.dep} is ${near.label} on ${near.t.end}, which would `
        + `leave the bus standing ${plural(freeDays(gap), "free day")} before this trip `
        + `— past the ${maxRest()}-day rest limit.`) + also;
  }

  if (d.yn === "Y") {
    return `Only ${companyName(d.constraint_company)} may serve this trip, and none of `
      + `its units reach ${d.dep} within the chaining window.`;
  }
  if (d.yn === "N") {
    return `${companyName(d.constraint_company)} is excluded from this trip, and only `
      + `its units reach ${d.dep} within the window.`;
  }

  // City, window and constraints all pass, so rule 6 is what is left: taking
  // this trip would break the unit's own timeline.
  const names = [...new Set(inWindow.map((s) => s.label))].join(", ");
  return `${names} could reach ${d.dep} in time, but taking this trip would clash with `
    + "what that unit is already booked for (overlap, or a trip already sitting in the gap).";
}

const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const shortDate = (iso) => {
  const d = new Date(Date.parse(iso));
  return `${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()}`;
};

/* The dual of noChainReason: that says why a TRIP has no bus, this says why a
   BUS has no trip. A unit row that simply stops halfway across the board while
   the uncovered row runs on for another month reads as a planner bug, and
   without this there is nothing on screen to say otherwise.

   The distinction worth drawing is stranded vs everything else. A chain only
   ever extends from its TAIL, so a bus whose tail no longer reaches anything
   can never take another trip, however much work is left and whatever order a
   person works in. That is a permanent, silent exit from the model, and it is
   not the same fact as "this bus is busy" (booked) or "there is nothing left"
   (done).

   What counts as "reaches" is the part this used to get wrong. It walked
   same-city departures against a flat 1-4 day window, which was the rule
   before empty runs existed. Since feasible_edges gained repositioning, a bus
   parked where nothing departs is NOT stranded — it can be driven empty — and
   the window it is judged against is the city pair's, not 1-4. The old version
   therefore reached the right verdict by the wrong route: it explained
   Nordibuzz 1 as "nearest STO departure is 3 past the limit" when the real
   story is that the two trips it could have been driven to miss by one day
   each, one on either side. Right answer, unusable explanation.

   The measurement itself (tailOptions) lives in rules.js with the rest of the
   window arithmetic; this function narrates it. */
function unitIdleReason(unitKey, unitTrips, uncovered) {
  if ((S.engine.suggestions_by_unit[unitKey] || []).length) return null;
  if (!unitTrips.length) return null;
  // Locked trips are not demand the server ever considered, so measuring
  // them here would diagnose a "booked" or "stranded" state off trips the
  // lock — not a rule — put out of reach. Trips outside the planning window
  // are the same case for the same reason: this bus was never offered them,
  // so "2 days short of the nearest departure" about one that has already
  // left would be an explanation of a decision nobody made.
  uncovered = uncovered.filter((t) => !isLocked(t.id) && !outOfScope(t.id));

  /* With empty runs off the engine never looks at a cross-city departure, so
     neither may this: measuring one against its pair window would report a miss
     by N days for a trip that was excluded by policy, not by the calendar. This
     is the one place the explanation is allowed to narrow — nextTrips below is
     an override hatch and deliberately keeps showing them. */
  const { tail, measured: aimable } = tailOptions(unitTrips, uncovered);
  const measured = repositionAllowed()
    ? aimable : aimable.filter((m) => !m.empty);

  if (!measured.length) {
    /* Trips DO start after the tail; they just all depart elsewhere. Calling
       that "done" would credit the fleet with finishing work it cannot reach,
       and hide the fact that a setting is what put it out of range. */
    if (aimable.length) {
      return { state: "stranded", note: `idle since ${shortDate(tail.end)}`,
        short: `in ${tail.arr} since ${shortDate(tail.end)}; nothing departs `
          + `${tail.arr} and empty runs are off`,
        title: `Parked in ${tail.arr} since ${tail.end}. `
          + `${plural(aimable.length, "uncovered trip")} `
          + `${aimable.length === 1 ? "starts" : "start"} after that, but none `
          + `departs from ${tail.arr} and empty runs are off, so nothing can be `
          + "chained onto this bus in any order. Allowing empty runs would let it "
          + "be driven to a departure city first." };
    }
    return { state: "done", note: "chain complete",
      title: `Chain runs to ${tail.end} in ${tail.arr}, and no uncovered trip starts `
        + "after that. This unit is not idle — it is done." };
  }

  /* Reachable in time from ANYWHERE in the chain: a same-city link can land
     in a gap earlier in the timeline. The empty run is the tail-only part
     (feasible_edges allows it off the tail alone — mid-chain it would strand
     whatever comes next), which is why only the tail gets the pair window.
     Nothing here recomputes the engine's verdict: reaching this function at
     all means it found no edge, so this only says which rule ran out first. */
  const reachable = uncovered.filter((d) => unitTrips.some((s) => {
    const g = daysBetween(s.end, d.start);
    // With empty runs off the tail loses its privilege and is judged like any
    // mid-chain trip: same city, flat window.
    if (s.id === tail.id && repositionAllowed()) {
      const lo = minGapDays(s.arr, d.dep);
      return g >= lo && g <= lo + maxRest();
    }
    return s.arr === d.dep && g >= minGap() && g <= maxGap();
  })).length;

  /* Stranded is a claim about the TAIL, not about the whole timeline, and it
     stays that way deliberately: a mid-chain slot does not move the tail, so
     a bus with one is still permanently done extending. Gating on `reachable`
     instead would quietly reclassify units the moment repositioning widened
     the window — Nordic Alliance 1 among them — and the capacity note counts
     these to say how much fleet is recoverable by scheduling. The mid-chain
     count is worth stating, but as detail, not as an escape hatch. */
  if (measured.some((m) => !m.early && !m.late)) {
    return { state: "booked", note: "",
      title: `${plural(reachable, "uncovered trip")} could be reached in time, but each is `
        + "ruled out by a company constraint or clashes with what this unit is already "
        + "booked for (an overlap, or a trip already sitting in the gap)." };
  }

  /* Stranded. Both sides of the window get named when both exist: a bus can
     be squeezed out by a trip that leaves before the drive fits AND by the
     next one leaving after the rest limit, and hearing only one of those
     makes the miss look bigger than it is. Ties break on the earlier
     departure so the text is stable across renders. */
  const closest = (list) => list.reduce((a, b) => {
    const [ma, mb] = [a.early + a.late, b.early + b.late];
    return mb < ma || (mb === ma && b.d.start < a.d.start) ? b : a;
  });
  const where = (m) => `${m.d.dep}→${m.d.arr} on ${m.d.start}`;
  const parts = [`Parked in ${tail.arr} since ${tail.end}.`];

  const early = measured.filter((m) => m.early);
  if (early.length) {
    const m = closest(early);
    parts.push(`The soonest it could aim for is ${where(m)}, but driving empty `
      + `${tail.arr}→${m.d.dep} needs ${plural(drivingDays(tail.arr, m.d.dep), "clear day")} `
      + `and there ${freeDays(m.gap) === 1 ? "is" : "are"} only `
      + `${plural(freeDays(m.gap), "free day")} before it — short by `
      + `${plural(m.early, "day")}.`);
  }
  const late = measured.filter((m) => m.late);
  if (late.length) {
    const m = closest(late);
    parts.push(`${early.length ? "The next one it could reach is" : "The nearest it could reach is"} `
      + `${where(m)}, which would leave the bus standing `
      + `${plural(freeDays(m.gap), "free day")}`
      + (m.empty ? `, ${drivingDays(tail.arr, m.d.dep)} of them driving empty to `
        + `${m.d.dep}` : "")
      + ` — ${plural(m.late, "day")} past the ${maxRest()}-day rest limit.`);
  }
  /* "trips it could aim for", not "trips after it": with empty runs off,
     `measured` is the same-city subset, and the cross-city ones it drops are out
     of scope rather than out of the window. The phrasing is true either way. */
  parts.push("The tail never advances, so "
    + (measured.length === 1
      ? "the one uncovered trip it could aim for"
      : `every one of the ${measured.length} uncovered trips it could aim for`)
    + " only gets further out of the window, never closer.");
  /* Otherwise the sentences above read as "this bus is finished", which is a
     stronger claim than the tail alone supports. */
  if (reachable) {
    parts.push(`${plural(reachable, "uncovered trip")} could still be reached in time `
      + "from earlier gaps in its timeline, but each is ruled out by a company "
      + "constraint or clashes with what the unit is already booked for.");
  }

  /* The one-clause version the capacity note lists by name. Only the single
     nearest miss, whichever side it falls on: every entry in `measured` misses
     by now (any fit returned `booked` above), so this is the one that came
     closest, and the full both-sides account stays in `title`. */
  const near = closest(measured);
  const short = `in ${tail.arr} since ${shortDate(tail.end)}; nearest trip, `
    + `${near.d.dep}→${near.d.arr} on ${shortDate(near.d.start)}, is `
    + (near.early
      ? `${plural(near.early, "day")} too soon to drive there`
      : `${plural(near.late, "day")} past the ${maxRest()}-day rest limit`);

  return { state: "stranded", note: `idle since ${shortDate(tail.end)}`,
    short, title: parts.join(" ") };
}

/* What the current selection means, resolved against fresh state. */
function boardSelection() {
  const t = SEL && tripById(SEL);
  if (!t) return null;
  if (!t.assignment) {
    return { trip: t, role: "demand",
             candidates: S.engine.suggestions_by_trip[t.id] || [] };
  }
  // An ad hoc assignment has no unit key and generates no supply, so this
  // lookup simply misses — which is the correct answer, not a gap.
  const key = `${t.assignment.company}|${t.assignment.unit}`;
  return {
    trip: t, role: "supply",
    candidates: (S.engine.suggestions_by_unit[key] || [])
      .filter((s) => s.supply === t.id),
  };
}

function tripBar(t, row, laneIdx, isOverlap, ctx) {
  /* The status class goes on FIRST and unconditionally, in every section: it
     is what colours the bar (see .bar.confirmed/.pending/.canceled), and the
     classes pushed below only ever change texture, weight or gamma. Adding it
     in the else-branch instead — which is what this did while the uncovered
     and ad hoc lanes had hues of their own — meant a Confirmed trip was green
     on a unit row, orange in the uncovered lane and purple in ad hoc. */
  const cls = ["bar", t.status.toLowerCase()];
  let note = "";
  const link = t.assignment ? null : ctx.planned.get(t.id);
  if (t.status === "Canceled") { /* status class is the whole story */ }
  /* Checked before the row kind: a planned trip drawn on a unit row would
     otherwise pick up that row's committed styling and read as assigned. It is
     not — nothing is written until someone confirms it. */
  else if (link) {
    cls.push("planned");
    if (link.reposition) cls.push("reposition");
    note = ` · PLANNED for ${unitLabel(link.company, link.unit)} `
      + `(${plural(freeDays(link.gap_days), "free day")} after its supply)${link.reposition
        ? ` — needs an EMPTY RUN ${link.from_city}→${link.to_city} first`
        : ""} — provisional, not assigned, not exported`;
  } else if (row.kind === "uncovered") {
    cls.push("uncovered");
    // Three states, because "nobody can take this yet" and "nobody can ever
    // take this" call for completely different responses from the planner.
    // A locked bar skips them: it has no candidates BECAUSE it is locked,
    // and the LOCKED note appended below is the true state.
    if (isLocked(t.id)) {
      /* note stays empty */
    } else if (outOfScope(t.id)) {
      // Not put to the fleet at all, so the three states below do not apply
      // — least of all the last, which is what its missing candidate and
      // missing reachability would otherwise be read as. It shares that
      // state's look (faded: nothing here for the planner to do) and says
      // its own reason.
      cls.push("never");
      note = ` · ${SCOPE_NOTE[outOfScope(t.id)]}`;
    } else if (S.engine.suggestions_by_trip[t.id]) {
      note = " · has options now";
    } else if (ctx.reachable.has(t.id)) {
      cls.push("later");
      note = " · opens up once earlier links are accepted";
    } else {
      cls.push("never");
      // `reachable` is same-city only, so this says nothing about repositioning
      // — a trip here can still turn out to have an empty-run candidate, which
      // is why it is worded as the chain failing rather than as a dead end.
      note = " · no same-city chain reaches this — needs an empty run or an ad hoc bus";
    }
  } else if (row.kind === "adhoc") cls.push("adhoc");
  // A unit row needs nothing further: the status class already on the bar is
  // exactly what a committed trip has to say.
  /* After the state classes, not instead of them: a locked trip is still
     uncovered or still assigned, and hiding that behind the lock would make
     the board unable to say what locking actually cost. */
  if (isLocked(t.id)) {
    cls.push("locked");
    note += " · LOCKED — held out of all chaining until unlocked";
  }
  if (isOverlap) cls.push("overlap");
  if (ctx.sel) {
    if (t.id === ctx.sel.trip.id) cls.push("selected");
    else if (ctx.partners.has(t.id)) cls.push("partner");
    else cls.push("dim");
  }
  const bar = el("div", { class: cls.join(" "), title:
    `#${t.id.split(":")[1]} ${t.dep}→${t.arr} · ${t.start} → ${t.end}`
    + ` · ${t.status}${t.pax ? ` · ${t.pax} pax` : ""}${t.er ? " · ER" : ""}${note}` },
    `${t.dep}→${t.arr}`);
  bar.dataset.trip = t.id;
  placeSpan(bar, t, ctx, laneIdx * LANE_H + 2 + "px");
  return bar;
}

/* One geometry for anything spanning a trip's days — bars and ghost slots
   must agree on it exactly or a candidate slot sits beside the trip it
   would become. */
function placeSpan(node, t, ctx, top) {
  node.style.left = ctx.idx(t.start) * DAY_W + "px";
  node.style.width =
    Math.max((ctx.idx(t.end) - ctx.idx(t.start) + 1) * DAY_W - 2, 4) + "px";
  node.style.top = top;
}

/* Connectors for links someone actually accepted. Drawn supply -> demand
   wherever both ends sit on this row, rather than only between adjacent
   trips: an accepted link is still that link even if another trip later
   lands between its ends. */
function chainLinks(row, lane, ctx) {
  const byId = new Map(row.trips.map((t) => [t.id, t]));
  const out = [];
  for (const t of row.trips) {
    const a = byId.get(ctx.supplyOf.get(t.id));
    if (!a) continue;
    const gap = ctx.idx(t.start) - ctx.idx(a.end);
    /* Re-checked against today's dates, not the dates at acceptance time. An
       edit since then can leave the link recorded but no longer chainable,
       and that mismatch is exactly what the planner needs to see.

       Cities differing is the one case that is NOT such a mismatch: only an
       accept puts a link here, and accepting across two cities is precisely
       what approving an empty run means (checks.py reaches the same verdict —
       an accepted cross-city pair is `reposition`, not `city_mismatch`). This
       reads it off the link itself rather than looking the pair up in the
       integrity list, which only examines CONSECUTIVE trips: an approved empty
       run with another trip landing between its ends is absent from that list
       and would fall through to broken. */
    const empty = a.arr !== t.dep;
    /* The PAIR window, not just the same-city one: an approved empty run is
       exempt from the CITY rule, not from rule 1 — dates drifting after the
       accept must surface here exactly like they do for same-city links. */
    const lo = minGapDays(a.arr, t.dep);
    const ok = gap >= lo && gap <= lo + maxRest();
    /* The label counts the blank columns it sits on, not the arrival-to-
       departure gap the engine ranks by — see freeDays. */
    const free = freeDays(gap);
    const x1 = (ctx.idx(a.end) + 1) * DAY_W;
    const x2 = ctx.idx(t.start) * DAY_W;
    const c = el("div", {
      class: "connector" + (ok ? (empty ? " reposition" : "") : " broken"),
      title: ok && empty
        ? `empty run ${a.arr}→${t.dep} · ${plural(free, "free day")} between these `
          + `two trips, ${drivingDays(a.arr, t.dep)} of them driving the bus over `
          + "empty. Approved when this link was accepted."
        : ok
        ? `chained · ${plural(free, "free day")} (arrives ${a.end}, departs ${t.start})`
        : `link no longer chains: ${plural(free, "free day")}`
          + (empty
            ? ` — outside the ${a.arr}→${t.dep} window (${lo}–${lo + maxRest()})`
            : ` (arrives ${a.end}, departs ${t.start})`) });
    c.style.left = x1 - 2 + "px";
    c.style.width = Math.max(x2 - x1 + 2, 6) + "px";
    c.style.top = lane.get(a.id) * LANE_H + LANE_H / 2 + "px";
    // An empty run costs money whether or not the next trip earns any, so it
    // is named on the connector, not left to a hover — the same rule the
    // candidate rows follow. It needs the room for the extra glyph.
    if (empty && x2 - x1 >= 40) {
      c.append(el("span", { class: "gap-label er-label" }, `${free}d ⇢`));
    } else if (x2 - x1 >= 26) {
      c.append(el("span", { class: "gap-label" }, `${free}d`));
    }
    out.push(c);
  }
  return out;
}

/* The candidate slot: where the demand trip would sit if this link were
   accepted. Feasibility already guarantees the unit is free on those days,
   so lane 0 is safe to draw into. */
function ghostSlot(s, ctx) {
  const d = tripById(s.demand);
  if (!d) return null;               // engine and state disagree: draw nothing
  const free = freeDays(s.gap_days);
  const g = el("div", { class: "ghost" + (s.reposition ? " reposition" : ""), title:
    `Accept — ${unitLabel(s.company, s.unit)} takes ${tripLabel(s.demand)}`
    + ` (${plural(free, "free day")} beforehand)`
    + (s.reposition ? `. Requires an empty repositioning run ${s.from_city}→`
      + `${s.to_city} first — the bus is not where this trip departs from, and `
      + `${drivingDays(s.from_city, s.to_city)} of those days go to the drive.` : ""),
    onclick: () => acceptSuggestion(s) },
    `+ ${d.dep}→${d.arr} · ${free}d${s.reposition ? " · ER" : ""}`);
  placeSpan(g, d, ctx, "2px");
  return g;
}

/* The selected trip's days, drawn as a full-height column band on every row and
   on the axis. A bar tells you WHEN a trip runs only if you can trace it down to
   the date header; across fifteen rows and a horizontal scroll, nobody can. The
   band answers "what else is happening in this window" — which bus is free then,
   which is mid-trip — by lining the whole board up against one selection.

   A fresh node per canvas rather than one overlay, because each row canvas is
   its own positioning context (the labels are sticky, so the rows cannot share
   a parent that spans them). */
const spanBand = (ctx) => {
  const band = el("div", { class: "span-band" });
  band.style.left = ctx.span.x0 + "px";
  band.style.width = ctx.span.w + "px";
  return band;
};

function boardRow(r, ctx) {
  const { lane, overlap, lanes } = packLanes(r.trips, r.kind === "unit");
  const canvas = el("div", { class: "row-canvas" });
  canvas.style.width = ctx.width + "px";
  // At least one lane: an empty bus row is a drop target and must be tall
  // enough to aim at.
  canvas.style.height = Math.max(lanes, 1) * LANE_H + 4 + "px";

  // First, so everything else paints over it: the band is context, and it must
  // never sit on top of a bar or hide the today line.
  if (ctx.span) canvas.append(spanBand(ctx));
  if (ctx.todayIdx != null) {
    const line = el("div", { class: "today-line" });
    line.style.left = ctx.todayIdx * DAY_W + "px";
    canvas.append(line);
  }
  for (const t of r.trips) {
    canvas.append(tripBar(t, r, lane.get(t.id), overlap.has(t.id), ctx));
  }
  if (r.kind === "unit") canvas.append(...chainLinks(r, lane, ctx));
  const ghostSuggestion = ctx.ghosts.get(r.key);
  const ghost = ghostSuggestion && ghostSlot(ghostSuggestion, ctx);
  if (ghost) canvas.append(ghost);

  /* The blank space after a bus's last bar is where a planner actually looks
     when asking "so what now?", and it was the one place on the board with
     nothing to click: a row that simply stops says nothing about whether the
     board ran out of work or the bus ran out of options. This is the click
     target for that question. Suppressed on `done` rows, where the honest
     answer is that there is no question — nothing starts after that tail.

     Anchored on the last live bar rather than the tail so it cannot land under
     a planned bar drawn on its bus; the panel it opens is still the tail's, which
     is the only place a chain extends from. */
  const live = r.trips.filter((t) => t.status !== "Canceled");
  const tailId = r.kind === "unit" ? unitTailId(r.key) : null;
  if (tailId && !ghost && live.length && r.state !== "done") {
    const last = live.reduce((a, b) => (b.end > a.end ? b : a));
    const stub = el("div", { class: "next-stub", title:
      `What can ${r.label} take next? Its chain extends from its last trip only.` },
      "+ next?");
    stub.dataset.trip = tailId;
    stub.style.left = (ctx.idx(last.end) + 1) * DAY_W + 3 + "px";
    stub.style.top = lane.get(last.id) * LANE_H + 3 + "px";
    canvas.append(stub);
  }

  const label = el("div", { class: "row-label " + (r.state || ""),
    title: r.title || r.label }, r.label, el("small", {}, ` ${r.note || r.trips.length}`));
  // The second way in, and the one people find first: the note that says
  // "idle since Sep" is right here, so this is where they click to ask why.
  if (tailId) label.dataset.trip = tailId;

  const row = el("div", { class: "board-row " + r.kind }, label, canvas);
  // What a drop onto this row means. The key is the engine's own — 'company|unit'
  // for a bus, 'adhoc|company' for a lane — so the drop-targets reply can be
  // matched against rows without the client re-deriving either naming.
  row.dataset.rowKey = r.key;
  row.dataset.rowKind = r.kind;
  row.dataset.rowLabel = r.label;
  return row;
}

function boardAxis(t0, days, ctx) {
  const canvas = el("div", { class: "axis-canvas" });
  canvas.style.width = ctx.width + "px";
  // Before the month strip and the day numbers, which have to stay readable
  // through it — the point of banding the axis is to name the dates, so the
  // band must not be what obscures them.
  if (ctx.span) canvas.append(spanBand(ctx));
  const base = Date.parse(t0);
  const dayAt = (i) => new Date(base + i * DAY_MS);

  let seg = null;
  for (let i = 0; i < days; i++) {
    const d = dayAt(i);
    const key = d.getUTCFullYear() * 12 + d.getUTCMonth();
    if (!seg || seg.key !== key) {
      const node = el("div", { class: "axis-month" },
        `${MONTHS[d.getUTCMonth()]} ${d.getUTCFullYear()}`);
      node.style.left = i * DAY_W + "px";
      canvas.append(node);
      seg = { key, node, len: 0 };
    }
    seg.len += 1;
    seg.node.style.width = seg.len * DAY_W + "px";
  }

  // Day numbers thin out with the zoom: below ~18px per day they collide into
  // an unreadable smear, and the month strip carries the axis instead.
  const step = DAY_W >= 18 ? 1 : DAY_W >= 12 ? 3 : 7;
  for (let i = 0; i < days; i += step) {
    const tick = el("div", { class: "axis-day" }, String(dayAt(i).getUTCDate()));
    tick.style.left = i * DAY_W + "px";
    tick.style.width = step * DAY_W + "px";
    canvas.append(tick);
  }
  return el("div", { class: "board-axis" },
    el("div", { class: "row-label" }), canvas);
}

const SECTION_LABEL = { unit: "Units", adhoc: "Ad hoc (chain-invisible)" };

function sectionHeader(kind, rows) {
  const collapsed = COLLAPSED.has(kind);
  const n = rows.filter((r) => r.kind === kind).length;
  const head = el("div", { class: "board-row board-section",
    onclick: (e) => {
      // Folding a section is not a selection change — without this the
      // click bubbles into #board's "empty canvas clears" handler and
      // closes the detail panel too.
      e.stopPropagation();
      if (collapsed) COLLAPSED.delete(kind); else COLLAPSED.add(kind);
      renderBoard();
    } },
    el("div", { class: "row-label" },
      collapsed ? "▸ " : "▾ ", SECTION_LABEL[kind], el("small", {}, ` ${n}`)),
    el("div", {}));
  return head;
}

/* There was a span warning here, for the case of a typo'd year stretching the
   axis over years and squeezing every real month into a few pixels. It is gone
   because the contract now refuses those rows outright (MAX_TRIP_SPAN_DAYS in
   contract.py), so they never reach the board to stretch anything — and one
   home for the rule beats two that can drift apart. Worth knowing if you are
   tempted to re-add it: the version that stood here for a long time triggered
   on the board's TOTAL extent exceeding 1100 days, and the reference data's 76
   typo'd-year rows only stretch that to 576, so it had never once fired on the
   data it was written for. A per-row test is the one that works. */

/* Chaining only moves buses you already have. When simultaneous uncovered
   demand exceeds the entire registered fleet, no sequence of accepts can close
   the gap — saying so here is what stops a wall of orange bars reading as "the
   engine is not doing its job". */
function renderCapacityNote(rows) {
  const box = $("#board-capacity");
  box.replaceChildren();
  const fleet = (S.registry.companies || [])
    .reduce((n, c) => n + (c.fleet_size || 0), 0);
  const uncovered = uncoveredTrips();
  if (!fleet || !uncovered.length) { box.classList.add("hidden"); return; }

  const perDay = new Map();
  for (const t of uncovered) {
    const last = Date.parse(t.end) / DAY_MS;
    for (let d = Date.parse(t.start) / DAY_MS; d <= last; d++) {
      perDay.set(d, (perDay.get(d) || 0) + 1);
    }
  }
  let peak = 0, peakDay = null;
  for (const [d, n] of perDay) if (n > peak) { peak = n; peakDay = d; }
  if (peak <= fleet) { box.classList.add("hidden"); return; }

  const day = new Date(peakDay * DAY_MS).toISOString().slice(0, 10);
  box.classList.remove("hidden");
  // "At least" because the fleet is not idle that day — this is the floor.
  box.append(el("span", {}, `Peak ${day}: ${peak} uncovered trips run at once, against a `
    + `registered fleet of ${fleet} buses. Chaining moves buses between trips — it cannot `
    + `close a gap this size, so at least ${peak - fleet} of that day's trips need `
    + "capacity beyond the registered fleet."));

  /* Left alone, the sentence above reads as "buy more buses" while some of the
     buses already chartered have dropped out of the model entirely. Both facts
     are true and a planner needs them together: stranded units are the part
     that is recoverable by scheduling rather than by capacity. Read off the
     board rows, whose labels carry the same diagnosis — unitIdleReason used to
     run a second time here, doubling the board's dominant cost.

     Named, not counted. A bare "1 unit is stranded" sent people hunting the
     board for which row, and its old wording ("parked somewhere no uncovered
     trip departs from") was the pre-empty-run rule that unitIdleReason's own
     comment calls the wrong explanation. Each name hovers to the row label's
     full diagnosis and clicks through to the tail's panel, which opens just
     below this banner. */
  const stranded = rows.filter((r) => r.kind === "unit" && r.state === "stranded");
  if (!stranded.length) return;
  const one = stranded.length === 1;
  const note = el("span", { class: "stranded-note" }, " Separately, "
    + (one ? "" : `${stranded.length} units of the fleet are stranded, so no accept `
      + "in any order can put them back to work. That capacity is idle rather "
      + "than missing: "));
  stranded.slice(0, 12).forEach((r, i) => {
    if (i) note.append(", ");
    note.append(el("button", { class: "linklike", title: r.title,
      onclick: () => selectOnBoard(unitTailId(r.key)) }, r.label), ` (${r.short})`);
  });
  if (stranded.length > 12) note.append(`, and ${stranded.length - 12} more`);
  note.append(one
    ? " is stranded. No accept in any order can put it back to work, so that "
      + "capacity is idle rather than missing. Click the name to see what it could "
      + "take next."
    : ". Click a name to see what it could take next.");
  box.append(note);
}

/* The buses the plan did not pick. Two cases that read alike and mean very
   different things, so they are never worded the same:

   - The plan's bus is itself among the candidates (a chain's first hop, whose
     supply is already assigned). Swapping is a like-for-like choice.
   - It is not (any later hop, fed by a trip nobody has accepted yet). Those
     candidates would take the trip OUT of the chain and hand it to a bus that
     can serve it today — a different decision, not a substitution.

   Either way nothing is destroyed by choosing: the downstream links were only
   ever proposals, and the plan re-derives from whatever is committed. */
function plannedAlternatives(t, link) {
  const others = otherUnitsFor(t.id, link.company, link.unit);
  const mine = (S.engine.suggestions_by_trip[t.id] || []).find(
    (s) => s.kind === "chain" && s.company === link.company
      && String(s.unit) === String(link.unit));
  const out = [];

  if (!others.length) {
    out.push(el("p", { class: "hint" }, mine
      ? "No other bus can take this trip — this is the only unit that reaches "
        + "it in time."
      : "No other bus can take this trip today. The chain above is the only "
        + "route to it, so the earlier links have to be accepted first."));
    return out;
  }

  out.push(el("p", { class: "hint" }, mine
    ? `${unitLabel(link.company, link.unit)} is the plan's choice. `
      + `${others.length === 1 ? "One other bus" : `${others.length} other buses`} `
      + "could take this trip instead:"
    : `The plan gives this to ${unitLabel(link.company, link.unit)} as step `
      + `${link.step + 1} of its chain. `
      + `${others.length === 1 ? "One bus" : `${others.length} buses`} could take `
      + "it now instead, outside that chain:"));

  if (mine) out.push(candidateRow(mine, { chosen: true }));
  out.push(...others.map((s) => candidateRow(s)));
  out.push(el("p", { class: "hint" },
    "Accepting any of these writes that assignment and replans everything else "
    + "around it — the chain above will change. Nothing is written until you do."));
  return out;
}

/* Assign a trip to any bus, ignoring the planner.

   Every other accept control on this board is engine-gated: it renders a
   candidate the engine proposed. So when the planner has no feasible link
   there is nothing to click anywhere on the screen — which is precisely when
   someone needs to place a bus by hand. The store has always allowed this
   (repo.accept takes supply=None, the same provenance an ad hoc fallback
   records); only the UI had no way to ask for it.

   Deliberately NOT filtered to units that could legally take the trip. The
   escape hatch is the point, and the rules still apply afterwards: the value
   is re-validated by the contract, and any overlap or city mismatch it creates
   is reported by the integrity check. Better a human sees it there than is
   quietly prevented from expressing it here. */
function directAssign(t) {
  /* Split into two groups, numbered units first. Most companies in a seeded
     registry have no units at all, so interleaving them by name buried the
     eight actual buses under two dozen ad hoc entries — and the two are not
     the same kind of choice anyway: a numbered unit joins the chain model, a
     company-level assignment is chain-invisible and generates no supply.
     Values are `key|unit` rather than the display names companyValues offers,
     because this picker posts straight to /accept and the API wants the key. */
  const companies = sortedCompanies();
  const units = companies.flatMap((c) =>
    c.units.map((u) => option(`${c.key}|${u}`, `${c.name} ${u}`, false)));
  const adhoc = companies.map((c) => option(`${c.key}|`, c.name, false));
  if (!units.length && !adhoc.length) {
    return el("p", { class: "hint" },
      "No companies in the registry yet, so there is nothing to assign to — "
      + "seed or add one on the Registry tab.");
  }

  const picker = el("select", {}, option("", "— pick a bus —", true),
    units.length
      ? el("optgroup", { label: "Buses" }, ...units)
      : "",
    el("optgroup", { label: "Ad hoc — company only, no unit (never chains)" },
      ...adhoc));
  const assign = async () => {
    if (!picker.value) { toast("Pick a bus first", true); return; }
    const [company, unit] = picker.value.split("|");
    // mutate clears SEL: the trip is covered, so its panel is spent.
    await mutate("/accept", { demand: t.id, company,
                              unit: unit === "" ? null : Number(unit),
                              supply: null }, "Assigned");
  };

  return el("div", { class: "direct-assign" },
    el("p", { class: "hint" }, "Assign by hand, outside the plan:"),
    el("div", { class: "cand" }, picker,
      el("button", { class: "btn small", onclick: assign }, "Assign")),
    el("p", { class: "hint" },
      "Written immediately and exported. No chain link is recorded, so the board "
      + "draws no connector to a previous trip, and anything this breaks shows up "
      + "in the integrity report rather than being refused here. Undo reverses it."));
}

/* ---------- the other direction: which trip for this bus ---------- */

/* directAssign answers "which bus for this trip". This answers "which trip for
   this bus", and until now nothing did: selecting a bus whose chain the engine
   cannot extend produced one sentence and no control anywhere on screen, so the
   only way to place that bus was to type a company name into the Data tab. The
   trip->bus direction got an escape hatch and the bus->trip direction never
   did; that asymmetry is the whole of this feature.

   Everything here is a manual override by definition — the engine already
   offered whatever it could — so every row states which rule it breaks, in the
   BUTTON rather than a tooltip. A pre-ranked list of one-click buttons is far
   less friction than directAssign's dropdown, and the cost has to travel with
   the click. */

// "Show all" for the list below. Reset whenever the selection changes: it is a
// property of looking at one bus, not a preference.
let NEXT_ALL = false;
const NEXT_LIMIT = 10;

async function assignAnyway(d, company, unit) {
  const res = await mutate(
    "/accept", { demand: d.id, company, unit: Number(unit), supply: null },
    `Assigned ${tripLabel(d.id)} to ${unitLabel(company, unit)}`,
    { keepSel: true });
  /* Land on the trip just assigned rather than clearing the selection the way
     every other accept does. The bus's tail has moved, and this panel is now
     showing what follows the NEW tail — which is the loop this feature exists
     to close: click, assign, keep going, without leaving the board. */
  if (res) selectOnBoard(d.id);
}

/* One option, with every rule it breaks named twice: as a pill for scanning and
   inside the button for the moment of clicking. A trip can break both at once —
   wrong week AND wrong company — so the pills are a list, and the button names
   the schedule miss when there is one, since that is the number a planner
   negotiates over. */
function nextTripRow(m, tail, company, unit) {
  const d = m.d;
  const free = freeDays(m.gap);
  const pills = [];
  let btn;

  /* An early miss always involves a drive: same-city needs only gap >= 1 and
     every trip here starts after the tail ends, so the same-city case cannot be
     early. The generic clause is the fallback if that floor ever moves. */
  if (m.early) {
    pills.push(`${plural(m.early, "day")} too soon — `
      + (m.empty
        ? `driving empty ${tail.arr}→${d.dep} needs `
          + `${plural(drivingDays(tail.arr, d.dep), "clear day")}, and there `
          + `${free === 1 ? "is" : "are"} only ${plural(free, "free day")} before it`
        : `this bus needs ${plural(minGapDays(tail.arr, d.dep), "day")} `
          + "between landing and departing again"));
    btn = `${m.early}d too soon`;
  } else if (m.late) {
    pills.push(`${plural(m.late, "day")} past the ${maxRest()}-day rest limit`);
    btn = `${m.late}d past rest`;
  }
  /* With empty runs off, the drive itself is why the planner passed. Without
     this a cross-city trip whose dates fit perfectly reaches the button with no
     pill and no label at all — it is only ever listed here because the setting
     excluded it, and the row has to say so. */
  if (m.empty && !repositionAllowed()) {
    pills.push(`empty runs are off, and this departs from ${d.dep}, not ${tail.arr}`);
    btn = btn || "needs an empty run";
  }
  if (m.blocked) {
    pills.push(d.yn === "Y"
      ? `only ${companyName(d.constraint_company)} may serve this trip`
      : `${companyName(d.constraint_company)} is excluded from this trip`);
    btn = btn ? `${btn}, wrong company` : "against constraint";
  }

  return el("div", { class: "cand next" },
    el("span", {}, `${d.dep}→${d.arr}`),
    statusBadge(d.status),
    el("span", { class: "gap" },
      `${shortDate(d.start)} · ${plural(free, "free day")} after this bus lands`),
    /* Not erBadge: that one promises rule 6 will record the pair as an approved
       ER, which is true of an accepted link and false of everything here — a
       hand assignment records no link, so the same drive is reported as a
       city_mismatch instead. */
    m.empty
      ? el("span", { class: "badge er", title:
          `The bus finishes in ${tail.arr}. Someone has to drive it empty to `
          + `${d.dep} before this trip can start — `
          + `${plural(drivingDays(tail.arr, d.dep), "day")} of the drive, unpaid.` },
          `empty run ${tail.arr}→${d.dep}`)
      : "",
    ...pills.map((p) => el("span", { class: "risk", title:
      "The planner will not propose this link. Assigning it anyway is allowed, "
      + "recorded, undoable, and reported." }, p)),
    el("button", { class: "btn small",
      onclick: () => assignAnyway(d, company, unit) },
      `Assign anyway · ${btn}`));
}

function nextTrips(unitKey, selected) {
  const [company, unit] = unitKey.split("|");
  const committed = (S.engine.timelines[unitKey] || []).map(tripById).filter(Boolean);
  if (!committed.length) return null;

  // Locked trips excluded for the same reason as unitIdleReason: they carry
  // no rule miss, so a row for one would render a pill-less "Assign anyway".
  const { tail, measured } = tailOptions(
    committed, uncoveredTrips().filter((t) => !isLocked(t.id)));
  const box = el("div", { class: "next-trips" });
  const who = unitLabel(company, unit);

  if (!measured.length) {
    box.append(el("p", { class: "hint" },
      `Nothing left for ${who} to take: it finishes in ${tail.arr} on ${tail.end}, `
      + "and no uncovered trip starts after that."));
    return box;
  }

  /* Engine-legal links keep their own Accept button, because accepting one
     records the chain link — provenance that "assign anyway" deliberately drops.

     Restricted to the tail so nothing below can contradict the engine. The
     unit's other suggestions are mid-chain ones, and those can only ever target
     a demand that starts BEFORE the tail ends (_unit_timeline_ok refuses any
     link with an existing trip sitting in its gap, and the tail is exactly such
     a trip), so they cannot collide with `measured` — which is a claim about
     what follows the tail. Without that, a trip the engine can legally chain
     elsewhere on this same bus would be listed here as a rule break. */
  const offered = new Map(
    (S.engine.suggestions_by_unit[unitKey] || [])
      .filter((s) => s.supply === tail.id)
      .map((s) => [s.demand, s]));

  const scored = measured
    .filter((m) => !offered.has(m.d.id))
    .map((m) => ({ ...m, miss: m.early + m.late,
                   blocked: constraintBlocks(m.d, company) }));

  /* Two lists, not one ranking. A trip whose dates fit perfectly but whose
     customer said "only company X" is not comparable to one that misses the
     rest limit by five days: blending them means inventing an exchange rate
     between a commercial promise and a driver's day off, and whatever rate I
     picked would be wrong for someone. It also buries the interesting case —
     ranking every breach below every stretch pushed the perfect-date trips past
     the cap entirely, where nobody would ever see them.

     Apart, each list is ranked by the one thing its own members vary by. */
  const byDate = (a, b) =>
    a.d.start.localeCompare(b.d.start) || a.d.id.localeCompare(b.d.id);
  const refused = scored.filter((m) => m.blocked && !m.miss).sort(byDate);
  const misses = scored.filter((m) => !(m.blocked && !m.miss))
    .sort((a, b) => a.miss - b.miss || byDate(a, b));

  const anchored = selected.id === tail.id;
  box.append(el("p", { class: "hint" }, anchored
    ? `What ${who} could take next — nothing below is a planner suggestion:`
    // tripLabel already carries the cities and both dates — naming them again
    // here read as two different trips.
    : `${who} can only be extended from its last trip, ${tripLabel(tail.id)}. `
      + "What it could take after that:"));

  // Only when the selection is mid-chain: at the tail, detailBody has already
  // rendered these above as the selection's own candidates.
  if (!anchored && offered.size) {
    box.append(...[...offered.values()].map(
      (s) => candidateRow(s, { showDemand: true })));
  }

  const cap = (list) => (NEXT_ALL ? list : list.slice(0, NEXT_LIMIT));
  const more = (list, what) => list.length > NEXT_LIMIT
    ? el("p", { class: "hint" },
        `${cap(list).length} of ${list.length} ${what}. `,
        el("button", { class: "linklike",
          onclick: () => { NEXT_ALL = !NEXT_ALL; renderBoard(); } },
          NEXT_ALL ? `Show the closest ${NEXT_LIMIT}` : `Show all ${list.length}`))
    : "";

  if (refused.length) {
    box.append(el("p", { class: "hint sub" },
      `The dates work — it is the company constraint that refuses `
      + `${refused.length === 1 ? "this one" : "these"}:`));
    box.append(...cap(refused).map((m) => nextTripRow(m, tail, company, unit)));
    box.append(more(refused, "constrained trips this bus could otherwise run"));
  }
  if (misses.length) {
    if (refused.length) {
      box.append(el("p", { class: "hint sub" }, "Nearest on the calendar:"));
    }
    box.append(...cap(misses).map((m) => nextTripRow(m, tail, company, unit)));
    box.append(more(misses, `uncovered trips after ${shortDate(tail.end)}, closest first`));
  }

  box.append(el("p", { class: "hint" },
    "Written immediately and exported. No chain link is recorded, so the board draws "
    + "no connector and the pair is listed by the integrity check — as a gap out of "
    + "window, or as a city mismatch if the bus had to be driven. That is the "
    + "override being visible rather than refused. Undo reverses it."));
  return box;
}

function renderBoardDetail(sel) {
  const box = $("#board-detail");
  box.replaceChildren();
  box.classList.toggle("hidden", !sel);
  if (!sel) return;
  const t = sel.trip;
  box.append(el("div", { class: "detail-head" },
    el("strong", {}, tripLabel(t.id)), " ", statusBadge(t.status),
    t.assignment
      ? el("span", { class: "hint" },
          ` · ${unitLabel(t.assignment.company, t.assignment.unit)}`)
      : "",
    // In the head rather than beside the explanations below: it applies to the
    // selected trip whatever the panel goes on to say about it, and both roles
    // (demand and supply) reach it.
    el("button", { class: "btn small", title:
      "Open this trip's row in the Data tab, where its cells can be edited. "
      + "Shortcut: d, while a bar is selected.",
      onclick: () => jumpToDataRow(t.id) }, "Show in Data"),
    /* The only place a lock is set or cleared, and it is in the head for the
       same reason "Show in Data" is: it applies to the selected trip whatever
       the panel goes on to say, and both roles reach it. A locked trip has no
       candidates, so anywhere further down would be inside a branch that never
       renders for one — the control would exist and be unreachable. */
    el("button", { class: "btn small" + (isLocked(t.id) ? " active" : ""), title:
      isLocked(t.id)
        ? "Locked: the planner leaves this trip alone and a re-chain will not "
          + "release it. Click to unlock."
        : "Lock: hold this trip out of all chaining. Its assignment (if any) "
          + "stays put through a re-chain, and it draws no suggestions.",
      onclick: () => toggleLock(t.id) },
      isLocked(t.id) ? "🔒 Locked" : "Lock"),
    el("button", { class: "btn small",
      onclick: () => { SEL = null; renderBoard(); } }, "×")));

  /* The explanation branches below all end in a return, so the manual control
     is appended once here rather than repeated in each. One hatch per
     direction: a trip that has no bus gets the bus picker, a bus gets the list
     of trips it could still take. `role === "supply"` must stay out of
     directAssign — that trip already has a bus. */
  detailBody(box, sel, t);
  if (sel.role === "demand") box.append(directAssign(t));
  // Numbered units only: an ad hoc assignment is chain-invisible, so it has no
  // timeline and therefore no tail to extend from.
  else if (t.assignment.unit != null) {
    const next = nextTrips(`${t.assignment.company}|${t.assignment.unit}`, t);
    if (next) box.append(next);
  }
}

function detailBody(box, sel, t) {
  /* A planned trip explains itself before anything else: the noChainReason
     path below would otherwise tell someone "no unit can take this" about a
     trip the plan has already routed onto a bus. */
  const link = t.assignment ? null : plannedLinks().get(t.id);
  if (link) {
    const chain = S.engine.plan.chains.find(
      (c) => c.company === link.company && c.unit === link.unit);
    box.append(el("p", { class: "hint" },
      `Planned for ${unitLabel(link.company, link.unit)} after `
      + `${tripLabel(link.supply)} (${plural(freeDays(link.gap_days), "free day")} `
      + `in between), step ${link.step + 1} `
      + `of ${chain ? chain.links.length : 1}. Provisional: nothing is written `
      + `and nothing is exported until you accept it.`));
    if (chain) box.append(chainRow(chain, chain.links));
    box.append(...plannedAlternatives(t, link));
    return;
  }

  if (!sel.candidates.length) {
    if (sel.role === "demand" && isLocked(t.id)) {
      // A locked trip is dropped from the server's demand set, so it HAS no
      // candidates by design — running noChainReason on it would fabricate
      // a rule-based explanation for a decision the lock made.
      box.append(el("p", { class: "hint" },
        "Locked — held out of all chaining, so it has no candidates by "
        + "design. Unlock it to see them."));
      return;
    }
    const scope = sel.role === "demand" ? outOfScope(t.id) : undefined;
    if (scope) {
      // Same shape as the lock above: no candidates because the trip was
      // never put to the fleet, so noChainReason — which assumes the engine
      // looked and found nothing — would invent a rule that refused it, and
      // the verdict under it would send someone to hire a bus. The bus picker
      // is appended after this returns (renderBoardDetail), so the trip can
      // still be given one by hand.
      box.append(el("p", { class: "hint" }, scope === "started"
        ? `Already started (on or before ${S.today}), so the planner leaves it `
          + "alone and offers no candidates. If a bus did run it, assign that "
          + "bus below: it records the trip and tells the planner where the "
          + "bus is now."
        : `Outside the plan-ahead window (after ${S.horizon_through}), so it is `
          + "not being planned yet and has no candidates. Widen the window to "
          + "plan it, or assign a bus below."));
      return;
    }
    if (sel.role !== "demand") {
      /* Deliberately narrower than it used to read ("no uncovered trip can
         follow this one"), which was a verdict where only a fact was true: the
         planner has no legal link, and the list appended below this panel names
         what it would take to make one anyway. */
      box.append(el("p", { class: "hint" },
        "The planner has no chainable link off this trip."));
      return;
    }
    box.append(el("p", { class: "hint" }, noChainReason(t)));
    // Whether it is merely early or genuinely out of reach changes what you
    // do next, so say which — the reason above only explains today.
    box.append(reachableSet().has(t.id)
      ? el("p", { class: "hint" }, "A chain could still reach this trip once "
          + "earlier links are accepted — it is waiting on those, not blocked.")
      : el("p", { class: "reason" }, "No sequence of accepts can reach this trip. "
          + "Covering it needs an ad hoc bus or a subcontractor."));
    return;
  }
  // candidateRow is the Suggestions tab's own renderer, reused whole: it
  // already carries the at-risk warnings, the ad hoc fallback (which has no
  // unit row to sit on) and the Accept button.
  box.append(...sel.candidates.map(
    (s) => candidateRow(s, { showDemand: sel.role === "supply" })));
}

function renderBoard(rows = null) {
  const box = $("#board");
  /* While the Timelines tab is hidden, the board (thousands of positioned
     nodes) is the most expensive render in the app and nobody can see it —
     mark it stale and build it when the tab opens (showTab). Everything else
     on the tab still renders: it is cheap and keeps the panel counts fresh. */
  if (!box.offsetParent) { BOARD_STALE = true; return; }
  BOARD_STALE = false;
  // Read before the rebuild, since replaceChildren resets scrollLeft to zero —
  // and only while the tab is on screen: a hidden element reports 0, which
  // would silently rewrite the user's position every time another tab
  // triggered a refresh.
  if (BOARD_RETARGET) BOARD_RETARGET = false;
  else if (box.firstChild) BOARD_DAY = box.scrollLeft / DAY_W;
  box.replaceChildren();
  rows = rows || boardRows();
  const all = rows.flatMap((r) => r.trips);
  const sel = boardSelection();
  renderBoardDetail(sel);
  if (!all.length) {
    box.append(el("p", { class: "hint" }, "No trips to display."));
    return;
  }

  const t0 = all.reduce((m, t) => (t.start < m ? t.start : m), all[0].start);
  const t1 = all.reduce((m, t) => (t.end > m ? t.end : m), all[0].end);
  const idx = (iso) => Math.round((Date.parse(iso) - Date.parse(t0)) / DAY_MS);
  const days = idx(t1) + 1;
  box.style.setProperty("--day-w", DAY_W + "px");

  // Where the selection's candidates would land. Both directions reduce to
  // the same rule — the demand trip is what moves onto a unit — so one map
  // keyed by unit row serves whichever way the selection is read.
  const ghosts = new Map();
  const partners = new Set();
  for (const s of sel ? sel.candidates : []) {
    if (s.kind !== "chain") continue;      // ad hoc has no unit row to sit on
    partners.add(sel.role === "demand" ? s.supply : s.demand);
    const key = `${s.company}|${s.unit}`;
    // One row can hold only one slot; the engine's own ranking picks it.
    const prev = ghosts.get(key);
    if (!prev || s.rank < prev.rank) ghosts.set(key, s);
  }

  const ctx = {
    idx, width: days * DAY_W, sel, ghosts, partners, reachable: reachableSet(),
    planned: plannedLinks(),
    supplyOf: new Map(S.accepted_links.map((l) => [l.demand, l.supply])),
    todayIdx: todayISO >= t0 && todayISO <= t1 ? idx(todayISO) : null,
    // Inclusive of the end day: a bus is occupied on the day it arrives, which
    // is the same reading packLanes and checks.py give an interval.
    span: sel ? { x0: idx(sel.trip.start) * DAY_W,
                  w: (idx(sel.trip.end) - idx(sel.trip.start) + 1) * DAY_W } : null,
  };

  box.append(boardAxis(t0, days, ctx));
  let section = null;
  for (const r of rows) {
    if (SECTION_LABEL[r.kind] && r.kind !== section) {
      section = r.kind;
      box.append(sectionHeader(r.kind, rows));
    }
    if (!COLLAPSED.has(r.kind)) box.append(boardRow(r, ctx));
  }

  if (BOARD_FOCUS) {
    const t = tripById(BOARD_FOCUS);
    if (t) BOARD_DAY = Math.max(idx(t.start) - 3, 0);
    BOARD_FOCUS = null;
  }
  // A dataset can open months before anything happens — landing on empty grid
  // reads as a broken board. Open on today when it is in range, then leave the
  // scroll position to the user.
  if (BOARD_DAY === null) BOARD_DAY = Math.max((ctx.todayIdx ?? 0) - 3, 0);
  applyBoardScroll();
}

/* Writing scrollLeft on a display:none element is dropped, so the position is
   also (re)applied when the tab becomes visible. */
function applyBoardScroll() {
  const box = $("#board");
  if (BOARD_DAY !== null && box.offsetParent) box.scrollLeft = BOARD_DAY * DAY_W;
}

function boardGoToday() {
  BOARD_DAY = null;                 // recomputed from today's index on render
  BOARD_RETARGET = true;
  renderBoard();
}

function renderTimelines() {
  // One boardRows pass feeds both: the rows carry each unit's idle diagnosis,
  // which the capacity note used to recompute per unit.
  const rows = boardRows();
  renderCapacityNote(rows);
  renderBoard(rows);

  const fleet = $("#fleet-warnings");
  fleet.replaceChildren();
  if (!S.engine.fleet_warnings.length) fleet.append(el("p", { class: "hint" }, "None."));
  // One summary line per company (the raw list can run to hundreds of days).
  // Only numbered-unit trips count here: the fleet number is the chartered
  // vehicle count, and an ad hoc assignment is a bus beyond that charter. An
  // empty panel is the normal state — a company can only exceed its own unit
  // count by double-booking one, which the integrity list reports as well.
  const byCompany = new Map();
  for (const w of S.engine.fleet_warnings) {
    const g = byCompany.get(w.company) || { days: 0, worst: w, fleet_size: w.fleet_size };
    g.days += 1;
    if (w.count > g.worst.count) g.worst = w;
    byCompany.set(w.company, g);
  }
  for (const [company, g] of byCompany) {
    fleet.append(el("details", {},
      el("summary", {},
        el("span", { class: "badge warn" }, `${g.days} day${g.days > 1 ? "s" : ""}`),
        ` ${companyName(company)} exceeds fleet ${g.fleet_size} ` +
        `(worst: ${g.worst.day}, ${g.worst.count} simultaneous)`),
      ...S.engine.fleet_warnings
        .filter((w) => w.company === company)
        .map((w) => el("div", { class: "hint" }, `${w.day}: ${w.count} trips (fleet ${w.fleet_size})`))));
  }

  const integ = $("#integrity");
  integ.replaceChildren();
  if (!S.engine.integrity.length) integ.append(el("p", { class: "hint" }, "None."));
  // Clicking a finding takes the board to the trip it names, so the reading
  // ("these two overlap") and the picture of it are one click apart.
  for (const f of S.engine.integrity) {
    integ.append(el("div", { class: "finding",
      onclick: () => selectOnBoard(f.later) },
      /* explicit_er and reposition are both enumerated facts rather than
         violations — one written in the sheet, one approved here — so neither
         is dressed as a warning the reader has to go and fix. */
      el("span", { class: "badge " + (["explicit_er", "reposition"]
        .includes(f.kind) ? "system" : "warn") }, f.kind),
      /* Most findings are about a PAIR that will not chain; pax_mismatch is
         about one trip sitting on a bus too small a party for it. The engine
         sends the same id twice there rather than growing the shape, so the
         arrow is dropped instead of pointing a row at itself. */
      ` ${unitLabel(f.company, f.unit)}: ` + (f.earlier === f.later
        ? tripLabel(f.later)
        : `${tripLabel(f.earlier)} → ${tripLabel(f.later)}`)));
  }

  const parkedBox = $("#parked");
  parkedBox.replaceChildren();
  if (!S.engine.parked.length) parkedBox.append(el("p", { class: "hint" }, "None."));
  /* A unit idle mid-chain longer than any chain window. The planner cannot
     propose into these (its cross-city moves are tail-only), so the fits are
     listed for a person instead — acting on one is the board's ordinary drag,
     which the drop rules already allow. Fits arrive cheapest-drive-first. */
  /* Windows with something to act on first — on live data most windows are
     history (a bus that sat idle months ago) and would bury the one that
     matters. Stable sort, so the canonical unit order holds within groups. */
  for (const w of [...S.engine.parked].sort((a, b) => b.fits.length - a.fits.length)) {
    const from = tripById(w.after), until = tripById(w.before);
    parkedBox.append(el("details", {},
      el("summary", {},
        el("span", { class: "badge " + (w.fits.length ? "warn" : "system") },
          w.fits.length ? `${plural(w.fits.length, "trip")} fit` : "idle"),
        ` ${unitLabel(w.company, w.unit)} parked in ${w.city} for ${w.days} days`
        + (from && until ? ` (${from.end} → ${until.start})` : "")),
      ...w.fits.map((f) => {
        const drive = f.er_out_days + f.er_back_days;
        return el("div", { class: "finding",
          onclick: () => selectOnBoard(f.demand) },
        el("span", { class: "badge system" },
          drive ? `${plural(drive, "day")} empty` : "no drive"),
        ` ${tripLabel(f.demand)} — departs ${f.gap_out} days after the bus lands`
        + (f.chains ? "" : ", records no link"));
      })));
  }
}

/* ---------- Worklist tab ---------- */

function renderWorklist() {
  const box = $("#worklist");
  box.replaceChildren();
  if (!S.engine.broken_chains.length) {
    box.append(el("p", { class: "hint" }, "No broken chains."));
    return;
  }
  for (const b of S.engine.broken_chains) {
    /* An imported bus can be cleared too — it writes a blank Company cell,
       exactly as typing over it in the Data tab does. The button says so
       rather than being withheld: what used to hide it was the store's old
       refusal, and that was an artefact of clearing-by-deleting-the-override
       (see Repo._clear_assignment), not a rule about source data. */
    const imported = !("Company name" in (S.overrides[b.assignment] || {}));
    box.append(el("div", { class: "cand" },
      el("span", {}, tripLabel(b.assignment)),
      el("span", { class: "reason" },
        `relied on ${tripLabel(b.canceled_dependency)} (canceled/unassigned)`),
      el("button", { class: "btn small",
        title: imported
          ? "This bus came from the spreadsheet. Clearing it exports a blank "
            + "Company cell for this row; undo reverses it."
          : "Frees the trip so the suggester re-proposes it.",
        // mutate: the in-flight guard, since this button has no confirm.
        onclick: () => mutate("/unassign", { trip: b.assignment },
                              null, { keepSel: true }) },
        imported ? "Clear sheet's bus & re-suggest" : "Unassign & re-suggest"),
    ));
  }
}

/* ---------- Registry tab ---------- */

async function seedRegistry() {
  try {
    const res = await post("/registry/seed", {});
    toast(`Seeded ${res.companies} companies` +
      (res.warnings.length ? ` · ${res.warnings.length} warnings` : ""));
    await refresh();
  } catch (err) { toast(err.message, true); }
}

/* The note is free text on an 18-row table, so it is always an input rather
   than click-to-edit. Saved via the same upsert the Add/update form uses. */
function noteInput(company) {
  const input = el("input", { class: "note-input", placeholder: "note" });
  input.value = company.note ?? "";
  const save = async () => {
    if (input.value === (company.note ?? "")) return;
    try {
      await post("/registry/companies", {
        name: company.name, note: input.value || null,
      });
      await refresh();
    } catch (err) { toast(err.message, true); input.value = company.note ?? ""; }
  };
  input.addEventListener("blur", save);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); input.blur(); }
    else if (e.key === "Escape") { input.value = company.note ?? ""; input.blur(); }
  });
  return input;
}

/* Cities and the passthrough pick lists render identically: chips with a
   remove button, plus an add form. */
function chipList(values, onAdd, onRemove, placeholder) {
  const box = el("div", {});
  box.append(...values.map((v) =>
    el("span", { class: "chip" }, v,
      el("button", {
        onclick: async () => {
          try { await onRemove(v); await refresh(); }
          catch (err) { toast(err.message, true); }
        },
      }, "×"))));
  const input = el("input", { placeholder });
  box.append(el("form", {
    class: "row-form",
    onsubmit: async (e) => {
      e.preventDefault();
      if (!input.value.trim()) return;
      try { await onAdd(input.value.trim()); await refresh(); }
      catch (err) { toast(err.message, true); }
    },
  }, input, el("button", { class: "btn", type: "submit" }, "Add")));
  return box;
}

/* A rename is not a registry-only edit and the confirmation says so: trip rows
   carry the company as text ('Ollex 1'), so the new name is written into every
   one of those cells too — otherwise they would stop resolving and drop out of
   the operable set. That also means the next export carries the new name into
   cells the app had never touched, which is worth stating before the click
   rather than discovering in a diff. One undo step covers all of it. */
async function renameCompany(c) {
  const name = prompt(
    `Rename ${c.name} to:\n\n`
    + "Every trip naming it — Company name and Bus Preference alike — is "
    + "rewritten to match, so nothing stops resolving. The exported sheet will "
    + "carry the new name too. Undo reverses the whole thing in one step.",
    c.name);
  if (name === null) return;
  if (!name.trim() || name.trim() === c.name) return;
  try {
    const res = await post(`/registry/companies/${encodeURIComponent(c.key)}/rename`,
                           { name });
    await refresh();
    toast(`Renamed ${c.name} to ${name.trim()}`
      + (res.cells ? ` — ${plural(res.cells, "cell")} updated` : ""));
  } catch (err) { toast(err.message, true); }
}

function renderRegistry() {
  $("#seed-btn").disabled = S.seeded;
  $("#seed-note").textContent = S.seeded
    ? "Registry seeded — maintained here from now on."
    : "One-time seed from the imported workbook's Information sheet.";
  const warn = $("#seed-warnings");
  warn.replaceChildren();
  /* Sync warnings sit beside the seed ones and are re-shown after every
     import, not written once: an unread seed warning is exactly how two real
     vehicles went unregistered and took 31 rows out of the operable set. */
  // Deduped: before the first seed, an import stores the reader's warnings as
  // sync warnings, and seeding then stores the same ones again as seed
  // warnings — the "Fleets column is empty" line printed twice.
  for (const w of new Set([...S.seed_warnings, ...(S.sync_warnings || [])])) {
    warn.append(el("div", { class: "reason" }, "⚠ " + w));
  }
  for (const name of S.unwritable_companies || []) {
    warn.append(el("div", { class: "reason" },
      `⚠ ${name} has no row in the Information sheet's Bus Companies column, `
      + "so its fleet cannot be written back on export."));
  }

  const table = $("#companies-table");
  /* Fleet is derived (= unit count), so it is shown, never edited: change it
     by adding or removing units, or in the sheet's Fleets column. */
  table.replaceChildren(el("tr", {},
    ...["Company", "Fleet", "Units", "Note", ""].map((h) => el("th", {}, h))));
  for (const c of S.registry.companies) {
    const unitChips = el("span", {},
      ...c.units.map((u) => el("span", { class: "chip" }, String(u),
        el("button", { onclick: async () => {
          try { await api(`/registry/companies/${encodeURIComponent(c.key)}/units/${u}`, { method: "DELETE" }); await refresh(); }
          catch (err) { toast(err.message, true); }
        }}, "×"))),
      el("button", { class: "btn small", onclick: async () => {
        const u = prompt(`Add unit number for ${c.name}:`);
        if (!u) return;
        try { await post(`/registry/companies/${encodeURIComponent(c.key)}/units`, { unit: Number(u) }); await refresh(); }
        catch (err) { toast(err.message, true); }
      }}, "+ unit"));
    table.append(el("tr", {},
      el("td", {}, c.name, " ", el("button", {
        class: "btn small", title: `Rename ${c.name}`,
        onclick: () => renameCompany(c),
      }, "Rename")),
      el("td", {}, c.fleet_size ?? ""),
      el("td", {}, unitChips),
      el("td", {}, noteInput(c)),
      el("td", {}, el("button", { class: "btn small", onclick: async () => {
        if (!confirm(`Delete company ${c.name}? Trips referencing it become non-operable.`)) return;
        try { await api(`/registry/companies/${encodeURIComponent(c.key)}`, { method: "DELETE" }); await refresh(); }
        catch (err) { toast(err.message, true); }
      }}, "Delete")),
    ));
  }

  $("#cities").replaceChildren(chipList(
    S.registry.cities,
    (code) => post("/registry/cities", { code }),
    (code) => api(`/registry/cities/${encodeURIComponent(code)}`, { method: "DELETE" }),
    "City code",
  ));

  // Nationality / OP: dropdown sources only. Deleting an entry prunes the
  // menu; it never invalidates a row, because the contract does not read
  // these columns. They live in the exported workbook (Information!O and !P)
  // and an import reads them back, so edits here are not app-only state —
  // worth saying, since pruning is otherwise easy to mistake for a local view.
  const INFO_COLUMN = { Nationality: "O", OP: "P" };
  const lists = $("#option-lists");
  lists.replaceChildren();
  for (const [column, values] of Object.entries(S.registry.options || {})) {
    lists.append(
      el("h2", {}, column, " ", el("small", {},
        `(${values.length} values · dropdown only · Information!${INFO_COLUMN[column] || "?"})`)),
      chipList(
        values,
        (value) => post("/registry/options", { column, value }),
        (value) => api(
          `/registry/options/${encodeURIComponent(column)}/${encodeURIComponent(value)}`,
          { method: "DELETE" }),
        `Add ${column}`,
      ));
  }
}

/* ---------- wiring ---------- */

/* An unseeded registry fails every row on Departure/Arrival, which reads as a
   broken app rather than a missing setup step. Say so from whichever tab the
   user is on, and put the fix one click away. */
function renderSetupBanner() {
  const banner = $("#setup-banner");
  const needsSeed = S.dataset && !S.seeded;
  banner.classList.toggle("hidden", !needsSeed);
  if (!needsSeed) return;
  /* Not "all rows, because no city resolves": the import syncs the city list
     whether or not the registry is seeded (api.import_file), so on an
     unseeded registry most rows are operable and the ones that are not
     usually fail for reasons seeding will not fix. Only companies wait on it. */
  const bad = S.non_operable.length;
  const total = S.trips.length + bad;
  banner.replaceChildren(
    el("span", {}, "Registry is not seeded, so no company or bus unit resolves yet — "
      + `${bad} of ${total} imported rows are currently non-operable.`),
    el("button", { class: "btn primary", onclick: seedRegistry },
      "Seed from Information sheet"),
  );
}

/* The New-trip form is static markup but its registry-derived lists are not: a
   company added on the Registry tab has to become pickable without a reload, so
   every select is refilled on every state load. The current choice is carried
   across, or a refresh triggered while the form is half-filled would drop it.

   Only the options are swapped, not the select itself: the element stays put so
   the form's grid styling and the submit handler's name= both still see an
   ordinary field. Which is also the constraint on what may go here — the
   builder has to return something whose childNodes are options. The Data tab's
   Company picker is the one that no longer can (comboInput builds a listbox of
   divs; its children inside a <select> render as nothing at all, which is what
   left this field permanently empty and silently unfillable), so the form has
   its own native version below over the same values. */
const MANUAL_LISTS = [
  ["#manual-company", manualCompanyInput],
  ["#manual-preference", preferenceInput],
  // Nationality and OP are passthrough columns with a stored pick list; the
  // ids are `manual-<title>` so the tie to the list is one lookup, not a table.
  ...["Nationality", "OP"].map((title) => [
    `#manual-${title.toLowerCase()}`,
    (raw) => listInput((S.registry.options || {})[title] || [], raw, "— none —"),
  ]),
  ...["departure", "arrival"].map((which) => [
    `#manual-${which}`,
    (raw) => listInput(S.registry.cities || [], raw, "— pick a city —"),
  ]),
];

/* The form's Company picker. Deliberately a plain select rather than the Data
   tab's tiered combo: those tiers rank units by availability against a trip's
   dates, and a trip being typed has none — companyNameInput passed no trip
   already degrades to exactly this flat list, so there is nothing to lose. */
function manualCompanyInput(raw) {
  const { units, adhoc } = companyValues();
  return el("select", {},
    option("", "— needs a bus —", raw === ""),
    el("optgroup", { label: "Buses" },
      ...units.map((v) => option(v, v, v === raw))),
    el("optgroup", { label: "Ad hoc — company only, no unit (never chains)" },
      ...adhoc.map((v) => option(v, v, v === raw))));
}

function renderManualLists() {
  for (const [id, build] of MANUAL_LISTS) {
    const slot = $(id);
    const current = slot.value || "";
    slot.replaceChildren(...build(current).childNodes);
    /* Re-asserted, not trusted to the move: extracting children out of the
       built <select> fires the DOM's select-reset on every removal of the
       currently-selected option, cascading a spurious selected flag onto
       whatever leaves last — which is how every dropdown here came to open on
       its last option (Trondheim, Vietnamese, 'NO <first company>'). Setting
       .value selects the one matching option and clears the rest. */
    slot.value = current;
  }
}

function renderAll() {
  const info = S.dataset
    ? `${S.dataset.filename || "csv import"} · ${S.dataset.source_kind} · imported ${S.dataset.imported_at}`
    : "no dataset imported";
  $("#dataset-info").textContent = info;
  renderUndo();
  renderSetupBanner();
  renderManualLists();
  renderPastePicker();
  renderColumnToggles();
  renderTrips();
  renderPlanToggles();
  renderRechain();
  renderOptimal();
  renderReclaim();
  renderPlanSummary();
  renderPlanChains();
  renderSuggestions();
  renderTimelines();
  renderWorklist();
  renderRegistry();
}

/* ---------- Manual tab ---------- */

/* The manual is static prose in its own file, fetched once on first open.
   Not part of renderAll and not built from S: it renders no state, so
   rebuilding it on every mutation would be pure cost. innerHTML rather than
   el(): the markup is a fixed asset from this app's own static mount, never
   user input, and hand-building a hundred paragraphs in JS would put the prose
   somewhere nobody can read or edit it as prose. A failed fetch leaves a retry
   rather than an empty tab. */
let MANUAL_LOADED = false;

async function loadManual() {
  if (MANUAL_LOADED) return;
  MANUAL_LOADED = true;                 // guard first: two fast clicks, one fetch
  const box = $("#manual-body");
  try {
    const res = await fetch("manual.html");
    if (!res.ok) throw new Error(res.statusText);
    box.innerHTML = await res.text();
  } catch (err) {
    MANUAL_LOADED = false;
    box.replaceChildren(
      el("p", { class: "reason" }, `Could not load the manual: ${err.message}`),
      el("button", { class: "btn", onclick: loadManual }, "Retry"));
  }
}

function showTab(name) {
  document.querySelectorAll("#tabs button").forEach(
    (b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tab").forEach(
    (s) => s.classList.toggle("active", s.id === "tab-" + name));
  // A render skipped while this tab was hidden is owed now that it can be seen.
  if (name === "timelines" && BOARD_STALE) {
    // The deferred render runs the instant the tab is visible again, when
    // scrollLeft genuinely reads 0 (display:none dropped it) — reading it
    // back would park the board on day 0. BOARD_DAY still holds the last
    // good position; retarget to it.
    BOARD_RETARGET = true;
    renderBoard();
  }
  if (name === "manual") loadManual();
  if (name === "data") {
    // Same hidden-measurement debt as the board: see alignFilterRow.
    alignFilterRow($("#trips-table"));
    alignFilterRow($("#nonop-table"));
  }
  applyBoardScroll();          // the board could not position itself while hidden
}

$("#tabs").addEventListener("click", (e) => {
  if (e.target.tagName !== "BUTTON") return;
  showTab(e.target.dataset.tab);
});

document.querySelectorAll(".search").forEach((input) =>
  input.addEventListener("input", () => {
    VIEWS[input.dataset.table].search = input.value;
    debounceRows(input.dataset.table);
  }));

document.querySelectorAll("[data-clear]").forEach((btn) =>
  btn.addEventListener("click", () => clearFilters(btn.dataset.clear)));

$("#trips-today").addEventListener("click", jumpToToday);

document.querySelectorAll('input[name="direction"]').forEach((r) =>
  r.addEventListener("change", renderSuggestions));

/* Board interaction. Bars are delegated (there are thousands of them); ghost
   slots carry their own handler, since each one closes over the exact
   suggestion it would accept — no id round-tripping through the DOM. */
$("#board").addEventListener("click", (e) => {
  // A drag ends with a click on the common ancestor of press and release,
  // which here is the board itself — i.e. "empty canvas", i.e. clear the
  // selection. Swallowing it is what keeps a move from also deselecting.
  if (DRAG_WAS_A_DRAG) { DRAG_WAS_A_DRAG = false; return; }
  if (e.target.closest(".ghost")) return;
  /* Three ways into a unit's panel, all resolving to its tail: the bar itself,
     the row label (which carries the "idle since Sep" note, so it is where the
     eye already is), and the stub in the empty space after the last bar. The
     control being correct is not the same as it being reachable — the picker
     shipped once behind the very gate it existed to escape. */
  const anchor = e.target.closest(".row-label[data-trip], .next-stub");
  const bar = anchor || e.target.closest(".bar");
  NEXT_ALL = false;                         // a fresh selection, a fresh list
  SEL = bar ? bar.dataset.trip : null;      // empty canvas clears
  renderBoard();
});

/* ---------- dragging a bar ---------- */

/* Two gestures share one grab, and the first few pixels decide which:
   sideways moves the trip's DATES, up or down moves it to another BUS. Locked
   rather than combined, because the two are different kinds of decision — one
   edits source data the export splices, the other records a routing decision —
   and a diagonal drag would have to mean both at once, in one undo step, with
   no way to express "I only meant the second one".

   Everything here is preview only. Nothing is written until the pointer comes
   up, the server re-derives what the drop means (see /reassign), and the
   ordinary refetch-and-render loop takes over. */

const DRAG_THRESHOLD = 5;                 // px before a press becomes a drag
let DRAG = null;
/* The click that follows a drag is not a selection — see the handler above. */
let DRAG_WAS_A_DRAG = false;

/* ISO dates parse as UTC midnight and toISOString renders in UTC, so shifting
   by whole days is exact arithmetic in both directions — the same property the
   board's axis relies on, and the reason no timezone can move a trip a day. */
const shiftISO = (iso, days) =>
  new Date(Date.parse(iso) + days * DAY_MS).toISOString().slice(0, 10);

const dropKey = (t) =>
  t.unit == null ? `adhoc|${t.company}` : `${t.company}|${t.unit}`;

/* Any assignment can be taken back off, including one that arrived on the
   spreadsheet: clearing it writes an empty Company cell, which is what typing
   over it in the Data tab has always done. Only the planner is barred from
   touching imported assignments (see Repo.system_assigned_ids and the master
   re-chain) — a person dragging a bar is not the planner. */
const canUncover = (t) => !!t.assignment;

/* What dropping the dragged bar on this row would do. One function behind the
   row highlight, the note that follows the pointer and the commit itself, so
   the three can never tell the user different things. */
function dropVerdict(d, key) {
  if (!key) return { ok: false, note: "" };
  if (key === d.fromKey) return { ok: false, note: "already on this row" };
  if (key === "uncovered") {
    if (!canUncover(d.trip)) {
      return { ok: false, note: "this trip has no bus to take off" };
    }
    // Naming the export consequence for an imported bus: clearing it splices a
    // BLANK over what the spreadsheet said, which is a different thing from
    // dropping an assignment this app made and worth knowing before the drop.
    const imported = !("Company name" in (S.overrides[d.trip.id] || {}));
    return { ok: true, action: "unassign",
             note: "take the bus off this trip — it returns to the uncovered queue"
               + (imported ? "; this bus came from the sheet, so the export will "
                             + "carry a blank where it named one" : "") };
  }
  if (!d.targets) return { ok: false, note: "asking the engine…" };
  const target = d.targets.get(key);
  if (target) return { ok: true, action: "reassign", target, note: dropStory(target) };
  return { ok: false, note: d.refused.get(key) || "cannot take this trip" };
}

/* What the drop costs, named before it happens — the same bargain the manual
   pickers strike by putting the rule they break inside the button. Never names
   the bus: the caller prefixes the row label, so that every verdict, refusals
   included, reads the same way round. */
function dropStory(t) {
  if (t.unit == null) {
    return "an ad hoc bus beyond the charter: it chains to nothing and "
      + "generates no supply";
  }
  if (t.chains) {
    return `chains on ${plural(freeDays(t.gap_days), "free day")} after its `
      + "previous trip"
      + (t.reposition ? ", with an EMPTY RUN to get there" : "");
  }
  if (t.gap_days == null) return "lands first on this bus";
  // The case the drop exists for and a suggestion could never express: legal,
  // but too far from the previous trip to be a handover. Saying so is the
  // difference between a considered decision and a surprise in the integrity
  // list afterwards.
  return `leaves the bus parked ${plural(freeDays(t.gap_days), "free day")} `
    + "after its previous trip: further than a chain reaches, so no link is "
    + "recorded";
}

function dragNote(e, text, ok) {
  let note = $("#drag-note");
  if (!note) {
    note = el("div", { id: "drag-note" });
    document.body.append(note);
  }
  note.textContent = text;
  note.className = ok ? "ok" : "bad";
  note.style.left = e.clientX + 14 + "px";
  note.style.top = e.clientY + 18 + "px";
  note.classList.toggle("hidden", !text);
}

$("#board").addEventListener("pointerdown", (e) => {
  if (e.button !== 0 || MUTATING) return;
  const bar = e.target.closest(".bar");
  const trip = bar && tripById(bar.dataset.trip);
  if (!trip) return;
  /* Where the trip actually IS, from its assignment — not from the row the bar
     was drawn in. With "Show plan on buses" ticked a provisional bar sits on
     the unit that would run it while still being uncovered, and reading the
     row would call
     dropping it there "already on this row" when it is in fact the accept. */
  const a = trip.assignment;
  const fromKey = !a ? "uncovered"
    : a.unit == null ? `adhoc|${a.company}` : `${a.company}|${a.unit}`;
  DRAG = { trip, bar, fromKey,
           x0: e.clientX, y0: e.clientY, axis: null, days: 0,
           targets: null, refused: null, overRow: null, overKey: null,
           pending: null, canceled: false };
  // Capture on the bar: the pointer will leave it immediately (that is the
  // whole gesture), and without capture the move events would go to whatever
  // is underneath instead.
  bar.setPointerCapture(e.pointerId);
});

$("#board").addEventListener("pointermove", (e) => {
  const d = DRAG;
  if (!d || d.canceled) return;
  const dx = e.clientX - d.x0, dy = e.clientY - d.y0;
  if (!d.axis) {
    if (Math.abs(dx) < DRAG_THRESHOLD && Math.abs(dy) < DRAG_THRESHOLD) return;
    d.axis = Math.abs(dx) >= Math.abs(dy) ? "x" : "y";
    d.bar.classList.add("dragging");
    $("#board").classList.add("dragging-" + d.axis);
    if (d.axis === "y") {
      /* Asked once, at the moment the gesture commits to being a move. It is a
         full engine evaluate, and nothing can change the answer while the
         pointer is down — no state mutates mid-drag. The drop awaits this if
         the user is faster than the round trip. */
      d.pending = api(`/drop-targets/${d.trip.id}`).then((res) => {
        d.targets = new Map(res.targets.map((t) => [dropKey(t), t]));
        d.refused = new Map(
          res.refused.map((r) => [`${r.company}|${r.unit}`, r.reason]));
        if (DRAG === d) markDropRows(d);
      }).catch((err) => {
        toast(err.message, true);
        d.targets = new Map();
        d.refused = new Map();
      });
    }
  }

  if (d.axis === "x") {
    d.days = Math.round(dx / DAY_W);
    d.bar.style.transform = `translateX(${d.days * DAY_W}px)`;
    dragNote(e, d.days
      ? `${shiftISO(d.trip.start, d.days)} → ${shiftISO(d.trip.end, d.days)}`
        + ` (${d.days > 0 ? "+" : ""}${d.days}d) — moves the trip's dates in the`
        + " sheet; the span is unchanged"
      : "sideways moves the trip's dates · up and down moves it to another bus",
      true);
    return;
  }

  d.bar.style.transform = `translateY(${dy}px)`;
  /* elementsFromPoint, not elementFromPoint: the date axis is sticky and
     paints OVER the top of the board, so with the board scrolled, the topmost
     element across a whole strip of visible rows is the axis — and singular
     elementFromPoint returned it, making every drop in that strip silently
     find no row at all. The row is in the hit stack underneath; take the first
     one that is a lane. (The dragged bar is pointer-transparent — see
     .bar.dragging — so it is never in the stack to begin with.) */
  const row = document.elementsFromPoint(e.clientX, e.clientY)
    .map((n) => n.closest && n.closest(".board-row[data-row-key]"))
    .find(Boolean) || null;
  if (row !== d.overRow) {
    if (d.overRow) d.overRow.classList.remove("drop-hover", "drop-bad");
    d.overRow = row;
    d.overKey = row ? row.dataset.rowKey : null;
  }
  const v = dropVerdict(d, d.overKey);
  if (row) {
    row.classList.toggle("drop-hover", v.ok);
    row.classList.toggle("drop-bad", !v.ok);
  }
  dragNote(e, row ? `${row.dataset.rowLabel} — ${v.note}` : "", v.ok);
});

function markDropRows(d) {
  for (const row of document.querySelectorAll("#board .board-row[data-row-key]")) {
    row.classList.toggle("drop-ok", dropVerdict(d, row.dataset.rowKey).ok);
  }
}

function endDrag(d) {
  d.bar.classList.remove("dragging");
  d.bar.style.transform = "";
  $("#board").classList.remove("dragging-x", "dragging-y");
  for (const row of document.querySelectorAll("#board .board-row")) {
    row.classList.remove("drop-ok", "drop-hover", "drop-bad");
  }
  const note = $("#drag-note");
  if (note) note.classList.add("hidden");
}

$("#board").addEventListener("pointerup", async (e) => {
  const d = DRAG;
  if (!d) return;
  DRAG = null;
  endDrag(d);
  if (!d.axis) return;                  // never passed the threshold: a click
  /* Set here and nowhere else, because a click follows a release and nothing
     else: setting it when the drag was CANCELLED would leave it armed with no
     click to consume it, and swallow the user's next real one. */
  DRAG_WAS_A_DRAG = true;
  if (d.canceled) return;

  if (d.axis === "x") {
    if (!d.days) return;
    /* Both ends in one call: moving one date and then the other would be two
       undo steps for one gesture, and would re-validate the row in between
       with a span the contract refuses (see Repo.set_cells). keepSel, because
       a trip that just moved is the one you are still looking at. */
    await mutate(`/trips/${d.trip.id}/cells`, { values: {
      "Start date": shiftISO(d.trip.start, d.days),
      "End date": shiftISO(d.trip.end, d.days),
    } }, `Moved ${tripLabel(d.trip.id)} ${d.days > 0 ? "+" : ""}${d.days} day`
       + `${Math.abs(d.days) === 1 ? "" : "s"}`, { keepSel: true });
    return;
  }

  if (d.pending) await d.pending;       // dropped faster than the round trip
  const v = dropVerdict(d, d.overKey);
  if (!v.ok) {
    if (v.note) toast(v.note, true);
    return;
  }
  if (v.action === "unassign") {
    await mutate("/unassign", { trip: d.trip.id },
                 `${tripLabel(d.trip.id)} is uncovered again`, { keepSel: true });
    return;
  }
  await mutate("/reassign",
               { trip: d.trip.id, company: v.target.company, unit: v.target.unit },
               `${tripLabel(d.trip.id)} → ${unitLabel(v.target.company, v.target.unit)}`,
               { keepSel: true });
});

/* Cancel paths. Escape while dragging, and the pointercancel the browser fires
   when something else claims the gesture (a scroll, a context menu); both have
   to put the bar back, since the transform is the only thing moving it.
   pointercancel means no pointerup is coming, so the drag is dropped outright;
   Escape is only marked, because the button is still down and the release —
   and the click after it — still have to be accounted for. */
$("#board").addEventListener("pointercancel", () => {
  if (DRAG) { endDrag(DRAG); DRAG = null; }
});
document.addEventListener("keydown", (e) => {
  if (!DRAG || DRAG.canceled || e.key !== "Escape") return;
  DRAG.canceled = true;
  endDrag(DRAG);
  // Escape here means "put that bar back", not "clear the selection" — which
  // is what the handler below would otherwise also do with the same keypress.
  e.stopPropagation();
}, true);                               // ahead of the selection handler

document.addEventListener("keydown", (e) => {
  if (!SEL) return;
  // Not while a cell editor is open — Escape belongs to the editor there, and
  // "d" is a letter someone is trying to type. Optional call because the target
  // is not always an element (it is the document itself when nothing is
  // focused), and matches() only exists on elements.
  if (e.target.matches?.("input, select, textarea")) return;
  if (e.key === "Escape") {
    SEL = null;
    renderBoard();
  } else if (e.key === "d" && !e.ctrlKey && !e.metaKey && !e.altKey) {
    // Unmodified letter: a browser or OS shortcut would carry a modifier, and
    // taking those would shadow it. Only bound while a bar is selected, which
    // is exactly when it has something to act on.
    jumpToDataRow(SEL);
  }
});

$(".zoom-group").addEventListener("click", (e) => {
  if (!e.target.dataset.zoom) return;
  const box = $("#board");
  // Convert the position to a day index at the *old* scale first, or the same
  // pixel offset would mean a different date once the day width changes.
  if (box.firstChild && box.offsetParent) BOARD_DAY = box.scrollLeft / DAY_W;
  BOARD_RETARGET = true;
  DAY_W = Number(e.target.dataset.zoom);
  document.querySelectorAll(".zoom-group button").forEach((b) =>
    b.classList.toggle("active", b === e.target));
  renderBoard();
});

$("#show-canceled").addEventListener("change", (e) => {
  SHOW_CANCELED = e.target.checked;
  renderBoard();
});

// The markup's default must not contradict the remembered choice.
$("#all-uncovered").checked = ALL_UNCOVERED;
$("#all-uncovered").addEventListener("change", (e) => {
  ALL_UNCOVERED = e.target.checked;
  try { localStorage.setItem(ALL_UNCOVERED_KEY, String(ALL_UNCOVERED)); } catch {}
  renderBoard();
});

$("#show-adhoc").addEventListener("change", (e) => {
  SHOW_ADHOC = e.target.checked;
  renderBoard();
});

/* "Show plan on buses". Stored as mode "auto" (ticked) / "manual" (not) — the
   names it had as a Suggestions control; renaming them would mean migrating a
   saved row for a word nobody sees. The re-render afterwards is for the save
   that did not happen: mutate returns without refreshing on an error or while
   another mutation is in flight, and a checkbox, unlike the buttons this
   replaced, flips the moment it is clicked. */
$("#plan-on-units").addEventListener("change", async (e) => {
  await setPlanSetting({ mode: e.target.checked ? "auto" : "manual" });
  if (S) renderPlanToggles();
});

$("#objective-group").addEventListener("click", (e) => {
  if (e.target.dataset.objective) {
    setPlanSetting({ objective: e.target.dataset.objective });
  }
});

$("#rechain-btn").addEventListener("click", previewRechain);
$("#optimize-btn").addEventListener("click", previewOptimal);
$("#reclaim-btn").addEventListener("click", previewReclaim);

$("#reposition-group").addEventListener("click", (e) => {
  if (e.target.dataset.reposition) {
    setPlanSetting({ reposition: e.target.dataset.reposition === "1" });
  }
});

/* Sent as the preset string ("30", "all"), never as a resolved date: the server
   decides what "30" means from its own today, so a tab left open overnight
   cannot plan against yesterday's window. */
$("#horizon-group").addEventListener("click", (e) => {
  if (e.target.dataset.horizon) {
    setPlanSetting({ horizon: e.target.dataset.horizon });
  }
});

$("#board-today").addEventListener("click", boardGoToday);

$("#import-file").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  if (S.dataset && !confirm("Importing replaces the current working dataset (in-system changes are kept only in exports you have already downloaded). Continue?")) {
    e.target.value = "";
    return;
  }
  const form = new FormData();
  form.append("file", file);
  // Disabled for the duration: a re-pick mid-upload would import twice (the
  // label-for a disabled input opens no picker).
  e.target.disabled = true;
  try {
    const res = await api("/import", { method: "POST", body: form });
    toast(`Imported ${res.rows} rows (${res.source_kind})`);
    await refresh();
  } catch (err) { toast(err.message, true); }
  finally { e.target.disabled = false; }
  e.target.value = "";
});

$("#show-manual-form").addEventListener("click", () =>
  $("#manual-form").classList.toggle("hidden"));

$("#manual-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  // Double-submit guard: a second Enter during the POST would add the row
  // twice, with two undo entries the first of which appears to do nothing.
  const btn = e.target.querySelector('[type="submit"]');
  if (btn.disabled) return;
  btn.disabled = true;
  const values = {};
  for (const field of e.target.elements) {
    if (field.name && field.value) values[field.name] = field.value;
  }
  try {
    await post("/trips", { values });
    $("#manual-error").textContent = "";
    e.target.reset();
    toast("Trip added (see Data tab for its contract result)");
    await refresh();
  } catch (err) { $("#manual-error").textContent = err.message; }
  finally { btn.disabled = false; }
});

$("#show-paste-form").addEventListener("click", () =>
  $("#paste-form").classList.toggle("hidden"));

/* Which contract columns a paste carries. Excel omits hidden columns from the
   copied text and the text carries no trace of it, so this is declared by the
   planner, never guessed — the sheet's hidden set is theirs to know. Stored as
   the UNticked titles: the empty set is the full layout, so the default needs
   no storage and a new column in a future layout arrives ticked. */
const PASTE_COLS_KEY = "buschain-paste-unticked";

function renderPastePicker() {
  const box = $("#paste-cols");
  if (!S.columns) return;
  // Rebuilt only when the column list itself changes — so refreshes never
  // wipe ticks the user just set, but an import that replaces the dataset
  // with a different layout does re-derive the picker. Keyed on the titles
  // rather than "built once": a stale picker would send the previous
  // layout's titles with every paste.
  const sig = S.columns.join("\0");
  if (box.dataset.sig === sig) return;
  box.dataset.sig = sig;
  box.replaceChildren();
  const unticked = loadSet(PASTE_COLS_KEY);
  box.append("My paste contains: ");
  for (const title of S.columns) {
    const input = el("input", { type: "checkbox", value: title,
                                checked: !unticked.has(title) });
    input.addEventListener("change", () => {
      const off = [...box.querySelectorAll("input")]
        .filter((i) => !i.checked).map((i) => i.value);
      try { localStorage.setItem(PASTE_COLS_KEY, JSON.stringify(off)); } catch {}
    });
    box.append(el("label", { class: "paste-col" }, input, title));
  }
}

function pasteColumns() {
  const ticked = [...$("#paste-cols").querySelectorAll("input")]
    .filter((i) => i.checked).map((i) => i.value);
  // The full layout is sent as null, not as all fifteen titles: 'nothing
  // declared' and 'everything declared' behave identically, and null keeps
  // the default path in the codec the one the tests exercise hardest.
  return ticked.length === (S.columns || []).length ? null : ticked;
}

$("#paste-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  // Same double-submit guard as the manual form.
  const btn = e.target.querySelector('[type="submit"]');
  if (btn.disabled) return;
  btn.disabled = true;
  const text = $("#paste-text").value;
  try {
    const res = await post("/trips/paste", { text, columns: pasteColumns() });
    $("#paste-error").textContent = "";
    $("#paste-text").value = "";
    await refresh();
    /* Say how the batch landed rather than just how big it was: the rows that
       failed the contract are in a different table from the ones that passed,
       and a paste of 40 that quietly put 12 of them there reads as a success
       until you scroll. Counted after the refresh, against the ids the server
       gave back — the server does not validate, the state load does. */
    const bad = new Set(S.non_operable.map((r) => r.id));
    const failed = res.ids.filter((id) => bad.has(id)).length;
    toast(failed
      ? `Added ${res.ids.length} rows — ${failed} need fixing, see Non-operable`
      : `Added ${res.ids.length} rows`);
    renderPasteColumns(res);
  } catch (err) {
    $("#paste-error").textContent = err.message;
    $("#paste-columns").replaceChildren();
  } finally { btn.disabled = false; }
});

/* What the paste turned out to contain, kept on screen after the toast goes.
   A hidden column in Excel is simply absent from the clipboard, so a paste can
   be three columns short and every row still looks plausible — naming the
   columns that were and were not read is the only place that shows. */
function renderPasteColumns({ columns, by_header, overflow_rows }) {
  const missing = (S.columns || []).filter((c) => !columns.includes(c));
  $("#paste-columns").replaceChildren(
    el("span", {}, by_header
      ? `A header row was included — read ${columns.length} columns by title: ${columns.join(", ")}.`
      : `Read ${columns.length} columns in order: ${columns.join(", ")}.`),
    missing.length
      ? el("span", { class: "warn-text" },
          ` Not in the paste, left empty: ${missing.join(", ")}.`)
      : "",
    overflow_rows
      ? el("span", { class: "warn-text" },
          ` ${overflow_rows} row${overflow_rows === 1 ? " carried" : "s carried"}`
          + " more fields than the ticked columns, and the extras were dropped"
          + " — if nothing is hidden in your sheet, tick every column and paste again.")
      : "",
  );
}

$("#seed-btn").addEventListener("click", seedRegistry);

$("#company-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  // Same double-submit guard as the manual form.
  const btn = e.target.querySelector('[type="submit"]');
  if (btn.disabled) return;
  btn.disabled = true;
  const f = e.target;
  try {
    await post("/registry/companies", {
      name: f.name.value,
      note: f.note.value || null,
    });
    f.reset();
    await refresh();
  } catch (err) { toast(err.message, true); }
  finally { btn.disabled = false; }
});


/* A failed first load is otherwise a dead page: S stays null, every later
   interaction throws into the console, and the old toast vanished after four
   seconds. Persistent banner, retry in place. */
function bootFailed(err) {
  const banner = $("#setup-banner");
  banner.classList.remove("hidden");
  banner.replaceChildren(
    el("span", {}, `Could not load the app's state: ${err.message} — `
      + "the server may be down. Nothing on this page works until it loads."),
    // On success renderAll's own renderSetupBanner reclaims the banner (it
    // either hides it or shows the needs-seeding message) — no cleanup here.
    el("button", { class: "btn primary",
      onclick: () => refresh().catch(bootFailed) }, "Retry"));
}

refresh().catch(bootFailed);
