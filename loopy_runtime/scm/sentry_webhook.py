"""Verify Sentry webhook deliveries at the ingress.

A Custom (internal) Integration signs each delivery with HMAC-SHA256 of the raw request
body keyed by the integration's Client Secret, sent as a bare hex digest in the
`Sentry-Hook-Signature` header — the same scheme as GitHub minus the `sha256=` prefix.
Verify once at the edge, then let each mapper pick out the deliveries it cares about
(see `scm/sentry_builtins.py`).

We hash the exact bytes received. Sentry's own docs examples re-serialize the parsed
body (`json.dumps(...)`) before hashing, which can disagree with the wire bytes
(getsentry/sentry#31012); the raw bytes are what Sentry actually signs.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable, Mapping

from loopy_runtime.scm.github_webhook import SignatureError

# The header Sentry signs the raw body with (bare hex, no algorithm prefix).
SIGNATURE_HEADER = "sentry-hook-signature"


def verify_signature(secret: str, body: bytes, signature_header: str | None) -> None:
    """Raise `SignatureError` unless `body` was signed with the Client Secret.

    Uses a constant-time compare so a mismatch leaks no timing signal.
    """
    if not signature_header:
        raise SignatureError("missing Sentry-Hook-Signature header")
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(digest, signature_header):
        raise SignatureError("signature mismatch — body not signed with the Client Secret")


def signature_verifier(secret: str) -> Callable[[bytes, Mapping[str, str]], None]:
    """Adapt `verify_signature` to the runner's `verify(body, headers)` seam.

    `headers` is a case-insensitive mapping (Starlette `Headers`), so the lowercase
    `SIGNATURE_HEADER` lookup works regardless of how Sentry cased the header.
    """

    def verify(body: bytes, headers: Mapping[str, str]) -> None:
        verify_signature(secret, body, headers.get(SIGNATURE_HEADER))

    return verify
