"""Backend-for-frontend proxy for `loopy admin --remote`.

The dashboard's remote mode splits the surface by concern: the front-end (`/` and `/static/*`)
is served from the local install, while only `/api/*` — the run data — is proxied to the remote
control plane with `Authorization: Bearer` injected from the local process env. That keeps the
token in this process — the browser never holds a credential (no XSS exposure), and it never
rides a URL (no access-log leak).

Serving the whole front-end locally (rather than proxying `/static` to the engine) means a
`loopy` refresh delivers the latest dashboard UI with no engine redeploy: the UI ships in the
CLI, the engine owns only the data, and `/api/*` is the versioned contract app.js is written
against. The engine still serves its own copy of these assets under `/admin` for any direct
hit, but the supported path is this client, so the two never need to agree byte-for-byte.

The proxy is read-only by construction: only GET is routed. Upstream failures are translated
into actionable errors — a remote 401 names `LOOPY_ADMIN_TOKEN`, a connection/TLS failure
names the URL — instead of surfacing as blank panels.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from loopy_runtime.dashboard.app import _STATIC
from loopy_runtime.dashboard.auth import is_loopback_host
from loopy_runtime.secrets import ADMIN_TOKEN_ENV


def validate_remote_url(url: str) -> str:
    """Normalize and vet a `--remote` URL; raises ValueError with an actionable message.

    Plain HTTP is refused unless the remote host is itself loopback (a local dev server):
    the bearer token must never cross the network unencrypted (guardrail 3, TLS only).
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(
            f"--remote must be a full http(s) URL like https://loopy.example.com (got {url!r})"
        )
    if parts.scheme == "http" and not is_loopback_host(parts.hostname):
        raise ValueError(
            f"refusing to send {ADMIN_TOKEN_ENV} over plain HTTP to {parts.hostname!r} — "
            "use https:// (the platform ingress terminates TLS)"
        )
    return url.rstrip("/")


def create_proxy_app(
    remote_url: str, token: str, *, transport: httpx.AsyncBaseTransport | None = None
) -> FastAPI:
    """The local admin client: serve the front-end locally, proxy only `/api` with the bearer.

    `transport` exists for tests (an `httpx.MockTransport` stands in for the network).
    """
    base = remote_url.rstrip("/")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        await app.state.client.aclose()

    app = FastAPI(title="Loopy admin (remote)", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.client = httpx.AsyncClient(
        base_url=base,
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
        transport=transport,
    )

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(_STATIC / "index.html")

    async def forward(request: Request) -> Response:
        try:
            upstream = await app.state.client.get(
                request.url.path, params=list(request.query_params.multi_items())
            )
        except httpx.HTTPError as exc:
            return JSONResponse(
                status_code=502,
                content={"detail": f"could not reach the remote control plane at {base}: {exc}"},
            )
        if upstream.status_code == 401:
            return JSONResponse(
                status_code=401,
                content={
                    "detail": f"auth failed: {base} rejected the token — check "
                    f"{ADMIN_TOKEN_ENV} (and whether it was rotated)"
                },
            )
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type"),
        )

    @app.get("/api/{path:path}")
    async def api(request: Request, path: str) -> Response:
        return await forward(request)

    # The front-end ships in this package, so serve it from disk — the assets are byte-identical
    # to the engine's own copy and carry no run data, so there's nothing to authenticate and no
    # reason to round-trip to the engine (a stale deploy can't leave the dashboard unstyled).
    # Mounted last so it never shadows the `/api` route above.
    app.mount("/static", StaticFiles(directory=_STATIC), name="static")

    return app
