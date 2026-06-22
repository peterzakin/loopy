// Loopy control-plane dashboard — a read-only viewer.
// Vanilla JS, no build step. Hash-routed SPA over four read APIs:
//   #runs       /api/runs(+/{id})  — run history (polled)
//   #schedules  /api/schedules     — cron workflows + poll sensors
//   #workflows  /api/workflows     — workflow templates as DAGs + event lineage
//   #registry   /api/registry      — agents / sandboxes / events (secrets redacted)

"use strict";

const POLL_MS = 3000;
const VIEWS = ["runs", "schedules", "workflows", "registry"];
const MANIFEST_VIEWS = ["schedules", "workflows", "registry"];

const state = {
  view: "runs",
  filter: "", // "" | running | completed | failed
  selected: null, // run_id of the open detail, or null
  manifest: null, // whether a manifest is loaded (from /api/meta)
};

const $ = (sel) => document.querySelector(sel);

// ── helpers ──────────────────────────────────────────────────────────────
async function getJSON(url) {
  const res = await fetch(url, { headers: { Accept: "application/json" } });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString(undefined, {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
}

function fmtRel(iso) {
  if (!iso) return "—";
  const ms = new Date(iso) - Date.now();
  const abs = Math.abs(ms);
  const mins = Math.round(abs / 60000);
  const unit = mins < 60 ? `${mins}m` : mins < 1440 ? `${Math.round(mins / 60)}h` : `${Math.round(mins / 1440)}d`;
  return ms >= 0 ? `in ${unit}` : `${unit} ago`;
}

function fmtDuration(startIso, endIso) {
  if (!startIso || !endIso) return null;
  const ms = new Date(endIso) - new Date(startIso);
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
  return `${Math.floor(ms / 60000)}m ${Math.round((ms % 60000) / 1000)}s`;
}

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
}

const STATE_CLASS = { completed: "is-ok", running: "is-running", failed: "is-bad" };

function badge(name, kind) {
  const cls = kind || STATE_CLASS[name] || "";
  const b = el("span", `lui-badge ${cls}`, name);
  return b;
}

function chip(text) {
  return el("span", "lui-chip", text);
}

function tags(items, builder) {
  const wrap = el("div", "taglist");
  (items || []).forEach((x) => wrap.appendChild((builder || chip)(x)));
  return wrap;
}

// ── runs ───────────────────────────────────────────────────────────────────
async function refreshRuns() {
  if (state.view !== "runs") return;
  const q = state.filter ? `?state=${encodeURIComponent(state.filter)}` : "";
  let runs;
  try {
    runs = await getJSON(`/api/runs${q}`);
  } catch (e) {
    return; // transient; the next poll retries
  }
  renderRunList(runs);
  if (state.selected) await refreshDetail();
}

function renderRunList(runs) {
  const list = $("#run-list");
  $("#run-count").textContent = runs.length ? `${runs.length}` : "";
  $("#runs-empty").hidden = runs.length > 0;
  list.replaceChildren();

  for (const run of runs) {
    const row = el("div", "run-row");
    row.dataset.runId = run.run_id;
    if (run.run_id === state.selected) row.classList.add("is-selected");

    const left = el("div");
    left.appendChild(badge(run.state));

    const mid = el("div");
    mid.appendChild(el("div", "run-id", run.run_id));
    const dur = fmtDuration(run.created_at, run.ended_at);
    mid.appendChild(
      el("div", "run-meta", `${run.workflow} · ${run.entry_event}${dur ? ` · ${dur}` : ""}`)
    );

    const right = el("div", "run-time", fmtTime(run.created_at));
    row.append(left, mid, right);
    row.addEventListener("click", () => selectRun(run.run_id));
    list.appendChild(row);
  }
}

async function selectRun(runId) {
  state.selected = runId;
  document.querySelectorAll(".run-row").forEach((r) =>
    r.classList.toggle("is-selected", r.dataset.runId === runId)
  );
  await refreshDetail();
}

async function refreshDetail() {
  if (!state.selected) return;
  let detail;
  try {
    detail = await getJSON(`/api/runs/${encodeURIComponent(state.selected)}`);
  } catch (e) {
    return;
  }
  renderDetail(detail);
}

function renderDetail(d) {
  $("#detail-empty").hidden = true;
  const body = $("#detail-body");
  body.hidden = false;
  body.replaceChildren();

  const head = el("div", "detail-head");
  const title = el("div", "detail-title");
  title.appendChild(el("h2", null, d.run_id));
  title.appendChild(badge(d.state));
  head.appendChild(title);
  const dur = fmtDuration(d.created_at, d.ended_at);
  const sub = el("div", "detail-sub");
  sub.innerHTML =
    `<b>${d.workflow}</b> · triggered by <b>${d.entry_event ?? "—"}</b> · ` +
    `${fmtTime(d.created_at)}${dur ? ` · ran ${dur}` : ""}`;
  head.appendChild(sub);
  body.appendChild(head);

  if (d.error) body.appendChild(el("div", "error-box", d.error));

  if (d.emitted && d.emitted.length) {
    const sec = section("Emitted events");
    sec.appendChild(tags(d.emitted));
    body.appendChild(sec);
  }

  const tl = section("Timeline");
  const wrap = el("div", "timeline");
  for (const ev of d.history) {
    const row = el("div", "tl-row");
    row.appendChild(el("div", `tl-dot ${ev.kind}`));
    const mid = el("div", "tl-kind");
    mid.innerHTML = `<b>${ev.kind}</b>${ev.step_id ? ` <span class="tl-step">${ev.step_id}</span>` : ""}`;
    row.appendChild(mid);
    row.appendChild(el("div", "tl-time", fmtTime(ev.at)));
    wrap.appendChild(row);
  }
  tl.appendChild(wrap);
  body.appendChild(tl);

  const outSec = section("Step outputs");
  const steps = Object.keys(d.outputs || {});
  if (!steps.length) {
    outSec.appendChild(el("div", "muted", "No recorded outputs."));
  } else {
    for (const step of steps) {
      const o = el("div", "output");
      o.appendChild(el("div", "output-step", step));
      o.appendChild(el("pre", "lui-code json", JSON.stringify(d.outputs[step], null, 2)));
      outSec.appendChild(o);
    }
  }
  body.appendChild(outSec);
}

function section(heading) {
  const sec = el("div", "section");
  sec.appendChild(el("h3", null, heading));
  return sec;
}

// ── schedules ────────────────────────────────────────────────────────────
async function loadSchedules() {
  const root = $("#schedules-body");
  root.replaceChildren();
  let d;
  try {
    d = await getJSON("/api/schedules");
  } catch (e) {
    root.appendChild(el("div", "empty", "Could not load schedules."));
    return;
  }
  if (!d.manifest_present) return;

  if (!d.schedules.length) {
    root.appendChild(el("div", "empty", "No cron workflows or poll sensors defined."));
  } else {
    const grid = el("div", "grid");
    for (const s of d.schedules) grid.appendChild(scheduleCard(s));
    root.appendChild(grid);
  }

  if (d.webhooks && d.webhooks.length) {
    root.appendChild(el("div", "subhead", "Inbound webhooks"));
    const grid = el("div", "grid");
    for (const w of d.webhooks) {
      const card = el("div", "lui-card entity");
      const t = el("div", "entity-title");
      t.appendChild(el("span", "name", w.sensor));
      t.appendChild(badge("webhook", "is-accent"));
      card.appendChild(t);
      const kv = el("dl", "kv");
      addKV(kv, "path", el("code", null, w.path || "—"));
      addKV(kv, "emits", chip(w.emits));
      card.appendChild(kv);
      grid.appendChild(card);
    }
    root.appendChild(grid);
  }
}

function scheduleCard(s) {
  const card = el("div", "lui-card entity");
  const t = el("div", "entity-title");
  t.appendChild(el("span", "name", s.type === "cron" ? s.workflow : s.sensor));
  t.appendChild(badge(s.type, "is-accent"));
  card.appendChild(t);

  const kv = el("dl", "kv");
  if (s.type === "cron") {
    addKV(kv, "cron", el("code", null, `${s.expr}${s.tz ? ` (${s.tz})` : ""}`));
    addKV(kv, "step", el("code", null, s.step));
  } else {
    addKV(kv, "every", el("code", null, s.interval));
  }
  if (s.emits && s.emits.length) addKV(kv, "emits", tags([].concat(s.emits)));
  addKV(kv, "last run", el("span", null, fmtTime(s.last_run)));
  const next = el("span", "next-run", `${fmtTime(s.next_run)} · ${fmtRel(s.next_run)}`);
  addKV(kv, "next run", next);
  card.appendChild(kv);
  return card;
}

function addKV(dl, label, valueNode) {
  dl.appendChild(el("dt", null, label));
  const dd = el("dd");
  dd.appendChild(typeof valueNode === "string" ? document.createTextNode(valueNode) : valueNode);
  dl.appendChild(dd);
}

// ── workflows / DAG ──────────────────────────────────────────────────────
async function loadWorkflows() {
  const root = $("#workflows-body");
  root.replaceChildren();
  let d;
  try {
    d = await getJSON("/api/workflows");
  } catch (e) {
    root.appendChild(el("div", "empty", "Could not load workflows."));
    return;
  }
  if (!d.manifest_present) return;

  for (const wf of d.workflows) root.appendChild(workflowCard(wf));

  const lineage = Object.keys(d.lineage || {});
  if (lineage.length) {
    root.appendChild(el("div", "subhead", "Event lineage — how workflows compose"));
    const card = el("div", "lui-card wf");
    const wrap = el("div", "lineage");
    for (const ev of lineage.sort()) {
      const e = d.lineage[ev];
      const row = el("div", "lineage-row");
      const left = el("div", "lineage-side left");
      (e.producers.length ? e.producers : ["—"]).forEach((p) => left.appendChild(chip(p)));
      const mid = el("div", "lineage-evt");
      mid.appendChild(badge(ev, "is-accent"));
      const right = el("div", "lineage-side right");
      (e.consumers.length ? e.consumers : ["—"]).forEach((c) => right.appendChild(chip(c)));
      row.append(left, mid, right);
      wrap.appendChild(row);
    }
    card.appendChild(wrap);
    root.appendChild(card);
  }
}

function workflowCard(wf) {
  const card = el("div", "lui-card wf");
  const head = el("div", "wf-head");
  head.appendChild(el("span", "wf-name", wf.name));
  head.appendChild(el("span", "wf-trigger", triggerLabel(wf.trigger)));
  card.appendChild(head);

  const byName = {};
  for (const st of wf.steps) byName[st.name] = st;

  const dag = el("div", "dag");
  wf.generations.forEach((gen, gi) => {
    if (gi > 0) {
      const arrow = el("div", "dag-arrow");
      arrow.textContent = "→";
      dag.appendChild(arrow);
    }
    const col = el("div", "dag-col");
    for (const name of gen) col.appendChild(stepNode(byName[name], name === wf.entry));
    dag.appendChild(col);
  });
  card.appendChild(dag);
  return card;
}

function stepNode(st, isEntry) {
  const node = el("div", `node${isEntry ? " is-entry" : ""}`);
  node.appendChild(el("div", "node-name", st.name));
  if (st.agent) node.appendChild(el("div", "node-agent", `agent · ${st.agent}`));
  const meta = el("div", "node-meta");
  (st.outputs || []).forEach((o) => meta.appendChild(chip(o.name)));
  (st.emits || []).forEach((e) => meta.appendChild(badge(e, "is-accent")));
  if (meta.childNodes.length) node.appendChild(meta);
  return node;
}

function triggerLabel(t) {
  if (!t) return "";
  if (t.kind === "event") return `on ${t.event}`;
  if (t.kind === "cron") return `cron ${t.expr}${t.tz ? ` (${t.tz})` : ""}`;
  return t.kind;
}

// ── registry ─────────────────────────────────────────────────────────────
async function loadRegistry() {
  const root = $("#registry-body");
  root.replaceChildren();
  let d;
  try {
    d = await getJSON("/api/registry");
  } catch (e) {
    root.appendChild(el("div", "empty", "Could not load registry."));
    return;
  }
  if (!d.manifest_present) return;

  if (d.agents.length) {
    root.appendChild(el("div", "subhead", "Agents"));
    const grid = el("div", "grid");
    for (const a of d.agents) grid.appendChild(agentCard(a));
    root.appendChild(grid);
  }
  if (d.sandboxes.length) {
    root.appendChild(el("div", "subhead", "Sandboxes"));
    const grid = el("div", "grid");
    for (const sb of d.sandboxes) grid.appendChild(sandboxCard(sb));
    root.appendChild(grid);
  }
  if (d.events.length) {
    root.appendChild(el("div", "subhead", "Events"));
    const grid = el("div", "grid");
    for (const ev of d.events) grid.appendChild(eventCard(ev));
    root.appendChild(grid);
  }
  if (d.limits) {
    root.appendChild(el("div", "subhead", "Limits"));
    const card = el("div", "lui-card entity");
    card.appendChild(el("pre", "lui-code json", JSON.stringify(d.limits, null, 2)));
    root.appendChild(card);
  }
}

function entityCard(name) {
  const card = el("div", "lui-card entity");
  const t = el("div", "entity-title");
  t.appendChild(el("span", "name", name));
  card.appendChild(t);
  return { card, title: t };
}

function agentCard(a) {
  const { card, title } = entityCard(a.name);
  if (a.runtime) title.appendChild(badge(a.runtime, "is-accent"));
  const kv = el("dl", "kv");
  if (a.model) addKV(kv, "model", el("code", null, a.model));
  if (a.sandbox) addKV(kv, "sandbox", el("code", null, a.sandbox));
  if (a.skills && a.skills.length) addKV(kv, "skills", tags(a.skills));
  card.appendChild(kv);
  return card;
}

function sandboxCard(sb) {
  const { card, title } = entityCard(sb.name);
  if (sb.provider) title.appendChild(badge(sb.provider, "is-accent"));
  const kv = el("dl", "kv");
  if (sb.image && Object.keys(sb.image).length)
    addKV(kv, "image", el("code", null, JSON.stringify(sb.image)));
  if (sb.network && sb.network.length) addKV(kv, "network", tags(sb.network));
  if (sb.repos && sb.repos.length) addKV(kv, "repos", tags(sb.repos.map((r) => r.url)));
  // Secrets are never sent by the API — show only that some are configured.
  const sec = sb.secrets || { count: 0 };
  addKV(kv, "secrets", el("span", "secret", `🔒 ${sec.count} env file${sec.count === 1 ? "" : "s"} (hidden)`));
  card.appendChild(kv);
  return card;
}

function eventCard(ev) {
  const { card } = entityCard(ev.name);
  if (!ev.fields.length) {
    card.appendChild(el("div", "muted", "No fields."));
    return card;
  }
  for (const f of ev.fields) {
    const row = el("div", "fieldrow");
    row.appendChild(el("span", "fname", f.name));
    let type = f.type || "any";
    if (f.format) type += `<${f.format}>`;
    if (f.enum) type = `enum[${f.enum.join(", ")}]`;
    row.appendChild(el("span", "ftype", type));
    card.appendChild(row);
  }
  return card;
}

// ── routing ──────────────────────────────────────────────────────────────
function currentView() {
  const h = (location.hash || "#runs").slice(1);
  return VIEWS.includes(h) ? h : "runs";
}

async function route() {
  state.view = currentView();
  for (const v of VIEWS) $(`#view-${v}`).hidden = v !== state.view;
  document.querySelectorAll(".view-link").forEach((l) =>
    l.classList.toggle("is-active", l.dataset.view === state.view)
  );

  const needsManifest = MANIFEST_VIEWS.includes(state.view);
  $("#no-manifest").hidden = !(needsManifest && state.manifest === false);

  if (state.view === "runs") await refreshRuns();
  else if (state.view === "schedules") await loadSchedules();
  else if (state.view === "workflows") await loadWorkflows();
  else if (state.view === "registry") await loadRegistry();
}

// ── wiring ───────────────────────────────────────────────────────────────
function initFilters() {
  $("#filters").addEventListener("click", (e) => {
    const btn = e.target.closest(".filter");
    if (!btn) return;
    state.filter = btn.dataset.state;
    document.querySelectorAll(".filter").forEach((b) => b.classList.toggle("is-active", b === btn));
    refreshRuns();
  });
}

async function loadMeta() {
  try {
    const meta = await getJSON("/api/meta");
    state.manifest = !!meta.manifest_present;
  } catch (e) {
    state.manifest = null;
  }
}

async function init() {
  initFilters();
  await loadMeta();
  window.addEventListener("hashchange", route);
  await route();
  // Only the runs view auto-refreshes; the others are static templates.
  setInterval(() => { if (state.view === "runs") refreshRuns(); }, POLL_MS);
}

init();
