"""Never-fail-fast diagnostics. The collector records errors/warnings and never
raises; the run reports all of them and exits nonzero iff any are errors."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel

from loopy_core.span import Span


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


class Diagnostic(BaseModel):
    severity: Severity
    code: str
    message: str
    span: Span | None = None
    hint: str | None = None

    def render(self) -> str:
        loc = str(self.span) if self.span else "<unknown>"
        line = f"{self.severity.value} {self.code} {loc}: {self.message}"
        if self.hint:
            line += f"\n  hint: {self.hint}"
        return line


class DiagnosticCollector:
    """Accumulates diagnostics across all passes without ever raising."""

    def __init__(self) -> None:
        self._items: list[Diagnostic] = []

    def add(self, diagnostic: Diagnostic) -> None:
        self._items.append(diagnostic)

    def error(
        self, code: str, message: str, span: Span | None = None, hint: str | None = None
    ) -> None:
        self.add(
            Diagnostic(severity=Severity.ERROR, code=code, message=message, span=span, hint=hint)
        )

    def warning(
        self, code: str, message: str, span: Span | None = None, hint: str | None = None
    ) -> None:
        self.add(
            Diagnostic(severity=Severity.WARNING, code=code, message=message, span=span, hint=hint)
        )

    @property
    def items(self) -> list[Diagnostic]:
        return list(self._items)

    @property
    def errors(self) -> list[Diagnostic]:
        return [d for d in self._items if d.severity is Severity.ERROR]

    def has_errors(self) -> bool:
        return any(d.severity is Severity.ERROR for d in self._items)

    def exit_code(self) -> int:
        """0 if no errors, 1 otherwise. Warnings don't fail the build."""
        return 1 if self.has_errors() else 0
