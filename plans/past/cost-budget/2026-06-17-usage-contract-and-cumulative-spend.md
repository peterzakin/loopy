# Usage reporting contract + cumulative cascade spend cap

**Status:** SHIPPED — 2026-06-20. Final surface is **`--max-spend` (USD) only**; the token cap
that briefly shipped was removed (illegible unit — see 2026-06-20 note). `Usage` keeps tokens as
telemetry. Cap gated on cost-reporting harnesses. **Superseded surface:** the spend cap later moved
out of the launch flag and into the registry (project + per-workflow caps, #54) and is marked
experimental with a documented cross-workflow caveat (#55) — the `--max-spend` flag below was the
first cut.
**Owner:** peter
**Date:** 2026-06-17

## Goal
Give the runtime a real terminator for runaway cascades: a **cumulative spend cap** across a
cascade (loop-backs are the danger), instead of relying on `max_iterations` (a count, not cost).
To do that cleanly, first make **usage reporting a first-class part of the harness contract** — the
harness reports what it consumed per invocation, and the runtime aggregates it.

This is backlog item **C** (see `BACKLOG.scratch.md`). Captured from a design discussion on
2026-06-17; deliberately deferred — recorded here so the reasoning survives.

## Context
- Budgets today are **per-step only**, enforced inside the harness: `wall_clock` (a per-run
  timeout) and `spend.usd` (this step's `total_cost_usd` vs this step's limit). `window`/`latency`
  (day-scale) are unimplemented (need durable timers).
- The runtime **never sees** the spend number — `ClaudeCodeHarness` computes `total_cost_usd`, hands
  it to `BudgetEnforcer.check_spend`, and throws it away. So there is no running total anywhere.
- A loop-back cascade (`ResultRejected → propose`, `GoalReopened → arbitrate`, or any step that
  emits the event it triggers on) can spin unboundedly: each step stays within its per-step budget
  while cumulative cost grows without limit. Only `max_iterations` stops it — a count, not money.
- The `inmemory.py` comment already says "the *real* terminator is budgets (cumulative spend) …
  this [max_iterations] just stops a runaway from spinning forever" — i.e., this is the missing piece.

## Key design conclusions (the reasoning to preserve)

### 1. Usage must be a harness-contract output, reported back to the runtime
Not an incidental `cost_usd` one harness fills — a `Usage` value every harness returns per
invocation. Interfaces are cheap to get right now and expensive to change once multiple harnesses
exist (same argument as the EventReceiver seam).

### 2. Tokens are portable; cost is NOT
- Every major provider reports prompt/completion tokens (Anthropic `input/output_tokens`, OpenAI
  `prompt/completion_tokens`, Gemini `prompt/candidatesTokenCount`, local servers eval counts).
  Mapping provider names → `input_tokens`/`output_tokens` is the adapter's job. Universally
  satisfiable.
- **Cost is derived, not reported.** Raw provider APIs (Anthropic API, OpenAI, Gemini) don't return
  dollars — you compute from tokens × a price table. The `total_cost_usd` we get is a feature of the
  **claude CLI** specifically, not of the model API. So requiring `cost_usd` would force every other
  harness to embed pricing (wrong layer) or report 0 (silently breaks the cap).
- ⇒ Contract: **tokens required; `cost_usd` optional** (filled when the harness already knows it).
  When absent, a future **runtime pricing layer** derives cost from tokens × a per-model rate table.
  Pricing centralizes where it belongs, not duplicated per harness.

### 3. An invocation can span MULTIPLE models → no singular `model` field
An agent run is a tool loop that can fan across models (subagents, routing, small+large mix). Even
claude-code does this; the CLI's `total_cost_usd` is **aggregated across models**. So a top-level
`model: str` is actively wrong and would be a corner.
- ⇒ Contract is an **aggregate** now (tokens + optional cost), with **no model field**. claude-code
  reports cost already summed across its models, so the dollar cap "just works" under multi-model
  today without us modeling the split.
- A **per-model breakdown** (`per_model: tuple[ModelUsage, …]`) is a separate, later structure. It
  is only *needed* by (a) runtime-derived pricing (can't price a summed token count when models
  differ) and (b) per-model observability — both deferred. `Usage` is a defaulted frozen dataclass,
  so adding `per_model` later (and making aggregates derived sums) is **non-breaking**. No corner.

### 4. The cap itself
- **Spend only.** Cumulative wall-clock was considered and rejected as unnecessary (per-step
  `wall_clock` already bounds each step; spend is the real concern). `window`/`latency` stay with
  durability.
- **Per-drain scope.** A `_drain()` call *is* one cascade in practice (a step's `emits` enqueues the
  next run into the same drain loop). Reset the accumulator where the `_draining` guard flips true.
  Caveat: under `serve()`, two unrelated events sharing a drain share the counter — only trips
  *earlier* (safe). Precise per-cascade-id accounting is a noted follow-up, not v1.
- **Knob, not declared budget.** `InMemoryRuntime(cascade_budget_usd: float | None = None)`, a
  backstop like `max_iterations`. A *declared* cascade budget (frontmatter / `registry.yml`) is the
  proper long-term form (ties to ARCHITECTURE open-decision #2) but is out of scope.
- **Check before each step, accumulate after.** Over-budget runs short-circuit before running the
  agent → emit nothing → the cascade winds down on its own.
- **Composes with run-failure handling** (the shipped #1 work): a `CascadeBudgetExceeded`
  (subclass of `BudgetExceeded`) raised in `_execute` is caught → recorded as a `failed` run with the
  message → surfaced via `status()` / `failed_runs` / WARNING log / `loopy trigger` exit 1. No new
  observability plumbing.

## v1 scope (decided 2026-06-18 — SUPERSEDED 2026-06-20)
> **SUPERSEDED:** the token-cap-only decision below was reversed. Final surface is `--max-spend`
> (USD) only; the token cap was built, then removed as an illegible unit. The dollar-cap design
> (originally "deferred") shipped instead. See the 2026-06-20 notes. The reasoning below is kept
> for the record.

**v1 is a token-based cumulative cap only.** A dollar `--max-spend` cap is explicitly **out of v1**
(captured below under "Deferred: dollar cap"). Rationale: tokens are the one signal every harness
reports (survey below); a dollar cap only works for cost-reporting harnesses and needs the
harness-capability gating machinery, which isn't worth building until there's demand. `cost_usd`
still rides along in the contract as optional metadata (claude-code fills it for free) — v1 simply
does not *enforce* on it.

## Proposed shape (for when we build it) — historical (token-cap variant superseded)
> The runtime/CLI lines below describe the token cap that was later removed; the final shape is
> the USD cap in "What shipped (final)". The `Usage`/`StepResult` shape is as built (tokens kept
> as telemetry).
```
# contract.py
@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None          # optional metadata; filled when the harness knows it
                                           # (claude CLI). NOT enforced in v1; reserved for the
                                           # deferred dollar cap / runtime pricing layer.
    # per_model: tuple[ModelUsage, ...] = ()  # ADD LATER, with pricing/observability — non-breaking

@dataclass(frozen=True)
class StepResult:
    output: StepOutput
    emits: Mapping[EventName, Mapping[str, Any]] = field(default_factory=dict)
    usage: Usage = Usage()                 # NEW: required of real harnesses (stubs may report zeros)
```
- `AgentHarness.run` docstring states reporting `usage` (tokens) is part of the contract; `cost_usd`
  is optional.
- `ClaudeCodeHarness` maps the `--output-format json` envelope (`usage` block → tokens;
  `total_cost_usd` → optional `cost_usd`) → `Usage`.
- Runtime (v1): `cascade_token_budget` knob; reset `_cascade_tokens` at drain start; before each step
  raise `CascadeBudgetExceeded` if `_cascade_tokens >= cap`; after each step add
  `input_tokens + output_tokens`.
- CLI (v1): `--max-tokens` on `run` and `trigger`.

## What shipped (final, 2026-06-20)
The cumulative cap is **USD only** (`--max-spend`). A token cap was built first, then removed —
dollars are the legible budget unit; a raw token count isn't (2026-06-20 decision). `Usage` keeps
tokens as reported telemetry (claude fills them for free; substrate for a future pricing layer),
but nothing enforces on them.

- [x] `Usage` type (`input_tokens`, `output_tokens` as telemetry; `cost_usd` the enforced signal;
      `total_tokens` helper) + `StepResult.usage` (`loopy_runtime/contract.py`); harness contract
      obligation documented on `AgentHarness.run`.
- [x] `ClaudeCodeHarness` fills `Usage` from the envelope (`usage` block → tokens; `total_cost_usd`
      → `cost_usd`). Base `_parse_response` returns `(message, Usage)`.
- [x] `CascadeBudgetExceeded` (subclass of `BudgetExceeded`); runtime cost accumulator
      (`_cascade_cost`) + per-step check (`_check_cascade_budget`) + reset at drain start;
      `InMemoryRuntime(cascade_budget_usd=…)` knob; `--max-spend` on `run` + `trigger`
      (`build_runtime`). Over-budget → recorded failed run.
- [x] **Static capability gate, all-or-nothing, at preflight.** When `--max-spend` is active,
      `preflight()` rejects up front if *any* reachable agent uses a harness with
      `reports_cost=False` (codex), naming the offending agents. Keyed off the static
      `agent → harness → provider.reports_cost` map (`_agent_reports_cost`).
- [x] **Runtime `None`-is-an-error backstop.** A `cost_usd is None` returned under an active cap
      is a recorded failed run (`_accumulate_cost`), never accumulated as $0 — covers a
      `reports_cost=True` harness that returns None for a specific call.
- [x] Tests: looping cascade trips the dollar cap and winds down (not via `max_iterations`);
      under-budget completes; no-cap default; accumulator resets between drains; `None` cost under
      an active cap is a recorded failure; `None` cost is fine with no cap; preflight
      rejects/permits by harness cost-capability; claude usage parsing; CLI flag + `build_runtime`
      wiring (`tests/test_b6_cascade_budget.py`, `tests/test_b2_claude_harness.py`,
      `tests/test_cli_runtime.py`).
- [ ] Follow-up: `CodexHarness` reports no cost (and zero tokens) today — it's refused under
      `--max-spend`. A runtime pricing layer (tokens × rate) would derive cost from its token
      events and make `--max-spend` cover cost-blind harnesses too.

## Dollar cap (`--max-spend`) design — NOW SHIPPED (was "deferred, NOT v1")
> Implemented 2026-06-20 exactly as designed below — see "What shipped (final)" above. Kept for
> the reasoning. A dollar-denominated cap is a clean layer on the same accumulator, but it cannot be
> universal because some harnesses report no cost (Codex; opencode for custom providers — see
> survey). The hard requirement is that it must **never silently no-op** (the "report 0 → cap
> silently broken" footgun). Design:

- **Static capability flag.** Each harness adapter declares `reports_cost: bool` (`ClaudeCodeHarness`
  → True; a `CodexHarness` → False). A static property, known from the manifest's agent→harness map.
- **Compile/startup gate, all-or-nothing over the cascade.** If `--max-spend` is set, **every agent
  reachable in the run/cascade** must use a `reports_cost=True` harness; otherwise reject before
  anything runs with a clear error (*"step `resolve/fix` uses a harness that doesn't report cost; use
  `--max-tokens`"*). All-or-nothing because one cost-blind step in a cascade makes its spend invisible
  and lets the runaway slip the cap.
- **Runtime `None`-is-an-error.** Even a `reports_cost=True` harness can return `cost_usd is None` for
  a specific call (opencode, custom provider, no price config). Under an active dollar cap, treat a
  `None` as a **recorded failed run** (existing run-failure path) — never accumulate it as 0.
- Shape: `cascade_budget_usd` knob + `--max-spend` flag, sitting beside the v1 token cap.
- Open: a runtime pricing table (tokens × rate) could later derive cost for cost-blind harnesses,
  making `--max-spend` universal without per-harness cost — but that's its own deferred milestone
  (see Non-goals).

## Non-goals / deferred (explicitly)
- ~~Dollar cap (`--max-spend`) and its harness-capability gating~~ — **SHIPPED 2026-06-20**
  (`reports_cost` flag, all-reachable-agents preflight gate, `None`-is-an-error). See "Dollar cap
  — SHIPPED" above.
- Cumulative wall-clock cap (rejected — unnecessary).
- `window`/`latency` day-scale budgets (durable-timer milestone).
- Runtime pricing table (tokens × rate) and the `per_model` breakdown it requires.
- Declared (frontmatter/registry) cascade budgets.
- Per-cascade-id precise scoping (per-drain is the v1 approximation).
- Replay-safe budget decisions (record elapsed/usage like other nondeterminism) — durable adapter.

## Harness usage-reporting survey (2026-06-18)
What four real coding harnesses actually emit, to ground "tokens required, cost optional" against
implementations rather than memory. Researched 2026-06-18 (links below).

| Harness | Structured output | Token counts | Native USD cost? | Per-model breakdown? |
|---|---|---|---|---|
| **Claude Code** | `--print --output-format json` | `input_tokens`, `output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens` | **Yes** — top-level `total_cost_usd` (client-side estimate from a bundled price table, not billing truth) | **Yes** — `modelUsage` map: per-model `costUSD` + token fields |
| **Codex** | `codex exec --json`; session JSONL | input, cached input, output, reasoning (cumulative per turn) | **No** — tokens only; billing is credit/token-based, cost derived externally (req: openai/codex#5085) | Yes — each turn tagged with model in `turn_context` |
| **opencode** | HTTP server API + JSON export | `input`, `output`, `reasoning`, `cache.read`, `cache.write` | **Yes for built-in providers** (computed from models.dev pricing); **$0 for custom providers** with no price config | Yes — per assistant message, tagged with model |
| **Pi** | `--mode json` (JSON-line events), `--mode rpc` | input/output, cache read (`R`), cache write (`W`) | **Yes** — computed from pricing tables; API-measured usage takes precedence over estimates | Yes — per message, tagged with `provider` + `model` |

**Conclusions that feed the contract:**
- **Tokens are universal** — all four report `input`/`output` (cache/reasoning vary). The required
  floor is `input_tokens`/`output_tokens`; treat cache/reasoning as optional extras. ⇒ token-based
  cap is enforceable everywhere.
- **Cost is the common case but cannot be required.** 3 of 4 emit USD; **Codex emits none**. And the
  three that do are all doing `tokens × price table` **client-side** — i.e. the same runtime pricing
  layer this plan defers. So `cost_usd` stays optional convenience, not authoritative; the token
  count is the truth. Codex is the concrete proof requiring cost would break a real harness.
- **Per-model is recoverable from all four** at the message/turn grain (the aggregate is just a
  convenience headline). Lowers the risk of the deferred `per_model` add-later — it's a regrouping,
  not new data we'd have to invent.

## Open questions
- Cap unit: **tokens** (universally enforceable, no pricing table — see survey) vs **dollars** (only
  works for harnesses that report cost; Codex doesn't). ~~Decision (2026-06-18): token cap only.~~
  **Final decision (2026-06-20): dollars (`--max-spend`) ONLY.** A token cap was shipped then
  removed — a raw token count is an illegible budget unit; cost is what an operator reasons about.
  The cost-blindness gap (Codex) is handled by refusing `--max-spend` when a reachable agent can't
  report cost, and is the motivation for the future runtime pricing layer (tokens × rate), which
  would make `--max-spend` universal. Tokens stay in `Usage` as telemetry only.
- Cost source per invocation: harness-reported (when present) vs runtime-priced from tokens+`per_model`
  (later). Survey shows even harness-reported cost is client-side `tokens × table`, so a runtime
  pricing layer is the same mechanism centralized — cost stays purely derived/optional, never required.

## Sources (harness survey, 2026-06-18)
- Claude Code: [Track cost and usage — Claude Code Docs](https://code.claude.com/docs/en/agent-sdk/cost-tracking)
- Codex: [Cost Tracking & Usage Analytics — openai/codex#5085](https://github.com/openai/codex/issues/5085), [ccusage Codex guide](https://ccusage.com/guide/codex/)
- opencode: [opencode CLI docs](https://opencode.ai/docs/cli/), [RFC: Cost Tracking Architecture — opencode#12377](https://github.com/anomalyco/opencode/issues/12377)
- Pi: [Pi session format](https://pi.dev/docs/latest/session-format), [Pi JSON mode](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/json.md)

## Notes / decisions
- 2026-06-17: Decided to DEFER all of this. No implementation, no doc changes now. Two exploratory
  edits made during discussion (`StepResult.cost_usd`, `CascadeBudgetExceeded`) were reverted; tree
  is clean. This plan is the record.
- 2026-06-18: Surveyed four real harnesses (Claude Code, Codex, opencode, Pi) — see "Harness
  usage-reporting survey". Tokens universal; cost native in 3/4 but Codex emits none, and even
  reported cost is client-side `tokens × table`. Decision: **token-based cumulative cap for v1**,
  `cost_usd` optional metadata, dollar cap as an optional later layer. Still design-only; no code.
- 2026-06-20: **shipped, then converged on dollars-only.** First landed the `Usage` contract +
  `ClaudeCodeHarness` reporting + per-drain accumulator + `CascadeBudgetExceeded`, with *both* a
  token cap (`--max-tokens`) and a dollar cap (`--max-spend`, gated all-or-nothing at preflight on
  the static `reports_cost` capability + runtime `None`-is-an-error). Then **removed the token cap**:
  a raw token count is an illegible budget unit, so `--max-spend` (USD) is the sole cumulative
  surface. `Usage` keeps tokens as telemetry. Cost-blind harnesses (Codex) are refused under
  `--max-spend`; closing that gap is the future runtime-pricing-layer follow-up. The dollar-cap
  design is preserved under "Dollar cap design" below.
- 2026-06-18: Scope call — **the dollar `--max-spend` cap is explicitly NOT in v1.** v1 enforces a
  token cap only. The harness-capability gating that a dollar cap needs (`reports_cost` flag,
  all-reachable-agents compile gate, `None`-is-an-error) is designed and recorded under "Deferred:
  dollar cap" but deferred until there's demand. `cost_usd` still rides the contract as optional
  metadata; v1 just doesn't enforce on it.
