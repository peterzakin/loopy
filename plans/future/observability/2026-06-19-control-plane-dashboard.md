# Control-plane dashboard (run observability frontend)

**Status:** draft
**Owner:** peter
**Date:** 2026-06-19

## Goal

Give operators a simple web UI to see what runs have happened and what occurred inside
each one — run list with state (running/completed/failed), per-run step timeline, emitted
events, step outputs, and the error on a failure. The dashboard is its own HTTP server,
started as a separate process via `loopy admin`, reading from a shared on-disk store
that `loopy run` writes to. This is the first concrete delivery against backend capability
**B12 (Observability)** beyond `status()` / `failed_runs`.

## Context

Today all run state is **in-process and ephemeral**:

- `InMemoryRuntime._runs: dict[RunId, RunStatus]` holds terminal status per run, in memory.
- `InMemoryStateStore` (`loopy_runtime/state/inmemory.py`) holds the event-sourced
  `history` and `outputs`, also in memory, lost on restart.
- The `StateStore` Protocol (`contract.py` §B8/B10/B11) exposes per-`run_id` reads only —
  `history(run_id)`, `outputs(run_id)` — and **no way to enumerate runs**. `Runtime` itself
  only has `status(run_id)` plus the in-memory-only `failed_runs` property.

So a genuinely separate dashboard process cannot see anything: there is no shared store and
no "list runs" call. The Redis broker shipped (`past/redis-broker/`) but it's an `EventBus`,
not a durable `StateStore`; durable run recovery is still B10. `loopy.yaml` config already
**reserves a top-level `state:` key** ("TODO #2 state", see `loopy_runtime/config.py`
`_RESERVED_TOP_LEVEL`) — the intended home for selecting a state backend.

**Decision (2026-06-19, peter):** the dashboard is a *separate process backed by a shared
SQLite file*, not an embedded server inside `loopy run`. Rationale: it matches the "own
server" mental model, the dashboard survives `loopy run` restarts, and a file-backed
`StateStore` is a real step toward B8/B10 durability — not throwaway scaffolding. The
embedded-in-`loopy run` alternative was rejected as a dead end (state still dies with the
process, no history across restarts).

### Key design conclusions

1. **A shared, durable `StateStore` is the substrate — the dashboard is a thin reader on
   top.** Everything the UI shows is already produced by the runtime as `RunEvent`s and
   `StepOutput`s; we only need to (a) persist them to a place a second process can read and
   (b) add the one missing query — enumerate runs.

2. **SQLite is the right v1 store.** Stdlib (no new dependency), single-file, and supports a
   concurrent writer (`loopy run`) + reader (`loopy admin`) on one host under WAL mode.
   It is explicitly single-host; a networked store (Redis/Postgres) for multi-host is a
   later B10/B11 concern behind the same Protocol, out of scope here.

3. **Run state is *derived from history*, not separately persisted.** The event-sourced
   `history` already encodes the full story: `run_started` → `step_completed`* /
   `event_emitted`* → `run_completed` | `run_failed` (with `error`). The dashboard derives
   the `RunStatus` shape from these kinds, so we don't introduce a second source of truth.
   A denormalized `runs` summary table is kept only as a read-optimization for the list view
   (cheap to maintain on `create_run` / terminal append).

4. **The only Protocol change is additive: `list_runs()`.** Adding it to the `StateStore`
   Protocol (and to the in-memory impl, for parity + tests) keeps backends interchangeable.

5. **Reuse the existing web stack.** `fastapi` + `uvicorn` are already core deps (used by
   `FastAPISensorRunner`). The frontend is a single static HTML page + vanilla JS that polls
   the REST API — no build step, no JS toolchain, matching "simple".

## Constraints & non-goals

- **Constraints**
  - No new runtime dependencies for the store (SQLite is stdlib `sqlite3`); reuse
    `fastapi`/`uvicorn` already present for the server.
  - The dashboard is **read-only** — it never mutates run state or triggers runs.
  - Writer/reader concurrency on one host must be safe: SQLite in **WAL** mode, short
    transactions, reader opens the DB read-only (`mode=ro`).
  - Match conventions: Typer command with lazily-imported heavy deps (keep `loopy compile`
    runtime-free), Python 3.11+, ruff (line length 100), `StateStore` Protocol unbroken.
  - Single new state backend must implement the **full existing** `StateStore` Protocol
    (watermarks, dedupe `seen`/`mark_seen`, outputs, history), not just the read paths —
    otherwise `loopy run` can't use it as its one StateStore.

