"""Golden test helpers, shared across milestones.

`write_project` materializes an in-memory project for inline negative fixtures;
`compile_fixture` runs the pipeline; `assert_code` asserts an exact diagnostic code
(optionally at a file:line) — the golden-negative contract."""

from __future__ import annotations

from pathlib import Path

from loopy_core.compile.pipeline import CompileResult, compile_project


def write_project(root: Path, files: dict[str, str]) -> Path:
    """Write `{relative_path: content}` under `root` and return it."""
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return root


def compile_fixture(root: Path) -> CompileResult:
    return compile_project(root)


def codes(result: CompileResult) -> list[str]:
    return [d.code for d in result.diagnostics.items]


def assert_code(
    result: CompileResult,
    code: str,
    *,
    file: str | None = None,
    line: int | None = None,
) -> None:
    """Assert at least one diagnostic with `code` (optionally at a given file/line)."""
    matches = [d for d in result.diagnostics.items if d.code == code]
    assert matches, f"expected {code}, got {codes(result)}"
    if file is not None:
        assert any(d.span and d.span.file.endswith(file) for d in matches), (
            f"{code} not reported in {file}: {[str(d.span) for d in matches]}"
        )
    if line is not None:
        assert any(d.span and d.span.line == line for d in matches), (
            f"{code} not reported at line {line}: {[str(d.span) for d in matches]}"
        )
