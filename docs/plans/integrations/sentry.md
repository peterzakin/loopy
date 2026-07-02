# Sentry (built-in integration)

**Loop it serves:** error → triage → fix (the canonical `Incident` loop). Sentry is already
the hand-written example sensor in `examples/incidents/sensors/sensors.py`; this promotes it
into the built-in catalog so a project triggers on `on: Sentry.IssueCreated` with no
`sensors/` code and no `registry.yml` event.

This plan is **self-contained**: it includes the small generalization of the GitHub-only
machinery that Sentry requires. It does **not** build a full multi-provider registry — that is
premature for a second provider (YAGNI); it should land when a third provider does. The seams
below are chosen so that later generalization is additive, not a rewrite.

## Webhook facts (verified against Sentry docs, July 2026)

- Sentry's **Custom / Internal Integration** (Settings → Developer Settings) POSTs JSON to one
  configured webhook URL.
- **Signature:** header **`Sentry-Hook-Signature`** = **hex HMAC-SHA256** of the body keyed by
  the integration's **Client Secret**. No `sha256=` prefix (unlike GitHub's
  `X-Hub-Signature-256`).
- **Other headers:** `Sentry-Hook-Resource` (which resource fired: `issue`, `error`,
  `comment`, `event_alert`, `metric_alert`, `installation`), `Sentry-Hook-Timestamp`,
  `Request-ID`. The **action** (`created`, `resolved`, `assigned`, `ignored`, ...) is in the
  JSON body's `action` field.

