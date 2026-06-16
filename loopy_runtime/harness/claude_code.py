"""ClaudeCodeHarness (B4) — the only shipped harness.

Runs the agent as the headless `claude` CLI **inside the sandbox** via `sandbox.exec`
(works uniformly for local/remote sandboxes; honors "don't build an agent loop").
Parses the JSON envelope, validates output against `step.output` (prompt-and-parse),
and feeds `total_cost_usd` to the budget enforcer.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping

from loopy_runtime.budget import BudgetEnforcer
from loopy_runtime.contract import Sandbox, StepContext, StepOutput
from loopy_runtime.manifest_model import AgentSpec, StepSpec
from loopy_runtime.providers import required_model_key
from loopy_runtime.render import TemplateRenderer


class HarnessError(Exception):
    """Transient failure — the RetryPolicy may retry."""


class ClaudeCodeHarness:
    def __init__(self, agents: Mapping[str, AgentSpec], renderer: TemplateRenderer | None = None):
        self._agents = dict(agents)
        self._renderer = renderer or TemplateRenderer()

    def required_keys(self, agent: AgentSpec) -> set[str]:
        return {required_model_key(agent.harness.runtime)}

    def build_argv(self, step: StepSpec, agent: AgentSpec, prompt: str) -> list[str]:
        argv = ["claude", "-p", prompt, "--output-format", "json"]
        if agent.harness.model:
            argv += ["--model", agent.harness.model]
        if agent.tools:
            argv += ["--allowed-tools", " ".join(agent.tools)]
        argv += ["--permission-mode", "bypassPermissions"]  # the sandbox is the boundary
        return argv

    async def run(self, step: StepSpec, ctx: StepContext, sandbox: Sandbox) -> StepOutput:
        if step.agent is None or step.agent not in self._agents:
            raise HarnessError(f"step '{step.id}' has no resolvable agent")
        agent = self._agents[step.agent]

        body = self._renderer.render(step, ctx.event, ctx.upstream)
        prompt = self._with_output_instruction(body, step)
        argv = self.build_argv(step, agent, prompt)

        timeout = BudgetEnforcer.wall_clock_seconds(step.budget)
        try:
            result = await asyncio.wait_for(sandbox.exec(argv), timeout=timeout)
        except TimeoutError as exc:
            raise HarnessError(f"step '{step.id}' exceeded wall_clock budget") from exc

        if result.exit_code != 0:
            raise HarnessError(f"claude exited {result.exit_code}: {result.stderr.strip()}")

        envelope = self._parse_envelope(result.stdout, step)
        BudgetEnforcer.check_spend(step.budget, float(envelope.get("total_cost_usd", 0.0)))
        return StepOutput(fields=self._parse_output(envelope.get("result", ""), step))

    @staticmethod
    def _with_output_instruction(body: str, step: StepSpec) -> str:
        if not step.output:
            return body
        keys = ", ".join(sorted(step.output))
        return f"{body}\n\nReturn ONLY a JSON object with these keys: {keys}."

    @staticmethod
    def _parse_envelope(stdout: str, step: StepSpec) -> dict:
        try:
            envelope = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise HarnessError(f"step '{step.id}': claude output was not JSON") from exc
        if not isinstance(envelope, dict):
            raise HarnessError(f"step '{step.id}': claude JSON envelope was not an object")
        return envelope

    @staticmethod
    def _parse_output(result_text: str, step: StepSpec) -> dict:
        if not step.output:
            return {}
        try:
            parsed = json.loads(result_text)
        except json.JSONDecodeError as exc:
            raise HarnessError(f"step '{step.id}': result was not JSON matching output:") from exc
        missing = set(step.output) - set(parsed) if isinstance(parsed, dict) else set(step.output)
        if missing:
            raise HarnessError(f"step '{step.id}': output missing keys {sorted(missing)}")
        return {key: parsed[key] for key in step.output}
