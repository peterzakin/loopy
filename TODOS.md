# TODOs

Prioritized backlog of the highest-priority items left to tackle. Each item links to its full
plan/source. When an item ships, check its box (`- [x]`) and strike the heading
(`~~...~~`) so the record of what's done stays visible. Graduate any durable decisions into
`ARCHITECTURE.md` per `plans/README.md`.

## Highest priority

- [ ] **1. Cumulative cascade spend cap + usage-reporting contract**
  — `plans/future/cost-budget/2026-06-17-usage-contract-and-cumulative-spend.md`
  No real terminator for runaway loop-back cascades today (any event that loops back to a
  workflow's entry): each step stays within its per-step budget while cumulative cost
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

- [x] ~~**4. Retry policy surface**~~
  — `ARCHITECTURE.md` §7 open decision #2 (now decided)
  `budget` covers wall_clock/spend but there's no failure-retry *surface*. **Decided: backend
  default only, no manifest surface.** The retry mechanism already operates as a backend default
  (`ExponentialBackoffRetry()` wired into the step executor, `loopy_runtime/runtime/inmemory.py`), so
  every step gets exponential backoff with no config to author. A per-step `retry:` override (the old
  leaning) was judged not worth the schema/compile surface for now; reintroduce a purpose-built field
  if a concrete need appears. The *idempotent-side-effects* half of B9 is unaffected — it still rides
  with durability (TODO #2). Note this only makes retries *configurable-by-default*; deciding which
  errors are even retryable is the separate terminal-vs-transient classification (#3).

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

- [x] ~~**8. `claude-code` harness hard-requires `ANTHROPIC_API_KEY` even when Claude is OAuth-authed**~~
  — `loopy_runtime/harness/claude_code.py` + `loopy_runtime/runtime/inmemory.py`
  The presence-only check ran before the agent started; local Claude Code is typically OAuth/
  subscription-authed (`~/.claude/.credentials.json`), so a runnable setup died with `sandbox provides
  no ANTHROPIC_API_KEY`. **Resolved:** added an env-aware `missing_keys(agent, env)` to the harness
  contract (alongside the static `required_keys`); `ClaudeCodeHarness` treats the model key as
  satisfied when OAuth creds are reachable via the sandbox's own `HOME` (`$HOME/.claude/.credentials.json`)
  — keyed on the sandbox env only, never the control-plane's, so it stays deterministic and never passes
  on creds the sandbox can't read. The error now spells out both fixes (add the key to `env_file`, or
  make OAuth creds reachable via `HOME`).

### P1 — Observability (when it breaks you're flying blind)

- [x] ~~**9. `loopy trigger` doesn't print step outputs**~~
  — `loopy_cli/__init__.py`
  Printed `run`/`steps`/`emitted` only; a step's `output: {pr_url, summary}` was never shown. **Resolved:**
  `trigger` now prints each completed step's output fields (queried from the StateStore), and a new
  `--json` flag dumps the full run record (steps, outputs, emits, failures) via a pure, unit-tested
  `_run_record` helper shared by both render paths.

- [x] ~~**10. Agent stdout/stderr is discarded on failure**~~
  — `loopy_runtime/harness/base.py`
  Non-zero exit surfaced a one-line `stderr.strip()`; the JSON-parse path reported only "result was not
  JSON". **Resolved:** the exit-code error now includes the transcript (stderr, falling back to stdout
  for combined-stream sandboxes like Daytona), tail-truncated; the parse-failure error includes the
  offending message text. (Full persistence to run history left for a `--verbose`/log-file follow-up.)

- [x] ~~**11. The output JSON protocol is brittle (final message must be JSON-only)**~~
  — `loopy_runtime/harness/base.py`
  `json.loads` ran on the agent's entire final message, so a ```` ```json ```` fence or a trailing
  sentence failed the whole run. **Resolved:** tolerant `_extract_json_object` tries the whole message,
  then a fenced block, then the last brace-balanced (string-aware) `{...}` object before failing. Schema
  validation already happens downstream in `_extract_output`/`_extract_emits`; re-prompting on mismatch
  is a possible future add.

### P2 — Ergonomics & examples

- [⚠️] **12. No way to start the agent in a checkout — local always gets a fresh empty temp dir**
  — `loopy_runtime/sandbox/local.py:51`
  `tempfile.mkdtemp(...)` is always empty, so "edit a codebase" used to require the agent to `git clone`
  inside the prompt. **Partly resolved:** a sandbox now declares `repos:` (a list of GitHub repos) and
  the `_RepoCloningProvider` factory wrapper clones them into the workspace at acquire time, via the
  shared `sandbox/workspace.py` helper over the `Sandbox.exec` seam — auth rides the injected GitHub
  token, so local/docker/daytona all get it. Still open: (a) static `repos:` only — no `{{ event.repo }}`
  templating yet; (b) an optional `workspace: <local path>` copy/mount for offline/local-only checkouts;
  (c) a `crosscheck` rule that `repos` hosts ⊆ `network`.

- [ ] **13. Live secrets on disk in `env_file` (only `.gitignore` guards them)**
  — `loopy_runtime/secrets.py:32-40`
  `env_file` values are parsed literally, so running the demo means writing a live `ANTHROPIC_API_KEY`
  (and historically a GitHub token) to disk with only `.gitignore` between them and a commit.
  **Decided against `${VAR}` process-env interpolation**: explicit env_files are a feature — one file
  tells you exactly what the sandbox sees, with no implicit coupling to the operator's shell env (which
  #6 just worked to stop leaking into the local sandbox). The residual concern is narrower than first
  framed, since the on-disk surface has already shrunk: **#7** mints scoped GitHub tokens at run time
  (no `GH_TOKEN` in an env_file) and **#8** lets Claude Code OAuth creds satisfy the model key via
  `HOME` (no `ANTHROPIC_API_KEY` on disk when subscription-authed). What's left is the API-key-authed
  Anthropic case. Treat as a docs/guardrail item rather than interpolation: document the live-value /
  gitignore requirement in the "Run locally" quickstart (#14), and consider a `doctor`-style warning if
  an env_file appears to be tracked by git.

- [x] ~~**14. Docs/examples assume the Daytona + `run` happy path; the local path is undocumented**~~
  The README + `incidents` example implied tokens are auto-injected on every path — untrue on the
  `trigger`/`local` path (#7, #8). **Resolved:** committed [`examples/codefix`](examples/codefix/) — a
  single repo-touching step (`CodeTask` → edit a checkout → open a PR → `PROpened`) with a **"Run
  locally" quickstart**: a per-`--sandbox` `env_file` matrix (`ANTHROPIC_API_KEY`/`GITHUB_TOKEN`, plus
  `PATH`/`HOME` for bare `local`), the `loopy auth github` path for token injection, and a
  `dev.env.example` template. README + `DEPLOYMENT.md` §5A now point at it and correct the
  "tokens are ambient" impression. CI smoke test is two-tier: an always-on, no-creds offline test
  (`tests/conformance/test_codefix.py`, compile + trigger on the stub harness) and a live one-command
  `examples/codefix/smoke.sh` (real edit against a throwaway repo). Still local-only on egress
  enforcement (same gap as Daytona); the live `smoke.sh` is not wired into CI (it needs real creds).

- [ ] **15. Sandbox network isolation is unenforced — `spec.network` egress allowlist is a no-op locally and on Daytona**
  — `loopy_runtime/sandbox/docker.py:121-122`, Daytona provider (same gap)
  The Docker provider is hermetic for filesystem/toolchain/env, but the container is started with
  `docker run` and no network controls, so the agent can reach any host the developer's machine can —
  `spec.network` (the per-host egress allowlist) is parsed but never enforced. Daytona has the same gap.
  This means "hermetic" today means *env/toolchain* isolation, not *network* isolation, so a compromised
  or misbehaving agent has unrestricted egress. Fix (Docker): run the container on a locked-down user
  network and enforce the allowlist (e.g. an egress proxy the container is forced through, or
  `--network` + iptables/`--add-host` rules derived from `spec.network`); deny by default. Mirror the
  enforcement contract on Daytona so both providers honor `spec.network` identically.

- [⚠️] **16. Harness toolchain contract — the harness contributes its toolchain as a layer; the sandbox image stays harness-agnostic**
  — `loopy_runtime/contract.py` (`ToolchainLayer`), `loopy_runtime/harness/{base,claude_code,codex,router}.py`,
  `loopy_runtime/sandbox/toolchain.py` (`compose_image`), `loopy_runtime/runtime/inmemory.py` (`_run_step`/`_verify_toolchain`)
  Env vars are necessary but not sufficient: `PATH`/`HOME` being set doesn't help if the binary the harness
  shells out to was never installed in the image. A bare `python:3.12-slim` had no `claude`/`node`, so (per
  #6) the agent failed deep in the run with a `command not found`. The sandbox `image:` had to stay
  harness-agnostic (one sandbox reusable across claude-code/codex), so the base image is NOT made
  harness-specific. **Shipped:**
  1. **Harness-contributed toolchain layer.** Each harness declares `toolchain()` → an *additive-only*
     `ToolchainLayer(apt, pip, run, env, probe)` (never a base/snapshot). Base = substrate (`git` +
     `ca-certificates`); `claude-code` adds node + the `claude` CLI; codex adds `codex`. `compose_image`
     **prepends** these ahead of the user's `image:` layers; the runtime composes into the *effective* image
     just before `acquire`, so providers (docker/daytona) stay untouched and the manifest spec stays
     harness-agnostic. Snapshots skip composition (prebuilt) and rely on the probe.
  2. **Runtime validation (backstop).** `required_tools()` = the layer's `probe`; `_verify_toolchain` probes
     the live sandbox after acquire (`command -v` via `sandbox.exec`, uniform across local/docker/daytona)
     and fails fast with an actionable error — so even a snapshot/base override that defeats composition
     can't reach step one ill-equipped.

  **Remaining:** (a) pin the CLI versions in each harness's toolchain for reproducible sandboxes (left as a
  `TODO(#16)` in `claude_code.py`/`codex.py`); (b) snapshot/cache the composed result keyed on
  `(base digest + toolchain hash)` so the install runs once, not on every acquire (today layers replay each
  acquire). Invariant that makes it all work: the toolchain contract is purely additive, never a base image.

## Developer experience — from a second end-to-end run (cold-start onboarding)

Findings from a second `codefix`-style run by a fresh operator, focused on the path from `loopy init`
to a first real PR. The engine works; the gaps are in the *cold-start* path — auth, what `init` hands
you, and trusting the result. Validated against current code (some of the run's complaints were already
fixed: the docker→daytona missing-user crash is now auto-handled, and there is no real
`GITHUB_APP_PRIVATE_KEY` vs `_FILE` mismatch — see #20). Suggested order: **17 (the wall) → 19 (trust)
→ 18, 21 (onboarding polish) → 20 (docs)**.

- [ ] **17. Git auth is a cold-start cliff — manifest-only, no `GITHUB_TOKEN`/`gh` fallback**
  — `loopy_cli/auth.py`, `loopy_cli/__init__.py:160-194` (`_make_token_provider`)
  `loopy auth github` is *only* the interactive browser App-manifest flow, and the resulting App must
  then be installed on each target repo by hand. `_make_token_provider` is always App-based; the scaffold
  *mentions* `GITHUB_TOKEN` ("only needed if you are NOT using a GitHub App") but no code path consumes
  it, and the sandbox inherits nothing from the shell (#6), so an exported `GITHUB_TOKEN` does nothing.
  A dev starting cold has a multi-step, mostly-undocumented detour before their first PR — the single
  biggest barrier. Add a token-based fallback: if `GITHUB_TOKEN`/`GH_TOKEN` is present (env_file or, for
  this one credential, opt-in from the operator), inject it as the SCM token and skip the App dance;
  keep the App flow as the production/multi-repo path. Unblocks #18.

- [ ] **18. `loopy init` compiles green but can't run as-is**
  — `loopy_cli/scaffold.py:63,142`, `loopy_cli/__init__.py:83-127`
  The starter points at `octocat/Hello-World` (no push access) and ships `ANTHROPIC_API_KEY=sk-ant-...`
  as a literal placeholder, with no git auth. It compiles clean, which is misleading — green compile ≠
  runnable. Before a first successful run a dev must change the repo, paste a real key into
  `secrets/dev.env`, and solve #17. Make `init` honest about the gap: print a "before your first run"
  checklist (set the key, point `repos:` at a repo you can push to, run auth), and/or a `loopy doctor`
  preflight that names exactly what's still placeholder. (Pairs with #17 — once a token fallback exists,
  the checklist gets much shorter.)

- [ ] **19. PR success is reported unverified — `pr_url` is taken from agent output, never confirmed**
  — `loopy_runtime/runtime/inmemory.py:328-333`, `loopy_cli/__init__.py:224-234` (`_run_record`)
  A step's `output.pr_url` is the agent's own parsed JSON; the run reports it with `failed: []` and no
  cross-check that the PR object actually exists on GitHub. A fabricated or malformed URL still reads as
  green. Add an optional post-step verification for SCM outputs (e.g. `GET /repos/{owner}/{repo}/pulls/...`
  using the already-minted token) that downgrades the run / annotates the record when the PR can't be
  confirmed. High trust-per-effort; self-contained.

- [ ] **20. Sandbox onboarding docs — `user:` semantics and the dual-key messaging**
  — `README.md:125-131`, `loopy_runtime/sandbox/docker.py:45-94`, `loopy_runtime/sandbox/daytona_image.py:116-140`,
  `loopy_cli/scaffold.py:161-165`
  Doc/consistency cleanup for two things the second run tripped on (both already correct in code, so this
  is docs + one small correctness call):
  (a) **`user:` on docker vs daytona.** Daytona now auto-creates the declared user (`useradd`+`chown`,
  idempotent) so the old `unable to find user … no matching entries in passwd file` crash is gone — but
  the README registry example and the scaffold don't explain this, and the **docker** provider silently
  *ignores* `user:` entirely (no `--user`, runs as root). Decide: make docker honor `user:` for parity,
  or warn/document that it's a no-op locally; either way document the auto-useradd so flipping providers
  isn't a mystery.
  (b) **`GITHUB_APP_PRIVATE_KEY` vs `..._FILE`.** Not a bug — the loader accepts the inline key (what
  `auth` writes and the scaffold documents) and falls back to `_FILE` — but presenting both forms led the
  operator to guess `_FILE` was required. Tighten the comment/error so the inline form is unambiguously
  the default.

- [ ] **21. Long cloud runs are silent, and the test command (`trigger`) isn't recorded**
  — `loopy_cli/__init__.py:408-485` (`trigger`), README:257-268
  `loopy trigger --sandbox daytona` prints ~one line, then nothing through image-build + boot + agent
  run, then the final JSON — you can't tell hung from working. And `trigger` (the command the docs hand
  you for testing) is in-memory/unrecorded, so `loopy admin` can't help; the recorded path is `loopy run`,
  a different command. Emit progress/heartbeat for the long phases, and either record `trigger` runs too
  or point users at `run` for anything observable.

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
| **B9** | Idempotent side effects + retries | ⚠️ | retries = backend default (exponential backoff, no manifest surface — TODO #4, decided); idempotent side effects deferred to durability (TODO #2) |
| **B10** | Crash recoverability | ❌ | Phase 11 (DurableLite/DBOS) → TODO #2 |
| **B11** | Manifest-version pinning | ❌ | not built |
| **B12** | Observability | ⚠️ | `status()` / `failed_runs` / `drain_errors` + a durable SQLite run store and read-only `loopy admin` dashboard (run list/timeline/outputs); no OTel yet |

Open backend work clusters into the items above: **B7 + B10** (+ remaining B6/B8) → TODO #2.
**B9**'s retry half is a settled backend default (TODO #4, decided); its idempotent-side-effects half
folds into the durability work (TODO #2). Update a row's status when its capability lands.

## Explicitly deferred (do not build speculatively)

- Sensor-ingress Stage 3: HTTP `POST /events` intake, producer auth, contract distribution to
  remote sensors — deferred until a real developer-hosted consumer exists.
- Sensor-secret scoping: per-sensor `env_file` references and isolating sensor secrets from the
  engine's process env. Shipped the runner-wide `sensors/.env` (`load_sensor_env`, merged into
  `os.environ` at `loopy run`); finer-grained, isolated delivery is deferred to the same boundary
  as producer auth (when sensors externalize / go polyglot). See `ARCHITECTURE.md` §6.
- Cumulative wall-clock cap, runtime pricing table + `per_model` breakdown, declared
  (frontmatter/registry) cascade budgets — see the cost-budget plan's non-goals.
