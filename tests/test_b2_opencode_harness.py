"""Unit tests for OpenCodeHarness — argv construction + JSONL event parsing + the
per-harness model/key/cost rules.

A CaptureSandbox stands in for a real sandbox: it records the argv and returns a canned
`opencode run --format json` JSONL event stream. No real model/sandbox calls. The event
shapes mirror what the CLI actually emits: one `{"type": ..., "sessionID": ..., "part":
{...}}` object per line, with the agent's answer in the last completed `text` part and
per-step tokens + USD cost on `step_finish` parts.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest

from loopy_runtime.budget import BudgetExceeded
from loopy_runtime.contract import Event, ExecResult, StepContext
from loopy_runtime.harness.opencode import HarnessError, OpenCodeHarness
from loopy_runtime.manifest_model import (
    AgentSpec,
    BudgetSpec,
    EventContract,
    StepSpec,
)
from loopy_runtime.providers import validate_model


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


AGENT = AgentSpec(model="anthropic/claude-sonnet-4-6", harness="opencode")


def _harness(events=None):
    return OpenCodeHarness({"Fixer": AGENT}, events)


def _stream(*events) -> str:
    """An `opencode run --format json` JSONL stream, optionally with noise interleaved."""
    return "\n".join(json.dumps(e) if isinstance(e, dict) else e for e in events)


def _text(text: str) -> dict:
    return {
        "type": "text",
        "timestamp": 0,
        "sessionID": "s1",
        "part": {"type": "text", "text": text, "time": {"start": 0, "end": 1}},
    }


def _step_finish(input_tokens: int, output_tokens: int, cost: float) -> dict:
    return {
        "type": "step_finish",
        "timestamp": 0,
        "sessionID": "s1",
        "part": {
            "type": "step-finish",
            "tokens": {
                "input": input_tokens,
                "output": output_tokens,
                "reasoning": 0,
                "cache": {"read": 0, "write": 0},
            },
            "cost": cost,
        },
    }


def test_build_argv_has_expected_flags():
    step = StepSpec(id="w/s", agent="Fixer", body="do it")
    argv = _harness().build_argv(step, AGENT, "do it")
    assert argv[:3] == ["opencode", "run", "do it"]
    assert "--format" in argv and "json" in argv
    assert "--model" in argv and "anthropic/claude-sonnet-4-6" in argv
    # Headless `run` auto-rejects permission requests unless --auto approves them.
    assert "--auto" in argv


def test_run_parses_typed_output_from_jsonl():
    step = StepSpec(id="w/s", agent="Fixer", output={"goal": {"type": "string"}}, body="b")
    stdout = _stream(
        {"type": "step_start", "timestamp": 0, "sessionID": "s1", "part": {}},  # noise
        _text(json.dumps({"output": {"goal": "ship it"}, "emits": {}})),
        _step_finish(10, 5, 0.001),
    )
    result = asyncio.run(_harness().run(step, _ctx(), CaptureSandbox(stdout)))
    assert result.output.fields == {"goal": "ship it"}
    assert result.emits == {}


def test_run_uses_last_text_event():
    step = StepSpec(id="w/s", agent="Fixer", output={"goal": {"type": "string"}}, body="b")
    stdout = _stream(
        _text(json.dumps({"output": {"goal": "draft"}, "emits": {}})),
        _text(json.dumps({"output": {"goal": "final"}, "emits": {}})),
    )
    result = asyncio.run(_harness().run(step, _ctx(), CaptureSandbox(stdout)))
    assert result.output.fields == {"goal": "final"}


def test_run_tolerates_non_json_noise_lines():
    step = StepSpec(id="w/s", agent="Fixer", output={"goal": {"type": "string"}}, body="b")
    stdout = _stream(
        "opencode v1.0.0",  # human-readable noise, not JSON
        _text(json.dumps({"output": {"goal": "ok"}, "emits": {}})),
    )
    result = asyncio.run(_harness().run(step, _ctx(), CaptureSandbox(stdout)))
    assert result.output.fields == {"goal": "ok"}


def test_run_produces_agent_emit_payload():
    step = StepSpec(id="w/s", agent="Fixer", emits=["WorkItem"], body="b")
    events = {"WorkItem": EventContract(fields={"link": {"type": "string"}})}
    stdout = _stream(
        _text(json.dumps({"output": {}, "emits": {"WorkItem": {"link": "https://pr/1"}}}))
    )
    result = asyncio.run(_harness(events).run(step, _ctx(), CaptureSandbox(stdout)))
    assert result.emits == {"WorkItem": {"link": "https://pr/1"}}


def test_run_sums_tokens_and_cost_across_steps():
    step = StepSpec(id="w/s", agent="Fixer", body="b")
    stdout = _stream(
        _step_finish(100, 20, 0.01),
        _text("{}"),
        _step_finish(50, 30, 0.02),
    )
    result = asyncio.run(_harness().run(step, _ctx(), CaptureSandbox(stdout)))
    assert result.usage.input_tokens == 150
    assert result.usage.output_tokens == 50
    assert result.usage.cost_usd == pytest.approx(0.03)


def test_run_reports_no_cost_without_step_finish():
    # No step_finish -> cost_usd stays None (unknown), not a misleading $0.
    step = StepSpec(id="w/s", agent="Fixer", body="b")
    result = asyncio.run(_harness().run(step, _ctx(), CaptureSandbox(_stream(_text("{}")))))
    assert result.usage.cost_usd is None


def test_run_enforces_spend_budget_on_summed_cost():
    # OpenCode reports USD cost, so a spend budget is enforceable (unlike codex).
    step = StepSpec(id="w/s", agent="Fixer", budget=BudgetSpec(spend={"usd": 0.01}), body="b")
    stdout = _stream(_step_finish(10, 5, 0.05), _text("{}"))
    with pytest.raises(BudgetExceeded):
        asyncio.run(_harness().run(step, _ctx(), CaptureSandbox(stdout)))


def test_run_errors_when_no_text_message():
    step = StepSpec(id="w/s", agent="Fixer", output={"goal": {"type": "string"}}, body="b")
    stdout = _stream(_step_finish(10, 5, 0.001))  # no text event anywhere
    with pytest.raises(HarnessError):
        asyncio.run(_harness().run(step, _ctx(), CaptureSandbox(stdout)))


def test_required_key_derives_from_model_provider_prefix():
    assert _harness().required_keys(AGENT) == {"ANTHROPIC_API_KEY"}
    openai_agent = AgentSpec(model="openai/gpt-5.5", harness="opencode")
    assert OpenCodeHarness({"Coder": openai_agent}).required_keys(openai_agent) == {
        "OPENAI_API_KEY"
    }


def test_bare_model_id_sugars_to_provider_form():
    """An agent may write the bare id every other runtime uses; the harness expands it to
    opencode's provider/model naming in the argv, and the key derivation follows suit."""
    step = StepSpec(id="w/s", agent="Fixer", body="b")
    bare = AgentSpec(model="claude-sonnet-4-6", harness="opencode")
    harness = OpenCodeHarness({"Fixer": bare})
    argv = harness.build_argv(step, bare, "b")
    assert "anthropic/claude-sonnet-4-6" in argv
    assert "claude-sonnet-4-6" not in argv  # never the bare form
    assert harness.required_keys(bare) == {"ANTHROPIC_API_KEY"}

    openai_bare = AgentSpec(model="gpt-5.5", harness="opencode")
    harness = OpenCodeHarness({"Fixer": openai_bare})
    assert "openai/gpt-5.5" in harness.build_argv(step, openai_bare, "b")
    assert harness.required_keys(openai_bare) == {"OPENAI_API_KEY"}


def test_run_raises_on_nonzero_exit():
    step = StepSpec(id="w/s", agent="Fixer", body="b")
    with pytest.raises(HarnessError):
        asyncio.run(_harness().run(step, _ctx(), CaptureSandbox("", exit_code=1)))


def test_construction_rejects_unknown_provider_model():
    # A model no recognized provider serves (and no sugar covers) has nothing to derive
    # eligibility or an auth key from.
    bad = AgentSpec(model="gemini-2.5-pro", harness="opencode")
    with pytest.raises(ValueError, match="not eligible"):
        OpenCodeHarness({"Fixer": bad})


def test_validate_model_rejects_missing_model():
    # Every agent names its model mandatorily — no runtime falls back to a CLI default
    # (and for opencode the provider key derives from the model's prefix).
    with pytest.raises(ValueError, match="must name a model"):
        validate_model("opencode", None)
