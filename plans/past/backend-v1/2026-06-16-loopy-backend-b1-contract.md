# loopy-backend B1 — the runtime contract (ARCHITECTURE Phase 6)

**Status:** draft
**Owner:** peter
**Date:** 2026-06-16

## Goal
Freeze the backend interfaces and value types — the structural seams every runtime piece plugs into
— and a `ManifestLoader` that reads the frontend's `manifest.json` into typed runtime models. No
behavior yet; this is the contract the in-memory backend (B2) and all future backends implement.

## Context
The frontend emits the manifest; the backend consumes `(manifest, triggering event)` and runs it
(ARCHITECTURE §1–§3). v1 is non-durable and single-process (see B2), but the **interfaces** are
defined now so durability/networked/Daytona/Codex variants drop in later without reshaping. The
seams are §3.4's Protocols; freezing them against the real manifest (not in the abstract) is the
point of this milestone.

## Constraints & non-goals
- Structural `typing.Protocol`s — any conforming class is valid without inheritance.
- **Effect-free orchestration is a contract requirement:** the `Runtime` walks the DAG and records
  results; all nondeterminism (agent output, time, randomness, event payloads) lives behind
  `AgentHarness`/`EventBus`/`SandboxProvider`. This holds even though v1's `StateStore` is a
  throwaway dict — it's what lets DurableLite replay later (§6, §9).
- **Non-goals:** any concrete implementation (B2), durability primitives, networked transports.

## Approach
New `loopy_runtime/` package. `contract.py` holds value types + Protocols. `manifest_model.py` holds
the typed manifest (pydantic) and `load_manifest(path) -> Manifest`. A `providers.py` holds the
harness-runtime → required-model-key registry. Validate by loading the incidents manifest into the
typed models in a test.

## Steps
- [ ] `loopy_runtime/` package skeleton (import-clean stubs).
- [ ] `contract.py` value types: `Event`, `StepOutput`, `StepContext`, `RunEvent`, `RunStatus`; id
      aliases `RunId`/`StepId`/`EventName`/`TriggerId`.
- [ ] `contract.py` Protocols: `Runtime`, `StateStore`, `AgentHarness`, `SandboxProvider`,
      `Sandbox`, `EventBus`, `SensorHost`, `RetryPolicy`, `SecretsResolver`.
- [ ] `manifest_model.py`: typed `Manifest` (+ `StepSpec`, `SandboxSpec` incl. `env_file`,
      `AgentSpec` incl. `harness.runtime`, `EventContract`, `Budget`, `SensorSpec`, lineage) and
      `load_manifest(path)`; tolerate the CLI-stamped `compiled_at`/`loopy_version`.
- [ ] `providers.py`: `REQUIRED_MODEL_KEY = {"claude-code": "ANTHROPIC_API_KEY"}` with
      `OPENAI_API_KEY` reserved for a future `codex`/`openai` runtime; helper
      `required_model_key(runtime) -> str`.
- [ ] Tests: load `examples/incidents` manifest into typed models; every Protocol importable; the
      provider registry resolves `claude-code` → `ANTHROPIC_API_KEY`.

## Files likely to change
- `loopy_runtime/__init__.py`, `loopy_runtime/contract.py`
- `loopy_runtime/manifest_model.py`, `loopy_runtime/providers.py`
- `tests/test_b1_contract.py`

## Acceptance gate
The incidents `manifest.json` loads into the typed `Manifest` (incl. sandbox `env_file`); all §3.4
Protocols import and are `runtime_checkable`; the provider-key registry resolves the v1 runtime.

## Dependencies
Frontend M0–M5; `env_file` addendum (for `SandboxSpec.env_file`).

## Open questions
- None blocking.

## Notes / decisions
- v1 defers durability but **not** the determinism discipline or the seams (decided).
- One harness in v1 (`claude-code`); `AgentHarness` Protocol + provider registry keep Codex open.
- **`EventBus` is built for a networked drop-in:** `publish` is `async` and `Event` is a plain
  serializable value type, so a `RedisEventBus`/`NatsEventBus` is a composition-time substitution
  behind the same Protocol (`InProcessEventBus` is just the first impl). The code swap is trivial;
  the *reliability* semantics it enables (at-least-once redelivery, idempotency) belong to the later
  durability work (B9/B10), not the swap itself.
- The conformance suite runs on a `StubAgentHarness` that lives in `tests/` (test infrastructure,
  not a shipped `loopy_runtime` component); the only shipped harness is `ClaudeCodeHarness`.
