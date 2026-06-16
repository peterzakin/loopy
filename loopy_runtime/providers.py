"""Model-provider registry: which API key a harness runtime requires.

v1 ships only `claude-code`. `OPENAI_API_KEY` is reserved for a future `codex`/`openai`
runtime so the seam exists now; the harness for it is not built yet.
"""

from __future__ import annotations

# harness.runtime -> the env var the sandbox must supply for that runtime.
REQUIRED_MODEL_KEY: dict[str, str] = {
    "claude-code": "ANTHROPIC_API_KEY",
    # "codex" / "openai": "OPENAI_API_KEY"  # reserved — harness not built in v1
}

# The full set of recognized model-provider keys (the union across runtimes).
RECOGNIZED_MODEL_KEYS: frozenset[str] = frozenset({"ANTHROPIC_API_KEY", "OPENAI_API_KEY"})


def required_model_key(runtime: str | None) -> str:
    """The provider key a sandbox must supply for `runtime`. Raises for unknown runtimes."""
    if runtime not in REQUIRED_MODEL_KEY:
        raise ValueError(
            f"unknown harness runtime {runtime!r}; supported: {sorted(REQUIRED_MODEL_KEY)}"
        )
    return REQUIRED_MODEL_KEY[runtime]
