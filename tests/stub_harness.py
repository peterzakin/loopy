"""StubAgentHarness — test infrastructure (NOT a shipped harness).

Returns deterministic, schema-valid output AND a payload per emitted event (from the
event contract), so the conformance suite and engine tests run offline with no
model/sandbox calls. The only shipped harness is `ClaudeCodeHarness`.
"""

from __future__ import annotations

from collections.abc import Mapping

from loopy_runtime.contract import (
    Sandbox,
    StepContext,
    StepOutput,
    StepResult,
    ToolchainLayer,
)
from loopy_runtime.manifest_model import AgentSpec, EventContract, StepSpec
from loopy_runtime.payloads import synthesize_fields


class StubAgentHarness:
    def __init__(self, events: Mapping[str, EventContract] | None = None):
        self._events = dict(events or {})

    def required_keys(self, agent: AgentSpec) -> set[str]:
        return set()  # no model key needed — bypasses the provider-key check

    def missing_keys(self, agent: AgentSpec, env: Mapping[str, str]) -> set[str]:
        return set()

    def toolchain(self, agent: AgentSpec) -> ToolchainLayer:
        return ToolchainLayer()  # no toolchain to compose; the stub shells out to nothing

    def required_tools(self, agent: AgentSpec) -> set[str]:
        return set()  # nothing to probe — keeps stub-driven engine tests offline

    async def run(self, step: StepSpec, ctx: StepContext, sandbox: Sandbox) -> StepResult:
        emits = {}
        for name in step.emits:
            contract = self._events.get(name)
            emits[name] = synthesize_fields(contract.fields) if contract else {}
        return StepResult(output=StepOutput(synthesize_fields(step.output)), emits=emits)
