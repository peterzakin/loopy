"""B6 cumulative cascade spend cap (v1: token-based).

The real terminator for a runaway loop-back cascade: each step stays within its per-step
budget while cumulative cost grows unbounded — `max_iterations` is a count, not a budget.
`cascade_token_budget` caps the sum of tokens consumed across one cascade (reset per drain),
raising CascadeBudgetExceeded before a step once the cap is reached. See the cost-budget plan.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from loopy_runtime.bus.inproc import InProcessEventBus
from loopy_runtime.contract import Event, StepOutput, StepResult, Usage
from loopy_runtime.manifest_model import Manifest
from loopy_runtime.runtime.inmemory import InMemoryRuntime
from loopy_runtime.sandbox.local import LocalSandboxProvider
from loopy_runtime.secrets import StaticSecretsResolver


class TokenHarness:
    """A stub that reports a fixed token spend per step (and re-emits its trigger so the
    cascade loops), so the token cap — not max_iterations — is what stops it."""

    def __init__(self, tokens_per_step: int):
        self.tokens_per_step = tokens_per_step
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
            usage=Usage(input_tokens=self.tokens_per_step, output_tokens=0),
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
