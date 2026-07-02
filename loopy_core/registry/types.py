"""P2 — the field-type desugarer (terse forms -> JSON Schema, draft 2020-12).

We own no type semantics: terse forms are sugar over JSON Schema, and raw schema
objects (dicts) pass through untouched. Reused for step `output:`
maps in M2. Unknown bare shorthand -> E201.
"""

from __future__ import annotations

from loopy_core.compile import codes
from loopy_core.compile.diagnostics import DiagnosticCollector
from loopy_core.span import Span

_SIMPLE: dict[str, dict] = {
    "str": {"type": "string"},
    "int": {"type": "integer"},
    "float": {"type": "number"},
    "bool": {"type": "boolean"},
    "url": {"type": "string", "format": "uri"},
}


def desugar(shorthand: object, *, span: Span, diags: DiagnosticCollector) -> dict | None:
    """Desugar a terse field type to a JSON Schema fragment, or pass through a raw
    schema object. Unknown bare shorthand -> E201 (returns None)."""
    # Raw JSON Schema object: pass through untouched.
    if isinstance(shorthand, dict):
        return dict(shorthand)

    if isinstance(shorthand, str):
        text = shorthand.strip()
        if text in _SIMPLE:
            return dict(_SIMPLE[text])
        if text.startswith("enum[") and text.endswith("]"):
            inner = text[len("enum[") : -1]
            values = [v.strip() for v in inner.split(",") if v.strip()]
            return {"type": "string", "enum": values}

    diags.error(
        codes.E201,
        f"unknown type shorthand: {shorthand!r}",
        span=span,
        hint="use one of str/int/float/bool/url/enum[...], or inline a JSON Schema object",
    )
    return None
