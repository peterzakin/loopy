# Built-in integrations

Plans for shipping outside services as first-class built-ins, the same way `Github.*` works
today: a workflow names an event in its `on:` and the compiler injects the event contract and a
producing sensor with zero `registry.yml` and zero `sensors/` code.

- **[sentry.md](sentry.md)** — **Sentry** (`Sentry.IssueCreated` / `IssueResolved` /
  `AlertTriggered`). The second built-in, now shipped. Drives the canonical `Incident` loop
  (error → triage → fix) and promotes the hand-written example sensor in
  `examples/incidents/sensors/sensors.py` into the platform. Self-contained: it includes the
  minimal generalization of the GitHub-only machinery (a second webhook path, mapper set,
  secret, and verifier) and deliberately deferred a full multi-provider registry to a third
  provider.
- **[datadog.md](datadog.md)** — **Datadog** (`Datadog.MonitorAlerted` / `MonitorRecovered`).
  The third built-in and the one that lands the deferred registry seam. Datadog differs from the
  first two in two ways that shape the whole plan: its webhook **signs no body** (auth is a
  shared secret in a custom header, so the verifier is a token compare, not an HMAC), and its
  payload is **user-templated**, so the built-in must prescribe a canonical payload the user
  pastes once.

> Scope note: earlier drafts of this branch also planned Slack and Linear. Those remain
> deferred so each PR does one thing well; they can return as their own plans later, reusing the
> same `BuiltinProvider` machinery.

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
