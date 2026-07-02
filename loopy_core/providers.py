"""Model-provider registry: per-harness model rules.

Each harness id (an agent's `harness` key) binds to a model provider and declares two
things v1 enforces:

* the env var the sandbox must supply (`ANTHROPIC_API_KEY` / `OPENAI_API_KEY`), and
* which models it is *eligible* to drive — a `claude-code` agent may only name a
  `claude-*` model, a `codex` agent only an OpenAI model. The rule (`validate_model`)
  is enforced twice: at compile time (the X2 cross-check, E508) so a mistyped pairing
  never reaches a manifest, and again where harnesses are wired, so a hand-edited
  manifest still fails fast at startup instead of shelling out to a CLI that would
  reject the model anyway.

The registry lives in `loopy_core` because eligibility is part of the manifest
contract; `loopy_runtime.providers` re-exports it for the harnesses.

Every agent names its model explicitly (`validate_model` rejects a missing one; there
is no per-runtime default model). Single-provider runtimes (`claude-code`, `codex`)
declare one static `model_key`. A model-agnostic runtime (`opencode`) authenticates per
provider, so the required key *derives from the model*: it declares `prefix_keys`.
Its CLI names models `provider/model`, but agents may write the bare id every other
runtime uses (`claude-sonnet-4-6`, `gpt-5.5`): `sugar` expands it to the namespaced
form (`anthropic/claude-sonnet-4-6`) everywhere it matters — eligibility, key
derivation, and the argv handed to the CLI (`canonical_model`). An explicit
`provider/model` passes through untouched.

`reports_cost` records whether the harness emits a USD cost we can budget on — Claude
Code does (`total_cost_usd`), and OpenCode prices each step from its model catalog;
Codex emits token usage only (see the cost-budget plan). The budget enforcer uses it
to refuse a spend budget it could only silently no-op.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RuntimeProvider:
    """How a harness id (an agent's `harness` key) binds to a model provider."""

    runtime: str  # agent `harness` id (e.g. "claude-code")
    model_prefixes: tuple[str, ...]  # eligible model-id prefixes this runtime may drive
    reports_cost: bool  # does the harness emit a USD cost we can enforce a spend budget on?
    model_key: str | None = None  # env var the sandbox must supply (single-provider runtimes)
    # model prefix -> env var, for runtimes whose key derives from the model's provider
    # prefix (`opencode`). Exactly one of `model_key` / `prefix_keys` is set.
    prefix_keys: tuple[tuple[str, str], ...] = ()
    # bare-model-id prefixes -> the provider namespace they expand into, so an agent may
    # write the same bare id every runtime uses and have it canonicalized to the
    # runtime's own `provider/model` naming.
    sugar: tuple[tuple[tuple[str, ...], str], ...] = ()

    def canonical(self, model: str | None) -> str | None:
        """`model` in the form this runtime's CLI expects: a bare id covered by `sugar`
        gains its provider namespace; an already-namespaced (or unrecognized) id passes
        through unchanged."""
        if model is None or not self.sugar:
            return model
        if any(model.startswith(prefix) for prefix, _ in self.prefix_keys):
            return model  # already provider/model
        for prefixes, namespace in self.sugar:
            if model.startswith(prefixes):
                return f"{namespace}/{model}"
        return model

    def is_eligible(self, model: str) -> bool:
        model = self.canonical(model) or model
        return any(model.startswith(prefix) for prefix in self.model_prefixes)

    def key_for(self, model: str | None) -> str:
        """The env var this runtime needs for `model`. Static-key runtimes ignore the
        model; prefix-keyed runtimes derive it (sugar expanded first), so an
        underivable pairing raises (`validate_model` rejects it before this is ever
        reached with a model outside the runtime's providers)."""
        if self.model_key is not None:
            return self.model_key
        model = self.canonical(model)
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


# The bare model-id families each provider serves. Shared between the single-provider
# runtimes' eligibility rules and opencode's sugar, so the two never drift.
_ANTHROPIC_MODEL_PREFIXES = ("claude-",)
# OpenAI coding models: the gpt-* family, the o-series reasoning models, the codex-* line.
_OPENAI_MODEL_PREFIXES = ("gpt-", "o1", "o3", "o4", "codex")

# OpenCode provider prefix -> the env var its driver reads. The v1 set is the providers
# whose keys loopy already recognizes; OpenCode itself supports more (openrouter, google,
# local models) — extending this table is how they get admitted.
_OPENCODE_PREFIX_KEYS: tuple[tuple[str, str], ...] = (
    ("anthropic/", "ANTHROPIC_API_KEY"),
    ("openai/", "OPENAI_API_KEY"),
)

# agent `harness` id -> its provider binding. The union of these is the v1 harness set.
PROVIDERS: dict[str, RuntimeProvider] = {
    "claude-code": RuntimeProvider(
        runtime="claude-code",
        model_key="ANTHROPIC_API_KEY",
        model_prefixes=_ANTHROPIC_MODEL_PREFIXES,
        reports_cost=True,  # `claude -p --output-format json` emits total_cost_usd
    ),
    "codex": RuntimeProvider(
        runtime="codex",
        model_key="OPENAI_API_KEY",
        model_prefixes=_OPENAI_MODEL_PREFIXES,
        reports_cost=False,  # `codex exec --json` emits token usage only, no USD cost
    ),
    "opencode": RuntimeProvider(
        runtime="opencode",
        model_prefixes=tuple(prefix for prefix, _ in _OPENCODE_PREFIX_KEYS),
        # `opencode run --format json` prices each step_finish event (cost + tokens)
        # from its model catalog — a client-side estimate, like claude's total_cost_usd.
        reports_cost=True,
        prefix_keys=_OPENCODE_PREFIX_KEYS,
        # Bare ids from the single-provider runtimes work here too: `claude-sonnet-4-6`
        # expands to `anthropic/claude-sonnet-4-6`, `gpt-5.5` to `openai/gpt-5.5`.
        sugar=(
            (_ANTHROPIC_MODEL_PREFIXES, "anthropic"),
            (_OPENAI_MODEL_PREFIXES, "openai"),
        ),
    ),
}

# agent `harness` id -> the env var the sandbox must supply for that runtime. Covers only
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
            f"unknown harness {runtime!r}; supported: {sorted(PROVIDERS)}"
        )
    return PROVIDERS[runtime]


