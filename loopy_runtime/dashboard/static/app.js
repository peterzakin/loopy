// Loopy control-plane dashboard — a read-only viewer.
// Vanilla JS, no build step. Hash-routed SPA over four read APIs:
//   #runs       /api/runs(+/{id})  — run history (polled)
//   #schedules  /api/schedules     — cron workflows + poll sensors
//   #workflows  /api/workflows     — workflow templates as DAGs + event lineage
//   #registry   /api/registry      — agents / sandboxes / events (secrets redacted)

"use strict";

const POLL_MS = 3000;
const VIEWS = ["runs", "workflows", "sensors", "registry"];
const MANIFEST_VIEWS = ["workflows", "sensors", "registry"];

const state = {
  view: "runs",
  filter: "", // "" | running | completed | failed
  selected: null, // run_id of the open detail, or null
  step: null, // "workflow/step" key of the open step panel, or null
  manifest: null, // whether a manifest is loaded (from /api/meta)
  demo: false, // serving synthesized fake data (`loopy demo`), from /api/meta
};

const $ = (sel) => document.querySelector(sel);

// ── helpers ──────────────────────────────────────────────────────────────
// API and asset URLs are relative (no leading slash) so they resolve against the
// document's base — the page serves either at the site root (`loopy admin`, the remote
// proxy) or under `/admin` when mounted on the engine's webhook server, and a leading
// slash would send every request to the wrong prefix and 404.
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

// ── markdown (step bodies) ───────────────────────────────────────────────
// A small renderer for the subset step prompts actually use: headings, lists,
// fenced code, inline code/bold/italic/links, paragraphs. Everything is
// HTML-escaped before any markup is added, so manifest content can't inject.
function escapeHTML(s) {
  return s.replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]
  );
}

