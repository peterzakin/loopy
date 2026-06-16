"""P2 — the field-type desugarer (terse forms -> JSON Schema, draft 2020-12).

We own no type semantics: terse forms are sugar, raw schema objects pass through.
Implemented in M1; reused for step `output:` maps in M2.
"""

from __future__ import annotations

from loopy_core.compile.diagnostics import DiagnosticCollector
from loopy_core.span import Span


def desugar(shorthand: object, *, span: Span, diags: DiagnosticCollector) -> dict | None:
    """Desugar a terse field type to a JSON Schema fragment, or pass through a raw
    schema object. Unknown bare shorthand -> E201 (None returned). Implemented in M1."""
    raise NotImplementedError("registry/types.desugar is implemented in M1")
