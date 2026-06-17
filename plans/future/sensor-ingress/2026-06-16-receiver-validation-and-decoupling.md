# Event ingress: make the receiver a real gate, decoupled from execution

**Status:** in progress
**Owner:** peter
**Date:** 2026-06-16

## Goal
Set the ingress path up so loopy-hosted Python sensors work today, **and** so expanding to
developer-hosted (remote, polyglot) sensors later is a clean addition rather than a rewrite. Two
behaviors are the whole point: the `EventReceiver` must (1) re-validate every event against the
registry, and (2) publish-and-acknowledge instead of running the workflow synchronously.

## Context
- Today: `FastAPISensorRunner` hosts the vendor webhook, runs the `@sensor` fn (`payload -> Event`),
  and calls `LocalEventReceiver.receive(event)`, which calls `Runtime.trigger(event)` — publish +
  synchronous drain, returning a `RunId`.
- Direction (decided): sensors stay **loopy-hosted in Python now**; **developer-hosted sensors**
  (their own app/language, the polyglot path) come later. See `DEPLOYMENT.md §3`.
- The structure is already in the right place: `receive()` takes an `Event` (not a raw payload), the
  `@sensor` fn is a standalone `payload -> Event` callable, `Event` is JSON-serializable, and
  `loopy compile` already writes the contract as static files (`loopy/events.py`).
- The corner risk is **behavioral, not structural**. Two behaviors the remote model can't tolerate:
  - The receiver currently does **no validation** — it trusts the sensor. Shipping that bakes in
    sensor-trust; un-trusting it later means adding the gate *and* auditing everything downstream.
  - `receive() -> RunId` runs the workflow synchronously. A remote HTTP receiver cannot hold a
    connection open for a minutes-to-days run. Synchronous-run-from-receive must not be depended on.

## Constraints & non-goals
**Constraints**
- Single-node `loopy run` stays one process (receiver + engine same node) and keeps working E2E.
- `Event` stays a plain serializable record. Nothing on the ingress path may assume the sensor and
  receiver share an in-memory registry.
- The receiver validates against the **manifest** registry (`manifest.registry.events`) — the single
  source of truth — never against the sensor's generated types.

**Non-goals (explicitly deferred to the developer-hosted milestone — do NOT build now)**
- HTTP `POST /events` intake endpoint.
- Authentication of remote producers.
- External broker (Redis/NATS/Kafka).
- Contract versioning / distribution to remote sensors.

These are additive; building them speculatively without a real second consumer will get the details
wrong. The only thing we owe them now is the interface shape and the two behaviors above.

## Approach
Land the two "don't corner ourselves" behaviors behind the existing interfaces, in-process. Keep
`EventReceiver` as a one-method seam so the future standalone gateway is a config change.

- **Validation (Stage 1):** the receiver checks an event's name is registered and its fields satisfy
  the contract (presence + type/enum per the DSL) before the event proceeds. Reject with a clear
  `unknown event` / `contract mismatch: expected X, got Y` error. Structural only — not semantic.
- **Decouple (Stage 2):** `receive()` publishes to the `EventBus` and returns an ack; the `Runtime`
  consumes off the bus in a background loop (it already subscribes per-workflow — today the drain
  only runs synchronously inside `trigger()`). `receive()` no longer calls `Runtime.trigger`.

Alternative considered — merge `SensorRunner` + `EventReceiver` into one ingress component: rejected
because it forecloses developer-hosted sensors (the merged unit is either loopy-hosted → per-language
servers, or dev-hosted → the validation gate runs in untrusted code). The split is what buys both
"sensors run anywhere" and "a trustworthy gate."

## Steps

### Stage 1 — make the receiver a real validation gate (now)
- [x] Add a registry-validation helper (`loopy_runtime/validation.py`, `validate_event`): name
      registered, required fields present, type/enum match. Structural only; extra fields allowed.
- [x] Call it inside `LocalEventReceiver.receive` before publishing; raise `EventValidationError`.
- [x] Tests: valid passes; unknown event; missing field; wrong type; bool-is-not-int; bad enum;
      extra fields allowed; receiver rejects + does not publish (`tests/test_b3_ingress.py`).

### Stage 2 — decouple intake from execution (now)
- [x] `Runtime.serve()` consume loop (drains on each enqueue, woken by `self._work`); guarded
      `_drain` (re-entrant-safe); `trigger()` kept synchronous for `loopy trigger`/tests; `drain()`
      exposed. `loopy run` starts `serve()` alongside uvicorn.
- [x] `LocalEventReceiver(bus, events)` now validates + `bus.publish` + returns `None` (ack), not
      `Runtime.trigger`.
