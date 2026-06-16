# loopy-core M2 — Frontmatter + step model + DAG

**Status:** draft
**Owner:** peter
**Date:** 2026-06-15

## Goal
Parse each workflow `.md` into a `Step`, desugar step `output:` schemas, and build + validate one
DAG per workflow.

## Context
Passes P3–P4 of FRONTEND §1; frontmatter parsing §4; DAG rules §5. `on:` is now a **single** event
or `cron(...)` — unions are unsupported (decision §13 #3).

## Constraints & non-goals
- `on:` accepts one event or `cron(...)`; a list (`on: [A, B]`) → `E111`.
- `after:` / `emits:` accept scalar or list, normalized to lists.
- Step `output:` maps are desugared here via the M1 `registry/types.py` function.
- Loop-backs go through events, never `after:` (the `after:` graph must be acyclic).
- **Non-goals:** template refs/resolution (M3), cross-cutting resolution of agents/events (M5).

## Approach
`workflow/frontmatter.py` splits `---/body` (via `python-frontmatter`) and parses YAML with
`ruamel.yaml` for line numbers; track the body start line so template `Ref` spans (M3) are
accurate. `workflow/loader.py` builds `Step`s (identity `<dir>/<stem>`), normalizes
`after:`/`emits:`, validates `on:` shape, and desugars `output:`. `workflow/dag.py` builds a
`networkx.DiGraph` and runs W1–W7 + cron parsing.

## Steps
- [ ] P3 `workflow/frontmatter.py` + `loader.py`: split frontmatter/body; retain line numbers;
      track body start line; step identity `<workflow_dir>/<filename_stem>`.
- [ ] Normalize `after:`/`emits:` to lists; validate `on:` is a single event or `cron(...)` —
      list → `E111`.
- [ ] Desugar each step `output:` via `registry/types.py` — unknown shorthand → `E201` at the
      step's `file:line`.
- [ ] P4 `workflow/dag.py`: build `networkx.DiGraph`; W1–W7 → `E101`–`E107`.
- [ ] Cron parse via `croniter` (5 fields) + optional IANA `tz=`; malformed → `E110`.

## Files likely to change
- `loopy_core/workflow/frontmatter.py`, `loader.py`, `dag.py`, `model.py`

## Codes owned
`E101`–`E107`, `E110`, `E111`, `E201` (step `output:`).

## Acceptance gate
All W-rules fire on negative fixtures; valid workflows build a DAG with the `on:` step as root;
cron parses; both single-step and multi-step workflows handled.

## Dependencies
M0, M1 (IR + `registry/types.py`).

## Open questions
- None blocking. (§13 #2 settled: `after:` refs are direct-only — reach back by adding the step
  to `after:`.)

## Notes / decisions
- (fill in as you go)
