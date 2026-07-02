"""B6 cumulative cascade spend cap (USD).

The real terminator for a runaway loop-back cascade: each step stays within its per-step
budget while cumulative cost grows unbounded — `max_iterations` is a count, not a budget.
`cascade_budget_usd` caps the sum of USD spent across one cascade (reset per drain), raising
CascadeBudgetExceeded before a step once the cap is reached. Only enforceable when every
reachable agent uses a cost-reporting harness (gated at preflight); a `cost_usd is None`
under an active cap is a recorded failure, never counted as $0. See the cost-budget plan.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from loopy_runtime.bus.inproc import InProcessEventBus
from loopy_runtime.contract import Event, StepOutput, StepResult, Usage
from loopy_runtime.manifest_model import Manifest
from loopy_runtime.runtime.inmemory import InMemoryRuntime, PreflightError
from loopy_runtime.sandbox.local import LocalSandboxProvider
from loopy_runtime.secrets import StaticSecretsResolver
from tests.stub_harness import StubAgentHarness


class CostHarness:
    """A stub that reports a fixed USD cost per step (and re-emits its trigger so the cascade
    loops), so the dollar cap — not max_iterations — is what stops it.

    `cost_per_step=None` mimics a cost-blind harness (codex); a number mimics a cost-reporting
    one (claude's total_cost_usd)."""

    def __init__(self, cost_per_step: float | None = None):
        self.cost_per_step = cost_per_step
        self.calls = 0

    def required_keys(self, agent):
        return set()

    def missing_keys(self, agent, env):
        return set()

    async def run(self, step, ctx, sandbox):
        self.calls += 1
        emits = {ev: {} for ev in step.emits}
        return StepResult(
            output=StepOutput({}),
            emits=emits,
            usage=Usage(cost_usd=self.cost_per_step),
        )


def _loop_step(emits: list[str]) -> dict:
    return {
        "id": "loop/tick",
        "trigger": {"kind": "event", "event": "Tick"},
        "after": [],
        "agent": None,
        "output": {},
        "emits": emits,
        "budget": None,
        "body": "spin",
        "refs": [],
    }


def _loop_manifest(emits: list[str], *, limits: dict | None = None) -> Manifest:
    registry = {"sandboxes": {}, "agents": {}, "events": {"Tick": {"fields": {}}}}
    if limits is not None:
        registry["limits"] = limits
    return Manifest.model_validate(
        {
            "schema_version": "2",
            "registry": registry,
            "workflows": {"loop": {"entry": "tick", "steps": {"tick": _loop_step(emits)}}},
            "sensors": [],
            "lineage": {"events": {}},
        }
    )


def _tick() -> Event:
    return Event(name="Tick", fields={}, id="t", emitted_at=datetime.now(UTC))


def _runtime(manifest: Manifest, harness, **kw) -> InMemoryRuntime:
    return InMemoryRuntime(
        manifest,
        harness=harness,
        sandboxes=LocalSandboxProvider(),
        secrets=StaticSecretsResolver({}),
        bus=InProcessEventBus(),
        **kw,
    )


# --- dollar cap (registry limits.cascade_spend) -------------------------------


def test_looping_cascade_trips_dollar_cap_not_iteration_cap():
    # Each step costs $0.10 and re-emits Tick. A $0.25 cap winds the cascade down after 3
    # steps (the 4th, at $0.30 ≥ $0.25, is short-circuited before running) — well under the
    # high max_iterations, so it's the *spend* cap, not the iteration backstop, that stops it.
    harness = CostHarness(cost_per_step=0.10)
    rt = _runtime(
        _loop_manifest(["Tick"]), harness, max_iterations=10_000, cascade_budget_usd=0.25
    )
    asyncio.run(rt.trigger(_tick()))
    assert harness.calls == 3
    failed = rt.failed_runs
    assert failed and "reaching the cap of $0.25" in (failed[-1].error or "")


def test_under_budget_cascade_completes():
    harness = CostHarness(cost_per_step=0.10)
    rt = _runtime(_loop_manifest([]), harness, cascade_budget_usd=1.00)  # no re-emit → 1 run
    run_id = asyncio.run(rt.trigger(_tick()))
    assert harness.calls == 1
    assert rt.failed_runs == []
    assert asyncio.run(rt.status(run_id)).state == "completed"


def test_no_cap_lets_cost_accumulate_freely():
    # cascade_budget_usd=None (the default) disables the cap: a finite cascade runs to
    # completion regardless of cost spent.
    harness = CostHarness(cost_per_step=999.0)
    rt = _runtime(_loop_manifest([]), harness)
    asyncio.run(rt.trigger(_tick()))
    assert harness.calls == 1
    assert rt.failed_runs == []


def test_accumulator_resets_between_drains():
    # Each `trigger` is its own drain (cascade); the counter resets, so a second trigger gets
    # the full budget again rather than starting already over-cap.
    harness = CostHarness(cost_per_step=0.10)
    rt = _runtime(_loop_manifest([]), harness, cascade_budget_usd=0.15)
    asyncio.run(rt.trigger(_tick()))
    asyncio.run(rt.trigger(_tick()))
    assert harness.calls == 2
    assert rt.failed_runs == []


def test_none_cost_under_active_cap_is_recorded_failure():
    # A cost-blind call under an active dollar cap is NEVER counted as $0 — it's a recorded
    # run failure (the "never silently no-op" guard), backstopping the static preflight gate.
    harness = CostHarness(cost_per_step=None)
    rt = _runtime(_loop_manifest([]), harness, cascade_budget_usd=1.00)
    asyncio.run(rt.trigger(_tick()))
    failed = rt.failed_runs
    assert failed and "no USD cost for this call" in (failed[-1].error or "")


def test_none_cost_is_fine_without_a_cap():
    # No dollar cap → cost_usd None (codex) never trips anything; the run completes.
    harness = CostHarness(cost_per_step=None)
    rt = _runtime(_loop_manifest([]), harness)
    run_id = asyncio.run(rt.trigger(_tick()))
    assert rt.failed_runs == []
    assert asyncio.run(rt.status(run_id)).state == "completed"


# --- per-workflow cap (registry limits.workflows) -----------------------------


def test_per_workflow_cap_winds_down_its_workflow():
    # The `loop` workflow has its own $0.25 cap and no project cascade cap. Same winding-down
    # as the cascade cap, but scoped to and reported for the one workflow.
    harness = CostHarness(cost_per_step=0.10)
    manifest = _loop_manifest(["Tick"], limits={"workflows": {"loop": {"spend": {"usd": 0.25}}}})
    rt = _runtime(manifest, harness, max_iterations=10_000)
    asyncio.run(rt.trigger(_tick()))
    assert harness.calls == 3  # 4th ($0.30 ≥ $0.25) short-circuited
    failed = rt.failed_runs
    assert failed and "workflow 'loop'" in (failed[-1].error or "")
    assert "reaching its cap of $0.25" in (failed[-1].error or "")


def test_per_workflow_cap_resets_between_drains():
    harness = CostHarness(cost_per_step=0.10)
    manifest = _loop_manifest([], limits={"workflows": {"loop": {"spend": {"usd": 0.15}}}})
    rt = _runtime(manifest, harness)
    asyncio.run(rt.trigger(_tick()))
    asyncio.run(rt.trigger(_tick()))
    assert harness.calls == 2  # each drain gets the full per-workflow budget again
    assert rt.failed_runs == []


# --- preflight gate: all reachable agents must report cost under the cap -------


def _agent_manifest(runtime: str, *, limits: dict | None = None) -> Manifest:
    step = {
        "id": "loop/tick",
        "trigger": {"kind": "event", "event": "Tick"},
        "after": [],
        "agent": "Coder",
        "output": {},
        "emits": [],
        "budget": None,
        "body": "go",
        "refs": [],
    }
    # Any model eligible for the harness under test; the cost gate keys on the harness only.
    model = {"codex": "gpt-5-codex"}.get(runtime, "claude-sonnet-4-6")
    registry = {
        "sandboxes": {},
        "agents": {"Coder": {"model": model, "harness": runtime}},
        "events": {"Tick": {"fields": {}}},
    }
    if limits is not None:
        registry["limits"] = limits
    return Manifest.model_validate(
        {
            "schema_version": "2",
            "registry": registry,
            "workflows": {"loop": {"entry": "tick", "steps": {"tick": step}}},
            "sensors": [],
            "lineage": {"events": {}},
        }
    )


def _preflight_runtime(manifest: Manifest, **kw) -> InMemoryRuntime:
    # StubAgentHarness keeps preflight focused on the cost gate (it requires no provider keys
    # and no toolchain, so neither the key check nor the GitHub check fires).
    return _runtime(manifest, StubAgentHarness(manifest.registry.events), **kw)


def test_preflight_rejects_dollar_cap_with_cost_blind_agent():
    # codex reports no USD cost → a cascade-wide dollar cap can't be enforced → rejected up
    # front, naming the offending agent.
    rt = _preflight_runtime(_agent_manifest("codex"), cascade_budget_usd=5.0)
    with pytest.raises(PreflightError) as exc:
        rt.preflight()
    assert "limits.cascade_spend" in str(exc.value)
    assert "Coder" in str(exc.value)


def test_preflight_allows_dollar_cap_with_cost_reporting_agent():
    # claude-code reports cost → the gate is satisfied, preflight is a no-op.
    rt = _preflight_runtime(_agent_manifest("claude-code"), cascade_budget_usd=5.0)
    rt.preflight()


def test_preflight_rejects_per_workflow_cap_with_cost_blind_agent():
    # A per-workflow cap also gates: the cost-blind agent inside the capped `loop` workflow is
    # refused up front, even with no project cascade cap set.
    manifest = _agent_manifest("codex", limits={"workflows": {"loop": {"spend": {"usd": 5.0}}}})
    rt = _preflight_runtime(manifest)
    with pytest.raises(PreflightError) as exc:
        rt.preflight()
    assert "Coder" in str(exc.value)


def test_preflight_ignores_cost_capability_without_a_cap():
    # No dollar cap → a cost-blind agent is perfectly fine.
    rt = _preflight_runtime(_agent_manifest("codex"))
    rt.preflight()
