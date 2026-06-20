"""Verify GitHub webhook deliveries at the ingress.

GitHub posts *every* subscribed event type (pull_request, push, issues, …) to the
single webhook URL configured on the App/repo, signing the raw request body with
HMAC-SHA256 keyed by the webhook secret (header `X-Hub-Signature-256`). There is
no server-side filtering by action — `opened` vs `closed`/merged both arrive — so
the model is: verify the signature once at the edge, then let each sensor pick out
the deliveries it cares about (by `action`, merged-state, etc.) and return None for
the rest. See `examples/github` for the sensor side.

This is the inbound counterpart to `scm/token_provider.py` (outbound, scoped
tokens): together they give a webhook the same trust posture depot uses — signed
ingress in, short-lived least-privilege creds out, no static secrets in between.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable, Mapping

# The header GitHub signs the raw body with (the older `X-Hub-Signature`/SHA-1 is
# deprecated; we only accept SHA-256).
SIGNATURE_HEADER = "x-hub-signature-256"


class SignatureError(Exception):
    """The delivery's signature is missing, malformed, or doesn't match the secret."""


def verify_signature(secret: str, body: bytes, signature_header: str | None) -> None:
    """Raise `SignatureError` unless `body` was signed with `secret`.

    Compares the `sha256=<hex>` HMAC GitHub sends against one we recompute from the
    raw bytes (must be the exact bytes received — re-serialized JSON won't match).
    Uses a constant-time compare so a mismatch leaks no timing signal.
    """
    if not signature_header or not signature_header.startswith("sha256="):
        raise SignatureError("missing or malformed X-Hub-Signature-256 header")
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    expected = f"sha256={digest}"
    if not hmac.compare_digest(expected, signature_header):
        raise SignatureError("signature mismatch — body not signed with the webhook secret")


def signature_verifier(secret: str) -> Callable[[bytes, Mapping[str, str]], None]:
    """Adapt `verify_signature` to the runner's `verify(body, headers)` seam.

    `headers` is a case-insensitive mapping (Starlette `Headers`), so the lowercase
    `SIGNATURE_HEADER` lookup works regardless of how GitHub cased the header.
    """

    def verify(body: bytes, headers: Mapping[str, str]) -> None:
        verify_signature(secret, body, headers.get(SIGNATURE_HEADER))

    return verify
