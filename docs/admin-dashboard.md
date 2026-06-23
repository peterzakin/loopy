# Admin dashboard

The control-plane dashboard (`loopy admin`) is a **read-only** viewer over a Loopy
deployment. It answers two questions:

- **What is defined?** — the static system, read from the compiled `manifest.json`
  (workflow templates, sensors, the registry).
- **What happened?** — run history, read from the run-state DB that `loopy run` writes
  (`.loopy/state.db` by default).

It never writes, and it **never serves secret values**.

```
loopy compile .                 # produces manifest.json
loopy run                       # executes; writes .loopy/state.db
loopy admin                     # serves the dashboard on http://127.0.0.1:9000
loopy admin --manifest path.json --port 9000 db.sqlite
```

The run views work with just the DB; the template/sensor/registry views light up when a
`manifest.json` is present (default: `./manifest.json`).

## Requirements

### 1. Run history
- List runs newest-first, filterable by state (`all` / `running` / `completed` / `failed`).
- Per-run detail: derived state, workflow, entry event, duration, error (if any), the
  emitted events, the full event-sourced timeline, and each step's validated output.
- The runs view auto-refreshes (3s poll); the manifest-backed views are static.

### 2. Workflow templates (the full DAG)
- Each workflow rendered as a DAG: steps grouped into topological layers (by their `after`
  dependencies) with the edges between them. The entry step is marked, and each node shows
  its agent, outputs, and emitted events.
- Workflows are split into two sections: **Scheduled (cron)** — workflows whose entry step
  has an `on: cron(...)` trigger, each showing its expression/timezone plus last fire (the
  stored watermark) and computed next fire — and **Event-triggered**.

### 3. Sensors
- Every sensor with its **function signature** and **emitted event** —
  `def metric_watch(req) -> MetricThreshold`.
- **Poll sensors** also show their interval and last/next fire; **webhook sensors** show
  their inbound path.

### 4. Registry entities
- **Agents** — runtime, model, sandbox, skills.
- **Sandboxes** — provider, image, network allowlist, repos. **Secrets redacted**: a
  sandbox's `env_file` references are never sent to the browser — only a count
  (`🔒 N env files (hidden)`).
- **Events** — each event contract's fields with types/formats/enums.
- **Limits** — cascade and per-workflow spend caps, when configured.

> **Secrets boundary.** The manifest holds secret *references* (paths), never values; the
> dashboard API strips even the references. No endpoint returns `env_file`, and a regression
> test (`test_build_registry_redacts_secrets`) asserts the paths never appear in the payload.

## Design system

The dashboard is visually consistent with the marketing site (`loopy-landing`). Both consume
a shared design system, **`loopy-ui.css`** — the `:root` token contract (colors, type,
spacing, elevation, radii) plus a small set of `lui-`-namespaced primitives (`lui-card`,
`lui-badge`, `lui-chip`, `lui-btn`, `lui-dot`, `lui-code`, `lui-tab`). The file is authored in
`loopy-landing` and vendored byte-for-byte into `loopy_runtime/dashboard/static/`; keep the
two copies in sync.

## API surface

| Endpoint | Purpose |
|----------|---------|
| `GET /api/runs?state=&limit=&offset=` | run list (newest first) |
| `GET /api/runs/{run_id}` | one run's full detail |
| `GET /api/meta` | system summary + whether a manifest is loaded |
| `GET /api/workflows` | workflow templates as DAGs (cron workflows carry their schedule) |
| `GET /api/registry` | agents / sandboxes / events / limits (secrets redacted) |
| `GET /api/sensors` | sensors: signature + emitted event (poll sensors carry last/next run) |

All view-shaping lives in `loopy_runtime/dashboard/views.py` as pure, unit-tested functions;
the FastAPI handlers in `app.py` stay thin.
