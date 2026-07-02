"""WorkflowRunner — runs one workflow instance (a run) to completion.

Walks the `after:` DAG in topological order, builds each step's context, runs the step's
agent in a sandbox via the AgentHarness (with retry), validates and publishes `emits:` to
the bus, and records outputs plus an event-sourced history in the StateStore. The DAG-walk
is effect-free: the only effects are the harness, the bus, and the sandbox.

One runner serves the whole engine. Per-run state lives in `run()`'s locals; what the
runner itself holds is cascade-global (the observability logs and the emitted-event
counter). The cascade loop — queueing, drain, budget reset — stays in the runtime
(inmemory.py, whose docstring states the split).
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import uuid4

from loopy_runtime.budget import BudgetExceeded, CascadeBudget
from loopy_runtime.contract import (
    Event,
    RunEvent,
    RunStatus,
    StepContext,
    StepOutput,
    StepResult,
)
from loopy_runtime.manifest_model import Manifest, SandboxSpec, StepSpec, WorkflowSpec
from loopy_runtime.sandbox.toolchain import compose_image

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


class WorkflowRunner:
    def __init__(
        self,
        manifest: Manifest,
        *,
        harness,
        sandboxes,
        secrets,
        bus,
        retry,
        state,
        tokens,
        budget: CascadeBudget,
    ):
        self.manifest = manifest
        self.harness = harness
        self.sandboxes = sandboxes
        self.secrets = secrets
        self.bus = bus
        self.retry = retry
        self.state = state
        # Optional SCM TokenProvider: when set, a fresh scoped token is minted and
        # injected into each sandbox's env (the App key stays at the control-plane).
        # None preserves the original behavior — no token, no extra network.
        self.tokens = tokens
        self.budget = budget
        self._event_seq = 0
        # Observability for tests/CLI: global ordered logs across the cascade.
        self.execution_log: list[str] = []
        self.emitted_log: list[str] = []

    async def run(self, wf_name: str, event: Event) -> RunStatus:
        """Run one instance of workflow `wf_name` triggered by `event`, to completion or
        first failure. A failed run is a recorded outcome (returned with state 'failed' and
        the error), not a raise — the caller's drain loop continues to other runs."""
        wf = self.manifest.workflows[wf_name]
        # A short random token, not a per-process counter: each `loopy trigger` is a fresh
        # process, so a counter would restart at 1 and two unrelated runs would collide on the
        # same id (and any side effects an author namespaces by it). uuid keeps runs unique
        # across processes.
        run_id = f"{wf_name}-{uuid4().hex[:8]}"
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
                # Raises when a cap is already reached; caught below and recorded as a failed
                # run (composes with the run-failure path, no new plumbing).
                self.budget.check(wf_name, step.id)
                ctx = StepContext(
                    run_id=run_id,
                    step_id=step.id,
                    event=event,
                    upstream={p: outputs[p] for p in step.after if p in outputs},
                    idempotency_key=f"{run_id}:{step.id}",
                )
                result = await self._run_step(step, ctx)
                self.budget.add(wf_name, step.id, result.usage)
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
            # engine crash: record it and let the caller's drain continue to other runs.
            # (CancelledError is BaseException, so it is NOT caught here and still propagates.)
            if current_local is not None:
                step_states.setdefault(current_local, "failed")
            logger.warning("run %s failed at step %s: %s", run_id, current_step_id, exc)
            await self.state.append(
                run_id,
                RunEvent("run_failed", current_step_id, {"error": str(exc)}, _now()),
            )
            return RunStatus(
                run_id=run_id,
                state="failed",
                step_states=step_states,
                emitted=tuple(emitted),
                error=str(exc),
            )

        await self.state.append(run_id, RunEvent("run_completed", None, {}, _now()))
        return RunStatus(
            run_id=run_id,
            state="completed",
            step_states=step_states,
            emitted=tuple(emitted),
        )

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

        # Run identity for traceability: the agent (and any tooling it shells out to) can stamp
        # the run id onto what it produces — a PR body, a commit trailer, an uploaded artifact —
        # so an external side effect can be traced back to the run that made it. Engine-owned, so
        # it's set last and an env_file can't shadow it.
        secrets["LOOPY_RUN_ID"] = ctx.run_id

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
                "layers), use a snapshot/base image that bundles them, or — for a `local` provider "
                "sandbox — install them on the host and ensure PATH reaches them."
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
