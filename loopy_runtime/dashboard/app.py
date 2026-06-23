"""The read-only control-plane dashboard API (B12).

`create_app(store, manifest=None)` builds a FastAPI app that serves run state — and, when a
compiled manifest is supplied, the static system definition too — over JSON:

    GET /api/runs?state=&limit=&offset=   the run list (newest first)
    GET /api/runs/{run_id}                one run's full detail (history + outputs)
    GET /api/meta                         system summary + whether a manifest is loaded
    GET /api/workflows                    workflow templates as DAGs (+ cron schedule) + lineage
    GET /api/registry                     agents / sandboxes / events / limits (secrets redacted)
    GET /api/sensors                      sensors: signature + emitted event (+ poll last/next)

It takes a `StateStore` (not a DB path) and an optional `Manifest`, so it's testable against the
in-memory store and the `loopy admin` CLI owns opening the SQLite file and loading the manifest.
The app never writes — it's a viewer — and never serves secret values.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from loopy_runtime.contract import StateStore
from loopy_runtime.dashboard.views import (
    VALID_RUN_STATES,
    build_meta,
    build_registry,
    build_run_detail,
    build_sensors,
    build_workflows,
    summary_to_dict,
)
from loopy_runtime.manifest_model import Manifest

_STATIC = Path(__file__).parent / "static"


def create_app(store: StateStore, manifest: Manifest | None = None) -> FastAPI:
    app = FastAPI(title="Loopy control plane", docs_url=None, redoc_url=None)
    app.state.store = store
    app.state.manifest = manifest

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(_STATIC / "index.html")

    @app.get("/api/runs")
    async def list_runs(state: str | None = None, limit: int = 100, offset: int = 0):
        if state is not None and state not in VALID_RUN_STATES:
            raise HTTPException(400, f"state must be one of {VALID_RUN_STATES}")
        runs = await store.list_runs(limit=limit, offset=offset, state=state)
        return [summary_to_dict(s) for s in runs]

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str):
        history = await store.history(run_id)
        if not history:  # an existing run always has at least a run_started entry
            raise HTTPException(404, f"no run {run_id!r}")
        outputs = await store.outputs(run_id)
        return build_run_detail(run_id, history, outputs)

    @app.get("/api/meta")
    async def meta():
        return build_meta(manifest)

    @app.get("/api/workflows")
    async def workflows():
        return await build_workflows(manifest, store)

    @app.get("/api/registry")
    async def registry():
        return build_registry(manifest)

    @app.get("/api/sensors")
    async def sensors():
        return await build_sensors(manifest, store)

    # Mounted last so it doesn't shadow the API routes above.
    app.mount("/static", StaticFiles(directory=_STATIC), name="static")
    return app
