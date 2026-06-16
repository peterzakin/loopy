"""`loopy compile` — run the pipeline, print diagnostics with file:line, set exit code."""

from __future__ import annotations

from pathlib import Path

import typer

from loopy_core.compile.pipeline import compile_project

app = typer.Typer(add_completion=False, help="Loopy compiler — workflows -> manifest.")


@app.callback()
def main() -> None:
    """Loopy — compile workflows into a validated manifest."""
    # Present so Typer keeps `compile` as an explicit subcommand (no auto-promotion).


@app.command()
def compile(
    path: Path = typer.Argument(Path("."), help="Project directory to compile."),
    out: Path | None = typer.Option(None, "--out", help="Write the manifest JSON to this path."),
) -> None:
    """Compile a Loopy project, reporting every diagnostic."""
    result = compile_project(path)

    for diagnostic in result.diagnostics.items:
        typer.echo(diagnostic.render(), err=True)

    # M5: serialize result.project to `out` as the deterministic manifest.

    raise typer.Exit(code=result.diagnostics.exit_code())


if __name__ == "__main__":  # pragma: no cover
    app()
