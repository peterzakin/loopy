"""ClaudeCodeHarness (B4) — runs an agent as the headless `claude` CLI.

Runs inside the sandbox via `sandbox.exec` (works uniformly for local/remote sandboxes;
honors "don't build an agent loop"), parses the `--output-format json` envelope, and
feeds `total_cost_usd` to the budget enforcer. The JSON output protocol, model-eligibility
rule, and budget enforcement live in `JsonProtocolHarness`; this only adds the `claude`
argv and how to read its envelope.
"""

from __future__ import annotations

import json

from loopy_runtime.harness.base import HarnessError, JsonProtocolHarness
from loopy_runtime.manifest_model import AgentSpec, StepSpec

__all__ = ["ClaudeCodeHarness", "HarnessError"]


class ClaudeCodeHarness(JsonProtocolHarness):
    RUNTIME = "claude-code"

    def build_argv(self, step: StepSpec, agent: AgentSpec, prompt: str) -> list[str]:
        argv = ["claude", "-p", prompt, "--output-format", "json"]
        if agent.harness.model:
            argv += ["--model", agent.harness.model]
        if agent.tools:
            argv += ["--allowed-tools", " ".join(agent.tools)]
        argv += ["--permission-mode", "bypassPermissions"]  # the sandbox is the boundary
        return argv

    def _parse_response(self, stdout: str, step: StepSpec) -> tuple[str, float]:
        try:
            envelope = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise HarnessError(f"step '{step.id}': claude output was not JSON") from exc
        if not isinstance(envelope, dict):
            raise HarnessError(f"step '{step.id}': claude JSON envelope was not an object")
        return str(envelope.get("result", "")), float(envelope.get("total_cost_usd", 0.0))