// Inline spans over already-escaped text. Code spans are carved out first so
// template refs / bold / links are not rewritten inside backticks.
function mdInline(escaped) {
  return escaped
    .split(/(`[^`]+`)/)
    .map((seg) => {
      if (seg.startsWith("`") && seg.endsWith("`")) return `<code>${seg.slice(1, -1)}</code>`;
      return seg
        .replace(/\{\{\s*([^{}]+?)\s*\}\}/g, '<span class="md-ref">{{ $1 }}</span>')
        .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
        .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
        .replace(/(^|\W)\*([^*\n]+)\*/g, "$1<em>$2</em>")
        .replace(/(^|\W)_([^_\n]+)_/g, "$1<em>$2</em>");
    })
    .join("");
}

function renderMarkdown(md) {
  const out = [];
  const lines = md.replace(/\r\n/g, "\n").split("\n");
  let i = 0;
  let list = null; // "ul" | "ol" while inside a list

  const closeList = () => {
    if (list) out.push(`</${list}>`);
    list = null;
  };

  while (i < lines.length) {
    const line = lines[i];

    if (/^```/.test(line)) {
      closeList();
      const code = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i])) code.push(lines[i++]);
      i++; // closing fence (or EOF)
      out.push(`<pre class="lui-code">${escapeHTML(code.join("\n"))}</pre>`);
      continue;
    }

    const heading = line.match(/^(#{1,4})\s+(.*)$/);
    if (heading) {
      closeList();
      const level = heading[1].length;
      out.push(`<h${level}>${mdInline(escapeHTML(heading[2]))}</h${level}>`);
      i++;
      continue;
    }

    const item = line.match(/^\s*([-*]|\d+\.)\s+(.*)$/);
    if (item) {
      const kind = /^\d+\.$/.test(item[1]) ? "ol" : "ul";
      if (list !== kind) {
        closeList();
        out.push(`<${kind}>`);
        list = kind;
      }
      out.push(`<li>${mdInline(escapeHTML(item[2]))}</li>`);
      i++;
      continue;
    }

    if (!line.trim()) {
      closeList();
      i++;
      continue;
    }

    // paragraph — consume consecutive plain lines
    closeList();
    const para = [line];
    while (
      i + 1 < lines.length &&
      lines[i + 1].trim() &&
      !/^(#{1,4}\s|```|\s*([-*]|\d+\.)\s)/.test(lines[i + 1])
    ) {
      para.push(lines[++i]);
    }
    out.push(`<p>${mdInline(escapeHTML(para.join("\n")))}</p>`);
    i++;
  }
  closeList();
  return out.join("\n");
}

// ── runs ───────────────────────────────────────────────────────────────────
async function refreshRuns() {
  if (state.view !== "runs") return;
  const q = state.filter ? `?state=${encodeURIComponent(state.filter)}` : "";
  let runs;
  try {
    runs = await getJSON(`api/runs${q}`);
  } catch (e) {
    return; // transient; the next poll retries
  }
  renderRunList(runs);
  if (state.selected) await refreshDetail();
}

function renderRunList(runs) {
  const body = $("#run-rows");
  $("#runs-empty").hidden = runs.length > 0;
  body.replaceChildren();

  for (const run of runs) {
    const row = el("tr", "run-row");
    row.dataset.runId = run.run_id;
    if (run.run_id === state.selected) row.classList.add("is-selected");

    const stateCell = el("td");
    stateCell.appendChild(badge(run.state));
    const dur = fmtDuration(run.created_at, run.ended_at);

    row.appendChild(stateCell);
    row.appendChild(el("td", "mono run-id", run.run_id));
    row.appendChild(el("td", null, run.workflow));
    row.appendChild(el("td", "mono", run.entry_event || "—"));
    row.appendChild(el("td", "run-time", fmtTime(run.created_at)));
    row.appendChild(el("td", "num run-time", dur || "—"));

    row.addEventListener("click", () => selectRun(run.run_id));
    body.appendChild(row);
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
    detail = await getJSON(`api/runs/${encodeURIComponent(state.selected)}`);
  } catch (e) {
    return;
  }
  renderDetail(detail);
}

function renderDetail(d) {
  $("#detail").hidden = false;
  const body = $("#detail-body");
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

// ── sensors ──────────────────────────────────────────────────────────────
async function loadSensors() {
  const root = $("#sensors-body");
  root.replaceChildren();
  let d;
  try {
    d = await getJSON("api/sensors");
  } catch (e) {
    root.appendChild(el("div", "empty", "Could not load sensors."));
    return;
  }
  if (!d.manifest_present) return;

  if (!d.sensors.length) {
    root.appendChild(el("div", "empty", "No sensors defined."));
    return;
  }
  const grid = el("div", "grid");
  for (const s of d.sensors) grid.appendChild(sensorCard(s));
  root.appendChild(grid);
}

function sensorCard(s) {
  const card = el("div", "lui-card entity");
  const t = el("div", "entity-title");
  t.appendChild(el("span", "name", s.name));
  t.appendChild(badge(s.kind, "is-accent"));
  card.appendChild(t);

  // The function signature, with the emitted return type — the focus of this view.
  card.appendChild(el("pre", "lui-code sig", s.signature));

  const kv = el("dl", "kv");
  addKV(kv, "emits", chip(s.emits));
  addKV(kv, "source", el("code", null, s.qualname));
  if (s.kind === "poll") {
    addKV(kv, "every", el("code", null, s.interval));
    addKV(kv, "last run", el("span", null, fmtTime(s.last_run)));
    addKV(kv, "next run", el("span", "next-run", `${fmtTime(s.next_run)} · ${fmtRel(s.next_run)}`));
  } else if (s.kind === "webhook") {
    addKV(kv, "path", el("code", null, s.path || "—"));
  }
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
    d = await getJSON("api/workflows");
  } catch (e) {
    root.appendChild(el("div", "empty", "Could not load workflows."));
    return;
  }
  if (!d.manifest_present) return;

  const cron = d.workflows.filter((w) => w.trigger && w.trigger.kind === "cron");
  const evented = d.workflows.filter((w) => !w.trigger || w.trigger.kind !== "cron");

  if (cron.length) {
    root.appendChild(el("div", "subhead", "Scheduled (cron)"));
    for (const wf of cron) root.appendChild(workflowCard(wf));
  }
  if (evented.length) {
    if (cron.length) root.appendChild(el("div", "subhead", "Event-triggered"));
    for (const wf of evented) root.appendChild(workflowCard(wf));
  }
}

function workflowCard(wf) {
  const card = el("div", "lui-card wf");
  const head = el("div", "wf-head");
  head.appendChild(el("span", "wf-name", wf.name));
  const meta = el("div", "wf-meta");
  meta.appendChild(el("span", "wf-trigger", triggerLabel(wf.trigger)));
  if (wf.schedule) {
    const next = wf.schedule.next_run;
    meta.appendChild(el("span", "wf-next", next ? `next ${fmtTime(next)} · ${fmtRel(next)}` : ""));
  }
  head.appendChild(meta);
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
    for (const name of gen) col.appendChild(stepNode(wf, byName[name], name === wf.entry));
    dag.appendChild(col);
  });
  card.appendChild(dag);
  return card;
}

