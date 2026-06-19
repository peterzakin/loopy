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
from loopy_runtime.manifest_model import (
    AgentSpec,
    BudgetSpec,
    EventContract,
    HarnessSpec,
    StepSpec,
)


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


AGENT = AgentSpec(harness=HarnessSpec(runtime="claude-code", model="claude-opus-4-8"))


def _harness():
    return ClaudeCodeHarness({"Fixer": AGENT})


def test_build_argv_has_expected_flags():
    step = StepSpec(id="w/s", agent="Fixer", body="do it")
    argv = _harness().build_argv(step, AGENT, "do it")
    assert argv[:2] == ["claude", "-p"]
    assert "--output-format" in argv and "json" in argv
    assert "--model" in argv and "claude-opus-4-8" in argv
    # `--allowed-tools` is an allowlist over Claude's built-in tools, not loopy capability
    # names — wiring it here would strip the agent's default toolset, so it is never passed.
    assert "--allowed-tools" not in argv
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"


def test_run_parses_typed_output():
    step = StepSpec(id="w/s", agent="Fixer", output={"goal": {"type": "string"}}, body="b")
    envelope = {
        "result": json.dumps({"output": {"goal": "ship it"}, "emits": {}}),
        "total_cost_usd": 0.01,
    }
    result = asyncio.run(_harness().run(step, _ctx(), CaptureSandbox(envelope)))
    assert result.output.fields == {"goal": "ship it"}
    assert result.emits == {}


def test_run_tolerates_fenced_json_result():
    # The agent wrapped its JSON in a ```json fence and added a sentence — still parses.
    step = StepSpec(id="w/s", agent="Fixer", output={"goal": {"type": "string"}}, body="b")
    message = 'Done!\n```json\n{"output": {"goal": "ship it"}, "emits": {}}\n```'
    envelope = {"result": message, "total_cost_usd": 0.0}
    result = asyncio.run(_harness().run(step, _ctx(), CaptureSandbox(envelope)))
    assert result.output.fields == {"goal": "ship it"}


def test_run_surfaces_offending_text_on_non_json_result():
    step = StepSpec(id="w/s", agent="Fixer", output={"goal": {"type": "string"}}, body="b")
    envelope = {"result": "I was unable to finish the task.", "total_cost_usd": 0.0}
    with pytest.raises(HarnessError, match="unable to finish"):
        asyncio.run(_harness().run(step, _ctx(), CaptureSandbox(envelope)))


def test_run_surfaces_stderr_on_nonzero_exit():
    class FailingSandbox:
        id = "fail"

        async def exec(self, cmd):
            return ExecResult(1, "", "boom: credentials rejected")

        async def release(self):
            pass

    step = StepSpec(id="w/s", agent="Fixer", output={"goal": {"type": "string"}}, body="b")
    with pytest.raises(HarnessError, match="credentials rejected"):
        asyncio.run(_harness().run(step, _ctx(), FailingSandbox()))


def test_run_produces_agent_emit_payload():
    # The agent generates the emitted event's fields (decision #3 = B).
    step = StepSpec(id="w/s", agent="Fixer", emits=["WorkItem"], body="b")
    events = {"WorkItem": EventContract(fields={"link": {"type": "string"}})}
    harness = ClaudeCodeHarness({"Fixer": AGENT}, events)
    envelope = {
        "result": json.dumps({"output": {}, "emits": {"WorkItem": {"link": "https://pr/1"}}}),
        "total_cost_usd": 0.0,
    }
    result = asyncio.run(harness.run(step, _ctx(), CaptureSandbox(envelope)))
    assert result.emits == {"WorkItem": {"link": "https://pr/1"}}


def test_run_errors_when_emit_payload_missing():
    step = StepSpec(id="w/s", agent="Fixer", emits=["WorkItem"], body="b")
    events = {"WorkItem": EventContract(fields={"link": {"type": "string"}})}
    harness = ClaudeCodeHarness({"Fixer": AGENT}, events)
    envelope = {"result": json.dumps({"output": {}, "emits": {}}), "total_cost_usd": 0.0}
    with pytest.raises(HarnessError):
        asyncio.run(harness.run(step, _ctx(), CaptureSandbox(envelope)))


def test_run_requires_model_key_declared():
    assert _harness().required_keys(AGENT) == {"ANTHROPIC_API_KEY"}


def test_missing_keys_demands_model_key_without_creds():
    # Keyed on the sandbox env only (not the control-plane HOME), so this is deterministic.
    assert _harness().missing_keys(AGENT, {}) == {"ANTHROPIC_API_KEY"}


def test_missing_keys_satisfied_by_api_key():
    assert _harness().missing_keys(AGENT, {"ANTHROPIC_API_KEY": "sk-x"}) == set()


def test_missing_keys_satisfied_by_oauth_credentials(tmp_path):
    creds = tmp_path / ".claude" / ".credentials.json"
    creds.parent.mkdir(parents=True)
    creds.write_text("{}")
    # No API key, but OAuth creds are reachable via the sandbox HOME → not missing.
    assert _harness().missing_keys(AGENT, {"HOME": str(tmp_path)}) == set()


def test_missing_keys_demands_key_when_home_has_no_credentials(tmp_path):
    assert _harness().missing_keys(AGENT, {"HOME": str(tmp_path)}) == {"ANTHROPIC_API_KEY"}


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
