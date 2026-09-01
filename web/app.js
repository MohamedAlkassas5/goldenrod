/* Goldenrod — the interface.
 *
 * No framework and no build step, on purpose: the whole surface is one call
 * sheet, the server already returns exactly the shape it needs, and a demo that
 * depends on a toolchain is a demo that breaks on the day.
 *
 * Two rules this file must not break, both from CLAUDE.md:
 *   1. No confidence percentages. Ever. Raw counts or nothing.
 *   2. Nothing is rendered that the API did not return. There is no client-side
 *      severity, no client-side ordering by anything but the server's own rank,
 *      and no invented text on a finding.
 */

const STEP_LABELS = {
  parse_day:  "Parse day",
  draft_diff: "Draft diff",
  traverse:   "Traverse",
  ledger:     "Ledger",
  commitment: "Commitment",
  rank:       "Rank + cite",
};

const state = {
  callSheet: null,
  run: null,
  steps: [],
  selectedStep: "ledger",
  graph: null,
  tab: "decisions",
  // Who the server says we are. Never trusted for scoping — the server scopes —
  // but it is what decides whether a control is worth drawing at all.
  me: null,
  identities: [],
  trail: null,
};

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
};

/* --------------------------------------------------------------- identity */
/* The chosen identity travels as a cookie rather than a header, because the
 * check is an EventSource and EventSource cannot send one. Behind Cloud IAP
 * none of this runs: the platform sets the header on every request, including
 * the stream, and the server ignores the cookie entirely. */
function signIn(subject) {
  document.cookie =
    `goldenrod_subject=${encodeURIComponent(subject)}; path=/; SameSite=Strict`;
}

function renderIdentity() {
  if (!state.me || !state.identities.length) return;
  $("identity").hidden = false;
  const select = $("identity-select");
  select.replaceChildren();
  state.identities.forEach((person) => {
    const option = el("option", null, person.display_name || person.subject);
    option.value = person.subject;
    option.selected = person.subject === state.me.subject;
    select.append(option);
  });
  $("identity-role").textContent = state.me.role.title;
  select.onchange = async () => {
    signIn(select.value);
    state.graph = null;
    state.trail = null;
    state.me = await (await fetch("/api/access/me")).json();
    renderIdentity();
    renderFindings();
    runCheck();
  };
}

/* What this role is not being shown, said out loud rather than left blank. */
function renderAccessStrip() {
  const strip = $("access-strip");
  const access = state.run && state.run.access;
  if (!access || !state.me) { strip.hidden = true; return; }

  const scoped = access.departments[0] !== "*";
  if (!scoped && !access.withheld && !access.script_redacted) {
    strip.hidden = true;
    return;
  }
  strip.hidden = false;
  strip.replaceChildren();
  strip.append(el("span", "who", state.me.role.title));
  const notes = [];
  if (scoped) {
    notes.push(`scoped to ${access.departments.join(", ")}`);
  }
  if (access.withheld) {
    notes.push(
      `${access.withheld} finding${access.withheld === 1 ? "" : "s"} withheld`);
  }
  if (access.script_redacted) notes.push("verbatim pages withheld");
  strip.append(el("span", "notes", notes.join("  ·  ")));
}

/* ------------------------------------------------------------------ dates */
function longDate(iso) {
  if (!iso) return "—";
  const d = new Date(iso.length === 10 ? iso + "T00:00:00" : iso);
  if (isNaN(d)) return iso;
  return d.toLocaleDateString("en-GB", {
    weekday: "long", day: "numeric", month: "long", year: "numeric",
  }).toUpperCase();
}

function shortStamp(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return d.toLocaleDateString("en-GB", { day: "2-digit", month: "short" }) +
         " " + d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
}

/* ------------------------------------------------------------- the header */
function renderHeader() {
  const cs = state.callSheet;
  if (!cs) return;
  $("production-title").textContent = (cs.production_id || "").replace(/[-_]/g, " ");
  $("production-day").textContent =
    `${cs.call_sheet_id || "call sheet"} · ${cs.unit || "Main Unit"}`;
  $("f-shoot-date").textContent = longDate(cs.shoot_date);
  $("f-issued").textContent = shortStamp(cs.issued_at) || "—";
  $("f-revision").textContent = cs.revision_id;
  $("f-prior").textContent = cs.prior_revision_id || "…";
  $("scene-count").textContent =
    `${cs.scenes.length} scene${cs.scenes.length === 1 ? "" : "s"}`;
}

