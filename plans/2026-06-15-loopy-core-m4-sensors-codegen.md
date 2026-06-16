# loopy-core M4 — Sensor AST loader + `loopy.events` codegen

**Status:** draft
**Owner:** peter
**Date:** 2026-06-15

## Goal
Generate the `loopy.events` module from the registry, and statically validate `sensors/*.py` by
AST — no import, no user code executed.

## Context
Pass P7 of FRONTEND §1; sensor rules §7; codegen §9. Decision §13 #4: sensors are validated by
AST inspection; both an annotated `-> Event` return and a `(Event, payload)` tuple emit are
accepted. `loopy.events` is a pure **output** artifact, never a compile input.

## Constraints & non-goals
- Never import or execute sensor modules — parse to AST only.
- Accept both emit forms: annotation (`-> Incident`, incl. `Iterator[Incident]`) or tuple
  (`(Event, payload)` in each `return`/`yield`). Try annotation first, then scan emit expressions.
- The only fact needed from the rest of compile is the set of registered event names (M1).
- **Non-goals:** running sensors, validating sensor bodies/payloads, checking third-party imports.

## Approach
`events/codegen.py` emits a real `loopy.events` module + `.pyi` stubs from the registry (via
`datamodel-code-generator` or hand-emitted pydantic). `sensors/loader.py` parses each sensor file
to an AST, finds `@sensor` functions, resolves the emitted event type via the two accepted forms,
and runs S1–S3, recording a descriptor per sensor.

## Steps
- [ ] `events/codegen.py`: emit `loopy.events` module + `.pyi` from registry events.
- [ ] P7 `sensors/loader.py`: AST-parse each `sensors/*.py`; find `@sensor` functions; read trigger
      config (`webhook` / `poll`).
- [ ] Resolve emitted event: annotation form, else tuple form. S1 (none determinable) → `E402`.
- [ ] S2: emitted event registered in `registry.yml` → else `E401`.
- [ ] S3: webhook `path` unique across sensors; `poll` interval well-formed → else `E403`.
- [ ] Record `{name, trigger(webhook|poll), emits, module, fn}` per sensor.

## Files likely to change
- `loopy_core/events/codegen.py`, `loopy_core/sensors/loader.py`

## Codes owned
`E401`, `E402`, `E403`.

## Acceptance gate
S-rules fire on negative fixtures; both emit forms validate; no user code executes at compile time;
`loopy.events` is importable by authors/typecheckers.

## Dependencies
M0, M1 (registered event names). Parallelizable with M2/M3.

## Open questions
- None blocking.

## Notes / decisions
- (fill in as you go)
