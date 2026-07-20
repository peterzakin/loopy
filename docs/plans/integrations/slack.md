# Slack (built-in integration) — spike

**Question:** can the Slack API power a new trigger, so a workflow fires on
`on: Slack.MessagePosted` / `on: Slack.AppMention` the way `Github.*` and `Sentry.*` work today?

**Verdict: yes.** The `BuiltinProvider` seam the Sentry work left behind
(`loopy_core/builtins.py:110`) fits Slack almost exactly — contracts, mappers, verifier, and CLI
wiring are all additive, per the Sentry playbook. There are two genuine deltas, both anticipated
by [sentry.md](sentry.md):

1. **The `url_verification` challenge handshake.** Slack validates the Request URL at save time
   by POSTing `{"type": "url_verification", "challenge": "..."}` and requiring the challenge
   echoed back in the response body. Our webhook runner
   (`loopy_runtime/sensors/runner.py`) always answers `{"emitted": [...]}` and has no way to
   return a custom body — this is the one place sentry.md could say "no runner change needed"
   and Slack can't. Small, provider-neutral fix below.
2. **Timestamp in the signed material.** Slack signs `v0:{timestamp}:{body}` and expects a
   ±5-minute replay guard — the verifier reads one more header than GitHub/Sentry's
   body-only HMAC. The `Verifier` seam (`Callable[[bytes, Mapping], None]`) already receives
   headers, so this needs **no** framework change, just a slightly richer verifier.

There is also a **zero-framework-change fallback**: a user-authored `@sensor(poll="1m",
emits=...)` calling `conversations.history` with `SLACK_BOT_TOKEN` from `sensors/.env`, windowed
on `tick.last_run`. That works today and is the fastest way to demo a Slack-triggered loop, but
it isn't the product answer — built-ins exist so users write zero sensor code.

## Webhook facts (verified against Slack docs, July 2026)

- The **Events API** POSTs JSON to one configured **Request URL** per app; which event types
  arrive is chosen per-app under Event Subscriptions (`message.channels`, `app_mention`,
  `reaction_added`, ...). One URL, many event types — same fan-out shape as GitHub.
