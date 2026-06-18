"""B2 unit tests for CodexHarness — argv construction + JSONL message parsing + the
per-harness model/cost rules.

A CaptureSandbox stands in for a real sandbox: it records the argv and returns a canned
`codex exec --json` JSONL event stream. No real model/sandbox calls.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest

from loopy_runtime.contract import Event, ExecResult, StepContext
from loopy_runtime.harness.codex import CodexHarness, HarnessError
from loopy_runtime.manifest_model import (
    AgentSpec,
    BudgetSpec,
    EventContract,
    HarnessSpec,
    StepSpec,
)


class CaptureSandbox:
    id = "capture"

    def __init__(self, stdout: str, exit_code: int = 0):
        self.stdout = stdout
        self.exit_code = exit_code
        self.argv: list[str] | None = None

    async def exec(self, cmd: list[str]) -> ExecResult:
        self.argv = cmd
        return ExecResult(self.exit_code, self.stdout, "")

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
    harness=HarnessSpec(runtime="codex", model="gpt-5-codex"), tools=["open_pr"]
)


def _harness(events=None):
    return CodexHarness({"Fixer": AGENT}, events)


def _stream(*events: dict) -> str:
    """A codex `--json` JSONL stream, optionally with banner noise interleaved."""
    return "\n".join(json.dumps(e) if isinstance(e, dict) else e for e in events)


def _agent_message(text: str) -> dict:
    return {"type": "item.completed", "item": {"type": "agent_message", "text": text}}


def test_build_argv_has_expected_flags():
    step = StepSpec(id="w/s", agent="Fixer", body="do it")
    argv = _harness().build_argv(step, AGENT, "do it")
    assert argv[:3] == ["codex", "exec", "do it"]
    assert "--json" in argv
    assert "--model" in argv and "gpt-5-codex" in argv
    assert "--dangerously-bypass-approvals-and-sandbox" in argv


def test_run_parses_typed_output_from_jsonl():
    step = StepSpec(id="w/s", agent="Fixer", output={"goal": {"type": "string"}}, body="b")
    stdout = _stream(
        {"type": "thread.started", "thread_id": "t1"},  # noise
        _agent_message(json.dumps({"output": {"goal": "ship it"}, "emits": {}})),
        {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}},
    )
    result = asyncio.run(_harness().run(step, _ctx(), CaptureSandbox(stdout)))
    assert result.output.fields == {"goal": "ship it"}
    assert result.emits == {}


def test_run_uses_last_agent_message():
    step = StepSpec(id="w/s", agent="Fixer", output={"goal": {"type": "string"}}, body="b")
    stdout = _stream(
        _agent_message(json.dumps({"output": {"goal": "draft"}, "emits": {}})),
        _agent_message(json.dumps({"output": {"goal": "final"}, "emits": {}})),
    )
    result = asyncio.run(_harness().run(step, _ctx(), CaptureSandbox(stdout)))
    assert result.output.fields == {"goal": "final"}


def test_run_tolerates_non_json_banner_lines():
    step = StepSpec(id="w/s", agent="Fixer", output={"goal": {"type": "string"}}, body="b")
    stdout = "\n".join(
        [
            "OpenAI Codex v0.0.0",  # human-readable banner, not JSON
            json.dumps(_agent_message(json.dumps({"output": {"goal": "ok"}, "emits": {}}))),
        ]
    )
    result = asyncio.run(_harness().run(step, _ctx(), CaptureSandbox(stdout)))
    assert result.output.fields == {"goal": "ok"}


def test_run_produces_agent_emit_payload():
    step = StepSpec(id="w/s", agent="Fixer", emits=["WorkItem"], body="b")
    events = {"WorkItem": EventContract(fields={"link": {"type": "string"}})}
    stdout = _stream(
        _agent_message(json.dumps({"output": {}, "emits": {"WorkItem": {"link": "https://pr/1"}}}))
    )
    result = asyncio.run(_harness(events).run(step, _ctx(), CaptureSandbox(stdout)))
    assert result.emits == {"WorkItem": {"link": "https://pr/1"}}


def test_run_errors_when_no_agent_message():
    step = StepSpec(id="w/s", agent="Fixer", output={"goal": {"type": "string"}}, body="b")
    stdout = _stream({"type": "turn.completed", "usage": {}})  # no agent_message anywhere
    with pytest.raises(HarnessError):
        asyncio.run(_harness().run(step, _ctx(), CaptureSandbox(stdout)))


def test_run_requires_model_key_declared():
    assert _harness().required_keys(AGENT) == {"OPENAI_API_KEY"}


def test_run_rejects_spend_budget_codex_reports_no_cost():
    # Codex emits token usage only; a spend budget can't be enforced, so refuse it
    # rather than silently let it pass.
    step = StepSpec(id="w/s", agent="Fixer", budget=BudgetSpec(spend={"usd": 1}), body="b")
    stdout = _stream(_agent_message("{}"))
    with pytest.raises(HarnessError, match="reports no USD cost"):
        asyncio.run(_harness().run(step, _ctx(), CaptureSandbox(stdout)))


def test_run_raises_on_nonzero_exit():
    step = StepSpec(id="w/s", agent="Fixer", body="b")
    with pytest.raises(HarnessError):
        asyncio.run(_harness().run(step, _ctx(), CaptureSandbox("", exit_code=1)))


def test_construction_rejects_ineligible_model():
    bad = AgentSpec(harness=HarnessSpec(runtime="codex", model="claude-opus-4-8"))
    with pytest.raises(ValueError, match="not eligible"):
        CodexHarness({"Fixer": bad})
