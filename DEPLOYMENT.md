# Loopy — Deployment Architecture

> A companion to [ARCHITECTURE.md](./ARCHITECTURE.md). That doc explains the *design* —
> the compile→manifest→runtime split, the swappable modules, the phased build. **This doc
> explains a *deployment*** — the concrete pieces that make up a running Loopy, how they
> wire together, and what changes as you go from a laptop to production.

> ⚠️ **Ingress direction — read first.** The intended sensor model is **polling** (Loopy calls
> out to each source on a schedule). **Webhook ingress is a future improvement:** supporting
> arbitrary third-party webhooks requires per-source authentication, which is deferred (see
> `BACKLOG.scratch.md` and the sensor-ingress plan). The webhook examples below illustrate that
> future path — they are **not** a production ingress. (Honest status: webhook routes currently
> run but are unauthenticated; durable poll scheduling is itself still ahead — ARCHITECTURE B7/B8.
> So neither is production-ready yet; polling is the *direction*.)

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
| **AgentHarness** | Runs a step's prose body against its bound agent (model + tools + skills) and validates the result against the step's typed `output:`/`emits:`. | `HarnessRouter` dispatches each step to the harness for its agent's `runtime`, enforcing per-harness model eligibility (a `claude-code` agent may only name a `claude-*` model, a `codex` agent only an OpenAI model). `ClaudeCodeHarness` runs `claude -p … --output-format json` and feeds `total_cost_usd` to the budget enforcer; `CodexHarness` runs `codex exec … --json` (token usage only — no USD cost). Both run *inside the sandbox*. |
| **SandboxProvider** | Provisions the compute + egress an agent runs in, from the sandbox spec (image build + `network:` allowlist). The trust boundary. | `local` (subprocess, for dev) or `daytona` (isolated cloud container). Selected with `--sandbox`. |
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
   loopy trigger manifest.json --event Incident --fields '{...}' --sandbox local
   └─ InMemoryRuntime + InProcessEventBus + InMemoryStateStore + LocalSandboxProvider
      one process, fires one event, runs the cascade to completion, prints the step order
```

`loopy trigger` is the one-shot form (fire one event, run to completion, exit) — ideal for
tests and CI. `loopy run` is the same composition but long-lived, hosting the webhooks.

### B. Single-node server — what ships today

```
   loopy run manifest.json --host 0.0.0.0 --port 8000 --sandbox daytona
   └─ InMemoryRuntime + InProcessEventBus + InMemoryStateStore
      + ClaudeCodeHarness + DaytonaSandboxProvider
```

One server process hosts the sensor webhooks; agents run in isolated Daytona sandboxes. This
is a genuine deployment for workloads that tolerate process-lifetime state — **if the process
restarts, in-flight runs are lost** (the InMemory runtime stubs durability). Good for
short-lived cascades; not yet for day-spanning runs.

**`loopy.yaml` — deployment defaults for `loopy run`.** The `run`-time wiring choices map to a
small config file so they need not be retyped as flags. It is optional: absent the file,
defaults apply unchanged.

```yaml
# loopy.yaml (next to where you invoke `loopy run`; override path with --config)
sensor_server:        # the host:port that binds the sensor-webhook listener
  host: 0.0.0.0
  port: 8000
bus: redis            # inproc (single-process) | redis (networked broker)
```

Precedence is **explicit flag > loopy.yaml > built-in default**, so `--bus inproc` still wins
over a file that says `redis`. Connection strings stay in the environment, never the file:
`bus: redis` reads its URL from the `REDIS_URL` env var (or the `--redis-url` flag), defaulting
to `redis://localhost:6379`. `sandbox` is *not* a config key — it's declared per-agent in
`registry.yml`; the `--sandbox` flag selects only the provider backend (`local`/`daytona`).
`state:` (durable StateStore) and `limits:` (spend caps) are reserved for B10/B-cost and not yet
read.

