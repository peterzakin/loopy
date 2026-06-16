"""Source spans — every IR node (except derived nodes) carries one so diagnostics
can report `file:line`."""

from __future__ import annotations

from pydantic import BaseModel


class Span(BaseModel):
    """A location in a source file. `line`/`col` are 1-based; 0 means unknown."""

    file: str
    line: int = 0
    col: int = 0

    def __str__(self) -> str:  # pragma: no cover - convenience
        if self.line:
            return f"{self.file}:{self.line}"
        return self.file


def span_at(file: str, line: int = 0, col: int = 0) -> Span:
    """Helper to attach a span to a node."""
    return Span(file=file, line=line, col=col)
