# loopy-core M0 — Foundation: IR, diagnostics, skeleton

**Status:** draft
**Owner:** peter
**Date:** 2026-06-15

## Goal
Stand up the shared substrate every later milestone builds on: the in-memory IR, the
never-fail-fast diagnostics machinery, source spans, the diagnostic-code registry, and the
package/CLI skeleton. No parsing or validation logic yet.

## Context
First issue in the `loopy-core` build (see `plans/README` and `FRONTEND.md`). The compiler is a
`discover → parse → resolve → validate → emit` pipeline (FRONTEND §1) that accumulates an IR
(§2) and reports every error with `file:line` (§intro "collect all diagnostics, never fail-fast").
Pulled ahead of M1 because the IR + diagnostics + code constants are cross-cutting and shouldn't
be reinvented per milestone.

## Constraints & non-goals
- Pure, dependency-free of any runtime; executes no workflow logic.
- Never fail-fast: diagnostics accumulate; the run reports all of them and exits nonzero iff any
  are errors.
- Every IR node carries a `Span` (`file`, `line`, `col`).
- **Non-goals:** any rule logic, parsing, resolution, codegen, or emission (those are M1–M5).

## Approach
Build the package skeleton from FRONTEND §10, the pydantic v2 IR models from §2, a `Diagnostic`
type + collector in `compile/diagnostics.py`, and a code-constants module enumerating every code
in FRONTEND §14. Wire `compile/pipeline.py` as a P0–P9 orchestration skeleton (no-op stages) and
`cli.py` (`loopy compile [--out manifest.json]`) that runs it, prints diagnostics, and sets the
exit code.

## Steps
- [ ] Package skeleton per §10 (`discovery.py`, `registry/`, `workflow/`, `template/`, `sensors/`,
      `events/`, `compile/`, `cli.py`) with import-clean stubs.
- [ ] IR models (§2): `Sandbox`, `Harness`, `Agent`, `Event`, `Registry`; `Trigger`, `Budget`,
      `Ref`, `Step`, `Workflow`; `Sensor`, `SensorTrigger`. Pydantic v2. (`Trigger` carries a single
      `event`, not a list; `SensorTrigger` is a distinct `webhook`|`poll` union — do not reuse
      `Trigger`. M0 defines all IR nodes; M4 populates `Sensor`.)
- [ ] `Span` model; helper to attach spans to nodes.
- [ ] `compile/diagnostics.py`: `Diagnostic {severity, code, message, span, hint?}` + a collector
      that records and never raises; final "exit nonzero if any errors" helper.
- [ ] Diagnostic-code constants for every code in FRONTEND §14 (E001, E101–E111, E201, E210–E211,
      E301–E305, E401–E403, E501–E504, W501).
- [ ] `compile/pipeline.py` orchestration skeleton (P0–P9 hooks, no logic).
- [ ] `cli.py`: parse args, run pipeline, render diagnostics with `file:line`, set exit code.

## Files likely to change
- `loopy_core/**` — new package skeleton
- `loopy_core/registry/model.py`, `loopy_core/workflow/model.py`, `loopy_core/sensors/model.py` — IR
- `loopy_core/compile/diagnostics.py` — diagnostics + code constants
- `loopy_core/compile/pipeline.py`, `loopy_core/cli.py` — orchestration + entrypoint

## Acceptance gate
Package imports cleanly; the pipeline runs end-to-end as no-ops over a sample dir, producing an
empty diagnostics list; a named constant exists for every §14 code; diagnostics render with
`file:line`; exit code reflects presence of errors.

## Open questions
- None blocking. (Code numbers are fixed in FRONTEND §14.)

## Notes / decisions
- (fill in as you go)
