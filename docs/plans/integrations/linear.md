# Linear

**Loop it serves:** issue → work (feeds a `WorkItem`-style loop; issue/comment triggers for
planning and triage agents).

**Prereq:** [00 — Provider framework](00-provider-framework.md).

## Webhook facts

- Linear webhooks POST JSON to one configured URL.
- Signature: header **`Linear-Signature`** = hex HMAC-SHA256 of the **raw body** keyed by the
  webhook's signing secret. No prefix (like Sentry, unlike GitHub).
- Discriminator: the body carries **`type`** (`Issue`, `Comment`, `Project`, `Cycle`, ...)
  and **`action`** (`create`, `update`, `remove`). Both are in the JSON body — no header
  needed, so Linear needs *none* of the header plumbing Sentry/Slack do.
- Replay guard: body includes **`webhookTimestamp`** (ms). Linear recommends rejecting
  deliveries older than ~1 minute. Enforce in the verifier (it has the parsed-or-raw body;
  simplest is to check after parse in the mapper-agnostic prehandler, or parse the timestamp
  in the verifier from the raw bytes).

## Contract — `loopy_core/builtins.py`

```python
"linear": ProviderSpec(
    name="linear", prefix="Linear.", webhook_path="/hooks/linear",
    secret_env="LINEAR_WEBHOOK_SECRET", events=LINEAR_EVENTS,
),
```

```python
LINEAR_EVENTS = {
    "Linear.IssueCreated": {        # type=Issue, action=create
        "id": "str", "identifier": "str", "title": "str", "description": "str",
        "priority": "int", "state": "str", "team": "str", "assignee": "str", "url": "url",
    },
    "Linear.IssueUpdated": {        # type=Issue, action=update
        "id": "str", "identifier": "str", "title": "str", "state": "str",
        "assignee": "str", "url": "url",
    },
    "Linear.CommentCreated": {      # type=Comment, action=create
        "issue_id": "str", "body": "str", "author": "str", "url": "url",
    },
}
```

`identifier` is the human key (e.g. `ENG-123`); `id` is the UUID. Both are useful to agents.

## Mappers — `loopy_runtime/scm/linear_builtins.py` (new)

```python
def _issue_created(body: dict) -> dict | None:
    if body.get("type") != "Issue" or body.get("action") != "create":
        return None
    d = body["data"]
    return {"id": d["id"], "identifier": d.get("identifier", ""), "title": d["title"],
            "description": d.get("description") or "", "priority": d.get("priority", 0),
            "state": (d.get("state") or {}).get("name", ""),
            "team": (d.get("team") or {}).get("key", ""),
            "assignee": (d.get("assignee") or {}).get("name", ""),
            "url": d.get("url", "")}
# _issue_updated (action == "update"), _comment_created (type == "Comment")
MAPPERS = {"Linear.IssueCreated": _issue_created, "Linear.IssueUpdated": _issue_updated,
           "Linear.CommentCreated": _comment_created}
```

Register in `scm/builtin_registry.py`. Linear nests the entity under `body["data"]`; some
relations (`state`, `assignee`, `team`) arrive as nested objects on create but may be bare
IDs on update — normalize with `.get(...) or {}` and lock a fixture per event.

## Verifier — `loopy_runtime/scm/linear_webhook.py` (new)

HMAC-hex over raw body (same as Sentry) plus the timestamp freshness check:

```python
SIGNATURE_HEADER = "linear-signature"
MAX_SKEW_MS = 60_000
def verify_signature(secret, body, sig):
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not sig or not hmac.compare_digest(expected, sig):
        raise SignatureError("signature mismatch")
    ts = json.loads(body or b"{}").get("webhookTimestamp")
    # reject if ts is missing or |now - ts| > MAX_SKEW_MS
```

The skew check needs a clock; keep it injectable (`now_ms` param) so tests are deterministic.
Register in `scm/verifiers.py` under `"linear"`.

## Tests

- Drift test covers `Linear.*` automatically.
- `tests/test_linear_builtin.py`: compile happy-path (`on: Linear.IssueCreated`), signature
  good/bad, timestamp-too-old rejected, and per-event mapper tests from fixtures (wrong
  `type`/`action` → `None`).

## Docs

`#linear` section in `loopy-landing/docs/integrations.html` (events, sample payloads,
`LINEAR_WEBHOOK_SECRET`), add to the catalog and the landing hero service list.

## Credentials & setup

**No OAuth for a single workspace.** The user creates a webhook in Settings → API →
Webhooks — no app, no OAuth grant. Setup:

1. Add a webhook pointed at `https://<host>/hooks/linear`, subscribed to the resources you
   want (Issues, Comments).
2. Copy Linear's generated **signing secret** into `LINEAR_WEBHOOK_SECRET` in the sandbox's
   `env_file`.
3. Absent the secret, `/hooks/linear` runs unverified with the standard loud dev warning.

An OAuth app is only needed if you want multi-workspace distribution or the agent to call
Linear's GraphQL API on behalf of a user (the write-back / action side — out of scope). For
single-tenant write-back, a personal API key is simpler than OAuth.

## Effort

Small — arguably the cleanest of the four (all discrimination is in-body, no header
plumbing). The one novel bit is the timestamp replay guard, which Slack reuses.
