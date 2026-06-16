"""B2 unit tests for ClaudeCodeHarness — argv construction + envelope parsing + budget.

A CaptureSandbox stands in for a real sandbox: it records the argv and returns a
canned `claude --output-format json` envelope. No real model/sandbox calls.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest

from loopy_runtime.budget import BudgetExceeded
from loopy_runtime.contract import Event, ExecResult, StepContext
from loopy_runtime.harness.claude_code import ClaudeCodeHarness, HarnessError
from loopy_runtime.manifest_model import AgentSpec, BudgetSpec, HarnessSpec, StepSpec


class CaptureSandbox:
    id = "capture"

    def __init__(self, envelope: dict, exit_code: int = 0):
        self.envelope = envelope
        self.exit_code = exit_code
        self.argv: list[str] | None = None

    async def exec(self, cmd: list[str]) -> ExecResult:
        self.argv = cmd
        return ExecResult(self.exit_code, json.dumps(self.envelope), "")

    async def release(self) -> None:
        pass


def _ctx():
    return StepContext(
        run_id="r1",
        step_id="w/s",
        event=Event(name="E", fields={}, id="x", emitted_at=datetime.now(UTC)),
        upstream={},
        idempotency_key="r1:w/s",
    )


AGENT = AgentSpec(
    harness=HarnessSpec(runtime="claude-code", model="claude-opus-4-8"), tools=["open_pr"]
)


def _harness():
    return ClaudeCodeHarness({"Fixer": AGENT})


def test_build_argv_has_expected_flags():
    step = StepSpec(id="w/s", agent="Fixer", body="do it")
    argv = _harness().build_argv(step, AGENT, "do it")
    assert argv[:2] == ["claude", "-p"]
    assert "--output-format" in argv and "json" in argv
    assert "--model" in argv and "claude-opus-4-8" in argv
    assert "--allowed-tools" in argv and "open_pr" in argv
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"


def test_run_parses_typed_output():
    step = StepSpec(id="w/s", agent="Fixer", output={"goal": {"type": "string"}}, body="b")
    sandbox = CaptureSandbox({"result": json.dumps({"goal": "ship it"}), "total_cost_usd": 0.01})
    out = asyncio.run(_harness().run(step, _ctx(), sandbox))
    assert out.fields == {"goal": "ship it"}
    assert sandbox.argv[0] == "claude"


def test_run_requires_model_key_declared():
    assert _harness().required_keys(AGENT) == {"ANTHROPIC_API_KEY"}


def test_run_trips_spend_budget():
    step = StepSpec(id="w/s", agent="Fixer", budget=BudgetSpec(spend={"usd": 1}), body="b")
    sandbox = CaptureSandbox({"result": "", "total_cost_usd": 5.0})
    with pytest.raises(BudgetExceeded):
        asyncio.run(_harness().run(step, _ctx(), sandbox))


def test_run_raises_on_nonzero_exit():
    step = StepSpec(id="w/s", agent="Fixer", body="b")
    sandbox = CaptureSandbox({"result": ""}, exit_code=1)
    with pytest.raises(HarnessError):
        asyncio.run(_harness().run(step, _ctx(), sandbox))
