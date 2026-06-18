"""HarnessRouter (B4) — dispatch each step to the harness for its agent's runtime.

The runtime holds a single `AgentHarness`; the router *is* that one object, fanning
`run`/`required_keys` out to the right per-runtime harness (`claude-code` → Claude Code,
`codex` → Codex) based on the step's agent. It is also the single chokepoint that
enforces, at startup, that every registered agent names a *supported* harness runtime —
the per-harness model-eligibility rule is enforced inside each harness's constructor.

One harness instance is built per runtime actually used; each is handed the full agents
map but only ever receives its own runtime's steps to run.
"""

from __future__ import annotations

from collections.abc import Mapping

from loopy_runtime.contract import Sandbox, StepContext, StepResult
from loopy_runtime.harness.base import HarnessError, JsonProtocolHarness
from loopy_runtime.harness.claude_code import ClaudeCodeHarness
from loopy_runtime.harness.codex import CodexHarness
from loopy_runtime.manifest_model import AgentSpec, EventContract, StepSpec
from loopy_runtime.render import TemplateRenderer

# harness.runtime -> the harness class that implements it.
BUILDERS: dict[str, type[JsonProtocolHarness]] = {
    ClaudeCodeHarness.RUNTIME: ClaudeCodeHarness,
    CodexHarness.RUNTIME: CodexHarness,
}


class HarnessRouter:
    def __init__(
        self,
        agents: Mapping[str, AgentSpec],
        events: Mapping[str, EventContract] | None = None,
        renderer: TemplateRenderer | None = None,
    ):
        self._agents = dict(agents)
        # Every registered agent must resolve to a supported harness runtime (fail-fast).
        for name, agent in self._agents.items():
            runtime = agent.harness.runtime
            if runtime not in BUILDERS:
                raise ValueError(
                    f"agent '{name}' uses unsupported harness runtime {runtime!r}; "
                    f"supported: {sorted(BUILDERS)}"
                )
        # One harness per runtime actually in use (each validates its own agents' models).
        used = {agent.harness.runtime for agent in self._agents.values()}
        self._harnesses: dict[str, JsonProtocolHarness] = {
            runtime: BUILDERS[runtime](self._agents, events, renderer) for runtime in used
        }

    def required_keys(self, agent: AgentSpec) -> set[str]:
        return self._harness_for(agent).required_keys(agent)

    async def run(self, step: StepSpec, ctx: StepContext, sandbox: Sandbox) -> StepResult:
        agent = self._agents.get(step.agent) if step.agent else None
        if agent is None:
            raise HarnessError(f"step '{step.id}' has no resolvable agent")
        return await self._harness_for(agent).run(step, ctx, sandbox)

    def _harness_for(self, agent: AgentSpec) -> JsonProtocolHarness:
        # Membership is guaranteed by the constructor's supported-runtime check.
        return self._harnesses[agent.harness.runtime]
