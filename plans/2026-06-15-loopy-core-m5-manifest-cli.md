# loopy-core M5 — Cross-cutting validation + manifest + schema + CLI + golden tests

**Status:** draft
**Owner:** peter
**Date:** 2026-06-15

## Goal
Run the whole-IR cross-cutting checks, emit the deterministic `manifest.json` (+ its schema),
finish the CLI, and stand up the golden test suite.

## Context
Passes P8–P9 of FRONTEND §1; cross-cutting checks §8; manifest §9; testing §12. This is the
keystone — the manifest is the contract every backend depends on (ARCHITECTURE §2).

## Constraints & non-goals
- Manifest is deterministic: sorted keys, stable ordering, content-hashable (decision §13 #5).
- `compiled_at` / `loopy_version` are stamped by the CLI wrapper, **outside** the hashed core.
- `body` stays a template; pre-bound `refs` are recorded (the backend renders at run time).
- **Non-goals:** any backend/runtime behavior.

## Approach
Add the P8 whole-IR pass for the cross-cutting checks whose inputs now all exist (X2, X3, X5);
(X1 and X4 already fire in M1/M2). `compile/manifest.py` serializes the IR to the §9 shape with
sorted keys. Ship `manifest.schema.json` to validate output in CI. Finish `cli.py`. Build the test
suite.

## Steps
- [ ] P8 X2: every step `agent:` resolves to a registered Agent (`E501`); its `sandbox` resolves
      (`E502`); each `agent.skills[]` resolves in `skills/` (`E503`, no external skills).
- [ ] P8 X3: every event-kind `on:` / `emits:` is registered → else `E504` (cron exempt).
- [ ] P8 X5 lineage: build the event graph into the typed `Lineage` IR (M0 model); registered `on:`
      with no producer → `W501`; terminal `emits:` with no consumer is allowed. **Sort all derived
      collections** (each event's `producers`/`consumers`, and the events map) so the manifest stays
      byte-stable (P6).
- [ ] P9 `compile/manifest.py`: emit deterministic JSON (sorted keys) in the §9 shape; record
      pre-bound refs.
- [ ] `manifest.schema.json`: JSON Schema for the manifest; validate emitted output.
- [ ] `cli.py`: finalize `loopy compile [--out manifest.json]`; stamp `compiled_at`/version outside
      the hashed core.
- [ ] Grow `examples/incidents/` to the **full** README example (incidents + autoresearch) — the
      golden-positive source (#2; seeded after M0, completed here).
- [ ] Tests (§12): golden-positive (`examples/incidents/` → byte-stable manifest snapshot);
      golden-negative (per-milestone fixtures, one per §14 code, gathered here); properties
      (idempotent, order-independent, schema-valid).

## Files likely to change
- `loopy_core/compile/pipeline.py` (P8), `loopy_core/compile/manifest.py` (P9)
- `loopy_core/manifest.schema.json`, `loopy_core/cli.py`
- `tests/**` — golden + property fixtures

## Codes owned
`E501`, `E502`, `E503`, `E504`, `W501`.

## Acceptance gate
The README example (incidents + autoresearch) compiles to a byte-stable `manifest.json` snapshot;
every code in FRONTEND §14 has a passing negative fixture; the manifest validates against
`manifest.schema.json`; compile is idempotent and order-independent.

## Dependencies
M0–M4.

## Open questions
- None blocking.

## Notes / decisions
- (fill in as you go)
