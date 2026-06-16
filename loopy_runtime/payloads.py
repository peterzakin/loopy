"""Deterministic value synthesis from JSON Schema fragments.

Used by the runtime to build emitted-event payloads from the event contract, and by
the test `StubAgentHarness` to build step outputs. **v1 simplification:** emitted-event
fields are synthesized from the registry contract (with real step-output values where
field names match); a future refinement lets the agent produce the payload directly.
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
        if fmt == "loopy-id":
            return "stub-id"
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
