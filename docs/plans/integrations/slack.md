# Slack

**Loop it serves:** the human-in-the-loop / chatops seam — a mention or slash command as a
*trigger*, and (later) notifications as an *output*. Highest leverage because every other loop
wants a way for a person to kick it off or get pinged.

**Prereq:** [00 — Provider framework](00-provider-framework.md). Slack is the biggest of the
four and is the reason [00 §6] adds runner extension points.

## Why Slack is different

Three things no other provider needs, all handled in the runner (not per-mapper):

1. **Timestamped signing.** `X-Slack-Signature` = `v0=` + HMAC-SHA256 of the string
   `v0:{X-Slack-Request-Timestamp}:{raw_body}` keyed by the **signing secret**. The timestamp
   is a separate header and must be folded into the signed base string. Reject requests with a
   timestamp skew > 5 minutes (replay protection) — reuse the freshness pattern from
   [linear.md].
2. **URL verification handshake.** When you register the endpoint, Slack POSTs
   `{"type": "url_verification", "challenge": "<x>"}` and expects the raw `challenge` echoed
   back (200, `text/plain` or JSON `{"challenge": x}`). This must short-circuit *before*
   fan-out to mappers.
3. **Two body encodings.** Events API deliveries are JSON; **slash commands** are
   `application/x-www-form-urlencoded`. Both are signed the same way (HMAC over raw body), but
   parse differently.

## Runner extension points ([00 §6])

Add to `FastAPISensorRunner.register_webhook` an optional per-path:

- `prehandler(raw: bytes, headers) -> Response | None` — Slack returns the challenge response
  here for `url_verification` and lets everything else fall through (`None`).
- `parse_body(raw: bytes, headers) -> dict` — default JSON; Slack's parses form-encoding when
  `content-type` is `x-www-form-urlencoded`, else JSON. Also folds selected headers into
  `payload["__headers__"]` (shared with Sentry).

These default to today's behavior, so GitHub/Sentry/Linear/Datadog are unaffected.

## Contract — `loopy_core/builtins.py`

```python
"slack": ProviderSpec(
    name="slack", prefix="Slack.", webhook_path="/hooks/slack",
    secret_env="SLACK_SIGNING_SECRET", events=SLACK_EVENTS,
),
```

```python
SLACK_EVENTS = {
    "Slack.AppMentioned": {         # event_callback / app_mention
        "channel": "str", "user": "str", "text": "str", "ts": "str", "thread_ts": "str",
    },
    "Slack.MessagePosted": {        # event_callback / message (non-bot)
        "channel": "str", "user": "str", "text": "str", "ts": "str", "thread_ts": "str",
    },
    "Slack.SlashCommand": {         # form-encoded slash command
        "command": "str", "text": "str", "user_id": "str", "user_name": "str",
        "channel_id": "str", "channel_name": "str", "response_url": "url", "trigger_id": "str",
    },
}
```

`response_url` and `trigger_id` are the handles an agent step needs to reply to or open a
modal on the slash command; keep them in the contract even though the loop only *triggers* on
them for now.

## Mappers — `loopy_runtime/scm/slack_builtins.py` (new)

```python
def _app_mentioned(body: dict) -> dict | None:
    if body.get("type") != "event_callback":
        return None
    e = body.get("event", {})
    if e.get("type") != "app_mention":
        return None
    return {"channel": e["channel"], "user": e.get("user", ""), "text": e.get("text", ""),
            "ts": e.get("ts", ""), "thread_ts": e.get("thread_ts", "")}

def _slash_command(body: dict) -> dict | None:
    # form-encoded → dict by parse_body; slash commands have `command`, no `type`.
    if "command" not in body:
        return None
    return {k: body.get(k, "") for k in
            ("command", "text", "user_id", "user_name", "channel_id", "channel_name",
             "response_url", "trigger_id")}
# _message_posted: type==event_callback, event.type==message, ignore bot_id/subtype edits
MAPPERS = {"Slack.AppMentioned": _app_mentioned, "Slack.MessagePosted": _message_posted,
           "Slack.SlashCommand": _slash_command}
```

Guard `_message_posted` against bot echoes (`event.bot_id` present or `subtype` set) to avoid
loops where an agent's own Slack post re-triggers the workflow. Register in
`scm/builtin_registry.py`.

## Verifier — `loopy_runtime/scm/slack_webhook.py` (new)

```python
SIG_HEADER, TS_HEADER = "x-slack-signature", "x-slack-request-timestamp"
MAX_SKEW_S = 300
def verify_signature(secret, body, headers, now_s):
    ts = headers.get(TS_HEADER)
    if not ts or abs(now_s - int(ts)) > MAX_SKEW_S:
        raise SignatureError("stale or missing Slack timestamp")
    base = f"v0:{ts}:".encode() + body
    expected = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, headers.get(SIG_HEADER, "")):
        raise SignatureError("signature mismatch")
```

This verifier needs **headers + a clock**, both already available at the runner edge. Register
its factory in `scm/verifiers.py`. Keep `now_s` injectable for tests.

## `url_verification` prehandler

```python
def prehandler(raw, headers):
    body = json.loads(raw or b"{}")
    if body.get("type") == "url_verification":
        return PlainTextResponse(body.get("challenge", ""))
    return None
```

Slack registers this on `/hooks/slack`; other providers pass `None`.

## Tests

- Drift test covers `Slack.*` automatically.
- `tests/test_slack_builtin.py`: signature good/bad and stale-timestamp rejected;
  `url_verification` returns the challenge and emits nothing; slash-command form body parses
  and maps; `app_mention` maps; a bot-authored `message` returns `None`.

## Docs

`#slack` section in `loopy-landing/docs/integrations.html` covering the three events, the
signing-secret setup, the events-vs-slash-command distinction, and `SLACK_SIGNING_SECRET`.
Add to the catalog and hero.

## Credentials & setup

Slack is the **only** provider that touches OAuth, and only as the install step. The user
creates a **Slack app** (required even to receive events) and installs it to their workspace;
the install is a one-click OAuth *grant*, but **Loopy runs no OAuth flow** and holds no Slack
token on the trigger path. Setup:

1. Create a Slack app (from an app manifest is easiest). Enable **Event Subscriptions** with
   the request URL `https://<host>/hooks/slack` (Slack immediately fires the
   `url_verification` handshake — the prehandler answers it), subscribe to `app_mention` /
   `message.channels`, and register any **slash commands** (also pointed at `/hooks/slack`).
2. Copy the app's **Signing Secret** into `SLACK_SIGNING_SECRET` in the sandbox's `env_file`.
3. **Install the app to the workspace** (the OAuth grant) so Slack starts delivering events.
4. Absent the secret, `/hooks/slack` runs unverified with the standard loud dev warning.

No bot token is needed for triggers or for slash-command replies (`response_url` is
unauthenticated). A bot token via OAuth is only needed for proactive post-back — the output
side, out of scope here. Multi-workspace distribution would require Loopy to implement the
real OAuth flow; single-tenant self-host does not.

## Effort

Largest of the four. Budget the runner extension points ([00 §6]) as part of this plan if they
weren't landed with 00. Everything else (Sentry/Linear/Datadog) rides those seams for free.
