"""The read-only control-plane dashboard API (B12).

`create_app(store, manifest=None, *, auth=None)` builds a FastAPI app that serves run state —
and, when a compiled manifest is supplied, the static system definition too — over JSON:

    GET /api/runs?state=&limit=&offset=   the run list (newest first)
    GET /api/runs/{run_id}                one run's full detail (history + outputs)
    GET /api/meta                         system summary + whether a manifest is loaded
    GET /api/workflows                    workflow templates as DAGs (+ cron schedule) + lineage
    GET /api/registry                     agents / sandboxes / events / limits (secrets redacted)
    GET /api/sensors                      sensors: signature + emitted event (+ poll last/next)
    GET /healthz                          liveness only, no data — open for platform probes

It takes a `StateStore` (not a DB path) and an optional `Manifest`, so it's testable against the
in-memory store and the `loopy admin` CLI owns opening the SQLite file and loading the manifest.
The app never writes — it's a viewer — and never serves secret values. With an `AdminAuth`
supplied, every `/api/*` route requires `Authorization: Bearer` (`docs/design/admin-auth.md`);
run/step outputs are *not* redacted, so a non-loopback bind must pass one. `/`, `/static`, and
`/healthz` stay open — they carry app code and liveness, never run data.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from loopy_runtime.contract import StateStore
from loopy_runtime.dashboard.auth import AdminAuth, is_loopback_host
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


def create_app(
    store: StateStore,
    manifest: Manifest | None = None,
    *,
    auth: AdminAuth | None = None,
    demo: bool = False,
) -> FastAPI:
    app = FastAPI(title="Loopy control plane", docs_url=None, redoc_url=None)
    app.state.store = store
    app.state.manifest = manifest
    app.state.demo = demo

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(_STATIC / "index.html")

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    # With an AdminAuth, the bearer check runs as a dependency — before the handler — on
    # every /api/* route. Applied per route (not via an included router) so `app.routes`
    # stays flat and introspectable; a new /api route must carry `dependencies=guarded`.
    guarded = [Depends(auth.require_admin)] if auth is not None else []

    @app.get("/api/runs", dependencies=guarded)
    async def list_runs(state: str | None = None, limit: int = 100, offset: int = 0):
        if state is not None and state not in VALID_RUN_STATES:
            raise HTTPException(400, f"state must be one of {VALID_RUN_STATES}")
        runs = await store.list_runs(limit=limit, offset=offset, state=state)
        return [summary_to_dict(s) for s in runs]

    @app.get("/api/runs/{run_id}", dependencies=guarded)
    async def get_run(run_id: str):
        history = await store.history(run_id)
        if not history:  # an existing run always has at least a run_started entry
            raise HTTPException(404, f"no run {run_id!r}")
        outputs = await store.outputs(run_id)
        return build_run_detail(run_id, history, outputs)

    @app.get("/api/meta", dependencies=guarded)
    async def meta():
        return build_meta(manifest, demo=demo)

    @app.get("/api/workflows", dependencies=guarded)
    async def workflows():
        return await build_workflows(manifest, store)

    @app.get("/api/registry", dependencies=guarded)
    async def registry():
        return build_registry(manifest)

    @app.get("/api/sensors", dependencies=guarded)
    async def sensors():
        return await build_sensors(manifest, store)

    # Mounted last so it doesn't shadow the API routes above.
    app.mount("/static", StaticFiles(directory=_STATIC), name="static")
    return app


def mount_admin(
    parent: FastAPI,
    store: StateStore,
    manifest: Manifest | None = None,
    *,
    host: str,
    env: Mapping[str, str],
) -> str | None:
    """Mount the dashboard under `/admin` on the engine's webhook server.

    This is what makes the admin URL deterministic across providers: webhooks and dashboard
    share one hostname and one port, path-routed — deliveries at `$LOOPY_PUBLIC_URL/hooks/*`,
    the dashboard at `$LOOPY_PUBLIC_URL/admin` — with no reverse proxy or second service.

    Mirrors `loopy admin`'s own bind policy: a loopback bind mounts it open (the dev loop);
    a non-loopback bind mounts it only when `LOOPY_ADMIN_TOKEN` is configured, and otherwise
    doesn't mount at all — fail-closed by absence, so the engine still serves webhooks but
    run data is never exposed openly. Returns a short description of the mount ("open
    (loopback)" / "bearer auth"), or None when not mounted.
    """
    if is_loopback_host(host):
        auth = None  # loopback is trusted, same as local `loopy admin`
    else:
        auth = AdminAuth.from_env(env)
        if auth is None:
            return None
    parent.mount("/admin", create_app(store, manifest, auth=auth))
    return "open (loopback)" if auth is None else "bearer auth"