**`loopy.env` — the secret companion to `loopy.yaml`.** Because connection strings and provider
keys can't live in the YAML, a local-dev convenience file supplies them: `loopy run` reads
`loopy.env` at the project root and merges it into the process env with **non-override** (a value
already set in the real/platform environment always wins), *before* resolving `redis_url` and
before any Daytona client is created. It holds **infra creds only** — `REDIS_URL`,
`DAYTONA_API_KEY`/`DAYTONA_API_URL` — and is gitignored; agent secrets stay in sandbox `env_file`s
and sensor secrets in `sensors/.env`. In production you typically skip the file and inject these
straight from the platform's secret store, which the non-override semantics respect.

```ini
# loopy.env (project root; gitignored; local-dev convenience)
REDIS_URL=redis://localhost:6379
DAYTONA_API_KEY=dt-...
DAYTONA_API_URL=https://...
```

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

1. **A green compile.** `loopy compile . --out manifest.json` exits 0. This is the gate —
   every `on:`/`emits:` names a registered event, every `{{ }}` ref resolves, the DAG is
   acyclic, every sensor declares a registered `emits`. Run it in CI.
2. **The manifest, shipped.** `manifest.json` is the deploy artifact. Carry the project root
   alongside it (needed only for sensor module source + `env_file`s).
3. **Agent secrets, as sandbox `env_file`s.** Each sandbox's `env_file` must exist under the
   project root and contain the keys the harness needs — at minimum the model API key for the
   agent's runtime (`ANTHROPIC_API_KEY` for `claude-code`, `OPENAI_API_KEY` for `codex`). The runtime refuses to start a
   step if its sandbox can't supply the harness's required keys, and refuses `env_file` paths that
   escape the root.
4. **Sensor secrets, as `sensors/.env`.** Credentials an in-process `@sensor` needs (a poll API
   key, a webhook-signing secret) go in a single runner-wide `sensors/.env` under the project
   root. `loopy run` merges them into the process env (non-override: a value already set in the
   real environment wins), so sensors read them via `os.environ`. Optional and gitignored. Keep
   infra creds (`DAYTONA_API_KEY`, `REDIS_URL`) *out* of it — those are the server's own env
   (item 5), and sensors share the engine's process env today.
5. **Control-plane / infra creds.** The creds the engine itself needs go in its **process env**:
   for `--sandbox daytona`, `DAYTONA_API_KEY` / `DAYTONA_API_URL` (and `loopy-core[daytona]`
   installed); for `--bus redis`, a reachable `REDIS_URL` (or `--redis-url`). In production these
   come from the platform (container env, systemd, CI secret store). For **local dev**, drop them
   in **`loopy.env`** at the project root — the secret companion to `loopy.yaml`, merged into the
   process env at `loopy run` with non-override (a value set in the real environment always wins).
   Keep this file to infra creds only; agent secrets live in sandbox `env_file`s (item 3) and
   sensor secrets in `sensors/.env` (item 4). For `--sandbox local`: nothing — but agents run as
   subprocesses on the host, so it's dev-only.
6. **Egress allowlist.** Each sandbox's `network:` list is the egress contract — it must include
   every host an agent's tools reach (e.g. `github.com` for `open_pr`/`merge_pr`). The model
   API endpoint must be reachable from inside the sandbox.
7. **Sensor sources pointed at the server.** Each external source's webhook must POST to the
   matching `@sensor(webhook="/hooks/…")` path on `host:port`. `loopy run` prints the hosted
   webhook paths on startup — verify the count and paths match what you expect.

### Failure modes worth knowing

- **Sensor module won't load** → the server logs a warning and falls back to *synthesizing*
  events from the contract, so the path still exercises end-to-end. Real payloads need the
  module to import cleanly (it imports the `loopy` authoring shim).
- **`cron(…)` / poll triggers** → recorded in the manifest but **not scheduled** by the v1
  runtime (durable timers are B7/B8, deferred). Polling is the intended sensor model; the
  durable scheduler that runs it is still ahead.
- **Webhook ingress** → a **future improvement**, not production-ready: the webhook routes run
  but are unauthenticated (authenticating arbitrary third-party webhooks is deferred). Don't
  expose them to untrusted networks.
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