- **Non-goals (explicitly out of scope)**
  - **OpenTelemetry / metrics / structured-log export** — deferred (separate B12 effort).
  - **Capturing raw agent stdout/stderr** per step. History records step *outcomes* and
    validated *outputs*, not the agent's console log; surfacing raw logs needs the harness to
    persist them and is its own milestone. Note it as a follow-up.
  - **Auth / multi-tenant / exposed-to-internet hardening.** v1 binds localhost; auth is a
    later concern (mirrors sensor-ingress: trusted by co-location first).
  - **Live push (WebSocket/SSE).** v1 UI **polls**; push is a nice-to-have follow-up.
  - **Multi-host / networked state.** SQLite is single-host by design; Redis/Postgres store
    behind the same Protocol is future B10/B11 work.
  - **Editing/retrying/cancelling runs from the UI.** Read-only only.

## Approach

Three layers, built bottom-up so each is independently testable:

1. **Persistence** — a `SqliteStateStore` implementing the `StateStore` Protocol, plus a new
   `list_runs()` query added to the Protocol and to both stores. `loopy run` selects it via
   the reserved `state:` config block (or a `--state` flag), sharing the one instance between
   runtime and bus exactly as `InMemoryStateStore` is shared today.

2. **Read API** — a small FastAPI app (`loopy_runtime/dashboard/`) that opens the SQLite file
   read-only and serves JSON: `GET /api/runs` (list + filter) and `GET /api/runs/{run_id}`
   (derived status + history + outputs). A `RunView` builder turns `history`+`outputs` into
   the view shape, unit-testable without HTTP.

3. **Frontend + CLI** — a single static HTML/JS page served by the same app (list view →
   click a run → detail view; polls the API for refresh), and a `loopy admin` Typer
   command that points at the DB file and runs uvicorn.

### Proposed shape (sketches, not final)

`StateStore` Protocol gains one method (`contract.py`):

```python
@dataclass(frozen=True)
class RunSummary:
    run_id: RunId
    workflow: str            # parsed from run_id prefix / recorded at create_run
    state: str               # running | completed | failed (derived)
    entry_event: EventName
    created_at: datetime
    ended_at: datetime | None
    error: str | None

@runtime_checkable
class StateStore(Protocol):
    ...  # existing methods unchanged
    async def list_runs(
        self, *, limit: int = 100, offset: int = 0, state: str | None = None
    ) -> list[RunSummary]: ...
```

SQLite schema (one file, WAL):

```sql
CREATE TABLE runs (
  run_id TEXT PRIMARY KEY, workflow TEXT, manifest_version TEXT,
  entry_event TEXT, created_at TEXT, state TEXT, ended_at TEXT, error TEXT
);
CREATE TABLE run_events (
  run_id TEXT, seq INTEGER, kind TEXT, step_id TEXT, payload TEXT, at TEXT,
  PRIMARY KEY (run_id, seq)
);
CREATE TABLE step_outputs (run_id TEXT, step_id TEXT, fields TEXT,
  PRIMARY KEY (run_id, step_id));
CREATE TABLE watermarks (trigger_id TEXT PRIMARY KEY, ts TEXT);
CREATE TABLE seen (key TEXT PRIMARY KEY);
```

`runs.state` flips to `completed`/`failed` (+ `ended_at`, `error`) when a `run_completed` /
`run_failed` event is appended — derivation stays single-sourced in history; `runs` is the
index. JSON payloads/outputs stored as TEXT (mirrors how `trigger --json` serializes today).

Read API (`loopy_runtime/dashboard/app.py`):

```
GET /api/runs?state=failed&limit=50   -> [RunSummary, ...]   (newest first)
GET /api/runs/{run_id}                -> {summary, history:[RunEvent], outputs:{step:fields}}
GET /                                 -> static index.html (list + detail, polls the API)
```

CLI:

```python
@app.command()
def admin(
    db: Path = typer.Argument(..., help="Path to the loopy state DB (e.g. loopy-state.db)."),
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(9000, "--port"),
) -> None:
    """Serve the read-only control-plane dashboard over the shared state DB."""
    import asyncio, uvicorn
    from loopy_runtime.dashboard.app import create_app
    asyncio.run(uvicorn.Server(uvicorn.Config(create_app(db), host=host, port=port)).serve())
```

`loopy run` wiring: where it does `state = InMemoryStateStore()` today, select the backend
from the resolved `state:` config / `--state` flag (default `inproc` → unchanged behavior;
`sqlite` → `SqliteStateStore(path)`), then share that one instance with runtime + bus as now.

