# loopy-core M1 — Discovery + registry loader + type desugar

**Status:** draft
**Owner:** peter
**Date:** 2026-06-15

## Goal
Turn a project directory into a file inventory and a typed, defaults-applied `Registry`, and
implement the field-type desugarer (terse forms → JSON Schema).

## Context
Passes P0–P2 of FRONTEND §1; registry model in §2; type system in §3. The desugar function lives
in `registry/types.py` and is reused later for step `output:` maps (M2).

## Constraints & non-goals
- We own no type semantics — terse forms desugar to JSON Schema (draft 2020-12); raw schema
  objects pass through untouched (§3).
- Use `ruamel.yaml` so registry parse retains line numbers for spans.
- **Non-goals:** workflows/steps, step-output desugaring (M2), sensors (M4), manifest (M5).

## Approach
`discovery.py` walks the project and inventories `registry.yml`, `workflows/*/*.md`, `skills/*`,
`sensors/*.py`. `registry/loader.py` parses the registry, applies `defaults.agent` inheritance,
and produces the typed `Registry`. `registry/types.py` implements the §3 desugar table + raw
schema pass-through. Registry naming checks (X1) run here.

## Steps
- [ ] P0 `discovery.py`: inventory all four input kinds; missing/unreadable `registry.yml` → `E001`.
- [ ] P1 `registry/loader.py`: parse with `ruamel.yaml`; apply `defaults.agent`; build typed
      `Registry` with spans. **Merge semantics (P3): deep-merge `harness` field-by-field** (an agent
      setting `harness: {model: X}` keeps the default `runtime`); **replace list fields**
      (`tools`/`skills`) wholesale with the agent's own list (no union — an agent's list is its full
      set).
- [ ] P2 `registry/types.py`: desugar table (`str`/`int`/`float`/`bool`/`id`/`url`/`enum[...]`);
      pass through inline JSON Schema; unknown shorthand → `E201`.
- [ ] X1 naming: entities Capitalized; `default` the one reserved lowercase sandbox; no dup
      names → `E210` / `E211`.
- [ ] Seed `examples/incidents/` with at least a real `registry.yml` (the README registry) to
      dogfood discovery + loader against (#2; grows through M2–M4, full by M5).

## Files likely to change
- `loopy_core/discovery.py`
- `loopy_core/registry/loader.py`, `loopy_core/registry/types.py`, `loopy_core/registry/model.py`
- `examples/incidents/registry.yml` — seed of the canonical example

## Codes owned
`E001`, `E201` (event `fields:`), `E210`, `E211`.

## Acceptance gate
Registry round-trips to typed IR with defaults applied; bad type shorthand reports `E201` at the
correct `file:line`; naming violations report `E210`/`E211`.

## Dependencies
M0.

## Open questions
- None blocking.

## Notes / decisions
- (fill in as you go)
