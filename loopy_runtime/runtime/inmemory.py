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
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from loopy_runtime.budget import BudgetExceeded
from loopy_runtime.contract import (
    Event,
    RunEvent,
    RunId,
    RunStatus,
    StepContext,
    StepOutput,
)
from loopy_runtime.manifest_model import Manifest, SandboxSpec, StepSpec, WorkflowSpec
from loopy_runtime.payloads import synthesize_fields
from loopy_runtime.retry import ExponentialBackoffRetry
from loopy_runtime.state.inmemory import InMemoryStateStore


def _now() -> datetime:
    return datetime.now(UTC)


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
    ):
        self.manifest = manifest
        self.harness = harness
        self.sandboxes = sandboxes
        self.secrets = secrets
        self.bus = bus
        self.retry = retry or ExponentialBackoffRetry()
        self.state = state or InMemoryStateStore()

        self._run_seq = 0
        self._event_seq = 0
        self._runs: dict[RunId, RunStatus] = {}
        self._run_ids: list[RunId] = []  # creation order, for trigger()'s return
        # Observability for tests/CLI: global ordered logs across the cascade.
        self.execution_log: list[str] = []
        self.emitted_log: list[str] = []

        # Subscribe a handler PER workflow so an event with multiple `on:` consumers
        # fans out to all of them (not just the first).
        for wf_name, wf in manifest.workflows.items():
            entry = wf.steps.get(wf.entry) if wf.entry else None
            if entry and entry.trigger and entry.trigger.kind == "event":
                self.bus.subscribe(entry.trigger.event, self._handler_for(wf_name))

    # ── Runtime Protocol ────────────────────────────────────────────────────────
    async def trigger(self, event: Event) -> RunId | None:
        """Route `event` through the bus; return the first run it started (or None)."""
        before = len(self._run_ids)
        await self.bus.publish(event)
        started = self._run_ids[before:]
        return started[0] if started else None

    async def tick(self, t, scheduled_at):  # pragma: no cover - cron is deferred (B7/B8)
        raise NotImplementedError("cron ticks land with durable timers (B7/B8)")

    async def resume(self, run_id):  # pragma: no cover - durability deferred (B10)
        raise NotImplementedError("resume requires a durable StateStore (B10)")

    async def status(self, run_id: RunId) -> RunStatus:
        return self._runs[run_id]

    # ── Internals ──────────────────────────────────────────────────────────────
    def _handler_for(self, wf_name: str) -> Callable[[Event], Awaitable[None]]:
        async def handler(event: Event) -> None:
            await self._execute(wf_name, event)

        return handler

    async def _execute(self, wf_name: str, event: Event) -> RunId:
        wf = self.manifest.workflows[wf_name]
        self._run_seq += 1
        run_id = f"{wf_name}-{self._run_seq}"
        self._run_ids.append(run_id)
        await self.state.create_run(run_id, self.manifest.schema_version, event)
        await self.state.append(
            run_id, RunEvent("run_started", None, {"event": event.name}, _now())
        )

        outputs: dict[str, StepOutput] = {}  # keyed by local step name
        step_states: dict[str, str] = {}
        emitted: list[str] = []

        for local_name in self._topo_order(wf):
            step = wf.steps[local_name]
            ctx = StepContext(
                run_id=run_id,
                step_id=step.id,
                event=event,
                upstream={p: outputs[p] for p in step.after if p in outputs},
                idempotency_key=f"{run_id}:{step.id}",
            )
            output = await self._run_step(step, ctx)
            outputs[local_name] = output
            step_states[local_name] = "completed"
            self.execution_log.append(step.id)
            await self.state.record_output(run_id, local_name, output)
            await self.state.append(run_id, RunEvent("step_completed", step.id, {}, _now()))

            for event_name in step.emits:
                emitted.append(event_name)
                self.emitted_log.append(event_name)
                await self.state.append(
                    run_id, RunEvent("event_emitted", step.id, {"event": event_name}, _now())
                )
                await self.bus.publish(self._build_emitted(event_name, output))

        await self.state.append(run_id, RunEvent("run_completed", None, {}, _now()))
        self._runs[run_id] = RunStatus(
            run_id=run_id,
            state="completed",
            step_states=step_states,
            emitted=tuple(emitted),
        )
        return run_id

    async def _run_step(self, step: StepSpec, ctx: StepContext) -> StepOutput:
        agent = self.manifest.registry.agents.get(step.agent) if step.agent else None
        sandbox_name = (agent.sandbox if agent and agent.sandbox else "default") or "default"
        spec = self.manifest.registry.sandboxes.get(sandbox_name) or SandboxSpec()
        secrets = self.secrets.resolve(sandbox_name, spec)

        # Provider-key rule: the sandbox must supply whatever keys the harness needs.
        if agent is not None:
            missing = self.harness.required_keys(agent) - set(secrets)
            if missing:
                raise RuntimeError(
                    f"sandbox '{sandbox_name}' provides no {', '.join(sorted(missing))} "
                    f"required by agent '{step.agent}'"
                )

        sandbox = await self.sandboxes.acquire(spec, secrets)
        try:
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

    def _build_emitted(self, event_name: str, source: StepOutput) -> Event:
        contract = self.manifest.registry.events.get(event_name)
        fields = (
            synthesize_fields(contract.fields, source.fields) if contract else dict(source.fields)
        )
        self._event_seq += 1
        return Event(
            name=event_name,
            fields=fields,
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
