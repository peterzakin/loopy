"""Datadog API client for `loopy auth datadog` — create/manage the Webhooks integration.

Datadog's Webhooks integration is fully manageable over its v1 REST API, so `loopy auth
datadog` can script the setup rather than have the user click through the integration tile.
This is the thin stdlib-`urllib` client for that (create / read-back / URL update), mirroring
`sentry_app`'s HTTP boundary so the runtime gains no new dependency and tests can stub one
function.

Two things differ from the Sentry client, and both come from Datadog's design:

- **Two credentials, not one.** Datadog's v1 API authenticates with an **API key**
  (`DD-API-KEY`) *and* an **Application key** (`DD-APPLICATION-KEY`); managing an integration
  needs an app key whose owner can manage integrations. Both are bootstrap credentials used at
  setup and never stored.
- **We mint the secret, Datadog doesn't.** A Sentry integration returns a Client Secret we
  persist. A Datadog webhook has no such secret — the shared secret that authenticates inbound
  deliveries is *ours*: the caller generates it and pushes it into the webhook's
  `custom_headers` (an `Authorization: Bearer <secret>` header), then persists the same value
  as `DATADOG_WEBHOOK_SECRET` for the ingress verifier to compare.

The API base varies by Datadog **site** (`datadoghq.com`, `us3.datadoghq.com`,
`datadoghq.eu`, `ddog-gov.com`, ...); it is always `https://api.<site>`, so the caller passes
the resolved base and this module never hard-codes a region.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Mapping

# Default site; every other region is `https://api.<site>` for the site's full domain.
DATADOG_DEFAULT_SITE = "datadoghq.com"
_WEBHOOKS_PATH = "/api/v1/integration/webhooks/configuration/webhooks"

# The canonical body template the user pastes into the webhook's Payload field (or that
# `loopy auth datadog` pushes via the API). Datadog substitutes the `$`-variables at delivery
# time; the mappers in `datadog_builtins.py` read exactly these keys. `tags`/`date` are carried
# for a future event and the deferred replay guard, and are ignored by today's mappers.
CANONICAL_PAYLOAD = json.dumps(
    {
        "event": "monitor",
        "id": "$ALERT_ID",
        "transition": "$ALERT_TRANSITION",
        "alert_type": "$ALERT_TYPE",
        "priority": "$ALERT_PRIORITY",
        "title": "$EVENT_TITLE",
        "body": "$EVENT_MSG",
        "metric": "$ALERT_METRIC",
        "scope": "$ALERT_SCOPE",
        "host": "$HOSTNAME",
        "tags": "$TAGS",
        "url": "$LINK",
        "date": "$DATE",
    },
    indent=2,
)


def api_base(site: str = DATADOG_DEFAULT_SITE) -> str:
    """The API base for a Datadog site: `https://api.<site>` for the site's full domain
    (`datadoghq.com`, `us3.datadoghq.com`, `datadoghq.eu`, `ddog-gov.com`, ...)."""
    return f"https://api.{site}"


class DatadogAppError(Exception):
    """Base error for the Datadog integration client."""


class DatadogAPIError(DatadogAppError):
    """A Datadog API call returned a non-2xx response."""

    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(f"Datadog API error {status}: {detail}")


def _request_json(
    method: str,
    url: str,
    *,
    api_key: str,
    app_key: str,
    payload: Mapping[str, object] | None = None,
) -> dict | list:
    """Issue one Datadog API request and parse the JSON body.

    The single network boundary of this module — tests stub this rather than hitting the wire.
    Raises `DatadogAPIError` on a non-2xx response.
    """
    headers = {
        "DD-API-KEY": api_key,
        "DD-APPLICATION-KEY": app_key,
        "Accept": "application/json",
        "User-Agent": "loopy-auth-datadog",
    }
    data: bytes | None = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request) as response:  # noqa: S310 - https Datadog API only
            body = response.read().decode()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace") if exc.fp else exc.reason
        raise DatadogAPIError(exc.code, detail) from exc
    return json.loads(body) if body else {}


def create_webhook(
    api_key: str,
    app_key: str,
    *,
    name: str,
    url: str,
    payload: str,
    custom_headers: str,
    base_url: str,
    encode_as: str = "json",
) -> dict:
    """Create a webhook in the Webhooks integration and return the created object.

    `payload` and `custom_headers` are JSON *strings* (Datadog stores them verbatim): `payload`
    is the canonical body template, `custom_headers` carries the `Authorization: Bearer
    <secret>` header the ingress verifier checks. Requires an app key that can manage
    integrations; an under-scoped key gets a 403.
    """
    body = {
        "name": name,
        "url": url,
        "payload": payload,
        "custom_headers": custom_headers,
        "encode_as": encode_as,
    }
    result = _request_json(
        "POST", f"{base_url}{_WEBHOOKS_PATH}", api_key=api_key, app_key=app_key, payload=body
    )
    return result if isinstance(result, dict) else {}


def get_webhook(api_key: str, app_key: str, name: str, *, base_url: str) -> dict:
    """Read a webhook back (verify its URL round-tripped)."""
    result = _request_json(
        "GET", f"{base_url}{_WEBHOOKS_PATH}/{name}", api_key=api_key, app_key=app_key
    )
    return result if isinstance(result, dict) else {}


def update_webhook(
    api_key: str,
    app_key: str,
    name: str,
    *,
    url: str,
    custom_headers: str,
    base_url: str,
) -> dict:
    """Repoint an existing webhook's URL, re-sending `custom_headers` so the shared secret is
    preserved (a PUT that omitted them could drop the auth header)."""
    body = {"url": url, "custom_headers": custom_headers}
    result = _request_json(
        "PUT", f"{base_url}{_WEBHOOKS_PATH}/{name}", api_key=api_key, app_key=app_key, payload=body
    )
    return result if isinstance(result, dict) else {}
