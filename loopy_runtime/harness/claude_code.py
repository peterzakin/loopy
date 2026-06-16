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
from loopy_runtime.contract import Sandbox, StepContext, StepOutput, StepResult
from loopy_runtime.manifest_model import AgentSpec, EventContract, StepSpec
from loopy_runtime.providers import required_model_key
from loopy_runtime.render import TemplateRenderer


class HarnessError(Exception):
    """Transient failure — the RetryPolicy may retry."""


class ClaudeCodeHarness:
    def __init__(
        self,
        agents: Mapping[str, AgentSpec],
        events: Mapping[str, EventContract] | None = None,
        renderer: TemplateRenderer | None = None,
    ):
        self._agents = dict(agents)
        self._events = dict(events or {})
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

    async def run(self, step: StepSpec, ctx: StepContext, sandbox: Sandbox) -> StepResult:
        if step.agent is None or step.agent not in self._agents:
            raise HarnessError(f"step '{step.id}' has no resolvable agent")
        agent = self._agents[step.agent]

        body = self._renderer.render(step, ctx.event, ctx.upstream)
        prompt = body + self._instruction(step)
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

        parsed = self._parse_result(envelope.get("result", ""), step)
        return StepResult(
            output=StepOutput(fields=self._extract_output(parsed, step)),
            emits=self._extract_emits(parsed, step),
        )

    def _instruction(self, step: StepSpec) -> str:
        """Ask for a JSON object with `output` keys and an `emits` payload per emitted event."""
        if not step.output and not step.emits:
            return ""
        parts = []
        if step.output:
            parts.append(f'"output" with keys {sorted(step.output)}')
        for event_name in step.emits:
            fields = sorted(self._events[event_name].fields) if event_name in self._events else []
            parts.append(f'"emits.{event_name}" with keys {fields}')
        return (
            '\n\nReturn ONLY a JSON object {"output": {...}, "emits": {"<Event>": {...}}} '
            f"providing {'; '.join(parts)}."
        )

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
    def _parse_result(result_text: str, step: StepSpec) -> dict:
        if not step.output and not step.emits:
            return {}
        try:
            parsed = json.loads(result_text)
        except json.JSONDecodeError as exc:
            raise HarnessError(f"step '{step.id}': result was not JSON") from exc
        if not isinstance(parsed, dict):
            raise HarnessError(f"step '{step.id}': result JSON was not an object")
        return parsed

    @staticmethod
    def _extract_output(parsed: dict, step: StepSpec) -> dict:
        if not step.output:
            return {}
        obj = parsed.get("output")
        missing = set(step.output) - set(obj) if isinstance(obj, dict) else set(step.output)
        if missing:
            raise HarnessError(f"step '{step.id}': output missing keys {sorted(missing)}")
        return {key: obj[key] for key in step.output}

    def _extract_emits(self, parsed: dict, step: StepSpec) -> dict[str, dict]:
        emits_obj = parsed.get("emits") if isinstance(parsed.get("emits"), dict) else {}
        out: dict[str, dict] = {}
        for event_name in step.emits:
            payload = emits_obj.get(event_name)
            if not isinstance(payload, dict):
                raise HarnessError(f"step '{step.id}': no payload for emits '{event_name}'")
            contract = self._events.get(event_name)
            if contract is not None:
                missing = set(contract.fields) - set(payload)
                if missing:
                    raise HarnessError(
                        f"step '{step.id}': emit '{event_name}' missing {sorted(missing)}"
                    )
            out[event_name] = payload
        return out