/* ----------------------------------------------------------- the pipeline */
function renderSteps() {
  const wrap = $("steps");
  wrap.replaceChildren();
  const names = Object.keys(STEP_LABELS);
  names.forEach((name, index) => {
    const done = state.steps.find((s) => s.name === name);
    const running = !done && state.steps.length === index;
    const button = el("button", "step");
    if (done) button.classList.add("done");
    if (running) button.classList.add("running");
    if (name === state.selectedStep && done) button.classList.add("selected");
    button.disabled = !done;

    const head = el("span", "name");
    head.append(el("span", "n", `${index + 1} `), document.createTextNode(STEP_LABELS[name]));
    button.append(head);
    button.append(el("span", "meta",
      done ? `${done.rows} rows · ${done.ms} ms` : (running ? "" : "waiting")));

    if (done) button.onclick = () => { state.selectedStep = name; renderSteps(); renderQuery(); };
    wrap.append(button);
  });
}

/* -------------------------------------------------------------- the SQL */
const SQL_KEYWORDS = /\b(WITH|SELECT|FROM|WHERE|GROUP BY|ORDER BY|LEFT JOIN|INNER JOIN|FULL OUTER JOIN|JOIN|ON|AS|AND|OR|NOT|IN|LIMIT|FINAL|ASC|DESC|DISTINCT|CASE|WHEN|THEN|ELSE|END)\b/g;

function highlight(sql) {
  const escaped = sql.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
  return escaped
    .replace(/'[^']*'/g, (m) => `<span class="str">${m}</span>`)
    .replace(SQL_KEYWORDS, (m) => `<span class="kw">${m}</span>`);
}

function renderQuery() {
  const step = state.steps.find((s) => s.name === state.selectedStep);
  $("query-title").textContent = step
    ? `ClickHouse · ${STEP_LABELS[step.name]} · via the MCP server`
    : "ClickHouse · via the MCP server";
  $("query-note").textContent = step
    ? `${step.rows} row${step.rows === 1 ? "" : "s"} in ${step.ms} ms`
    : "Select a pipeline step above";

  const pre = $("sql");
  if (!step || !step.sql) {
    pre.textContent = step
      ? "This step reads no database — it parses the call sheet the production office issued."
      : "—";
  } else {
    pre.innerHTML = highlight(step.sql);
  }

  const rows = $("rows");
  rows.replaceChildren();
  if (step && step.sample_withheld) {
    // The query ran and returned rows; this role may not read what is in them.
    // Saying "no rows" here would be a lie about the pipeline.
    rows.append(el("div", "empty withheld",
      `${step.sample_withheld} row${step.sample_withheld === 1 ? "" : "s"} returned — ` +
      `withheld. These rows carry the pages, and your role has no page access.`));
    return;
  }
  if (!step || !step.sample || !step.sample.length) {
    rows.append(el("div", "empty",
      step && step.sql ? "Query returned no rows." : "No rows for this step."));
    return;
  }
  const table = el("table", "rows");
  const head = el("tr");
  const columns = Object.keys(step.sample[0]);
  columns.forEach((c) => head.append(el("th", null, c)));
  table.append(el("thead").appendChild(head).parentNode);
  const body = el("tbody");
  step.sample.forEach((row) => {
    const tr = el("tr");
    columns.forEach((c) => {
      const value = row[c];
      tr.append(el("td", null, Array.isArray(value) ? value.join(", ") : String(value ?? "")));
    });
    body.append(tr);
  });
  table.append(body);
  rows.append(table);
}

/* ----------------------------------------------------------- the scenes */
function findingsByScene() {
  const map = new Map();
  ((state.run && state.run.findings) || []).forEach((f) => {
    if (!map.has(f.scene)) map.set(f.scene, []);
    map.get(f.scene).push(f);
  });
  return map;
}

