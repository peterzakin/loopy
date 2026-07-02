# Loopy — Deployment Architecture

> A companion to [ARCHITECTURE.md](./ARCHITECTURE.md). That doc explains the *design* —
> the compile→manifest→runtime split, the swappable modules, the phased build. **This doc
> explains a *deployment*** — the concrete pieces that make up a running Loopy, how they
> wire together, and what changes as you go from a laptop to production.

> ⚠️ **Ingress status — read first.** Both sensor models run today. `loopy run` schedules **poll**
> and **cron** triggers on an in-process scheduler (one watermark-gated task per sensor) and hosts
> **webhook** sensors as HTTP routes, fanning one URL out to every sensor on it. Webhook ingress can
> be **signed**: for `/hooks/github` paths, `loopy run` verifies GitHub's `X-Hub-Signature-256` HMAC
> at the edge when `GITHUB_WEBHOOK_SECRET` is set (a path left without a secret runs unverified — dev
> only — and says so loudly). The remaining gap is **durability**, not the trigger type: the
> scheduler is in-process, so restart-survival and single-firing across workers are still ahead
> (ARCHITECTURE B7/B8). For production you want the durable runtime behind the same `Scheduler` seam.

---

## 1. The two lifecycles

A Loopy deployment has a **build-time** path and a **run-time** path, joined by one
artifact: the **manifest**.

```
                          BUILD TIME (CI / laptop)                RUN TIME (a long-lived server)
                  ┌───────────────────────────────────┐   ┌────────────────────────────────────────┐
   author  ─────► │  registry.yml                      │   │                                          │
   the project    │  workflows/*.md                    │   │   `loopy run manifest.json`              │
                  │  skills/**/SKILL.md   ── compile ──┼──►│   hosts sensors, drives workflow runs    │
                  │  sensors/*.py                      │   │                                          │
                  └───────────────────────────────────┘   └────────────────────────────────────────┘
                       `loopy compile`  →  manifest.json          consumes the manifest; never
                       (pure, no runtime deps, runs in CI)        reads a single .md file
```

- **`loopy compile`** parses the project, resolves the DAG, statically checks every
  `{{ event.* }}` / `{{ step.* }}` reference, and emits `manifest.json` (+ generates
  `loopy.events` for sensor authors). It executes nothing and has no runtime dependency —
  it belongs in CI. A green compile is the deploy gate.
- **`loopy run manifest.json`** is the server. It loads the manifest, stands up the sensor
  webhooks, and runs workflows as events arrive. It never reads `.md` — the manifest is the
  complete IR, which is what makes "edit a workflow, recompile, redeploy" safe.

The deploy unit is therefore **`manifest.json` + the project root** (the root is still needed
at run time for two things only: the sensor module source and the sandbox `env_file`s).

---

## 2. Anatomy of a successful deployment

This is the full picture of a healthy running deployment — the Loopy server in the middle, the
outside world it integrates with on the edges. Solid arrows are the live request/event path.

```
   EXTERNAL SOURCES                    THE LOOPY SERVER  (`loopy run`)                       EXTERNAL TARGETS
 ┌──────────────────┐        ┌──────────────────────────────────────────────────┐
 │ Sentry / Linear  │        │                                                    │
 │ Datadog / Slack  │        │   ┌──────────────┐                                 │
 │ PagerDuty …      │ webhook│   │ SensorRunner │  the @sensor fn normalizes the  │
 │                  ├───POST─┼──►│  (FastAPI)   │  raw payload → a registered     │
 └──────────────────┘ /hooks │   └──────┬───────┘  Event                          │
                              │          │ Event                                  │
                              │          ▼                                        │
                              │   ┌──────────────┐                                │
                              │   │ EventReceiver│  transport-neutral intake      │
                              │   └──────┬───────┘                                │
                              │          ▼                                        │
                              │   ┌──────────────┐   routes registered events     │
                              │   │   EventBus   │   to every `on:` subscriber    │
                              │   └──────┬───────┘   (fan-out + loop-backs)        │
                              │          ▼                                        │
                              │   ┌────────────────────────────────────────┐     │
                              │   │            Runtime (the engine)          │    │
                              │   │  • instantiate a run at the `on:` step   │    │
                              │   │  • walk the `after:` DAG (topo order)    │    │
                              │   │  • render {{ event.* }} / {{ step.* }}   │    │
                              │   │  • record outputs + event-sourced history│   │
                              │   │  • publish `emits:` back to the bus ─────┼────┐ (loop-back
                              │   └───────┬──────────────────────────────────┘    │  to EventBus)
                              │           │ per step                          ◄───┘
                              │           ▼                                        │
                              │   ┌──────────────┐     ┌──────────────────┐        │
                              │   │ AgentHarness │────►│ SandboxProvider  │        │
                              │   │ (claude-code)│     │ local | daytona  │        │
                              │   └──────┬───────┘     └────────┬─────────┘        │
                              │          │ runs `claude -p` INSIDE the sandbox     │
                              └──────────┼───────────────────────┼────────────────┘
                                         │ model API calls         │ git push / open_pr /
                                         ▼                         ▼ merge_pr / set_flags …
                                ┌──────────────────┐      ┌──────────────────┐
                                │  Anthropic API   │      │  GitHub / CI /    │
                                │  (claude models) │      │  feature flags …  │
                                └──────────────────┘      └──────────────────┘
                                                          (egress gated by the sandbox
                                                           `network:` allowlist)
```

