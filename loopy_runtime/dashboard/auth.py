"""Edge auth for the dashboard's `/api/*` surface.

A single symmetric bearer token, checked *before* any handler runs. The five guardrails:

1. **Constant-time compare** — `secrets.compare_digest`, never `==` (a naive compare is a
   timing oracle that leaks the secret byte by byte).
2. **High entropy** — `generate_admin_token` mints 256 bits from the CSPRNG, prefixed
   `loopy_sk_` so secret scanners can match it.
3. **TLS only** — terminated by the platform ingress, not here; the *client* side
   (`loopy_runtime.dashboard.proxy`) refuses to send the token over plain HTTP to a
   non-loopback host.
4. **Read-only scope** — inherent: the app has no write routes.
5. **Fail-closed** — the serve entry (`loopy admin`) refuses a non-loopback bind without a
   configured token; `is_loopback_host` is the shared predicate.

Rotation: the server accepts `LOOPY_ADMIN_TOKEN` and `LOOPY_ADMIN_TOKEN_NEXT` at once, so
the dev machine and the platform env can roll without a mid-session lockout. Token values never
appear in logs or reprs.
"""

from __future__ import annotations

import ipaddress
import secrets
from collections.abc import Mapping, Sequence

from fastapi import HTTPException, Request

from loopy_runtime.secrets import ADMIN_TOKEN_ENV, ADMIN_TOKEN_NEXT_ENV

# Prefix for issued tokens, so secret scanners (and greps) can match a leaked one.
TOKEN_PREFIX = "loopy_sk_"


def generate_admin_token() -> str:
    """Mint a fresh admin bearer token: 256 bits from the CSPRNG, `loopy_sk_`-prefixed."""
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def is_loopback_host(host: str) -> bool:
    """Whether a bind/connect host stays on this machine (the no-auth-required case)."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:  # a DNS name (or an empty/garbage bind) — assume it leaves the box
        return False


class AdminAuth:
    """Bearer check for `/api/*`: constant-time compare against the configured token(s)."""

    def __init__(self, tokens: Sequence[str]):
        cleaned = tuple(t.strip() for t in tokens if t and t.strip())
        if not cleaned:
            raise ValueError("AdminAuth needs at least one non-empty token")
        self._tokens = cleaned

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> AdminAuth | None:
        """Build from `LOOPY_ADMIN_TOKEN` (+ `LOOPY_ADMIN_TOKEN_NEXT` during a rotation
        overlap); `None` when no token is configured — the caller decides whether that's
        fine (loopback) or fatal (any other bind)."""
        tokens = [env.get(ADMIN_TOKEN_ENV, ""), env.get(ADMIN_TOKEN_NEXT_ENV, "")]
        if not any(t.strip() for t in tokens):
            return None
        return cls(tokens)

    def __repr__(self) -> str:  # token values must never reach logs or tracebacks
        return f"AdminAuth({len(self._tokens)} token(s) configured)"

    def check(self, authorization: str | None) -> bool:
        """Verify an `Authorization` header value. Constant-time per configured token."""
        if not authorization:
            return False
        scheme, _, candidate = authorization.partition(" ")
        if scheme.lower() != "bearer":
            return False
        candidate = candidate.strip()
        # `any` short-circuits across *slots* (current vs. rotation), never within a token —
        # each comparison itself is constant-time.
        return any(secrets.compare_digest(candidate, t) for t in self._tokens)

    async def require_admin(self, request: Request) -> None:
        """FastAPI dependency: runs before any `/api/*` handler, 401 on missing/invalid."""
        if not self.check(request.headers.get("authorization")):
            raise HTTPException(
                status_code=401,
                detail="missing or invalid bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )
