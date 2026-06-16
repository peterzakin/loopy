"""`loopy-run` CLI — execute a manifest against a triggering event.

Wires the real `ClaudeCodeHarness` + local sandbox + env-file secrets + in-process
bus. v1 is single-process and non-durable; one event runs to completion (with the
cross-workflow cascade) and the result is printed.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import typer

from loopy_runtime.bus.inproc import InProcessEventBus
from loopy_runtime.contract import Event
from loopy_runtime.harness.claude_code import ClaudeCodeHarness
from loopy_runtime.manifest_model import load_manifest
from loopy_runtime.runtime.inmemory import InMemoryRuntime
from loopy_runtime.sandbox.local import LocalSandboxProvider
from loopy_runtime.secrets import EnvFileSecretsResolver

app = typer.Typer(add_completion=False, help="Loopy runtime — run a manifest against an event.")


@app.callback()
def main() -> None:
    """Loopy runtime."""


def _load_event(name: str, fields_json: str | None) -> Event:
    fields = json.loads(fields_json) if fields_json else {}
    return Event(name=name, fields=fields, id="trigger", emitted_at=datetime.now(UTC))


@app.command()
def run(
    manifest: Path = typer.Argument(..., help="Path to manifest.json."),
    event: str = typer.Option(..., "--event", help="Triggering event name."),
    fields: str | None = typer.Option(None, "--fields", help="Event fields as a JSON object."),
    root: Path = typer.Option(Path("."), "--root", help="Project root (for env_file resolution)."),
) -> None:
    """Trigger `--event` against the manifest and run it to completion."""
    m = load_manifest(manifest)
    runtime = InMemoryRuntime(
        m,
        harness=ClaudeCodeHarness(m.registry.agents),
        sandboxes=LocalSandboxProvider(),
        secrets=EnvFileSecretsResolver(root),
        bus=InProcessEventBus(),
    )
    run_id = asyncio.run(runtime.trigger(_load_event(event, fields)))
    if run_id is None:
        typer.echo(f"no workflow subscribes to event '{event}'", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"run: {run_id}")
    typer.echo(f"steps: {' -> '.join(runtime.execution_log)}")
    typer.echo(f"emitted: {', '.join(runtime.emitted_log) or '(none)'}")


if __name__ == "__main__":  # pragma: no cover
    app()
