"""CodexHarness (B4) — runs an agent as the headless OpenAI `codex` CLI.

Mirrors ClaudeCodeHarness behind the same `AgentHarness` protocol: runs `codex exec`
inside the sandbox, then parses the agent's final message out of the `--json` event
stream and feeds it through the shared JSON output protocol (`JsonProtocolHarness`).

Two provider differences from Claude Code, both handled in the base via `providers.py`:

* Eligible models are OpenAI's (`gpt-*`, the o-series, `codex-*`) — enforced at
  construction by the model-eligibility rule.
* `codex exec` reports **token usage only, no USD cost** (see the cost-budget plan), so
  cost is 0.0 here and a `spend.usd` budget on a codex step is rejected rather than
  silently ignored.

`codex exec --json` emits a JSONL event stream on stdout; the agent's answer is the last
`agent_message` item in it. Non-JSON banner lines and partial deltas are tolerated.
"""

from __future__ import annotations

import json

from loopy_runtime.contract import ToolchainLayer
from loopy_runtime.harness.base import HarnessError, JsonProtocolHarness
from loopy_runtime.manifest_model import AgentSpec, StepSpec

__all__ = ["CodexHarness", "HarnessError"]

# The OpenAI `codex` CLI and its Node runtime, on top of the base substrate.
# TODO(#16): pin the CLI version once a baseline is chosen, for reproducible sandboxes.
_TOOLCHAIN = ToolchainLayer(
    apt=("nodejs", "npm"),
    run=("npm install -g @openai/codex",),
    probe=("codex",),
)


class CodexHarness(JsonProtocolHarness):
    RUNTIME = "codex"

    def toolchain(self, agent: AgentSpec) -> ToolchainLayer:
        return super().toolchain(agent).merge(_TOOLCHAIN)

    def build_argv(self, step: StepSpec, agent: AgentSpec, prompt: str) -> list[str]:
        # `exec` is codex's non-interactive mode; `--json` streams structured events.
        argv = ["codex", "exec", prompt, "--json"]
        if agent.harness.model:
            argv += ["--model", agent.harness.model]
        # The loopy sandbox is the boundary, so drop codex's own approval/sandbox layer —
        # the analogue of claude's `bypassPermissions`.
        argv += ["--dangerously-bypass-approvals-and-sandbox"]
        return argv

    def _parse_response(self, stdout: str, step: StepSpec) -> tuple[str, float]:
        message = self._final_message(stdout, step)
        return message, 0.0  # codex reports token usage only, no USD cost

    @classmethod
    def _final_message(cls, stdout: str, step: StepSpec) -> str:
        """The text of the last completed agent message in the JSONL event stream."""
        message: str | None = None
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue  # codex may interleave non-JSON banner lines on stdout
            if isinstance(event, dict):
                text = cls._agent_text(event)
                if text is not None:
                    message = text
        if message is None:
            raise HarnessError(f"step '{step.id}': codex produced no agent message")
        return message

    @staticmethod
    def _agent_text(event: dict) -> str | None:
        """Pull the text of a *completed* agent message from a codex event, across the
        couple of shapes codex emits. Partial `*_delta` events are skipped so we never
        return a fragment."""
        # `codex exec --json`: {"type": "item.completed", "item": {"type": "agent_message",
        # "text": "..."}}
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message":
            text = item.get("text")
            return text if isinstance(text, str) else None
        # event-stream protocol: {"msg": {"type": "agent_message", "message": "..."}}
        msg = event.get("msg")
        if isinstance(msg, dict) and msg.get("type") == "agent_message":
            text = msg.get("message")
            return text if isinstance(text, str) else None
        # flat: {"type": "agent_message", "message"/"text": "..."}
        if event.get("type") == "agent_message":
            text = event.get("message") or event.get("text")
            return text if isinstance(text, str) else None
        return None
