"""InMemoryRuntime (B1–B6) — non-durable, single-process engine.

Walks the `after:` DAG, renders templates, runs each step's agent in a sandbox,
records outputs + an event-sourced history, and publishes `emits:` to the bus
(which triggers subscribed workflows — the cross-workflow cascade, incl. loop-backs
and event fan-out). The DAG-walk is effect-free: the only effects are the harness,
the bus, and the sandbox.

Out of scope (v1): durable timers, crash-recoverable `resume`, version pinning.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime

from loopy_runtime.budget import BudgetExceeded
from loopy_runtime.contract import (
    Event,
    RunEvent,
    RunId,
    RunStatus,
    StepContext,
    StepOutput,
    StepResult,
    TriggerId,
)
from loopy_runtime.manifest_model import Manifest, SandboxSpec, StepSpec, WorkflowSpec
from loopy_runtime.retry import ExponentialBackoffRetry
from loopy_runtime.sandbox.toolchain import compose_image
from loopy_runtime.sensors.scheduler import cron_prev
from loopy_runtime.state.inmemory import InMemoryStateStore

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


class PreflightError(RuntimeError):
    """A credential/config problem found before any step runs.

    Raised by `preflight()` (fail-fast) so a misconfigured project is reported up front,
    in full, rather than failing mid-cascade on the first step that needs a missing key.
    Subclasses RuntimeError so existing CLI error handling catches it."""


class InMemoryRuntime:
    def __init__(
        self,
        manifest: Manifest,
        *,
        harness,
        sandboxes,
        secrets,
        bus,
        retry=None,
        state=None,
        tokens=None,
        max_iterations: int = 100_000,
    ):
        self.manifest = manifest
        self.harness = harness
        self.sandboxes = sandboxes
        self.secrets = secrets
        self.bus = bus
        # Optional SCM TokenProvider: when set, a fresh scoped token is minted and
        # injected into each sandbox's env (the App key stays at the control-plane).
        # None preserves the original behavior — no token, no extra network.
        self.tokens = tokens
        self.retry = retry or ExponentialBackoffRetry()
        self.state = state or InMemoryStateStore()
        # Backstop against an unbounded event loop. The *real* terminator is budgets
        # (cumulative spend / window), enforced with durability; this just stops a
        # runaway from spinning forever. Set high — legitimate loops run for many turns.
        self.max_iterations = max_iterations

        self._run_seq = 0
        self._event_seq = 0
        self._runs: dict[RunId, RunStatus] = {}
        self._queue: deque[tuple[str, Event]] = deque()  # pending (workflow, event) work
        self._work = asyncio.Event()  # signalled on enqueue; wakes the serve() consumer
        self._draining = False  # guard so concurrent drains don't race the shared queue
        # Observability for tests/CLI: global ordered logs across the cascade.
        self.execution_log: list[str] = []
        self.emitted_log: list[str] = []
        self.drain_errors: list[Exception] = []  # runs that failed under serve()

        # Subscribe a handler PER workflow so an event with multiple `on:` consumers
        # fans out to all of them (not just the first).
        for wf_name, wf in manifest.workflows.items():
            entry = wf.steps.get(wf.entry) if wf.entry else None
            if entry and entry.trigger and entry.trigger.kind == "event":
                self.bus.subscribe(entry.trigger.event, self._handler_for(wf_name))

    # ── Pre-flight ────────────────────────────────────────────────────────────────
    def preflight(self) -> None:
        """Fail-fast credential check (B6): before any step runs, verify every agent a
        step references can be authenticated — its sandbox must supply the provider keys
        its harness requires (`ANTHROPIC_API_KEY` for `claude-code`, etc.). Aggregates
        *all* missing keys across every agent into a single error so a misconfigured
        project is reported up front, in full, instead of dying mid-cascade on the first
        step that needs the key. The per-step check in `_run_step` remains as a backstop
        for paths that don't run preflight (direct `drain`/`tick`, tests)."""
        problems: list[str] = []
        seen: set[str] = set()
        for wf in self.manifest.workflows.values():
            for step in wf.steps.values():
                name = step.agent
                if not name or name in seen:
                    continue
                seen.add(name)
                agent = self.manifest.registry.agents.get(name)
                if agent is None:
                    continue  # an unresolvable agent is a compile-time error, not preflight's
                sandbox_name = (agent.sandbox or "default") or "default"
                spec = self.manifest.registry.sandboxes.get(sandbox_name) or SandboxSpec()
                secrets = self.secrets.resolve(sandbox_name, spec)
                missing = self.harness.required_keys(agent) - set(secrets)
                if missing:
                    problems.append(
                        f"agent '{name}' (sandbox '{sandbox_name}'): "
                        f"missing {', '.join(sorted(missing))}"
                    )
        if problems:
            raise PreflightError(
                "pre-flight failed — sandbox(es) cannot supply the harness's required "
                "provider key(s):\n  " + "\n  ".join(problems) + "\n\nAdd the missing "
                "key(s) to the sandbox's env_file (see ARCHITECTURE.md §6)."
            )

    # ── Runtime Protocol ────────────────────────────────────────────────────────
    async def trigger(self, event: Event) -> RunId | None:
        """Synchronous one-shot entry (for `loopy trigger` and tests): publish `event`,
        then drain the cascade to completion and return the first run started (or None if
        nothing subscribes). The server path uses the `EventReceiver` (publish only) plus
        `serve()` to drain in the background instead."""
        await self.bus.publish(event)  # handlers enqueue matching (workflow, event)
        return await self._drain()

    async def serve(self) -> None:
        """Background consumer for the long-lived server: drain whenever work is enqueued
        on the bus. A run that raises is recorded in `drain_errors` and skipped, so one
        bad event can't stop ingress. This is what decouples intake (the receiver just
        publishes) from execution."""
        while True:
            await self._work.wait()
            self._work.clear()
            try:
                await self._drain()
            except Exception as exc:  # noqa: BLE001 - a bad run must not kill the consumer
                # Per-run failures are handled in _execute; this catches drain-level faults
                # (e.g. the max_iterations backstop) so the consumer survives them.
                logger.exception("drain aborted; ingress continues")
                self.drain_errors.append(exc)

    async def drain(self) -> RunId | None:
        """Run all currently-queued work to completion; return the first run started.
        Exposed for tests and the synchronous path; `serve()` calls it on each wake."""
        return await self._drain()

    @property
    def failed_runs(self) -> list[RunStatus]:
        """Runs that ended in state 'failed' (each carries its `error`). For the
        one-shot CLI and observability — a failed run is recorded, not raised."""
        return [s for s in self._runs.values() if s.state == "failed"]

    async def tick(self, t: TriggerId, scheduled_at: datetime) -> RunId | None:
        """Fire a cron tick for trigger `t` (a workflow's `on: cron(...)` entry step id): build
        the tick event (`scheduled_at` + `last_run`) and instantiate a run rooted at that entry.

        Watermark (`last_run`) semantics mirror the poll path's cold start, but advance here
        rather than in the scheduler: a cron tick has no upstream fetch that can fail, so the
        tick *fired* the moment we build its event — we record that (advance to `scheduled_at`)
        before draining. A per-run failure is recorded in history (B12), not a reason to re-fire
        the same instant; re-scan-on-failure is durable-retry territory (B9/B10), deferred.

        Returns the run started, or None if no workflow has a cron entry with id `t`. In the
        server, the background `serve()` consumer may win the drain (the guard makes this a
        no-op that returns None) — the run still executes and the watermark already advanced."""
        match = self.manifest.workflow_for_cron(t)
        if match is None:
            return None
        wf_name, entry = match
        last_run = await self.state.get_watermark(t)
        if last_run is None and entry.trigger and entry.trigger.expr:  # cold start: one window back
            last_run = cron_prev(entry.trigger.expr, scheduled_at, entry.trigger.tz)
        self._event_seq += 1
        event = Event(
            name=f"cron:{t}",
            fields={"scheduled_at": scheduled_at, "last_run": last_run},
            id=f"tick-{t}-{scheduled_at.isoformat()}",
            emitted_at=scheduled_at,
        )
        await self.state.set_watermark(t, scheduled_at)  # the tick fired
        self._queue.append((wf_name, event))
        self._work.set()
        return await self._drain()

    async def resume(self, run_id):  # pragma: no cover - durability deferred (B10)
        raise NotImplementedError("resume requires a durable StateStore (B10)")

    async def status(self, run_id: RunId) -> RunStatus:
        return self._runs[run_id]

    # ── Internals ──────────────────────────────────────────────────────────────
    def _handler_for(self, wf_name: str) -> Callable[[Event], Awaitable[None]]:
        async def handler(event: Event) -> None:
            self._queue.append((wf_name, event))  # enqueue; the drain loop runs it
            self._work.set()  # wake serve() if it's the one draining

        return handler

    async def _drain(self) -> RunId | None:
        """Run queued (workflow, event) work until the queue empties. Each run's emits
        enqueue more work via the bus; depth stays flat (one run on the stack at a time).

        Guarded so concurrent callers don't race the shared queue: if a drain is already
        in flight, this no-ops and returns None — that drain's `while self._queue` loop
        will pick up the newly-enqueued work (safe because no `await` sits between the
        loop's empty-check and clearing the guard)."""
        if self._draining:
            return None
        self._draining = True
        first_run_id: RunId | None = None
        iterations = 0
        try:
            while self._queue:
                iterations += 1
                if iterations > self.max_iterations:
                    raise RuntimeError(
                        f"cascade exceeded max_iterations ({self.max_iterations}) — "
                        "possible unbounded event loop with no terminating budget"
                    )
                wf_name, event = self._queue.popleft()
                run_id = await self._execute(wf_name, event)
                first_run_id = first_run_id or run_id
        finally:
            self._draining = False
        return first_run_id

    async def _execute(self, wf_name: str, event: Event) -> RunId:
        wf = self.manifest.workflows[wf_name]
        self._run_seq += 1
        run_id = f"{wf_name}-{self._run_seq}"
        await self.state.create_run(run_id, self.manifest.schema_version, event)
        await self.state.append(
            run_id, RunEvent("run_started", None, {"event": event.name}, _now())
        )

        outputs: dict[str, StepOutput] = {}  # keyed by local step name
        step_states: dict[str, str] = {}
        emitted: list[str] = []
        current_local: str | None = None  # local step name (key for step_states)
        current_step_id: str | None = None  # workflow-qualified id (for history/logs)

        try:
            for local_name in self._topo_order(wf):
                step = wf.steps[local_name]
                current_local = local_name
                current_step_id = step.id
                ctx = StepContext(
                    run_id=run_id,
                    step_id=step.id,
                    event=event,
                    upstream={p: outputs[p] for p in step.after if p in outputs},
                    idempotency_key=f"{run_id}:{step.id}",
                )
                result = await self._run_step(step, ctx)
                outputs[local_name] = result.output
                step_states[local_name] = "completed"
                self.execution_log.append(step.id)
                await self.state.record_output(run_id, local_name, result.output)
                await self.state.append(run_id, RunEvent("step_completed", step.id, {}, _now()))

                for event_name in step.emits:
                    payload = result.emits.get(event_name)
                    if payload is None:
                        raise RuntimeError(
                            f"step '{step.id}' declares emits '{event_name}' "
                            "but produced no payload"
                        )
                    emitted.append(event_name)
                    self.emitted_log.append(event_name)
                    await self.state.append(
                        run_id, RunEvent("event_emitted", step.id, {"event": event_name}, _now())
                    )
                    await self.bus.publish(self._build_emitted(event_name, payload))
        except Exception as exc:  # noqa: BLE001 - a failed run is a recorded outcome, not an
            # engine crash: record it, mark status, and let the drain continue to other runs.
            # (CancelledError is BaseException, so it is NOT caught here and still propagates.)
            if current_local is not None:
                step_states.setdefault(current_local, "failed")
            logger.warning("run %s failed at step %s: %s", run_id, current_step_id, exc)
            await self.state.append(
                run_id,
                RunEvent("run_failed", current_step_id, {"error": str(exc)}, _now()),
            )
            self._runs[run_id] = RunStatus(
                run_id=run_id,
                state="failed",
                step_states=step_states,
                emitted=tuple(emitted),
                error=str(exc),
            )
            return run_id

        await self.state.append(run_id, RunEvent("run_completed", None, {}, _now()))
        self._runs[run_id] = RunStatus(
            run_id=run_id,
            state="completed",
            step_states=step_states,
            emitted=tuple(emitted),
        )
        return run_id

    async def _run_step(self, step: StepSpec, ctx: StepContext) -> StepResult:
        agent = self.manifest.registry.agents.get(step.agent) if step.agent else None
        sandbox_name = (agent.sandbox if agent and agent.sandbox else "default") or "default"
        spec = self.manifest.registry.sandboxes.get(sandbox_name) or SandboxSpec()
        secrets = dict(self.secrets.resolve(sandbox_name, spec))

        # Provider-key rule: the sandbox must supply whatever keys the harness needs — unless
        # the harness can satisfy them another way (e.g. Claude Code OAuth creds via HOME).
        if agent is not None:
            missing = self.harness.missing_keys(agent, secrets)
            if missing:
                keys = ", ".join(sorted(missing))
                raise RuntimeError(
                    f"sandbox '{sandbox_name}' provides no {keys} required by agent "
                    f"'{step.agent}'. Add {keys} to the sandbox's env_file, or authenticate the "
                    f"CLI so its OAuth credentials are reachable via the sandbox HOME."
                )
            # Compose the harness's toolchain into the effective image so the CLI/runtime it
            # shells out to is present by construction — the sandbox spec itself stays
            # harness-agnostic (#16). No-op for harnesses with an empty toolchain.
            image = compose_image(spec.image, self.harness.toolchain(agent))
            spec = spec.model_copy(update={"image": image})

        # Mint + inject scoped SCM creds last, so they win over any env_file key and so
        # the ephemeral token is freshly minted for this step (the App key never enters).
        if self.tokens is not None:
            secrets.update(await self.tokens.token_env(spec))

        sandbox = await self.sandboxes.acquire(spec, secrets)
        try:
            if agent is not None:
                await self._verify_toolchain(sandbox, agent, step.agent, sandbox_name)
            attempt = 0
            while True:
                try:
                    return await self.harness.run(step, ctx, sandbox)
                except BudgetExceeded:
                    raise  # budget trips are terminal, not retried
                except Exception as exc:  # noqa: BLE001 - RetryPolicy decides
                    delay = self.retry.next_backoff(attempt, exc)
                    if delay is None:
                        raise
                    attempt += 1
                    if delay.total_seconds() > 0:
                        await asyncio.sleep(delay.total_seconds())
        finally:
            await sandbox.release()

    async def _verify_toolchain(self, sandbox, agent, agent_name, sandbox_name: str) -> None:
        """Runtime validation (#16): probe the live sandbox for the harness's required
        binaries before the agent runs, turning a cryptic mid-run `command not found` into an
        actionable up-front error. Catches an image/override that left the sandbox ill-equipped
        even after toolchain composition (e.g. a `snapshot:` that doesn't bundle the CLI, or the
        bare `local` provider with the tool absent from the host)."""
        tools = sorted(self.harness.required_tools(agent))
        if not tools:
            return
        # One probe: print each tool that isn't resolvable on PATH. `command -v` is a POSIX
        # shell builtin (no dependency on `which`), so this works across local/docker/daytona.
        script = "; ".join(f'command -v "{t}" >/dev/null 2>&1 || echo "{t}"' for t in tools)
        try:
            result = await sandbox.exec(["sh", "-c", script])
            missing = [t for t in result.stdout.split() if t in set(tools)]
        except Exception:
            # Couldn't even run the probe (e.g. the bare `local` provider with an empty env, so
            # `sh` isn't resolvable) — treat the whole toolchain as unverifiable/missing.
            missing = tools
        if missing:
            raise RuntimeError(
                f"sandbox '{sandbox_name}' is missing tool(s) {', '.join(missing)} required by "
                f"agent '{agent_name}'. Add them to the sandbox image (e.g. `apt:`/`run:` "
                "layers), use a snapshot/base image that bundles them, or — for `--sandbox local` "
                "— install them on the host and ensure PATH reaches them."
            )

    def _build_emitted(self, event_name: str, payload: Mapping[str, object]) -> Event:
        # The agent produced this payload (decision #3 = B); validate it against the
        # registered contract's top-level fields.
        contract = self.manifest.registry.events.get(event_name)
        if contract is not None:
            missing = set(contract.fields) - set(payload)
            if missing:
                raise RuntimeError(
                    f"emitted event '{event_name}' is missing fields {sorted(missing)}"
                )
        self._event_seq += 1
        return Event(
            name=event_name,
            fields=dict(payload),
            id=f"evt-{self._event_seq}",
            emitted_at=_now(),
        )

    @staticmethod
    def _topo_order(wf: WorkflowSpec) -> list[str]:
        steps = wf.steps
        indegree = {name: 0 for name in steps}
        adjacency: dict[str, list[str]] = {name: [] for name in steps}
        for name, step in steps.items():
            for pred in step.after:
                if pred in steps:
                    adjacency[pred].append(name)
                    indegree[name] += 1
        queue = deque(sorted(n for n, d in indegree.items() if d == 0))
        order: list[str] = []
        while queue:
            node = queue.popleft()
            order.append(node)
            for nxt in sorted(adjacency[node]):
                indegree[nxt] -= 1
                if indegree[nxt] == 0:
                    queue.append(nxt)
        return order
