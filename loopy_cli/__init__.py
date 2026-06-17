"""Loopy CLI — one binary over both halves.

    loopy compile   produce the manifest (frontend)
    loopy run       start the server: host sensor webhooks; events drive workflow runs
    loopy trigger   fire one event at the manifest and run it to completion (for testing)

Heavy deps are imported lazily per command so `loopy compile` stays runtime-free.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

app = typer.Typer(add_completion=False, help="Loopy — compile and run durable agent workflows.")


@app.callback()
def main() -> None:
    """Author, compile, and run durable agent workflows."""


@app.command()
def compile(
    path: Path = typer.Argument(Path("."), help="Project directory to compile."),
    out: Path | None = typer.Option(None, "--out", help="Write the manifest JSON to this path."),
) -> None:
    """Compile a project to a validated manifest (and generate loopy.events)."""
    import datetime

    from loopy_core import __version__
    from loopy_core.compile.manifest import to_manifest
    from loopy_core.compile.pipeline import compile_project
    from loopy_core.events.codegen import write_events

    result = compile_project(path)
    for diagnostic in result.diagnostics.items:
        typer.echo(diagnostic.render(), err=True)

    if not result.diagnostics.has_errors() and result.project is not None:
        write_events(result.project.registry, path)
        if out is not None:
            manifest = to_manifest(result.project)
            manifest["compiled_at"] = datetime.datetime.now(datetime.UTC).isoformat()
            manifest["loopy_version"] = __version__
            out.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    raise typer.Exit(code=result.diagnostics.exit_code())


@app.command()
def run(
    manifest: Path = typer.Argument(..., help="Path to manifest.json."),
    root: Path = typer.Option(Path("."), "--root", help="Project root (for env_file + sensors)."),
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port"),
    sandbox: str = typer.Option("local", "--sandbox", help="Sandbox provider: local | daytona."),
) -> None:
    """Start the Loopy server: host sensor webhooks; incoming events drive workflow runs."""
    import asyncio

    from loopy_runtime.bus.inproc import InProcessEventBus
    from loopy_runtime.harness.claude_code import ClaudeCodeHarness
    from loopy_runtime.manifest_model import load_manifest
    from loopy_runtime.receiver import LocalEventReceiver
    from loopy_runtime.runtime.inmemory import InMemoryRuntime
    from loopy_runtime.sandbox.factory import make_sandbox_provider
    from loopy_runtime.secrets import EnvFileSecretsResolver
    from loopy_runtime.sensors.loader import load_poll_sensor, load_webhook_sensor
    from loopy_runtime.sensors.runner import FastAPISensorRunner, synthesizing_publisher
    from loopy_runtime.sensors.scheduler import PollScheduler, parse_interval

    m = load_manifest(manifest)
    bus = InProcessEventBus()  # shared: the receiver publishes to it, the runtime consumes
    runtime = InMemoryRuntime(
        m,
        harness=ClaudeCodeHarness(m.registry.agents, m.registry.events),
        sandboxes=make_sandbox_provider(sandbox),
        secrets=EnvFileSecretsResolver(root),
        bus=bus,
    )
    receiver = LocalEventReceiver(bus, m.registry.events)  # shared gate for webhooks + polls
    sensor_runner = FastAPISensorRunner(receiver)
    for sensor in m.sensors:
        if sensor.trigger.kind != "webhook" or not sensor.trigger.path:
            continue
        try:
            fn = load_webhook_sensor(sensor, root)  # run the real @sensor function
        except Exception as exc:  # noqa: BLE001 - any load failure degrades gracefully
            typer.echo(
                f"warning: sensor '{sensor.name}' not loadable ({exc}); synthesizing events",
                err=True,
            )
            fn = synthesizing_publisher(m, sensor)
        sensor_runner.register_webhook(sensor.trigger.path, fn)

    # Poll sensors run on the in-process scheduler, sharing the runtime's StateStore for
    # watermarks and the same receiver -> bus -> serve() delivery path as webhooks.
    scheduler = PollScheduler(receiver=receiver, state=runtime.state)
    for sensor in m.sensors:
        if sensor.trigger.kind != "poll" or not sensor.trigger.interval:
            continue
        try:
            poll_fn = load_poll_sensor(sensor, root)
            interval = parse_interval(sensor.trigger.interval)
        except Exception as exc:  # noqa: BLE001 - a bad poll sensor is skipped, not fatal
            typer.echo(
                f"warning: poll sensor '{sensor.name}' not loadable ({exc}); skipping", err=True
            )
            continue
        scheduler.register(sensor.name, interval, poll_fn)

    typer.echo(
        f"serving {len(sensor_runner.webhook_paths)} webhook(s) on {host}:{port}: "
        f"{', '.join(sensor_runner.webhook_paths) or '(none)'}"
    )
    typer.echo(
        f"polling {len(scheduler.poll_names)} sensor(s): "
        f"{', '.join(scheduler.poll_names) or '(none)'}"
    )

    async def _serve() -> None:  # pragma: no cover - exercised by running the server
        consumer = asyncio.create_task(runtime.serve())  # drain runs in the background
        poller = asyncio.create_task(scheduler.start())  # poll sensors fire on their tasks
        try:
            await sensor_runner.start(host, port)
        finally:
            consumer.cancel()
            poller.cancel()

    asyncio.run(_serve())  # pragma: no cover


@app.command()
def trigger(
    manifest: Path = typer.Argument(..., help="Path to manifest.json."),
    event: str = typer.Option(..., "--event", help="Triggering event name."),
    fields: str | None = typer.Option(None, "--fields", help="Event fields as a JSON object."),
    root: Path = typer.Option(Path("."), "--root", help="Project root (for env_file resolution)."),
    sandbox: str = typer.Option("local", "--sandbox", help="Sandbox provider: local | daytona."),
) -> None:
    """Fire one event at the manifest and run the cascade to completion (for testing)."""
    import asyncio
    from datetime import UTC, datetime

    from loopy_runtime.bus.inproc import InProcessEventBus
    from loopy_runtime.contract import Event
    from loopy_runtime.harness.claude_code import ClaudeCodeHarness
    from loopy_runtime.manifest_model import load_manifest
    from loopy_runtime.runtime.inmemory import InMemoryRuntime
    from loopy_runtime.sandbox.factory import make_sandbox_provider
    from loopy_runtime.secrets import EnvFileSecretsResolver

    m = load_manifest(manifest)
    triggering = Event(
        name=event,
        fields=json.loads(fields) if fields else {},
        id="trigger",
        emitted_at=datetime.now(UTC),
    )
    try:
        runtime = InMemoryRuntime(
            m,
            harness=ClaudeCodeHarness(m.registry.agents, m.registry.events),
            sandboxes=make_sandbox_provider(sandbox),
            secrets=EnvFileSecretsResolver(root),
            bus=InProcessEventBus(),
        )
        run_id = asyncio.run(runtime.trigger(triggering))
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    if run_id is None:
        typer.echo(f"no workflow subscribes to event '{event}'", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"run: {run_id}")
    typer.echo(f"steps: {' -> '.join(runtime.execution_log)}")
    typer.echo(f"emitted: {', '.join(runtime.emitted_log) or '(none)'}")
    if runtime.failed_runs:  # a recorded run failure → report and exit non-zero
        for status in runtime.failed_runs:
            typer.echo(f"FAILED {status.run_id}: {status.error}", err=True)
        raise typer.Exit(code=1)


if __name__ == "__main__":  # pragma: no cover
    app()
