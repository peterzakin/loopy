"""StubAgentHarness — test infrastructure (NOT a shipped harness).

Returns deterministic, schema-valid step output so the conformance suite and engine
tests run offline with no model/sandbox calls. The only shipped harness is
`ClaudeCodeHarness`.
"""

from __future__ import annotations

from loopy_runtime.contract import Sandbox, StepContext, StepOutput
from loopy_runtime.manifest_model import AgentSpec, StepSpec
from loopy_runtime.payloads import synthesize_fields


class StubAgentHarness:
    def required_keys(self, agent: AgentSpec) -> set[str]:
        return set()  # no model key needed — bypasses the provider-key check

    async def run(self, step: StepSpec, ctx: StepContext, sandbox: Sandbox) -> StepOutput:
        return StepOutput(fields=synthesize_fields(step.output))
