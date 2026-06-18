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

- [ ] **3. Sensor ingress — no terminal-vs-transient failure distinction**
  — `plans/future/sensor-ingress/2026-06-16-receiver-validation-and-decoupling.md` (open #7)
  A misconfig becomes a recorded failed run; under `serve()` a server re-produces identical
  failures forever. Add a fail-fast / classify pass for config vs transient errors.

- [ ] **4. Retry policy surface**
  — `ARCHITECTURE.md` §7 open decision #2
  `budget` covers wall_clock/spend but there's no failure-retry surface. Settle the decision
  (leaning: backend default + optional `retry:` step override).

## Backend capability status (B1–B12)

Live status of the backend capabilities defined in `ARCHITECTURE.md §3.1` (full definitions +
rationale there). **B1–B6** are required by every backend (the in-memory dev one included) and are
essentially done; **B7–B12** scale with the durability target and the in-memory backend may stub
B7/B10. Legend: ✅ shipped · ⚠️ partial · ❌ not built / deferred.

| # | Capability | Status | Notes |
|---|---|---|---|
| **B1** | Trigger → run instantiation | ✅ | in-memory |
| **B2** | DAG execution honoring `after:` | ✅ | |
| **B3** | Runtime template resolution (`{{ event.* }}`/`{{ step.* }}`) | ✅ | |
| **B4** | Agent invocation + typed output capture | ✅ | `claude-code` + `codex` harnesses, dispatched per agent runtime by `HarnessRouter` with per-harness model eligibility |
| **B5** | Event emission onto the bus | ✅ | in-proc + Redis bus |
| **B6** | Budget enforcement | ⚠️ | `wall_clock` + per-step `spend.usd` done; `window`/`latency` need durable timers (B7) |
| **B7** | Durable timers | ❌ | poll scheduler is in-process only → TODO #2 (DurableLite) |
| **B8** | Cron watermarks | ⚠️ | watermarks exist in the poll scheduler; durability deferred → TODO #2 |
| **B9** | Idempotent side effects + retries | ❌ | not built → TODO #4 (retry-policy open decision) |
| **B10** | Crash recoverability | ❌ | Phase 11 (DurableLite/DBOS) → TODO #2 |
| **B11** | Manifest-version pinning | ❌ | not built |
| **B12** | Observability | ⚠️ | `status()` / `failed_runs` / `drain_errors` exist; no OTel |

Open backend work clusters into the items above: **B7 + B10** (+ remaining B6/B8) → TODO #2;
**B9** → TODO #4. Update a row's status when its capability lands.

## Explicitly deferred (do not build speculatively)

- Sensor-ingress Stage 3: HTTP `POST /events` intake, producer auth, contract distribution to
  remote sensors — deferred until a real developer-hosted consumer exists.
- Sensor-secret scoping: per-sensor `env_file` references and isolating sensor secrets from the
  engine's process env. Shipped the runner-wide `sensors/.env` (`load_sensor_env`, merged into
  `os.environ` at `loopy run`); finer-grained, isolated delivery is deferred to the same boundary
  as producer auth (when sensors externalize / go polyglot). See `ARCHITECTURE.md` §6.
- Cumulative wall-clock cap, runtime pricing table + `per_model` breakdown, declared
  (frontmatter/registry) cascade budgets — see the cost-budget plan's non-goals.
