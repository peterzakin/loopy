# Datadog (built-in integration)

**Loop it serves:** monitor alert → triage → fix, and monitor recovery → confirm (the
canonical `Incident` / `MetricThreshold` loop). `datadog` is already a listed `Incident`/
`WorkItem` source enum in the README and scaffold; this promotes it to a first-class built-in
so a project triggers on `on: Datadog.MonitorAlerted` with no `sensors/` code and no
`registry.yml` event — the same zero-declaration experience as `Github.*` and `Sentry.*`.

This plan is **self-contained**. It reuses the `BuiltinProvider` machinery that the Sentry
work already generalized (`BUILTIN_PROVIDERS`, `provider_for`, `is_reserved`, generic
`doctor`/`integrations`), and it lands the one generalization the Sentry plan explicitly
**deferred to the third provider**: a mapper/verifier lookup keyed by provider instead of a
hand-merged dict. Datadog is that third provider.

## Two ways Datadog is not GitHub or Sentry

Everything else in this plan is mechanical (a contract, a mapper, a verifier, three wiring
touches). These two facts are the whole design, and both are verified against Datadog's
[Webhooks integration docs](https://docs.datadoghq.com/integrations/webhooks/) (July 2026):

### 1. No body signature — auth is a shared secret in a custom header

GitHub signs the raw body with `X-Hub-Signature-256`; Sentry signs it with
`Sentry-Hook-Signature`. **Datadog signs nothing.** Its webhook integration offers exactly
three ways to authenticate a delivery to *us*:

- **Basic auth in the URL** (`https://user:pass@host/…`) — leaks the credential into the URL,
  and our ingress would have to parse `Authorization: Basic`.
- **OAuth 2.0** — Datadog fetches a bearer from *our* token endpoint. This is the wrong
  direction and far too heavy: it presumes loopy runs an OAuth server.
- **Custom Headers** (a JSON object attached to the webhook) — the clean fit. The user adds
  one header carrying a shared secret; our ingress compares it.

So the Datadog verifier is a **constant-time equality check on a header value**, not an HMAC
over the body. Recommended header: `Authorization: Bearer <secret>` (or a bespoke
`X-Loopy-Token: <secret>` to avoid colliding with any proxy that strips `Authorization`). The
secret is loopy's own value, stored as **`DATADOG_WEBHOOK_SECRET`** and mirrored into the
webhook's Custom Headers.

> **The runner seam already fits — no change to `runner.py`.** The ingress verifier is typed
> `Callable[[bytes, Mapping[str, str]], None]` (`loopy_runtime/sensors/runner.py`). GitHub and
> Sentry use the `body` argument; Datadog's verifier simply ignores it and reads `headers`.
> The `except SignatureError → 401` path is unchanged; reuse the same exception.

> **Honest weakness, and the mitigation.** A static bearer is weaker than a per-body HMAC: a
> captured request can be replayed verbatim, and the secret is symmetric (Datadog stores it
> too). This is a property of Datadog's webhook design, not our choice. Mitigations: (a) TLS
> at the ingress means the header never travels in clear; (b) the secret is rotatable
> (`loopy auth datadog --update` re-PUTs the header, `DATADOG_WEBHOOK_SECRET_NEXT` overlap
> like the admin token); (c) the delivery *starts* a triage run — it does not mutate loopy
> state — so replay costs at most a duplicate investigation, which downstream idempotency on
> `monitor_id` already dampens. An **optional** light replay guard: include `$DATE` in the
> template and reject deliveries older than a few minutes. Deferred from v1; note it in the
> verifier's docstring as the upgrade path.

### 2. The payload is user-templated, so we must prescribe it

GitHub and Sentry send a **fixed** wire shape; our mapper reads known JSON paths
(`pull_request.head.ref`, `data.issue.id`). Datadog sends **whatever template the webhook is
configured with** — the body is authored by the user in the integration tile from `$`-prefixed
variables ([full list](https://docs.datadoghq.com/integrations/webhooks/)). There is no
canonical Datadog payload to map.

The consequence: a Datadog built-in must **ship a canonical payload template** that the user
pastes into the webhook's **Payload** field once. The mapper reads exactly that shape. This is
the Datadog equivalent of GitHub's "run `loopy webhooks github` once" and Sentry's "create the
Custom Integration once" — a single setup step, still **no Python and no `registry.yml`
entry**. `loopy auth datadog` (below) can push the template via the API so even that paste is
automated.

**Canonical template (paste into the webhook's Payload):**

```json
{
  "event":      "monitor",
  "id":         "$ALERT_ID",
  "transition": "$ALERT_TRANSITION",
  "alert_type": "$ALERT_TYPE",
  "priority":   "$ALERT_PRIORITY",
  "title":      "$EVENT_TITLE",
  "body":       "$EVENT_MSG",
  "metric":     "$ALERT_METRIC",
  "scope":      "$ALERT_SCOPE",
  "host":       "$HOSTNAME",
  "tags":       "$TAGS",
  "url":        "$LINK",
  "date":       "$DATE"
}
```

The `"event": "monitor"` literal is a self-describing discriminator that lets the mapper (and a
future `Datadog.EventTriggered`) tell monitor deliveries apart from other Datadog event sources
without header plumbing — the same body-only discrimination GitHub and Sentry mappers use.

## How a webhook fires (setup-side, for the docs)

A Datadog webhook does **not** fire on its own. The user creates the webhook (name it `loopy`),
then references it from a monitor by adding **`@webhook-loopy`** to that monitor's notification
message. One webhook config, referenced from any number of monitors. `$ALERT_TRANSITION`
distinguishes the firing reason (Triggered / Re-Triggered / Warn / Recovered / No Data), which
is exactly what our two events discriminate on.

## Events (contract)

Two events, discriminated purely on the body's `transition` field (no header plumbing):

- `Datadog.MonitorAlerted` — `transition ∈ {Triggered, Re-Triggered, Warn}`. The flagship: a
  monitor crosses into alert → triage/fix. Maps naturally onto the `Incident` loop.
- `Datadog.MonitorRecovered` — `transition == Recovered`. Closes the loop / drives the confirm
  step (mirrors `Sentry.IssueResolved`).

```python
# loopy_core/builtins.py
DATADOG_EVENTS: dict[str, dict[str, str]] = {
    "Datadog.MonitorAlerted": {          # transition in {Triggered, Re-Triggered, Warn}
        "monitor_id": "str",
        "title":      "str",
        "body":       "str",             # the monitor message ($EVENT_MSG)
        "alert_type": "enum[error, warning, info, success]",   # $ALERT_TYPE
        "priority":   "str",             # P1..P5, or "" if unset ($ALERT_PRIORITY)
        "metric":     "str",             # $ALERT_METRIC ("" for non-metric monitors)
        "scope":      "str",             # tags that triggered it ($ALERT_SCOPE)
        "host":       "str",             # $HOSTNAME ("" if not host-scoped)
        "transition": "str",             # the raw transition, carried through
        "url":        "url",             # $LINK
    },
    "Datadog.MonitorRecovered": {        # transition == Recovered
        "monitor_id": "str",
        "title":      "str",
        "scope":      "str",
        "host":       "str",
        "url":        "url",
    },
}
```

> **`alert_type` clamping.** `$ALERT_TYPE` is documented as one of `error|warning|success|info`,
> but recovery deliveries and some monitor types emit other strings; clamp anything outside the
> enum to `error` on `MonitorAlerted` (same pattern the Sentry `level` mapper uses), so the
> event always validates.

> **Deferred events (contract + mapper entry, no framework change):**
> `Datadog.EventTriggered` (the generic Event Management stream — a different, broader template
> keyed on `"event": "event"`), `Datadog.SecuritySignal`, and `Datadog.NoData`
> (`transition == "No Data"` as its own event rather than folded into alerted). Keep v1 to the
> two monitor events that drive the incident loop.

## Minimal generalization — the third-provider seam

Three GitHub-only strings became provider-generic in the Sentry work, and those need **no
change**: `loopy_core/compile/builtins.py` already resolves path/provider/contracts through
`provider_for()` + `provider.events`, and `loopy_cli/doctor.py` / `loopy_cli/integrations.py`
already iterate `BUILTIN_PROVIDERS`. Adding the provider entry lights those up for free.

What still hand-merges two providers, and must generalize now (this is the seam the Sentry plan
said to generalize "when a third provider arrives"):

1. **Contract + provider registration — `loopy_core/builtins.py`.** Add `DATADOG_PREFIX =
   "Datadog."`, `DATADOG_EVENTS` (above), and append one entry to the `BUILTIN_PROVIDERS`
   tuple:

   ```python
   BuiltinProvider("datadog", DATADOG_PREFIX, "/hooks/datadog",
                   DATADOG_EVENTS, "DATADOG_WEBHOOK_SECRET"),
   ```

   The compiler, doctor, and `integrations` views require nothing further — they are already
   generic over the tuple. Confirm E112's "known built-ins" message scopes to
   `provider.events` (it does), so an unknown `Datadog.Nope` lists the Datadog catalog.

2. **Runtime mapper lookup — `loopy_runtime/sensors/loader.py`.** Today `builtin_webhook_sensor`
   hand-merges `{**GITHUB_MAPPERS, **SENTRY_MAPPERS}`. Three providers is the point to replace
   the merge with a small registry rather than grow the dict literal. Add
   `loopy_runtime/scm/builtin_registry.py`:

   ```python
   from loopy_runtime.scm.github_builtins import BUILTIN_MAPPERS as _github
   from loopy_runtime.scm.sentry_builtins import MAPPERS as _sentry
   from loopy_runtime.scm.datadog_builtins import MAPPERS as _datadog

   # provider name (Sensor.provider) -> {event name -> payload mapper}
   MAPPERS_BY_PROVIDER = {"github": _github, "sentry": _sentry, "datadog": _datadog}

   def mapper_for(provider: str, emits: str):
       return (MAPPERS_BY_PROVIDER.get(provider) or {}).get(emits)
   ```

   `builtin_webhook_sensor` then resolves `mapper_for(spec.provider, spec.emits)` — keyed by the
   `provider` discriminator the data model already carries, so a fourth provider is one line
   here.

3. **CLI verifier wiring — `loopy_cli/__init__.py` (~L1497).** Today `edge_verifiers` is a
   path-keyed dict of `(secret_env, hmac_verifier_factory)`. Datadog's verifier is a **different
   kind** (header compare, not HMAC), so make the verifier factory a property of the provider
   rather than a parallel dict. Cleanest: key off `sensor.provider` and pull `secret_env` from
   the `BuiltinProvider` (already there) plus a `verifier_factory` looked up per provider:

   ```python
   from loopy_runtime.scm import github_webhook, sentry_webhook, datadog_webhook
   _VERIFIER_FACTORY = {
       "github":  github_webhook.signature_verifier,
       "sentry":  sentry_webhook.signature_verifier,
       "datadog": datadog_webhook.token_verifier,   # header compare, not HMAC
   }
   # for a builtin sensor: secret = env[provider.secret_env];
   # verify = _VERIFIER_FACTORY[provider.name](secret) if secret else warn_once(provider)
   ```

   Keep the per-provider "warn once when the secret is unset" set that already exists. Absent
   `DATADOG_WEBHOOK_SECRET`, `/hooks/datadog` runs unverified with the standard loud dev warning
   — same posture as the other two.

4. **Doctor / integrations copy.** `loopy_cli/doctor.py`: add `"datadog": "run \`loopy auth
   datadog\` (or set it in loopy.env)"` to `_WEBHOOK_SECRET_FIX`; leave Datadog **out** of
   `_SECRET_CHECK_SKIP` (unlike GitHub, its delivery isn't covered by `registration_findings`,
   so the secret check should fire). `loopy_cli/integrations.py`: add `"datadog": "loopy auth
   datadog"` to `_SECRET_FIX`.

## Verifier — `loopy_runtime/scm/datadog_webhook.py` (new)

A constant-time equality check on a header value. Structurally simpler than the HMAC verifiers
and reusing their `SignatureError`:

```python
from loopy_runtime.scm.github_webhook import SignatureError

# The header the canonical webhook config carries the shared secret in.
# "authorization" -> value is "Bearer <secret>"; a bespoke header carries the bare secret.
TOKEN_HEADER = "authorization"
_BEARER = "Bearer "

def verify_token(secret: str, header_value: str | None) -> None:
    if not header_value:
        raise SignatureError("missing Datadog auth header")
    presented = header_value[len(_BEARER):] if header_value.startswith(_BEARER) else header_value
    if not hmac.compare_digest(presented, secret):     # constant-time; body is not consulted
        raise SignatureError("token mismatch — header not signed with DATADOG_WEBHOOK_SECRET")

def token_verifier(secret):          # adapts to the runner's verify(body, headers) seam
    def verify(body, headers):       # body deliberately unused: Datadog signs no body
        verify_token(secret, headers.get(TOKEN_HEADER))
    return verify
```

Name it `token_verifier` (not `signature_verifier`) precisely so the diff advertises that this
is a token compare, not a body signature — the reviewer should see the difference at the call
site.

## Mappers — `loopy_runtime/scm/datadog_builtins.py` (new)

Mirror `sentry_builtins.py`: `payload -> field dict | None`, `None` when the delivery isn't this
event's concern. Discriminate on `transition` (and defensively on `event == "monitor"`):

```python
_ALERT_TRANSITIONS = frozenset({"Triggered", "Re-Triggered", "Warn"})
_ALERT_TYPES = frozenset({"error", "warning", "info", "success"})

def _is_monitor(body: dict) -> bool:
    return body.get("event") == "monitor" or "transition" in body

def _monitor_alerted(body: dict) -> dict | None:
    if not _is_monitor(body) or body.get("transition") not in _ALERT_TRANSITIONS:
        return None
    at = body.get("alert_type")
    return {
        "monitor_id": str(body.get("id") or ""),
        "title":      body.get("title") or "",
        "body":       body.get("body") or "",
        "alert_type": at if at in _ALERT_TYPES else "error",
        "priority":   body.get("priority") or "",
        "metric":     body.get("metric") or "",
        "scope":      body.get("scope") or "",
        "host":       body.get("host") or "",
        "transition": body.get("transition") or "",
        "url":        body.get("url") or "",
    }

def _monitor_recovered(body: dict) -> dict | None:
    if not _is_monitor(body) or body.get("transition") != "Recovered":
        return None
    return {
        "monitor_id": str(body.get("id") or ""),
        "title":      body.get("title") or "",
        "scope":      body.get("scope") or "",
        "host":       body.get("host") or "",
        "url":        body.get("url") or "",
    }

MAPPERS = {
    "Datadog.MonitorAlerted":   _monitor_alerted,
    "Datadog.MonitorRecovered": _monitor_recovered,
}
```

> Because the payload shape is **ours** (the canonical template), the mapper is exact rather than
> defensive-by-necessity — but `$`-variables that Datadog can't resolve are sent as empty
> strings, and an unconfigured optional (e.g. a non-metric monitor's `$ALERT_METRIC`) arrives as
> `""`, so the `or ""` guards still earn their place. The contract in `builtins.py` is the source
> of truth the drift test enforces.

## `loopy auth datadog` (optional follow-up, mirrors `loopy auth sentry`)

Datadog's Webhooks integration is fully manageable over its REST API, so the same "script the
setup" command is possible — with two twists worth stating up front.

- **API:** `POST /api/v1/integration/webhooks/configuration/webhooks` with
  `{ "name": "loopy", "url": "<public>/hooks/datadog", "payload": "<canonical template>",
  "custom_headers": "{\"Authorization\": \"Bearer <secret>\"}", "encode_as": "json" }`.
  `PUT …/webhooks/{name}` updates it. Read-back is `GET …/webhooks/{name}`.
- **Credentials:** an **API key** (`DD_API_KEY`) **and** an **Application key** (`DD_APP_KEY`)
  whose owner can manage integrations. Two keys, unlike Sentry's one token — spell both out in
  the token-help text, and where to mint them (Organization Settings → API Keys / Application
  Keys).
- **Twist 1 — loopy mints the secret, not Datadog.** In the Sentry flow the provider *returns*
  the Client Secret and we persist it. Here the secret is **loopy's** value: the command
  generates it (`secrets.token_urlsafe`), writes `DATADOG_WEBHOOK_SECRET` to `loopy.env`, **and**
  pushes it into the webhook's `custom_headers` via the API. Rotation re-generates and re-PUTs.
- **Twist 2 — the webhook alone fires nothing.** Creation registers the endpoint, but a monitor
  must reference `@webhook-loopy` to deliver. The command cannot know which monitors the user
  wants, so it ends with the nudge: add `@webhook-loopy` to the monitor(s) you want to drive a
  workflow (UI, or `PUT /api/v1/monitor/{id}` appending it to `message`). This is the Datadog
  analogue of Sentry's "add the integration as an Alert Rule Action" step.
- **Multi-region / `DD_SITE`.** Datadog's API base varies by site
  (`api.datadoghq.com`, `.eu`, `us3`, `us5`, `ap1`, `ddog-gov.com`). Resolve from `DD_SITE`
  (default `datadoghq.com`), `--site` override — a real difference from Sentry's single
  `sentry.io` base, and easy to get wrong, so make it an explicit, validated input.
- **`--manual` fallback:** print the canonical payload template and the `custom_headers` JSON
  (with a freshly generated secret) for the user to paste into the integration tile, and write
  `DATADOG_WEBHOOK_SECRET` locally. The honest path when the app key isn't available.

Client lives in `loopy_runtime/scm/datadog_app.py` (thin `urllib` client, mirroring
`sentry_app.py`), the command in `loopy_cli/auth.py`, reusing `LOOPY_PUBLIC_URL` +
`config.hook_url(public, "/hooks/datadog")` for the URL. As with Sentry, `loopy init` stays out
of it — Datadog setup is an explicit `loopy auth datadog` step.

## Tests

- **Drift** (`tests/test_builtins.py`): extend the contract↔mapper assertion to cover
  `DATADOG_EVENTS` against `datadog_builtins.MAPPERS`. If step 2's registry lands, the drift
  test can iterate `MAPPERS_BY_PROVIDER` generically instead of naming each provider.
- **Compile** (`tests/test_datadog_builtin.py`, new): `on: Datadog.MonitorAlerted` compiles
  clean with no event entry and no sensor file; the injected sensor is `source="builtin"`,
  `provider="datadog"`, path `/hooks/datadog`; `on: Datadog.Nope` → E112 (message lists the
  Datadog catalog); a user event or sensor in the `Datadog.` namespace → E215.
- **Verifier**: correct bearer passes; missing/mismatched header → `SignatureError`; **and a
  test that mutating the body does NOT fail verification** — this pins the intended, documented
  difference from GitHub/Sentry (header-only auth) so a future "add HMAC" refactor is a
  conscious contract change, not an accident.
- **Mappers**: one test per event from the canonical-template payload; assert a `Recovered`
  delivery returns `None` from `_monitor_alerted` and a dict from `_monitor_recovered` (and vice
  versa), an out-of-enum `alert_type` clamps to `error`, and a non-monitor body returns `None`.

## Docs

- Add a `#datadog` section to `loopy-landing/docs/integrations.html` (+ the `.md` twin, then
  rebuild `llms-full.txt`) mirroring the Sentry section: the two events with sample payloads,
  **the canonical payload template front and center** (it's the one thing a Datadog user must
  paste), the custom-header auth story, `DATADOG_WEBHOOK_SECRET`, the `@webhook-loopy` monitor
  step, and the `DD_SITE` note. Be explicit that Datadog's payload is user-defined and loopy
  prescribes one — it's the conceptual departure from GitHub/Sentry a reader needs called out.
- Add Datadog to the landing hero service list (`index.html` `.svc` row, next to
  `Sentry`/`GitHub`) and the integrations catalog.
- Follow `loopy-landing/STYLE_GUIDE.md` for all landing copy: no emdashes or en-dashes, no
  three-circle window chrome, no marketing-speak, every link visibly styled.

## Effort

Medium — a step up from Sentry. The contract, mapper, and wiring are small and now largely
generic (the Sentry work paid that down). The added cost is real but bounded: (a) the verifier
is a **new kind** (header token compare, not body HMAC) and needs its own test asserting the
body is intentionally unchecked; (b) the **prescribed-payload** model is a genuine conceptual
departure that lives mostly in docs and the `loopy auth datadog` template push; (c) being the
**third provider**, it lands the small `builtin_registry` generalization the Sentry plan
deferred; (d) `DD_SITE` multi-region and the two-key (`DD_API_KEY` + `DD_APP_KEY`) auth add
setup surface Sentry didn't have.
