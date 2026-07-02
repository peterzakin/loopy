# Sentry

**Loop it serves:** error → triage → fix (the canonical `Incident` loop). Sentry is already
the example sensor in `examples/incidents/sensors/sensors.py`; this promotes it into the
built-in catalog so users get it with no `sensors/` code.

**Prereq:** [00 — Provider framework](00-provider-framework.md).

## Webhook facts

- Sentry (Integration / "Internal Integration") webhooks POST JSON to one configured URL.
- Signature: header **`Sentry-Hook-Signature`** = hex HMAC-SHA256 of the **raw body** keyed
  by the integration's **Client Secret**. No `sha256=` prefix (unlike GitHub).
- Resource discriminator: header **`Sentry-Hook-Resource`** names the resource
  (`issue`, `error`, `event_alert`, `metric_alert`, `comment`, ...). The action is in the
  JSON body's `action` field (`created`, `resolved`, `assigned`, ...).
- So a mapper discriminates on `(Sentry-Hook-Resource header, body["action"])`. The
  runtime mapper takes only the parsed body today; the resource is in the header.

### Header access for mappers

Mappers are `Callable[[dict], dict | None]` — body only. Two clean options:

- **A (recommended):** the runner already reads headers on signed paths. Fold the
  `Sentry-Hook-Resource` header into the parsed payload under a reserved key
  (e.g. `payload["__headers__"]`) before fan-out, so mappers stay `(dict) -> dict | None`.
  Do this generically in the runner's `parse_body` seam from [00 §6], not Sentry-specifically.
- **B:** widen `Mapper` to `Callable[[dict, Mapping], dict | None]`. Bigger blast radius
  (touches GitHub mappers + loader). Prefer A.

## Contract — `loopy_core/builtins.py`

Add to `BUILTIN_PROVIDERS`:

```python
"sentry": ProviderSpec(
    name="sentry", prefix="Sentry.", webhook_path="/hooks/sentry",
    secret_env="SENTRY_WEBHOOK_SECRET", events=SENTRY_EVENTS,
),
```

```python
SENTRY_EVENTS = {
    "Sentry.IssueCreated": {          # resource=issue, action=created
        "issue_id": "str", "title": "str", "culprit": "str",
        "level": "enum[debug, info, warning, error, fatal]",
        "project": "str", "url": "url",
    },
    "Sentry.IssueResolved": {         # resource=issue, action=resolved
        "issue_id": "str", "title": "str", "project": "str", "url": "url",
    },
    "Sentry.AlertTriggered": {        # resource=metric_alert/event_alert
        "alert_id": "str", "title": "str", "project": "str",
        "status": "enum[critical, warning, resolved]", "url": "url",
    },
}
```

## Mappers — `loopy_runtime/scm/sentry_builtins.py` (new)

```python
def _issue_created(body: dict) -> dict | None:
    if _resource(body) != "issue" or body.get("action") != "created":
        return None
    d = body["data"]["issue"]
    return {"issue_id": d["id"], "title": d["title"], "culprit": d.get("culprit", ""),
            "level": d.get("level", "error"), "project": d.get("project", {}).get("slug", ""),
            "url": d["web_url"]}
# _issue_resolved (action == "resolved"), _alert_triggered (resource in {metric_alert, event_alert})
MAPPERS = {"Sentry.IssueCreated": _issue_created, "Sentry.IssueResolved": _issue_resolved,
           "Sentry.AlertTriggered": _alert_triggered}
```

`_resource(body)` reads `body["__headers__"]["sentry-hook-resource"]` (per option A). Register
`MAPPERS` in `scm/builtin_registry.py`.

> Verify field paths against a live Sentry payload before finalizing (`data.issue.web_url`,
> `level`, `culprit` availability vary by resource). The contract is the source of truth the
> drift test enforces, so lock the payload sample into a fixture.

## Verifier — `loopy_runtime/scm/sentry_webhook.py` (new)

Same structure as `github_webhook.py`, minus the `sha256=` prefix:

```python
SIGNATURE_HEADER = "sentry-hook-signature"
def verify_signature(secret, body, sig):
    if not sig: raise SignatureError("missing Sentry-Hook-Signature")
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        raise SignatureError("signature mismatch")
```

Register `signature_verifier` in `scm/verifiers.py` under `"sentry"`.

## Tests

- Drift test covers `Sentry.*` automatically once registered.
- `tests/test_sentry_builtin.py`: compile happy-path (`on: Sentry.IssueCreated` with no
  registry/sensor), a good/bad signature unit test, and one mapper test per event from a
  recorded payload fixture (assert `resource != issue` and wrong `action` both return `None`).

## Docs

Add a `#sentry` section to `loopy-landing/docs/integrations.html` mirroring the GitHub section
(the three events, sample payloads, `SENTRY_WEBHOOK_SECRET`), and add Sentry to the catalog.
Then delete/annotate the hand-written Sentry sensor in `examples/incidents` to point at the
built-in (or keep it as the "here's what the built-in replaces" teaching example).

## Credentials & setup

**No OAuth.** The user creates a Sentry **Internal Integration** (Settings → Developer
Settings → Internal Integration). That is app-like but has no OAuth grant: it issues a
**Client Secret** used to sign webhooks. Setup:

1. Create the Internal Integration; enable the webhook and the resources you want (`issue`,
   `metric_alert`, ...), pointed at `https://<host>/hooks/sentry`.
2. Copy the **Client Secret** into `SENTRY_WEBHOOK_SECRET` in the sandbox's `env_file`.
3. Absent the secret, `/hooks/sentry` runs unverified with the standard loud dev warning.

Write-back (agent resolves/comments on an issue) would use the same Internal Integration's
auth token — that is the action side and is out of scope for this plan.

## Effort

Small. Clean HMAC, JSON body, header+action discrimination. The only shared-framework touch
is the generic `__headers__` fold in the runner (reused by Slack/Linear).
