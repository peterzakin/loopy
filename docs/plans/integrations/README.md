# Built-in integrations beyond GitHub

Plans for shipping **Sentry**, **Slack**, **Linear**, and **Datadog** as first-class
built-in integrations, the same way `Github.*` works today: a workflow names a
`Provider.Event` in its `on:` and the compiler injects the event contract and a producing
sensor with zero `registry.yml` and zero `sensors/` code.

## How a built-in works today (GitHub)

A built-in is four coordinated pieces, plus tests and docs:

| Piece | File | Role |
|---|---|---|
| Reserved namespace + event contracts | `loopy_core/builtins.py` (`GITHUB_PREFIX`, `GITHUB_EVENTS`) | Field maps the compiler injects; reserves the `Github.` prefix |
| Compiler injection | `loopy_core/compile/builtins.py` | For each referenced `Github.*` event: register the contract, synthesize a webhook sensor on `/hooks/github`, guard the namespace (E215), report unknowns (E112) |
| Payload → field mappers | `loopy_runtime/scm/github_builtins.py` (`BUILTIN_MAPPERS`) | One delivery → one event's fields, or `None` when the delivery isn't this event's concern |
| Signature verification | `loopy_runtime/scm/github_webhook.py` | HMAC-SHA256 of the raw body (`X-Hub-Signature-256`) verified at the edge |
| Wiring | `loopy_cli/__init__.py` (~L1195) | Reads `GITHUB_WEBHOOK_SECRET`, attaches the verifier to `/hooks/github`, registers built-in sensors |
| Drift test | `tests/test_builtins.py` | Asserts every contract in `GITHUB_EVENTS` has a matching mapper (the two halves never drift) |
| Docs | `loopy-landing/docs/integrations.html` | The catalog users read |

The design already anticipates more providers: `Sensor.provider` and `SensorSpec.provider`
exist, `source="builtin"` is a discriminator, and the runner's `verify(body, headers)` seam
is provider-agnostic. What is **not** yet generalized is the GitHub-specific hardcoding in the
compiler (`_BUILTIN_PATH = "/hooks/github"`), the runtime mapper lookup
(`from ...github_builtins import BUILTIN_MAPPERS`), and the CLI wiring
(`GITHUB_WEBHOOK_SECRET`, `/hooks/github` string check).

## Order of work

1. **[00 — Provider framework](00-provider-framework.md)** (prerequisite). Generalize the
   GitHub-only hardcoding into a provider registry so a new integration is *data*, not a
   fork of the injection/wiring logic. Ships with GitHub as the first registry entry (no
   behavior change) to prove the refactor.
2. **[Sentry](sentry.md)** and **[Linear](linear.md)** next: both are clean HMAC-hex-over-raw-JSON
   webhooks that discriminate by a header and/or a `type`+`action` in the body. They exercise
   the framework with the least provider-specific plumbing.
3. **[Slack](slack.md)**: the largest. Adds three things the runner doesn't do yet — a
   timestamped signing scheme (`v0:{ts}:{body}`) with replay protection, a `url_verification`
   challenge handshake, and form-encoded slash-command bodies alongside JSON events.
4. **[Datadog](datadog.md)**: different in kind. Datadog webhooks carry a **user-defined**
   payload template and are **not signed by default**, so the built-in ships a canonical
   payload template and a shared-secret header check rather than a vendor HMAC.

## The shape every provider plan follows

Once [00](00-provider-framework.md) lands, each provider is the same five edits:

- **Contract** — add a `ProviderSpec` entry (prefix, `/hooks/<provider>` path, `secret_env`,
  event field maps) in `loopy_core/builtins.py`.
- **Mappers** — add `loopy_runtime/scm/<provider>_builtins.py` with a `MAPPERS` dict and
  register it in the runtime mapper registry.
- **Verifier** — add the provider's signature check in `loopy_runtime/scm/<provider>_webhook.py`
  and register its factory in the verifier registry.
- **Tests** — the drift test iterates all providers automatically; add a compile happy-path
  and a verifier unit test.
- **Docs** — a section in `loopy-landing/docs/integrations.html` and a bump to the catalog on
  the landing hero.

## Non-negotiable design choices

- **No new events on the bus without a contract.** Built-in contracts live in `builtins.py`
  and are injected only when referenced (no catalog dump), exactly as GitHub does today.
- **Verify at the edge, once.** Every provider path gets a verifier keyed by its own secret
  env var; an unset secret degrades to an unverified dev mode with a loud warning (matching
  the current GitHub behavior), never a silent pass in production.
- **Keep the two halves in lockstep.** A contract field with no mapper (or vice versa) must
  fail `tests/test_builtins.py`. This is what makes a built-in trustworthy.
