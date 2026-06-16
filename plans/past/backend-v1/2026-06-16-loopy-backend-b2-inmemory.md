# loopy-backend B2 — in-memory backend, end-to-end (ARCHITECTURE Phase 7)

**Status:** draft
**Owner:** peter
**Date:** 2026-06-16

## Goal
A non-durable, single-process backend that runs the incidents example end-to-end: trigger a run from
an event, walk the `after:` DAG, render templates, invoke the agent in a sandbox, capture typed
output, emit events (incl. cross-workflow loop-backs), and enforce budgets. Satisfies **B1–B6**;
stubs B7/B10/B11. This is the milestone that proves the manifest→runtime mapping works.

## Context
Implements the B1 contract. v1 is explicitly a **dev/demo engine** (ARCHITECTURE §5 Phase 7): a
crash drops in-flight runs, long-horizon (day-scale) waits are out. The point is to validate the
model and dogfood the example before taking on any durability/operational dependency.

## Constraints & non-goals
- **Effect-free DAG-walk:** the only effects are `AgentHarness.run`, `EventBus.publish`, and
  `Sandbox.exec`; the runtime records every result into the `StateStore` (a dict in v1) so the same
  code path is replayable later.
- **Secrets at the sandbox:** resolved at run time from the sandbox's `env_file`, injected via the
  provider, inherited by tool subprocesses; never logged, never recorded, never written to the
  workspace.
- **`ClaudeCodeHarness` only**; behind the `AgentHarness` Protocol so a `CodexHarness` lands later.
- **Non-goals:** durability/durable timers (B7), crash-recoverable `resume` (B10), version pinning
  (B11), idempotency-across-replay, networked `EventBus`, Daytona, Temporal/DBOS, Codex harness,
  long-horizon runs.

## Approach
One module per seam, each behind its B1 Protocol. The `InMemoryRuntime` composes them: an asyncio
DAG-walk that schedules a step once its `after:` predecessors complete, renders the step body, runs
the agent, validates output, records it, and publishes `emits:` to the bus (which can trigger new
runs). The conformance suite drives the incidents manifest with `StubAgentHarness` so it stays
offline and deterministic.

## Steps
- [ ] `state/inmemory.py` — `InMemoryStateStore`: event-sourced log, step outputs, watermarks,
      idempotency keys (dict-backed, lost on restart).
- [ ] `render.py` — `TemplateRenderer`: render the manifest's recorded `{{ event.* }}`/`{{ step.* }}`
      refs against the run's event + predecessor outputs.
- [ ] `budget.py` — `BudgetEnforcer`: enforce `wall_clock` (minutes) and `spend.usd`; ignore
      `window`/`latency` (day-scale → durable).
- [ ] `retry.py` — `ExponentialBackoffRetry` (`RetryPolicy`) for transient agent/tool failures (the
      lightweight half of B9).
- [ ] `secrets.py` — `EnvFileSecretsResolver`: load the sandbox's `env_file`(s) (dotenv), resolve
      relative to project root (no path escape); assert a recognized model-provider key is present
      for the harness runtime, else a clear runtime error.
- [ ] `sandbox/local.py` — `LocalSubprocessSandbox` + `LocalSandboxProvider`:
      `acquire(spec, secrets)` sets the sandbox env; `exec(cmd)` runs with it (secrets inherit to
      child tool processes). (Daytona deferred behind the same Protocol.)
- [ ] `tests/stub_harness.py` — `StubAgentHarness` (**test infrastructure, not shipped**):
      deterministic, schema-valid canned output (first enum value, stub strings/urls); bypasses the
      provider-key check. The default for the conformance suite + engine tests so they stay offline.
- [ ] `harness/claude_code.py` — `ClaudeCodeHarness` (the only shipped harness): build
      `claude -p <rendered body> --output-format json --allowed-tools <agent.tools> --model
      <harness.model> --permission-mode bypassPermissions`, run via `sandbox.exec`; require
      `ANTHROPIC_API_KEY` in the sandbox env; parse the JSON envelope, validate against `output:`
      (prompt-and-parse, retry on mismatch), feed `total_cost_usd` to the `BudgetEnforcer`.
- [ ] `bus/inproc.py` — `InProcessEventBus`: route published events to `on:` subscribers, incl.
      loop-backs.
- [ ] `runtime/inmemory.py` — `InMemoryRuntime`: `trigger(event)`, `tick(trigger, scheduled_at)`,
      `status(run_id)`; asyncio DAG-walk honoring `after:`; `resume()` raises NotImplemented.
- [ ] `sensors/host.py` — `FastAPISensorHost`: webhook routes for `@sensor(webhook=)`; basic
      in-process poll scheduler for `poll=`; publishes returns to the bus.
- [ ] `cli.py` — `loopy run <manifest> --event <event.json>` (one run to completion, prints
      status/outputs/emits); `loopy dev <manifest>` (start sensors + bus, serve runs).
- [ ] `tests/conformance/` — run the incidents manifest E2E with `StubAgentHarness`; assert step
      outputs, emitted events, and order; assert the Incident→WorkItem→GoalShipped cascade across
      workflows. (§3.3 — the suite every future backend must pass.)
- [ ] Unit tests: template rendering; budget trip; secrets injection + missing-key error;
      `ClaudeCodeHarness` builds the expected `claude` argv (via a stub `Sandbox` capturing
      `exec` args — no real model call); retry on transient failure.

## Files likely to change
- `loopy_runtime/{state,render,budget,retry,secrets}.py`
- `loopy_runtime/sandbox/local.py`, `loopy_runtime/harness/{fake,claude_code}.py`
- `loopy_runtime/bus/inproc.py`, `loopy_runtime/runtime/inmemory.py`
- `loopy_runtime/sensors/host.py`, `loopy_runtime/cli.py`
- `tests/conformance/**`, `tests/test_b2_*.py`

## Acceptance gate
`loopy run` on the incidents manifest with `StubAgentHarness` produces the expected outputs, emitted
events, and step order, including the cross-workflow cascade; the conformance suite passes;
`ClaudeCodeHarness` emits the correct `claude` invocation (argv asserted via a stub sandbox) and
errors clearly when the sandbox supplies no model-provider key; a tripped `wall_clock`/`spend`
budget halts the step. No real model/sandbox calls in the test suite.

## Dependencies
B1 (contract); `env_file` addendum (sandbox secrets).

## Open questions
- Poll scheduler fidelity in v1 (in-process timer vs minimal stub) — lean minimal; real durable
  scheduling is the cron/watermark milestone (B7/B8, deferred).

## Notes / decisions
- `ClaudeCodeHarness` = headless `claude` CLI via `sandbox.exec`, JSON output, `bypassPermissions`
  inside the sandbox (the sandbox is the containment boundary) — decided.
- Conformance runs on `StubAgentHarness`; the real harness is exercised only by argv-level unit
  tests (offline) — decided.
