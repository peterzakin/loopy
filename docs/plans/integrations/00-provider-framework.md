# 00 — Provider framework (prerequisite)

**Goal.** Turn the GitHub-only built-in machinery into a provider *registry* so adding
Sentry / Slack / Linear / Datadog is data plus one mapper module each, not a fork of the
compiler and CLI wiring. Ship it with GitHub as the sole registry entry so there is **no
behavior change** on merge; the four providers then land as additive entries.

## Why this is first

Three places hardcode GitHub and would otherwise be copy-pasted per provider:

- `loopy_core/compile/builtins.py`: `_BUILTIN_PATH = "/hooks/github"`, the `GITHUB_EVENTS`
  lookup, `provider="github"`, and GitHub-specific E215/E112 messages.
- `loopy_runtime/sensors/loader.py`: `builtin_webhook_sensor` imports `BUILTIN_MAPPERS`
  straight from `github_builtins`.
- `loopy_cli/__init__.py` (~L1195-1227): reads `GITHUB_WEBHOOK_SECRET`, string-matches
  `/hooks/github`, and builds a GitHub `signature_verifier`.

## Changes

### 1. `loopy_core/builtins.py` — a provider registry

Replace the flat `GITHUB_PREFIX` / `GITHUB_EVENTS` / `is_reserved` with a keyed structure.
Keep GitHub's contracts verbatim; only restructure.

```python
from dataclasses import dataclass, field

@dataclass(frozen=True)
class ProviderSpec:
    name: str                       # "github", "sentry", ...
    prefix: str                     # "Github.", "Sentry." (reserved namespace)
    webhook_path: str               # "/hooks/github"
    secret_env: str                 # "GITHUB_WEBHOOK_SECRET"
    events: dict[str, dict[str, str]]  # event name -> {field: terse type}

BUILTIN_PROVIDERS: dict[str, ProviderSpec] = {
    "github": ProviderSpec(
        name="github", prefix="Github.", webhook_path="/hooks/github",
        secret_env="GITHUB_WEBHOOK_SECRET", events=GITHUB_EVENTS,
    ),
    # sentry / slack / linear / datadog appended by their own plans
}

def provider_for_event(name: str) -> ProviderSpec | None:
    return next((p for p in BUILTIN_PROVIDERS.values() if name.startswith(p.prefix)), None)

def is_reserved(name: str) -> bool:
    return provider_for_event(name) is not None

def all_builtin_events() -> dict[str, dict[str, str]]:
    return {ev: fields for p in BUILTIN_PROVIDERS.values() for ev, fields in p.events.items()}
```

Keep `GITHUB_EVENTS`, `GITHUB_PREFIX`, `BUILTIN_AGENTS`, `is_builtin_agent` as-is so nothing
else needs touching in this step. `is_reserved` keeps its signature.

### 2. `loopy_core/compile/builtins.py` — drive injection off the registry

- Delete `_BUILTIN_PATH`. In `inject_builtins`, for each referenced reserved event, resolve
  its provider with `provider_for_event(name)`; use `provider.events[...]` for the contract,
  `provider.webhook_path` for the synthesized sensor's path, and `provider.name` for
  `Sensor(provider=...)`.
- E112 (unknown built-in): list known names from `all_builtin_events()` instead of
  `GITHUB_EVENTS`. If the event's prefix matches a known provider but the event isn't in that
  provider's catalog, scope the "known built-ins" list to that provider for a better message.
- E215 (reserved namespace): the guard already keys off `is_reserved`; only the message text
  ("built-in GitHub events") needs to name the matched provider generically, e.g.
  `f"the reserved '{provider.prefix}' namespace (built-in {provider.name} events)"`.

No structural change to the pass — same order (before `resolve_refs`/`cross_check`), same
diagnostics, just registry-driven values.

### 3. Runtime mapper registry — `loopy_runtime/scm/builtin_registry.py` (new)

A single lookup that merges every provider's mappers, so `builtin_webhook_sensor` no longer
imports one provider's module directly.

```python
from loopy_runtime.scm.github_builtins import BUILTIN_MAPPERS as _github

BUILTIN_MAPPERS: dict[str, Mapper] = {**_github}
# sentry/slack/linear/datadog append their MAPPERS here in their own plans.
```

Update `loopy_runtime/sensors/loader.py::builtin_webhook_sensor` to
`from loopy_runtime.scm.builtin_registry import BUILTIN_MAPPERS`.

> Note: do **not** name the package `providers` — `loopy_runtime/providers.py` already exists
> for sandbox/model providers. Use `scm/builtin_registry.py` and `scm/<provider>_builtins.py`.

### 4. Verifier registry — `loopy_runtime/scm/verifiers.py` (new)

Each provider contributes a `verifier(secret) -> Callable[[bytes, Mapping], None]` factory.
GitHub's already exists (`github_webhook.signature_verifier`).

```python
from loopy_runtime.scm.github_webhook import signature_verifier as _github_verifier

VERIFIER_FACTORIES: dict[str, VerifierFactory] = {
    "github": _github_verifier,
    # "sentry": ..., "slack": ..., "linear": ..., "datadog": ...
}
```

### 5. `loopy_cli/__init__.py` — provider-driven wiring

Replace the GitHub-specific block (~L1195-1227) with a per-sensor lookup keyed by
`sensor.provider`:

```python
from loopy_core.builtins import BUILTIN_PROVIDERS
from loopy_runtime.scm.verifiers import VERIFIER_FACTORIES

for sensor in m.sensors:
    if sensor.trigger.kind != "webhook" or not sensor.trigger.path:
        continue
    fn = builtin_webhook_sensor(sensor) if sensor.source == "builtin" \
        else load_webhook_sensor(sensor, root)   # (keep the try/except degrade-to-synthesize)
    verify = None
    if sensor.source == "builtin" and sensor.provider in BUILTIN_PROVIDERS:
        spec = BUILTIN_PROVIDERS[sensor.provider]
        secret = os.environ.get(spec.secret_env)
        if secret:
            verify = VERIFIER_FACTORIES[sensor.provider](secret)
        else:
            warn_once(f"{spec.secret_env} not set; {spec.webhook_path} signatures are "
                      f"unverified (dev only — set it before exposing this endpoint)")
    sensor_runner.register_webhook(sensor.trigger.path, fn, verify=verify)
```

`warn_once` replaces the single `warned_unverified` bool with a per-provider set so each
provider warns once. User-authored (`source="module"`) sensors keep whatever verification
they configure themselves — this block only auto-attaches verifiers to built-ins.

### 6. Runner extension point (needed by Slack, stubbed here)

The signed-path handler in `loopy_runtime/sensors/runner.py` always `json.loads` the raw body
and returns `{"emitted": [...]}`. Slack needs (a) form-encoded parsing and (b) a
challenge-response short-circuit. Add an optional per-path `prehandler(raw, headers) ->
Response | None` and `parse_body(raw, headers) -> dict` seam now (default: JSON parse, no
prehandler) so Slack is additive later rather than a runner rewrite. GitHub/Sentry/Linear/
Datadog use the defaults. Details in [slack.md](slack.md).

## Tests

- `tests/test_builtins.py`: change the contract↔mapper drift assertion to iterate
  `all_builtin_events()` against `builtin_registry.BUILTIN_MAPPERS` (every contract has a
  mapper and vice versa). With only GitHub registered it must still pass unchanged.
- All existing GitHub tests must pass untouched — that is the acceptance bar for this step.

## Acceptance

- `uv run pytest tests/test_builtins.py` green.
- Full suite green (no behavior change).
- `loopy compile` on `examples/github` unchanged; `manifest.json` sensor entries identical.
