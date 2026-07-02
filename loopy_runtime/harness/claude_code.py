"""ClaudeCodeHarness (B4) — runs an agent as the headless `claude` CLI.

Runs inside the sandbox via `sandbox.exec` (works uniformly for local/remote sandboxes;
honors "don't build an agent loop"), parses the `--output-format json` envelope, and
feeds `total_cost_usd` to the budget enforcer. The JSON output protocol, model-eligibility
rule, and budget enforcement live in `JsonProtocolHarness`; this only adds the `claude`
argv and how to read its envelope.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from loopy_runtime.contract import ToolchainLayer, Usage
from loopy_runtime.harness.base import HarnessError, JsonProtocolHarness
from loopy_runtime.manifest_model import AgentSpec, StepSpec
from loopy_runtime.providers import required_model_key

__all__ = ["ClaudeCodeHarness", "HarnessError"]

# Where the Claude Code CLI stores OAuth/subscription credentials under HOME. When present
# and reachable, the agent authenticates without an ANTHROPIC_API_KEY.
_OAUTH_CREDENTIALS = Path(".claude", ".credentials.json")

# The Claude Code CLI (`claude`) and its Node runtime, on top of the base substrate. Composed
# onto the user's image so the binary the harness shells out to is present by construction.
# TODO(#16): pin the CLI version once a baseline is chosen, for reproducible sandboxes.
_TOOLCHAIN = ToolchainLayer(
    apt=("nodejs", "npm"),
    run=("npm install -g @anthropic-ai/claude-code",),
    probe=("claude",),
)


def _claude_oauth_available(env: Mapping[str, str]) -> bool:
    """True when Claude Code OAuth credentials are reachable, so no API key is needed.

    Keyed strictly on the sandbox's own `HOME` (from the env_file / image) — not the
    control-plane's — so the check is deterministic and never passes on creds the sandbox
    can't actually read. To use OAuth, point the sandbox `HOME` at a dir holding
    `.claude/.credentials.json` (for the bare `local` provider, your host home).
    """
    home = env.get("HOME")
    return bool(home) and (Path(home) / _OAUTH_CREDENTIALS).is_file()


class ClaudeCodeHarness(JsonProtocolHarness):
    RUNTIME = "claude-code"

    def toolchain(self, agent: AgentSpec) -> ToolchainLayer:
        return super().toolchain(agent).merge(_TOOLCHAIN)

    def missing_keys(self, agent: AgentSpec, env: Mapping[str, str]) -> set[str]:
        # Local Claude Code is usually OAuth/subscription-authed, not API-keyed. Treat the
        # model key as satisfied when those credentials are reachable, so a runnable OAuth
        # setup isn't rejected up front for lacking ANTHROPIC_API_KEY.
        missing = super().missing_keys(agent, env)
        model_key = required_model_key(self.RUNTIME)
        if model_key in missing and _claude_oauth_available(env):
            missing = missing - {model_key}
        return missing

    def build_argv(self, step: StepSpec, agent: AgentSpec, prompt: str) -> list[str]:
        # The model is always present (agents name one mandatorily; construction
        # rejects a missing/ineligible one), so the CLI never falls back to its own
        # default model.
        argv = ["claude", "-p", prompt, "--output-format", "json"]
        argv += ["--model", agent.model]
        # No `--allowed-tools`: that flag is an allowlist over Claude's *built-in* tools
        # (Bash/Edit/Write/…), not loopy's capability vocabulary, so narrowing it here would
        # strip the agent's default toolset. The sandbox is the capability boundary; the agent
        # keeps its full toolset under bypassPermissions.
        argv += ["--permission-mode", "bypassPermissions"]  # the sandbox is the boundary
        return argv

    def _parse_response(self, stdout: str, step: StepSpec) -> tuple[str, Usage]:
        try:
            envelope = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise HarnessError(f"step '{step.id}': claude output was not JSON") from exc
        if not isinstance(envelope, dict):
            raise HarnessError(f"step '{step.id}': claude JSON envelope was not an object")
        # `usage` → input/output tokens (the portable signal the cascade cap enforces on);
        # `total_cost_usd` → optional metadata (a client-side estimate, not enforced in v1).
        # Cache/reasoning token fields are deliberately left out of the v1 floor.
        block = envelope.get("usage")
        block = block if isinstance(block, dict) else {}
        cost = envelope.get("total_cost_usd")
        usage = Usage(
            input_tokens=int(block.get("input_tokens") or 0),
            output_tokens=int(block.get("output_tokens") or 0),
            cost_usd=float(cost) if cost is not None else None,
        )
        return str(envelope.get("result", "")), usage
