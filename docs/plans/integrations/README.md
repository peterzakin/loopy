# Built-in Sentry integration

Plan for shipping **Sentry** as the second first-class built-in integration, the same way
`Github.*` works today: a workflow names `on: Sentry.IssueCreated` and the compiler injects
the event contract and a producing sensor with zero `registry.yml` and zero `sensors/` code.

Sentry is the natural next integration: it drives the canonical `Incident` loop (error →
triage → fix) and is already the hand-written example sensor in
`examples/incidents/sensors/sensors.py`, so this promotes existing example code into the
platform.

**The plan:** [sentry.md](sentry.md). It is self-contained — it includes the minimal
generalization of the GitHub-only machinery that Sentry needs (a second webhook path, mapper
set, secret, and verifier), and defers a full multi-provider registry until a third provider
actually lands.

> Scope note: earlier drafts of this branch also planned Slack, Linear, and Datadog. Those are
> removed so this PR does one thing well. They can return as their own PRs later; the Sentry
> work deliberately leaves seams (see [sentry.md](sentry.md) "Minimal generalization") that a
> third provider would generalize further.

## How a built-in works today (GitHub), for reference

| Piece | File | Role |
|---|---|---|
| Reserved namespace + event contracts | `loopy_core/builtins.py` (`GITHUB_PREFIX`, `GITHUB_EVENTS`) | Field maps the compiler injects; reserves the `Github.` prefix |
| Compiler injection | `loopy_core/compile/builtins.py` | Register the contract, synthesize a webhook sensor on `/hooks/github`, guard the namespace (E215), report unknowns (E112) |
| Payload → field mappers | `loopy_runtime/scm/github_builtins.py` (`BUILTIN_MAPPERS`) | One delivery → one event's fields, or `None` when the delivery isn't this event's concern |
| Signature verification | `loopy_runtime/scm/github_webhook.py` | HMAC-SHA256 of the raw body (`X-Hub-Signature-256`) verified at the edge |
| Wiring | `loopy_cli/__init__.py` (~L1195) | Reads `GITHUB_WEBHOOK_SECRET`, attaches the verifier, registers built-in sensors |
| Drift test | `tests/test_builtins.py` | Asserts every contract has a matching mapper (the two halves never drift) |
| Docs | `loopy-landing/docs/integrations.html` | The catalog users read |

The data model already anticipates more providers: `Sensor.provider` /
`SensorSpec.provider` exist and `source="builtin"` is a discriminator. What Sentry has to
generalize is the GitHub *string* hardcoding in the compiler, the runtime mapper lookup, and
the CLI wiring — kept minimal (see the plan).
