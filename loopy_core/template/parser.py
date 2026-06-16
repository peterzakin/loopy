"""P5 — extract `{{ producer.field }}` refs from a step body with accurate spans.

The grammar is substitution-only: no `{% %}` control flow, no filters/expressions,
and exactly two segments (flat keys, T5). Anything else -> E301.
"""

from __future__ import annotations

import re

from loopy_core.compile import codes
from loopy_core.compile.diagnostics import DiagnosticCollector
from loopy_core.span import span_at
from loopy_core.workflow.model import Ref

# Match either a {{ ... }} substitution or a {% ... %} control-flow tag (rejected).
_TAG_RE = re.compile(r"\{\{(?P<sub>.*?)\}\}|\{%(?P<ctrl>.*?)%\}", re.DOTALL)
# A legal ref is exactly two bare identifiers joined by a single dot.
_REF_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*$")


def extract_refs(
    body: str, body_start_line: int, file: str, diags: DiagnosticCollector
) -> list[Ref]:
    refs: list[Ref] = []
    for match in _TAG_RE.finditer(body):
        line = body_start_line + body[: match.start()].count("\n")
        span = span_at(file, line)
        raw = match.group(0)

        if match.group("ctrl") is not None:
            diags.error(
                codes.E301,
                f"control-flow tags are not allowed in step bodies: {raw!r}",
                span=span,
                hint="bodies support only {{ producer.field }} substitution",
            )
            continue

        inner = match.group("sub").strip()
        if not _REF_RE.match(inner):
            diags.error(
                codes.E301,
                f"illegal template reference: {raw!r}",
                span=span,
                hint="use exactly {{ producer.field }} — no filters, expressions, or dotted paths",
            )
            continue

        producer, field = inner.split(".", 1)
        refs.append(Ref(producer=producer, field=field, raw=raw, span=span))
    return refs
