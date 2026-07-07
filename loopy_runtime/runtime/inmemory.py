"""InMemoryRuntime (B1–B6) — non-durable, single-process engine.

Owns ingress and orchestration: subscribes workflows to the bus, queues (workflow, event)
work, and drains cascades under the iteration backstop and dollar caps. A run's `emits:`
come back through the bus into this queue — the cross-workflow cascade, incl. loop-backs
and event fan-out. Actually running a workflow instance — the `after:` DAG-walk, sandbox,
harness, emits — lives in `WorkflowRunner` (workflow_runner.py): this class decides *when*
runs happen, the runner decides *how*.

Out of scope (v1): durable timers, crash-recoverable `resume`, version pinning.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime

from loopy_runtime.budget import CascadeBudget
from loopy_runtime.contract import Event, RunId, RunStatus, TriggerId
from loopy_runtime.manifest_model import Manifest, SandboxSpec
from loopy_runtime.providers import provider
from loopy_runtime.retry import ExponentialBackoffRetry
from loopy_runtime.runtime.workflow_runner import WorkflowRunner
from loopy_runtime.sensors.scheduler import cron_prev
from loopy_runtime.state.inmemory import InMemoryStateStore

logger = logging.getLogger(__name__)


def _sandbox_needs_github(spec: SandboxSpec) -> bool:
    """A sandbox needs GitHub auth when it declares `repos:` to clone — private repos can't
    clone without a token. Derived from the spec (not a separate flag) so the requirement
    stays in lockstep with `repos:` and can't drift. A push-to-an-unlisted-repo case would
    need an explicit sandbox knob; deferred until a workflow actually requires it."""
    return bool(spec.repos)


def _agent_reports_cost(agent) -> bool:  # noqa: ANN001 - AgentSpec
    """Whether `agent`'s harness surfaces a USD cost the runtime can accumulate toward a
    dollar cap. Static, from the agent→harness→provider map; an unknown runtime can't report
    cost (and is a compile-time error anyway)."""
    try:
        return provider(agent.harness).reports_cost
    except ValueError:
        return False


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
        github_auth_hint: str | None = None,
        max_iterations: int = 100_000,
        cascade_budget_usd: float | None = None,
    ):
        self.manifest = manifest
        self.harness = harness
        self.sandboxes = sandboxes
        self.secrets = secrets
        self.bus = bus
        self.tokens = tokens
        # Where the control-plane creds were looked for (e.g. `<root>/loopy.env`), shown in
        # the GitHub-auth preflight error so a mispointed --root is self-diagnosing.
        self._github_auth_hint = github_auth_hint
        self.state = state or InMemoryStateStore()
        # Backstop against an unbounded event loop. A count, not a budget — it just stops a
        # runaway from spinning forever (set high; legitimate loops run for many turns). The
        # money-aware terminator is the cascade budget below.
        self.max_iterations = max_iterations
        # The money-aware terminator for a runaway loop-back cascade: the project-wide
        # `cascade_budget_usd` plus the per-named-workflow caps from registry
        # `limits.workflows`. None (and no workflow caps) disables it. Accounting and
        # rationale live in `CascadeBudget`; the runner checks it per step, `_drain` resets
        # it per cascade.
        limits = manifest.registry.limits
        workflow_caps: dict[str, float] = {}
        if limits is not None:
            for wf_name, wl in limits.workflows.items():
                if wl.spend and wl.spend.get("usd") is not None:
                    workflow_caps[wf_name] = wl.spend["usd"]
        self.budget = CascadeBudget(cascade_budget_usd, workflow_caps)

        # The piece that runs one workflow instance (DAG-walk, sandbox, harness, emits).
        self.runner = WorkflowRunner(
            manifest,
            harness=harness,
            sandboxes=sandboxes,
            secrets=secrets,
            bus=bus,
            # The retry policy has exactly one consumer (the runner's step loop), so it is
            # handed over rather than kept as a second reference here.
            retry=retry or ExponentialBackoffRetry(),
            state=self.state,
            tokens=tokens,
            budget=self.budget,
        )

        self._runs: dict[RunId, RunStatus] = {}
        self._queue: deque[tuple[str, Event]] = deque()  # pending (workflow, event) work
        self._work = asyncio.Event()  # signalled on enqueue; wakes the serve() consumer
        self._draining = False  # guard so concurrent drains don't race the shared queue
        self.drain_errors: list[Exception] = []  # runs that failed under serve()

        # Subscribe a handler PER workflow so an event with multiple `on:` consumers
        # fans out to all of them (not just the first).
        for wf_name, wf in manifest.workflows.items():
            entry = wf.steps.get(wf.entry) if wf.entry else None
            if entry and entry.trigger and entry.trigger.kind == "event":
                self.bus.subscribe(entry.trigger.event, self._handler_for(wf_name, entry.trigger))

    # ── Read-only views for tests/CLI over runner- and budget-owned state ────────
    @property
    def execution_log(self) -> list[str]:
        """Global ordered step ids across the cascade; owned by the runner."""
        return self.runner.execution_log

    @property
    def emitted_log(self) -> list[str]:
        """Global ordered emitted event names across the cascade; owned by the runner."""
        return self.runner.emitted_log

    @property
    def cascade_budget_usd(self) -> float | None:
        """The project-wide cascade USD cap (None disables); owned by the budget."""
        return self.budget.cascade_cap

    # ── Pre-flight ────────────────────────────────────────────────────────────────
    def preflight(self) -> None:
        """Fail-fast credential check (B6): before any step runs, verify every agent a
        step references can be authenticated — its sandbox must supply the provider keys
        its harness requires (`ANTHROPIC_API_KEY` for `claude-code`, etc.). Aggregates
        *all* missing keys across every agent into a single error so a misconfigured
        project is reported up front, in full, instead of dying mid-cascade on the first
        step that needs the key. The per-step check in the runner remains as a backstop
        for paths that don't run preflight (direct `drain`/`tick`, tests).

        When a dollar cap is active (registry `limits.cascade_spend`, or a per-workflow
        `limits.workflows.<name>.spend`), also enforce the all-or-nothing cost-capability gate
        here: every agent reachable under a cap must use a harness that reports USD cost
        (`reports_cost`), because one cost-blind step makes its spend invisible and lets a runaway
        slip the cap. Rejected up front with the offending agents named, before anything runs."""
        # Agents that run under some spend cap: all of them when the cascade cap is set, plus any
        # agent reachable inside a capped workflow. Precomputed across all workflows so an agent
        # shared between a capped and an uncapped workflow is still caught.
        agents_under_cap: set[str] = set()
        for wf_name, wf in self.manifest.workflows.items():
            if self.budget.applies_to(wf_name):
                agents_under_cap.update(s.agent for s in wf.steps.values() if s.agent)

        problems: list[str] = []
        github_sandboxes: set[str] = set()
        cost_blind: list[str] = []  # capped agents on a harness that reports no USD cost
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
                if _sandbox_needs_github(spec):
                    github_sandboxes.add(sandbox_name)
                if name in agents_under_cap and not _agent_reports_cost(agent):
                    cost_blind.append(name)

        sections: list[str] = []
        if cost_blind:
            sections.append(
                f"a spend cap (registry limits.cascade_spend / limits.workflows) is set but "
                f"agent(s) {', '.join(sorted(cost_blind))} use a harness that reports no USD cost, "
                "so the cap can't be enforced (one cost-blind step makes its spend invisible).\n  "
                "→ move those agents to a cost-reporting harness (e.g. claude-code), or remove the "
                "cap from registry.yml."
            )
        if problems:
            sections.append(
                "sandbox(es) cannot supply the harness's required provider key(s):\n  "
                + "\n  ".join(problems)
                + "\n  → add the missing key(s) to the sandbox's env_file."
            )
        github_problem = self._github_preflight_problem(github_sandboxes)
        if github_problem:
            sections.append(github_problem)
        if sections:
            raise PreflightError("pre-flight failed —\n" + "\n".join(sections))

    def _github_preflight_problem(self, sandboxes: set[str]) -> str | None:
        """Verify GitHub auth for the sandboxes that need it (those cloning `repos:`), or
        return a describing string when it's missing/unmintable. One mint covers all of
        them: today's token is installation-scoped (the repos chosen at install time), not
        per-sandbox — so this proves auth *exists and mints*, not that it reaches each
        specific repo (per-sandbox scoping is deferred)."""
        if not sandboxes:
            return None
        names = ", ".join(sorted(sandboxes))
        if self.tokens is None:
            where = (
                f" in {self._github_auth_hint} or the environment"
                if self._github_auth_hint
                else ""
            )
            return (
                f"sandbox(es) {names} clone repos but no GitHub auth is configured "
                f"(no GITHUB_APP_ID found{where}).\n"
                "  → run `loopy auth github` from the project dir to set up a GitHub App,\n"
                "    or put GITHUB_TOKEN in the sandbox's env_file."
            )
        try:
            asyncio.run(self.tokens.preflight())
        except Exception as exc:  # noqa: BLE001 - surface any mint failure as a preflight problem
            return (
                f"GitHub auth required by sandbox(es) {names} but unusable: {exc}\n"
                "  → check the GitHub App credentials and that it's installed on your repos."
            )
        return None

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
                # Per-run failures are handled in the runner; this catches drain-level faults
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
    def _handler_for(self, wf_name: str, trigger) -> Callable[[Event], Awaitable[None]]:  # noqa: ANN001 - TriggerSpec
        async def handler(event: Event) -> None:
            if not trigger.matches(event.fields):
                # A logged skip, not a silent drop — a filter that never matches is the
                # kind of gap that's miserable to debug from the outside.
                logger.info(
                    "event %s (%s) skipped for workflow %s: trigger filters %r not matched",
                    event.name,
                    event.id,
                    wf_name,
                    trigger.filters,
                )
                return
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
        # One `_drain()` is one cascade in practice (a step's `emits` enqueues the next run
        # into this same loop), so reset the cumulative-cost accumulators here. Caveat: under
        # serve(), two unrelated events that share a drain share the counter — that only trips
        # the cap *earlier* (safe); precise per-cascade-id scoping is a follow-up.
        self.budget.reset()
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
                status = await self.runner.run(wf_name, event)
                self._runs[status.run_id] = status
                first_run_id = first_run_id or status.run_id
        finally:
            self._draining = False
        return first_run_id
