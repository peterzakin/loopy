"""Sentry API client for `loopy auth sentry` — create/manage the Custom Integration.

Sentry has no GitHub-style browser manifest flow for internal integrations: creating one
is a plain authenticated `POST .../sentry-apps/`. This module is the thin stdlib-`urllib`
client for that (and the read-back / URL-update calls), mirroring `github_app`'s HTTP
boundary so the runtime gains no new HTTP dependency and tests can stub one function.

The auth token this uses is a *bootstrap* credential (a User Auth Token, or an internal
integration token, with `org:write`). It creates the integration and is never stored;
what we persist is the integration's Client Secret (`SENTRY_WEBHOOK_SECRET`), which signs
inbound webhooks. Organization Auth Tokens are CI-scoped and 403 here — the caller turns
that 403 into a specific message.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence

SENTRY_API = "https://sentry.io"


class SentryAppError(Exception):
    """Base error for the Sentry integration client."""


class SentryAPIError(SentryAppError):
    """A Sentry API call returned a non-2xx response."""

    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(f"Sentry API error {status}: {detail}")


def _request_json(
    method: str,
    url: str,
    *,
    token: str,
    payload: Mapping[str, object] | None = None,
) -> dict | list:
    """Issue one Sentry API request and parse the JSON body.

    The single network boundary of this module — tests stub this rather than hitting the
    wire. Raises `SentryAPIError` on a non-2xx response.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "loopy-auth-sentry",
    }
    data: bytes | None = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request) as response:  # noqa: S310 - https Sentry API only
            body = response.read().decode()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace") if exc.fp else exc.reason
        raise SentryAPIError(exc.code, detail) from exc
    return json.loads(body) if body else {}


def list_organizations(token: str, *, base_url: str = SENTRY_API) -> list[dict]:
    """Every org the token can see — used to auto-detect `--org` when it's a single org."""
    result = _request_json("GET", f"{base_url}/api/0/organizations/", token=token)
    return result if isinstance(result, list) else []


def create_internal_integration(
    token: str,
    org: str,
    *,
    name: str,
    webhook_url: str,
    events: Sequence[str],
    scopes: Sequence[str],
    base_url: str = SENTRY_API,
) -> dict:
    """Create an internal (Custom) Integration and return the created app.

    The response carries `slug` and `clientSecret` — the secret is shown **only** here and
    masked on every later read, so the caller must persist it immediately. Requires the
    token to hold `org:write`/`org:admin`; a CI-scoped Organization Auth Token gets a 403.
    """
    payload = {
        "name": name,
        "isInternal": True,
        "verifyInstall": False,  # internal integrations aren't install-verified
        "webhookUrl": webhook_url,
        "scopes": list(scopes),
        "events": list(events),
    }
    result = _request_json(
        "POST", f"{base_url}/api/0/organizations/{org}/sentry-apps/", token=token, payload=payload
    )
    return result if isinstance(result, dict) else {}


def get_integration(token: str, slug: str, *, base_url: str = SENTRY_API) -> dict:
    """Read an integration back (verify its webhook URL round-tripped)."""
    result = _request_json("GET", f"{base_url}/api/0/sentry-apps/{slug}/", token=token)
    return result if isinstance(result, dict) else {}


def update_integration_webhook(
    token: str, slug: str, webhook_url: str, *, base_url: str = SENTRY_API
) -> dict:
    """Point an existing integration at a new webhook URL (leaves the secret untouched)."""
    result = _request_json(
        "PUT",
        f"{base_url}/api/0/sentry-apps/{slug}/",
        token=token,
        payload={"webhookUrl": webhook_url},
    )
    return result if isinstance(result, dict) else {}
