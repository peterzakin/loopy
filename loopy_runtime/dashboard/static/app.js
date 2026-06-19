// Loopy control-plane dashboard — a read-only viewer over /api/runs.
// Vanilla JS, no build step: poll the API, render the run list + selected run detail.

"use strict";

const POLL_MS = 3000;

const state = {
  filter: "", // "" | running | completed | failed
  selected: null, // run_id of the open detail, or null
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

function badge(stateName) {
  return `<span class="badge ${stateName}">${stateName}</span>`;
}

// ── run list ─────────────────────────────────────────────────────────────
async function refreshList() {
  const q = state.filter ? `?state=${encodeURIComponent(state.filter)}` : "";
  let runs;
  try {
    runs = await getJSON(`/api/runs${q}`);
  } catch (e) {
    return; // transient; the next poll retries
  }
  renderList(runs);
}

function renderList(runs) {
  const list = $("#run-list");
  $("#run-count").textContent = runs.length ? `${runs.length}` : "";
  $("#runs-empty").hidden = runs.length > 0;
  list.replaceChildren();

  for (const run of runs) {
    const row = el("div", "run-row");
    row.dataset.runId = run.run_id;
    if (run.run_id === state.selected) row.classList.add("is-selected");

    const left = el("div");
    left.innerHTML = badge(run.state);

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

// ── detail ───────────────────────────────────────────────────────────────
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

  // header
  const head = el("div", "detail-head");
  const title = el("div", "detail-title");
  title.innerHTML = `<h2>${d.run_id}</h2>${badge(d.state)}`;
  head.appendChild(title);
  const dur = fmtDuration(d.created_at, d.ended_at);
  const sub = el("div", "detail-sub");
  sub.innerHTML =
    `<b>${d.workflow}</b> · triggered by <b>${d.entry_event ?? "—"}</b> · ` +
    `${fmtTime(d.created_at)}${dur ? ` · ran ${dur}` : ""}`;
  head.appendChild(sub);
  body.appendChild(head);

  if (d.error) {
    body.appendChild(el("div", "error-box", d.error));
  }

  // emitted events
  if (d.emitted && d.emitted.length) {
    const sec = section("Emitted events");
    const chips = el("div", "chips");
    for (const ev of d.emitted) chips.appendChild(el("span", "chip", ev));
    sec.appendChild(chips);
    body.appendChild(sec);
  }

  // timeline
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

  // outputs
  const outSec = section("Step outputs");
  const steps = Object.keys(d.outputs || {});
  if (!steps.length) {
    outSec.appendChild(el("div", "muted", "No recorded outputs."));
  } else {
    for (const step of steps) {
      const o = el("div", "output");
      o.appendChild(el("div", "output-step", step));
      o.appendChild(el("pre", "json", JSON.stringify(d.outputs[step], null, 2)));
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

// ── wiring ───────────────────────────────────────────────────────────────
function initFilters() {
  $("#filters").addEventListener("click", (e) => {
    const btn = e.target.closest(".filter");
    if (!btn) return;
    state.filter = btn.dataset.state;
    document.querySelectorAll(".filter").forEach((b) => b.classList.toggle("is-active", b === btn));
    refreshList();
  });
}

async function tick() {
  await refreshList();
  await refreshDetail();
}

initFilters();
tick();
setInterval(tick, POLL_MS);
