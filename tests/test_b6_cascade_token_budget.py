"""B6 cumulative cascade spend cap (v1: token-based).

The real terminator for a runaway loop-back cascade: each step stays within its per-step
budget while cumulative cost grows unbounded — `max_iterations` is a count, not a budget.
`cascade_token_budget` caps the sum of tokens consumed across one cascade (reset per drain),
raising CascadeBudgetExceeded before a step once the cap is reached. See the cost-budget plan.
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


class TokenHarness:
    """A stub that reports a fixed token spend per step (and re-emits its trigger so the
    cascade loops), so the token cap — not max_iterations — is what stops it.

    `cost_per_step=None` mimics a cost-blind harness (codex); a number mimics a
    cost-reporting one (claude's total_cost_usd), for the dollar-cap tests."""

    def __init__(self, tokens_per_step: int = 0, cost_per_step: float | None = None):
        self.tokens_per_step = tokens_per_step
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
            usage=Usage(
                input_tokens=self.tokens_per_step,
                output_tokens=0,
                cost_usd=self.cost_per_step,
            ),
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


def _loop_manifest(emits: list[str]) -> Manifest:
    return Manifest.model_validate(
        {
            "schema_version": "1",
            "registry": {"sandboxes": {}, "agents": {}, "events": {"Tick": {"fields": {}}}},
            "workflows": {"loop": {"entry": "tick", "steps": {"tick": _loop_step(emits)}}},
            "sensors": [],
            "lineage": {"events": {}},
        }
    )


def _tick() -> Event:
    return Event(name="Tick", fields={}, id="t", emitted_at=datetime.now(UTC))


def test_looping_cascade_trips_token_cap_not_iteration_cap():
    # Each step spends 100 tokens and re-emits Tick. With a 250-token cap the cascade winds
    # down after 3 steps (the 4th is short-circuited before running) — well under the high
    # max_iterations, so it's the *token* cap, not the iteration backstop, that stops it.
    harness = TokenHarness(tokens_per_step=100)
    rt = InMemoryRuntime(
        _loop_manifest(["Tick"]),
        harness=harness,
        sandboxes=LocalSandboxProvider(),
        secrets=StaticSecretsResolver({}),
        bus=InProcessEventBus(),
        max_iterations=10_000,
        cascade_token_budget=250,
    )
    asyncio.run(rt.trigger(_tick()))
    # Steps 1–3 run (0/100/200 tokens are all < 250); step 4 sees 300 ≥ 250 and is refused.
    assert harness.calls == 3
    failed = rt.failed_runs
    assert failed and "reaching the cap of 250" in (failed[-1].error or "")


def test_under_budget_cascade_completes():
    # A non-looping run that spends under the cap finishes cleanly — no failed run.
    harness = TokenHarness(tokens_per_step=100)
    rt = InMemoryRuntime(
        _loop_manifest([]),  # no re-emit → single run, no cascade
        harness=harness,
        sandboxes=LocalSandboxProvider(),
        secrets=StaticSecretsResolver({}),
        bus=InProcessEventBus(),
        cascade_token_budget=1_000,
    )
    run_id = asyncio.run(rt.trigger(_tick()))
    assert harness.calls == 1
    assert rt.failed_runs == []
    assert asyncio.run(rt.status(run_id)).state == "completed"


def test_no_cap_lets_tokens_accumulate_freely():
    # cascade_token_budget=None (the default) disables the cap entirely: a finite cascade
    # runs to completion regardless of tokens spent.
    harness = TokenHarness(tokens_per_step=10_000)
    rt = InMemoryRuntime(
        _loop_manifest([]),
        harness=harness,
        sandboxes=LocalSandboxProvider(),
        secrets=StaticSecretsResolver({}),
        bus=InProcessEventBus(),
    )
    asyncio.run(rt.trigger(_tick()))
    assert harness.calls == 1
    assert rt.failed_runs == []


def test_accumulator_resets_between_drains():
    # Each `trigger` is its own drain (cascade); the counter resets, so a second trigger
    # gets the full budget again rather than starting already over-cap.
    harness = TokenHarness(tokens_per_step=100)
    rt = InMemoryRuntime(
        _loop_manifest([]),
        harness=harness,
        sandboxes=LocalSandboxProvider(),
        secrets=StaticSecretsResolver({}),
        bus=InProcessEventBus(),
        cascade_token_budget=150,
    )
    asyncio.run(rt.trigger(_tick()))
    asyncio.run(rt.trigger(_tick()))
    # Both single-step runs complete (100 < 150 each); the reset is what keeps the 2nd alive.
    assert harness.calls == 2
    assert rt.failed_runs == []


# --- dollar cap (--max-spend) -------------------------------------------------


def test_looping_cascade_trips_dollar_cap():
    # Each step costs $0.10 and re-emits Tick. A $0.25 cap winds the cascade down after 3
    # steps (the 4th, at $0.30 ≥ $0.25, is short-circuited before running).
    harness = TokenHarness(cost_per_step=0.10)
    rt = InMemoryRuntime(
        _loop_manifest(["Tick"]),
        harness=harness,
        sandboxes=LocalSandboxProvider(),
        secrets=StaticSecretsResolver({}),
        bus=InProcessEventBus(),
        max_iterations=10_000,
        cascade_budget_usd=0.25,
    )
    asyncio.run(rt.trigger(_tick()))
    assert harness.calls == 3
    failed = rt.failed_runs
    assert failed and "reaching the cap of $0.25" in (failed[-1].error or "")


def test_under_budget_dollar_cascade_completes():
    harness = TokenHarness(cost_per_step=0.10)
    rt = InMemoryRuntime(
        _loop_manifest([]),
        harness=harness,
        sandboxes=LocalSandboxProvider(),
        secrets=StaticSecretsResolver({}),
        bus=InProcessEventBus(),
        cascade_budget_usd=1.00,
    )
    run_id = asyncio.run(rt.trigger(_tick()))
    assert harness.calls == 1
    assert rt.failed_runs == []
    assert asyncio.run(rt.status(run_id)).state == "completed"


def test_none_cost_under_active_dollar_cap_is_recorded_failure():
    # A cost-blind call under an active dollar cap is NEVER counted as $0 — it's a recorded
    # run failure (the "never silently no-op" guard), backstopping the static preflight gate.
    harness = TokenHarness(cost_per_step=None)
    rt = InMemoryRuntime(
        _loop_manifest([]),
        harness=harness,
        sandboxes=LocalSandboxProvider(),
        secrets=StaticSecretsResolver({}),
        bus=InProcessEventBus(),
        cascade_budget_usd=1.00,
    )
    asyncio.run(rt.trigger(_tick()))
    failed = rt.failed_runs
    assert failed and "no USD cost for this call" in (failed[-1].error or "")


def test_none_cost_is_fine_without_a_dollar_cap():
    # No dollar cap → cost_usd None (codex) never trips anything; the run completes.
    harness = TokenHarness(cost_per_step=None)
    rt = InMemoryRuntime(
        _loop_manifest([]),
        harness=harness,
        sandboxes=LocalSandboxProvider(),
        secrets=StaticSecretsResolver({}),
        bus=InProcessEventBus(),
    )
    run_id = asyncio.run(rt.trigger(_tick()))
    assert rt.failed_runs == []
    assert asyncio.run(rt.status(run_id)).state == "completed"


# --- preflight gate: all reachable agents must report cost under --max-spend ---


def _agent_manifest(runtime: str) -> Manifest:
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
    return Manifest.model_validate(
        {
            "schema_version": "1",
            "registry": {
                "sandboxes": {},
                "agents": {"Coder": {"harness": {"runtime": runtime}}},
                "events": {"Tick": {"fields": {}}},
            },
            "workflows": {"loop": {"entry": "tick", "steps": {"tick": step}}},
            "sensors": [],
            "lineage": {"events": {}},
        }
    )


def _preflight_runtime(manifest: Manifest, **kw) -> InMemoryRuntime:
    # StubAgentHarness keeps preflight focused on the cost gate (it requires no provider keys
    # and no toolchain, so neither the key check nor the GitHub check fires).
    return InMemoryRuntime(
        manifest,
        harness=StubAgentHarness(manifest.registry.events),
        sandboxes=LocalSandboxProvider(),
        secrets=StaticSecretsResolver({}),
        bus=InProcessEventBus(),
        **kw,
    )


def test_preflight_rejects_dollar_cap_with_cost_blind_agent():
    # codex reports no USD cost → a cascade-wide dollar cap can't be enforced → rejected up
    # front, naming the offending agent and pointing at --max-tokens.
    rt = _preflight_runtime(_agent_manifest("codex"), cascade_budget_usd=5.0)
    with pytest.raises(PreflightError) as exc:
        rt.preflight()
    assert "--max-spend" in str(exc.value)
    assert "Coder" in str(exc.value)
    assert "--max-tokens" in str(exc.value)


def test_preflight_allows_dollar_cap_with_cost_reporting_agent():
    # claude-code reports cost → the gate is satisfied, preflight is a no-op.
    rt = _preflight_runtime(_agent_manifest("claude-code"), cascade_budget_usd=5.0)
    rt.preflight()


def test_preflight_ignores_cost_capability_without_a_dollar_cap():
    # No dollar cap → a cost-blind agent is perfectly fine (token cap still applies).
    rt = _preflight_runtime(_agent_manifest("codex"))
    rt.preflight()