function renderScenes() {
  const cs = state.callSheet;
  if (!cs) return;
  const wrap = $("scenes");
  wrap.replaceChildren();
  const byScene = findingsByScene();
  const done = !!state.run;

  cs.scenes.forEach((scene) => {
    const hits = byScene.get(scene.scene_number) || [];
    const row = el("div", "scene");
    row.append(el("div", "scene-no", scene.scene_number));

    const body = el("div", "scene-body");
    body.append(el("div", "slug",
      [scene.int_ext, scene.location, scene.day_night].filter(Boolean).join(". ")));
    if (scene.synopsis) body.append(el("div", "synopsis", scene.synopsis));

    const foot = el("div", "scene-foot");
    foot.append(el("span", null, [
      scene.estimated_call ? `call ${scene.estimated_call}` : null,
      scene.page_eighths ? `${scene.page_eighths}/8 pages` : null,
    ].filter(Boolean).join("  ·  ")));

    const verdict = el("span", "verdict");
    if (!done) { verdict.classList.add("pending"); verdict.textContent = "Checking"; }
    else if (hits.length) {
      verdict.classList.add("flagged");
      verdict.textContent = `${hits.length} finding${hits.length === 1 ? "" : "s"}`;
    } else { verdict.classList.add("clear"); verdict.textContent = "Clear"; }
    foot.append(verdict);

    body.append(foot);
    row.append(body);
    wrap.append(row);
  });

  // Findings can land on scenes that are not on tomorrow's sheet. That is the
  // point of a dependency graph, so the sheet says so rather than hiding them.
  const scheduled = new Set(cs.scenes.map((s) => s.scene_number));
  const offsheet = ((state.run && state.run.findings) || [])
    .filter((f) => !scheduled.has(f.scene));
  $("offsheet").hidden = offsheet.length === 0;
  const list = $("offsheet-list");
  list.replaceChildren();
  offsheet.forEach((f) => {
    const li = el("li");
    li.append(el("span", null, `Scene ${f.scene}`));
    li.append(el("span", null, f.commitment_state));
    list.append(li);
  });
}

/* --------------------------------------------------------- the findings */
function citation(item, redacted) {
  const li = el("li");
  if (item.type === "scene_line") {
    li.append(el("span", "cite", `${item.scene} : ${item.line}`));
    // The citation survives redaction — scene and line are still there, and the
    // finding still cites. Only the page is gone.
    if (redacted) {
      li.append(el("span", "quote gone", item.quote));
      return li;
    }
    const q = el("span", "quote", `“${item.quote}”`);
    if (item.revision_id && state.run && item.revision_id !== state.run.revision_id) {
      q.append(el("span", "cite", `  ${item.revision_id}`));
    }
    li.append(q);
  } else if (item.type === "decision") {
    li.append(el("span", "cite", "decision"));
    const why = el("span", "why");
    why.append(el("b", null, item.reason));
    const who = [item.decided_by, item.decided_at ? shortStamp(item.decided_at) : null]
      .filter(Boolean).join(" · ");
    if (who) why.append(document.createTextNode(` — ${who}`));
    li.append(why);
  } else {
    li.append(el("span", "cite", "committed"));
    li.append(el("span", "why",
      `${item.entity_name || item.entity_id} — ${item.state}` +
      (item.committed_at ? ` since ${shortStamp(item.committed_at)}` : "")));
  }
  return li;
}

function renderFindings() {
  const wrap = $("findings");
  wrap.replaceChildren();
  if (!state.run) {
    wrap.append(el("div", "all-clear", "Running the check…"));
    return;
  }
  const scheduled = new Set((state.callSheet.scenes || []).map((s) => s.scene_number));

  if (!state.run.findings.length) {
    wrap.append(el("div", "all-clear",
      "Nothing shoots tomorrow that this revision broke."));
  }

  state.run.findings.forEach((f) => {
    const card = el("div", "finding");
    card.dataset.state = f.commitment_state;

    const head = el("div", "finding-head");
    head.append(el("span", "tag", f.commitment_state));
    head.append(el("span", "finding-scene", `Scene ${f.scene}`));
    head.append(el("span", "finding-kind", (f.kind || "").replace(/_/g, " ")));
    if (scheduled.has(f.scene)) head.append(el("span", "tomorrow", "Shoots tomorrow"));
    card.append(head);

    card.append(el("p", "claim", f.claim));

    const ev = el("div", "evidence");
    ev.append(el("h4", null, `Evidence — ${f.evidence.length}`));
    const ul = el("ul");
    f.evidence.forEach((item) => ul.append(citation(item, f.redacted)));
    ev.append(ul);
    card.append(ev);

    const action = el("div", "action");
    action.append(el("span", "text", f.suggested_action || ""));
    // Accepting a deviation is an act of the production office. A role without
    // that grant is told so rather than shown a button the server will refuse.
    if (state.me && state.me.role.write_ledger) {
      const button = el("button", "stamp", "Mark intentional");
      button.onclick = () => openModal(f);
      action.append(button);
    } else {
      action.append(el("span", "stamp disabled", "Office sign-off required"));
    }
    card.append(action);

    wrap.append(card);
  });

  (state.run.dismissed || []).forEach((f) => {
    const note = f.dismissed || {};
    const card = el("div", "silenced");
    const head = el("div", "head");
    head.append(el("span", null, "Silenced"));
    head.append(el("span", null, `Scene ${f.scene}`));
    head.append(el("span", null, (f.kind || "").replace(/_/g, " ")));
    card.append(head);
    card.append(el("div", "body",
      `Marked intentional by ${note.marked_by || "—"}: ${note.reason || ""}`));
    card.append(el("div", "body", f.claim));
    wrap.append(card);
  });

  const dropped = (state.run.dropped || []);
  $("foot-right").textContent = dropped.length
    ? `${dropped.length} dropped or weakened: ${dropped.join(" · ")}`
    : "";
}

