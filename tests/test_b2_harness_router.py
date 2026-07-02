"""B2 unit tests for HarnessRouter — per-runtime dispatch + startup validation."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest

from loopy_runtime.contract import Event, ExecResult, StepContext
from loopy_runtime.harness.claude_code import ClaudeCodeHarness
from loopy_runtime.harness.codex import CodexHarness
from loopy_runtime.harness.opencode import OpenCodeHarness
from loopy_runtime.harness.router import HarnessError, HarnessRouter
from loopy_runtime.manifest_model import AgentSpec, HarnessSpec, StepSpec

CLAUDE_AGENT = AgentSpec(harness=HarnessSpec(runtime="claude-code", model="claude-opus-4-8"))
CODEX_AGENT = AgentSpec(harness=HarnessSpec(runtime="codex", model="gpt-5-codex"))
OPENCODE_AGENT = AgentSpec(
    harness=HarnessSpec(runtime="opencode", model="anthropic/claude-sonnet-4-6")
)
AGENTS = {"Fixer": CLAUDE_AGENT, "Coder": CODEX_AGENT, "Helper": OPENCODE_AGENT}


class CaptureSandbox:
    id = "capture"

    def __init__(self, stdout: str):
        self.stdout = stdout
        self.argv: list[str] | None = None

    async def exec(self, cmd: list[str]) -> ExecResult:
        self.argv = cmd
        return ExecResult(0, self.stdout, "")

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


def test_builds_one_harness_per_runtime_used():
    router = HarnessRouter(AGENTS)
    assert isinstance(router._harnesses["claude-code"], ClaudeCodeHarness)
    assert isinstance(router._harnesses["codex"], CodexHarness)
    assert isinstance(router._harnesses["opencode"], OpenCodeHarness)


def test_required_keys_route_by_runtime():
    router = HarnessRouter(AGENTS)
    assert router.required_keys(CLAUDE_AGENT) == {"ANTHROPIC_API_KEY"}
    assert router.required_keys(CODEX_AGENT) == {"OPENAI_API_KEY"}
    # opencode's key follows the agent's model provider, not the runtime.
    assert router.required_keys(OPENCODE_AGENT) == {"ANTHROPIC_API_KEY"}


def test_run_dispatches_to_the_agents_harness():
    router = HarnessRouter(AGENTS)
    # A claude step shells out to `claude`; a codex step to `codex exec`.
    claude_step = StepSpec(id="w/c", agent="Fixer", body="b")
    claude_box = CaptureSandbox(json.dumps({"result": "{}", "total_cost_usd": 0.0}))
    asyncio.run(router.run(claude_step, _ctx(), claude_box))
    assert claude_box.argv[0] == "claude"

    codex_step = StepSpec(id="w/x", agent="Coder", body="b")
    codex_box = CaptureSandbox(
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "{}"}})
    )
    asyncio.run(router.run(codex_step, _ctx(), codex_box))
    assert codex_box.argv[:2] == ["codex", "exec"]

    opencode_step = StepSpec(id="w/o", agent="Helper", body="b")
    opencode_box = CaptureSandbox(
        json.dumps({"type": "text", "part": {"type": "text", "text": "{}"}})
    )
    asyncio.run(router.run(opencode_step, _ctx(), opencode_box))
    assert opencode_box.argv[:2] == ["opencode", "run"]


def test_rejects_unsupported_runtime_at_construction():
    agents = {"Mystery": AgentSpec(harness=HarnessSpec(runtime="gemini", model="gemini-2"))}
    with pytest.raises(ValueError, match="unsupported harness runtime"):
        HarnessRouter(agents)


def test_rejects_ineligible_model_at_construction():
    agents = {"Coder": AgentSpec(harness=HarnessSpec(runtime="codex", model="claude-opus-4-8"))}
    with pytest.raises(ValueError, match="not eligible"):
        HarnessRouter(agents)


def test_run_errors_on_unresolvable_agent():
    router = HarnessRouter(AGENTS)
    step = StepSpec(id="w/s", agent=None, body="b")
    with pytest.raises(HarnessError):
        asyncio.run(router.run(step, _ctx(), CaptureSandbox("")))
