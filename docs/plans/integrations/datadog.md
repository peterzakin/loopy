# Datadog

**Loop it serves:** monitor alert → triage/remediate (the alerting side of the `Incident`
loop, alongside Sentry).

**Prereq:** [00 — Provider framework](00-provider-framework.md).

## Why Datadog is different in kind

Datadog's Webhooks integration is **outbound and user-templated**, unlike GitHub/Sentry/Linear:

- **The payload shape is not fixed by Datadog.** You define it in the Webhooks integration
  with template variables (`$EVENT_TITLE`, `$ALERT_ID`, `$ALERT_TRANSITION`, `$PRIORITY`,
  `$LINK`, `$TAGS`, ...). So there is no vendor-defined schema for a mapper to rely on.
- **Not signed by default.** Datadog does not HMAC the body. The supported auth is a
  **shared secret you add yourself** — either a custom header (e.g.
  `Authorization: Bearer <token>`) or a secret field embedded in the template.

This means "built-in Datadog" is really *"a canonical template we publish + a shared-secret
check"* rather than *"we parse Datadog's format."* Call this out prominently in the docs so
users know they must paste our template into Datadog for the built-in fields to line up.

## The canonical template (ships in docs)

Publish this as the payload body users configure in Datadog → Integrations → Webhooks. It
pins the field names the mapper expects:

```json
{
  "alert_id": "$ALERT_ID",
  "title": "$EVENT_TITLE",
  "body": "$EVENT_MSG",
  "priority": "$PRIORITY",
  "transition": "$ALERT_TRANSITION",
  "status": "$ALERT_STATUS",
  "tags": "$TAGS",
  "url": "$LINK",
  "event_id": "$ID"
}
```

The mapper then reads a stable, self-defined schema — decoupled from Datadog's internal
representation, which is the point.

## Contract — `loopy_core/builtins.py`

```python
"datadog": ProviderSpec(
    name="datadog", prefix="Datadog.", webhook_path="/hooks/datadog",
    secret_env="DATADOG_WEBHOOK_SECRET", events=DATADOG_EVENTS,
),
```

```python
DATADOG_EVENTS = {
    "Datadog.AlertTriggered": {     # transition in {Triggered, Re-Triggered}
        "alert_id": "str", "title": "str", "body": "str",
        "priority": "enum[P1, P2, P3, P4, P5, normal]",
        "status": "str", "tags": "str", "url": "url",
    },
    "Datadog.AlertRecovered": {     # transition == Recovered
        "alert_id": "str", "title": "str", "url": "url",
    },
}
```

Discrimination is on the template's `transition` field (`Triggered` / `Recovered` /
`Re-Triggered` / `No Data` / `Warn`). Keep the event set to the two that drive loops;
`No Data`/`Warn` can be added later without a schema change.

## Mappers — `loopy_runtime/scm/datadog_builtins.py` (new)

```python
def _alert_triggered(body: dict) -> dict | None:
    if body.get("transition") not in ("Triggered", "Re-Triggered"):
        return None
    return {"alert_id": body.get("alert_id", ""), "title": body.get("title", ""),
            "body": body.get("body", ""), "priority": body.get("priority", "normal"),
            "status": body.get("status", ""), "tags": body.get("tags", ""),
            "url": body.get("url", "")}
def _alert_recovered(body: dict) -> dict | None:
    if body.get("transition") != "Recovered":
        return None
    return {"alert_id": body.get("alert_id", ""), "title": body.get("title", ""),
            "url": body.get("url", "")}
MAPPERS = {"Datadog.AlertTriggered": _alert_triggered,
           "Datadog.AlertRecovered": _alert_recovered}
```

Register in `scm/builtin_registry.py`. Because the body is our own template, the mapper is
trivial and stable.

## Verifier — `loopy_runtime/scm/datadog_webhook.py` (new)

No vendor HMAC, so verify a **shared secret** delivered in a header. Two acceptable modes,
support both:

```python
def verify_shared_secret(secret, body, headers):
    # 1) Authorization: Bearer <secret>, or 2) X-Loopy-Webhook-Token: <secret>
    got = (headers.get("authorization", "").removeprefix("Bearer ").strip()
           or headers.get("x-loopy-webhook-token", ""))
    if not got or not hmac.compare_digest(got, secret):
        raise SignatureError("missing or mismatched Datadog shared secret")
```

Constant-time compare still applies. Register in `scm/verifiers.py` under `"datadog"`. Docs
must tell users to add the matching header in the Datadog webhook config; without
`DATADOG_WEBHOOK_SECRET` the endpoint runs unverified with the standard loud dev warning.

> Design note: this is weaker than an HMAC of the body (a leaked token replays), but it is the
> strongest option Datadog's integration supports out of the box. Document the tradeoff and
> recommend HTTPS + a rotated token. If stronger integrity is ever needed, users can put the
> secret *inside* the template and we can HMAC-check a field — deferred, not worth it now.

## Tests

- Drift test covers `Datadog.*` automatically.
- `tests/test_datadog_builtin.py`: compile happy-path (`on: Datadog.AlertTriggered`); shared
  secret present/absent/mismatched; `transition` routing (`Triggered` → triggered event,
  `Recovered` → recovered, `Warn` → `None`).

## Docs

`#datadog` section in `loopy-landing/docs/integrations.html` that leads with **"paste this
template into Datadog"**, then the two events, the shared-secret header setup, and
`DATADOG_WEBHOOK_SECRET`. Add to the catalog and hero. This section carries more setup weight
than the others precisely because the payload is user-configured.

## Credentials & setup

**No OAuth, no app.** Datadog has no OAuth for webhooks. The user configures a Webhooks
integration entry directly. Setup:

1. Datadog → Integrations → Webhooks → New. URL `https://<host>/hooks/datadog`, body = the
   canonical template above, and add a custom header carrying a token you invent
   (`Authorization: Bearer <token>` or `X-Loopy-Webhook-Token: <token>`).
2. Reference the webhook by `@webhook-<name>` in the monitors whose alerts should drive Loopy.
3. Put the **same token** in `DATADOG_WEBHOOK_SECRET` in the sandbox's `env_file`.
4. Absent the secret, `/hooks/datadog` runs unverified with the standard loud dev warning.

The token is a shared secret you generate, not a vendor credential, so there is nothing to
register and no OAuth anywhere — for triggers *or* actions (Datadog write-back, if ever added,
uses API + app keys, still no OAuth).

## Effort

Small code, but the **docs and the template are the product** here. The novel framework piece
is a non-HMAC shared-secret verifier, which is a small addition to the verifier registry.
