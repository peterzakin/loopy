"""The backend contract: runtime value types + structural Protocols.

These are the seams every backend piece plugs into. Frozen against the manifest so
durable/networked/Daytona/Codex variants drop in behind the same interfaces. The
`Runtime` orchestration must stay deterministic and effect-free — all nondeterminism
lives behind `AgentHarness` / `EventBus` / `SandboxProvider`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from loopy_runtime.manifest_model import AgentSpec, SandboxSpec, StepSpec

# ── Identifiers ────────────────────────────────────────────────────────────────
RunId = str
StepId = str
EventName = str
TriggerId = str


# ── Runtime value types ─────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Event:
    """A runtime instance of a registered event."""

    name: EventName
    fields: Mapping[str, Any]
    id: str
    emitted_at: datetime


@dataclass(frozen=True)
class Tick:
    """A scheduler tick handed to a poll sensor — the poll analogue of the webhook `Request`.

    Webhook and poll sensors share their *output* (both return events) but not their
    *input*: a webhook gets the inbound HTTP `Request`, a poll gets a `Tick`. `scheduled_at`
    is when this tick fired; `last_run` is the watermark from the previous successful tick
    (None only before the seam fills it in — the scheduler supplies a cold-start window)."""

    scheduled_at: datetime
    last_run: datetime | None


@dataclass(frozen=True)
class StepOutput:
    fields: Mapping[str, Any]  # validated against step.output


@dataclass(frozen=True)
class Usage:
    """What one harness invocation consumed, reported back to the runtime.

    `cost_usd` is the enforced signal: the runtime accumulates it across a cascade to enforce
    a cumulative USD cap (the real terminator for runaway loop-backs — see the cost-budget
    plan). It's filled only when the harness knows the dollar figure (the claude CLI's
    `total_cost_usd`); None for cost-blind harnesses, which a dollar cap refuses up front.

    Tokens (`input_tokens`/`output_tokens`) ride along as reported telemetry — universal
    across providers, useful for observability and as the substrate for a future runtime
    pricing layer (tokens × rate) that could derive cost for cost-blind harnesses. They are
    NOT enforced on. A `per_model` breakdown can be added later without breaking this shape."""

    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class StepResult:
    """What a harness returns for one step: the step output, a payload per event the step
    emits (decision #3 = B — the agent produces the emitted event's fields), and the usage
    the invocation consumed (cost when the harness reports it; tokens as telemetry)."""

    output: StepOutput
    emits: Mapping[EventName, Mapping[str, Any]] = field(default_factory=dict)
    usage: Usage = field(default_factory=Usage)


@dataclass(frozen=True)
class StepContext:
    """Passed to the harness for one step execution."""

    run_id: RunId
    step_id: StepId
    event: Event  # the run's triggering event
    upstream: Mapping[StepId, StepOutput]  # after: predecessors' outputs
    idempotency_key: str


@dataclass(frozen=True)
class RunEvent:
    """An entry in the event-sourced run history."""

    kind: str  # run_started|step_scheduled|step_completed|step_failed|event_emitted|timer_fired
    step_id: StepId | None
    payload: Mapping[str, Any]
    at: datetime


@dataclass(frozen=True)
class RunStatus:
    run_id: RunId
    state: str  # running|completed|failed
    step_states: Mapping[StepId, str] = field(default_factory=dict)
    emitted: tuple[EventName, ...] = ()
    error: str | None = None  # set when state == "failed"


@dataclass(frozen=True)
class RunSummary:
    """A one-line index entry for a run, for the observability dashboard's list view (B12).

    Derived from the event-sourced history — `state`/`ended_at`/`error` come from the terminal
    `run_completed`/`run_failed` entry — so it stays a view over the single source of truth, not
    a second one. `workflow` is the run's workflow name; `ended_at` is None while running."""

    run_id: RunId
    workflow: str
    state: str  # running|completed|failed
    entry_event: EventName
    created_at: datetime
    ended_at: datetime | None = None
    error: str | None = None



@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class ToolchainLayer:
    """The toolchain a harness needs in its sandbox, expressed as *additive-only* image
    layers (apt/pip/run/env) — never a base or snapshot — so it composes onto any user
    base and the sandbox `image:` stays harness-agnostic (one sandbox reusable across
    harnesses). `probe` is the set of binaries the runtime verifies are on `PATH` in the
    live sandbox before the harness runs (#16)."""

    apt: tuple[str, ...] = ()
    pip: tuple[str, ...] = ()
    run: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    probe: tuple[str, ...] = ()

    def merge(self, *others: ToolchainLayer) -> ToolchainLayer:
        """Combine layers in order (substrate first, then the harness's): list fields
        concatenate de-duplicated (first occurrence wins position); `env` later-wins."""

        def _dedup(*seqs: tuple[str, ...]) -> tuple[str, ...]:
            seen: dict[str, None] = {}
            for seq in seqs:
                for item in seq:
                    seen.setdefault(item, None)
            return tuple(seen)

        env = dict(self.env)
        for other in others:
            env.update(other.env)
        return ToolchainLayer(
            apt=_dedup(self.apt, *(o.apt for o in others)),
            pip=_dedup(self.pip, *(o.pip for o in others)),
            run=_dedup(self.run, *(o.run for o in others)),
            env=env,
            probe=_dedup(self.probe, *(o.probe for o in others)),
        )


# ── AgentHarness ─ B4 ─────────────────────────────────────────────────────────────
@runtime_checkable
class AgentHarness(Protocol):
    async def run(self, step: StepSpec, ctx: StepContext, sandbox: Sandbox) -> StepResult:
        """Render step.body with ctx, run step.agent in `sandbox`, enforce budget, and return a
        StepResult: output validated against step.output, a payload per `emits:` event, and the
        `Usage` the invocation consumed. `Usage.cost_usd` (when the harness reports it) is what
        the runtime accumulates for the cumulative cascade spend cap; tokens ride along as
        telemetry. Raise on failure so RetryPolicy decides."""
        ...

    def required_keys(self, agent: AgentSpec) -> set[str]:
        """Env keys this harness needs in the sandbox for `agent` (e.g. the model key).
        The runtime asserts these are present before running; a stub returns an empty set."""
        ...

    def missing_keys(self, agent: AgentSpec, env: Mapping[str, str]) -> set[str]:
        """Required keys this harness cannot satisfy for `agent` given the sandbox `env`.
        The runtime refuses to run a step when this is non-empty. Defaults to
        `required_keys(agent) - env`, but a harness may honor alternative auth — e.g. Claude
        Code OAuth credentials reachable via `HOME` satisfy the model key without it being set."""
        ...

    def toolchain(self, agent: AgentSpec) -> ToolchainLayer:
        """The toolchain this harness needs in the sandbox for `agent`, as an additive image
        layer. The runtime composes it onto the sandbox's `image:` so the CLI/runtime the
        harness shells out to is present by construction, without coupling the sandbox to a
        harness. Defaults to the common substrate (git + TLS roots)."""
        ...

    def required_tools(self, agent: AgentSpec) -> set[str]:
        """Binaries that must be on `PATH` in the sandbox for `agent` (the toolchain's
        `probe`). The runtime probes the live sandbox for these before running the step and
        refuses to run when any is missing — the backstop for an image/override that defeats
        toolchain composition."""
        ...


# ── SandboxProvider ─ B4 ────────────────────────────────────────────────────────────
@runtime_checkable
class Sandbox(Protocol):
    id: str

    async def exec(self, cmd: list[str]) -> ExecResult: ...
    async def release(self) -> None: ...


@runtime_checkable
class SandboxProvider(Protocol):
    async def acquire(self, spec: SandboxSpec, secrets: Mapping[str, str]) -> Sandbox:
        """Provision compute + egress from the spec, with `secrets` injected as env."""
        ...


# ── SecretsResolver ─ §6 ───────────────────────────────────────────────────────────
@runtime_checkable
class SecretsResolver(Protocol):
    def resolve(self, sandbox_name: str, spec: SandboxSpec) -> Mapping[str, str]:
        """Resolve the sandbox's env_file(s) to an env map. Never recorded/logged."""
        ...


# ── TokenProvider ─ SCM creds for the sandbox (repo-access milestone) ──────────────────
@runtime_checkable
class TokenProvider(Protocol):
    async def token_env(self, spec: SandboxSpec) -> Mapping[str, str]:
        """Mint short-lived SCM credentials for this sandbox and return env to inject —
        the token plus its git credential-helper wiring. The long-lived secret (e.g. a
        GitHub App private key) stays at the control-plane; only this ephemeral, scoped
        token crosses into the sandbox. Returns an empty mapping when no SCM creds apply.
        Merged into the sandbox env after `SecretsResolver`, so it wins on key conflicts."""
        ...

    async def preflight(self) -> None:
        """Verify these credentials can actually mint a token, raising with an actionable
        message otherwise. Called by the runtime's fail-fast preflight when a referenced
        sandbox needs SCM auth, so a missing/misconfigured App is reported up front instead
        of mid-cascade on the first clone. A successful check warms the token cache."""
        ...


# ── StateStore ─ B8, B10, B11 ───────────────────────────────────────────────────────
@runtime_checkable
class StateStore(Protocol):
    async def create_run(self, run_id: RunId, manifest_version: str, entry: Event) -> None: ...
    async def append(self, run_id: RunId, ev: RunEvent) -> None: ...
    async def history(self, run_id: RunId) -> list[RunEvent]: ...
    async def list_runs(
        self, *, limit: int = 100, offset: int = 0, state: str | None = None
    ) -> list[RunSummary]:
        """Enumerate runs newest-first for the dashboard (B12). `state` filters by
        running|completed|failed; `limit`/`offset` paginate. The per-run reads above answer
        'what happened in run X'; this answers 'what runs happened'."""
        ...
    async def record_output(self, run_id: RunId, step_id: StepId, out: StepOutput) -> None: ...
    async def outputs(self, run_id: RunId) -> Mapping[StepId, StepOutput]: ...
    async def get_watermark(self, t: TriggerId) -> datetime | None: ...
    async def set_watermark(self, t: TriggerId, ts: datetime) -> None: ...
    async def seen(self, key: str) -> bool: ...
    async def mark_seen(self, key: str) -> None: ...


# ── EventBus ─ B5 ─────────────────────────────────────────────────────────────────
# `publish` is async and `Event` is a plain serializable value type so a networked
# broker (Redis/NATS) is a drop-in behind this Protocol; in-proc is just the first impl.
@runtime_checkable
class EventBus(Protocol):
    async def publish(self, event: Event) -> None: ...
    def subscribe(self, name: EventName, handler: Callable[[Event], Awaitable[None]]) -> None: ...
    async def run(self) -> None:
        """Run the bus's delivery loop until cancelled. In-process delivery happens inline in
        `publish`, so the in-proc bus returns immediately; a networked bus (Redis/NATS) consumes
        off the broker here and dispatches to subscribers. Started as a task by the server."""
        ...


# ── EventReceiver ─ B1 ingress (executor-side, transport-neutral) ─────────────────────────
@runtime_checkable
class EventReceiver(Protocol):
    async def receive(self, event: Event) -> RunId | None:
        """Accept an event from a SensorRunner (untrusted; any language/transport),
        re-validate it against the registry, and publish it to the EventBus. This is
        publish-and-acknowledge: it does NOT run the workflow, so it returns None once the
        event is accepted; the Runtime produces RunIds asynchronously on consume. (A
        legacy synchronous receiver may return the started RunId — Optional permits both.)

        Validation belongs HERE (not in the Runtime): the bus fans out one event to N
        subscribers, so the receiver is the single chokepoint that validates once, before the
        event multiplies and before it reaches shared broker infra. Producer *authentication*
        is deferred — in-repo/in-process sensors are trusted by co-location; auth is added
        here (additively, in front of validate→publish) only when sensors externalize."""
        ...


# ── SensorRunner ─ B1 ingress (the webhook/push edge; language-pluggable) ──────────────────
# Hosts inbound webhook sensors and delivers their events to the EventReceiver. Poll (timer)
# sensors are NOT here — they're driven by the `Scheduler` below; the two are sibling sensor
# sources that share an output (the receiver) but not a trigger mechanism.
SensorFn = Callable[..., Any]


@runtime_checkable
class SensorRunner(Protocol):
    def register_webhook(self, path: str, fn: SensorFn, *, verify: Any = None) -> None: ...
    async def start(self) -> None: ...  # serves the webhooks; each Event goes to the EventReceiver


# ── Scheduler ─ poll timing (in-process now; durable-timer seam for B7) ──────────────────
# A poll sensor's body, already imported + normalized: given a Tick, return the events to
# deliver (fan-out allowed). Synchronous like the webhook invoke; the scheduler awaits the
# downstream delivery, not the user fn.
PollFn = Callable[[Tick], list[Event]]


@runtime_checkable
class Scheduler(Protocol):
    def register(self, name: TriggerId, interval: timedelta, poll_fn: PollFn) -> None:
        """Register a poll sensor: call `poll_fn` with a fresh `Tick` every `interval`,
        keyed by `name` for watermark tracking. The in-process variant runs one asyncio
        task per sensor; a durable variant (B7) persists next-fire + a single-firing claim
        behind this same seam — the seam is 'durable timer + watermark', not Redis-specific."""
        ...

    async def start(self) -> None:
        """Run every registered poll loop until cancelled (one tick at a time per sensor)."""
        ...


# ── RetryPolicy ─ B9 ──────────────────────────────────────────────────────────────────
@runtime_checkable
class RetryPolicy(Protocol):
    def next_backoff(self, attempt: int, error: Exception) -> timedelta | None:
        """Delay before retry, or None to give up."""
        ...


# ── Runtime ─ the engine: B1–B6 (B7/B10/B11 stubbed in v1) ───────────────────────────
@runtime_checkable
class Runtime(Protocol):
    async def trigger(self, event: Event) -> RunId | None: ...
    async def tick(self, t: TriggerId, scheduled_at: datetime) -> RunId | None: ...
    async def resume(self, run_id: RunId) -> None: ...
    async def status(self, run_id: RunId) -> RunStatus: ...
