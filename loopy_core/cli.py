"""`loopy compile` — run the pipeline, print diagnostics with file:line, set exit code.

On a clean compile it writes the deterministic manifest (when --out is given) with
`compiled_at`/`loopy_version` stamped outside the hashed core, and generates the
`loopy.events` typing module into the project.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path

import typer

from loopy_core import __version__
from loopy_core.compile.manifest import to_manifest
from loopy_core.compile.pipeline import compile_project
from loopy_core.events.codegen import write_events

app = typer.Typer(add_completion=False, help="Loopy compiler — workflows -> manifest.")


@app.callback()
def main() -> None:
    """Loopy — compile workflows into a validated manifest."""
    # Present so Typer keeps `compile` as an explicit subcommand (no auto-promotion).


def _write_manifest(project, out: Path) -> None:
    manifest = to_manifest(project)
    # Stamp non-hashed provenance fields outside the deterministic core.
    manifest["compiled_at"] = datetime.datetime.now(datetime.UTC).isoformat()
    manifest["loopy_version"] = __version__
    out.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


@app.command()
def compile(
    path: Path = typer.Argument(Path("."), help="Project directory to compile."),
    out: Path | None = typer.Option(None, "--out", help="Write the manifest JSON to this path."),
) -> None:
    """Compile a Loopy project, reporting every diagnostic."""
    result = compile_project(path)

    for diagnostic in result.diagnostics.items:
        typer.echo(diagnostic.render(), err=True)

    if not result.diagnostics.has_errors() and result.project is not None:
        write_events(result.project.registry, path)
        if out is not None:
            _write_manifest(result.project, out)

    raise typer.Exit(code=result.diagnostics.exit_code())


if __name__ == "__main__":  # pragma: no cover
    app()
