# daytona-sandbox — DaytonaSandboxProvider

**Status:** draft
**Owner:** peter
**Date:** 2026-06-16

## Goal
Run agents in **Daytona** cloud sandboxes (isolated containers, fast cold start) as an alternative
to `LocalSubprocessSandbox`, behind the existing `SandboxProvider`/`Sandbox` Protocols — selectable
per deployment, no engine change.

## Context
Backend v1 shipped `LocalSubprocessSandbox` and deferred Daytona behind the Protocol. Daytona is the
README's sandbox provider. SDK: PyPI `daytona` (async `AsyncDaytona`): `create(
CreateSandboxFromImageParams(image, env_vars, resources))` → sandbox; `sandbox.process.exec(command,
cwd, env, timeout)` → `.result`/`.exit_code`; `daytona.delete(sandbox)`.

## Constraints & non-goals
- Implement `SandboxProvider.acquire(spec, secrets) -> Sandbox` and `Sandbox.exec/release` only —
  the runtime and harness are unchanged.
- The `daytona` SDK is an **optional dependency** (lazy import; clear error if missing and no client
  injected). Client is injectable for tests (no real service in CI).
- Secrets inject as the sandbox's `env_vars` (the sandbox is the trust boundary); tools inherit them.
- `exec(cmd: list[str])` → a shell string via `shlex.join`; `ExecResult` from `.result`/`.exit_code`
  (Daytona returns combined output; `stderr` is left empty).
- **Non-goals / documented gaps (v1):** full image-build mapping (our `image:` dict → a Daytona
  `Image`/snapshot — v1 uses a base-image string with an `image` override key); **network allowlist
  enforcement** (not in the basic create params — recorded on the spec, not yet enforced); a
  real-service integration test (CI uses a fake client); cron/poll.

## Approach
`sandbox/daytona.py`: `DaytonaSandbox` wraps a created sandbox (exec/release); `DaytonaSandboxProvider`
lazily constructs `AsyncDaytona()` (or takes an injected client) and `acquire`s with image + secrets
as `env_vars`. A `make_sandbox_provider(name)` factory + a `--sandbox local|daytona` flag on
`loopy-run` select the provider.

## Steps
- [ ] `sandbox/daytona.py`: `DaytonaSandbox.exec` (`shlex.join` → `process.exec` → `ExecResult`),
      `release` (`delete`); `DaytonaSandboxProvider.acquire` (create with image + `env_vars=secrets`).
      Lazy SDK import; injectable client.
- [ ] `_image(spec)`: derive a base-image string (`spec.image.get("image")` else a default);
      document the simplification.
- [ ] `sandbox/factory.py`: `make_sandbox_provider("local"|"daytona")`; wire `--sandbox` on `loopy-run`.
- [ ] deps: `daytona` as an optional extra `[daytona]` + dev group (importable in CI).
- [ ] tests (fake client, no real service): `acquire` builds correct params (image, `env_vars` =
      secrets); `exec` maps `result`/`exit_code` and `shlex`-joins argv; `release` calls `delete`;
      missing SDK + no injected client → clear error.

## Files likely to change
- `loopy_runtime/sandbox/daytona.py`, `loopy_runtime/sandbox/factory.py`, `loopy_runtime/cli.py`
- `pyproject.toml` (optional `daytona` extra + dev), `tests/test_daytona_sandbox.py`

## Acceptance gate
With a fake Daytona client, `acquire → exec → release` round-trips and maps fields correctly;
the provider is selectable via factory/flag; with the SDK absent and no client injected, acquiring
fails with a clear "install loopy-core[daytona]" error. Existing 108 tests stay green.

## Dependencies
Backend v1 (merged).

## Open questions
- Image-build mapping fidelity (base string now; full `Image` builder later).
- Network allowlist enforcement mechanism in Daytona — recorded but not enforced in v1.

## Notes / decisions
- Daytona SDK optional + client-injectable so CI never hits the real service — decided.
