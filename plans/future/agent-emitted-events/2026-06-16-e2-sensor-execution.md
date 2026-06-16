# agent-emitted-events E2 — real sensor execution (authoring shim + loader)

**Status:** draft
**Owner:** peter
**Date:** 2026-06-16

## Goal
Run real user `@sensor` functions in `loopy dev`: ship the `loopy` authoring shim so user sensor
modules import, load each sensor from the manifest, invoke it with the incoming request, and inject
its returned event via the runtime. Replaces v1's synthesized-event webhook stub.

## Context
v1's `loopy dev` publishes a contract-synthesized event per webhook; the user's `@sensor` function
never runs. The same principle as decision #3 (B) applies at ingress: the **sensor produces the
event** (it `return`s `Incident(...)`). Running it requires the `loopy` shim, since user sensors do
`from loopy import sensor` and `from loopy.events import …` (the latter already emitted by codegen).

## Constraints & non-goals
- Importing/executing user sensor modules is a **runtime** concern (the backend), not compile —
  the frontend still never imports them.
- A sensor returns an event (or None / an iterator of events); normalize the return into the
  runtime `Event` and inject via `runtime.trigger` (publish + drain).
- **Non-goals:** durable poll scheduling (B7/B8), webhook auth/security hardening, any compile
  change.

## Approach
Ship a small `loopy` package — the authoring shim: a `sensor(...)` decorator that records
`{webhook|poll, emits}` and marks the function (no behavior), plus making generated `loopy.events`
importable. A backend loader imports `module:fn` from each manifest sensor spec and hands the
callable to `SensorHost`, which invokes it and injects the returned event.

## Steps
- [ ] `loopy/` authoring package: `sensor(...)` decorator (records config, returns the fn);
      ensure generated `loopy.events` is importable alongside it.
- [ ] `loopy_runtime/sensors/loader.py`: given a manifest `SensorSpec` + project root, import
      `module` and resolve `fn` (clear error if the module/shim is missing).
- [ ] `sensors/host.py`: register the real sensor callable; normalize its return (a `loopy.events`
      instance / dataclass with name + fields, or None) into the runtime `Event`.
- [ ] `cli.py` (`dev`): generate `loopy.events`, load + register real sensors from the manifest,
      then serve; fall back to a clear error if a sensor can't be loaded.
- [ ] Tests: a sample sensor module (using the shim) is loaded and invoked, returns an event, and
      drives the full cascade; missing module/shim → clear error.

## Files likely to change
- `loopy/**` (new authoring package), `loopy_runtime/sensors/loader.py`, `loopy_runtime/sensors/host.py`
- `loopy_runtime/cli.py`, `tests/**`

## Acceptance gate
A webhook invocation runs the real `@sensor` function, whose returned event triggers the cascade;
`loopy dev` wires real sensors from the manifest; a missing sensor module fails clearly.

## Dependencies
E1 (the event-construction model), backend v1.

## Open questions
- Where the `sensor` decorator lives (`loopy` shim vs `loopy_runtime`) — lean `loopy` (authoring
  surface, imported by user code).
- Exact normalized shape of a sensor's return (typed `loopy.events` instance vs plain payload).

## Notes / decisions
- Same producer-generates-the-event principle as #3 = B, applied at ingress — decided.
