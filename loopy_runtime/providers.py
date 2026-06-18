"""Model-provider registry: per-harness model rules.

Each harness `runtime` binds to a model provider and declares two things v1 enforces:

* the env var the sandbox must supply (`ANTHROPIC_API_KEY` / `OPENAI_API_KEY`), and
* which models it is *eligible* to drive — a `claude-code` agent may only name a
  `claude-*` model, a `codex` agent only an OpenAI model. The rule is enforced where
  harnesses are wired (`validate_model`), so a mistyped pairing fails fast at startup
  instead of shelling out to a CLI that would reject the model anyway.

`reports_cost` records whether the harness emits a USD cost we can budget on — Claude
Code does (`total_cost_usd`); Codex emits token usage only (see the cost-budget plan).
The budget enforcer uses it to refuse a spend budget it could only silently no-op.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RuntimeProvider:
    """How a harness `runtime` binds to a model provider."""

    runtime: str  # harness.runtime id (e.g. "claude-code")
    model_key: str  # env var the sandbox must supply for this runtime
    model_prefixes: tuple[str, ...]  # eligible model-id prefixes this runtime may drive
    reports_cost: bool  # does the harness emit a USD cost we can enforce a spend budget on?

    def is_eligible(self, model: str) -> bool:
        return any(model.startswith(prefix) for prefix in self.model_prefixes)


# harness.runtime -> its provider binding. The union of these is the v1 harness set.
PROVIDERS: dict[str, RuntimeProvider] = {
    "claude-code": RuntimeProvider(
        runtime="claude-code",
        model_key="ANTHROPIC_API_KEY",
        model_prefixes=("claude-",),
        reports_cost=True,  # `claude -p --output-format json` emits total_cost_usd
    ),
    "codex": RuntimeProvider(
        runtime="codex",
        model_key="OPENAI_API_KEY",
        # OpenAI coding models codex can drive: the gpt-* family, the o-series
        # reasoning models, and the codex-* line.
        model_prefixes=("gpt-", "o1", "o3", "o4", "codex"),
        reports_cost=False,  # `codex exec --json` emits token usage only, no USD cost
    ),
}

# harness.runtime -> the env var the sandbox must supply for that runtime.
REQUIRED_MODEL_KEY: dict[str, str] = {r: p.model_key for r, p in PROVIDERS.items()}

# The full set of recognized model-provider keys (the union across runtimes).
RECOGNIZED_MODEL_KEYS: frozenset[str] = frozenset(p.model_key for p in PROVIDERS.values())


def provider(runtime: str | None) -> RuntimeProvider:
    """The provider binding for `runtime`. Raises for unknown/unsupported runtimes."""
    if runtime not in PROVIDERS:
        raise ValueError(
            f"unknown harness runtime {runtime!r}; supported: {sorted(PROVIDERS)}"
        )
    return PROVIDERS[runtime]


def required_model_key(runtime: str | None) -> str:
    """The provider key a sandbox must supply for `runtime`. Raises for unknown runtimes."""
    return provider(runtime).model_key


def validate_model(runtime: str | None, model: str | None) -> None:
    """Enforce per-harness model eligibility: `model` (when named) must be one the
    `runtime` is allowed to drive. Raises ValueError on an unknown runtime or an
    ineligible pairing.

    A `None` model is permitted — the harness then falls back to the runtime's own
    default model (e.g. whatever `claude`/`codex` picks)."""
    prov = provider(runtime)
    if model is None:
        return
    if not prov.is_eligible(model):
        raise ValueError(
            f"model {model!r} is not eligible for the {runtime!r} harness "
            f"(expected a model with prefix {list(prov.model_prefixes)})"
        )
