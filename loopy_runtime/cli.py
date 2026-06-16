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
from loopy_runtime.manifest_model import Manifest, SensorSpec, load_manifest
from loopy_runtime.payloads import synthesize_fields
from loopy_runtime.runtime.inmemory import InMemoryRuntime
from loopy_runtime.sandbox.local import LocalSandboxProvider
from loopy_runtime.secrets import EnvFileSecretsResolver
from loopy_runtime.sensors.host import FastAPISensorHost
from loopy_runtime.sensors.loader import load_webhook_sensor

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
        harness=ClaudeCodeHarness(m.registry.agents, m.registry.events),
        sandboxes=LocalSandboxProvider(),
        secrets=EnvFileSecretsResolver(root),
        bus=InProcessEventBus(),
    )
    try:
        run_id = asyncio.run(runtime.trigger(_load_event(event, fields)))
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    if run_id is None:
        typer.echo(f"no workflow subscribes to event '{event}'", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"run: {run_id}")
    typer.echo(f"steps: {' -> '.join(runtime.execution_log)}")
    typer.echo(f"emitted: {', '.join(runtime.emitted_log) or '(none)'}")


def _synthesizing_publisher(manifest: Manifest, sensor: SensorSpec):
    """v1 webhook handler: publish the sensor's declared `emits` event, fields
    synthesized from the contract (merged with the request payload where keys match).
    Executing the user's sensor *function* is a later refinement (needs the authoring shim)."""
    contract = manifest.registry.events.get(sensor.emits)
    schema = contract.fields if contract else {}
    seq = {"n": 0}

    def publisher(payload: dict) -> Event:
        seq["n"] += 1
        return Event(
            name=sensor.emits,
            fields=synthesize_fields(schema, payload or {}),
            id=f"sensor-{sensor.name}-{seq['n']}",
            emitted_at=datetime.now(UTC),
        )

    return publisher


@app.command()
def dev(
    manifest: Path = typer.Argument(..., help="Path to manifest.json."),
    root: Path = typer.Option(Path("."), "--root", help="Project root (for env_file resolution)."),
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port"),
) -> None:
    """Serve sensor webhooks; published events drive workflow runs in-process."""
    m = load_manifest(manifest)
    runtime = InMemoryRuntime(
        m,
        harness=ClaudeCodeHarness(m.registry.agents, m.registry.events),
        sandboxes=LocalSandboxProvider(),
        secrets=EnvFileSecretsResolver(root),
        bus=InProcessEventBus(),
    )
    # The host injects sensor events via trigger (publish + drain the cascade).
    sensor_host = FastAPISensorHost(runtime.trigger)
    for sensor in m.sensors:
        if sensor.trigger.kind != "webhook" or not sensor.trigger.path:
            continue
        try:
            fn = load_webhook_sensor(sensor, root)  # run the real @sensor function
        except Exception as exc:  # noqa: BLE001 - any import/resolve failure degrades gracefully
            typer.echo(
                f"warning: sensor '{sensor.name}' not loadable ({exc}); "
                "falling back to synthesized events",
                err=True,
            )
            fn = _synthesizing_publisher(m, sensor)
        sensor_host.register_webhook(sensor.trigger.path, fn)
    typer.echo(
        f"serving {len(sensor_host.webhook_paths)} webhook(s): "
        f"{', '.join(sensor_host.webhook_paths) or '(none)'}"
    )
    asyncio.run(sensor_host.start(host, port))  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    app()