/* -------------------------------------------------------- mark intentional */
function openModal(finding) {
  const node = $("tpl-modal").content.firstElementChild.cloneNode(true);
  node.querySelector(".lead").textContent =
    `Scene ${finding.scene} — ${finding.claim}`;
  const reason = node.querySelector("#m-reason");
  const confirm = node.querySelector('[data-act="confirm"]');
  node.querySelector(".signed-as").textContent =
    state.me ? state.me.subject : "—";

  const close = () => node.remove();
  node.querySelector('[data-act="cancel"]').onclick = close;
  node.onclick = (e) => { if (e.target === node) close(); };
  document.addEventListener("keydown", function esc(e) {
    if (e.key === "Escape") { close(); document.removeEventListener("keydown", esc); }
  });

  confirm.onclick = async () => {
    if (!reason.value.trim()) {
      confirm.textContent = "A reason is required";
      return;
    }
    confirm.disabled = true;
    confirm.textContent = "Writing to the ledger…";
    try {
      const res = await fetch(
        `/api/findings/${encodeURIComponent(finding.finding_id)}/intentional`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          // No signature in the body: the server signs it with the identity it
          // proved, and would ignore anything sent here.
          body: JSON.stringify({ reason: reason.value.trim() }),
        },
      );
      const payload = await res.json();
      if (!res.ok) throw new Error(payload.detail || res.statusText);
      close();
      state.run = payload.run;
      state.steps = payload.run.steps;
      renderSteps(); renderQuery(); renderScenes(); renderFindings();
      renderAccessStrip();
      state.graph = null;
      state.trail = null;
    } catch (err) {
      confirm.disabled = false;
      confirm.textContent = "Retry";
      banner(String(err.message || err));
    }
  };

  document.body.append(node);
  reason.focus();
}

function banner(message) {
  const wrap = $("banner");
  wrap.replaceChildren();
  if (message) wrap.append(el("div", "banner", message));
}

/* ------------------------------------------------- what the office logged */
async function openDrawer() {
  $("drawer").classList.add("open");
  $("drawer").setAttribute("aria-hidden", "false");
  const scrim = el("div", "scrim");
  scrim.id = "scrim";
  scrim.onclick = closeDrawer;
  document.body.append(scrim);
  if (!state.graph) {
    $("drawer-body").replaceChildren(el("div", "empty", "Reading the graph…"));
    try {
      const res = await fetch("/api/graph");
      const payload = await res.json();
      if (!res.ok) throw new Error(payload.detail || res.statusText);
      state.graph = payload;
    } catch (err) {
      $("drawer-body").replaceChildren(el("div", "empty", String(err.message || err)));
      return;
    }
  }
  renderDrawer();
}

function closeDrawer() {
  $("drawer").classList.remove("open");
  $("drawer").setAttribute("aria-hidden", "true");
  const scrim = $("scrim");
  if (scrim) scrim.remove();
}

/* A surface this role is not entitled to. Rendering an empty list instead would
 * read as "the office has logged nothing", which is a different claim entirely. */
function withheldSurface(body, surface) {
  const note = surface === "decisions"
    ? "The decision ledger is the production office's record of why. Your role is not granted it."
    : "This is the script in structured form. Your role has no page access.";
  body.append(el("div", "empty withheld", note));
}