- **Handshake:** on saving the Request URL, Slack sends
  `{"type": "url_verification", "token": ..., "challenge": ...}` and the endpoint must respond
  2xx **within 3 seconds** with the `challenge` value (JSON `{"challenge": "..."}` or plain
  text). The URL cannot be saved until this succeeds — so unlike Sentry ("creation succeeds
  with any webhookUrl"), setup requires the engine to be up and reachable *first*.
- **Signature:** `X-Slack-Signature` = `v0=` + hex HMAC-SHA256 of the basestring
  `v0:{X-Slack-Request-Timestamp}:{raw body}`, keyed by the app's **Signing Secret**. Reject
  timestamps more than **5 minutes** from local time (replay guard). The handshake request is
  signed too, so verification still gates everything.
- **Ack deadline & retries:** Slack expects a 2xx within **3 seconds**; on failure/timeout it
  retries up to 3 times (`X-Slack-Retry-Num: 1|2|3`, `X-Slack-Retry-Reason`). Retries carry the
  same `event_id`.
- **Envelope:** deliveries are `{"type": "event_callback", "event_id": ..., "event_time": ...,
  "team_id": ..., "event": {"type": "message", ...}}` — the inner `event.type` discriminates,
  so mappers key off the body alone, exactly like GitHub/Sentry. No header plumbing.
- **Socket Mode** (websocket, no public URL) exists but is a different transport — it would
  need a persistent client alongside the poll scheduler, not a webhook route. Out of scope;
  noted as the answer for users who can't expose a URL (they can use the polling fallback).

## Events (proposed contract)

Start with three, discriminated on `event.type` (+ subtype filtering) in the body:

- `Slack.MessagePosted` — `event.type == "message"`, **no `subtype`, no `bot_id`**. Fields:
  `channel: str`, `user: str`, `text: str`, `ts: str`, `thread_ts: str` (`""` when not in a
  thread). Dropping bot/subtype messages in the mapper is load-bearing: a loop whose agent
  posts its result back to Slack must not re-trigger itself (the cascade budget would catch
  runaway, but the mapper returning `None` is the correct gate).
- `Slack.AppMention` — `event.type == "app_mention"`. Fields: `channel: str`, `user: str`,
  `text: str`, `ts: str`. The "assign work to the agent from Slack" trigger; requires only the
  `app_mentions:read` scope, making it the least-privileged flagship.
- `Slack.ReactionAdded` — `event.type == "reaction_added"`. Fields: `reaction: str`,
  `user: str`, `channel: str`, `message_ts: str`. Enables "👍 to approve" human-in-the-loop
  steps.

`ts`/`thread_ts` stay `str`, not timestamps — they are Slack's message identity keys and must
round-trip verbatim into `chat.postMessage(thread_ts=...)` replies.

**Event identity for dedup:** map Slack's `event_id` into the runtime `Event.id` so the bus's
seen-set (at-least-once dedup) absorbs Slack's retries for free. The synthesized-id path the
other providers use would process each retry as a fresh event.

## The one framework change — challenge handshake in the runner

Add an optional per-path pre-dispatch hook to `FastAPISensorRunner.register_webhook`:

```python
# loopy_runtime/sensors/runner.py
Handshake = Callable[[dict], dict | None]  # payload -> response body to short-circuit with

def register_webhook(self, path, fn, *, verify=None, handshake=None): ...

# in the signed-path handler, after verify + json.loads, before _dispatch_path:
if (hs := self._handshakes.get(path)) and (resp := hs(payload)) is not None:
    return resp
```

Slack's hook is three lines in `slack_webhook.py`:

```python
def url_verification(payload: dict) -> dict | None:
    if payload.get("type") == "url_verification":
        return {"challenge": payload.get("challenge", "")}
    return None
```

Provider-neutral (Zoom, Twilio, and Microsoft webhooks have equivalent handshakes), runs
*after* signature verification, and unregistered paths behave exactly as today. This is the
only edit to `runner.py`.

The 3-second ack deadline needs no work today: built-in mappers are pure dict transforms and
`receiver.receive` is an in-process bus publish. If a slow consumer ever appears ahead of the
ack, the fix is ack-then-publish, but don't build that now.

## Verifier — `loopy_runtime/scm/slack_webhook.py` (new)

Mirrors `sentry_webhook.py` (shared `SignatureError`), plus the two Slack-specific steps:

```python
def verify_signature(secret, body, sig_header, ts_header, *, now=None):
    if abs((now or time.time()) - int(ts_header)) > 300:
        raise SignatureError("stale timestamp (possible replay)")
    base = b"v0:" + ts_header.encode() + b":" + body
    expected = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig_header):
        raise SignatureError("signature mismatch")
```

`signature_verifier(secret)` adapts it to the runner's `(raw, headers)` seam, reading
`X-Slack-Signature` and `X-Slack-Request-Timestamp`. Inject `now` for tests.

## Everything else is the Sentry checklist verbatim

Per [sentry.md](sentry.md) §"Minimal generalization" — all additive, no new machinery:

| Piece | File | Change |
|---|---|---|
| Provider entry | `loopy_core/builtins.py` | `SLACK_PREFIX`, `SLACK_EVENTS`, `BuiltinProvider("slack", "Slack.", "/hooks/slack", SLACK_EVENTS, "SLACK_SIGNING_SECRET")` |
| Compiler | `loopy_core/compile/builtins.py` | none — already generic over `BUILTIN_PROVIDERS` |
| Mappers | `loopy_runtime/scm/slack_builtins.py` (new) | `MAPPERS` dict, payload → fields \| `None` |
| Mapper lookup | `loopy_runtime/sensors/loader.py` | add `SLACK_MAPPERS` to the merged dict |
| CLI wiring | `loopy_cli/__init__.py` (in-process serve) | add slack to the verifier table + pass the handshake hook |
| Secret status | `loopy_cli/integrations.py` | `_SECRET_FIX["slack"]` fix hint |
| Error codes | `loopy_core/compile/codes.py` | none — E112/E215 are generic |

## Setup flow (and why `loopy auth slack` is a later PR)

Users create a Slack app (from a manifest we can print), enable Event Subscriptions, paste the
public `/hooks/slack` URL, and copy the Signing Secret into `loopy.env` as
`SLACK_SIGNING_SECRET`. Two wrinkles vs Sentry:

- **Order matters.** Slack refuses to save the Request URL until the challenge succeeds, so
  the instructions must say "start `loopy run` (or deploy) first, then paste the URL" —
  `loopy integrations` already prints the delivery URL, and `loopy doctor` already flags the
  unset secret via `secret_env`.
- **No clean bootstrap-token story.** Sentry's `loopy auth sentry` can create the webhook via a
  User token; Slack app creation is dashboard-first (`apps.manifest.create` needs a
  short-lived *App Configuration Token* the user must mint by hand anyway). So v1 ships
  documented manual setup + a printable app manifest; a `loopy auth slack` wrapper is possible
  later but saves little.

## Tests

Mirror `tests/test_sentry_builtin.py`, plus the Slack-specific seams:

- Compile: `on: Slack.AppMention` compiles clean, injects `source="builtin"`,
  `provider="slack"`, path `/hooks/slack`; unknown `Slack.X` → E112; user event in the
  `Slack.` namespace → E215.
- Drift: add `"slack"` to `test_builtins.py::test_catalog_and_mappers_agree` (it hardcodes the
  provider→mappers table and **fails** until updated) and a representative fixture payload per
  event for `test_each_mapper_returns_exactly_its_contract_fields`.
- Verifier: good signature; mismatch → 401; stale timestamp → 401; missing secret → dev warn.
- Handshake: signed `url_verification` POST → 200 `{"challenge": ...}` and **no** event
  emitted; ordinary deliveries on the same path still dispatch.
- Mappers: bot/subtype messages → `None`; retry with same `event_id` → deduped by the bus.
- Update `tests/test_integrations.py`, which currently asserts `"slack"` is an *unknown*
  integration.

## Effort

Roughly the Sentry PR minus the generalization work it already did, plus the runner hook:
~1 day for provider + mappers + verifier + handshake + tests, plus docs
(`docs_md/authoring.md` catalog blurb, README, landing catalog). The polling fallback is ~an
hour and needs nothing from this plan.
