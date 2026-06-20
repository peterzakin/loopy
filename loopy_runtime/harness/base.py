"""Shared harness machinery (B4).

A loopy harness runs `step.agent` as a headless CLI **inside the sandbox** (the sandbox
is the security boundary — see the per-harness bypass flags), then parses a small JSON
protocol the agent is asked to emit: `{"output": {...}, "emits": {"<Event>": {...}}}`.
That protocol — the prompt instruction, result parsing, and output/emit validation — is
identical across providers, so it lives here; a concrete harness only supplies the CLI
argv and how to read the agent's final message + `Usage` (tokens, optional USD cost) out
of that CLI's stdout.

The base also enforces the two per-harness rules:

* **Model eligibility** — at construction, every agent this harness owns must name a
  model the runtime is allowed to drive (`providers.validate_model`). Fail-fast.
* **No silently-ignored spend budget** — at run, a step with a `spend.usd` budget on a
  harness that reports no USD cost (Codex) is a hard error, not a silent no-op.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping

from loopy_runtime.budget import BudgetEnforcer
from loopy_runtime.contract import (
    Sandbox,
    StepContext,
    StepOutput,
    StepResult,
    ToolchainLayer,
    Usage,
)
from loopy_runtime.manifest_model import AgentSpec, EventContract, StepSpec
from loopy_runtime.providers import provider, required_model_key, validate_model
from loopy_runtime.render import TemplateRenderer

# How much agent output to keep in an error message — enough to debug, not a flood.
_TRANSCRIPT_LIMIT = 2000
_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)

# Tools any agent needs regardless of harness: `git` for repo work (declared `repos:` are
# cloned into the workspace) and TLS roots for HTTPS. Each harness extends this with its own
# CLI + runtime via `toolchain`. Kept additive-only so it composes onto any user base image.
SUBSTRATE = ToolchainLayer(apt=("git", "ca-certificates"), probe=("git",))


def _tail(text: str, limit: int = _TRANSCRIPT_LIMIT) -> str:
    """The last `limit` chars of `text` (the end is where errors usually surface)."""
    text = text.strip()
    return text if len(text) <= limit else "…" + text[-limit:]


def _balanced_json_objects(text: str) -> list[str]:
    """Every top-level `{...}` span in `text`, brace-balanced and string-aware (so braces
    inside JSON strings don't throw off the depth count). Used to recover a JSON object an
    LLM wrapped in prose."""
    spans: list[str] = []
    depth = start = 0
    in_str = esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0:
                spans.append(text[start : i + 1])
    return spans


def _extract_json_object(text: str) -> dict | None:
    """Best-effort JSON object out of an agent's final message: try the whole thing, then a
    fenced ```json block, then the last balanced `{...}` (LLMs love a trailing sentence or a
    code fence). Returns the object, or None if nothing parses to one."""
    stripped = text.strip()
    fenced = _FENCE.match(stripped)
    candidates = [stripped] + ([fenced.group(1).strip()] if fenced else [])
    candidates += reversed(_balanced_json_objects(text))  # last balanced object first
    for chunk in candidates:
        try:
            obj = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


class HarnessError(Exception):
    """Transient failure — the RetryPolicy may retry."""


class JsonProtocolHarness:
    """Base for CLI harnesses that speak the loopy JSON output protocol."""

    RUNTIME: str  # the harness.runtime id this harness implements (set by subclass)

    def __init__(
        self,
        agents: Mapping[str, AgentSpec],
        events: Mapping[str, EventContract] | None = None,
        renderer: TemplateRenderer | None = None,
    ):
        self._agents = dict(agents)
        self._events = dict(events or {})
        self._renderer = renderer or TemplateRenderer()
        # Fail fast: every agent this harness is responsible for must name a model the
        # runtime is eligible to drive. Agents bound to a *different* runtime are left to
        # their own harness (the router only ever dispatches our runtime's steps to us).
        for name, agent in self._agents.items():
            if agent.harness.runtime == self.RUNTIME:
                try:
                    validate_model(self.RUNTIME, agent.harness.model)
                except ValueError as exc:
                    raise ValueError(f"agent '{name}': {exc}") from exc

    def required_keys(self, agent: AgentSpec) -> set[str]:
        return {required_model_key(agent.harness.runtime)}

    def missing_keys(self, agent: AgentSpec, env: Mapping[str, str]) -> set[str]:
        """Required keys not satisfiable for `agent` given the sandbox `env`. Default: those
        absent from `env`; a harness may override to honor other auth (e.g. OAuth creds)."""
        return self.required_keys(agent) - set(env)

    def toolchain(self, agent: AgentSpec) -> ToolchainLayer:
        """The common substrate every agent needs. A concrete harness overrides this to add
        its CLI + runtime, e.g. `return super().toolchain(agent).merge(_MY_TOOLCHAIN)`."""
        return SUBSTRATE

    def required_tools(self, agent: AgentSpec) -> set[str]:
        return set(self.toolchain(agent).probe)

    async def run(self, step: StepSpec, ctx: StepContext, sandbox: Sandbox) -> StepResult:
        if step.agent is None or step.agent not in self._agents:
            raise HarnessError(f"step '{step.id}' has no resolvable agent")
        agent = self._agents[step.agent]

        body = self._renderer.render(step, ctx.event, ctx.upstream)
        prompt = body + self._instruction(step)
        argv = self.build_argv(step, agent, prompt)

        result = await self._exec(argv, step, sandbox)
        if result.exit_code != 0:
            # Surface the transcript: stderr if the CLI separated streams, else stdout (the
            # Daytona sandbox returns combined output there), so a failure isn't a bare code.
            detail = _tail(result.stderr) or _tail(result.stdout) or "(no output)"
            raise HarnessError(f"{self.RUNTIME} exited {result.exit_code}: {detail}")

        message, usage = self._parse_response(result.stdout, step)
        self._enforce_spend(step, usage.cost_usd or 0.0)

        parsed = self._parse_result(message, step)
        return StepResult(
            output=StepOutput(fields=self._extract_output(parsed, step)),
            emits=self._extract_emits(parsed, step),
            usage=usage,
        )

    async def _exec(self, argv: list[str], step: StepSpec, sandbox: Sandbox):
        timeout = BudgetEnforcer.wall_clock_seconds(step.budget)
        try:
            return await asyncio.wait_for(sandbox.exec(argv), timeout=timeout)
        except TimeoutError as exc:
            raise HarnessError(f"step '{step.id}' exceeded wall_clock budget") from exc

    def _enforce_spend(self, step: StepSpec, spent_usd: float) -> None:
        spend = step.budget.spend if step.budget else None
        has_usd_cap = bool(spend) and spend.get("usd") is not None
        if has_usd_cap and not provider(self.RUNTIME).reports_cost:
            # Refusing beats silently passing a budget we can't measure (Codex reports no
            # USD cost — only token usage). Cap wall-clock instead, or pick a harness that
            # reports cost. (The cascade-wide dollar cap is deferred — see cost-budget plan.)
            raise HarnessError(
                f"step '{step.id}': the {self.RUNTIME} harness reports no USD cost, so its "
                "spend budget can't be enforced — use a wall_clock budget instead"
            )
        BudgetEnforcer.check_spend(step.budget, spent_usd)

    # ── subclass hooks ──────────────────────────────────────────────────────────
    def build_argv(self, step: StepSpec, agent: AgentSpec, prompt: str) -> list[str]:
        raise NotImplementedError

    def _parse_response(self, stdout: str, step: StepSpec) -> tuple[str, Usage]:
        """Read the agent's final message text and the invocation's `Usage` (cost_usd when the
        CLI reports one — the signal the cumulative cascade cap accumulates — plus tokens as
        telemetry) out of the CLI's stdout. `cost_usd` is None for harnesses that report no
        dollar figure."""
        raise NotImplementedError

    # ── shared JSON protocol ──────────────────────────────────────────────────────
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
    def _parse_result(result_text: str, step: StepSpec) -> dict:
        if not step.output and not step.emits:
            return {}
        parsed = _extract_json_object(result_text)
        if parsed is None:
            # Tolerant extraction already tried fences + trailing prose; if nothing parsed,
            # show what the agent actually returned so the mismatch is debuggable.
            raise HarnessError(
                f"step '{step.id}': no JSON object in result; got: "
                f"{_tail(result_text) or '(empty)'}"
            )
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
