# loopy-core addendum — `env_file` on Sandbox (secrets reference)

**Status:** draft
**Owner:** peter
**Date:** 2026-06-16

## Goal
Let a sandbox declare where its secrets come from by **referencing** an env file, carried through
to the manifest as a path — never the values. This is the compile-side half of the backend secrets
model (secrets live at the sandbox abstraction; ARCHITECTURE §6).

## Context
Backend v1 injects secrets into the sandbox at run time (per `loopy_runtime`). Decision: secrets are
**defined at the sandbox**, not the agent — the sandbox is the complete trust boundary (image +
egress allowlist + credentials, which co-vary). An agent needing different secrets uses a different
sandbox. The frontend's only job is to record the *reference*; it must stay pure and
environment-independent.

## Constraints & non-goals
- The YAML references an env file by **path**; secret *values* are never inlined and never enter the
  manifest (§6: "secrets ... never in the manifest").
- **The compiler does not read or validate the env file.** Env files are gitignored and
  environment-specific (dev/staging/prod) and often absent at CI/compile time — reading one would
  break the frontend's offline, deterministic, environment-independent contract. Missing/empty files
  are a **runtime** error in the backend, not a compile error.
- `env_file` accepts a scalar or list, normalized to a list (consistent with `after:`/`emits:`).
- **Non-goals:** reading env files, validating key presence (runtime), per-agent env (rejected —
  sandbox-level only), any new diagnostic code.

## Approach
Add `env_file: list[str]` to the `Sandbox` IR model; parse it in the registry loader (record the
path(s), no file I/O); serialize it in the manifest. Update the example + golden snapshot.

## Steps
- [ ] `registry/model.py`: add `env_file: list[str] = []` to `Sandbox`.
- [ ] `registry/loader.py`: read `env_file` from each sandbox (scalar/list → list); do **not** open
      the file.
- [ ] `compile/manifest.py`: include `env_file` in the serialized sandbox.
- [ ] `manifest.schema.json`: add `env_file` (array of strings) to the sandbox shape.
- [ ] `examples/incidents/registry.yml`: give `default` an `env_file: secrets/default.env`
      (gitignored; absent on disk — proves compile doesn't read it).
- [ ] Regenerate `tests/golden/incidents.manifest.json`.
- [ ] Tests: `env_file` scalar+list normalize; recorded on the IR; present in the manifest; a
      sandbox with no `env_file` serializes `[]`; compile stays clean with a *nonexistent* env_file
      path (proves no file I/O).

## Files likely to change
- `loopy_core/registry/model.py`, `loopy_core/registry/loader.py`
- `loopy_core/compile/manifest.py`, `loopy_core/manifest.schema.json`
- `examples/incidents/registry.yml`, `tests/golden/incidents.manifest.json`
- `tests/test_m1_registry.py` (or a new `tests/test_env_file.py`)

## Acceptance gate
A sandbox `env_file` round-trips into the manifest as a path list; compile is clean even when the
referenced file does not exist; the example still compiles to a byte-stable manifest.

## Dependencies
Frontend M0–M5 (shipped). Blocks backend B1/B2.

## Open questions
- None blocking. (Provider-key *requirement* is enforced at runtime by the harness, not here.)

## Notes / decisions
- Secrets defined at the **sandbox**, never overridden at the agent (decided).
- Reference-not-values; compiler records the path and never opens the file (decided).
