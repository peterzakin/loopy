"""Model-provider registry: per-harness model rules.

Each harness `runtime` binds to a model provider and declares two things v1 enforces:

* the env var the sandbox must supply (`ANTHROPIC_API_KEY` / `OPENAI_API_KEY`), and
* which models it is *eligible* to drive — a `claude-code` agent may only name a
  `claude-*` model, a `codex` agent only an OpenAI model. The rule is enforced where
  harnesses are wired (`validate_model`), so a mistyped pairing fails fast at startup
  instead of shelling out to a CLI that would reject the model anyway.

Single-provider runtimes (`claude-code`, `codex`) declare one static `model_key`.
A model-agnostic runtime (`opencode`) names its models `provider/model`
(e.g. `anthropic/claude-sonnet-4-6`), so the required key *derives from the model*:
it declares `prefix_keys` instead, and the model becomes mandatory — with no model
there is no provider prefix to derive auth or eligibility from.

`reports_cost` records whether the harness emits a USD cost we can budget on — Claude
Code does (`total_cost_usd`), and OpenCode prices each step from its model catalog;
Codex emits token usage only (see the cost-budget plan). The budget enforcer uses it
to refuse a spend budget it could only silently no-op.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RuntimeProvider:
    """How a harness `runtime` binds to a model provider."""

    runtime: str  # harness.runtime id (e.g. "claude-code")
    model_prefixes: tuple[str, ...]  # eligible model-id prefixes this runtime may drive
    reports_cost: bool  # does the harness emit a USD cost we can enforce a spend budget on?
    model_key: str | None = None  # env var the sandbox must supply (single-provider runtimes)
    # model prefix -> env var, for runtimes whose key derives from the model's provider
    # prefix (`opencode`). Exactly one of `model_key` / `prefix_keys` is set.
    prefix_keys: tuple[tuple[str, str], ...] = ()

    def is_eligible(self, model: str) -> bool:
        return any(model.startswith(prefix) for prefix in self.model_prefixes)

    def key_for(self, model: str | None) -> str:
        """The env var this runtime needs for `model`. Static-key runtimes ignore the
        model; prefix-keyed runtimes derive it, so they require one (`validate_model`
        rejects a keyless pairing before this is ever reached with None)."""
        if self.model_key is not None:
            return self.model_key
        for prefix, key in self.prefix_keys:
            if model is not None and model.startswith(prefix):
                return key
        raise ValueError(
            f"the {self.runtime!r} harness derives its provider key from the model's "
            f"prefix; model {model!r} matches none of {[p for p, _ in self.prefix_keys]}"
        )

    def all_keys(self) -> frozenset[str]:
        """Every env var this runtime may require, across its providers."""
        static = frozenset({self.model_key}) if self.model_key else frozenset()
        return static | frozenset(key for _, key in self.prefix_keys)


# OpenCode provider prefix -> the env var its driver reads. The v1 set is the providers
# whose keys loopy already recognizes; OpenCode itself supports more (openrouter, google,
# local models) — extending this table is how they get admitted.
_OPENCODE_PREFIX_KEYS: tuple[tuple[str, str], ...] = (
    ("anthropic/", "ANTHROPIC_API_KEY"),
    ("openai/", "OPENAI_API_KEY"),
)

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
    "opencode": RuntimeProvider(
        runtime="opencode",
        model_prefixes=tuple(prefix for prefix, _ in _OPENCODE_PREFIX_KEYS),
        # `opencode run --format json` prices each step_finish event (cost + tokens)
        # from its model catalog — a client-side estimate, like claude's total_cost_usd.
        reports_cost=True,
        prefix_keys=_OPENCODE_PREFIX_KEYS,
    ),
}

# harness.runtime -> the env var the sandbox must supply for that runtime. Covers only
# statically-keyed runtimes; a prefix-keyed runtime's key depends on the agent's model
# (use `required_model_key(runtime, model)`).
REQUIRED_MODEL_KEY: dict[str, str] = {
    r: p.model_key for r, p in PROVIDERS.items() if p.model_key is not None
}

# The full set of recognized model-provider keys (the union across runtimes).
RECOGNIZED_MODEL_KEYS: frozenset[str] = frozenset(
    key for p in PROVIDERS.values() for key in p.all_keys()
)


def provider(runtime: str | None) -> RuntimeProvider:
    """The provider binding for `runtime`. Raises for unknown/unsupported runtimes."""
    if runtime not in PROVIDERS:
        raise ValueError(
            f"unknown harness runtime {runtime!r}; supported: {sorted(PROVIDERS)}"
        )
    return PROVIDERS[runtime]


def required_model_key(runtime: str | None, model: str | None = None) -> str:
    """The provider key a sandbox must supply for `runtime` (and, for prefix-keyed
    runtimes, `model`). Raises for unknown runtimes and underivable pairings."""
    return provider(runtime).key_for(model)


def validate_model(runtime: str | None, model: str | None) -> None:
    """Enforce per-harness model eligibility: `model` (when named) must be one the
    `runtime` is allowed to drive. Raises ValueError on an unknown runtime or an
    ineligible pairing.

    A `None` model is permitted for statically-keyed runtimes — the harness then falls
    back to the runtime's own default model (e.g. whatever `claude`/`codex` picks). A
    prefix-keyed runtime (`opencode`) must name a model: its provider key and
    eligibility both derive from the model's provider prefix."""
    prov = provider(runtime)
    if model is None:
        if prov.model_key is None:
            raise ValueError(
                f"the {runtime!r} harness requires an explicit model in provider/model "
                f"form (e.g. {prov.model_prefixes[0]}...): its provider key derives "
                "from the model's prefix"
            )
        return
    if not prov.is_eligible(model):
        raise ValueError(
            f"model {model!r} is not eligible for the {runtime!r} harness "
            f"(expected a model with prefix {list(prov.model_prefixes)})"
        )