- [x] `EventReceiver` Protocol docstring updated to publish-and-ack (`Optional[RunId]` permits `None`).
- [x] Tests: `receive()` doesn't run synchronously until drained; `serve()` drains in background;
      webhook + conformance E2E still green.

### Stage 1.5 — run-failure handling (#1) + webhook status (#3) (now)
- [x] `_execute` wraps the step loop: a failed run records `run_failed` + a `failed` `RunStatus`
      (new `RunStatus.error` field), logs a WARNING, and returns — isolating the failure so the drain
      continues (siblings not stranded; `status()` no longer `KeyError`s). `CancelledError` still
      propagates. `serve()` keeps a backstop for drain-level faults (logs + `drain_errors`).
- [x] `loopy trigger` reports `failed_runs` and exits non-zero (loud one-shot behavior preserved).
- [x] Webhook handler translates `EventValidationError` → HTTP 422 (was an opaque 500).
- [x] Tests: failed run recorded-not-raised; failure doesn't strand siblings; `serve()` survives a
      failing run; webhook returns 422. Updated `test_runtime_missing_model_key_*` to the new
      recorded-failure contract.

### Stage 3 — developer-hosted expansion (DEFERRED — separate milestone, do not start here)
- [ ] HTTP `POST /events` intake on the receiver.
- [ ] Producer authentication.
- [ ] External broker behind the `EventBus` Protocol.
- [ ] Event/contract version stamping + distributing generated types to remote sensors.

## Files likely to change
- `loopy_runtime/receiver.py` — add validation; switch from `Runtime.trigger` to `EventBus.publish`.
- `loopy_runtime/runtime/inmemory.py` — continuous consume loop; keep `trigger()` one-shot semantics.
- `loopy_runtime/contract.py` — `EventReceiver` docstring/return clarification.
- `loopy_cli/__init__.py` — wire the registry/manifest into the receiver; confirm `run`/`trigger`.
- new validation helper (location TBD — `loopy_runtime/` near payloads/contract).
- tests under `tests/` for both stages.

## Open questions
- Ack type for `receive()`: return `None`, or a lightweight `EventId`/receipt? (Leaning `None` now;
  a receipt type when remote intake lands.)
- Does `loopy trigger` (one-shot CLI) keep a synchronous "run to completion + print steps" path on
  top of the consume loop, or move to poll-for-status? (Leaning: keep synchronous wrapper for the CLI
  only; the receiver path is publish-and-ack.)

## Known issues / follow-ups (tracked)
Found during the Stage 1/2 review. **Fixed now:** #1 (run-failure handling) and #3 (webhook 422).
**Still open** (none block single-node; revisit with the developer-hosted milestone):

- **[open] #2 — `loopy trigger` bypasses the validation gate.** The operator one-shot calls
  `Runtime.trigger` directly, so `--fields` events aren't validated against the registry; only the
  `EventReceiver` path is. Fix: validate in `trigger`, or route it through the receiver.
- **[open] #4 — validation is structural only.** No `format` enforcement (`loopy-id`, `uri` are just
  "is a string"); extra fields are allowed and flow into the recorded event. Intended; documented so
  no one expects format/strict checks.
- **[open] #5 — no backpressure.** The in-proc bus + deque is unbounded; a burst grows the queue
  while slow agent runs drain it. Belongs to the broker work (Stage 3), not single-node.
- **[open] #6 — cosmetic.** `receive()` still typed `RunId | None` but always returns `None` (a
  receipt type would be cleaner with remote intake); `serve()`/`trigger()` must not both drive one
  runtime (safe today, guard prevents races, undocumented invariant); `serve()` cancel at shutdown
  may log a benign pending-task warning.
- **[open] #7 (new) — no terminal/config vs transient failure distinction.** A misconfig (e.g.
  missing model key) now becomes a *recorded failed run* like any other, surfaced via WARNING logs +
  `failed_runs` + `loopy trigger` exit 1. A server would keep producing identical failed runs. A
  future refinement could fail-fast/classify config errors. `drain_errors` is also unbounded +
  unsurfaced beyond logs.

## Notes / decisions
- 2026-06-16: Decided loopy-hosted Python sensors now, developer-hosted later. The near-term
  "don't corner ourselves" work is exactly Stages 1+2; Stage 3 is deferred until we commit to
  developer-hosted. Merging SensorRunner+EventReceiver rejected (see Approach).
- 2026-06-16: Stages 1, 2, and 1.5 implemented (139 tests pass, ruff clean). Decision: a run failure
  is a **recorded outcome, not a raised exception** — `_execute` records it and the drain continues,
  so one bad run can't crash the server or strand siblings. This changed `test_runtime_missing_model_key`
  from "raises" to "records a failed run". On merge, graduate the durable decisions (receiver = trusted
  validation gate; publish-and-ack; recorded run failures) into `ARCHITECTURE.md` per `plans/README.md`.