### The pieces, and what each is responsible for

| Piece | What it does in a running deployment | Today (`loopy run`) |
|---|---|---|
| **SensorRunner** | Hosts each `@sensor(webhook=…)` as an HTTP route; runs the author's function to turn a raw vendor payload into a registered `Event`. The one language-pluggable edge. | `FastAPISensorRunner` on `uvicorn`; loads the real sensor module, or synthesizes events if it can't load. Poll (`@sensor(poll=…)`) sensors run on the separate `PollScheduler`, not here. |
| **EventReceiver** | Transport-neutral intake — accepts an `Event` from any runner and injects it into the runtime. The seam that lets a non-Python sensor feed the Python engine. | `LocalEventReceiver` (in-proc; just calls `Runtime.trigger`). |
| **EventBus** | Routes registered events to every workflow whose entry step subscribes via `on:`. Handles fan-out (one event → many workflows) and loop-backs (a step's `emits:` re-enters the bus). | `InProcessEventBus` (single process). |
| **Runtime** | The engine. Instantiates a run at the `on:` step, walks the `after:` DAG in topo order, renders templates against real run data, records outputs + an event-sourced history, and publishes `emits:`. | `InMemoryRuntime` — covers B1–B6; single-process, non-durable. Durable timers/cron/resume are stubbed (B7/B10). |
| **AgentHarness** | Runs a step's prose body against its bound agent (model + skills) and validates the result against the step's typed `output:`/`emits:`. | `HarnessRouter` dispatches each step to the harness for its agent's `runtime`, enforcing per-harness model eligibility (a `claude-code` agent may only name a `claude-*` model, a `codex` agent only an OpenAI model). `ClaudeCodeHarness` runs `claude -p … --output-format json` and feeds `total_cost_usd` to the budget enforcer; `CodexHarness` runs `codex exec … --json` (token usage only — no USD cost). Both run *inside the sandbox*. |
| **SandboxProvider** | Provisions the compute + egress an agent runs in, from the sandbox spec (image build + `network:` allowlist). The trust boundary. | `local` (bare subprocess, for dev), `docker` (hermetic local container from the spec's `image:`), or `daytona` (isolated cloud container). Selected per-sandbox via `provider:` in `registry.yml` (required on every sandbox — a missing one is compile error E214); the runtime routes each step to the backend its sandbox names. No launch-time flag. `loopy init` scaffolds `daytona`. |
| **Secrets** | Resolves a sandbox's `env_file`(s) at run time and injects them into the sandbox as env vars. Never written to the manifest, never logged. | `EnvFileSecretsResolver` — reads dotenv files relative to the project root (and refuses paths that escape it). |
| **Sensor secrets** | Supplies credentials to in-process `@sensor` functions (poll + webhook). | A single runner-wide **`sensors/.env`** (`load_sensor_env`) merged into the process env at `loopy run`, so sensors read them via `os.environ`. Optional, gitignored, never in the manifest. |
| **StateStore** | Holds run history (event-sourced), step outputs, and cron/poll watermarks. | `InMemoryStateStore` — process-lifetime only. |
| **RetryPolicy** | Wraps side-effecting calls with backoff + an idempotency key (`run_id:step_id`) so retries/replays don't double-fire. | `ExponentialBackoffRetry`; budget trips are terminal (not retried). |

The agent **never calls the model from the `Runtime` process** — the `AgentHarness` shells out to
`claude` *inside the `Sandbox`*, so model calls and any tool side effects (git push, `open_pr`)
originate from the `Sandbox` and are governed by its `network:` allowlist. The `Runtime` process
itself only needs to accept events from its sensor sources and reach the `SandboxProvider`'s
control plane.

---

## 3. Compute topology: the physical boundaries

Section 2 is the *logical* pipeline — the order data flows through. This is the *physical* one:
which OS process and trust domain each piece runs in. Architecturally there are **five tiers**, and
the **`EventBus` is the seam** — everything above it is *ingress*, everything below it is
*execution*, and the two halves talk **only** through it.

**Terms — one canonical name per piece (the Protocol name from the code).** Role words (*sensor
surface*, *ingress*, *seam*, *engine*) are descriptions; the back-ticked component is the term used
throughout. **"Broker"** means one specific thing: a *networked* `EventBus` (Redis/NATS/Kafka), as
opposed to the in-process one.

| Tier | Component | Role |
|---|---|---|
| 1 | **`SensorRunner`** | the *sensor surface* — hosts developer `@sensor` code; produces events |
| 2 | **`EventReceiver`** | the *ingress gateway* — authenticates + re-validates, then publishes |
| 3 | **`EventBus`** | the *seam* — routes events to `on:` subscribers; in-process, or a **broker** when networked |
| 4 | **`Runtime`** | the *engine* — instantiates and drives runs; uses `StateStore` + `RetryPolicy` |
| (4) | **`AgentHarness`** | runs a step's agent; straddles `Runtime` ↔ `Sandbox` |
| 5 | **`Sandbox`** (via `SandboxProvider`) | the agent's exec domain — provisioned per spec |

```
                    THE INTERNET  ── your sources
                 Sentry · Linear · Datadog · Slack
                            │  HTTPS POST /hooks/…
                            ▼
 ┌─ TIER 1 · SensorRunner  (the sensor surface) ─────────────┐  own process domain
 │  developer-authored · language-pluggable · UNTRUSTED      │  Python host today;
 │  @sensor fn: raw vendor payload → a candidate Event       │  your own app via SDK
 └──────────────────────────┬─────────────────────────────────┘
                            │  deliver Event   (in-proc call | HTTPS POST /events)
                            ▼
 ┌─ TIER 2 · EventReceiver  (ingress gateway) ───────────────┐  loopy-owned · TRUSTED
 │  authenticate the producer · RE-VALIDATE the event        │  the backend's front
 │  against the registry contract · publish to the EventBus  │  door — runs NO workflows
 └──────────────────────────┬─────────────────────────────────┘
                            │  EventBus.publish(event)
            ════════════════▼═════════════════════════════════  ◄── THE SEAM
 ┌─ TIER 3 · EventBus  (the seam; modular) ──────────────────┐  decouples + buffers
 │  routes registered events to every `on:` subscriber       │  in-process (1 node)
 │  fan-out · loop-backs (a step's emits re-enters here ↺)   │  | broker: Redis/NATS/Kafka
 └──────────────────────────┬─────────────────────────────────┘
                            │  subscribe / consume
                            ▼
 ┌─ TIER 4 · Runtime  (the engine) ──────────────────────────┐  own process domain
 │  instantiate a run at the `on:` step · walk the after: DAG│  (N workers when
 │  · render templates · record history · emits → EventBus ↺ │   distributed)
 │  AgentHarness orchestration: prompt · argv · validate     │
 └──────────────────────────┬─────────────────────────────────┘
                            │  Sandbox.exec(argv)
                            │  → subprocess spawn (local) | control-plane RPC (daytona)
                            ▼
 ┌─ TIER 5 · Sandbox ────────────────────────────────────────┐  always a separate
 │  the `claude` CLI runs HERE — model API calls and every   │  exec domain, even on
 │  tool side effect (git push, open_pr) originate HERE      │  a laptop
 └──────────────────────────┬─────────────────────────────────┘
                            │  HTTPS egress, gated by the Sandbox `network:` allowlist
                            ▼
                    THE INTERNET  ── your targets
                 Anthropic API · GitHub · CI · feature flags
```

Five facts this makes explicit — and that the logical pipeline leaves ambiguous:

- **The `SensorRunner` is its own domain, and it's untrusted.** It runs developer code, possibly in
  another language (TypeScript via the SDK) in an app you already operate. It produces a *candidate*
  event and hands it off — it never touches the `EventBus` or the `Runtime` directly.
- **The `EventReceiver` is the trusted front door — on the producer side of the `EventBus`, not in
  the `Runtime`.** Its only job is producer-facing: authenticate the sender and **re-validate** the
  event against the registry contract (never trust the producer), then `publish`. It runs no
  workflows. (Where it physically runs is the next callout.)
- **The `EventBus` is the one modular seam between ingress and execution.** Swap the in-process
  implementation for a **broker** (Redis/NATS/Kafka) without touching a single tier above or below
  it — that is the entire point of the `EventBus` Protocol. It also buffers: the `EventReceiver` can
  accept and enqueue while the `Runtime` is busy or restarting.
- **The `Sandbox` is the one hard process boundary that always exists** — even on a laptop. The
  agent never runs in the `Runtime` process; `Sandbox.exec` puts it in a child subprocess (`local`)
  or a remote container (`daytona`). That boundary *is* the trust/egress boundary: secrets are
  injected into it, and its `network:` allowlist governs what the agent can reach.
- **The `AgentHarness` straddles the `Runtime` ↔ `Sandbox` boundary.** Its *orchestration* (render
  the prompt, build the argv, parse/validate the JSON envelope, enforce the budget) runs in the
  `Runtime` process; the *agent it launches* runs in the `Sandbox`. It's the one component that
  reaches across.

> **Recommendation: run the `EventReceiver` as its own small service, separate from the engine.**
> Give it one job — take the event, check it's valid, put it on the `EventBus`, done. The `Runtime`
> reads events off the bus on its own.
>
> **Why not bundle it into the engine?** If they share a process, then while the engine is busy or
> restarting you stop accepting events from your sensors. Keep them separate and the sensors keep
> delivering, the bus holds the backlog, and the engine catches up — and you can run several engine
> workers behind one receiver.
>
> **The one change that makes this possible:** today `receive()` runs the whole workflow and returns
> a `RunId`. Change it to just publish the event and return. That's the whole difference between "the
> receiver is glued to the engine" and "the receiver is its own thing."
>
> **What to do right now:** for single-node `loopy run`, leave everything in one process — it's the
> simple starting point and it's fine for dev and small deployments. The separate service is the
> production target, not day-one work. (It is never inside the `SensorRunner` — untrusted — or inside
> the broker — Redis/NATS run no loopy code.)

**What crosses each boundary:**

| Boundary | Mechanism | Over the network? |
|---|---|---|
| Source → `SensorRunner` | HTTPS `POST /hooks/…` | yes — from the internet |
| `SensorRunner` → `EventReceiver` | in-proc call (1 node) **or** HTTPS `POST /events` (SDK) | only when sensors are split out |
| `EventReceiver` → `EventBus` → `Runtime` | `EventBus.publish` / subscribe | **only here is the seam** — in-process, or a broker |
| `Runtime` → `Sandbox` (`exec`) | subprocess spawn (`local`) / control-plane RPC (`daytona`) | only `daytona` |
| `Sandbox` → model API, GitHub, … | HTTPS egress, `network:`-gated | yes — from the `Sandbox` |

### 3.1 Physical topology — today, and where we're going

The tiers are *logical*; how many *processes* they occupy is a deploy choice. **The direction is a
service-oriented architecture:** the `EventReceiver` and the `Runtime` become separate services that
talk through a broker. **For now we run them as one node** — the simple starting point — and evolve
outward. Switching modes changes only which `EventBus` you wire in; no `.md` changes, no recompile.

| | Receiver + Runtime | `EventBus` (the seam) | `Sandbox` |
|---|---|---|---|
| **Single-node — today** (`loopy run`) | **one node, one process** | in-process | separate (always) |
| **Service-oriented — the direction** | **separate services**; N `Runtime` workers behind one `EventReceiver` | a broker (Redis / NATS / Kafka) | separate (always) |

Two things hold in **both** modes:

- **The `Sandbox` is always separate.** Even single-node runs the agent in its own sandbox (a
  `local` subprocess or a `daytona` container) — it is never part of the receiver/engine node.
- **Everything on the bus is already valid, so sensors never write to the `EventBus` directly.** The
  `SensorRunner` is untrusted (developer code, maybe another language, maybe in your own app); the
  `EventReceiver` is the gate that authenticates it and re-validates the event against the registry
  contract before publishing. A sensor writing to the bus directly would skip that check (and a
  remote one can't reach an in-process bus anyway, and shouldn't get direct broker write access).
  That gate is the receiver's entire reason to exist.

### 3.2 Staying out of the corner

Today sensors are **loopy-hosted in Python** (the simple start). The plan is to expand to
**developer-hosted sensors** later — the dev's own app/language posting events to a loopy-owned
endpoint — which is what unlocks polyglot. Getting there cleanly depends on two **behaviors**, not on
extra machinery. The structure is already right (`receive()` takes a serializable `Event`, the
`@sensor` fn is a standalone `payload → Event` callable, the contract is generated as static files),
so the only corner risk is shipping the in-process version with the *wrong behaviors* and calling the
interface "ready to split." Two things land **now**:

1. **The `EventReceiver` re-validates every event against the manifest registry** — even though
   in-process the sensor "should" be correct. Skipping it bakes in sensor-trust; un-trusting it later
   means adding the gate *and* auditing everything downstream.
2. **`receive()` publishes and acknowledges — it does not run the workflow synchronously.** A remote
   receiver can't hold a connection open for a minutes-to-days run, so synchronous-run-from-receive
   must never be depended on. The `Runtime` consumes off the bus instead.

An **external broker has now landed**: `RedisEventBus` (Redis Streams + a consumer group) is a
drop-in for the in-process bus, selected with `loopy run --bus redis` — durable, out-of-process,
at-least-once delivery, exactly the networked seam this section anticipated. *(Tracked in
`plans/past/redis-broker/`.)* Crash-mid-run recovery is a separate, later concern (the durable
`Runtime`, B10) — the broker makes *transport* durable, not in-flight *runs*.

Still deliberately **deferred** to the developer-hosted milestone (additive, easy to get wrong if
built speculatively): the HTTP `POST /events` endpoint, producer authentication, and contract
versioning/distribution to remote sensors. The one rule while they're deferred — keep `Event`
serializable and never assume the sensor and receiver share an in-memory registry. *(Tracked in
`plans/future/sensor-ingress/`.)*

---

## 4. The request path of one incident (end to end)

Tracing the README's incidents example through a live deployment makes the wiring concrete:

```
1. Sentry fires a webhook            ── POST /hooks/sentry ──►  SensorRunner
2. the @sensor fn maps the payload   ── returns Incident(source=sentry, …) ──►  EventReceiver
3. EventReceiver injects it          ── Runtime.trigger(Incident) ──►  EventBus.publish
4. EventBus routes Incident          ── matches triage/investigate `on: Incident` ──►  a run starts
5. investigate runs                  ── ClaudeCodeHarness runs `claude` in a sandbox ──►  emits WorkItem
6. WorkItem re-enters the bus        ── matches resolve/arbitrate `on: WorkItem` ──►  a new run starts
7. arbitrate → fix → review → ship   ── one workflow, `after:` chain, outputs passed by reference
8. ship emits GoalShipped            ── terminal announcement on the bus
```

Steps 4 and 6 are **cross-workflow event seams** (they go through the bus). The
`arbitrate → fix → review → ship` chain in step 7 is **within-workflow** — those handoffs are
*outputs* passed by reference (`{{ fix.diff }}`), they never touch the bus. This is the
distinction from the README, observed at run time: events cross workflow boundaries; outputs
stay inside one.

---

## 5. Deployment topologies

The same five-axis composition (Runtime · StateStore · EventBus · Harness · SandboxProvider)
gets wired differently per environment. **Only the wiring changes — no `.md` and no compile
step is touched.**

### A. Laptop / CI smoke test — what ships today

```
   loopy trigger manifest.json --event Incident --fields '{...}'   # sandboxes with provider: local
   └─ InMemoryRuntime + InProcessEventBus + InMemoryStateStore + LocalSandboxProvider
      one process, fires one event, runs the cascade to completion, prints the step order
```

`loopy trigger` is the one-shot form (fire one event, run to completion, exit) — ideal for
tests and CI. `loopy run` is the same composition but long-lived, hosting the webhooks.

Heads-up on creds on this path: agent secrets are **not** ambient. A GitHub token is injected
only when a GitHub App is configured (`loopy auth github` — then `trigger` mints one too), and
a bare `provider: local` subprocess inherits *nothing* from your shell (no `PATH`/`HOME`/model
key), so its `env_file` must supply them — prefer `provider: docker`, which gets the toolchain
from the image. The [`codefix` example](../examples/codefix/) is a runnable, single-step
walkthrough of exactly this (its README has the per-provider `env_file` matrix and a
one-command smoke test).

### B. Single-node server, in-process — the dev inner loop

```
   loopy run manifest.json --in-process --host 0.0.0.0 --port 8000   # sandboxes with provider: daytona
   └─ InMemoryRuntime + InProcessEventBus + InMemoryStateStore
      + ClaudeCodeHarness + DaytonaSandboxProvider
```

One server process hosts the sensor webhooks; agents run in isolated Daytona sandboxes. No
Docker/redis needed — ideal for local iteration. This tolerates process-lifetime state only —
**if the process restarts, in-flight runs are lost** (the InMemory runtime stubs durability).
Good for short-lived cascades; not yet for day-spanning runs.

A bare `loopy run` (no `--in-process`) brings up the containerized version of this same
single-node setup — §B′ — which is the **default** and what runs inside the container.

**Fast inner loop on Daytona — `image: { snapshot: … }`.** A default `debian_slim` +
`apt`/`pip` + `npm install -g @anthropic-ai/claude-code` build is the bulk of each *cold*
Daytona run's wall-clock (several minutes), and you pay it on every `trigger` while iterating
on a workflow. The escape hatch is a **snapshot**: bake the toolchain image once, then point the
sandbox at it — Daytona skips the build entirely and boots from the prebuilt image, turning a
~minutes inner loop into ~seconds.

```yaml
# registry.yml — iterating: build the toolchain once, reuse it every run
sandboxes:
  default:
    provider: daytona
    image: { snapshot: loopy-claude-code }   # prebuilt; no apt/npm on each run
    network: [github.com]
    env_file: secrets/base.env
```

Build the snapshot once from the same layers your `image:` would compose (a Python/Node base
with `git` + the `claude` CLI), name it (e.g. `loopy-claude-code`), then reference it by name.
A `snapshot:` is **exclusive of build layers** — it's expected to already bundle the harness
toolchain, and the runtime's post-acquire probe (#16) is the backstop that fails fast with an
actionable error if the snapshot is missing a required binary. Drop back to a declarative
`image:` build whenever the toolchain changes (then re-bake the snapshot).

**`loopy run` wiring flags.** The `run`-time wiring choices are passed as CLI flags, resolved over
built-in defaults; there is no config file. The common case needs no flags at all.

```bash
loopy run manifest.json \
  --host 0.0.0.0 --port 8000 \   # the host:port that binds the sensor-webhook listener
  --bus redis \                  # inproc (single-process) | redis (networked broker)
  --state sqlite \               # sqlite (durable, default) | inproc (ephemeral)
  --state-path .loopy/state.db   # where run history is recorded (B12 observability)
```

When `--bus` is not given, it is auto-detected from the Redis connection string: a `REDIS_URL` in
the environment (or a `--redis-url` flag) selects the `redis` bus, otherwise `inproc`. So setting
`REDIS_URL` is enough to opt into Redis with no flag, and `--bus inproc` always forces the
single-process bus. Connection strings stay in the environment, never in a file: the `redis` bus
reads its URL from `REDIS_URL` (or `--redis-url`), defaulting to `redis://localhost:6379`. The
sandbox backend is not a launch flag — each sandbox's `provider:` (`local`/`docker`/`daytona`) is
declared in `registry.yml`, and the runtime routes each step to the backend its sandbox names.
`--state` selects the run-history `StateStore`: it defaults to a durable **SQLite** file
(`.loopy/state.db`, gitignored), so run history survives restarts and the `loopy admin` dashboard
can read it (see below); `--state inproc` opts back into the old ephemeral store.

**`loopy admin` — the read-only run dashboard (B12).** Run history written by `loopy run` is
served by a separate, read-only web process:

```bash
loopy run manifest.json          # writes .loopy/state.db as runs execute
loopy admin                      # in another terminal: serves http://127.0.0.1:9000
# loopy admin path/to/state.db --host 0.0.0.0 --port 9000   # explicit DB / bind
```

`loopy admin` opens the SQLite file **read-only** (it never mutates state — only `loopy run`
writes) and serves a run list with per-run timeline, emitted events, step outputs, and the failure
error. It pairs with the `loopy run` default, so the common case needs no flags. The dashboard is
single-host (it reads the local file) and unauthenticated — bind it to localhost or front it with
your own auth; internet exposure is out of scope for v1. OpenTelemetry/metrics export is a later
layer.

**`loopy.env` — control-plane infra creds for local dev.** Connection strings and provider keys
never belong in checked-in config, so for local development an optional env file supplies them: `loopy run` reads
`loopy.env` at the project root and merges it into the process env with **non-override** (a value
already set in the real/platform environment always wins), *before* resolving `redis_url` and
before any Daytona client is created. It holds **infra creds only** — `REDIS_URL`,
`DAYTONA_API_KEY`/`DAYTONA_API_URL` — and is gitignored; agent secrets stay in sandbox `env_file`s
and sensor secrets in `sensors/.env`. In production you typically skip the file and inject these
straight from the platform's secret store, which the non-override semantics respect.

```ini
# loopy.env (project root; gitignored; for local dev — production uses the platform env)
REDIS_URL=redis://localhost:6379
DAYTONA_API_KEY=dt-...
```

#### Repo access — `loopy auth github`

Agents need authenticated git access to operate on a codebase (clone, push, open PRs). loopy uses
a **bring-your-own GitHub App**: each deployment registers its *own* App, and the runtime mints
short-lived, repo-scoped **installation tokens** from it. The powerful long-lived secret — the App
private key — stays at the control-plane and never enters a sandbox; only the ephemeral token
crosses the trust boundary. There is **no loopy-owned central app and no persistent server**:
minting is pure client-side (private key → JWT → installation token via `api.github.com`).

`loopy auth github` runs GitHub's App Manifest flow so you don't register the App by hand:

```bash
loopy auth github            # create under your account
loopy auth github --org acme # create under an org
loopy auth status            # show + verify stored creds
```

It opens a browser to a local page that POSTs a manifest to GitHub; you confirm (~2 clicks);
GitHub redirects back to a one-shot `127.0.0.1` listener with a temporary code, which loopy
exchanges for the App's credentials. The App id and private key are written into `loopy.env`
(which is added to `.gitignore`, since it now holds the key); the key is stored inline with its
newlines escaped rather than referenced by a file path, so credential loading doesn't depend on
which `--root` a later command uses:

```ini
# written by `loopy auth github` (loopy.env is gitignored)
GITHUB_APP_ID=1234567
GITHUB_APP_PRIVATE_KEY=-----BEGIN RSA PRIVATE KEY-----\n…\n-----END RSA PRIVATE KEY-----\n
```

(A `GITHUB_APP_PRIVATE_KEY_FILE=<path>` is still honored if you set it yourself — e.g. a mounted
secret file in production.)

The manifest *creates* the App but does not *install* it, so the command prints an install URL —
visit it to choose exactly which repos the App can touch. The default permissions are the
fix/PR baseline: `contents: write`, `pull_requests: write`, `metadata: read`.

**Token injection.** Once an App is configured, `loopy run` mints a short-lived installation token
per step and injects it into the sandbox — the App private key stays at the control-plane; only the
ephemeral, scoped token crosses the boundary. The token rides `GITHUB_TOKEN`, and git is wired to it
via env-based config (a per-host credential helper), so `git clone`/`push` inside the sandbox just
work. Tokens are cached until shortly before they expire, so a burst of steps shares one mint. With
no App configured, nothing is injected (unchanged behavior). The token is scoped to the App's
installation — i.e. the repos you selected at install time — which is the least-privilege boundary
until a per-sandbox `repos:` field narrows it further (a sibling milestone).

### B′. Single-node, containerized — `loopy run` (the default)

Topology B, run as containers — and this is what a bare `loopy run` does. The Docker plumbing is
an implementation detail, not something you author: `loopy run` brings up the single-node
composition (redis bus, sqlite state, daytona sandbox) in containers, defaulting the manifest to
`manifest.json`. (`--in-process` opts back out to §B.)

```
  loopy run                       # manifest defaults to manifest.json
  ├─ loopy   one process: sensor webhooks + scheduler + event-bus consumer + runtime
  │          (the same `loopy run --in-process`, now inside a container)
  ├─ redis   the EventBus — decouples ingress from execution; buffers the backlog
  └─ Daytona the agent sandbox (external; reached via DAYTONA_API_KEY / DAYTONA_API_URL)
```

The five tiers collapse into the single `loopy` process: the `EventReceiver` (webhook ingress)
runs in the same process as the `Runtime`, both publishing to and consuming from **redis** as the
bus. The one tier that stays separate is the **Sandbox** — agents run in Daytona, never in the
engine container.

Two persistence facts make the deploy durable:

- **A named state volume.** SQLite is a file; a container's filesystem is ephemeral, so the
  run-history DB lives on a named volume (mounted at `/state`) to survive container recreation.
  It's scoped to the single `loopy` service — the one SQLite writer.
- **A redis volume + AOF.** Redis runs with `--appendonly yes` so accepted-but-not-yet-processed
  events survive a redis restart.

Usage:

```bash
loopy compile <project> --out <project>/manifest.json     # 1. produce the manifest
cd <project> && loopy run                                  # 2. bring up redis + loopy
# or from elsewhere: loopy run --root <project>
# add --detach to background it; --no-build to skip the image rebuild
```

Everything compose needs is derived from flags `loopy run` already takes — `--root` (the project,
bind-mounted **read-only** at `/project`), the manifest (relative to `--root`, default
`manifest.json`), `--port` — plus the project's `loopy.env` for the Daytona creds. Which backend
each agent runs in rides the manifest (the sandbox's `provider:`), not the launch command. There is no compose file or `.env` to write. Agent secrets stay
in the sandbox `env_file`s under the project, and `loopy.env` (read from the project root) still
carries the GitHub App creds for token minting. (The container stack builds the engine image from a
local source tree when one is present, otherwise from the pinned PyPI release (`loopy-computer`) via
the shipped `Dockerfile.pypi` — so container mode needs only Docker, not a source checkout.)

### B″. Render — the single-node stack on a managed host (a worked example)

Topology B′ assumes you bring a Docker host and let `loopy run` orchestrate the redis + engine
containers with compose. On a managed platform like **Render** there is no Docker daemon to
orchestrate, so you wire the same composition from the platform's own primitives instead. The
**only** change from B′ is who brings up the parts: Render runs the engine container directly with
`--in-process` (the same command the compose `loopy` service runs internally), and you point
`--bus redis` at Render's managed Redis rather than a sidecar container. No `.md` changes, no
recompile, same five modules.

The mapping is one Render resource per piece:

| B′ piece | Render resource | Notes |
|---|---|---|
| `loopy` engine process | a **Web Service** (Docker), **single instance** | it hosts the sensor webhooks, so it needs the public HTTPS ingress a Web Service gives. SQLite is a single writer and a disk pins the service to one instance anyway — never scale it past one. |
| `EventBus` (redis) | a **Key Value** instance (managed Redis) | `REDIS_URL` is read from it via `fromService`, never pasted. Pick a plan with persistence so accepted-but-unprocessed events survive a restart (the reason the compose stack runs redis with `--appendonly yes`). |
| `StateStore` (SQLite) | a **persistent disk** at `/state` | run history survives a redeploy. The disk is what forces (and matches) the single-instance constraint. |
| `Sandbox` (Daytona) | **unchanged** — external | agents still run in Daytona; Render hosts only the engine. `DAYTONA_API_KEY`/`DAYTONA_API_URL` ride the service env. |

The engine ships as its own image (Render has no bind mount, so the project travels *inside* the
image rather than mounted read-only at `/project` as in B′). Install the release from PyPI, copy the
project in, and compile during the build so a red compile fails the deploy:

```dockerfile
# Dockerfile — the engine image Render builds from your repo
FROM python:3.12-slim
RUN pip install --no-cache-dir "loopy-computer[redis]"

WORKDIR /project
COPY . /project
RUN loopy compile .            # green compile gates the build; writes manifest.json

ENTRYPOINT ["loopy"]
CMD ["run", "manifest.json", "--in-process", "--root", ".", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--bus", "redis", "--state", "sqlite", "--state-path", "/state/state.db"]
```

A `render.yaml` Blueprint declares the two services and the disk; the Web Service reads its
`REDIS_URL` straight from the Key Value instance:

```yaml
services:
  - type: web                  # the engine: sensor webhooks + runtime
    name: loopy-engine
    runtime: docker
    numInstances: 1            # SQLite is a single writer: never scale past one
    disk:
      name: loopy-state        # run history survives a redeploy
      mountPath: /state
      sizeGB: 1
    envVars:
      - key: REDIS_URL
        fromService: { type: keyvalue, name: loopy-bus, property: connectionString }
      - key: DAYTONA_API_KEY        # set in the dashboard (secret)
        sync: false
      - key: GITHUB_WEBHOOK_SECRET
        sync: false
      - key: GITHUB_APP_ID          # control-plane creds (token minting)
        sync: false
      - key: GITHUB_APP_PRIVATE_KEY
        sync: false

  - type: keyvalue             # the EventBus: managed Redis
    name: loopy-bus
    maxmemoryPolicy: noeviction
    ipAllowList: []            # internal only; reachable from the engine service
```

**The three secret surfaces (§6, §3.2) map onto two Render features.** Infra/control-plane creds —
`DAYTONA_API_KEY`/`DAYTONA_API_URL` and the GitHub App's `GITHUB_APP_ID`/`GITHUB_APP_PRIVATE_KEY`
(in the escaped-newline form `loopy auth github` writes) — are the engine's process env, set as the
service env vars above. The one that is *not* an env var is the **agent/workload secret**: it lives
in the sandbox `env_file` (e.g. `secrets/base.env`), which is gitignored and therefore in neither the
repo nor the image. Add it as a Render **Secret File** at the same project-relative path the
`env_file:` in `registry.yml` names, and `EnvFileSecretsResolver` resolves it at run time exactly as
on a VM. (`sensors/.env`, if used, is added the same way.)

**Ingress.** Render terminates TLS and gives the Web Service a public hostname; register each
source's webhook against its `/hooks/...` paths, which `loopy run` prints on startup. Set
`GITHUB_WEBHOOK_SECRET` so GitHub deliveries are signature-verified at the edge. The engine serves
only `POST` webhook routes and exposes no health endpoint, so leave Render's health check path blank
— it falls back to the open-port check on `:8000`.

**The B′ durability caveat still applies, unchanged.** The disk and Key Value persistence make
*run history* and the *event queue* survive a restart, but a run that is mid-flight when the engine
redeploys is lost — the InMemory `Runtime` stubs crash-recoverable `resume` (B10). In-flight
durability is exactly what topology C below adds; Render is a clean home for that too (point the
durable `Runtime` at a Render Postgres), but the adapter is the work, not the hosting.

### C. Durable / distributed — the production target (design-complete, behind the same interfaces)

```
   ┌─ DurableLite (DBOS, Postgres-backed)  OR  Temporal adapter (cluster)   ← swap the Runtime
   ├─ Postgres / Temporal history          ← swap the StateStore
   ├─ NATS / Redis EventBus                 ← swap the bus (single node → distributed)
   ├─ ClaudeCodeHarness                     ← unchanged
   └─ DaytonaSandboxProvider                ← unchanged
```

This is where durable timers (`cron(…)`, budget windows that span days), crash-recoverable
`resume`, and manifest-version pinning (B7–B11) become real. The interfaces in
`loopy_runtime/contract.py` are frozen precisely so this drops in without touching the
frontend. See [ARCHITECTURE.md §9](./ARCHITECTURE.md) for the per-provider adapter mapping.

**What's invisible vs. what leaks:** the *code* is swappable across all three; the
*operational shape* is not. A → "nothing extra"; B → "a Daytona account"; C → "bring a
Postgres" or "run a Temporal cluster." Whoever deploys sees that difference.

---

## 6. The deployment checklist

What you actually need in place for a successful run, in order:

1. **A green compile.** `loopy compile .` (writes `manifest.json`; use `--check` to validate
   without writing) exits 0. This is the gate —
   every `on:`/`emits:` names a registered event, every `{{ }}` ref resolves, the DAG is
   acyclic, every sensor declares a registered `emits`. Run it in CI.
2. **The manifest, shipped.** `manifest.json` is the deploy artifact. Carry the project root
   alongside it (needed only for sensor module source + `env_file`s).
3. **Agent secrets, as sandbox `env_file`s.** Each sandbox's `env_file` must exist under the
   project root and contain the keys the harness needs — at minimum the model API key for the
   agent's runtime (`ANTHROPIC_API_KEY` for `claude-code`, `OPENAI_API_KEY` for `codex`). A pre-flight
   check at startup (`loopy run`/`loopy trigger`) aggregates and reports every agent whose sandbox
   can't supply its harness's required keys, failing fast before any step runs; `env_file` paths that
   escape the root are rejected too.
4. **Sensor secrets, as `sensors/.env`.** Credentials an in-process `@sensor` needs (a poll API
   key, a webhook-signing secret) go in a single runner-wide `sensors/.env` under the project
   root. `loopy run` merges them into the process env (non-override: a value already set in the
   real environment wins), so sensors read them via `os.environ`. Optional and gitignored. Keep
   infra creds (`DAYTONA_API_KEY`, `REDIS_URL`) *out* of it — those are the server's own env
   (item 5), and sensors share the engine's process env today.
5. **Control-plane / infra creds.** The creds the engine itself needs go in its **process env**:
   for the `daytona` provider (loopy's default sandbox), `DAYTONA_API_KEY` / `DAYTONA_API_URL`
   (the SDK ships in the core deps); for `--bus redis`, a reachable `REDIS_URL` (or `--redis-url`). In production these
   come from the platform (container env, systemd, CI secret store). For **local dev**, drop them
   in **`loopy.env`** at the project root — merged into the process env at `loopy run` with
   non-override (a value set in the real environment always wins).
   Keep this file to infra creds only; agent secrets live in sandbox `env_file`s (item 3) and
   sensor secrets in `sensors/.env` (item 4). For the `local` provider: nothing — but agents run as
   subprocesses on the host, so it's dev-only.
6. **Egress allowlist.** Each sandbox's `network:` list is the egress contract — it must include
   every host an agent reaches (e.g. `github.com` for opening or merging a PR). The model
   API endpoint must be reachable from inside the sandbox.
7. **Sensor sources pointed at the server.** Each external source's webhook must POST to the
   matching `@sensor(webhook="/hooks/…")` path on `host:port`. `loopy run` prints the hosted
   webhook paths on startup — verify the count and paths match what you expect.

### Failure modes worth knowing

- **Sensor module won't load** → the server logs a warning and falls back to *synthesizing*
  events from the contract, so the path still exercises end-to-end. Real payloads need the
  module to import cleanly (it imports the `loopy` authoring shim).
- **`cron(…)` / poll triggers** → scheduled today by the **in-process** `PollScheduler` (poll
  sensors fire watermark-gated; `on: cron(...)` entries fire `Runtime.tick` on each occurrence).
  What's still ahead is **durable** scheduling (survives restart, single-firing across workers) —
  the B7/B8 timer work, which drops in behind the same `Scheduler` seam.
- **Webhook ingress** → hosted by `loopy run` as HTTP routes, with **signed ingress** for GitHub
  (`X-Hub-Signature-256` verified at the edge when `GITHUB_WEBHOOK_SECRET` is set). A `/hooks/github`
  route left without a secret runs unverified — **dev only**; set the secret before exposing it to
  untrusted networks. A general per-source auth framework for arbitrary providers is still ahead.
- **Process restart** → in-flight runs are lost under the InMemory runtime (topology A/B). This
  is the single biggest reason to move to topology C for anything long-running.
- **Budget trip** → terminal, not retried. A step exceeding `wall_clock` or `spend.usd` fails
  the run rather than looping.

---

## 7. One-glance summary

```
  AUTHOR ──compile──► manifest.json ──run──►

      ingress  │                        ║  the seam  ║                  │  execution
      ─────────┤                        ║ (EventBus) ║                  ├────────────
   SensorRunner ─► EventReceiver ──────► EventBus ──────► Runtime ─────► AgentHarness
   (untrusted)     (trusted gate)        (in-proc |       (walk DAG ·         │
                                          broker)          emits ↺)           ▼ runs `claude` in
                                                                          Sandbox (local | daytona)
                                                                              │            ▲
                                                                         model API     secrets (env_file)
                                                                         + tool egress  injected at run time
                                                                         (network: allowlist)
```

The **`EventBus` is the seam**: ingress (`SensorRunner` → trusted `EventReceiver`) on one side,
execution (`Runtime` → `AgentHarness`) on the other, talking only through it. The **`Sandbox`** is
the other hard boundary — where trust and egress are enforced. The manifest is the contract between
author and `Runtime`; the `Sandbox` is the contract between agent and the outside world.