function renderDrawer() {
  const body = $("drawer-body");
  body.replaceChildren();
  const g = state.graph;
  if (!g) return;
  const names = Object.fromEntries((g.entities || []).map((e) => [e.entity_id, e.name]));
  const withheld = new Set((g.access && g.access.withheld_surfaces) || []);
  const surface = { decisions: "decisions", knowledge: "knowledge_state", facts: "facts" }[state.tab];
  if (surface && withheld.has(surface)) {
    withheldSurface(body, surface);
    return;
  }

  if (state.tab === "access") {
    renderAccessTab(body, g);
    return;
  }

  if (state.tab === "decisions") {
    body.append(el("p", "empty",
      "The ledger. Every entry carries the reason given at the time — this is what lets a finding say why, not only where."));
    (g.decisions || []).forEach((d) => {
      const entry = el("div", "ledger-entry");
      const top = el("div", "top");
      top.append(el("span", null, `Scene ${d.scene_id}`));
      top.append(el("span", null, d.decision_type));
      top.append(el("span", null, d.cause_tag));
      top.append(el("span", null, shortStamp(d.decided_at)));
      top.append(el("span", null, d.decided_by));
      if (d.status !== "active") top.append(el("span", "pill superseded", d.status));
      if (Number(d.intentional_deviation) === 1) {
        top.append(el("span", "pill deviation", "intentional deviation"));
      }
      entry.append(top);
      entry.append(el("div", "choice", d.selected_option));
      entry.append(el("div", "reason", d.reason));
      if ((d.entity_ids || []).length) {
        entry.append(el("div", "reason",
          d.entity_ids.map((e) => names[e] || e).join(" · ")));
      }
      body.append(entry);
    });
  }

  if (state.tab === "knowledge") {
    body.append(el("p", "empty",
      "Who knows what, by which scene. This is the table that catches the good ones."));
    (g.knowledge_state || []).forEach((k) => {
      const entry = el("div", "ledger-entry");
      const top = el("div", "top");
      top.append(el("span", null, names[k.character_entity_id] || k.character_entity_id));
      top.append(el("span", null, `by scene ${k.scene_number}`));
      top.append(el("span", "pill", Number(k.knows) ? "knows" : "does not know"));
      entry.append(top);
      if (k.acquired_via) entry.append(el("div", "reason", `Learns it: ${k.acquired_via}`));
      entry.append(el("div", "reason", k.fact_key));
      body.append(entry);
    });
  }

  if (state.tab === "facts") {
    body.append(el("p", "empty",
      `Facts established in ${g.revision_id}. The diff runs over these, not over the text.`));
    (g.facts || []).forEach((f) => {
      const entry = el("div", "ledger-entry");
      const top = el("div", "top");
      top.append(el("span", null, `Scene ${f.scene}`));
      top.append(el("span", null, `line ${f.source_line}`));
      top.append(el("span", "pill", f.kind));
      entry.append(top);
      entry.append(el("div", "choice", f.statement));
      entry.append(el("div", "reason",
        (f.entity_ids || []).map((e) => names[e] || e).join(" · ")));
      body.append(entry);
    });
  }

  if (state.tab === "commitments") {
    body.append(el("p", "empty",
      "What has already been paid for. Ranked by the same function the findings are ranked by."));
    (g.commitments || []).forEach((c) => {
      const entry = el("div", "ledger-entry");
      const top = el("div", "top");
      top.append(el("span", null, `Scene ${c.scene_id}`));
      top.append(el("span", null, names[c.entity_id] || c.entity_id));
      top.append(el("span", "pill", c.state));
      top.append(el("span", null, `${c.cost_band} cost`));
      entry.append(top);
      if (c.notes) entry.append(el("div", "reason", c.notes));
      body.append(entry);
    });
  }
}

/* Who holds what, and who has looked. The trail is the half of governance a
 * filter does not give you: the office can answer "who opened the pages". */
