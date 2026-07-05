"""Verify Datadog webhook deliveries at the ingress.

Unlike GitHub (`X-Hub-Signature-256`) and Sentry (`Sentry-Hook-Signature`), **Datadog signs
no body.** Its Webhooks integration offers only basic-auth-in-URL, OAuth 2.0 (the wrong
direction — Datadog fetching a token from *our* endpoint), and arbitrary custom headers. The
clean fit is a shared secret in a custom header: the canonical webhook config carries
`Authorization: Bearer <secret>`, and we compare that value in constant time. So this verifier
is a **token equality check**, not an HMAC over the request body — the body argument the
runner passes is deliberately unused.

This is a genuinely weaker posture than a per-body HMAC (a captured request can be replayed
verbatim, and the secret is symmetric), and that is a property of Datadog's webhook design,
not a choice here. It is mitigated by TLS at the ingress (the header never travels in clear),
a rotatable secret (`loopy auth datadog --update`), and the fact that a delivery only *starts*
a triage run rather than mutating loopy state. A future hardening — include `$DATE` in the
template and reject stale deliveries — would add a light replay guard; deferred for now.

`SignatureError` is reused from `github_webhook` so the runner's `except SignatureError -> 401`
path is unchanged. The factory is named `token_verifier` (not `signature_verifier`) so a
reader at the call site sees this is a token compare, not a body signature.
"""

from __future__ import annotations

import hmac
from collections.abc import Callable, Mapping

from loopy_runtime.scm.github_webhook import SignatureError

# The header the canonical webhook config carries the shared secret in. An `Authorization`
# value is `Bearer <secret>`; a bespoke header (e.g. `X-Loopy-Token`) would carry the bare
# secret — `verify_token` accepts either by stripping an optional `Bearer ` prefix.
TOKEN_HEADER = "authorization"
_BEARER_PREFIX = "Bearer "


def verify_token(secret: str, header_value: str | None) -> None:
    """Raise `SignatureError` unless `header_value` carries the shared secret.

    The request body is not consulted — Datadog signs no body. Uses a constant-time compare so
    a mismatch leaks no timing signal.
    """
    if not header_value:
        raise SignatureError("missing Datadog auth header")
    presented = header_value
    if presented.startswith(_BEARER_PREFIX):
        presented = presented[len(_BEARER_PREFIX) :]
    if not hmac.compare_digest(presented, secret):
        raise SignatureError("token mismatch — header not signed with DATADOG_WEBHOOK_SECRET")


def token_verifier(secret: str) -> Callable[[bytes, Mapping[str, str]], None]:
    """Adapt `verify_token` to the runner's `verify(body, headers)` seam.

    `body` is accepted and ignored: Datadog authenticates by header token, not body signature.
    `headers` is a case-insensitive mapping (Starlette `Headers`), so the lowercase
    `TOKEN_HEADER` lookup works regardless of how Datadog cased the header.
    """

    def verify(body: bytes, headers: Mapping[str, str]) -> None:  # noqa: ARG001 - body unused
        verify_token(secret, headers.get(TOKEN_HEADER))

    return verify