def required_model_key(runtime: str | None, model: str | None = None) -> str:
    """The provider key a sandbox must supply for `runtime` (and, for prefix-keyed
    runtimes, `model`). Raises for unknown runtimes and underivable pairings."""
    return provider(runtime).key_for(model)


def canonical_model(runtime: str | None, model: str | None) -> str | None:
    """`model` as the `runtime`'s CLI expects it — opencode sugar expanded
    (`claude-sonnet-4-6` -> `anthropic/claude-sonnet-4-6`), everything else unchanged.
    Harnesses call this when building the argv."""
    return provider(runtime).canonical(model)


def validate_model(runtime: str | None, model: str | None) -> None:
    """Enforce per-harness model eligibility: every agent must name a model, and it
    must be one the `runtime` is allowed to drive. Raises ValueError on an unknown
    runtime, a missing model, or an ineligible pairing.

    There is no fallback to a runtime's own default model — the agent definition is
    the single place the model is decided, so a manifest never silently tracks
    whatever a CLI happens to default to. A bare id (`claude-sonnet-4-6`) and the
    explicit `provider/model` form both work for opencode."""
    prov = provider(runtime)
    if model is None:
        raise ValueError(
            f"an agent on the {runtime!r} harness must name a model — a bare id "
            "(e.g. claude-sonnet-4-6) or, for opencode, provider/model form"
        )
    if not prov.is_eligible(model):
        raise ValueError(
            f"model {model!r} is not eligible for the {runtime!r} harness "
            f"(expected a model with prefix {list(prov.model_prefixes)})"
        )