function renderAccessTab(body, g) {
  body.append(el("p", "empty",
    "Access is granted per production and enforced on the server. Every ledger " +
    "write is attributed, and every read and refusal is recorded."));

  state.identities.forEach((person) => {
    const entry = el("div", "ledger-entry");
    const top = el("div", "top");
    top.append(el("span", null, person.display_name || person.subject));
    top.append(el("span", "pill", person.role.title));
    if (person.subject === (state.me && state.me.subject)) {
      top.append(el("span", "pill deviation", "you"));
    }
    entry.append(top);
    entry.append(el("div", "reason", person.subject));
    entry.append(el("div", "reason",
      "sees " + person.role.departments.join(", ") +
      (person.role.read_script ? " · pages" : " · no pages") +
      (person.role.write_ledger ? " · may accept deviations" : "")));
    body.append(entry);
  });

  if (!state.me.role.read_ledger) {
    body.append(el("div", "empty withheld",
      "The audit trail records who read what. Oversight of it belongs to the " +
      "production office, so your role is not granted it — including your own rows."));
    return;
  }
  if (state.trail === null) {
    body.append(el("div", "empty", "Reading the audit trail…"));
    fetch("/api/access/log").then(async (res) => {
      state.trail = res.ok ? (await res.json()).trail : [];
      if (state.tab === "access") renderDrawer();
    });
    return;
  }
  if (!state.trail.length) return;

  body.append(el("p", "empty", "Audit trail — newest first."));
  state.trail.slice(0, 40).forEach((row) => {
    const entry = el("div", "ledger-entry");
    const top = el("div", "top");
    top.append(el("span", null, shortStamp(row.at)));
    top.append(el("span", null, row.subject));
    top.append(el("span", "pill" + (Number(row.granted) ? "" : " superseded"), row.action));
    entry.append(top);
    const detail = [
      row.detail,
      Number(row.released) ? `${row.released} released` : null,
      Number(row.withheld) ? `${row.withheld} withheld` : null,
    ].filter(Boolean).join(" · ");
    if (detail) entry.append(el("div", "reason", detail));
    body.append(entry);
  });
}

/* ------------------------------------------------------------- the run */
function runCheck() {
  state.steps = [];
  state.run = null;
  renderSteps(); renderFindings(); renderScenes();

  const source = new EventSource("/api/run/stream");

  source.addEventListener("step", (e) => {
    state.steps.push(JSON.parse(e.data));
    renderSteps();
    renderQuery();
  });

  source.addEventListener("done", (e) => {
    state.run = JSON.parse(e.data);
    state.steps = state.run.steps;
    $("f-prior").textContent = state.run.prior_revision_id || "—";
    renderSteps(); renderQuery(); renderScenes(); renderFindings();
    renderAccessStrip();
    source.close();
  });

  source.addEventListener("error", (e) => {
    let detail = "The check could not run. Is ClickHouse reachable, and are both revisions loaded?";
    try { detail = JSON.parse(e.data).detail; } catch (_) { /* transport error */ }
    banner(detail);
    // Never leave "running" on screen after a failure. A check that stopped is
    // not a check that found nothing, and the difference is the whole product.
    $("findings").replaceChildren(
      el("div", "all-clear", "The check did not finish. Nothing here has been verified."));
    renderSteps();
    source.close();
  });
}

/* --------------------------------------------------------------- startup */
async function start() {
  $("open-drawer").onclick = openDrawer;
  $("close-drawer").onclick = closeDrawer;
  $("tabs").onclick = (e) => {
    const tab = e.target.closest(".tab");
    if (!tab) return;
    state.tab = tab.dataset.tab;
    [...$("tabs").children].forEach((t) => t.classList.toggle("on", t === tab));
    renderDrawer();
  };

  // Sign in first: every route below is scoped, and a page that renders the
  // day before it knows who is reading has already decided nothing is secret.
  if (!(await establishIdentity())) return;

  const sheet = await fetch("/api/call-sheet");
  if (!sheet.ok) {
    banner((await sheet.json()).detail || "Could not read the call sheet.");
    return;
  }
  state.callSheet = await sheet.json();
  renderHeader();
  renderScenes();
  renderSteps();

  // Nobody presses anything. The call sheet being issued is the trigger.
  runCheck();
}

/* Returns false when nobody could be identified, which is a dead end rather
 * than a degraded page: without an identity there is nothing to show. */
async function establishIdentity() {
  try {
    const chooser = await fetch("/api/access/identities");
    if (chooser.ok) {
      state.identities = (await chooser.json()).identities;
    }
  } catch (_) { /* behind IAP the chooser does not exist; that is not an error */ }

  let me = await fetch("/api/access/me");
  if (me.status === 401 && state.identities.length) {
    // No cookie yet. Start as the office, which is who opens this at 7pm.
    const office =
      state.identities.find((p) => p.role.write_ledger) || state.identities[0];
    signIn(office.subject);
    me = await fetch("/api/access/me");
  }
  if (!me.ok) {
    banner((await me.json()).detail || "No access to this production.");
    return false;
  }
  state.me = await me.json();
  renderIdentity();
  return true;
}

start();
