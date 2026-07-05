"""The built-in provider registry — runtime behavior keyed by provider name.

`loopy_core/builtins.py` owns the built-in *contracts* and the `BUILTIN_PROVIDERS` metadata
the compiler, `loopy doctor`, and `loopy integrations` read. This module owns the runtime
*behavior* keyed by the same provider names (the `Sensor.provider` discriminator): the payload
mappers and the ingress verifier factory for each provider.

Two providers were fine to hand-merge at each call site; three is the point to centralize, so
a fourth provider is one entry in each table below rather than another `{**a, **b, **c}` at the
loader and the CLI. The Sentry plan flagged this seam
(`docs/plans/integrations/sentry.md`, "Minimal generalization"); the Datadog work lands it.

Verifiers are not uniform: GitHub and Sentry verify an HMAC over the raw body, while Datadog
compares a shared secret in a header (its webhook signs no body). Both shapes satisfy the
runner's `verify(body, headers)` seam, so a single `VERIFIER_FACTORY_BY_PROVIDER` table holds
them side by side.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from loopy_runtime.scm.datadog_builtins import MAPPERS as _datadog_mappers
from loopy_runtime.scm.datadog_webhook import token_verifier as _datadog_verifier
from loopy_runtime.scm.github_builtins import BUILTIN_MAPPERS as _github_mappers
from loopy_runtime.scm.github_webhook import signature_verifier as _github_verifier
from loopy_runtime.scm.sentry_builtins import MAPPERS as _sentry_mappers
from loopy_runtime.scm.sentry_webhook import signature_verifier as _sentry_verifier

# payload -> field dict, or None when the delivery isn't this event's concern.
Mapper = Callable[[dict], "dict | None"]
# secret -> verify(body, headers), raising SignatureError on a bad delivery.
Verifier = Callable[[bytes, Mapping[str, str]], None]
VerifierFactory = Callable[[str], Verifier]

# provider name (Sensor.provider) -> {event name -> payload mapper}
MAPPERS_BY_PROVIDER: dict[str, dict[str, Mapper]] = {
    "github": _github_mappers,
    "sentry": _sentry_mappers,
    "datadog": _datadog_mappers,
}

# provider name -> factory that builds the ingress verifier from that provider's secret.
VERIFIER_FACTORY_BY_PROVIDER: dict[str, VerifierFactory] = {
    "github": _github_verifier,
    "sentry": _sentry_verifier,
    "datadog": _datadog_verifier,
}


def mapper_for(provider: str, emits: str) -> Mapper | None:
    """The payload mapper for a built-in event, or None if the provider/event is unknown."""
    return MAPPERS_BY_PROVIDER.get(provider, {}).get(emits)
