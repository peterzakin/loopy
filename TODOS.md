# TODOs

Prioritized backlog of the highest-priority items left to tackle. Each item links to its full
plan/source. When an item ships, check its box (`- [x]`) and strike the heading
(`~~...~~`) so the record of what's done stays visible. Graduate any durable decisions into
`ARCHITECTURE.md` per `plans/README.md`.

## Highest priority

- [ ] **1. Cumulative cascade spend cap + usage-reporting contract**
  — `plans/future/cost-budget/2026-06-17-usage-contract-and-cumulative-spend.md`
  No real terminator for runaway loop-back cascades today (`ResultRejected → propose`,
  `GoalReopened → arbitrate`): each step stays within its per-step budget while cumulative cost
  grows unbounded — only `max_iterations` (a count, not money) stops it. Make `Usage` (tokens
  required, `cost_usd` optional) a harness-contract output, accumulate per-drain, raise
  `CascadeBudgetExceeded` before each step, expose `--max-tokens`. **v1 is a token-based cap only**;
  the dollar `--max-spend` cap is deferred (tokens are the only universally-reported signal — see the
  plan's harness survey). Design-complete; best value-to-effort ratio. **Pick up first.**

- [ ] **2. Durability — DurableLite backend (B7 + B10), Phase 11**
  — `ARCHITECTURE.md` §5 (phase 11), §8, §9
  Largest capability gap. InMemory runtime loses all state on crash and has no durable timers, so
  `cron` ticks, watermarks, and `window`/`latency` day-scale budgets are all stubbed (poll
  scheduler and Redis broker both shipped with durability deferred to B7/B10). Decided approach:
  adopt DBOS (Postgres-backed durable-execution library) behind the existing `Runtime` interface
  rather than hand-rolling event-sourcing. Gate to anything beyond a dev demo; larger lift than #1.

## Secondary

- [ ] **3a. Sensor ingress — `loopy trigger` bypasses the validation gate**
  — `plans/future/sensor-ingress/2026-06-16-receiver-validation-and-decoupling.md` (open #2)
  The operator one-shot calls `Runtime.trigger` directly, so `--fields` events skip registry
  validation. Route it through the receiver, or validate in `trigger`.

- [ ] **3b. Sensor ingress — no terminal-vs-transient failure distinction**
  — same plan (open #7)
  A misconfig becomes a recorded failed run; under `serve()` a server re-produces identical
  failures forever. Add a fail-fast / classify pass for config vs transient errors.

- [ ] **4. Retry policy surface**
  — `ARCHITECTURE.md` §7 open decision #2
  `budget` covers wall_clock/spend but there's no failure-retry surface. Settle the decision
  (leaning: backend default + optional `retry:` step override).

## Explicitly deferred (do not build speculatively)

- Sensor-ingress Stage 3: HTTP `POST /events` intake, producer auth, contract distribution to
  remote sensors — deferred until a real developer-hosted consumer exists.
- Cumulative wall-clock cap, runtime pricing table + `per_model` breakdown, declared
  (frontmatter/registry) cascade budgets — see the cost-budget plan's non-goals.
