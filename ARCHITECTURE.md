# Loopy — Architecture

## 0. Architecture principle

> **loopy-core compiles workflows into a declarative, validated plan (the manifest) with
> runtime-resolved holes; each runtime adapter is a *host* that interprets that plan against its
> own durable-execution primitives — and a conformance suite over a reference plan keeps every
> adapter honest.**

This is the dbt-core + adapters model: compile once to a portable artifact, then add runtime
support incrementally as adapters, never touching the compile layer or any workflow `.md`. Three
clarifications keep the analogy precise:

1. **The plan is an IR with runtime holes, not finished code.** dbt resolves `ref()` at compile
   time and emits ready-to-run SQL. Loopy can't fully resolve — `{{ event.* }}` / `{{ step.* }}`
   values exist only per-run — so the manifest is a validated DAG-with-templates the adapter
   instantiates each run. The holes are exactly the recorded, replayable values (§6).
2. **Adapters interpret the plan; they don't codegen it (by default).** An adapter is a generic
   engine that reads the manifest and drives steps against its runtime's primitives. Generating a
   native program per workflow (a Temporal workflow definition, a DBOS module) is a later
   optimization, not the baseline.
3. **Loopy adapters are hosts, not just translators.** A dbt adapter is ~a stateless SQL emitter;
   a loopy runtime adapter *owns a long-lived, resumable, stateful run* (durable timers, retries,
   replay across days). Don't size adapter effort by dbt's.

---

## 1. Mental model

Loopy is an **authoring + compile layer over a durable-execution runtime** — the same
relationship DBT has to a data warehouse:

```
dbt-core   : warehouse        ::   loopy frontend : loopy backend
manifest   : compiled artifact ::  loopy manifest : compiled artifact
```

The system splits into two halves joined by one serialized artifact, the **manifest**:

- **Frontend** (`loopy-core`) — parses `registry.yml`, `workflows/*.md`, `skills/`, and
  `sensors/*.py`; resolves the DAG; statically checks every `{{ event.* }}` / `{{ step.* }}`
  reference; and emits the manifest. Pure, dependency-free, runs at build/CI time, executes
  nothing.
- **Backend** (a pluggable runtime) — consumes `(manifest, triggering event)` and runs it.
  Never reads `.md` files; the manifest is the complete IR.

```
AUTHORING / FRONTEND (loopy-core)        │  BOUNDARY       │  RUNTIME / BACKEND (pluggable)
─────────────────────────────────────── │ ─────────────── │ ──────────────────────────────
registry.yml ─┐                          │                 │
workflows/*.md ┼─ parse ─ resolve DAG ───┼─► manifest ─────┼─► Runtime  (the modular engine)
skills/*       │  ─ static {{ }} check   │   (.json:       │     ├─ InMemory      (dev)
sensors/*.py ──┘  ─ compile / validate   │    DAG + event  │     ├─ DurableLite   (sqlite/pg)
                                         │    contracts +  │     └─ Temporal      (adapter)
                                         │    lineage)     │   composed from modules:
                                         │                 │     • AgentHarness · SandboxProvider
                                         │                 │     • EventBus · StateStore · SensorRunner
```

---

## 2. The manifest (the contract between halves)

A versioned JSON document — the only thing a backend is allowed to depend on. It carries:

- **`schema_version`** — a backend pins each in-flight run to the version it started under, so
  editing a `.md` mid-run never corrupts replay.
- **Nodes (steps)**: `name`, trigger (`on: <Event>` or `cron("expr", tz)`), `after:`
  edges, resolved `agent` binding, `output:` schema (typed field map), `emits:`, `budget`, and
  the prose **body with validated template slots**. Template *values* exist only at runtime, so
  the manifest carries the checked ref graph, not rendered text — the backend renders `{{ }}` at
  execution.
- **Registry, resolved**: agents merged with `defaults`; sandbox specs (image + network
  allowlist); event field contracts (typed maps).
- **Lineage**: cross-workflow event seams (`Incident`, `WorkItem`, `MetricThreshold`,
  `GoalReopened`, `ResultRejected`) and within-workflow output edges.
- **Sensors**: registered sensor metadata (route/poll config + the event each declares it emits).
  The function *bodies* stay in their authoring language — Python today, other languages later;
  the manifest is language-neutral and records only the descriptor (see §4.6).

---

## 3. Backend requirements

This section defines what **any** backend must do. A conforming backend, given a manifest and a
triggering event, must satisfy these. Each requirement is also where a modular piece plugs in.

### 3.1 Functional contract (what a backend MUST guarantee)

