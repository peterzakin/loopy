"""Re-export of the model-provider registry.

The registry itself lives in `loopy_core.providers`: the eligibility rule (which
models each harness may drive) is part of the manifest contract, so the compiler
enforces it at compile time (E508) and the harnesses re-enforce it fail-fast at
construction. This module keeps the runtime's historical import path working.
"""

from __future__ import annotations

from loopy_core.providers import (
    PROVIDERS,
    RECOGNIZED_MODEL_KEYS,
    REQUIRED_MODEL_KEY,
    RuntimeProvider,
    canonical_model,
    provider,
    required_model_key,
    validate_model,
)

__all__ = [
    "PROVIDERS",
    "RECOGNIZED_MODEL_KEYS",
    "REQUIRED_MODEL_KEY",
    "RuntimeProvider",
    "canonical_model",
    "provider",
    "required_model_key",
    "validate_model",
]
