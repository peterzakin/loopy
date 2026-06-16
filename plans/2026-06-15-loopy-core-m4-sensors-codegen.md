# loopy-core M4 — Sensor AST loader + `loopy.events` codegen

**Status:** draft
**Owner:** peter
**Date:** 2026-06-15

## Goal
Generate the `loopy.events` module from the registry, and statically validate `sensors/*.py` by
AST — no import, no user code executed.

## Context
Pass P7 of FRONTEND §1; sensor rules §7; codegen §9. Decision §13 #4 (resolved): sensors are
validated by **static inspection, never import**, and the emitted event is **declared** via
`emits` (a literal Core reads) — *not* inferred from the return type. The declaration is exposed
per language idiom: a `@sensor(emits=...)` decorator (Python) or a statically-analyzable
`sensorRegistry` literal (TypeScript/others). The return annotation is optional sugar the author's
typechecker enforces. `loopy.events` is a pure **output** artifact, never a compile input.

## Constraints & non-goals
- Never import or execute sensor modules — read declarations statically (Python → AST) only.
- `emits` is the source of truth and must be a **literal Core can read without executing code**;
  an imperatively built registry → `E402` (not silent omission). No return-type inference.
- The per-language inspector is **pluggable**, reducing each sensor to a common descriptor
  `{name, trigger(webhook|poll), emits, module, fn}`. M4 ships the **Python AST inspector** only;
  the registry-literal/TS inspector is a later, additive target.
- The only fact needed from the rest of compile is the set of registered event names (M1).
- **Non-goals:** running sensors, validating sensor bodies/payloads, checking third-party imports,
  building non-Python inspectors or non-Python codegen targets (later).

## Approach
`events/codegen.py` emits a real `loopy.events` module + `.pyi` stubs from the registry (via
`datamodel-code-generator` or hand-emitted pydantic); written behind a target interface so a
TypeScript `.d.ts` target can be added later without reshaping it. `sensors/loader.py` parses each
Python sensor file to an AST, finds `@sensor` functions, reads the **declared** `emits` literal +
trigger config, and runs S1–S3, recording a descriptor per sensor — behind an inspector interface
so other-language inspectors slot in producing the same descriptor.

## Steps
- [ ] `events/codegen.py`: emit `loopy.events` module + `.pyi` from registry events (behind a
      codegen-target interface; Python target only in M4). **Output (#5): write to `<project>/loopy/`
      by default** (so `from loopy.events import X` resolves and mypy/IDEs find it); generated +
      gitignored; a CLI flag relocates it.
- [ ] P7 `sensors/loader.py` (Python AST inspector, behind an inspector interface): parse each
      `sensors/*.py`; find `@sensor` functions; read `emits` + trigger config (`webhook` / `poll`).
- [ ] S1: `emits` is declared and statically readable → else `E402` (missing / not statically
      analyzable). No return-type inference.
- [ ] S2: declared `emits` event registered in `registry.yml` → else `E401`.
- [ ] S3: webhook `path` unique across sensors; `poll` interval well-formed → else `E403`.
- [ ] Derive `module` (P4): **dotted path from project root**, `.py` dropped, subdirs → dots
      (`sensors/github/issues.py` → `sensors.github.issues`). Python-only for now; `module` is a
      *language-appropriate locator* and a `lang` discriminator arrives with the second language.
- [ ] Populate one `Sensor` per sensor (model defined in M0: `sensors/model.py` —
      `{name, trigger(webhook|poll), emits, module, fn}` + span).

## Files likely to change
- `loopy_core/events/codegen.py`, `loopy_core/sensors/loader.py`

## Codes owned
`E401`, `E402`, `E403`.

## Acceptance gate
S-rules fire on negative fixtures (incl. missing `emits` and a non-statically-analyzable registry
→ `E402`); a declared `emits` validates against the registry; no user code executes at compile
time; `loopy.events` is importable by authors/typecheckers.

## Dependencies
M0, M1 (registered event names). Parallelizable with M2/M3.

## Open questions
- None blocking.

## Notes / decisions
- (fill in as you go)