| # | Requirement | Why |
|---|---|---|
| **B1** | **Trigger → run instantiation.** On receiving a registered event (or a cron tick) that matches a manifest entry step's `on:`, create a new run rooted at that step. | The `on:` step is the DAG root. |
| **B2** | **DAG execution honoring `after:`.** Run each non-entry step only after all its `after:` predecessors complete, passing predecessor **outputs** by reference for `{{ step.field }}` resolution. | The engine's core job. |
| **B3** | **Template resolution at runtime.** Resolve `{{ event.* }}` (entry event) and `{{ step.* }}` (a direct `after:` predecessor's output) against real run data before invoking the agent. | Values only exist at run time. |
| **B4** | **Agent invocation + typed output capture.** Run a step's prose against its bound agent (model + tools + skills + sandbox) and validate the result against the step's `output:` schema. | Steps are agent tasks. |
| **B5** | **Event emission onto the bus.** When a step (or sensor) `emits:` a registered event, publish it so other workflows' `on:` subscriptions fire — including loop-backs (`ResultRejected → propose`, `GoalReopened → arbitrate`). | Cross-workflow seams. |
| **B6** | **Budget enforcement.** Enforce `budget.wall_clock` (minutes) and `budget.spend.usd`; honor `window`/`latency` (days). | Runs span days; must be bounded. |
| **B7** | **Durable timers.** `cron(expr)` ticks and budget/wait windows must survive process restarts (no language `sleep`). | Day-long waits can't live in memory. |
| **B8** | **Cron watermarks.** Persist `last_run` per cron/poll trigger and supply `{{ event.scheduled_at }}` / `{{ event.last_run }}` so steps scan only what changed. | README's incremental-scan contract. |
| **B9** | **Idempotent side effects + retries.** Retry failed agent/sensor/tool calls with backoff; side effects (emit, `open_pr`, `merge_pr`) carry an idempotency key (`run_id + step_id`) so retries/replays don't double-fire. | Long runs *will* hit transient failures. |
| **B10** | **Crash recoverability (durability level is the swap point).** State sufficient to resume a run without re-executing already-completed steps. *How much* durability is exactly what distinguishes the InMemory / DurableLite / Temporal adapters. | The A/B/C decision lives here. |
| **B11** | **Manifest-version pinning.** A run executes under the manifest version it began with. | Safe mid-flight edits. |
| **B12** | **Observability.** Expose run status, step state, emitted events, and failures. | Operability. |

B1–B6 are required by *every* backend, even the in-memory dev one. B7–B12 scale with the
durability target — an InMemory backend may stub B7/B10 (process-lifetime only); a production
backend must implement them fully.

### 3.2 The swappable modules

A backend is **assembled** from these independently replaceable pieces. "Make the engine
modular" = swap the `Runtime`; the rest give modularity along orthogonal axes.

| Module | Responsibility | Satisfies | Example implementations |
|---|---|---|---|
| **`Runtime`** | Orchestration: walk the DAG, schedule steps, record outputs, drive timers, resume. The durable-execution core. | B1–B3, B7, B10, B11 | InMemory · DurableLite (sqlite/pg) · Temporal adapter |
| **`StateStore`** | Persist run history (event-sourced log), step outputs, watermarks (`last_run`). | B8, B10, B11 | dict (in-mem) · SQLite · Postgres · Temporal history |
| **`AgentHarness`** | Run a step's prose against a model + tools + skills inside a sandbox; return structured output. | B4 | claude-code (Claude Code) · codex (OpenAI Codex) |
| **`SandboxProvider`** | Provision compute + egress from a sandbox spec (image build, network allowlist). | B4 | Daytona · local subprocess · container |
| **`EventBus`** | Route registered events to `on:` subscribers. In-proc = single machine; networked = distributed. | B5 | in-process · **Redis (Streams)** · NATS · Kafka |
| **`SensorRunner`** | The **language-pluggable** ingress edge: hosts + runs the developer's `@sensor` code, normalizes returns into `Event`s, and delivers them to the `EventReceiver`. Stateless. | B1 (ingress) | Python (FastAPI) · (future) Node/TS |
| **`EventReceiver`** | Trusted ingress intake on the **producer side of the `EventBus`**: accepts an `Event` from any `SensorRunner` (any language/process), re-validates it against the registry contract, and hands it on toward the `EventBus`. Loopy-owned and trusted — distinct from the untrusted `SensorRunner`. The seam that lets a non-Python `SensorRunner` feed the Python `Runtime`. | B1 (ingress) | in-process · HTTP `POST /events` · broker |
| **`Scheduler`** | Fire poll sensors on a timer: hand each `@sensor(poll=…)` a `Tick` (`scheduled_at`, `last_run`) every interval, deliver the normalized event(s) through the `EventReceiver`, and advance the per-sensor watermark only on success. The **timer** seam (distinct from the `EventBus` delivery seam). | B1 (poll) · B7 (durable timing) | in-process asyncio · (future) Redis zset+lock · Postgres · Temporal/DBOS native timers |
| **`RetryPolicy`** (cross-cutting) | Backoff + idempotency-key strategy wrapping all side-effecting calls. | B9 | exponential-backoff default |

> **Trigger direction:** `poll` is the intended near-term sensor trigger. **Webhook ingress is a
> future improvement** — hosting webhooks for arbitrary third-party services requires per-source
> authentication (deferred); the `webhook` references in this doc describe that future path. Durable
> poll scheduling itself is the deferred B7/B8 work, so neither is production-ready yet.

**The ingress boundary (and why it's split):** the durable `Runtime` is always a single Python
implementation; the **sensor surface is the one language-pluggable layer**. A `SensorRunner`
produces events; an `EventReceiver` re-validates and hands them on; the `EventBus` routes them to
the `Runtime`:

```
SensorRunner (any language)  ──Event──▶  EventReceiver  ──▶  EventBus  ──▶ Runtime
   (untrusted producer)                  (trusted ingress)   (the seam)    (subscribes)
```

**Where the `EventReceiver` runs follows from one interface choice — and there are exactly two
modes.** `receive(event) -> Optional[RunId]` permits both:

- **Synchronous (in-process), today.** `receive` accepts the event and *triggers a run*, returning
  its `RunId` (`LocalEventReceiver` just calls `Runtime.trigger`). Because it drives execution, it
  co-resides with the `Runtime` in one process — single-node, in-process `EventBus`.
- **Decoupled (distributed).** `receive` *publishes to the `EventBus` and returns `None` (an ack)*;
  a `Runtime` worker produces the `RunId` later, on consume. Now the receiver is a small
  **stateless ingress service in front of the broker**, scaled and kept up independently of the
  engine.

So the `EventReceiver` has exactly **two physical homes, never more**: **embedded** in the single
loopy process (synchronous mode), or a **standalone ingress service** in front of the broker
(decoupled mode). Never inside the untrusted `SensorRunner`, and never inside the broker (Redis/NATS
run no loopy code). This is what makes a TypeScript `SensorRunner` possible **without** a second
`Runtime` — it just delivers `Event`s to the receiver.

**Why validation lives at the receiver, and why auth doesn't (yet).** The receiver's job is
*validation*, and it sits at ingress for one structural reason: the bus **fans out** (one event → N
`on:` subscribers), so the receiver is the single chokepoint where an event is checked **once,
before it multiplies** and before it lands on shared broker infra. Validating downstream in the
`Runtime` instead would either re-check per-workflow (N times) or force a new single gate on the
consumer side of the bus — i.e. reinvent the receiver, just in a worse place. So validation stays
here. *Authentication is a separate, deferred concern.* Today sensors are in-repo, compiled, and
**in-process**, so they're trusted by co-location — there is no producer to authenticate and the
receiver does validation only (no auth). Producer auth becomes necessary precisely when a
`SensorRunner` moves **out of process / out of repo** (the polyglot, developer-hosted path): "in
repo ⇒ trusted" stops describing reality, and the receiver — the first trusted component on the far
side of that network boundary — picks up authenticating the remote producer at `POST /events`. That
step is **additive** (a credential check in front of the same `validate → publish`), not a move of
where validation lives. Until sensors externalize, receiver auth may legitimately never be built.

**`EventBus.publish` means different things in the two modes — wire callers accordingly.**
In-process, `publish` runs the subscribed handlers *inline* and returns once they're enqueued, so
`Runtime.trigger()` can `await bus.publish(event)` and then `_drain()` the work it just produced.
A **networked bus cannot honor that**: `publish` only durably accepts the event (`XADD`); the
matching handler fires later, from the bus's `run()` consumer loop. So a networked `EventBus`
(`RedisEventBus`) is **decoupled-mode only** — it backs the long-lived `serve()` path, never the
synchronous `trigger()` path. The lesson the seam encodes: treat `publish` as *"durably accepted,"*
not *"delivered,"* and never wire a synchronous-drain caller onto a networked bus (it would drain an
empty queue). The in-proc bus's inline delivery is the convenience of single-process, not the
contract. `RedisEventBus` uses Redis **Streams + a consumer group** (durable, buffered,
at-least-once with `XACK`; dedupe by `Event.id` via `StateStore.seen`), not pub/sub (lossy).

**Key property — orthogonality:** these axes are independent. Same `Runtime` + in-proc `EventBus`
= single-process; same `Runtime` + networked `EventBus` + Postgres `StateStore` = distributed.
Swap Daytona for a local sandbox in tests without touching the engine. The A/B/C durability
choice is just *which `Runtime` + `StateStore` pair* you wire in.

**The `Scheduler` is a swappable module with a durability target — and a broker does not replace
it.** A poll tick is three steps: (1) *fire the tick* (decide "it's time") — needs a **timer
source**; (2) *read/advance the watermark* (`last_run`) — needs the `StateStore`; (3) *deliver the
produced events* — the `EventReceiver` → `EventBus`. A message broker (Redis/NATS pub-sub) only
helps **step 3** (delivery), which already rides the `EventBus`; it does **not** fire timers. So
"lean on Redis" would mean Redis in up to *three* distinct roles (streams = delivery/`EventBus`, KV
= watermark/`StateStore`, sorted-set + `SETNX` lock = a hand-rolled durable timer) — only the first
is the broker role. The seam to design for is **"durable timer + watermark," not anything
Redis-specific**: ship the **in-process asyncio** scheduler now (the InMemory-equivalent for
timers, one task per sensor, sequential per sensor, watermark-advances-only-on-success), and drop a
durable backing in behind the `Scheduler` Protocol at B7. Crucially, if a durable-execution
`Runtime` (Temporal/DBOS) is adopted, **durable timers arrive as a native primitive** — so the
hand-rolled Redis zset is likely never built. (Cron-expression intervals + workflow `on: cron(…)`
arrive with that same scheduler later; poll sensors use duration strings only.)

### 3.3 Conformance suite

One backend-agnostic test set every `Runtime` must pass: run the README's incidents +
autoresearch example manifest end-to-end and assert outputs, emitted events, and step order.
This is what keeps the engine genuinely swappable rather than swappable-in-theory.

### 3.4 Interface signatures (Phase 6 boundary)

Python `Protocol`s — structural, so any conforming class is a valid backend piece without
inheritance. These pin the seams from §3.2; the requirement each satisfies is noted inline.

```python
from __future__ import annotations
from typing import Protocol, runtime_checkable, Any, Awaitable, Callable, Iterator, Mapping, Optional
from dataclasses import dataclass
from datetime import datetime, timedelta

# ── Identifiers ───────────────────────────────────────────────────────────────
RunId     = str   # one workflow run
StepId    = str   # workflow-qualified step, e.g. "resolve/fix"
EventName = str   # registered event type, e.g. "WorkItem"
TriggerId = str   # a cron/poll trigger instance, for watermarks

# ── Runtime value types (subset of the manifest IR; full dataclasses in Phase 6) ─
@dataclass(frozen=True)
class Event:                          # a runtime instance of a registered event
    name: EventName
    fields: Mapping[str, Any]         # validated against the registry contract
    id: str                           # stable id → dedupe / idempotency
    emitted_at: datetime

@dataclass(frozen=True)
class Tick:                           # a scheduler tick handed to a poll sensor
    scheduled_at: datetime            # when this tick fired
    last_run: Optional[datetime]      # the watermark; scheduler supplies a cold-start window

@dataclass(frozen=True)
class StepOutput:
    fields: Mapping[str, Any]         # validated against step.output schema

@dataclass(frozen=True)
class StepContext:                    # passed to the agent for one step execution
    run_id: RunId
    step_id: StepId
    event: Event                              # the run's triggering event
    upstream: Mapping[StepId, StepOutput]     # outputs of after: predecessors
    idempotency_key: str                      # = f"{run_id}:{step_id}"

@dataclass(frozen=True)
class RunEvent:                       # an entry in the event-sourced history
    kind: str   # run_started|step_scheduled|step_completed|step_failed|event_emitted|timer_fired
    step_id: Optional[StepId]
    payload: Mapping[str, Any]
    at: datetime

# StepSpec / SandboxSpec / Budget / Manifest come from the compiled manifest (§2).

# ── AgentHarness ─ B4 ───────────────────────────────────────────────────────────
@runtime_checkable
class AgentHarness(Protocol):
    async def run(self, step: "StepSpec", ctx: StepContext, sandbox: "Sandbox") -> StepOutput:
        """Render step.body templates with ctx, run step.agent in `sandbox`, enforce
        step.budget.wall_clock, and return output validated against step.output.
        Raise AgentError on failure so RetryPolicy can decide."""

# ── SandboxProvider ─ B4 ──────────────────────────────────────────────────────────
@runtime_checkable
class SandboxProvider(Protocol):
    async def acquire(self, spec: "SandboxSpec") -> "Sandbox": ...   # build image + apply egress allowlist

@runtime_checkable
class Sandbox(Protocol):
    id: str
    async def exec(self, cmd: list[str]) -> "ExecResult": ...
    async def release(self) -> None: ...

# ── StateStore ─ B8, B10, B11 ─────────────────────────────────────────────────────
@runtime_checkable
class StateStore(Protocol):
    # run lifecycle (event-sourced)
    async def create_run(self, run_id: RunId, manifest_version: str, entry: Event) -> None: ...
    async def append(self, run_id: RunId, ev: RunEvent) -> None: ...        # append-only
    async def history(self, run_id: RunId) -> list[RunEvent]: ...           # → replay (B10)
    # step outputs (replayed, never re-derived)
    async def record_output(self, run_id: RunId, step_id: StepId, out: StepOutput) -> None: ...
    async def outputs(self, run_id: RunId) -> Mapping[StepId, StepOutput]: ...
    # cron/poll watermarks ─ B8
    async def get_watermark(self, t: TriggerId) -> Optional[datetime]: ...
    async def set_watermark(self, t: TriggerId, ts: datetime) -> None: ...
    # idempotency ─ B9
    async def seen(self, key: str) -> bool: ...
    async def mark_seen(self, key: str) -> None: ...

# ── EventBus ─ B5 ─────────────────────────────────────────────────────────────────
@runtime_checkable
class EventBus(Protocol):
    async def publish(self, event: Event) -> None: ...                      # in-proc: deliver inline; networked: durably accept (XADD)
    def subscribe(self, name: EventName, handler: Callable[[Event], Awaitable[None]]) -> None: ...
    async def run(self) -> None: ...                                        # consume loop (no-op in-proc; XREADGROUP→dispatch→XACK on Redis)

# ── EventReceiver ─ B1 ingress (trusted, producer-side of the EventBus) ───────────────
@runtime_checkable
class EventReceiver(Protocol):
    async def receive(self, event: Event) -> Optional[RunId]:
        # Re-validate against the registry, then hand the event on. Two modes:
        #   synchronous (in-proc) — trigger a run, return its RunId (today); or
        #   decoupled — publish to the EventBus, return None (ack); a Runtime
        #   worker produces the RunId on consume. The Optional permits both.
        ...

# ── SensorRunner ─ B1 ingress (the webhook/push edge; language-pluggable) ──────────────
SensorFn = Callable[..., "Event | None | Iterator[Event]"]   # @sensor-decorated fn

@runtime_checkable
class SensorRunner(Protocol):
    def register_webhook(self, path: str, fn: SensorFn) -> None: ...
    async def start(self) -> None: ...      # serves webhooks; each Event → EventReceiver
    # Poll (timer) sensors are NOT here — they run on the Scheduler below. Sibling sensor
    # sources: shared output (the receiver), different trigger (HTTP push vs timer).

# ── Scheduler ─ poll timing (in-process now; durable-timer seam for B7) ───────────────
PollFn = Callable[[Tick], list[Event]]      # imported + normalized poll body (fan-out allowed)

@runtime_checkable
class Scheduler(Protocol):
    def register(self, name: TriggerId, interval: timedelta, poll_fn: PollFn) -> None:
        # Fire poll_fn with a fresh Tick every interval, keyed by name for the watermark.
        # In-process: one asyncio task/sensor, sequential. Durable (B7): persists next-fire
        # + a single-firing claim behind this same seam (timer + watermark, not Redis-specific).
        ...
    async def start(self) -> None: ...      # run every poll loop until cancelled

# ── RetryPolicy ─ B9 ────────────────────────────────────────────────────────────────
@runtime_checkable
class RetryPolicy(Protocol):
    def next_backoff(self, attempt: int, error: Exception) -> Optional[timedelta]:
        """Delay before retry, or None to give up."""

# ── Runtime ─ the engine: B1–B3, B6, B7, B10, B11, B12 ───────────────────────────────
@runtime_checkable
class Runtime(Protocol):
    async def trigger(self, event: Event) -> RunId:        # B1: event → new run rooted at matching on:
        ...
    async def tick(self, t: TriggerId, scheduled_at: datetime) -> RunId:  # B7/B8: cron tick
        ...
    async def resume(self, run_id: RunId) -> None:         # B10: rebuild from history, continue
        ...
    async def status(self, run_id: RunId) -> "RunStatus":  # B12
        ...
```

**Composition is the modularity** — a backend is wired from the pieces; swap any argument:

```python
runtime = DurableLiteRuntime(                 # ← swap for InMemoryRuntime / TemporalRuntime
    manifest = load_manifest("manifest.json"),
    state    = PostgresStateStore(dsn),       # ← or InMemoryStateStore / SQLite / Temporal history
    harness  = HarnessRouter(agents),         # ← AgentHarness axis: dispatch per agent runtime
                                              #   (claude-code → ClaudeCodeHarness, codex → CodexHarness)
    sandboxes= DaytonaProvider(),             # ← or LocalSandboxProvider in tests
    bus      = NatsEventBus(...),             # ← in-proc (single node) vs networked (distributed)
    retry    = ExponentialBackoff(max_attempts=5),
)
sensor_runner = FastAPISensorRunner(LocalEventReceiver(runtime))   # producer → receiver → bus
```

The **`Runtime` is the unit of durability modularity** — one adapter per provider (§9). The other
five axes vary independently. Note `StateStore` is **optional plumbing**, not a mandatory seam:
self-managed runtimes (the in-memory dev backend, or a thin "framework-as-logger" build) use it,
but library-owns-the-loop adapters (Temporal, DBOS) keep durability internal and ignore it —
their own history/Postgres *is* the store. Every concrete `Runtime` must pass the §3.3 conformance
suite.

---

## 4. Frontend components (`loopy-core`)

### 4.1 Registry + type DSL
Parse `registry.yml` into typed models (`Defaults`, `Sandbox`, `Agent`, `Event`); apply
`defaults.agent` inheritance; parse the field-type mini-DSL (`id`, `str`, `int`, `url`,
`enum[a,b]`). Enforce naming: entities Capitalized; `default`
is the one reserved lowercase sandbox.

### 4.2 Event codegen → `loopy.events`
Generate importable typed Event definitions from the registry so sensors get author-facing types
and their own typechecker validates payload shapes. **Multi-target**, one per supported sensor
language: Python (dynamic module backed by the loaded registry + `.pyi` stubs from `loopy compile`),
TypeScript (`.d.ts`), more additively. Author-facing only — Core reads each sensor's declared
`emits`, not these types (see §4.6).

### 4.3 Workflow loader + DAG
Split each `.md` into YAML frontmatter + prose body → `Step`. Build the DAG: exactly one `on:`
entry per workflow; every other step has `after:`; a step with neither is a compile error; the
`after`-graph must be acyclic (loop-backs go through *events*, not `after:`).

### 4.4 Template resolver + static ref check
Extract `{{ event.field }}` / `{{ step.field }}` refs. Statically validate: `event.*` resolves to
the entry event's fields; `step.*` resolves only to a direct `after:` predecessor and the field
exists in that step's `output:`. No `ref()` — pure templates.

### 4.5 Compiler (`loopy compile`) → manifest
One pass tying 4.1–4.4 together: orphan steps; `on:`/`emits:` reference registered events; sensor
return annotations are registered Event classes; template refs resolve to declared fields;
`agent`/`skills`/`sandbox` references resolve; output type maps valid. Rich `file:line` errors.
Emits the **manifest** (§2). This is the keystone — everything downstream trusts it.

### 4.6 Sensors (compile-time half)
Validate the README rule by **static inspection, never import**: a sensor must *declare* a
registered event via `emits` (a decorator arg in Python, a `sensorRegistry` literal in TypeScript)
in a form Core can read without executing code, or it fails to load. The declaration — not the
return type — is the source of truth; the return annotation is optional sugar the author's own
typechecker enforces. A **pluggable per-language inspector** reduces each sensor to a common
descriptor (Python AST first); the function *bodies* run in the backend's `SensorRunner` (§3.2),
which executes whatever language the sensor is written in. Sensors straddle the line: declarations
are frontend-validated, execution is backend.

---

## 5. Phased build

| Phase | Deliverable | Half |
|---|---|---|
| **1** | Registry + type DSL | frontend |
| **2** | Event codegen (`loopy.events`) | frontend |
| **3** | Workflow loader + DAG builder | frontend |
| **4** | Template resolver + static ref check | frontend |
| **5** | `loopy compile` → **manifest** (+ sensor compile checks) | frontend |
| **6** | Define backend interfaces (§3.2) + manifest schema v1 | boundary |
| **7** | **InMemory backend** — satisfies B1–B6, stubs B7/B10; prove README example end-to-end | backend |
| **8** | `AgentHarness` (claude-code, codex) + `SandboxProvider` (daytona) behind interfaces | backend |
| **9** | `SensorRunner` (webhook) + `EventBus` | backend |
| **10** | Cron triggers + watermarks (B7, B8) | backend |
| **11** | **DurableLite backend** — event-sourced StateStore, retries, durable timers (B7–B11) | backend |
| **12** | Conformance suite + full example E2E; **(optional) Temporal adapter** | backend |

**Build order rule:** Phases 1–5 deliver value with zero runtime dependencies (the DBT
parse→compile→manifest path). Phase 6 freezes the interfaces *against the in-memory backend in
Phase 7* — do not freeze the contract before one real backend validates it. Durable/Temporal
adapters (11–12) then drop in behind the same interfaces without touching the frontend or any
workflow `.md`.

---

## 6. Cross-cutting

- **Testing:** README example as golden compile fixtures (assert exact errors on broken
  variants); engine tests with fake `AgentHarness`/`SandboxProvider` (no real model/sandbox
  calls); conformance suite across backends (§3.3).
- **Versioning:** manifest `schema_version` + per-run pinning (B11).
- **Determinism boundary:** all nondeterminism (agent output, time, randomness) is captured as
  *recorded* values in the StateStore and replayed — never re-derived. (Temporal's
  Workflow/Activity discipline, applied even to the hand-built backend.)
- **Secrets / egress:** sandbox `network:` allowlist is the egress contract; secrets injected per
  agent, never in the manifest. **Three secret surfaces, one principle** (resolve at the point of
  execution, never serialize) — each with its own home so the layers don't bleed into one another:
  - **Agent/workload secrets** — defined at the **sandbox** (`env_file` reference), resolved by a
    `SecretsResolver` at run time, injected as the sandbox's `env_vars` (the trust boundary).
  - **Sensor secrets** — a single runner-wide **`sensors/.env`** (`load_sensor_env`), merged into
    the engine's process env at `loopy run` so in-process `@sensor` functions read them via
    `os.environ`. Sensors are in-repo and trusted-by-co-location today, so they share the process
    env rather than carry per-sensor references. Per-sensor `env_file` scoping and isolation from
    the engine's process env are deferred until sensors externalize (the same boundary as
    producer-auth, §3.2).
  - **Control-plane / infra creds** — the creds the *engine itself* needs (`REDIS_URL`,
    `DAYTONA_API_KEY`/`DAYTONA_API_URL`). In production these are the deployment's process env; for
    local dev they may be supplied from **`loopy.env`** at the project root
    (`load_control_plane_env`), merged with `setdefault` (real/platform env always wins). This is
    the secret companion to `loopy.yaml`; the explicit name keeps its scope unambiguous —
    connection strings / provider keys only, never agent or sensor secrets.
- **Runtime config (`loopy.yaml` + `loopy.env`):** deployment defaults for `loopy run` (the
  `sensor_server` host/port and the `bus` backend) live in an optional `loopy.yaml`, mapping ~1:1
  to CLI flags. Resolution precedence is **explicit flag > `loopy.yaml` > built-in default**; an
  absent file changes nothing. Connection strings/secrets stay out of the YAML — `redis_url`
  resolves from `--redis-url` > the `REDIS_URL` env var > default, where that env var may itself be
  seeded for local dev from `loopy.env` (the secret companion, loaded before resolution).
  `sandbox` is *not* a config key (it's registry-owned, per-agent); `state:`/`limits:` are reserved
  for the durable StateStore (B10) and spend caps. See `DEPLOYMENT.md` §5.

---

## 7. Open decisions

1. **Durability target for the first production backend** — DurableLite (self-contained,
   sqlite/pg) vs Temporal adapter (offload durability, take the operational dependency). Deferred
   to Phase 11 by design; the interface work in Phase 6 keeps both open.
2. **Retry policy surface** — README's `budget` covers wall_clock/spend but not failure-retry.
   Add a `retry:` block to step frontmatter, or keep retry policy backend-config-only? (Leaning:
   backend default + optional step override.)
3. **MVP cut** — ship Phases 1–7 (compile + in-memory run of the example) as milestone 1?

---

## 8. Leverage / dependencies (build vs adopt)

Most of loopy is glue over mature OSS; the value is the compile rules and the manifest→runtime
mapping, not the plumbing.

| Seam | Adopt | Notes |
|---|---|---|
| Frontmatter split | `python-frontmatter` | YAML header + prose body |
| YAML parse | `ruamel.yaml` | preserves line numbers → `file:line` errors |
| Schemas / validation | `pydantic` v2 | registry, events, outputs; type DSL → pydantic types |
| Template render + static `{{ }}` check | `Jinja2` | `env.parse()` + `meta.find_undeclared_variables()` for §4.4; renders at runtime |
| DAG build / validate | `networkx` | topo sort, cycle detection, reachability for `after:` |
| CLI | `Typer` | `loopy compile` / `run` / `dev` |
| `loopy.events` stubs | `datamodel-code-generator` | `.pyi` from the registry |
| Cron + scheduling | `croniter` + `APScheduler` | 5-field expr + tz; drives poll/cron ticks |
| `SensorRunner` webhooks | `FastAPI` + `uvicorn` | mount `@sensor(webhook=…)` routes |
| `EventBus` (networked) | `FastStream` over NATS/Redis | in-proc bus is trivial; swap brokers without touching the engine |
| Observability | `OpenTelemetry` + `structlog` | DBOS and Temporal both emit OTel |

**Three high-leverage adoptions that change the plan:**

1. **`AgentHarness` = Claude Agent SDK (`claude-agent-sdk`).** The README's `harness.runtime:
   claude-code` maps directly onto it — tool loop, subagents, MCP, **skills**, human-in-the-loop.
   The `skills:` registry field becomes SDK config. Do not build an agent loop. The `codex`
   runtime (OpenAI's headless `codex exec`) is a sibling behind the same interface — pick it per
   agent with `harness.runtime: codex` and an OpenAI model.
2. **`DurableLite` ≈ DBOS, not hand-built.** DBOS is a Postgres-backed durable-execution
   *library* (no cluster) — matches loopy's self-contained aesthetic and delivers B7–B11 for free.
   Revises Phase 11 from "reimplement event-sourcing" to "map manifest steps onto DBOS
   decorators."
3. **`SandboxProvider` = Daytona (per README), but the interface lets users pick isolation.**
   Daytona = containers, sub-90ms cold start; Modal = gVisor. Choice is per-deployment.

Net: the A/B/C durability paths become **adopt, don't build** — DBOS (self-contained) or Temporal
(heavy-duty), both behind the §3.4 `Runtime` interface; the from-scratch surface shrinks to the
in-memory dev backend plus the manifest→primitive mapping.

---

## 9. Durable provider adapters

The durable layer is modular at the **`Runtime`-adapter grain** — coarse (own a whole run), not
fine-grained durability primitives (which would leak each framework's model). Loopy hands the
adapter the manifest and says *"own this run; expose `trigger`/`tick`/`resume`/`status`."* How it
achieves durability is its business. This is what makes a third-party `loopy-runtime-<x>` package
possible — the dbt community-adapter ecosystem.

**Provider mapping** (same coarse interface, different internals):

| Provider | `trigger(event)` → | steps → | `resume` | brings |
|---|---|---|---|---|
| **InMemory** | asyncio task walking the DAG | direct `AgentHarness.run` | n/a (lost on crash) | nothing |
| **DBOS** | a DBOS `@workflow` invocation | `@step` (checkpointed + retried) | auto recovery from Postgres | a Postgres |
| **Temporal** | start a Workflow (manifest+event as input) | **Activities** (agent run, emit, tools) | auto replay from event history | a cluster |
| **Restate** | a Restate handler invocation | journaled invocations | journal replay | a Restate runtime |

**The constraint this imposes on loopy-core (non-negotiable):** orchestration must be
**deterministic and effect-free**. All nondeterminism — agent output, time, randomness, event
payloads — is captured as recorded results ("activities"), never executed inline in the DAG-walk.
Effects live only in `AgentHarness` / `emit` / `SandboxProvider`. Pay this and any replay engine
can host loopy; violate it and portability silently breaks at replay time. (This is the §6
determinism boundary, restated as a portability requirement.)

**What stays provider-specific:** the portable interface exposes only the intersection of provider
capabilities ∩ loopy's needs (trigger, run-DAG, emit, durable timer, resume, status). Provider
extras (Temporal signals/queries/search-attributes, DBOS queues) stay behind the adapter. The
**operational shape leaks** — "run a cluster" vs "bring a Postgres" vs "nothing" is visible to
whoever deploys; the code is swappable, the deployment isn't invisible.

**Contract for a conforming adapter:** (a) consume the documented manifest schema; (b) implement
the §3.4 `Runtime` Protocol; (c) pass the §3.3 conformance suite — run the reference manifest with
identical outputs/emits/ordering **and** survive a kill-and-resume test. (c) is the enforcement;
without it, "modular" is theoretical and adapters drift.