function stepNode(wf, st, isEntry) {
  const node = el("button", `node${isEntry ? " is-entry" : ""}`);
  node.type = "button";
  node.dataset.stepKey = `${wf.name}/${st.name}`;
  if (node.dataset.stepKey === state.step) node.classList.add("is-selected");
  node.appendChild(el("div", "node-name", st.name));
  if (st.agent) node.appendChild(el("div", "node-agent", `agent · ${st.agent}`));
  const meta = el("div", "node-meta");
  (st.outputs || []).forEach((o) => meta.appendChild(chip(o.name)));
  (st.emits || []).forEach((e) => meta.appendChild(badge(e, "is-accent")));
  if (meta.childNodes.length) node.appendChild(meta);
  node.addEventListener("click", () => selectStep(wf, st));
  return node;
}

// ── step panel — the markdown body of the selected step ─────────────────
function selectStep(wf, st) {
  const key = `${wf.name}/${st.name}`;
  if (state.step === key) {
    closeStepPanel();
    return;
  }
  state.step = key;
  document.querySelectorAll(".node").forEach((n) =>
    n.classList.toggle("is-selected", n.dataset.stepKey === key)
  );

  $("#step-panel-wf").textContent = wf.name;
  $("#step-panel-name").textContent = st.name;
  const body = $("#step-panel-body");
  body.replaceChildren();

  const facts = el("div", "step-facts");
  if (st.agent) facts.appendChild(badge(`agent · ${st.agent}`, "is-accent"));
  if (st.trigger) facts.appendChild(badge(triggerLabel(st.trigger)));
  if (st.after && st.after.length) facts.appendChild(badge(`after ${st.after.join(", ")}`));
  (st.emits || []).forEach((e) => facts.appendChild(badge(`emits ${e}`, "is-accent")));
  if (facts.childNodes.length) body.appendChild(facts);

  const md = el("div", "step-md");
  if (st.body && st.body.trim()) {
    md.innerHTML = renderMarkdown(st.body); // renderMarkdown escapes all input
  } else {
    md.appendChild(el("div", "muted", "This step has no markdown body."));
  }
  body.appendChild(md);

  if (st.outputs && st.outputs.length) {
    const sec = section("Outputs");
    for (const f of st.outputs) {
      const row = el("div", "fieldrow");
      row.appendChild(el("span", "fname", f.name));
      let type = f.type || "any";
      if (f.format) type += `<${f.format}>`;
      if (f.enum) type = `enum[${f.enum.join(", ")}]`;
      row.appendChild(el("span", "ftype", type));
      sec.appendChild(row);
    }
    body.appendChild(sec);
  }

  if (st.refs && st.refs.length) {
    const sec = section("Reads");
    sec.appendChild(tags(st.refs.map((r) => `${r.producer}.${r.field}`)));
    body.appendChild(sec);
  }

  $("#step-panel").hidden = false;
}

function closeStepPanel() {
  state.step = null;
  $("#step-panel").hidden = true;
  document.querySelectorAll(".node.is-selected").forEach((n) => n.classList.remove("is-selected"));
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
    d = await getJSON("api/registry");
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
  if (state.view !== "workflows") closeStepPanel();
  for (const v of VIEWS) $(`#view-${v}`).hidden = v !== state.view;
  document.querySelectorAll(".view-link").forEach((l) =>
    l.classList.toggle("is-active", l.dataset.view === state.view)
  );

  const needsManifest = MANIFEST_VIEWS.includes(state.view);
  $("#no-manifest").hidden = !(needsManifest && state.manifest === false);

  if (state.view === "runs") await refreshRuns();
  else if (state.view === "workflows") await loadWorkflows();
  else if (state.view === "sensors") await loadSensors();
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
    const meta = await getJSON("api/meta");
    state.manifest = !!meta.manifest_present;
    state.demo = !!meta.demo;
  } catch (e) {
    state.manifest = null;
    state.demo = false;
  }
  // Flag a demo server (fake in-memory data) so it can't be mistaken for a real deployment.
  $("#demo-banner").hidden = !state.demo;
  $("#demo-tag").hidden = !state.demo;
}

async function init() {
  initFilters();
  $("#step-panel-close").addEventListener("click", closeStepPanel);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && state.step) closeStepPanel();
  });
  await loadMeta();
  window.addEventListener("hashchange", route);
  await route();
  // Only the runs view auto-refreshes; the others are static templates.
  setInterval(() => { if (state.view === "runs") refreshRuns(); }, POLL_MS);
}

init();
