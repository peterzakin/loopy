"""OpenCodeHarness — runs an agent as the headless `opencode` CLI.

Mirrors ClaudeCodeHarness/CodexHarness behind the same `AgentHarness` protocol: runs
`opencode run` inside the sandbox, parses the agent's final message out of the
`--format json` event stream, and feeds it through the shared JSON output protocol
(`JsonProtocolHarness`).

OpenCode is model-agnostic — its models are named `provider/model` (e.g.
`anthropic/claude-sonnet-4-6`, `openai/gpt-5.5`) and it authenticates each provider
from that provider's own env var. Both harness rules therefore derive from the model's
provider prefix (see `providers.py`): eligibility admits the prefixes loopy recognizes,
and `required_keys` resolves per agent (an `anthropic/*` agent needs ANTHROPIC_API_KEY,
an `openai/*` agent OPENAI_API_KEY). A model is mandatory — with no `provider/` prefix
there is nothing to derive auth from.

`opencode run --format json` emits one JSON event per line on stdout, each
`{"type": ..., "timestamp": ..., "sessionID": ..., ...}`:

* `text` — a *completed* assistant text part (`part.text` is the full text, never a
  delta); the agent's answer is the last one.
* `step_finish` — closes one model step, carrying `part.tokens` (input/output/
  reasoning/cache) and `part.cost`: a USD estimate priced from OpenCode's model
  catalog, the same client-side signal class as claude's `total_cost_usd`. Costs sum
  across steps into `Usage.cost_usd`, so spend budgets are enforceable here (unlike
  codex).
* `step_start` / `tool_use` / `reasoning` / `error` — skipped; a fatal `error` also
  sets a non-zero exit code, which the base surfaces with the transcript tail.
"""

from __future__ import annotations

import json

from loopy_runtime.contract import ToolchainLayer, Usage
from loopy_runtime.harness.base import HarnessError, JsonProtocolHarness
from loopy_runtime.manifest_model import AgentSpec, StepSpec

__all__ = ["OpenCodeHarness", "HarnessError"]

# The OpenCode CLI and its Node runtime, on top of the base substrate.
# TODO(#16): pin the CLI version once a baseline is chosen, for reproducible sandboxes.
_TOOLCHAIN = ToolchainLayer(
    apt=("nodejs", "npm"),
    run=("npm install -g opencode-ai",),
    probe=("opencode",),
)


class OpenCodeHarness(JsonProtocolHarness):
    RUNTIME = "opencode"

    def toolchain(self, agent: AgentSpec) -> ToolchainLayer:
        return super().toolchain(agent).merge(_TOOLCHAIN)

    def build_argv(self, step: StepSpec, agent: AgentSpec, prompt: str) -> list[str]:
        # `run` is opencode's non-interactive mode; `--format json` emits one JSON
        # event per line. The model is always present (construction rejects a None
        # model for this runtime — auth derives from its provider prefix).
        argv = ["opencode", "run", prompt, "--format", "json"]
        if agent.harness.model:
            argv += ["--model", agent.harness.model]
        # The loopy sandbox is the boundary, so approve opencode's own permission
        # requests — headless `run` auto-REJECTS them otherwise, silently crippling
        # the agent. The analogue of claude's `bypassPermissions`.
        argv += ["--auto"]
        return argv

    def _parse_response(self, stdout: str, step: StepSpec) -> tuple[str, Usage]:
        message: str | None = None
        input_tokens = output_tokens = 0
        cost: float | None = None
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue  # tolerate non-JSON noise interleaved on stdout
            if not isinstance(event, dict):
                continue
            part = event.get("part")
            part = part if isinstance(part, dict) else {}
            if event.get("type") == "text":
                # A completed text part; the last one is the agent's final answer.
                text = part.get("text")
                if isinstance(text, str):
                    message = text
            elif event.get("type") == "step_finish":
                tokens = part.get("tokens")
                tokens = tokens if isinstance(tokens, dict) else {}
                input_tokens += int(tokens.get("input") or 0)
                output_tokens += int(tokens.get("output") or 0)
                # Sum the per-step USD estimates; None (no step_finish seen) stays
                # distinguishable from a real $0 run.
                cost = (cost or 0.0) + float(part.get("cost") or 0.0)
        if message is None:
            raise HarnessError(f"step '{step.id}': opencode produced no text message")
        return message, Usage(
            input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=cost
        )
