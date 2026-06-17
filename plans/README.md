# Plans

Working plans for features and tasks, drafted with a coding agent before code is written, then
ticked off and annotated with decisions as the work proceeds.

## Layout

```
plans/
  TEMPLATE.md              # copy this to start a new plan
  README.md                # this file
  future/<epic>/           # plans whose work has NOT yet merged
  past/<epic>/             # archived plans whose work HAS merged
```

- **Epics** group related plans (a coherent body of work). An epic is a kebab-case directory, e.g.
  `backend-v1`, `loopy-core-frontend`. One milestone = one plan file inside its epic.
- **Lifecycle is per-plan.** A plan starts in `future/<epic>/`. When its work merges to `main`, move
  that one file to `past/<epic>/` (`git mv`). The **same epic name appears under both `future/` and
  `past/`** while the epic is in flight — the directory name is what correlates them.
- `future/` answers "what's left to do"; `past/` is the archive of shipped work.

## Conventions

- File naming: `YYYY-MM-DD-short-slug.md` (the date orders milestones within an epic).
- Each plan carries a `**Status:**` line (`draft` / `active` / `done`) so progress *within* an
  in-flight epic is visible without moving the file — the directory tracks merged/not, the field
  tracks in-progress state.
- Commit plans alongside the code they produce; move a plan to `past/` in the same PR (or the
  follow-up) that merges its work.
- **Lasting architectural decisions don't live only in archived plans.** When a plan captures a
  durable decision, graduate the rationale into `ARCHITECTURE.md` / `FRONTEND.md` (or a
  `docs/decisions/` ADR) so it survives independently of the archive.

## Starting new work

1. `mkdir -p plans/future/<epic>/` if the epic is new.
2. Copy `TEMPLATE.md` to `plans/future/<epic>/YYYY-MM-DD-slug.md` and draft it with the agent.
3. As you build, tick steps and log decisions in the plan.
4. On merge, `git mv` the plan to `plans/past/<epic>/`.

Point your agent at the active epic under `future/` (e.g. from `CLAUDE.md`) so it reads the live
plan at the start of a session.

## Current epics

- **`future/sensor-ingress/`** — receiver-as-gate + decoupling (core shipped; HTTP intake / auth /
  contract distribution still open).
- **`future/cost-budget/`** — usage-reporting contract + cumulative cascade spend cap (design only).
- **`past/redis-broker/`** — `RedisEventBus` (Redis Streams) as the first networked broker behind
  the `EventBus` seam. Shipped; durable-run recovery still B10.
- **`past/poll-sensors/`** — in-process asyncio poll scheduler behind a durable-timer seam
  (`Tick` input, multi-event fan-out, watermarks). Shipped; durability deferred to B7.
- **`past/daytona-sandbox/`** — DaytonaSandboxProvider behind the Sandbox Protocol. Shipped.
- **`past/agent-emitted-events/`** — the agent produces emitted-event payloads (#3, E1) and real
  sensor execution (#2, E2). Shipped.
- **`past/backend-v1/`** — the non-durable, in-memory backend (env_file addendum → contract →
  in-memory engine). Shipped.
- **`past/loopy-core-frontend/`** — the compile frontend (M0–M5). Shipped.
