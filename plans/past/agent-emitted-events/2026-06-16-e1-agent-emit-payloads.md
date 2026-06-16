# agent-emitted-events E1 — agent-produced emit payloads (decision #3 = B)

**Status:** draft
**Owner:** peter
**Date:** 2026-06-16

## Goal
When a step `emits:` an event, publish the **agent-produced, contract-validated** payload
instead of synthesizing it from the contract. The agent is responsible for generating the
structured event data.

## Context
Backend v1 synthesized emit payloads from the event contract (a documented simplification), so
cross-workflow data was stubbed. Decision #3 = **B**: the agent produces the emitted event's
fields. This keeps the determinism boundary intact — emitted payloads are agent output, captured
as a recorded result, then published. No frontend reopen: emitted events' contracts already live
in the manifest registry; "the agent fills them" is purely a runtime/harness concern.

## Constraints & non-goals
- For each event in a step's `emits:`, the agent produces a payload validated against that event's
  registry contract (top-level fields present); a missing/invalid payload is a clear runtime error.
- The contract-synthesis path stays only as the **test stub's** way to produce deterministic
  payloads (offline conformance).
- **Non-goals:** conditional emission ("emit only if X"), emitting the same event multiple times,
  sensor execution (E2), any frontend change.

## Approach
Evolve the `AgentHarness` contract so `run` returns a richer `StepResult` carrying both the step
`output:` and a payload per emitted event. The runtime publishes the agent's payloads; the stub
synthesizes deterministic ones; `ClaudeCodeHarness` prompts for and parses them.

## Steps
- [ ] `contract.py`: add `StepResult { output: StepOutput, emits: Mapping[EventName, Mapping] }`;
      change `AgentHarness.run -> StepResult`.
- [ ] `runtime/inmemory.py`: `_build_emitted` uses the agent-produced payload for each declared
      emit (validate top-level keys against the event contract; error on missing); record emitted
      payloads in history. Drop the contract-synthesis production path.
- [ ] `harness/claude_code.py`: prompt the agent to return JSON with `output` + a payload per
      `emits:` event; parse and validate each against its contract; return `StepResult`.
- [ ] `tests/stub_harness.py`: produce `output` + synthesize each emit payload from the event
      contract (needs the registry events) — deterministic, offline.
- [ ] Tests: a downstream step's `{{ event.field }}` resolves to the agent-produced value (not a
      stub) end-to-end; conformance still asserts cascade + order; missing emit payload → error.

## Files likely to change
- `loopy_runtime/contract.py`, `loopy_runtime/runtime/inmemory.py`
- `loopy_runtime/harness/claude_code.py`, `tests/stub_harness.py`
- `tests/conformance/**`, `tests/test_b2_*.py`

## Acceptance gate
With a harness that returns real payloads, a downstream step's `{{ event.field }}` resolves to the
agent-produced value; the conformance suite (stub harness) still passes with cascade + order intact;
a declared emit with no agent payload fails with a clear error.

## Dependencies
Backend v1 (merged).

## Open questions
- Whether to validate emit payloads strictly (all contract keys) or leniently in v1 — lean strict
  (clear errors), revisit if noisy.

## Notes / decisions
- #3 = B (agent produces emit payloads) — decided.
