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

## Developer experience — from a real end-to-end run

Findings from building a `codefix`-style example (one `CodeTask` event → a `claude-code` agent that
clones a repo, edits, pushes a branch, opens a PR) and running it with `loopy trigger … --sandbox
local` against a real GitHub repo. It works, but getting the first green run required solving several
non-obvious issues. Suggested order: **5, 7, 8, 6 (unblock real local runs) → 9, 10, 11 (make
failures debuggable) → 12, 13, 14 (polish)**. A single integration test that runs `compile` +
`trigger` against a throwaway repo and asserts a PR/branch appears would catch #5–#8 mechanically.

### P0 — Correctness (silently breaks real runs)

- [x] ~~**5. `tools:` becomes `--allowed-tools`, restricting the agent to capability names that aren't real tools**~~
  — `loopy_runtime/harness/claude_code.py`
  `agent.tools` was passed verbatim to `claude --allowed-tools`, but the registry modeled tools as plain
  capability names (`open_pr`, `merge_pr`, `run_evals`). As an allowlist these dropped Claude's default
  `Bash`/`Edit`/`Write`, so the agent silently did nothing. **Resolved by removing the `tools:` field
  entirely** — both harnesses are tool-heavy and run under bypass, so a capability allowlist did no work
  (codex already ignored it). Capability now comes from the sandbox (image + egress), `skills:`, injected
  SCM creds, and `budget`. Removed from the registry schema, manifest schema, both harnesses, the
  `incidents` example, docs, and golden manifest. A future least-privilege story (restrict an agent's
  built-in tools) can reintroduce a purpose-built field then.

- [ ] **6. The local sandbox inherits no environment — agents start with an empty env**
  — `loopy_runtime/sandbox/local.py:51-52`
  `create_subprocess_exec(..., env=secrets)` where `secrets` is only the resolved `env_file`, so
  `claude`/`git`/`curl` aren't on `PATH`, there's no `HOME` (no `~/.gitconfig`, no Claude creds).
  Every local run failed until `PATH`+`HOME` were hand-copied into the `env_file`. **Resolved with a
  new hermetic `--sandbox docker` provider** (`loopy_runtime/sandbox/docker.py`) rather than leaking
  host env: the agent runs in a container built from the spec's `image:`, so `PATH`/`HOME`/toolchain
  come from the image — same isolation story as remote Daytona, but needing only a local Docker daemon.
  Reuses `plan_image` for validation; `apt`/`pip`/`run` layers replay via `docker exec`; secrets/tokens
  inject as `-e` env (secrets win). The bare-subprocess `local` provider stays as the no-Docker
  fallback. (Per-host egress allowlisting still unenforced locally — same gap as Daytona.)

- [x] ~~**7. `loopy trigger` doesn't inject GitHub tokens (only `loopy run` does)**~~
  — `loopy_cli/__init__.py`
  `trigger` is the documented one-shot test path but built the runtime with no `tokens=`, so an agent
  needing SCM creds couldn't get them, and the two runtime-construction sites drifted. **Resolved:**
  extracted a single `build_runtime(...)` helper (harness + sandboxes + secrets + bus + state + tokens)
  now used by both `run` and `trigger`, plus a shared `_make_token_provider(root, enabled, announce)`
  that reads the App creds from `loopy.env` (merged under the process env). `trigger` mints scoped
  tokens when an App is configured, with a `--no-tokens` opt-out for fully offline tests. (Pairs with
  #13 — once tokens are minted here the manual-token workaround goes away.)

- [ ] **8. `claude-code` harness hard-requires `ANTHROPIC_API_KEY` even when Claude is OAuth-authed**
  — `loopy_runtime/harness/base.py:59-60` + `loopy_runtime/runtime/inmemory.py:283-289` + `loopy_runtime/providers.py:38`
  The presence-only check runs before the agent starts; local Claude Code is typically OAuth/
  subscription-authed (`~/.claude/.credentials.json`), so a runnable setup dies with `sandbox provides
  no ANTHROPIC_API_KEY`. Fix: make the check provider/`HOME`-aware (if `HOME` is provided and
  `~/.claude/.credentials.json` exists, don't require the key), or downgrade to a warning for the local
  provider. At minimum the error should say how to satisfy it. Dovetails with #6 (once `HOME` is
  inherited the OAuth creds are reachable).

### P1 — Observability (when it breaks you're flying blind)

- [ ] **9. `loopy trigger` doesn't print step outputs**
  — `loopy_cli/__init__.py:320-322`
  Prints `run`/`steps`/`emitted` only; a step's `output: {pr_url, summary}` is never shown, so the PR
  URL has to be fished out of the GitHub API. For a test command the outputs are the point. Fix: print
  each completed step's outputs, or add `--json` to dump the full run record (outputs, emits, per-step
  status, spend).

- [ ] **10. Agent stdout/stderr is discarded on failure**
  — `loopy_runtime/harness/base.py:73-75` and `:138`
  Non-zero exit surfaces a one-line `stderr.strip()`; the JSON-parse failure path reports only "result
  was not JSON" — no transcript of what the agent did/decided. Fix: persist raw stdout/stderr to run
  history (or a `--verbose`/per-run log file), and surface the offending output on JSON-parse errors.

- [ ] **11. The output JSON protocol is brittle (final message must be JSON-only)**
  — `loopy_runtime/harness/base.py:136`
  `json.loads(result_text)` runs on the agent's entire final message, so a ```` ```json ```` fence or a
  trailing sentence fails the whole run even when the work succeeded. Fix: tolerant extraction — strip
  code fences and parse the last balanced JSON object before failing; optionally validate against the
  declared `output:`/`emits:` schema and re-prompt once on mismatch.

### P2 — Ergonomics & examples

- [ ] **12. No way to start the agent in a checkout — local always gets a fresh empty temp dir**
  — `loopy_runtime/sandbox/local.py:51`
  `tempfile.mkdtemp(...)` is always empty, so "edit a codebase" requires the agent to `git clone` inside
  the prompt. Fix: optional sandbox `workspace: <path>` (copy/mount into the workdir) and/or a built-in
  "clone `{{ event.repo }}` first" step option.

- [ ] **13. Secrets must be written to a file on disk; no interpolation**
  — `loopy_runtime/secrets.py:32-40`
  `env_file` values are parsed literally with no `${VAR}` expansion, so running the demo means writing a
  live `ANTHROPIC_API_KEY` and GitHub token to disk with only `.gitignore` between them and a commit.
  Fix: support env interpolation in `env_file` values (`GH_TOKEN=${GH_TOKEN}`) so secrets pass through
  from the process env without being persisted. (Pairs with #7.)

- [ ] **14. Docs/examples assume the Daytona + `run` happy path; the local path is undocumented**
  The README + `incidents` example imply tokens are auto-injected on every path — untrue on the
  `trigger`/`local` path (#7, #8). `examples/codefix` was built during the run but never committed, so
  there's nothing to mirror yet. Fix: commit a `codefix`-style example and add a "Run locally"
  quickstart documenting the `env_file` needs (`PATH`, `HOME`, `ANTHROPIC_API_KEY`, `GH_TOKEN`), plus a
  one-command CI smoke test that drives a tiny edit end-to-end.

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