> **Signing gotcha to validate early.** Sentry's own docs examples compute the HMAC over the
> *JSON-stringified* body (`json.dumps(...)`), not the raw received bytes, and there is a
> long-standing mismatch bug when the two serializations differ (getsentry/sentry#31012). Our
> verifier hashes the **raw received bytes** (what Sentry actually sent), which is the robust
> choice and matches how our GitHub verifier already works — the runner hands the verifier the
> raw body. Lock a real signed payload into a fixture and confirm raw-bytes verification
> passes before finalizing; if Sentry ever signs a canonicalized form, fall back to
> `json.dumps(body, separators=(",", ":"))`. Sentry does **not** include the timestamp in the
> signed material, so there is no replay-guard step (unlike Slack/Linear would need).

## Events (contract)

Ship two solid events in v1; discriminate purely on the **body** (no header plumbing needed),
mirroring how the GitHub mappers key off which object is present.

- `Sentry.IssueCreated` — `data.issue` present, `action == "created"`. The flagship: a new
  issue appears → triage/fix.
- `Sentry.IssueResolved` — `data.issue` present, `action == "resolved"`. Closes the loop /
  confirm step.

`Sentry.AlertTriggered` (issue-alert `event_alert` / `metric_alert`) is a **documented
follow-on within this same plan**, deferred only because its payload fields need validation
against a live alert delivery; adding it is a contract + mapper entry, no framework change.

## Minimal generalization (the GitHub-only bits Sentry must touch)

Three places string-hardcode GitHub. Generalize each *just enough* for a second provider:

1. **Compiler path/lookup — `loopy_core/compile/builtins.py`.** Today `_BUILTIN_PATH =
   "/hooks/github"` and the contract lookup reads `GITHUB_EVENTS`. Introduce a tiny map from
   reserved prefix → (webhook path, contracts):

   ```python
   # loopy_core/builtins.py
   SENTRY_PREFIX = "Sentry."
   SENTRY_EVENTS = { ... }                      # see below
   _WEBHOOK_PATHS = {GITHUB_PREFIX: "/hooks/github", SENTRY_PREFIX: "/hooks/sentry"}
   _PROVIDER_NAME = {GITHUB_PREFIX: "github", SENTRY_PREFIX: "sentry"}

   def contracts_for(name: str) -> dict[str, str] | None:
       if name.startswith(GITHUB_PREFIX): return GITHUB_EVENTS.get(name)
       if name.startswith(SENTRY_PREFIX): return SENTRY_EVENTS.get(name)
       return None

   def is_reserved(name: str) -> bool:
       return name.startswith(GITHUB_PREFIX) or name.startswith(SENTRY_PREFIX)
   ```

   In `inject_builtins`, resolve the referenced event's prefix → path/provider/contracts from
   these maps instead of the hardcoded constants. Keep E215 (reserved namespace) and E112
   (unknown built-in) exactly as they are; E112's "known built-ins" list becomes
   `GITHUB_EVENTS | SENTRY_EVENTS`, scoped to the matched provider for a better message.

2. **Runtime mapper lookup — `loopy_runtime/sensors/loader.py`.** `builtin_webhook_sensor`
   imports `BUILTIN_MAPPERS` directly from `github_builtins`. Change it to a merged lookup so a
   second provider's mappers resolve by `emits`:

   ```python
   from loopy_runtime.scm.github_builtins import BUILTIN_MAPPERS as _gh
   from loopy_runtime.scm.sentry_builtins import MAPPERS as _sentry
   _ALL = {**_gh, **_sentry}
   ```

   (A one-line merge here is enough; a dedicated `builtin_registry` module is the thing to add
   only when a third provider arrives.)

3. **CLI verifier wiring — `loopy_cli/__init__.py` (~L1195-1227).** Today it hardcodes
   `GITHUB_WEBHOOK_SECRET`, the `/hooks/github` string check, and the GitHub verifier. Key the
   verifier off the sensor's `provider`:

   ```python
   _SECRET_ENV = {"github": "GITHUB_WEBHOOK_SECRET", "sentry": "SENTRY_WEBHOOK_SECRET"}
   _VERIFIER  = {"github": gh_signature_verifier, "sentry": sentry_signature_verifier}
   ...
   if sensor.source == "builtin" and sensor.provider in _SECRET_ENV:
       secret = os.environ.get(_SECRET_ENV[sensor.provider])
       if secret:
           verify = _VERIFIER[sensor.provider](secret)
       else:
           warn_once(sensor.provider, f"{_SECRET_ENV[sensor.provider]} not set; "
                     f"{sensor.trigger.path} signatures are unverified (dev only)")
   ```

   Replace the single `warned_unverified` bool with a per-provider `set` so each provider warns
   once. User-authored (`source="module"`) sensors are untouched.

No change to `loopy_runtime/sensors/runner.py` is needed — Sentry discriminates on the body,
so the existing signed-path handler (verify raw bytes → `json.loads` → fan out) works as-is.

## Contract — `loopy_core/builtins.py`

```python
SENTRY_EVENTS: dict[str, dict[str, str]] = {
    "Sentry.IssueCreated": {          # data.issue present, action == "created"
        "issue_id": "str", "title": "str", "culprit": "str",
        "level": "enum[debug, info, warning, error, fatal]",
        "project": "str", "url": "url",
    },
    "Sentry.IssueResolved": {         # data.issue present, action == "resolved"
        "issue_id": "str", "title": "str", "project": "str", "url": "url",
    },
}
```

## Mappers — `loopy_runtime/scm/sentry_builtins.py` (new)

Mirror `github_builtins.py`: `payload -> field dict | None`, `None` when the delivery isn't
this event's concern.

```python
def _issue(body, action):
    d = (body.get("data") or {}).get("issue")
    if not d or body.get("action") != action:
        return None
    return d

def _issue_created(body):
    d = _issue(body, "created")
    if d is None: return None
    return {"issue_id": d["id"], "title": d["title"], "culprit": d.get("culprit") or "",
            "level": d.get("level") or "error",
            "project": (d.get("project") or {}).get("slug", ""),
            "url": d.get("web_url") or d.get("url", "")}

def _issue_resolved(body):
    d = _issue(body, "resolved")
    if d is None: return None
    return {"issue_id": d["id"], "title": d["title"],
            "project": (d.get("project") or {}).get("slug", ""),
            "url": d.get("web_url") or d.get("url", "")}

MAPPERS = {"Sentry.IssueCreated": _issue_created, "Sentry.IssueResolved": _issue_resolved}
```

> Field paths (`data.issue.web_url`, `level`, `culprit`, `project.slug`) vary by payload
> version; the contract in `builtins.py` is the source of truth the drift test enforces, so
> pin a recorded payload as a fixture and adjust the mapper to it.

## Verifier — `loopy_runtime/scm/sentry_webhook.py` (new)

Structurally identical to `github_webhook.py`, minus the `sha256=` prefix:

```python
SIGNATURE_HEADER = "sentry-hook-signature"

def verify_signature(secret: str, body: bytes, sig: str | None) -> None:
    if not sig:
        raise SignatureError("missing Sentry-Hook-Signature header")
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        raise SignatureError("signature mismatch — body not signed with the Client Secret")

def signature_verifier(secret):   # adapts to the runner's verify(body, headers) seam
    def verify(body, headers): verify_signature(secret, body, headers.get(SIGNATURE_HEADER))
    return verify
```

Reuse the existing `SignatureError` from `github_webhook.py` (import it) so the runner's
`except SignatureError -> 401` path is unchanged.

## How users set up the Sentry webhook

The clean, signed path is a **Custom / Internal Integration** (org-level; no OAuth grant).

**UI (typical):**
1. Sentry → Settings → Developer Settings → **Custom Integrations** → *Create New
   Integration* → **Internal**.
2. Set **Webhook URL** to `https://<your-host>/hooks/sentry`.
3. Under **Webhooks**, subscribe to the **issue** resource (this delivers the `created` /
   `resolved` actions the v1 events map).
4. Give it the read **scopes** it needs (e.g. `Issue & Event: Read`).
5. Save, then copy the generated **Client Secret** into `SENTRY_WEBHOOK_SECRET` in the
   sandbox's `env_file` (gitignored, per project convention).
6. Absent the secret, `/hooks/sentry` runs unverified with the standard loud dev warning
   (same posture as GitHub today) — never a silent pass in production.

### Can they set it up with an API token? — Yes, for creation/management.

The Custom Integration (including its webhook URL and subscribed events) can be created and
updated programmatically with a Sentry **auth token**, no UI clicking:

- **Create:** `POST /api/0/organizations/{org_slug}/sentry-apps/` with
  `{ "name": ..., "isInternal": true, "webhookUrl": "https://<host>/hooks/sentry",
  "scopes": ["event:read"], "events": ["issue"] }`. Requires a token with **`org:write`**
  (org admin) scope. The response includes the **`clientSecret`** — capture it then; it is
  masked afterward.
- **Update:** `PUT /api/0/sentry-apps/{slug}/` (same fields).

Two things to be precise about:

- **The API token is for *setup*, not for authenticating deliveries.** The inbound webhook is
  authenticated by the **Client Secret** HMAC signature (above), not by any API token. The
  auth token only creates/manages the integration (and would later authorize outbound
  write-back — resolving an issue, out of scope here).
- **Alert-rule webhooks need an extra step.** The `issue` resource subscription (v1) is fully
  covered by `events: ["issue"]`. But `event_alert` / `metric_alert` deliveries (the deferred
  `Sentry.AlertTriggered`) require enabling the integration as an **Alert Rule Action** and
  adding it to alert rules (UI, or `POST /api/0/projects/{org}/{project}/rules/`).

**Optional enhancement (not required for this PR):** because creation is a single authenticated
API call, a future `loopy auth sentry` could script it end-to-end. See
[Appendix: `loopy auth sentry`](#appendix-loopy-auth-sentry-follow-up).

## Tests

- **Drift** (`tests/test_builtins.py`): extend the contract↔mapper assertion to include
  `SENTRY_EVENTS` against `sentry_builtins.MAPPERS` — every contract has a mapper and vice
  versa. Existing GitHub assertions stay green unchanged.
- **Compile** (`tests/test_sentry_builtin.py`, new): `on: Sentry.IssueCreated` compiles clean
  with no event entry and no sensor file; the injected sensor is `source="builtin"`,
  `provider="sentry"`, path `/hooks/sentry`; `on: Sentry.Nonexistent` → E112; a user event in
  the `Sentry.` namespace → E215.
- **Verifier**: good signature passes; missing/mismatched → `SignatureError`; verify against a
  recorded raw-body fixture (the signing gotcha above).
- **Mappers**: one test per event from a recorded payload fixture; assert wrong `action` and a
  non-issue delivery (`data.error` present) both return `None`.

## Docs

- Add a `#sentry` section to `loopy-landing/docs/integrations.html` mirroring the GitHub
  section: the two events, a sample payload, the Custom Integration setup (UI + the API-token
  path), and `SENTRY_WEBHOOK_SECRET`. Add Sentry to the integrations catalog and the landing
  hero service list.
- Update `examples/incidents`: either point the hand-written Sentry sensor at the built-in, or
  keep it annotated as "here's what the built-in replaces" teaching code.
- Follow the loopy-landing `STYLE_GUIDE.md` for any copy (no emdashes, no traffic-light window
  chrome, no marketing-speak).

## Appendix: `loopy auth sentry` (follow-up)

Not part of the built-in-integration PR, but the natural companion command — the equivalent of
`loopy auth github` (`loopy_cli/auth.py`). It scripts the Custom/Internal Integration setup so
the user doesn't click through Sentry's Developer Settings by hand.

### Which token generates the webhook: a User token, as a one-time bootstrap

`loopy auth github` runs a browser **App manifest flow** because GitHub lets you create an app
from nothing. Sentry has no such flow for internal integrations — creation is a plain
`POST .../sentry-apps/` that needs `org:write` / `org:admin`. That scope requirement decides
which token to use, and the intuitive choice (an org token) is the wrong one:

- **Organization Auth Token — cannot do it.** The natural "automation" credential, but its
  scope set is **fixed to CI** (`org:ci`: releases, source maps, code mappings) and can't be
  widened. It **403s** on integration creation. Not an option for auto-generating the webhook.
- **User Auth Token (from an org owner/manager) — the default.** It can be minted with
  `org:write` in one step, so it's what a fresh org can actually use. This is what
  `loopy auth sentry` reads from `SENTRY_AUTH_TOKEN`.
- **Provisioner internal-integration token — the person-independent alternative.** A team that
  wants hands-off automation creates one admin internal integration by hand (`org:write`), then
  uses *its* org-scoped token — not tied to any person — to provision others. Documented as the
  advanced option; same env var.

**Why a personal user token is acceptable here:** the integration is created **once** and its
Client Secret is persisted to `loopy.env`; the auth token is a **one-time bootstrap** — used at
setup, never stored, never read at run time. If it's later revoked, nothing breaks until you
next re-provision. So the choice is "what can create it once," where the user token wins on
availability; it is not the service's run-time identity.

If create returns **403**, the command prints exactly the above (org tokens can't; use a user
or provisioner token) and points at `--manual` — a silent failure here would be the worst
outcome, so it is an explicit, specific error.

Two other differences from `loopy auth github` remain:

- **The public webhook URL is an input.** A Sentry integration *is* the event source (the
  GitHub App has no webhook), so the command must register the public URL `loopy run` serves at
  (`https://<host>/hooks/sentry`). See "The hosted URL" below — this is the flow's one
  genuinely awkward input.
- **No install step.** An internal integration is org-scoped and active on creation — no
  `_wait_for_installation` poll like the GitHub App needs.

### Command surface

```
loopy auth sentry
  [--webhook-url <url>]            # public https URL for /hooks/sentry (prompted if omitted)
  [--org <slug>]                   # default: $SENTRY_ORG, else auto-detected from the token
  [--name loopy]                   # integration name (default: loopy[-org])
  [--sentry-url https://sentry.io] # base URL; override for self-hosted ($SENTRY_URL)
  [--events issue]                 # resources to subscribe (v1: issue)
  [--update]                       # PUT a new webhook URL to the stored integration
  [--root .] [--force] [--manual]
```

The auth token is deliberately **not a flag** (it would land in shell history). It resolves in
three steps, so the command works for both "already exported" and "first time, no idea":

1. **Environment:** `$SENTRY_AUTH_TOKEN`, if set.
2. **Prompt:** a hidden-input prompt (`typer.prompt(..., hide_input=True)`).
3. **Instruct:** the prompt is preceded by exactly where to get one — *Sentry → User Settings →
   Auth Tokens → Create New Token, with `org:write` (Organization: Read & Write). Not the
   org-settings "Auth Tokens" page: those are CI-scoped and cannot create integrations.* The
   specificity matters; the two pages look alike and the wrong one 403s.

`--org` falls back to `$SENTRY_ORG`, then to auto-detection: `GET /api/0/organizations/` with
the token — one org means use it silently; several means prompt with the list. The common case
is a bare `loopy auth sentry`.

### The hosted URL

Sentry must reach the webhook over **public HTTPS**, and `loopy run` binds `127.0.0.1:8000` —
so the URL cannot be inferred; the user supplies it (`--webhook-url`, or an interactive prompt).
Three cases the prompt text must handle:

- **Deployed:** the user knows their host; suggest the shape `https://<host>/hooks/sentry`.
- **Local dev:** no public URL exists. Reject localhost/http answers with the fix spelled out:
  run a tunnel (`cloudflared tunnel --url http://127.0.0.1:8000` or `ngrok http 8000`) and give
  Sentry the tunnel URL + `/hooks/sentry`.
- **It changes later** (tunnel → prod, host move): `loopy auth sentry --update` PUTs the new
  URL to `/api/0/sentry-apps/{slug}/` using the stored `SENTRY_INTEGRATION_SLUG` — no
  re-create, secret unchanged.

**Sentry does not validate the URL.** Unlike Slack's challenge handshake, creation succeeds
with any `webhookUrl`; a typo fails silently at delivery time. So the command cannot prove
end-to-end delivery — it ends with a verification nudge instead (below), and `loopy doctor`
checks the secret side.

### Flow

1. Resolve the token (env → prompt → instructions, above). Preflight
   `GET {sentry_url}/api/0/organizations/{org}/` to confirm it's valid for the org.
2. Idempotency guard: `SENTRY_WEBHOOK_SECRET` already in `loopy.env` and no `--force` → error
   (mirrors the `GITHUB_APP_ID` guard in `run_github_auth`).
3. Resolve the webhook URL (flag → prompt, with the tunnel guidance above).
4. `POST {sentry_url}/api/0/organizations/{org}/sentry-apps/` with
   `{ name, isInternal: true, webhookUrl, scopes: ["event:read"], events: ["issue"] }`.
   Suffix the name on collision (as `default_app_name` does for GitHub). **On 403, emit the
   scope-caveat message and stop.**
5. **Immediately** write `SENTRY_WEBHOOK_SECRET` + `SENTRY_INTEGRATION_SLUG` to `loopy.env`
   (`write_control_plane_env` + `_ensure_gitignored`, reused from `auth.py`) — before any
   verify step. The `clientSecret` appears **only** in this create response and is masked on
   every later read; a crash between create and write would orphan it. If the command ever
   finds the integration existing but no stored secret, the recovery is: regenerate/copy the
   secret in the Sentry UI, then `loopy auth sentry --manual`.
6. Verify what's verifiable: `GET {sentry_url}/api/0/sentry-apps/{slug}/`, confirm
   `webhookUrl` round-trips, print a ✓ with the slug.
7. Print the delivery-side nudge Sentry can't give us: start `loopy run`, trigger a test issue
   (or resolve/unresolve one), and watch for the event; for the deferred
   `Sentry.AlertTriggered`, add the integration as an Alert Rule Action. `issue` events need
   nothing more.

### The token ≠ the signing secret

Worth stating plainly now that a token is assumed present: `SENTRY_AUTH_TOKEN` **creates** the
integration; it does **not** authenticate inbound webhooks. Deliveries are signed with the
integration's **Client Secret**, which only exists after create — that is what we persist as
`SENTRY_WEBHOOK_SECRET` and what the verifier checks. The org token is **neither stored by this
command nor read at run time** (an internal integration also mints an *installation token* for
outbound API calls; capture that only when the write-back/action side is built — no unused
secret now).

### `--manual` fallback

For a CI-only org token (or no token at all): `loopy auth sentry --manual` skips the API and
securely prompts for a Client Secret from an integration created in the UI, writing
`SENTRY_WEBHOOK_SECRET`. The honest path when the API create isn't available.

### Wiring

- Add a `sentry` command to the existing `auth_app` Typer group in `loopy_cli/auth.py`; defer
  heavy HTTP imports into the body, matching the module's convention.
- `loopy init`: add `SENTRY_AUTH_TOKEN` to the environment credentials the wizard already
  detects (alongside `ANTHROPIC_API_KEY` / `DAYTONA_API_KEY`) and offer to run `auth sentry`
  when a workflow triggers on `Sentry.*`.
- `loopy auth status`: report the Sentry integration (slug + whether `SENTRY_WEBHOOK_SECRET` is
  set, best-effort `GET` to confirm the webhook URL).
- `loopy doctor`: flag a missing `SENTRY_WEBHOOK_SECRET` when a workflow triggers on `Sentry.*`
  (runnability check, alongside the existing GitHub checks).

## Effort

Small. Clean HMAC over raw JSON body, body-only discrimination (no runner change), two events.
The only shared-code touches are the three minimal generalizations above, each a few lines and
each leaving an additive seam for a future third provider.