## Steps

- [ ] **Stage 1 — Protocol + stores.** Add `RunSummary` + `list_runs()` to `contract.py`;
      implement `list_runs()` on `InMemoryStateStore`. Add tests.
- [ ] **Stage 2 — SqliteStateStore.** New `loopy_runtime/state/sqlite.py` implementing the
      full `StateStore` Protocol (history/outputs/watermarks/seen/dedupe + `list_runs`), WAL
      mode, schema bootstrap on open. Maintain the `runs` summary row on `create_run` and on
      terminal `append`. Conformance tests run the *same* suite against both stores.
- [ ] **Stage 3 — Wire into `loopy run`.** Parse the reserved `state:` config block (+ `--state`
      / `--state-path` flags) in `config.py`/CLI; construct the chosen store; keep default
      `inproc` behavior byte-for-byte. Smoke test: run a workflow with `--state sqlite`, confirm
      rows land.
- [ ] **Stage 4 — Read API.** `loopy_runtime/dashboard/app.py`: `create_app(db)` opens SQLite
      read-only; `RunView` builder (pure) turns history+outputs into the detail shape; wire the
      two `/api/...` routes. Unit-test the builder + routes with a seeded DB.
- [ ] **Stage 5 — Frontend.** Single `index.html` (+ small `app.js`, `style.css`) served as
      static assets: run list (state badge, workflow, time), click → detail (step timeline,
      emitted events, outputs, error). Poll every few seconds for refresh.
- [ ] **Stage 6 — CLI + docs.** Add `loopy admin` command (lazy imports). Document the
      flow in `DEPLOYMENT.md` (run with `--state sqlite`, then `loopy admin <db>`); flip
      TODOS.md B12 row from ⚠️ toward the dashboard milestone; graduate the durable-store
      decision into `ARCHITECTURE.md`.
- [ ] **Stage 7 — On merge.** `git mv` this plan to `plans/past/observability/`.

## Files likely to change

- `loopy_runtime/contract.py` — add `RunSummary` + `StateStore.list_runs()` (additive).
- `loopy_runtime/state/inmemory.py` — implement `list_runs()`.
- `loopy_runtime/state/sqlite.py` — **new**: SQLite-backed `StateStore`.
- `loopy_runtime/config.py` — parse the reserved `state:` block (backend + path).
- `loopy_cli/__init__.py` — `loopy run` store selection; new `loopy admin` command.
- `loopy_runtime/dashboard/__init__.py`, `app.py`, `views.py` — **new**: read API + view builder.
- `loopy_runtime/dashboard/static/{index.html,app.js,style.css}` — **new**: the UI.
- `tests/` — store conformance suite (both stores), `list_runs`, view builder, API routes.
- `DEPLOYMENT.md`, `ARCHITECTURE.md`, `TODOS.md` — docs + B12 status.

## Open questions

- **Run→workflow on the summary.** `run_id` is `f"{wf_name}-{seq}"`, so workflow is parseable
  from the prefix, but a workflow name could contain `-`. Cleaner to record `workflow`
  explicitly at `create_run` — do we extend the `StateStore.create_run` signature, or stash it
  in the `run_started` payload? (Leaning: add it to the `run_started` `RunEvent` payload to
  avoid a Protocol signature change.)
- **Default DB path / discovery.** Should `loopy admin` default to a conventional path
  (e.g. `./loopy-state.db`) so it pairs with a `loopy run --state sqlite` default, or always
  require the path argument explicitly? (Leaning: explicit arg in v1.)
- **Live-ish refresh.** Is polling every ~3s acceptable for v1, or is SSE/WebSocket wanted
  soon enough to design the API for it now? (Leaning: poll v1, note SSE as follow-up.)
- **In-flight visibility.** A `running` run only has a `runs` row + partial history mid-flight;
  confirm the writer commits per-`append` (not per-run) so the dashboard sees steps as they
  complete. (Leaning: commit per append — small writes, WAL makes this cheap.)

## Notes / decisions

- 2026-06-19 — Chose separate-process + shared **SQLite** store over embedding the dashboard in
  `loopy run` (peter). Embedded was rejected: state would still die with the process and give no
  cross-restart history. SQLite doubles as a first durable `StateStore` step toward B8/B10.
- 2026-06-19 — OTel explicitly **out of scope** for this milestone (per request).
- 2026-06-19 — Dashboard is **read-only**; reuse existing `fastapi`/`uvicorn`; frontend is a
  static page (no JS build step).
