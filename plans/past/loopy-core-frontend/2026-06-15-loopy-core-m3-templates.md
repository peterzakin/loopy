# loopy-core M3 — Template extraction + static ref resolution

**Status:** draft
**Owner:** peter
**Date:** 2026-06-15

## Goal
Extract `{{ producer.field }}` refs from step bodies and statically resolve each one against the
triggering event or a direct `after:` predecessor's output.

## Context
Passes P5–P6 of FRONTEND §1; grammar + resolution rules §6. The restricted grammar (decision
§13 #1) and direct-only `after:` refs (§13 #2) make resolution total. Refs are flat two-segment
`{{ producer.key }}` — no dotted descent (T5).

## Constraints & non-goals
- Grammar is substitution-only: no `{% %}` control flow, filters, or expressions; no multi-segment
  / dotted paths → `E301`.
- `<field>` is a **top-level key** of the producer's output object / event `fields:`.
- T4 (type compatibility) is **deferred** — validate existence only, no type code in v1.
- **Non-goals:** runtime rendering (backend), type compatibility.

## Approach
`template/parser.py` is a dedicated regex/lexer that pulls refs with accurate spans and rejects
anything outside the grammar. `template/resolver.py` binds each `Ref` to its producing node using
the M2 DAG (direct predecessors) and M1 event contracts, recording the binding on the `Step`.

## Steps
- [ ] P5 `template/parser.py`: extract `{{ producer.field }}` refs + spans; reject control flow /
      filters / multi-segment → `E301`.
- [ ] P6 `template/resolver.py` T1: `event.<field>` exists in the triggering event's contract →
      else `E302`.
- [ ] T2: cron triggers expose only `scheduled_at` / `last_run`; other field → `E303`.
- [ ] T3: `<step>` is a **direct** `after:` predecessor → else `E304`; `<field>` is a top-level
      key of that step's `output:` → else `E305`.
- [ ] Record bound refs on each `Step` for the manifest (M5).

## Files likely to change
- `loopy_core/template/parser.py`, `loopy_core/template/resolver.py`

## Codes owned
`E301`, `E302`, `E303`, `E304`, `E305`. (T4 deferred — no code.)

## Acceptance gate
T1–T3 + T5 fire on negative fixtures (T4 deferred); valid refs bind to their producing nodes and
are recorded on the step.

## Dependencies
M0, M1 (event contracts), M2 (steps, DAG, desugared `output:`).

## Open questions
- None blocking. (§13 #2 settled: T3 enforces direct-only `after:` predecessors; reach an earlier
  step by adding it to `after:`.)

## Notes / decisions
- (fill in as you go)
