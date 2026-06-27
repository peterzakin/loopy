"""Deterministic value synthesis from JSON Schema fragments.

Used by the test `StubAgentHarness` (output + emit payloads) and the `loopy dev`
webhook stub. The runtime no longer synthesizes emitted payloads — per decision #3=B
the agent produces them; this is only for offline/deterministic stand-ins.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def default_value_for_schema(schema: dict) -> Any:
    kind = schema.get("type")
    if kind == "string":
        if schema.get("enum"):
            return schema["enum"][0]
        fmt = schema.get("format")
        if fmt == "uri":
            return "https://example.test"
        return "stub"
    if kind == "integer":
        return 0
    if kind == "number":
        return 0.0
    if kind == "boolean":
        return False
    if kind == "array":
        return []
    if kind == "object":
        return {}
    return "stub"


def synthesize_fields(
    contract_fields: Mapping[str, dict], overrides: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Build a value per contract field: a matching override wins, else a stub value."""
    overrides = overrides or {}
    return {
        name: overrides[name] if name in overrides else default_value_for_schema(schema)
        for name, schema in contract_fields.items()
    }
