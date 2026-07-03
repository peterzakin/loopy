"""Backend-for-frontend proxy for `loopy admin --remote` (`docs/design/admin-auth.md`).

The dashboard's remote mode splits the surface: the UI shell (`/`) is served from the local
install, while `/api/*` and `/static/*` are proxied to the remote control plane with
`Authorization: Bearer` injected from the local process env. That keeps the token in this
process — the browser never holds a credential (no XSS exposure), and it never rides a URL
(no access-log leak). Proxying `/static` (rather than serving local assets) keeps the app
code version-matched to the API it talks to.

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
    """The local admin client: serve the UI shell, proxy `/api` + `/static` with the bearer.

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

    @app.get("/static/{path:path}")
    async def static(request: Request, path: str) -> Response:
        return await forward(request)

    return app
